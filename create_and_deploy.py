"""
串接 Denodo Solution Manager REST API:
1) 建立 Revision (loadFromVQL)
2) 驗證 Revision (validate)
3) 觸發部署 (deployments)
4) 輪詢部署進度 (progress)
 
由 Azure Pipelines 呼叫，參數從 command line 傳入，
帳密從環境變數 SM_USER / SM_PASSWORD 讀取（對應 pipeline 的 secret variable）。
"""
 
import argparse
import base64
import os
import time
from datetime import datetime
 
import requests

parser = argparse.ArgumentParser(description="Denodo Solution Manager CI deployment")
parser.add_argument("--name", required=True, help="Revision name")
args = parser.parse_args()
print(args.name)
