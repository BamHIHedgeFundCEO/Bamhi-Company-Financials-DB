#!/usr/bin/env python3
"""
關鍵指標的全市場逐家排查（零 SEC 請求，打本機 dev server）。

存在的理由：對齊富途的過程中，「公式改對了、但某些公司從有值變成沒值」這一類錯
**連踩三次**（速動比率打掉 59 家、EBITDA 打掉沒有利息費用標籤的公司、ROIC 打掉蘋果），
而前兩次都是等覆蓋率統計事後才發現的。覆蓋率統計看的是 26 個計分項，
整本活頁簿有 175 個指標 —— 其餘 149 個沒有任何地方在看。

和既有兩支掃描的分工：
  api_sweep.py    三大報表那張表哪一格是 n/a（科目層）
  score_sweep.py  轉折點那一頁拿不拿得到總分（計分項層）
  metric_audit.py 每一個指標、每一家公司、每一期的**值本身**合不合理

**不另寫求值器**：迷你公式語言已經有三份實作（metrics.ts／formulas.py／peer_stats.py），
稽核再寫第四份，等於拿一個可能不一致的東西去查正確性。這支讀的是
`/api/signals?audit=1`，也就是網頁與評分用的**同一份值**。

四類檢查：
  ① 恆等式 —— 兩個指標之間必然成立的關係（財務槓桿 × 股東權益比率 ＝ 1、
     應收週轉率 × 應收天數 ＝ 365…）。這類不需要任何外部資料就抓得到定義寫錯
  ② 範圍 —— 毛利率 > 100%、週轉天數為負這種不可能的值
  ③ 大小關係 —— 速動比率 ≤ 流動比率、EBIT 利潤率 ≤ EBITDA 利潤率
  ④ 前後對照（--baseline）—— 列出所有「原本有值、現在沒值」的格子。
     上面那三次踩的坑，這一類會當場抓到

用法：
  python tools/metric_audit.py --limit 150                        # 先跑 150 家
  python tools/metric_audit.py --limit 150 --save base.json       # 存基準
  python tools/metric_audit.py --limit 150 --baseline base.json   # 改完之後比
  python tools/metric_audit.py --tickers AAPL,NVDA,AXON --verbose
"""
import argparse
import gzip
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from sweep import load_universe  # noqa: E402

CACHE = os.path.join(ROOT, "tools", "sweep_out", "audit")
BASE = os.environ.get("BAMHI_API", "http://localhost:3000")
UNIVERSE = os.environ.get(
    "BAMHI_UNIVERSE", os.path.join(os.path.expanduser("~"), "Downloads", "羅素1000.xlsx"))


def mul(a, b):
    return None if a is None or b is None else a * b


def neg(a):
    return None if a is None else -a


def add(*xs):
    return None if any(x is None for x in xs) else sum(xs)


# ── ① 恆等式 ────────────────────────────────────────────────────────────────
# (說明, 左式, 右式, 相對容差)。兩邊都算得出來才比；任一邊空白就跳過
IDENTITIES = [
    ("財務槓桿 x 股東權益比率 = 1",
     lambda v: mul(v("financial_leverage"), v("equity_ratio")), lambda v: 1.0, 1e-6),
    ("應收週轉率 x 應收天數 = 365",
     lambda v: mul(v("ar_turnover_ttm"), v("dso_ttm")), lambda v: 365.0, 1e-6),
    ("存貨週轉率 x 存貨天數 = 365",
     lambda v: mul(v("inventory_turnover_ttm"), v("dio_ttm")), lambda v: 365.0, 1e-6),
    ("應付週轉率 x 應付天數 = 365",
     lambda v: mul(v("ap_turnover_ttm"), v("dpo_ttm")), lambda v: 365.0, 1e-6),
    ("現金轉換循環 = 應收 + 存貨 - 應付",
     lambda v: add(v("dso_ttm"), v("dio_ttm"), neg(v("dpo_ttm"))),
     lambda v: v("ccc_ttm"), 1e-6),
    ("EBITDA = EBIT + 折舊攤銷",
     lambda v: add(v("ebit_ttm"), v("dna_ttm")), lambda v: v("ebitda_ttm"), 1e-6),
    ("EBIT 利潤率 x 營收 = EBIT",
     lambda v: mul(v("ebit_margin_ttm"), v("revenue_ttm")), lambda v: v("ebit_ttm"), 1e-6),
    ("財務費用 = EBIT - 稅前淨利",
     lambda v: add(v("ebit_ttm"), neg(v("pretax_income_ttm"))),
     lambda v: v("finance_cost_ttm"), 1e-6),
    ("毛利率 x 營收 = 毛利",
     lambda v: mul(v("gross_margin_ttm"), v("revenue_ttm")),
     lambda v: v("gross_profit_ttm"), 1e-6),
]

