"""
串接 Denodo Solution Manager REST API（經由 Advantech APIM 轉送）

認證流程：
  0) 先向 APIM 的 user login 端點換取 JWT
  1) 之後每支 API 都帶 Ocp-Apim-Subscription-Key + Authorization: Bearer <token>

主流程：
  1) 取得環境清單     (GET  /pool/DenodoSolutionManager/environments)
  2) 取得環境內伺服器 (GET  /pool/DenodoSolutionManager/environments/{id}/servers)
  3) 建立 Revision    (POST /pool/DenodoSolutionManager/revisions/loadFromVQL)
  4) 驗證 Revision    (POST /pool/DenodoSolutionManager/revisions/{id}/validate)
  5) 觸發部署         (POST /pool/DenodoSolutionManager/deployments)
  6) 輪詢部署進度     (GET  /pool/DenodoSolutionManager/deployments/{id}/progress)

環境變數（對應 pipeline 的 secret variable，程式不寫死任何憑證）：
  APIM_BASE_URL          例如 https://ebizapis.advantech.com
  APIM_SUBSCRIPTION_KEY
  APIM_LOGIN_EMAIL
  APIM_LOGIN_PASSWORD

分階段測試：
  --stage environments  只取環境清單（第一次測用這個）
  --stage servers       再取某環境的 server 與 clusterId
  --stage revision      建立 revision
  --stage validate      建立並驗證
  --stage deploy        完整跑到部署完成
"""

import argparse
import base64
import json
import re
import os
import sys
import time

import requests

from vql_transform import transform_vql, format_stats


BASE_URL = os.environ.get(
    "APIM_BASE_URL", "https://ebizapis.advantech.com").rstrip("/")
SUB_KEY = os.environ.get("APIM_SUBSCRIPTION_KEY", "")
LOGIN_EMAIL = os.environ.get("APIM_LOGIN_EMAIL", "")
LOGIN_PASSWORD = os.environ.get("APIM_LOGIN_PASSWORD", "")

LOGIN_PATH = "/user/api/user/login"
SM_PREFIX = "/pool/DenodoSolutionManager"

TIMEOUT = 60

_token = None


# ---------------------------------------------------------------- 工具

def log(msg):
    print(msg, flush=True)


def fail(msg):
    print("##[error]%s" % msg, flush=True)
    sys.exit(1)


def _check_env():
    missing = [n for n in ("APIM_SUBSCRIPTION_KEY",
                           "APIM_LOGIN_EMAIL",
                           "APIM_LOGIN_PASSWORD")
               if not os.environ.get(n)]
    if missing:
        fail("缺少環境變數：%s（請確認 pipeline 已連結 variable group 並授權）"
             % ", ".join(missing))


def _mask(s, keep=4):
    """只露出開頭幾碼，避免憑證寫進 pipeline log。"""
    if not s:
        return "(空)"
    return s[:keep] + "*" * max(0, len(s) - keep)


def read_as_base64(path, label):
    """讀取檔案並轉成 base64。

    Denodo 匯出的 VQL 與 properties 檔可能帶 UTF-8 BOM，
    BOM 會讓 Solution Manager 解析失敗，必須先去除。
    """
    if not os.path.isfile(path):
        fail("找不到 %s 檔案：%s" % (label, path))

    with open(path, "rb") as f:
        raw = f.read()

    had_bom = raw.startswith(b"\xef\xbb\xbf")
    if had_bom:
        raw = raw[3:]

    encoded = base64.b64encode(raw).decode("ascii")
    log("%s：%s" % (label, path))
    log("  原始大小 %d bytes，base64 後 %d bytes%s"
        % (len(raw), len(encoded), "，已去除 BOM" if had_bom else ""))

    # 提醒需要搭配 properties 檔的情形
    head = raw[:500].decode("utf-8", errors="ignore")
    if "REQUIRES-PROPERTIES-FILE" in head:
        log("  注意：此 VQL 標記為需搭配 properties 檔，"
            "未提供 properties 會導致連線參數無法解析")

    return encoded


# ---------------------------------------------------------------- 0. 取得 token

