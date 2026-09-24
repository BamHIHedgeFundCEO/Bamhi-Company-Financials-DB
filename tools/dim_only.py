#!/usr/bin/env python3
"""「這家公司只用維度揭露這個科目」的離線盤點（零 SEC 請求）。

    python tools/dim_only.py 2025q3.zip … 2026q2.zip --out config/dim_only.json

為什麼要這一份：companyfacts **只收無維度事實**。有些公司的資產負債表上明明有那一行，
但事實整批帶著維度申報 —— AES 的長期負債按有／無追索權拆、AMP 按是否 VIE、
BRK-B 按法人實體 × 幣別、ALK 的合約負債按 AirTrafficLiability／LoyaltyPlanRevenue 拆。
於是我們什麼都抓不到，格子寫 n/a，而 n/a 對讀者說的是「該申報卻抓不到，你自己去 EDGAR 查」
—— 他查到的會是那一行明明就在上面，然後以為我們壞掉。

正確的說法是第三種：**公司有這一行，但只用維度揭露，這條路取不到**。這不是
「不適用」（寫「—」等於說公司沒有這門生意，那是說謊），也不是「我們漏標籤」。

判定要兩個條件同時成立，少一個都會誤報：

  ① 這個科目的某個已對照標籤出現在 pre.txt 的**該張報表**上
     —— 證明那一行真的印在報表上，不是只在附註裡提過
  ② 該科目在整個視窗內**沒有任何無維度事實**，但**有帶維度的事實**
     —— 只看 ② 的話，只在附註揭露的科目也會被算進來

**不回補數值**：按軸加總回補合計實測只有 46–62% 落在 ±3%（見 CLAUDE.md），
這份表只回答「為什麼是空的」，不產生任何數字。
"""
import argparse
import csv
import io
import json
import os
import sys
import zipfile
from collections import defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(ROOT, "config/xbrl_zh_map.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zips", nargs="+", help="DERA FSDS 季度 zip")
    ap.add_argument("--out", default="config/dim_only.json")
    args = ap.parse_args()

    with open(MAP_PATH, encoding="utf-8") as f:
        m = json.load(f)
    # internal 科目不輸出成報表列，讀者看不到它的留白，收了只是讓表變大
    tag2cid, stmt_of = {}, {}
    for c in m["concepts"]:
        if c.get("internal"):
            continue
        stmt_of[c["id"]] = c.get("statement")
        for t in (c.get("tags") or []) + (c.get("tags_ifrs") or []):
            tag2cid[t] = c["id"]

    presented = defaultdict(set)   # cik -> {cid}  ①：那一行印在該張報表上
    plain = defaultdict(set)       # cik -> {cid}  有無維度事實
    dimmed = defaultdict(set)      # cik -> {cid}  有帶維度事實

    for z in args.zips:
        print(f"→ {z}", file=sys.stderr)
        with zipfile.ZipFile(z) as zf:
            sub = {}
            with zf.open("sub.txt") as fh:
                for r in csv.DictReader(io.TextIOWrapper(fh, "utf-8", errors="replace"),
                                        delimiter="\t"):
                    if r["form"] in ("10-K", "10-Q", "20-F"):
                        sub[r["adsh"]] = str(int(r["cik"]))
            with zf.open("pre.txt") as fh:
                for r in csv.DictReader(io.TextIOWrapper(fh, "utf-8", errors="replace"),
                                        delimiter="\t"):
                    cik = sub.get(r["adsh"])
                    cid = tag2cid.get(r["tag"])
                    if cik and cid and r["stmt"] == stmt_of.get(cid):
                        presented[cik].add(cid)
            with zf.open("num.txt") as fh:
                for r in csv.DictReader(io.TextIOWrapper(fh, "utf-8", errors="replace"),
                                        delimiter="\t"):
                    cid = tag2cid.get(r["tag"])
                    if cid is None or r["coreg"]:
                        continue
                    cik = sub.get(r["adsh"])
                    if cik is None:
                        continue
                    (dimmed if r["segments"] else plain)[cik].add(cid)

    out, n = {}, 0
    for cik, cids in presented.items():
        hit = sorted(c for c in cids
                     if c in dimmed.get(cik, ()) and c not in plain.get(cik, ()))
        if hit:
            out[cik] = hit
            n += len(hit)

    concepts = sorted({c for v in out.values() for c in v})
    idx = {c: i for i, c in enumerate(concepts)}
    payload = {
        "version": "1.0",
        "generated": __import__("datetime").date.today().isoformat(),
        "source": [os.path.basename(p) for p in args.zips],
        "map_version": m.get("version"),
        "note": ("由 tools/dim_only.py 產生。這家公司的報表上有這一行（pre.txt 的該張報表），"
                 "但整個視窗內只有帶維度的事實、沒有任何無維度事實 —— companyfacts 只收"
                 "無維度事實，所以我們取不到。頁面據此把這一格從 n/a（我們漏抓）改寫成"
                 "「只用維度揭露」。**不回補數值**：按軸加總實測只有 46–62% 落在 ±3%。"
                 "體積考量存成索引：concepts 是科目 id 陣列，companies 的值是逗號分隔的索引。"),
        "concepts": concepts,
        "companies": {k: ",".join(str(idx[c]) for c in v) for k, v in sorted(out.items())},
    }
    dst = os.path.join(ROOT, args.out) if not os.path.isabs(args.out) else args.out
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out):,} 家公司、{n:,} 個 (公司,科目) 只用維度揭露 → {args.out}")
    top = defaultdict(int)
    for v in out.values():
        for c in v:
            top[c] += 1
    for c, k in sorted(top.items(), key=lambda kv: -kv[1])[:12]:
        print(f"  {c:28} {k:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
