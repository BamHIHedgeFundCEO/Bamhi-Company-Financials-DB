#!/usr/bin/env python3
"""
同族標籤候選的驗證器 —— sweep.py 段 1 的把關。

sweep.py 的「同族標籤缺口」是**用字面相似度猜的候選**，不是驗證過的結論。
羅素 3000 跑出來的前幾名長這樣：

    負債總計 + LiabilitiesAndStockholdersEquity   291 家   ← 其實是資產總計
    負債總計 + LiabilitiesCurrent                 289 家   ← 只有流動負債
    短期投資 + MarketableSecuritiesUnrealizedGainLoss 51 家 ← 未實現損益不是餘額
    庫藏股   + TreasuryStockValueAcquiredCostMethod  160 家 ← 當期買回額不是餘額

照收進 config 會產生「看起來正常的錯數字」，比 n/a 危險得多。

驗證方法（與今天驗 `WeightedAverageNumberOfShareOutstandingBasicAndDiluted`
所用的同一套）：拿**同一家公司、同一個期間、主標籤與候選標籤都有值**的那些格子
逐格比。同一個量的兩個標籤在重疊期必然相等；語意不同的會系統性偏離。

⚠️ 只能比同一份申報之外還要注意重編：本工具比的是「同一期間的最新值」，
分割或重編會讓兩個標籤停在不同版本 —— 所以判定看的是**中位數比值**與一致率，
不是要求 100%。

用法：
  python tools/tag_validate.py                      # 驗 coverage.jsonl 裡全部 kin 候選
  python tools/tag_validate.py --concept long_term_debt
  python tools/tag_validate.py --min-overlap 50 --json out.json
"""
import argparse
import gzip
import io
import json
import os
import statistics
import sys
from collections import defaultdict
from concurrent.futures import ProcessPoolExecutor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(ROOT, "config", "xbrl_zh_map.json")
COVERAGE = os.path.join(ROOT, "tools", "sweep_out", "coverage.jsonl")
CACHE = os.environ.get("BAMHI_FACTS_CACHE",
                       os.path.join(os.path.expanduser("~"), ".bamhi-facts-cache"))

AGREE = 0.01        # 逐格視為相等的相對容差
ACCEPT_RATE = 0.90  # 一致率門檻
MIN_OVERLAP = 30    # 可比對的格數下限（低於此視為「無法驗證」，不自動收）
MIN_FIRMS = 3


def load_map() -> dict:
    with io.open(MAP_PATH, encoding="utf-8") as f:
        return json.load(f)


def facts_of(us: dict, tag: str) -> dict:
    """(start, end) → 最新申報的值。instant 的 start 為空字串"""
    out = {}
    node = us.get(tag)
    if not node:
        return out
    for unit, arr in node.get("units", {}).items():
        if unit not in ("USD", "shares", "USD/shares"):
            continue
        for u in arr:
            k = (u.get("start", ""), u["end"])
            prev = out.get(k)
            if prev is None or u["filed"] > prev[1]:
                out[k] = (u["val"], u["filed"])
    return {k: v[0] for k, v in out.items()}


