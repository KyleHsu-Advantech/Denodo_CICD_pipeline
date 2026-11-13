"""決定驗證時哪些「回退使用 revision（DEV）值」的屬性可以自動放行。

Solution Manager 對每個 ${變數} 的取值順序（由 validate 回應的三個欄位反映）：
  1. PRD 環境的 VDP Properties 有定義            -> 用環境值（不會出現在 missing 清單）
  2. 環境沒定義，但 PRD 伺服器上該元素已存在     -> 用伺服器現有值
                                                    (missingVqlPropertiesWithServerValues)
  3. 環境、伺服器都沒有（元素在 PRD 是全新的）   -> 用 revision 帶的值，即 DEV 值
                                                    (missingVqlPropertiesUseRevisionValues)

需求對應：
  - PRD 已有的 datasource：落在 1 或 2，本來就用 PRD 的值，不需放行。
  - PRD 沒有的 datasource：落在 3，要自動放行，讓 DEV 的值直接帶上 PRD。

本模組只放行「整個元素在 PRD 都不存在」的情形，下列一律擋下：
  - 同一元素有部分屬性取到伺服器值（元素已存在，卻有新屬性要套 DEV 值）
  - wrapper（base view）屬性要回退，但它所用的 datasource 不是本次全新的
    （例如在 PRD 既有 datasource 上新建 base view，ROUTE 會帶 DEV 的 URL）
  - 無法辨識歸屬元素的屬性名稱
"""

import re

# databases.<db>[.folder.<path>].{datasources|views}.<type>.<name>.<屬性...>
# folder 路徑本身可能含多段，用非貪婪比對到第一個 datasources / views。
_PROP_RE = re.compile(
    r"^databases\.([^.]+)\.(?:folder\..+?\.)?(datasources|views)\.([^.]+)\.([^.]+)\.(.+)$",
    re.IGNORECASE)

_CONNECT_RE = re.compile(r"^CONNECT DATABASE (\w+);", re.IGNORECASE)
_WRAPPER_RE = re.compile(r'^CREATE (?:OR REPLACE )?WRAPPER\s+(\w+)\s+"?([\w$]+)"?', re.IGNORECASE)
_DSNAME_RE = re.compile(r'^\s*DATASOURCENAME\s*=\s*"?([\w$]+)"?', re.IGNORECASE)


def element_of(prop):
    """屬性名稱 -> (kind, db, type, name)，kind 為 datasource / view；無法辨識時回傳 None。"""
    m = _PROP_RE.match(str(prop))
    if not m:
        return None
    db, section, typ, name = (g.lower() for g in m.groups()[:4])
    return ("datasource" if section == "datasources" else "view", db, typ, name)


def wrapper_datasources(vql):
    """由 VQL 取出 (db, wrapper 名稱) -> datasource 名稱 的對照。"""
    mapping, db, current = {}, None, None
    for line in (vql or "").splitlines():
        c = _CONNECT_RE.match(line)
        if c:
            db = c.group(1).lower()
            continue
        w = _WRAPPER_RE.match(line)
        if w:
            current = (db, w.group(2).lower())
            continue
        if current:
            d = _DSNAME_RE.match(line)
            if d:
                mapping[current] = d.group(1).lower()
                current = None
            elif line.startswith(("CREATE", "ALTER", "CONNECT", "CLOSE", "#")):
                current = None
    return mapping


def classify_fallback(use_revision, with_server, vql):
    """把 missingVqlPropertiesUseRevisionValues 分成可放行與須擋下兩組。

    use_revision: 會回退用 DEV 值的屬性名稱清單
    with_server:  會用 PRD 伺服器現值的屬性名稱（dict 或 list）
    vql:          本次部署的 VQL，用來找 wrapper 所屬的 datasource

    回傳 (allowed, blocked, reasons)；reasons 為 {屬性: 擋下原因}。
    """
    existing = {element_of(p) for p in (with_server or [])} - {None}
    fallback_elems = {element_of(p) for p in use_revision} - {None}
    wrappers = wrapper_datasources(vql)

    # 全新的 datasource：有屬性要回退，且沒有任何屬性取到伺服器值
    new_ds = {(e[1], e[3]) for e in fallback_elems
              if e[0] == "datasource" and e not in existing}

    allowed, blocked, reasons = [], [], {}
    for prop in use_revision:
        e = element_of(prop)
        if e is None:
            reason = "無法辨識屬性所屬的元素"
        elif e in existing:
            reason = "元素已存在於 PRD，此屬性卻要套用 DEV 值"
        elif e[0] == "datasource":
            reason = None
        else:
            ds = wrappers.get((e[1], e[3]))
            if ds is None:
                reason = "VQL 中找不到此 base view 使用的 datasource"
            elif (e[1], ds) not in new_ds:
                reason = "base view 所用的 datasource %s 不是本次新建的，ROUTE 會帶 DEV 值" % ds
            else:
                reason = None
        if reason:
            blocked.append(prop)
            reasons[prop] = reason
        else:
            allowed.append(prop)
    return allowed, blocked, reasons