# (說明, 左, 右) 左必須 <= 右
ORDERINGS = [
    ("速動比率 <= 流動比率", "quick_ratio", "current_ratio"),
    ("EBIT 利潤率 <= EBITDA 利潤率（折舊攤銷不為負）", "ebit_margin_ttm", "ebitda_margin_ttm"),
]

# ── ② 範圍 ──────────────────────────────────────────────────────────────────
RANGES = {
    "gross_margin_ttm": (-5.0, 1.0001), "gross_margin": (-5.0, 1.0001),
    "dso_ttm": (0.0, 3000.0), "dio_ttm": (0.0, 5000.0), "dpo_ttm": (0.0, 3000.0),
    "equity_ratio": (-5.0, 1.0001),
    "ar_turnover_ttm": (0.0, 1e4), "inventory_turnover_ttm": (0.0, 1e4),
    "ap_turnover_ttm": (0.0, 1e4), "fixed_asset_turnover_ttm": (0.0, 1e5),
    "current_ratio": (0.0, 1e4), "quick_ratio": (0.0, 1e4),
}


def fetch(ticker: str, refresh: bool) -> dict | None:
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, ticker.replace("/", ".") + ".json.gz")
    if os.path.exists(path) and not refresh:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    url = BASE + "/api/signals?ticker=" + urllib.parse.quote(ticker) + "&audit=1"
    try:
        with urllib.request.urlopen(url, timeout=300) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return {"_error": "HTTP %d" % e.code, "ticker": ticker}
    except Exception as e:  # 連不上 dev server 也要留下痕跡，不要靜靜少一家
        return {"_error": str(e)[:80], "ticker": ticker}
    slim = {
        "ticker": d.get("ticker"),
        "periods": d.get("periods") or [],
        "periodicity": d.get("periodicity"),
        "metrics": {
            m["id"]: {"zh": m.get("zh"), "inapplicable": m.get("inapplicable"),
                      "cells": m.get("cells") or []}
            for m in (d.get("allMetrics") or [])
        },
    }
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(slim, f)
    return slim


def check(d: dict) -> list:
    """回傳 [(類別, 期別, 說明)]"""
    out = []
    ps = d.get("periods") or []
    M = d.get("metrics") or {}
    if not ps or not M:
        return out

    def val_at(i):
        def v(mid):
            c = (M.get(mid) or {}).get("cells") or []
            if not 0 <= i < len(c):
                return None
            cell = c[i]
            return cell.get("value") if cell.get("reason") == "ok" else None
        return v

    for i, p in enumerate(ps):
        v = val_at(i)
        for name, lhs, rhs, tol in IDENTITIES:
            a, b = lhs(v), rhs(v)
            if a is None or b is None:
                continue
            scale = max(abs(a), abs(b), 1e-9)
            if abs(a - b) / scale > tol:
                out.append(("恆等式", p, "%s：左 %.6g vs 右 %.6g" % (name, a, b)))
        for name, lo_id, hi_id in ORDERINGS:
            a, b = v(lo_id), v(hi_id)
            if a is None or b is None:
                continue
            if a > b * (1 + 1e-9) + 1e-12:
                out.append(("大小關係", p, "%s：%s %.6g > %s %.6g" % (name, lo_id, a, hi_id, b)))
        for mid, (lo, hi) in RANGES.items():
            x = v(mid)
            if x is None:
                continue
            if x < lo or x > hi:
                out.append(("超出範圍", p, "%s = %.6g（合理區間 %g ~ %g）" % (mid, x, lo, hi)))
    return out


