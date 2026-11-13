"""建立 revision 之前，決定 VQL 裡每個 ${變數} 怎麼處理。

Solution Manager 的實際行為（revision 1031 實測）：
  revision 用到的每個 ${變數}，目標環境的 VDP Properties 都必須有定義，否則驗證失敗。
  SM 不會自動改用 revision 帶的值。

因此在建立 revision 前，先讀取目標環境已定義的屬性
（GET /environments/{id}/vdpProperties，唯讀），逐一決定：
  - 目標環境已定義                      -> 保留 ${變數}，部署時由 SM 帶入 PRD 的值
  - 未定義，且此元素在目標環境沒有任何屬性 -> 視為 PRD 尚無此元素，把 DEV 的值直接寫進 VQL
  - 其餘無法安全判斷的情形               -> 中止並列出，請在 SM 設定

判斷「PRD 有沒有這個元素」的依據是：目標環境中有沒有任何屬於該元素的屬性。
前提是 PRD 既有的元素，其屬性都已登錄在 SM 的 PRD 環境（SM 部署本來就要求如此）。
若有元素是在 SM 之外直接建在 PRD、SM 環境裡完全沒有它的屬性，會被誤判為新元素而寫入 DEV 值。
"""

import re

# databases.<db>[.folder.<path>].{datasources|views}.<type>.<name>.<屬性...>
_PROP_RE = re.compile(
    r"^databases\.([^.]+)\.(?:folder\..+?\.)?(datasources|views)\.([^.]+)\.([^.]+)\.(.+)$",
    re.IGNORECASE)
_SECRET_RE = re.compile(r"(PASSWORD|SECRET|TOKEN|PRIVATE_?KEY|PASSPHRASE)$", re.IGNORECASE)

_CONNECT_RE = re.compile(r"^CONNECT DATABASE (\w+);", re.IGNORECASE)
_WRAPPER_RE = re.compile(r'^CREATE (?:OR REPLACE )?WRAPPER\s+(\w+)\s+"?([\w$]+)"?', re.IGNORECASE)
_DSNAME_RE = re.compile(r'^\s*DATASOURCENAME\s*=\s*"?([\w$]+)"?', re.IGNORECASE)


def element_of(prop):
    """屬性名稱 -> (kind, db, type, name)，kind 為 datasource / view；無法辨識時回傳 None。
    folder 路徑不列入，元素搬資料夾後仍視為同一個。"""
    m = _PROP_RE.match(str(prop))
    if not m:
        return None
    db, section, typ, name = (g.lower() for g in m.groups()[:4])
    return ("datasource" if section == "datasources" else "view", db, typ, name)


def is_secret(prop):
    """密碼類屬性（.ENCRYPTED / .ALGORITHM 旗標本身不算）。"""
    return bool(_SECRET_RE.search(str(prop)))


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


def plan_properties(variables, env_props, dev_props, vql):
    """variables: VQL 中的 ${變數} 名稱
    env_props:  目標環境已定義的屬性 {名稱: {"undefined": bool, "useRevisionSourceValue": bool}}
    dev_props:  properties 檔解析後的 {名稱: DEV 值}
    vql:        本次部署的 VQL

    回傳 (keep, inline, blocked)
      keep:    保留為 ${變數} 的名稱
      inline:  [{"name", "value", "secret"}]，要把 DEV 值寫進 VQL 的
      blocked: {名稱: 原因}
    """
    env_props = env_props or {}
    dev_props = dev_props or {}
    env_lower = {k.lower(): v for k, v in env_props.items()}
    env_elements = {element_of(p) for p in env_props} - {None}
    wrappers = wrapper_datasources(vql)

    keep, inline, blocked = [], [], {}
    candidates = []
    for name in variables:
        info = env_lower.get(name.lower())
        if info is not None:
            if info.get("undefined"):
                blocked[name] = "目標環境已有此屬性，但值設為 undefined，請在 SM 填入值"
            else:
                keep.append(name)
            continue
        e = element_of(name)
        if e is None:
            blocked[name] = "無法辨識屬性所屬的元素"
        elif e in env_elements:
            blocked[name] = "此元素在目標環境已存在（已有其他屬性），這是新增的設定，請在 SM 設定 PRD 要用的值"
        elif name not in dev_props:
            blocked[name] = "properties 檔沒有此屬性的 DEV 值"
        else:
            candidates.append((name, e))

    # 本次以 DEV 值建立的 datasource
    new_ds = {(e[1], e[3]) for _, e in candidates if e[0] == "datasource"}
    for name, e in candidates:
        if e[0] == "view":
            ds = wrappers.get((e[1], e[3]))
            if ds is None:
                blocked[name] = "VQL 中找不到此 base view 使用的 datasource"
                continue
            if (e[1], ds) not in new_ds:
                blocked[name] = ("base view 所用的 datasource %s 在目標環境已存在，"
                                 "套用 DEV 值會指向開發環境，請在 SM 設定" % ds)
                continue
        inline.append({"name": name, "value": dev_props[name], "secret": is_secret(name)})
    return keep, inline, blocked
