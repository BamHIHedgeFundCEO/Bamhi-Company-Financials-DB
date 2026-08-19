#!/usr/bin/env python3
"""
「營收整列無值」的公司 → 找出它們實際用哪個標籤報總營收。

為什麼需要這支：coverage.py 只問「map 裡的標籤在這家公司存不存在」，
XEL 的 RevenueFromContractWithCustomerExcludingAssessedTax 確實存在（只是帶維度
或只有零星幾筆），於是覆蓋率顯示「營收有涵蓋」，實際整條線是空的
（見 memory: coverage-sweep-false-negatives）。這支直接看**無維度、季度長度、
最近兩年**的事實筆數，排出候選。

  python tools/revenue_tags.py AGNC XEL TFC ...
"""
import io
import json
import os
import sys
import time
import urllib.request
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from sweep import build_cik_lut  # noqa: E402

UA = {"User-Agent": os.environ.get("SEC_USER_AGENT", "BamHI frank940702@gmail.com")}
HINT = ("Revenue", "Revenues", "InterestAndDividendIncome", "InterestIncome",
        "NoninterestIncome", "Sales", "PremiumsEarned", "OperatingLeases")


def facts(cik: str) -> dict:
    req = urllib.request.Request(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik}.json", headers=UA)
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.load(r)


def main() -> None:
    lut = build_cik_lut()
    cutoff = date.today().year - 2
    for t in sys.argv[1:]:
        cik = lut.get(t.upper())
        if not cik:
            print(f"{t}: 名冊查無")
            continue
        try:
            d = facts(cik)
        except Exception as e:
            print(f"{t}: {str(e)[:60]}")
            continue
        ns = d["facts"].get("us-gaap") or d["facts"].get("ifrs-full") or {}
        rows = []
        for tag, body in ns.items():
            if not any(h in tag for h in HINT):
                continue
            for u, pts in body["units"].items():
                if not (len(u) == 3 and u.isupper()):
                    continue
                q = [p for p in pts if p.get("start")
                     and 45 <= (date.fromisoformat(p["end"]) - date.fromisoformat(p["start"])).days <= 130
                     and int(p["end"][:4]) >= cutoff]
                if len(q) < 2:
                    continue
                newest = max(q, key=lambda p: p["end"])
                rows.append((len(q), tag, u, newest["end"], newest["val"]))
        rows.sort(reverse=True)
        print(f"\n=== {t} ({d.get('entityName','')[:40]}) ===")
        for n, tag, u, end, val in rows[:8]:
            print(f"  {n:4}筆 {tag[:56]:58} {u} {end} {val:,.0f}")
        if not rows:
            print("  （找不到任何無維度的季度營收類事實）")
        time.sleep(0.2)


if __name__ == "__main__":
    main()