def regressions(cur: dict, base: dict) -> list:
    """原本有值、現在沒值 —— 對齊外部定義時最常踩的那一類"""
    out = []
    ps = cur.get("periods") or []
    bps = base.get("periods") or []
    common = [p for p in ps if p in set(bps)]
    ci = {p: i for i, p in enumerate(ps)}
    bi = {p: i for i, p in enumerate(bps)}
    for mid, bm in (base.get("metrics") or {}).items():
        cm = (cur.get("metrics") or {}).get(mid)
        if cm is None:
            out.append(("指標消失", "-", "%s（%s）整條不見了" % (mid, bm.get("zh"))))
            continue
        bcells = bm.get("cells") or []
        ccells = cm.get("cells") or []
        for p in common:
            bc = bcells[bi[p]] if bi[p] < len(bcells) else None
            cc = ccells[ci[p]] if ci[p] < len(ccells) else None
            if not bc or not cc:
                continue
            if bc.get("reason") == "ok" and cc.get("reason") != "ok":
                out.append(("值變空", p, "%s：%.6g -> 空（%s）"
                            % (mid, bc.get("value"), cc.get("reason"))))
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=UNIVERSE)
    ap.add_argument("--tickers")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--save", help="把這次的逐家結果存成基準")
    ap.add_argument("--baseline", help="與這份基準比對，列出值變空的格子")
    ap.add_argument("--verbose", action="store_true", help="逐筆列出，不只摘要")
    a = ap.parse_args()

    # load_universe 回傳的是 dict（ticker/sector/industry），不是字串
    tickers = ([t.strip().upper() for t in a.tickers.split(",") if t.strip()]
               if a.tickers else [u["ticker"] for u in load_universe(a.universe)])
    if a.limit:
        tickers = tickers[:a.limit]
    print("排查 %d 家（%s）" % (len(tickers), BASE))

    data = {}
    with ThreadPoolExecutor(max_workers=a.workers) as ex:
        results = ex.map(lambda t: fetch(t, a.refresh), tickers)
        for i, (t, d) in enumerate(zip(tickers, results), 1):
            if d:
                data[t] = d
            if i % 25 == 0:
                print("  ... %d/%d" % (i, len(tickers)))

    base = None
    if a.baseline:
        with open(a.baseline, encoding="utf-8") as f:
            base = json.load(f)

    kinds = Counter()
    by_item = Counter()
    firms = defaultdict(list)
    errs = 0
    for t, d in data.items():
        if d.get("_error"):
            errs += 1
            continue
        found = check(d)
        if base and t in base:
            found += regressions(d, base[t])
        for kind, p, msg in found:
            kinds[kind] += 1
            by_item[msg.split("：")[0]] += 1
            firms[t].append((kind, p, msg))

    print("\n" + "=" * 72)
    print("%d 家排查完成（%d 家取不到）；%d 個問題、%d 家有問題"
          % (len(data) - errs, errs, sum(kinds.values()), len(firms)))
    for k, c in kinds.most_common():
        print("  %-10s%6d" % (k, c))
    if by_item:
        print("\n問題最多的項目")
        for k, c in by_item.most_common(15):
            print("  %5d  %s" % (c, k))
    if firms:
        print("\n有問題的公司" + ("" if a.verbose else "（前 20 家，--verbose 看全部）"))
        for t in (list(firms) if a.verbose else list(firms)[:20]):
            print("  " + t)
            for kind, p, msg in (firms[t] if a.verbose else firms[t][:3]):
                print("      [%s] %s  %s" % (kind, p, msg))
    else:
        print("\n沒有發現任何恆等式、範圍或回歸問題")

    if a.save:
        with open(a.save, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        print("\n基準已存：" + a.save)
    return 1 if firms else 0


if __name__ == "__main__":
    sys.exit(main())
