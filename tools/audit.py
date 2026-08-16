#!/usr/bin/env python3
"""
資料品質稽核 —— 系統性找「真問題」，取代人工逐檔檢查。

原理：n/a 分三種
  1. 邊緣 n/a（序列頭或尾）——通常正常：範圍起點缺 TTM、上市前、會計準則生效前
  2. 中間 n/a（前後都有值，中間突然缺）——**可疑，多半是處理 bug 或漏標籤**
  3. 整條 n/a——公司真的沒有此項目（正常）

本工具只揪「中間 n/a」，並跨多檔統計哪個科目最常中間缺 → 那就是最該修的根因。
不需人工看，給幾百檔它自己排序出問題。

用法：
  python tools/audit.py NVDA
  python tools/audit.py NVDA AAPL MSFT TSLA AMZN     # 批次找系統性中間缺口
  python tools/audit.py --scan russell1000.txt        # 大規模
  python tools/audit.py NVDA --base https://bamhi-company-financials.vercel.app
"""
import argparse
import io
import json
import sys
import time
import urllib.request
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

DEFAULT_BASE = "http://localhost:3210"


def fetch(base: str, ticker: str) -> dict | None:
    url = f"{base}/api/financials?ticker={ticker}&from=2018Q1&to=2027Q1"
    try:
        with urllib.request.urlopen(url, timeout=40) as r:
            return json.load(r)
    except Exception as e:
        print(f"  {ticker}: 讀取失敗 {e}")
        return None


def interior_gaps(periods: list, values: dict) -> list:
    """回傳中間 n/a 的期別（前後都有值）。純頭尾缺不算。"""
    present = [p for p in periods if p in values and _num(values[p]) is not None]
    if len(present) < 2:
        return []
    first, last = periods.index(present[0]), periods.index(present[-1])
    gaps = []
    for i in range(first + 1, last):
        p = periods[i]
        if p not in values or _num(values[p]) is None:
            gaps.append(p)
    return gaps


def _num(v):
    if isinstance(v, dict):
        return v.get("value")
    return v


def audit(d: dict) -> dict:
    """回傳 {概念id: [中間缺的期別]}。涵蓋科目、衍生指標、估值倍數。"""
    periods = d["periods"]
    out = {}
    for li in d.get("lineItems", []):
        g = interior_gaps(periods, li["values"])
        if g:
            out[f"科目/{li['zh']}"] = g
    # 衍生指標與估值倍數是公式/計算值，中間缺通常反映底層科目缺 → 也列
    for row in (d.get("valuation") or {}).get("rows", []):
        g = interior_gaps(periods, row["values"])
        if g:
            out[f"估值/{row['zh']}"] = g
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--scan", metavar="FILE")
    ap.add_argument("--base", default=DEFAULT_BASE)
    args = ap.parse_args()
    tickers = list(args.tickers)
    if args.scan:
        tickers += [ln.strip() for ln in open(args.scan) if ln.strip()]
    if not tickers:
        ap.error("給 ticker 或 --scan")

    systemic = defaultdict(int)
    total = 0
    for t in tickers:
        d = fetch(args.base, t)
        if not d:
            continue
        total += 1
        gaps = audit(d)
        if len(tickers) <= 5:  # 少量：印明細
            print(f"\n{'='*56}\n{t}  {d.get('company','')}")
            if not gaps:
                print("  ✓ 無中間 n/a（頭尾邊緣缺屬正常）")
            for k, ps in sorted(gaps.items(), key=lambda x: -len(x[1])):
                short = [p.replace("FY", "").replace(" ", "") for p in ps]
                print(f"  ⚠ {k:24s} 中間缺 {len(ps)}: {short[:8]}")
        for k in gaps:
            systemic[k] += 1
        time.sleep(0.1)

    if total > 1:
        print(f"\n{'='*56}\n系統性中間缺口（{total} 家中出現最多 → 最該修的根因）：")
        for k, cnt in sorted(systemic.items(), key=lambda x: -x[1])[:20]:
            print(f"  {cnt:3d}/{total}  {'█'*min(cnt,40)}  {k}")


if __name__ == "__main__":
    main()
