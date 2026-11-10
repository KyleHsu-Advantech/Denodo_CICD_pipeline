#!/usr/bin/env python3
"""Parse and validate a descvqlbatch response before it enters the deploy pipeline.

Pipeline position:  DESC API  ->  desc_response.py  ->  vql_transform.py  ->  SM loadFromVQL

Usage:
    python desc_response.py desc.json --out merged.vql [--dev-marker ACLDTPLTFRM-DEV]
                            [--datasources allow|forbid] [--create-datasources '*'|'-'|db.ds,...]

Exit code 0 = VQL written (warnings may be printed), 1 = validation failed.
Output lines use the Azure DevOps ##[warning] / ##[error] prefixes.
"""
import argparse
import json
import re
import sys
from dataclasses import dataclass, field

OK_STATUSES = {"OK", "MERGED"}

# Statement starters at column 0. CLOSE and '#' comments are not counted; this
# matches how the DESC API computes statement_count.
STMT_RE = re.compile(r"^(?:CREATE|ALTER|CONNECT|DROP)\b", re.M)
DB_BLOCK_RE = re.compile(r"^CONNECT DATABASE (\w+);[ \t]*$(.*?)^CLOSE;[ \t]*$", re.M | re.S)
OBJ_RE = re.compile(r'^CREATE OR REPLACE (?:VIEW|TABLE) "?([\w$]+)"?', re.M)
DS_START_RE = re.compile(r"^CREATE (?:OR REPLACE )?DATASOURCE\s+\S+\s+\"?([\w$]+)\"?")
STMT_START_RE = re.compile(r"^(?:CREATE|ALTER|CONNECT|CLOSE|DROP)\b|^#")
CONNECT_RE = re.compile(r"^CONNECT DATABASE (\w+);")
# Credentials may appear only inside DATASOURCE statements, and only encrypted
# (case-sensitive: column names such as "Password" are fine).
SECRET_RE = re.compile(r"\bENCRYPTED\b|\b(?:USER)?PASSWORD\b\s*=?\s*'")
# One PASSWORD literal: group 1 = the quoted value, group 2 = an ENCRYPTED flag right after it.
PASSWORD_LITERAL_RE = re.compile(r"\b(?:USER)?PASSWORD\b\s*=?\s*'([^']*)'(\s*ENCRYPTED\b)?")
# Parameterized VQL (DESC VQL ... 'includeProperties' = 'yes') uses ${variable} placeholders.
PLACEHOLDER_RE = re.compile(r"\$\{([^}]+)\}")
REQUIRES_PROPS_RE = re.compile(r"^#\s*REQUIRES-PROPERTIES-FILE\b", re.M)


class DescError(ValueError):
    """The DESC response must not be deployed."""


@dataclass
class DescResult:
    vql: str
    requested: list = field(default_factory=list)
    excluded: list = field(default_factory=list)
    statement_count: int = 0
    warnings: list = field(default_factory=list)
    datasources: list = field(default_factory=list)           # every datasource DESC brought out
    stripped_datasources: list = field(default_factory=list)  # removed from the VQL (PRD keeps its own)
    properties: str = None        # properties file text that goes with a parameterized VQL
    parameterized: bool = False   # VQL uses ${variables} and needs a properties file
    variables: list = field(default_factory=list)             # ${variables} left in the final VQL


def _split_names(value):
    return [n.strip() for n in (value or "").split(";") if n.strip()]


def _objects_by_db(vql):
    objs = {}
    for m in DB_BLOCK_RE.finditer(vql):
        objs.setdefault(m.group(1).lower(), set()).update(
            n.lower() for n in OBJ_RE.findall(m.group(2))
        )
    return objs


def _split_datasources(vql):
    """Split the VQL into (text without DATASOURCE statements, [(name, statement_text)])."""
    rest, blocks, cur = [], [], None
    for line in vql.splitlines(keepends=True):
        m = DS_START_RE.match(line)
        if m:
            cur = (m.group(1), [line])
            blocks.append(cur)
            continue
        if cur is not None and not STMT_START_RE.match(line):
            cur[1].append(line)
            continue
        cur = None
        rest.append(line)
    return "".join(rest), [(n, "".join(ls)) for n, ls in blocks]