def get_token():
    """向 APIM 換取 JWT。token 有效期有限，每次 pipeline 執行都重新取得。"""
    global _token

    url = BASE_URL + LOGIN_PATH
    log("取得 user token：POST %s" % url)
    log("  帳號：%s" % LOGIN_EMAIL)

    try:
        resp = requests.post(
            url,
            json={"email": LOGIN_EMAIL, "password": LOGIN_PASSWORD},
            headers={
                "Content-Type": "application/json",
                "Ocp-Apim-Subscription-Key": SUB_KEY,
            },
            timeout=TIMEOUT,
        )
    except requests.exceptions.RequestException as exc:
        fail("登入請求失敗：%s" % exc)

    log("HTTP %d" % resp.status_code)

    if resp.status_code >= 400:
        log("回應內容：%s" % resp.text[:1000])
        if resp.status_code == 401:
            log("提示：401 多半是帳密錯誤，或 subscription key 無效。")
        fail("取得 token 失敗")

    try:
        data = resp.json()
    except ValueError:
        fail("登入回應非 JSON：%s" % resp.text[:500])

    token = data.get("token")
    if not token:
        fail("登入回應中沒有 token 欄位：%s"
             % json.dumps(data, ensure_ascii=False)[:500])

    _token = token
    log("已取得 token：%s（長度 %d）" % (_mask(token, 12), len(token)))
    return token


def _headers(json_body=True):
    h = {
        "Accept": "application/json",
        "Ocp-Apim-Subscription-Key": SUB_KEY,
        "Authorization": "Bearer %s" % _token,
    }
    if json_body:
        h["Content-Type"] = "application/json"
    return h


def _request(method, path, allow_404=False, **kwargs):
    """對 SM API 發出請求，失敗時印出完整回應以利除錯。

    allow_404=True 時，404 不視為致命錯誤，回傳 None 由呼叫端決定如何處理。
    用於 APIM 上可能尚未發布的 operation。
    """
    url = BASE_URL + SM_PREFIX + path
    log("%s %s" % (method, url))

    try:
        resp = requests.request(
            method, url,
            headers=_headers(json_body="json" in kwargs),
            timeout=TIMEOUT,
            **kwargs
        )
    except requests.exceptions.Timeout:
        fail("請求逾時（%d 秒）。APIM 的後端逾時可能更短，請檢查 APIM 設定。"
             % TIMEOUT)
    except requests.exceptions.RequestException as exc:
        fail("連線失敗：%s" % exc)

    log("HTTP %d" % resp.status_code)

    if resp.status_code == 404 and allow_404:
        log("此 operation 在 APIM 上不存在（404），略過")
        return None

    if resp.status_code >= 400:
        log("回應內容：%s" % resp.text[:2000])
        if resp.status_code == 401:
            log("提示：401 可能是 token 已過期，或 APIM 未將 Authorization 轉給後端。")
        elif resp.status_code == 403:
            log("提示：403 可能是 subscription key 無效，或該帳號在 SM 權限不足。")
        elif resp.status_code == 404:
            log("提示：404 請確認 APIM 上該 operation 的路徑定義。")
        elif resp.status_code == 413:
            log("提示：413 代表 body 超過 APIM 大小上限，VQL 內容過大。")
        fail("API 呼叫失敗：%s %s" % (method, path))

    return resp


# ------------------------------------------------- 1. 由 DESC API 取得 VQL

