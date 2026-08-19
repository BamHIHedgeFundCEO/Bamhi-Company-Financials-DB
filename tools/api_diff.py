#!/usr/bin/env python3
"""
三表快取兩版逐格 diff（api_sweep.py 落地的 {TICKER}.n.json.gz）。

改期間／幣別／標籤選擇邏輯後一定要跑：只看總數會漏掉「補了 A 壞了 B」。

  python tools/api_diff.py --a api_v3 --b api
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

OUT = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools", "sweep_out")


def flat(d: dict) -> dict:
    o = {}
    for li in d.get("lineItems", []):
        for p, c in (li.get("values") or {}).items():
            o[(li["id"], p)] = c.get("value")
    return o


def load(dirname: str, t: str):
    p = os.path.join(OUT, dirname, f"{t}.n.json.gz")
    if not os.path.exists(p):
        return None
    with gzip.open(p, "rt", encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", default="api_v3")
    ap.add_argument("--b", default="api")
    ap.add_argument("--detail", type=int, default=15)
    args = ap.parse_args()

    da, db = os.path.join(OUT, args.a), os.path.join(OUT, args.b)
    ta = {f.split(".n.json")[0] for f in os.listdir(da) if f.endswith(".n.json.gz")}
    tb = {f.split(".n.json")[0] for f in os.listdir(db) if f.endswith(".n.json.gz")}
    tickers = sorted(ta & tb)

    gained, lost, moved = Counter(), Counter(), Counter()
    by_concept = defaultdict(lambda: [0, 0, 0])   # id → [補上, 消失, 數值改變]
    cur_change = []

    for t in tickers:
        a, b = load(args.a, t), load(args.b, t)
        if not a or not b or a.get("_error") or b.get("_error"):
            continue
        if a.get("currency") != b.get("currency"):
            cur_change.append((t, a.get("currency"), b.get("currency")))
        fa, fb = flat(a), flat(b)
        for k in set(fa) | set(fb):
            x, y = fa.get(k), fb.get(k)
            if x == y:
                continue
            if x is None:
                gained[t] += 1
                by_concept[k[0]][0] += 1
            elif y is None:
                lost[t] += 1
                by_concept[k[0]][1] += 1
            else:
                moved[t] += 1
                by_concept[k[0]][2] += 1

    print(f"比對 {len(tickers)} 家：{args.a} → {args.b}\n")
    print(f"補上（原本 n/a 現在有值）{sum(gained.values()):,} 格 / {len(gained)} 家")
    print(f"消失（原本有值現在 n/a）{sum(lost.values()):,} 格 / {len(lost)} 家")
    print(f"數值改變　　　　　　　　{sum(moved.values()):,} 格 / {len(moved)} 家")

    print("\n【幣別改變】（外國發行人本來被當成美元）")
    for t, x, y in cur_change[:40]:
        print(f"  {t:<8}{x} → {y}")
    if not cur_change:
        print("  （無）")

    print(f"\n【消失最多的公司】—— 這裡要逐家看，是「拒絕生假數字」還是真的弄壞了")
    for t, n in lost.most_common(args.detail):
        print(f"  {t:<8}{n:>6} 格")
    if not lost:
        print("  （無）")

    print("\n【依科目】")
    print(f"  {'科目':<22}{'補上':>8}{'消失':>8}{'數值改變':>10}")
    for cid, (g, l, m) in sorted(by_concept.items(), key=lambda kv: -sum(kv[1]))[:20]:
        print(f"  {cid:<22}{g:>8}{l:>8}{m:>10}")

    print("\n【補上最多的公司】")
    for t, n in gained.most_common(args.detail):
        print(f"  {t:<8}{n:>6} 格")


if __name__ == "__main__":
    main()
