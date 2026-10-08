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
import json
import os
import sys
import time

import requests


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

def validate_revision(revision_id):
    log("")
    log("--- 驗證 revision %s ---" % revision_id)
    resp = _request("POST", "/revisions/%s/validate" % revision_id)

    try:
        result = resp.json()
    except ValueError:
        log("驗證回應非 JSON：%s" % resp.text[:500])
        return None

    log("完整回應：%s" % json.dumps(result, ensure_ascii=False, indent=2)[:2000])

    if result.get("errors"):
        fail("Revision 驗證失敗：%s" % result["errors"])

    log("驗證通過")
    return result


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
    ap.add_argument("--vql-base64", default="",
                    help="VQL 內容，base64 編碼的 UTF-8，不得含 BOM")
    ap.add_argument("--properties-base64", default="",
                    help="選填。環境專屬 properties 檔內容，base64")
    ap.add_argument("--environment", "--environment-id", dest="environment",
                    default="",
                    help="目標環境，可填名稱（如 ACLDTPLTFRM-PRD）或數字 id")
    ap.add_argument("--cluster-id", default="",
                    help="選填。留空則由目標環境的 server 清單自動推導")
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
    if args.name in placeholder or args.vql_base64 in placeholder:
        fail("stage=%s 需要 --name 與 --vql-base64（目前是空值或占位符）"
             % args.stage)

    revision_id = create_revision(
        args.name,
        "" if args.description in placeholder else args.description,
        args.vql_base64,
        None if args.properties_base64 in placeholder else args.properties_base64)
    log("##vso[task.setvariable variable=revisionId]%s" % revision_id)

    if args.stage == "revision":
        log("")
        log("stage=revision，就此結束")
        return

    # 階段 4：驗證
    validate_revision(revision_id)
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