def fetch_vql_from_api(components, api_path, database=None,
                       transform=True, keep_drop_types=False):
    """呼叫 APIM 上的 DESC API，取回展開相依後的完整 VQL。

    一次送出全部 component，由 API 端合併去重後回傳單一份 VQL。
    不可逐一呼叫後自行串接：DESC 輸出開頭帶有
    DROP ... IF EXISTS ... CASCADE，多份串接時後面的 DROP
    會把前面已建立的物件連帶刪除。

    回應可為純文字 VQL，或 JSON（支援 vql / content / data 等欄位）。
    """
    log("")
    log("--- 取得完整 VQL（含相依展開）---")
    log("  元件：%s" % ", ".join(components))

    resp = _request("POST", api_path, json={"views": components})

    ctype = resp.headers.get("Content-Type", "")
    if "json" in ctype.lower():
        data = resp.json()
        if isinstance(data, dict):
            vql = (data.get("vql") or data.get("content")
                   or data.get("data") or data.get("result"))
            if vql is None:
                fail("DESC API 回應中找不到 VQL 欄位：%s"
                     % json.dumps(data, ensure_ascii=False)[:500])
        elif isinstance(data, list):
            fail("DESC API 回傳陣列。請改為回傳單一份合併後的 VQL，"
                 "避免多份 DROP CASCADE 互相刪除")
        else:
            vql = str(data)
    else:
        vql = resp.text

    if not vql or not vql.strip():
        fail("DESC API 回傳空的 VQL")

    raw = vql.encode("utf-8")
    if raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
        log("  已去除 BOM")

    text = raw.decode("utf-8", errors="ignore")
    log("  原始 VQL %d bytes" % len(raw))

    # 加工：DROP+CREATE 轉 CREATE OR REPLACE、補上 CONNECT DATABASE
    if transform:
        text, stats = transform_vql(text, database=database,
                                    keep_drop_types=keep_drop_types)
        log("  加工：%s" % format_stats(stats))
        raw = text.encode("utf-8")
    elif database:
        log("##[warning]未啟用加工，CONNECT DATABASE 不會加入，"
            "物件將建在目標端的預設 database")

    # 加工後仍殘留的破壞性語句，部署前須確認影響範圍
    leftovers = [ln.strip() for ln in text.splitlines()
                 if ln.strip().upper().startswith("DROP")]
    if leftovers:
        log("")
        log("##[warning]VQL 仍含 %d 行 DROP，會先刪除再建立：" % len(leftovers))
        for ln in leftovers[:10]:
            log("##[warning]  %s" % ln[:120])
        if len(leftovers) > 10:
            log("##[warning]  （其餘 %d 行省略）" % (len(leftovers) - 10))

    if not re.search(r"^\s*CONNECT\s+DATABASE", text, re.IGNORECASE | re.MULTILINE):
        log("##[warning]VQL 未指定 CONNECT DATABASE，"
            "物件會建在目標端的預設 database")

    encoded = base64.b64encode(raw).decode("ascii")
    log("  最終 %d bytes，base64 後 %d bytes" % (len(raw), len(encoded)))
    return encoded


# ---------------------------------------------------------------- 1. 環境清單

def list_environments(quiet=False):
    log("")
    log("--- 取得環境清單 ---")
    resp = _request("GET", "/environments")

    try:
        data = resp.json()
    except ValueError:
        fail("環境清單回應非 JSON：%s" % resp.text[:500])

    if not quiet:
        log("完整回應：%s"
            % json.dumps(data, ensure_ascii=False, indent=2)[:3000])

    if isinstance(data, list):
        items = data
    else:
        items = data.get("environments") or data.get("items") or []

    log("")
    log("環境摘要：")
    for env in items:
        log("  id=%s  name=%s" % (env.get("id"), env.get("name")))

    return items


def resolve_environment_id(environments, wanted):
    """把使用者給的環境名稱或 id 解析成實際的 environment id。

    接受 "ACLDTPLTFRM-PRD" 這種名稱，也接受純數字 id。
    名稱比對不分大小寫。
    """
    wanted = str(wanted).strip()

    # 純數字就直接當 id 用，但仍確認它存在
    if wanted.isdigit():
        for env in environments:
            if str(env.get("id")) == wanted:
                log("目標環境：id=%s name=%s"
                    % (env.get("id"), env.get("name")))
                return int(wanted)
        fail("環境清單中找不到 id=%s" % wanted)

    for env in environments:
        if str(env.get("name", "")).strip().lower() == wanted.lower():
            log("目標環境：name=%s 解析為 id=%s"
                % (env.get("name"), env.get("id")))
            return int(env["id"])

    names = ", ".join(str(e.get("name")) for e in environments)
    fail("環境清單中找不到名稱 %s。可用的環境：%s" % (wanted, names))


# ---------------------------------------------------------------- 2. 伺服器清單

def list_servers(environment_id, allow_missing=False):
    log("")
    log("--- 取得環境 %s 的伺服器 ---" % environment_id)
    resp = _request("GET", "/environments/%s/servers" % environment_id,
                    allow_404=allow_missing)

    if resp is None:
        log("提示：APIM 尚未發布 /environments/{id}/servers，")
        log("      無法自動推導 clusterId。")
        return None

    try:
        data = resp.json()
    except ValueError:
        fail("伺服器清單回應非 JSON：%s" % resp.text[:500])

    log("完整回應：%s" % json.dumps(data, ensure_ascii=False, indent=2)[:3000])

    if isinstance(data, list):
        items = data
    else:
        # SM 實際回傳 {"servers": [...]}；保留其他鍵名以防版本差異
        items = data.get("servers") or data.get("items") or []

    log("")
    log("伺服器摘要：")
    for srv in items:
        log("  id=%s name=%s type=%s clusterId=%s enabled=%s"
            % (srv.get("id"), srv.get("name"), srv.get("typeNode"),
               srv.get("clusterId"), srv.get("enabled")))

    cluster_ids = sorted({str(s.get("clusterId")) for s in items
                          if s.get("clusterId") is not None})
    if cluster_ids:
        log("")
        log("可用的 clusterId：%s" % ", ".join(cluster_ids))

    return items