def _has_plaintext_password(block):
    """True if a PASSWORD literal is neither a ${placeholder} nor flagged ENCRYPTED."""
    for m in PASSWORD_LITERAL_RE.finditer(block):
        value, encrypted = m.group(1), m.group(2)
        if PLACEHOLDER_RE.fullmatch(value) or encrypted:
            continue
        return True
    return False


def parse_properties(text):
    """key=value lines of a properties file -> dict (comments and blank lines ignored)."""
    props = {}
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or line[0] in "#!":
            continue
        key, sep, value = line.partition("=")
        if sep:
            props[key.strip()] = value.strip()
    return props


def _filter_properties(text, used_keys):
    """Keep comments/blank lines and only the properties the final VQL still references."""
    out = []
    for raw in text.splitlines(keepends=True):
        line = raw.strip()
        if not line or line[0] in "#!" or "=" not in line:
            out.append(raw)
        elif line.partition("=")[0].strip() in used_keys:
            out.append(raw)
    return "".join(out)


def _strip_datasources(vql, keep):
    """Remove DATASOURCE statements for which keep(db, name) is False.

    Returns (new_vql, kept_names, stripped_names); names are 'db.ds'.
    """
    out, kept, stripped = [], [], []
    db, skipping = None, False
    for line in vql.splitlines(keepends=True):
        c = CONNECT_RE.match(line)
        if c:
            db = c.group(1)
        m = DS_START_RE.match(line)
        if m:
            full = "%s.%s" % (db, m.group(1)) if db else m.group(1)
            if keep(db, m.group(1)):
                kept.append(full)
                skipping = False
            else:
                stripped.append(full)
                skipping = True
                continue
        elif skipping:
            if STMT_START_RE.match(line):
                skipping = False
            else:
                continue
        out.append(line)
    return "".join(out), kept, stripped


