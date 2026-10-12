"""將 DESC VQL 的輸出加工成適合部署的形式。

兩項加工：
  1. DROP ... CASCADE + CREATE  ->  CREATE OR REPLACE
     DESC VQL 產生的是「先刪再建」，刪除瞬間相依物件會一起消失，
     若後續建立失敗，目標環境會處於破損狀態。
     CREATE OR REPLACE 是原子替換，失敗時舊物件仍在。

  2. 於檔首加入 CONNECT DATABASE
     DESC VQL 不含此語句，物件會被建到目標端的預設 database。
"""

import re

# DESC VQL 產生的 DROP 樣式，例如：
#   DROP MAP i18n IF EXISTS tw CASCADE;
#   DROP WRAPPER JSON IF EXISTS ds_x CASCADE;
#   DROP TYPE IF EXISTS "t_x" CASCADE;
#   DROP VIEW IF EXISTS bv_x CASCADE;
DROP_RE = re.compile(
    r'^\s*DROP\s+(MAP|WRAPPER|DATASOURCE|TYPE|VIEW|TABLE)\b.*\bIF\s+EXISTS\b.*;\s*$',
    re.IGNORECASE)

# 對應的 CREATE。FOLDER 原本就是 OR REPLACE，不在此列。
CREATE_RE = re.compile(
    r'^(\s*)CREATE\s+(MAP|WRAPPER|DATASOURCE|TYPE|VIEW|TABLE)\b',
    re.IGNORECASE)

ALREADY_RE = re.compile(r'^\s*CREATE\s+OR\s+REPLACE\b', re.IGNORECASE)


def transform_vql(text, database=None, keep_drop_types=False):
    """回傳 (加工後的 VQL, 統計資訊 dict)。

    keep_drop_types=True 時保留 TYPE 的 DROP+CREATE 原樣，
    供 CREATE OR REPLACE TYPE 不被支援時退回使用。
    """
    stats = {"dropped": {}, "replaced": {}, "kept": {}, "connect_added": False}
    out = []

    for line in text.splitlines(keepends=True):
        m = DROP_RE.match(line)
        if m:
            kind = m.group(1).upper()
            if kind == "TYPE" and keep_drop_types:
                stats["kept"][kind] = stats["kept"].get(kind, 0) + 1
                out.append(line)
            else:
                stats["dropped"][kind] = stats["dropped"].get(kind, 0) + 1
                # 整行移除，不留空行
            continue

        if not ALREADY_RE.match(line):
            m = CREATE_RE.match(line)
            if m:
                kind = m.group(2).upper()
                if not (kind == "TYPE" and keep_drop_types):
                    line = CREATE_RE.sub(
                        lambda mm: "%sCREATE OR REPLACE %s"
                                   % (mm.group(1), mm.group(2)),
                        line, count=1)
                    stats["replaced"][kind] = stats["replaced"].get(kind, 0) + 1

        out.append(line)

    result = "".join(out)

    if database:
        result = _prepend_connect(result, database, stats)

    return result, stats


def _prepend_connect(text, database, stats):
    """在開頭註解之後、第一個語句之前插入 CONNECT DATABASE。"""
    if re.search(r'^\s*CONNECT\s+DATABASE\b', text, re.IGNORECASE | re.MULTILINE):
        return text  # 已經有了就不重複加

    lines = text.splitlines(keepends=True)
    insert_at = 0
    for i, ln in enumerate(lines):
        stripped = ln.strip()
        if stripped and not stripped.startswith("#"):
            insert_at = i
            break
        insert_at = i + 1

    stmt = "CONNECT DATABASE %s;\n\n" % database
    lines.insert(insert_at, stmt)
    stats["connect_added"] = True
    return "".join(lines)


def format_stats(stats):
    parts = []
    if stats["dropped"]:
        parts.append("移除 DROP：" + "、".join(
            "%s x%d" % (k, v) for k, v in sorted(stats["dropped"].items())))
    if stats["replaced"]:
        parts.append("改為 CREATE OR REPLACE：" + "、".join(
            "%s x%d" % (k, v) for k, v in sorted(stats["replaced"].items())))
    if stats["kept"]:
        parts.append("保留原樣：" + "、".join(
            "%s x%d" % (k, v) for k, v in sorted(stats["kept"].items())))
    if stats["connect_added"]:
        parts.append("已加入 CONNECT DATABASE")
    return "；".join(parts) if parts else "未做任何變更"