def pick_cluster_id(servers):
    """由 server 清單推導出要部署的 clusterId。

    只取啟用中的 VDP 節點。若找不到或有多個，不靜默猜測，
    而是印出完整資訊要求明確指定。
    """
    candidates = []
    for srv in servers:
        cid = srv.get("clusterId")
        if cid is None:
            continue
        node_type = str(srv.get("typeNode", "")).upper()
        enabled = srv.get("enabled")
        # 只取 VDP 節點。注意 VDP_DATA_CATALOG 與 SCHEDULER 都不是部署目標，
        # 因此必須精確比對，不能用子字串判斷。
        if node_type and node_type != "VDP":
            continue
        if enabled is False:
            continue
        candidates.append((int(cid), srv.get("name")))

    unique = sorted({c for c, _ in candidates})

    if not unique:
        log("servers 完整內容：%s"
            % json.dumps(servers, ensure_ascii=False, indent=2)[:2000])
        fail("無法從 server 清單推導 clusterId，請以 --cluster-id 明確指定")

    if len(unique) > 1:
        log("偵測到多個 clusterId：%s"
            % ", ".join(str(c) for c in unique))
        for cid, name in candidates:
            log("  clusterId=%s server=%s" % (cid, name))
        fail("目標環境有多個 cluster，請以 --cluster-id 明確指定要部署的那一個")

    log("自動推導 clusterId = %s" % unique[0])
    return unique[0]


# ---------------------------------------------------------------- 3. 建立 Revision

def create_revision(name, description, vql_b64, properties_b64=None):
    """由 VQL 建立 revision。

    content 與 properties 必須是 base64 編碼的 UTF-8 內容，
    且 VQL 不得含 BOM，否則 SM 解析會失敗。
    這一步同時驗證 APIM 轉送過程有沒有破壞 base64 字串。
    """
    log("")
    log("--- 建立 revision：%s ---" % name)
    log("  VQL base64 長度：%d" % len(vql_b64))

    body = {"name": name, "content": vql_b64}
    if description:
        body["description"] = description
    if properties_b64:
        body["properties"] = properties_b64
        log("  properties base64 長度：%d" % len(properties_b64))

    resp = _request("POST", "/revisions/loadFromVQL", json=body)
    data = resp.json()

    log("完整回應：%s" % json.dumps(data, ensure_ascii=False, indent=2)[:2000])
    log("")
    log("revision id = %s" % data.get("id"))
    log("  useDefaultDatabase = %s" % data.get("useDefaultDatabase"))
    log("  replace            = %s" % data.get("replace"))

    return data["id"]


# ---------------------------------------------------------------- 4. 驗證