def parse_desc_response(payload, dev_marker="ACLDTPLTFRM-DEV", strict_count=True,
                        datasources="allow", create_datasources=None):
    """datasources='allow': the VQL may recreate datasources (SM then applies PRD property
    values where defined). datasources='forbid': any DATASOURCE statement is rejected.

    create_datasources (list or None) decides which datasources stay in the VQL:
      None / ['*']   all stay (SM environment properties decide the PRD values)
      []             all removed (PRD keeps whatever it already has)
      ['db.ds', ...] only the listed ones stay ('ds' alone also matches); the rest are removed
    """
    if not isinstance(payload, dict) or payload.get("name") != "descvqlbatch":
        raise DescError("unexpected payload: top-level name is not 'descvqlbatch'")
    elements = payload.get("elements")
    if not elements:
        raise DescError("response has no elements")

    # 1. every element must be OK / MERGED
    bad = [e for e in elements if e.get("status") not in OK_STATUSES]
    if bad:
        detail = "; ".join(
            f"{e.get('requested_name') or '<merged>'}: {e.get('status')} - {e.get('out_message')}"
            for e in bad
        )
        raise DescError(f"DESC reported failures: {detail}")

    # 2. exactly one MERGED element carrying the VQL
    merged = [e for e in elements if e.get("status") == "MERGED"]
    if len(merged) != 1:
        raise DescError(f"expected exactly 1 MERGED element, got {len(merged)}")
    merged = merged[0]
    vql = merged.get("vql")
    if not vql or not vql.strip():
        raise DescError("MERGED element has empty vql")

    requested = [e["requested_name"] for e in elements if e.get("requested_name")]
    excluded = _split_names(merged.get("excluded_datasources"))
    warnings = []

    # 3. every requested view is really in the VQL, under the right database
    objs = _objects_by_db(vql)
    missing = []
    for name in requested:
        db, _, obj = name.partition(".")
        if obj.lower() not in objs.get(db.lower(), set()):
            missing.append(name)
    if missing:
        raise DescError(f"requested view(s) not found in VQL: {', '.join(missing)}")

    # 4. datasources: allowed only under the 'allow' policy, and never with plaintext passwords
    rest, ds_blocks = _split_datasources(vql)
    ds_included = [n for n, _ in ds_blocks]
    if ds_blocks and datasources == "forbid":
        raise DescError(f"VQL contains datasource statements (PRD owns them): {', '.join(ds_included)}")
    for name, block in ds_blocks:
        if _has_plaintext_password(block):
            raise DescError(f"datasource {name} carries a plaintext password (expected ENCRYPTED)")

    # 5. credentials are tolerated only inside datasource statements
    if SECRET_RE.search(rest):
        raise DescError("VQL contains credential material outside datasource statements (PASSWORD/ENCRYPTED)")

    # 6. statement_count consistency (counted on the VQL as DESC delivered it)
    actual = len(STMT_RE.findall(vql))
    declared = merged.get("statement_count")
    if declared is not None and declared != actual:
        msg = f"statement_count mismatch: declared {declared}, counted {actual}"
        if strict_count:
            raise DescError(msg)
        warnings.append(msg)

    # 7. decide which datasources stay in the VQL
    out_vql, kept, stripped = vql, [f"?.{n}" for n in ds_included], []
    if create_datasources is not None and ds_blocks:
        wanted = {a.strip().lower() for a in create_datasources if a and a.strip()}
        allow_all = "*" in wanted

        def keep(db, name):
            if allow_all:
                return True
            n = name.lower()
            return n in wanted or bool(db and f"{db}.{n}".lower() in wanted)

        out_vql, kept, stripped = _strip_datasources(vql, keep)
    variables = sorted(set(PLACEHOLDER_RE.findall(out_vql)))
    parameterized = bool(REQUIRES_PROPS_RE.search(vql)) or bool(variables)
    if kept and parameterized:
        warnings.append(
            f"VQL recreates {len(kept)} datasource(s) as parameterized elements: {', '.join(kept)}. "
            "The PRD values come from the SM environment properties; any property the PRD "
            "environment leaves undefined or marks 'Use revision source value' gets the DEV value"
        )
    elif kept:
        warnings.append(
            f"VQL recreates {len(kept)} datasource(s) with literal DEV values: {', '.join(kept)}. "
            "SM cannot replace literals; generate the VQL with 'includeProperties' = 'yes'"
        )
    if stripped:
        warnings.append(
            f"{len(stripped)} datasource(s) removed from the VQL, PRD must already have them: "
            f"{', '.join(stripped)}"
        )

    # every datasource a wrapper refers to must be included here or already exist in PRD
    wrapper_ds = {n.lower() for n in re.findall(r"DATASOURCENAME\s*=\s*\"?([\w$]+)", vql)}
    covered = {n.lower() for n in ds_included} | {n.split(".")[-1].lower() for n in excluded}
    unlisted = sorted(wrapper_ds - covered)
    if unlisted:
        warnings.append(
            "wrapper uses datasource that is neither included nor listed in excluded_datasources "
            f"(must already exist in PRD): {', '.join(unlisted)}"
        )

    # 8. warnings only: things the SM promotion step or a human should know about
    dbs = list(dict.fromkeys(m.group(1) for m in DB_BLOCK_RE.finditer(vql)))
    if dbs:
        warnings.append("VQL will CONNECT to database(s) that must already exist in the "
                        "target environment: " + ", ".join(dbs))
    if dev_marker:
        lines = [i for i, l in enumerate(out_vql.splitlines(), 1) if dev_marker in l]
        if lines:
            warnings.append(
                f"{len(lines)} line(s) contain '{dev_marker}' (line {', '.join(map(str, lines[:5]))}"
                f"{'...' if len(lines) > 5 else ''}); SM promotion config must override them in PRD"
            )
    if re.search(r"^CREATE OR REPLACE MAP i18n\b", vql, re.M):
        warnings.append("VQL overwrites server-level i18n map; confirm PRD has the same definition")

    # 9. properties file that goes with a parameterized VQL
    properties = merged.get("properties")
    if properties is not None and not isinstance(properties, str):
        raise DescError("'properties' must be the text of the properties file")
    if parameterized and not properties:
        warnings.append("VQL is parameterized (REQUIRES-PROPERTIES-FILE) but the response carries no "
                        "'properties'; it must be supplied separately (propertiesFile)")
    if properties:
        if not parameterized:
            warnings.append("response carries 'properties' but the VQL has no ${variables}; ignored")
            properties = None
        else:
            known = parse_properties(properties)
            missing_vars = [v for v in variables if v not in known]
            hard = [v for v in missing_vars if not v.endswith(".ENCRYPTED")]
            soft = [v for v in missing_vars if v.endswith(".ENCRYPTED")]
            if hard:
                raise DescError("properties file has no value for variable(s): " + ", ".join(hard))
            if soft:
                warnings.append("properties file has no entry for: " + ", ".join(soft))
            if stripped:
                properties = _filter_properties(properties, set(variables))

    return DescResult(out_vql, requested, excluded, actual, warnings, ds_included, stripped,
                      properties, parameterized, variables)


