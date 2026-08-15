"""
Vercel Python function 入口（excel-service 的 FastAPI app）。
第二個 Vercel project（rootDirectory=repo 根層）用此檔；主站 project（rootDirectory=web）不碰。
vercel.json 以 includeFiles 把 excel-service/ 與 config/ 打包進 function。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "excel-service"))

from main import app  # noqa: E402  (FastAPI ASGI app，Vercel Python runtime 自動偵測 `app`)