def validate_revision(revision_id, environment_id, description,
                      allow_revision_fallback=False):
    """驗證 revision 是否可安全部署到目標環境。

    SM 即使驗證失敗也可能回 HTTP 200，因此必須檢查回應內容，
    不能只看狀態碼。

    三個 missing 欄位代表屬性值的回退層級：
      missingVqlPropertiesWithServerValues
          PRD 環境設定未定義，但 PRD 伺服器已有值 -> 用 server 的值（可接受）
      missingVqlPropertiesUseRevisionValues
          PRD 環境與 server 都沒有 -> 回退使用 revision（來源端 DEV）的值
          這會讓開發環境的設定進入正式環境，預設視為錯誤
      missingSchPropertiesWithServerValues
          同第一項，但針對 Scheduler

    回傳 (ok: bool, summary: str, detail: dict)。
    """
    log("")
    log("--- 驗證 revision %s 對環境 %s ---" % (revision_id, environment_id))

    body = {
        "environmentId": str(environment_id),
        "description": description or ("validate revision %s" % revision_id),
    }
    resp = _request("POST", "/revisions/%s/validate" % revision_id, json=body)

    try:
        payload = resp.json()
    except ValueError:
        return False, "驗證回應非 JSON：%s" % resp.text[:300], {}

    v = payload.get("validation") or {}
    problems = []

    # 1) 整體與分項結果
    state = str(v.get("state", "")).upper()
    vql_result = str(v.get("resultVqlValidation", "")).upper()
    sch_result = str(v.get("resultSchValidation", "")).upper()

    log("state=%s  VQL=%s  Scheduler=%s" % (state, vql_result, sch_result))
    log("訊息：%s" % v.get("messageResult", ""))

    if state and state != "OK":
        problems.append("整體驗證狀態為 %s" % state)
    if vql_result and vql_result != "OK":
        problems.append("VQL 驗證失敗：%s"
                        % (v.get("errorVqlValidation")
                           or v.get("outputVqlValidation") or vql_result))
    if sch_result and sch_result != "OK":
        problems.append("Scheduler 驗證失敗：%s"
                        % (v.get("errorSchValidation")
                           or v.get("outputSchValidation") or sch_result))

    # 2) 會回退使用來源端（DEV）值的屬性 —— 預設視為錯誤
    use_rev = v.get("missingVqlPropertiesUseRevisionValues") or []
    if use_rev:
        log("")
        log("以下屬性在目標環境與伺服器皆無值，將回退使用來源端的值：")
        for name in use_rev:
            log("  %s" % name)
        msg = ("有 %d 項屬性會沿用開發環境的值（%s%s）"
               % (len(use_rev), ", ".join(str(n) for n in use_rev[:3]),
                  " 等" if len(use_rev) > 3 else ""))
        if allow_revision_fallback:
            log("##[warning]%s，已依設定放行" % msg)
        else:
            problems.append(msg)

    # 3) 改用目標伺服器現有值的屬性 —— 警告但放行
    with_server = v.get("missingVqlPropertiesWithServerValues") or {}
    sch_server = v.get("missingSchPropertiesWithServerValues") or {}
    for label, mapping in (("VQL", with_server), ("Scheduler", sch_server)):
        if mapping:
            log("")
            log("##[warning]%s：有 %d 項屬性未在環境設定中定義，"
                "將採用目標伺服器現有的值：" % (label, len(mapping)))
            for name in list(mapping)[:20]:
                log("##[warning]  %s" % name)

    # 4) 腳本層級的驗證結果
    for item in payload.get("scriptValidationsResult") or []:
        st = str(item.get("state", item.get("result", ""))).upper()
        if st and st != "OK":
            problems.append("腳本驗證未通過：%s"
                            % json.dumps(item, ensure_ascii=False)[:300])

    detail = {
        "revisionId": revision_id,
        "environment": (v.get("targetEnvironment") or {}).get("name"),
        "state": state,
        "vqlValidation": vql_result,
        "schedulerValidation": sch_result,
        "useRevisionValues": list(use_rev),
        "withServerValues": list(with_server),
        "message": v.get("messageResult", ""),
    }

    if problems:
        return False, "；".join(problems), detail

    log("")
    log("驗證通過，可安全部署")
    return True, "驗證通過", detail


# ---------------------------------------------------------------- 5. 觸發部署

def start_deployment(revision_id, environment_id, cluster_id=None):
    log("")
    log("--- 觸發部署 ---")
    body = {
        "revisions": [int(revision_id)],
        "environmentId": int(environment_id),
    }
    if cluster_id:
        body["clusterId"] = int(cluster_id)

    log("  revision=%s environment=%s cluster=%s"
        % (revision_id, environment_id, cluster_id or "(未指定)"))

    resp = _request("POST", "/deployments", json=body)
    data = resp.json()

    log("完整回應：%s" % json.dumps(data, ensure_ascii=False, indent=2)[:2000])
    deployment_id = data.get("id")
    log("deployment id = %s" % deployment_id)
    return deployment_id


# ---------------------------------------------------------------- 6. 輪詢進度

# SM 的狀態字串依版本而異，先放寬比對範圍。
# 第一次實測後，請依實際回傳值收斂這兩組集合。
SUCCESS_STATES = {"OK", "SUCCESS", "SUCCESSFUL", "FINISHED", "DONE", "COMPLETED"}
FAILURE_STATES = {"ERROR", "FAILED", "FAILURE", "KO", "CANCELLED", "CANCELED"}


