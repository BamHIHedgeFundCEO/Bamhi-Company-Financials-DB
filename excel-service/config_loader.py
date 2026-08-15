"""設定層載入：config/ 的三個 JSON。程式只執行，內容全由 JSON 決定。"""
import json
import os
from functools import lru_cache

# Docker 內 config/ 複製到 /app/config；本地開發時取 repo 根層 ../config
_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "config"),
    os.path.join(os.path.dirname(__file__), "..", "config"),
]


def _config_dir() -> str:
    for c in _CANDIDATES:
        if os.path.isdir(c):
            return c
    raise FileNotFoundError("找不到 config/ 目錄")


@lru_cache(maxsize=None)
def load(name: str) -> dict:
    with open(os.path.join(_config_dir(), name), encoding="utf-8") as f:
        return json.load(f)


def xbrl_map() -> dict:
    return load("xbrl_zh_map.json")


def chart_spec() -> dict:
    return load("chart_spec.json")


def theme() -> dict:
    return load("theme.json")
