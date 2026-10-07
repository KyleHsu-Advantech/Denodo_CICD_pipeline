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


def _request(method, path, **kwargs):
    """對 SM API 發出請求，失敗時印出完整回應以利除錯。"""
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

def list_environments():
    log("")
    log("--- 取得環境清單 ---")
    resp = _request("GET", "/environments")

    try:
        data = resp.json()
    except ValueError:
        fail("環境清單回應非 JSON：%s" % resp.text[:500])

    log("完整回應：%s" % json.dumps(data, ensure_ascii=False, indent=2)[:3000])

    items = data if isinstance(data, list) else data.get("items", [])
    log("")
    log("環境摘要：")
    for env in items:
        log("  id=%s  name=%s" % (env.get("id"), env.get("name")))

    return items


# ---------------------------------------------------------------- 2. 伺服器清單

def list_servers(environment_id):
    log("")
    log("--- 取得環境 %s 的伺服器 ---" % environment_id)
    resp = _request("GET", "/environments/%s/servers" % environment_id)

    try:
        data = resp.json()
    except ValueError:
        fail("伺服器清單回應非 JSON：%s" % resp.text[:500])

    log("完整回應：%s" % json.dumps(data, ensure_ascii=False, indent=2)[:3000])

    items = data if isinstance(data, list) else data.get("items", [])
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
    ap.add_argument("--environment-id", default="", help="目標環境 id")
    ap.add_argument("--cluster-id", default="", help="目標 cluster id")
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
    list_environments()
    if args.stage == "environments":
        log("")
        log("stage=environments，就此結束")
        return

    # 階段 2：伺服器與 clusterId
    if not args.environment_id:
        fail("stage=%s 需要 --environment-id" % args.stage)
    list_servers(args.environment_id)
    if args.stage == "servers":
        log("")
        log("stage=servers，就此結束")
        return

    # 階段 3：建立 revision
    if not args.name or not args.vql_base64:
        fail("stage=%s 需要 --name 與 --vql-base64" % args.stage)

    revision_id = create_revision(
        args.name, args.description,
        args.vql_base64, args.properties_base64 or None)
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
        revision_id, args.environment_id, args.cluster_id or None)
    log("##vso[task.setvariable variable=deploymentId]%s" % deployment_id)

    wait_for_deployment(deployment_id, args.timeout, args.interval)

    log("")
    log("=" * 64)
    log("全部完成　revision=%s deployment=%s" % (revision_id, deployment_id))
    log("=" * 64)


if __name__ == "__main__":
    main()