def wait_for_deployment(deployment_id, timeout_sec=900, interval=10):
    """輪詢直到部署結束。

    這是內層輪詢，必須等到部署真正完成，pipeline 這個 run 才結束，
    呼叫端的外層輪詢才不會過早回報成功。
    """
    log("")
    log("--- 輪詢部署進度 ---")
    deadline = time.time() + timeout_sec
    last_status = None

    while time.time() < deadline:
        resp = _request("GET", "/deployments/%s/progress" % deployment_id)

        try:
            progress = resp.json()
        except ValueError:
            log("進度回應非 JSON：%s" % resp.text[:500])
            time.sleep(interval)
            continue

        status = str(progress.get("status", "")).upper()
        pct = progress.get("percentage", progress.get("progress", "?"))

        if status != last_status:
            log("狀態：%s（%s%%）" % (status or "(無 status 欄位)", pct))
            log("完整回應：%s"
                % json.dumps(progress, ensure_ascii=False)[:1500])
            last_status = status

        if status in SUCCESS_STATES:
            log("部署完成")
            return progress

        if status in FAILURE_STATES:
            fail("部署失敗：%s" % json.dumps(progress, ensure_ascii=False))

        time.sleep(interval)

    fail("部署逾時（%d 秒），請至 Solution Manager 確認實際狀態" % timeout_sec)


# ---------------------------------------------------------------- 主流程

STAGES = ["environments", "servers", "revision", "validate", "deploy"]


