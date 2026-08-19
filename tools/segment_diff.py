#!/usr/bin/env python3
"""
分部快取兩版逐格 diff。改對帳／上層判定邏輯後一定要跑 —— 只看總數會漏掉
「補了 A 壞了 B」（financials.ts 那邊就靠逐格 diff 抓到自己引入的兩個 bug）。

  python tools/segment_diff.py                       # seg_before → seg
  python tools/segment_diff.py --a X --b Y --detail 20
"""
import argparse
import gzip
import io
import json
import os
import sys
from collections import Counter, defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tools", "sweep_out")


def flatten(d: dict) -> dict:
    """(軸, 成員, 期間, 科目) → (值, verified, isParent)"""
    out = {}
    if d.get("_error"):
        return out
    for ax in d.get("axes", []):
        for mem in ax.get("members", []):
            for p, concepts in (mem.get("values") or {}).items():
                for cid, cell in concepts.items():
                    out[(ax["axis"], mem["key"], p, cid)] = (
                        cell.get("value"), cell.get("verified"), cell.get("isParent"))
    return out


def load(dirname: str, t: str):
    p = os.path.join(OUT, dirname, f"{t}.json.gz")
    if not os.path.exists(p):
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def vs(v) -> str:
    return "對得上" if v is True else ("對不上" if v is False else "無法校驗")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="seg_before")
    ap.add_argument("--b", default="seg")
    ap.add_argument("--detail", type=int, default=10, help="印前 N 家的逐格明細")
    args = ap.parse_args()

    da, db = os.path.join(OUT, args.a), os.path.join(OUT, args.b)
    tickers = sorted({f.split(".json")[0] for f in os.listdir(da)}
                     & {f.split(".json")[0] for f in os.listdir(db)})

    trans = Counter()      # (前, 後) → 格數
    per_ticker = defaultdict(Counter)
    value_moved = Counter()
    added, removed = Counter(), Counter()

    for t in tickers:
        a, b = load(args.a, t), load(args.b, t)
        if a is None or b is None:
            continue
        fa, fb = flatten(a), flatten(b)
        for k in set(fa) | set(fb):
            x, y = fa.get(k), fb.get(k)
            if x is None:
                added[t] += 1
                continue
            if y is None:
                removed[t] += 1
                continue
            if x[0] != y[0]:
                value_moved[t] += 1
            if x[1] != y[1]:
                trans[(vs(x[1]), vs(y[1]))] += 1
                per_ticker[t][(vs(x[1]), vs(y[1]))] += 1

    print(f"比對 {len(tickers)} 家：{args.a} → {args.b}\n")
    print("【校驗狀態變化】")
    for (x, y), n in sorted(trans.items(), key=lambda kv: -kv[1]):
        print(f"  {x} → {y:<8}{n:>7} 格")
    if not trans:
        print("  （無變化）")

    print("\n【數值本身改變】（不該有，抽取邏輯沒動）")
    if value_moved:
        for t, n in value_moved.most_common(20):
            print(f"  {t:<8}{n:>6} 格")
    else:
        print("  （無）")

    print("\n【格子增減】")
    for t, n in added.most_common(10):
        print(f"  +{n:<5} {t}")
    for t, n in removed.most_common(10):
        print(f"  -{n:<5} {t}")
    if not added and not removed:
        print("  （無）")

    print("\n【逐家】變好 = 對不上/無法校驗 → 對得上；變壞 = 反向")
    rows = []
    for t, c in per_ticker.items():
        better = sum(n for (x, y), n in c.items() if y == "對得上" and x != "對得上")
        worse = sum(n for (x, y), n in c.items() if x == "對得上" and y != "對得上")
        other = sum(c.values()) - better - worse
        rows.append((better - worse, t, better, worse, other))
    rows.sort(key=lambda r: -r[0])
    print(f"  {'ticker':<9}{'變好':>6}{'變壞':>6}{'其他':>6}")
    for _d, t, bt, ws, ot in rows[: args.detail]:
        print(f"  {t:<9}{bt:>6}{ws:>6}{ot:>6}")
    if len(rows) > args.detail:
        print("  …")
        for _d, t, bt, ws, ot in rows[-3:]:
            print(f"  {t:<9}{bt:>6}{ws:>6}{ot:>6}")
    tot_b = sum(r[2] for r in rows)
    tot_w = sum(r[3] for r in rows)
    print(f"\n  合計：變好 {tot_b} 格 / 變壞 {tot_w} 格 / "
          f"改善家數 {sum(1 for r in rows if r[0] > 0)} / 變壞家數 {sum(1 for r in rows if r[0] < 0)}")


if __name__ == "__main__":
    main()
