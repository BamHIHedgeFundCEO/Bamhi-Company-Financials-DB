#!/usr/bin/env python3
"""
分部數據抽樣掃描 —— 打真 API，量「使用者實際看到的分部表哪裡有問題」。

和 tools/segment.py 的分工：
  segment.py       只看 SEC instance 裡有什麼軸/成員，是迭代 config 的顯微鏡（單檔）
  segment_sweep.py 打 /api/segments，看整條管線（抽取→正規化→上層判定→對帳）
                   跑出來的表，哪些格子對不上總額、哪些公司整個沒有分部

為什麼只抽樣不掃全庫：分部要解析申報 instance，單檔 0.7–14MB、每家 12 次請求。
1000 家 ≈ 18GB 下載，沒必要 —— 分部的問題是「揭露型態」的問題，同型態的公司會一起
中招，抽樣（依產業分層）就看得到。

校驗三態（見 segments.ts 的 SegmentCell.verified）：
  true  分部加總 = 合併總額
  false 對不上 —— 值得懷疑
  null  **無法校驗**，不是校驗失敗（ASC 280 允許公司只揭露部分分部的費用）
本工具把 false 與 null 分開統計。混在一起看會得到「未校驗率 50%」這種嚇人但無意義的數字。

用法：
  python tools/segment_sweep.py --limit 100
  python tools/segment_sweep.py --tickers AAPL,INTC,KO --refresh
  python tools/segment_sweep.py --report            # 只從快取重印報告，不打 API
"""
import argparse
import gzip
import io
import json
import os
import random
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from sweep import load_universe  # noqa: E402

OUT = os.path.join(ROOT, "tools", "sweep_out")
SEG_CACHE = os.path.join(OUT, "seg")
BASE = os.environ.get("BAMHI_API", "http://[::1]:3001")
UNIVERSE = os.environ.get(
    "BAMHI_UNIVERSE", os.path.join(os.path.expanduser("~"), "Downloads", "羅素1000.xlsx"))


def fetch(ticker: str, refresh: bool) -> dict:
    os.makedirs(SEG_CACHE, exist_ok=True)
    path = os.path.join(SEG_CACHE, f"{ticker.replace('/', '.')}.json.gz")
    if os.path.exists(path) and not refresh:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass

    url = f"{BASE}/api/segments?ticker={urllib.parse.quote(ticker)}"
    data = None
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=600) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = json.load(e).get("message", "")
            except Exception:
                pass
            # 404 = 這家沒有可解析的年報 / ticker 查無，是結論不是失敗，直接落地
            if e.code == 404:
                data = {"_error": "no_data", "_msg": body[:120]}
                break
            if attempt == 2:
                data = {"_error": f"http_{e.code}", "_msg": body[:120]}
            else:
                time.sleep(3 * (attempt + 1))
        except Exception as e:
            if attempt == 2:
                data = {"_error": "exc", "_msg": str(e)[:120]}
            else:
                time.sleep(3 * (attempt + 1))
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


def summarize(t: str, sector: str, d: dict) -> dict:
    """把一家公司的分部回應壓成一列統計。"""
    row = {"ticker": t, "sector": sector, "error": d.get("_error"),
           "msg": d.get("_msg", ""), "axes": 0, "periods": 0, "members": 0,
           "cells": 0, "ok": 0, "bad": 0, "unver": 0, "parents": 0,
           "warnings": d.get("warnings", []) or [],
           "by_concept": defaultdict(lambda: [0, 0, 0]),   # concept → [ok, bad, unver]
           "axis_names": [], "bad_detail": []}
    if row["error"]:
        return row
    row["periods"] = len(d.get("periods", []))
    for ax in d.get("axes", []):
        row["axes"] += 1
        row["axis_names"].append(ax.get("axis", "?"))
        for mem in ax.get("members", []):
            row["members"] += 1
            for _p, concepts in (mem.get("values") or {}).items():
                for cid, cell in concepts.items():
                    row["cells"] += 1
                    v = cell.get("verified")
                    if cell.get("isParent"):
                        row["parents"] += 1
                    slot = 0 if v is True else (1 if v is False else 2)
                    row["by_concept"][cid][slot] += 1
                    if v is True:
                        row["ok"] += 1
                    elif v is False:
                        row["bad"] += 1
                        row["bad_detail"].append((ax.get("axis", "?"), cid, _p))
                    else:
                        row["unver"] += 1
    return row