def main():
    ap = argparse.ArgumentParser(
        description="Denodo Solution Manager CI deployment via APIM")
    ap.add_argument("--stage", default="environments", choices=STAGES,
                    help="執行到哪一階段為止，用於分段驗證")
    ap.add_argument("--name", default="", help="Revision 名稱")
    ap.add_argument("--description", default="", help="Revision 說明")
    ap.add_argument("--components", default="",
                    help="要過版的資料表清單，逗號分隔。"
                         "指定後會呼叫 DESC API 取得展開相依的完整 VQL")
    ap.add_argument("--database", default="",
                    help="物件要建立在哪個 database。"
                         "DESC VQL 不含 CONNECT DATABASE，未指定則會建在預設 db")
    ap.add_argument("--no-transform", action="store_true",
                    help="不加工 VQL，原樣送出（保留 DROP + CREATE）")
    ap.add_argument("--keep-drop-types", action="store_true",
                    help="TYPE 保留 DROP+CREATE 原樣。"
                         "若驗證回報 CREATE OR REPLACE TYPE 語法錯誤時使用")
    ap.add_argument("--desc-api-path", default="/descVql",
                    help="APIM 上 DESC API 的路徑（接在 SM 前綴之後）")
    ap.add_argument("--vql-file", default="",
                    help="repo 內的 VQL 檔路徑，腳本自行讀檔並轉 base64（建議）")
    ap.add_argument("--properties-file", default="",
                    help="repo 內的 properties 檔路徑，同上")
    ap.add_argument("--vql-base64", default="",
                    help="直接傳入 base64 字串。大檔請改用 --vql-file")
    ap.add_argument("--properties-base64", default="",
                    help="直接傳入 base64 字串。大檔請改用 --properties-file")
    ap.add_argument("--environment", "--environment-id", dest="environment",
                    default="",
                    help="目標環境，可填名稱（如 ACLDTPLTFRM-PRD）或數字 id")
    ap.add_argument("--cluster-id", default="",
                    help="選填。留空則由目標環境的 server 清單自動推導")
    ap.add_argument("--allow-revision-fallback", action="store_true",
                    help="允許屬性回退使用來源端（DEV）的值。"
                         "預設視為錯誤並中止，僅在明確確認後才開啟")
    ap.add_argument("--timeout", type=int, default=900)
    ap.add_argument("--interval", type=int, default=10)
    args = ap.parse_args()

    _check_env()

    log("=" * 64)
    log("Denodo SM 部署作業")
    log("  stage            = %s" % args.stage)
    log("  APIM base        = %s" % BASE_URL)
    log("  subscription key = %s" % _mask(SUB_KEY, 6))
    log("=" * 64)

    # 每次執行都重新取得 token
    get_token()

    # 階段 1：環境清單
    environments = list_environments()
    if args.stage == "environments":
        log("")
        log("stage=environments，就此結束")
        return

    # 把名稱或 id 解析成實際的 environment id
    if not args.environment or args.environment == "-":
        fail("stage=%s 需要 --environment（名稱或 id）" % args.stage)
    environment_id = resolve_environment_id(environments, args.environment)

    # 階段 2：決定 clusterId
    # 1) 有明確指定就直接用，不必呼叫 /servers
    # 2) 未指定則嘗試由 /servers 推導
    # 3) APIM 上若未發布 /servers，不中斷，改為不帶 clusterId 送出部署
    cluster_id = None
    if args.cluster_id and args.cluster_id != "-":
        cluster_id = int(args.cluster_id)
        log("")
        log("使用指定的 clusterId = %s（略過 /servers 查詢）" % cluster_id)
    else:
        servers = list_servers(environment_id, allow_missing=True)
        if servers:
            cluster_id = pick_cluster_id(servers)
        else:
            log("")
            log("##[warning]未取得 clusterId，部署將不指定 cluster，")
            log("##[warning]由 Solution Manager 自行決定目標。")
            log("##[warning]若需指定，請在 APIM 發布 /environments/{id}/servers，")
            log("##[warning]或於 clusterId 參數明確填入。")

    if args.stage == "servers":
        log("")
        log("stage=servers，就此結束")
        log("後續部署可用：environmentId=%s clusterId=%s"
            % (environment_id, cluster_id if cluster_id else "(未指定)"))
        return

    # 階段 3：建立 revision
    placeholder = {"", "-"}

    # VQL 來源，依序：DESC API > 檔案 > 直接傳入的 base64
    components = [c.strip() for c in args.components.split(",")
                  if c.strip() and c.strip() != "-"]

    if components:
        vql_b64 = fetch_vql_from_api(
            components, args.desc_api_path,
            database=None if args.database in placeholder else args.database,
            transform=not args.no_transform,
            keep_drop_types=args.keep_drop_types)
    elif args.vql_file not in placeholder:
        log("")
        log("--- 讀取 VQL 檔案 ---")
        vql_b64 = read_as_base64(args.vql_file, "VQL")
    elif args.vql_base64 not in placeholder:
        vql_b64 = args.vql_base64
    else:
        fail("stage=%s 需要 --components、--vql-file 或 --vql-base64 其一"
             % args.stage)

    if args.properties_file not in placeholder:
        props_b64 = read_as_base64(args.properties_file, "properties")
    elif args.properties_base64 not in placeholder:
        props_b64 = args.properties_base64
    else:
        props_b64 = None

    revision_name = args.name
    if revision_name in placeholder:
        if components:
            revision_name = "deploy-%s" % ",".join(components)[:80]
            log("未指定 revision 名稱，自動命名為：%s" % revision_name)
        else:
            fail("stage=%s 需要 --name" % args.stage)

    revision_id = create_revision(
        revision_name,
        "" if args.description in placeholder else args.description,
        vql_b64, props_b64)
    log("##vso[task.setvariable variable=revisionId]%s" % revision_id)

    if args.stage == "revision":
        log("")
        log("stage=revision，就此結束")
        return

    # 階段 4：驗證。失敗即中止，不往下部署。
    ok, summary, detail = validate_revision(
        revision_id, environment_id,
        "" if args.description in placeholder else args.description,
        allow_revision_fallback=args.allow_revision_fallback)

    # 輸出結構化結果，供呼叫端（bot）取用並呈現給使用者
    log("##vso[task.setvariable variable=validationState]%s"
        % detail.get("state", ""))
    log("##vso[task.setvariable variable=validationSummary]%s" % summary)
    log("")
    log("驗證結果摘要：%s" % json.dumps(detail, ensure_ascii=False))

    if not ok:
        fail("驗證未通過，已中止部署。原因：%s" % summary)

    if args.stage == "validate":
        log("")
        log("stage=validate，就此結束")
        return

    # 階段 5、6：部署並等待完成
    deployment_id = start_deployment(
        revision_id, environment_id, cluster_id)
    log("##vso[task.setvariable variable=deploymentId]%s" % deployment_id)

    wait_for_deployment(deployment_id, args.timeout, args.interval)

    log("")
    log("=" * 64)
    log("全部完成　revision=%s deployment=%s" % (revision_id, deployment_id))
    log("=" * 64)


if __name__ == "__main__":
    main()
