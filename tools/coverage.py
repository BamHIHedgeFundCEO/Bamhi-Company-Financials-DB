#!/usr/bin/env python3
"""
對照表覆蓋率診斷工具 —— 迭代 xbrl_zh_map.json 的核心。

用途：任何 ticker 丟進來，它告訴你
  1. 每個科目「抓到幾期 / 缺幾期」
  2. 完全沒抓到的科目，這家公司「實際可能用哪個 XBRL 標籤」（附樣本數值）
  3. 直接產生可貼進 xbrl_zh_map.json 的建議

用法：
  python tools/coverage.py NVDA
  python tools/coverage.py NVDA AAPL TSLA RKLB     # 多檔一起看，找系統性缺口
  python tools/coverage.py NVDA --suggest          # 印出建議加入的標籤
  python tools/coverage.py --scan sp500.txt        # 批次掃描（每行一個 ticker）

不需要跑伺服器，直接打 SEC。改完 config/xbrl_zh_map.json 後重新部署即生效。
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import date

# Windows 主控台預設 cp950，強制 UTF-8 才能印中文與符號
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

UA = os.environ.get("SEC_USER_AGENT", "BamHI frank940702@gmail.com")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(ROOT, "config", "xbrl_zh_map.json")

# 一年內財報常見的英文關鍵詞 → 用來替「缺的科目」在該公司標籤庫裡找候選
STOPWORDS = {"of", "and", "the", "net", "total", "current", "for", "to", "from", "in"}


def fetch(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req) as r:
        return json.load(r)


def resolve_cik(ticker: str) -> str | None:
    data = fetch("https://www.sec.gov/files/company_tickers.json")
    t = ticker.upper()
    for row in data.values():
        if row["ticker"].upper() in (t, t.replace(".", "-"), t.replace("-", ".")):
            return str(row["cik_str"]).zfill(10)
    return None


def days(p: dict):
    if not p.get("start"):
        return None
    return (date.fromisoformat(p["end"]) - date.fromisoformat(p["start"])).days


def tag_has_data(facts: dict, tag: str, unit_prefs: list[str]) -> tuple[bool, str, float]:
    """該標籤是否有近年資料。回傳 (有無, 最新期末, 最新值)。"""
    for ns in ("us-gaap", "ifrs-full"):
        node = facts.get(ns, {}).get(tag)
        if not node:
            continue
        for u in unit_prefs:
            pts = node.get("units", {}).get(u)
            if pts:
                latest = max(pts, key=lambda p: p["end"])
                return True, latest["end"], latest["val"]
    return False, "", 0.0


def unit_prefs(unit: str) -> list[str]:
    return {
        "USD": ["USD", "TWD", "EUR"],
        "shares": ["shares"],
        "USD/shares": ["USD/shares", "TWD/shares"],
    }.get(unit, ["USD"])


def keywords(concept: dict) -> list[str]:
    words = re.findall(r"[A-Za-z]+", concept["en"] + " " + concept["id"])
    return [w for w in words if len(w) > 3 and w.lower() not in STOPWORDS]


# 這些字出現在標籤名裡多半是稅務/調節/明細雜訊，不是主科目
NOISE = ("tax", "deferred", "reconciliation", "intrinsic", "sharebased", "sharebasedcompensation",
         "fairvalue", "unrealized", "maturities", "acquired", "businesscombination", "note")


def suggest_tags(facts: dict, concept: dict, existing: set[str]) -> list[tuple[str, str, float]]:
    """在該公司標籤庫裡，找名稱含關鍵詞、且有資料、尚未在對照表的候選標籤。"""
    kws = [k.lower() for k in keywords(concept)]
    if not kws:
        return []
    up = unit_prefs(concept["unit"])
    scored = []
    for ns in ("us-gaap", "ifrs-full"):
        for tag, node in facts.get(ns, {}).items():
            if tag in existing:
                continue
            tl = tag.lower()
            hits = sum(k in tl for k in kws)
            if hits == 0:
                continue
            if any(nz in tl for nz in NOISE):
                continue  # 剔除稅務/明細雜訊
            for u in up:
                pts = node.get("units", {}).get(u)
                if not pts:
                    continue
                latest = max(pts, key=lambda p: p["end"])
                if latest["end"] < "2022-01-01":
                    break
                # 分數：命中關鍵詞數優先，其次標籤越短越可能是主科目
                score = (hits, -len(tag))
                scored.append((score, tag, latest["end"], latest["val"]))
                break
    scored.sort(key=lambda x: x[0], reverse=True)
    return [(t, e, v) for _, t, e, v in scored[:5]]


def analyse(ticker: str, xbrl_map: dict, suggest: bool):
    cik = resolve_cik(ticker)
    if not cik:
        print(f"  {ticker}: 找不到 CIK（可能下市/ETF/外國）")
        return {}
    try:
        facts = fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json")["facts"]
    except Exception as e:
        print(f"  {ticker}: companyfacts 讀取失敗 {e}")
        return {}

    is_ifrs = "us-gaap" not in facts or len(facts.get("us-gaap", {})) < 20
    covered, missing = [], []
    for c in xbrl_map["concepts"]:
        tags = c.get("tags_ifrs", []) if is_ifrs else c["tags"]
        hit = None
        for tag in tags:
            ok, end, val = tag_has_data(facts, tag, unit_prefs(c["unit"]))
            if ok:
                hit = (tag, end, val)
                break
        (covered if hit else missing).append((c, hit))

    print(f"\n{'='*60}\n{ticker}  (CIK {cik}{'  · IFRS 外國發行人' if is_ifrs else ''})")
    print(f"覆蓋 {len(covered)}/{len(xbrl_map['concepts'])} 科目，缺 {len(missing)}")
    if missing:
        print("\n缺少的科目：")
        for c, _ in missing:
            print(f"  ✗ {c['id']:22s} {c['zh']}")
            if suggest:
                cand = suggest_tags(facts, c, set(c.get("tags_ifrs", []) if is_ifrs else c["tags"]))
                if cand:
                    for tag, end, val in cand:
                        print(f"      候選 → {tag}  (最新 {end}: {val:,.0f})")
                else:
                    print(f"      （該公司標籤庫找不到相近標籤，很可能是這家真的沒有此項目）")
    return {c["id"]: (hit is not None) for c, hit in covered + missing}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*", help="一或多個 ticker")
    ap.add_argument("--suggest", action="store_true", help="替缺的科目建議候選標籤")
    ap.add_argument("--scan", metavar="FILE", help="批次：每行一個 ticker 的檔案")
    args = ap.parse_args()

    xbrl_map = json.load(open(MAP_PATH, encoding="utf-8"))
    tickers = list(args.tickers)
    if args.scan:
        tickers += [ln.strip() for ln in open(args.scan) if ln.strip()]
    if not tickers:
        ap.error("請給至少一個 ticker，或用 --scan 檔案")

    # 批次時統計每個科目在多少公司缺 → 找系統性缺口（最該優先補的標籤）
    miss_count = defaultdict(int)
    total = 0
    for t in tickers:
        cov = analyse(t, xbrl_map, args.suggest or len(tickers) == 1)
        if cov:
            total += 1
            for cid, ok in cov.items():
                if not ok:
                    miss_count[cid] += 1
        time.sleep(0.15)  # SEC 限速

    if total > 1 and miss_count:
        print(f"\n{'='*60}\n系統性缺口（{total} 家中缺最多的科目，優先補）：")
        zh = {c["id"]: c["zh"] for c in xbrl_map["concepts"]}
        for cid, cnt in sorted(miss_count.items(), key=lambda x: -x[1])[:15]:
            bar = "█" * cnt
            print(f"  {cid:22s} {zh.get(cid,''):16s} {cnt:2d}/{total} {bar}")


if __name__ == "__main__":
    main()