def compare(args):
    """一家公司：對每個 (concept, 候選) 回報重疊格數、相等格數、比值樣本"""
    path, pairs = args
    try:
        with gzip.open(path, "rt", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return []
    facts = data.get("facts", {})
    us = facts.get("us-gaap") or {}
    if not us:
        return []
    out = []
    for (cid, primaries, cand) in pairs:
        if cand not in us:
            continue
        cand_v = facts_of(us, cand)
        if not cand_v:
            continue
        prim_v = {}
        for t in primaries:            # 逐期取第一個有值的主標籤，模擬對照表行為
            for k, v in facts_of(us, t).items():
                prim_v.setdefault(k, v)
        both = [k for k in cand_v if k in prim_v]
        if not both:
            continue
        same = 0
        ratios = []
        for k in both:
            a, b = prim_v[k], cand_v[k]
            if b == 0 and a == 0:
                same += 1
                continue
            if b == 0:
                ratios.append(float("inf"))
                continue
            r = a / b
            ratios.append(r)
            if abs(r - 1) < AGREE:
                same += 1
        out.append((cid, cand, len(both), same, ratios[:200]))
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--concept", help="只驗這個 concept id")
    ap.add_argument("--min-overlap", type=int, default=MIN_OVERLAP)
    ap.add_argument("--json", help="結果寫成 JSON")
    ap.add_argument("--top", type=int, default=200)
    args = ap.parse_args()

    m = load_map()
    primaries = {c["id"]: list(c.get("tags", [])) for c in m["concepts"]}

    # 收集候選：coverage.jsonl 的 kin（同族標籤）
    cand_firms = defaultdict(set)
    with io.open(COVERAGE, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            for cid, tags in (d.get("kin") or {}).items():
                if args.concept and cid != args.concept:
                    continue
                for t in tags:
                    cand_firms[(cid, t)].add(d["ticker"])
    if not cand_firms:
        print("沒有候選")
        return
    print(f"候選 {len(cand_firms)} 組（concept × 標籤），逐格驗證中…\n")

    pairs = [(cid, primaries.get(cid, []), tag) for (cid, tag) in cand_firms]
    files = sorted(f for f in os.listdir(CACHE) if f.endswith(".json.gz"))
    jobs = [(os.path.join(CACHE, f), pairs) for f in files]

    agg = defaultdict(lambda: {"both": 0, "same": 0, "firms": 0, "ratios": []})
    with ProcessPoolExecutor() as ex:
        for rows in ex.map(compare, jobs, chunksize=8):
            for cid, cand, both, same, ratios in rows:
                a = agg[(cid, cand)]
                a["both"] += both
                a["same"] += same
                a["firms"] += 1
                if len(a["ratios"]) < 4000:
                    a["ratios"].extend(ratios)

    rows = []
    for (cid, cand), a in agg.items():
        rate = a["same"] / a["both"] if a["both"] else 0.0
        finite = [r for r in a["ratios"] if r not in (float("inf"), float("-inf"))]
        med = statistics.median(finite) if finite else None
        ok = (a["both"] >= args.min_overlap and a["firms"] >= MIN_FIRMS
              and rate >= ACCEPT_RATE)
        why = ""
        if not ok:
            if a["both"] < args.min_overlap or a["firms"] < MIN_FIRMS:
                why = "重疊太少，無法驗證"
            elif med is not None and abs(med - 1) > 0.05:
                why = f"系統性偏離（主/候選中位數比值 {med:.3g}）"
            else:
                why = "一致率不足"
        rows.append((ok, len(cand_firms[(cid, cand)]), cid, cand,
                     a["firms"], a["both"], rate, med, why))

    rows.sort(key=lambda r: (-r[0], -r[1]))
    accepted = [r for r in rows if r[0]]
    print("═" * 96)
    print(f"通過 {len(accepted)} 組 / 共 {len(rows)} 組")
    print("═" * 96)
    hdr = f"{'':2} {'受益家數':>6} {'concept':22} {'候選標籤':52} {'比對':>5} {'一致':>6} 判定"
    for ok, nfirm, cid, cand, firms, both, rate, med, why in rows[:args.top]:
        mark = "收" if ok else "退"
        print(f"{mark:2} {nfirm:>6} {cid:22} {cand[:52]:52} {both:>5} {rate*100:>5.1f}% "
              + ("" if ok else why))

    if args.json:
        with io.open(args.json, "w", encoding="utf-8") as f:
            json.dump([{
                "accept": r[0], "firms_missing": r[1], "concept": r[2], "tag": r[3],
                "compare_firms": r[4], "overlap": r[5], "agree_rate": r[6],
                "median_ratio": r[7], "reason": r[8],
            } for r in rows], f, ensure_ascii=False, indent=1)
        print(f"\n完整結果 → {args.json}")


if __name__ == "__main__":
    main()