def load_response_file(path):
    """Read a saved DESC response (UTF-8, BOM tolerated)."""
    with open(path, "rb") as f:
        return f.read().decode("utf-8-sig")


def requested_names(body_text):
    """requested_name of every element in a descvqlbatch response ([] if not that format)."""
    try:
        data = json.loads(body_text)
    except ValueError:
        return []
    if not isinstance(data, dict):
        return []
    return [e["requested_name"] for e in data.get("elements") or [] if e.get("requested_name")]


def extract_vql(body_text, content_type="", dev_marker="ACLDTPLTFRM-DEV", strict_count=True,
                datasources="allow", create_datasources=None):
    """Single entry point for create_and_deploy.fetch_vql_from_api.

    Returns (vql_text, DescResult | None). DescResult is only set for the
    descvqlbatch format, which is fully validated; legacy shapes (plain text,
    JSON with vql/content/data/result) pass through unvalidated as before.
    """
    text = (body_text or "").strip()
    if not text:
        raise DescError("DESC API returned an empty body")

    looks_json = "json" in (content_type or "").lower() or text[0] in "{["
    if not looks_json:
        return body_text, None

    try:
        data = json.loads(text)
    except ValueError:
        if "json" in (content_type or "").lower():
            raise DescError("response declared as JSON but could not be parsed")
        return body_text, None  # plain VQL that happens to start with '{' / '['

    if isinstance(data, dict) and data.get("name") == "descvqlbatch":
        res = parse_desc_response(data, dev_marker, strict_count, datasources, create_datasources)
        return res.vql, res
    if isinstance(data, list):
        raise DescError("DESC API returned an array; expected one merged VQL "
                        "(concatenating separate DESC outputs lets later DROP CASCADE delete earlier objects)")
    if isinstance(data, dict):
        for key in ("vql", "content", "data", "result"):
            if data.get(key):
                return data[key], None
        raise DescError("no VQL field in response: " + json.dumps(data, ensure_ascii=False)[:300])
    return str(data), None


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("input", help="descvqlbatch response JSON file")
    ap.add_argument("--out", required=True, help="where to write the merged VQL")
    ap.add_argument("--dev-marker", default="ACLDTPLTFRM-DEV")
    ap.add_argument("--lenient-count", action="store_true", help="statement_count mismatch = warning")
    ap.add_argument("--datasources", choices=("allow", "forbid"), default="allow",
                    help="allow: VQL may recreate datasources; forbid: reject any DATASOURCE statement")
    ap.add_argument("--properties-out", default="",
                    help="where to write the properties file carried by the response (if any)")
    ap.add_argument("--create-datasources", default="*",
                    help="* = keep all datasources (default); - = remove all; or db.ds,... = keep only those")
    args = ap.parse_args(argv)

    create = [p.strip() for p in args.create_datasources.split(",") if p.strip() and p.strip() != "-"]

    with open(args.input, encoding="utf-8") as f:
        payload = json.load(f)
    try:
        res = parse_desc_response(payload, args.dev_marker, strict_count=not args.lenient_count,
                                  datasources=args.datasources, create_datasources=create)
    except DescError as e:
        print(f"##[error]DESC validation failed: {e}")
        return 1

    with open(args.out, "w", encoding="utf-8", newline="") as f:  # UTF-8, keep \n as-is
        f.write(res.vql)
    print(f"DESC OK: {len(res.requested)} view(s), {res.statement_count} statements, "
          f"{len(res.datasources)} datasource(s) from DESC ({len(res.stripped_datasources)} removed), "
          f"{len(res.excluded)} excluded -> {args.out}")
    if args.properties_out and res.properties:
        with open(args.properties_out, "w", encoding="utf-8", newline="") as f:
            f.write(res.properties)
        print(f"properties -> {args.properties_out}")
    for w in res.warnings:
        print(f"##[warning]{w}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