def pct(a: int, b: int) -> str:
    return f"{100.0 * a / b:5.1f}%" if b else "    –"


def report(rows: list[dict]) -> None:
    ok_rows = [r for r in rows if not r["error"]]
    err_rows = [r for r in rows if r["error"]]

    print("=" * 78)
    print(f"分部抽樣：{len(rows)} 家（成功 {len(ok_rows)} / 無資料或失敗 {len(err_rows)}）")
    print("=" * 78)

    # ── 段 1：拿不到分部的公司 ──────────────────────────────────────────
    print("\n【段 1】拿不到分部表的公司")
    if not err_rows:
        print("  （無）")
    for r in sorted(err_rows, key=lambda x: x["ticker"]):
        print(f"  {r['ticker']:<8} {r['error']:<10} {r['msg'][:60]}")

    empty = [r for r in ok_rows if r["axes"] == 0]
    print(f"\n  回應成功但一個軸都沒有：{len(empty)} 家"
          + ("　" + ", ".join(r["ticker"] for r in empty) if empty else ""))

    # ── 段 2：對不上總額（verified=false）排行 ──────────────────────────
    print("\n【段 2】對不上合併總額的格子（verified=false）—— 真異常")
    tot_cells = sum(r["cells"] for r in ok_rows)
    tot_bad = sum(r["bad"] for r in ok_rows)
    tot_unver = sum(r["unver"] for r in ok_rows)
    tot_ok = sum(r["ok"] for r in ok_rows)
    print(f"  全體 {tot_cells:,} 格：對得上 {tot_ok:,}（{pct(tot_ok, tot_cells)}）"
          f"／對不上 {tot_bad:,}（{pct(tot_bad, tot_cells)}）"
          f"／無法校驗 {tot_unver:,}（{pct(tot_unver, tot_cells)}）")
    ranked = sorted((r for r in ok_rows if r["bad"]), key=lambda x: -x["bad"] / max(1, x["cells"]))
    print(f"\n  {'ticker':<8}{'產業':<26}{'格數':>7}{'對不上':>8}{'比例':>8}")
    for r in ranked[:25]:
        print(f"  {r['ticker']:<8}{str(r['sector'])[:24]:<26}{r['cells']:>7}"
              f"{r['bad']:>8}{pct(r['bad'], r['cells']):>8}")
    if not ranked:
        print("  （無）")

    # ── 段 3：依科目看 false / null ─────────────────────────────────────
    print("\n【段 3】依科目：對不上 vs 無法校驗")
    agg = defaultdict(lambda: [0, 0, 0])
    for r in ok_rows:
        for cid, (a, b, c) in r["by_concept"].items():
            agg[cid][0] += a
            agg[cid][1] += b
            agg[cid][2] += c
    print(f"  {'科目':<22}{'格數':>8}{'對得上':>9}{'對不上':>9}{'無法校驗':>10}")
    for cid, (a, b, c) in sorted(agg.items(), key=lambda kv: -sum(kv[1])):
        n = a + b + c
        print(f"  {cid:<22}{n:>8}{pct(a, n):>9}{pct(b, n):>9}{pct(c, n):>10}")

    # ── 段 4：對不上的科目集中在哪 ──────────────────────────────────────
    print("\n【段 4】對不上的格子：ticker × 科目（前 30）")
    pair = Counter()
    for r in ok_rows:
        for _ax, cid, _p in r["bad_detail"]:
            pair[(r["ticker"], cid)] += 1
    for (t, cid), n in pair.most_common(30):
        print(f"  {t:<8}{cid:<22}{n:>4} 格")
    if not pair:
        print("  （無）")

    # ── 段 5：warnings ─────────────────────────────────────────────────
    print("\n【段 5】API 回報的 warnings")
    wc = Counter()
    for r in ok_rows:
        for w in r["warnings"]:
            # 把 ticker/數字抽掉才聚得起來
            key = "".join("#" if ch.isdigit() else ch for ch in w)
            wc[key] += 1
    for w, n in wc.most_common(20):
        print(f"  {n:>4}×  {w[:100]}")
    if not wc:
        print("  （無）")

    # ── 段 6：規模健檢 ─────────────────────────────────────────────────
    print("\n【段 6】表的規模（成員數異常多 = 軸沒正規化好，或該公司真的分很細）")
    big = sorted(ok_rows, key=lambda r: -r["members"])[:12]
    print(f"  {'ticker':<8}{'軸':>4}{'成員':>6}{'期數':>6}{'格數':>8}{'上層':>7}")
    for r in big:
        print(f"  {r['ticker']:<8}{r['axes']:>4}{r['members']:>6}{r['periods']:>6}"
              f"{r['cells']:>8}{r['parents']:>7}")

    thin = [r for r in ok_rows if r["axes"] and r["members"] <= 2]
    print(f"\n  只有 1–2 個成員（可能揭露不完整或抽取漏掉）：{len(thin)} 家"
          + ("　" + ", ".join(r["ticker"] for r in thin) if thin else ""))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=UNIVERSE)
    ap.add_argument("--limit", type=int, default=100)
    ap.add_argument("--tickers", help="逗號分隔，蓋過 universe")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--report", action="store_true", help="只讀快取重印報告")
    ap.add_argument("--workers", type=int, default=2)
    ap.add_argument("--seed", type=int, default=20260818)
    args = ap.parse_args()

    if args.tickers:
        universe = [{"ticker": t.strip().upper(), "sector": ""}
                    for t in args.tickers.split(",") if t.strip()]
    else:
        uni = load_universe(args.universe)
        # 依產業分層抽樣：分部的問題跟揭露型態綁在一起，同產業會一起中招，
        # 照市值前 N 名抽會整片漏掉公用事業與 REIT
        by_sec = defaultdict(list)
        for u in uni:
            by_sec[str(u.get("sector") or "?")].append(u)
        rnd = random.Random(args.seed)
        for v in by_sec.values():
            rnd.shuffle(v)
        universe, i = [], 0
        while len(universe) < min(args.limit, len(uni)):
            took = False
            for s in sorted(by_sec):
                if i < len(by_sec[s]) and len(universe) < args.limit:
                    universe.append(by_sec[s][i])
                    took = True
            if not took:
                break
            i += 1

    sec_of = {u["ticker"]: u.get("sector", "") for u in universe}
    tickers = [u["ticker"] for u in universe]

    if args.report:
        rows = []
        for t in tickers:
            p = os.path.join(SEG_CACHE, f"{t.replace('/', '.')}.json.gz")
            if not os.path.exists(p):
                continue
            with gzip.open(p, "rt", encoding="utf-8") as f:
                rows.append(summarize(t, sec_of[t], json.load(f)))
        report(rows)
        return

    t0 = time.time()
    done = [0]

    def work(t: str) -> dict:
        d = fetch(t, args.refresh)
        done[0] += 1
        if done[0] % 5 == 0:
            print(f"  … {done[0]}/{len(tickers)}  {time.time() - t0:.0f}s", flush=True)
        return summarize(t, sec_of[t], d)

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        rows = list(ex.map(work, tickers))
    print(f"抓取完成 {time.time() - t0:.0f}s\n")
    report(rows)


if __name__ == "__main__":
    main()
