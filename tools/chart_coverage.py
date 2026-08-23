#!/usr/bin/env python3
"""
網頁「財務報表」分頁的圖表覆蓋率盤點。

回答的問題只有一個：**哪些公司會少畫哪一張圖。**
頁面的規則是「缺資料就整張不畫」，所以使用者看到少一張圖時，要能在這裡查到
是哪個科目沒申報 —— 而不是以為網站壞了。

資料來源：`tools/sweep_out/coverage.jsonl`（`sweep.py` 的產物，羅素 3000）。
per-company 零 SEC 請求。

⚠️ 這份報告的單位是「家」不是「期」。coverage.jsonl 記的是「這家公司歷史上
有沒有這個科目」；網頁畫的是特定期間，所以這裡說「有」的公司仍可能在某幾季
缺值（那時該圖照畫、缺的點斷開）。反過來，這裡說「沒有」的一定畫不出來。

用法：
  python tools/chart_coverage.py                 # 摘要 + 各圖缺漏家數
  python tools/chart_coverage.py --list eps      # 列出畫不出 EPS 圖的公司
  python tools/chart_coverage.py --csv out.csv   # 逐家明細
"""
import argparse
import csv
import io
import json
import os
import sys
from collections import Counter, defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
COVERAGE = os.path.join(ROOT, "tools", "sweep_out", "coverage.jsonl")

# 圖 → (中文名, 判定函式)。判定與 web/pages/stock/[ticker]/financials.vue 同步
CHARTS = [
    ("income", "損益表大方向", lambda h: "revenue" in h and "net_income" in h),
    ("growth", "營收與季增率", lambda h: "revenue" in h),
    ("eps", "每股盈餘", lambda h: "eps_diluted" in h or "eps_basic" in h),
    ("balance", "資產負債表", lambda h: sum(
        k in h for k in ("current_assets", "total_assets", "current_liabilities",
                         "total_liabilities", "equity", "equity_total")) >= 2),
    ("waterfall", "利潤瀑布", lambda h: "revenue" in h and "net_income" in h),
]

# 圖上的折線（缺了只少一條線，圖照畫）
LINES = [
    ("gross_margin", "毛利率", "gross_profit"),
    ("operating_margin", "營業利益率", "operating_income"),
    ("opex_bar", "營業費用長條", "opex_total"),
]


def load(path):
    rows = []
    with io.open(path, encoding="utf-8") as f:
        for line in f:
            rows.append(json.loads(line))
    return rows


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", help="列出畫不出這張圖的公司（income/growth/eps/balance/waterfall）")
    ap.add_argument("--line", help="列出少這一條線的公司（gross_margin/operating_margin/opex_bar）")
    ap.add_argument("--csv", help="逐家明細寫成 CSV")
    ap.add_argument("--limit", type=int, default=60)
    args = ap.parse_args()

    if not os.path.exists(COVERAGE):
        print(f"找不到 {COVERAGE}；先跑 tools/sweep.py")
        return
    rows = load(COVERAGE)
    n = len(rows)

    missing = defaultdict(list)      # chart_id → [ticker]
    line_missing = defaultdict(list)
    by_sector = defaultdict(Counter)
    per_firm = []

    for d in rows:
        h = d["hits"]
        gone = []
        for cid, zh, ok in CHARTS:
            if not ok(h):
                missing[cid].append(d["ticker"])
                gone.append(zh)
                by_sector[cid][d["sector"] or "（未分類）"] += 1
        lgone = []
        for lid, zh, concept in LINES:
            if concept not in h:
                line_missing[lid].append(d["ticker"])
                lgone.append(zh)
        per_firm.append((d["ticker"], d["sector"], d["industry"],
                         5 - len(gone), "／".join(gone), "／".join(lgone)))

    print("═" * 78)
    print(f"財務報表分頁的圖表覆蓋率 · 母體 {n} 家（羅素 3000）")
    print("═" * 78)
    for cid, zh, _ in CHARTS:
        m = len(missing[cid])
        print(f"  {zh:12} 畫得出 {n - m:>5} 家（{(n - m) / n * 100:5.1f}%）　畫不出 {m} 家")
        if m:
            top = by_sector[cid].most_common(4)
            print(f"{'':16}集中在：" + "、".join(f"{s} {c}" for s, c in top))

    print("\n── 折線（缺了只少一條線，圖仍照畫）" + "─" * 40)
    for lid, zh, concept in LINES:
        m = len(line_missing[lid])
        print(f"  {zh:12} 有 {n - m:>5} 家（{(n - m) / n * 100:5.1f}%）　缺 {m} 家"
              f"　← 科目 {concept}")

    full = sum(1 for r in per_firm if r[3] == 5 and not r[5])
    print(f"\n五張圖與三條線全部畫得出來：{full} 家（{full / n * 100:.1f}%）")

    if args.list:
        cid = args.list
        names = {c[0]: c[1] for c in CHARTS}
        if cid not in names:
            print(f"\n--list 只接受：{'、'.join(names)}")
            return
        lst = missing[cid]
        print(f"\n畫不出「{names[cid]}」的 {len(lst)} 家：")
        for i in range(0, min(len(lst), args.limit), 12):
            print("  " + " ".join(f"{t:<7}" for t in lst[i:i + 12]))
        if len(lst) > args.limit:
            print(f"  …共 {len(lst)} 家（--limit 調整；完整清單用 --csv）")

    if args.line:
        names = {l[0]: l[1] for l in LINES}
        if args.line not in names:
            print(f"\n--line 只接受：{'、'.join(names)}")
            return
        lst = line_missing[args.line]
        print(f"\n少「{names[args.line]}」的 {len(lst)} 家：")
        for i in range(0, min(len(lst), args.limit), 12):
            print("  " + " ".join(f"{t:<7}" for t in lst[i:i + 12]))

    if args.csv:
        with io.open(args.csv, "w", encoding="utf-8-sig", newline="") as f:
            w = csv.writer(f)
            w.writerow(["ticker", "sector", "industry", "畫得出幾張圖", "畫不出的圖", "缺的折線"])
            w.writerows(sorted(per_firm, key=lambda r: (r[3], r[0])))
        print(f"\n逐家明細 → {args.csv}")


if __name__ == "__main__":
    main()
