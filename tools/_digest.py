"""設定檔的「這一段內容」指紋 —— 生成端與檢查端共用同一支，避免兩份實作。

為什麼不直接比 `version`：`config/scoring.json` 同時裝了兩個方向的東西 ——
`dimensions`／`models`／兩道覆蓋率門檻是 `peer_stats.py` 的**輸入**，而 `grades`
（評級級距）是它的**輸出**（級距＝全市場總分的分位數，錨點一換就要重訂）。
拿整份的 `version` 當相依會成環：重訂級距要升版，一升版錨點又被判成過期，
而那兩件事根本不是同一個方向。所以只對「真的餵進批次的那幾個鍵」取指紋。
"""
import hashlib
import json


def config_digest(cfg: dict, keys: list[str]) -> str:
    """cfg 裡 keys 這幾段的指紋。鍵排序、緊湊分隔 -> 只有內容變才會變。"""
    payload = json.dumps({k: cfg.get(k) for k in sorted(keys)},
                         sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
