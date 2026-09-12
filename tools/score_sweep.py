#!/usr/bin/env python3
"""
評分覆蓋率掃描 —— 打**真的** /api/signals，量「有幾家拿不到總分、為什麼」。

和 api_sweep.py 的分工：
  api_sweep.py   財務報表那張表哪一格是 n/a（科目層）
  score_sweep.py 轉折點那一頁**拿不拿得到總分**（計分項層）

為什麼要獨立一支：覆蓋率門檻（35%）與構面門檻（40%）是評分會不會出現的開關，
科目層的 n/a 統計看不到這兩個開關。而且改評分模型（例如新增銀行／保險／REIT 的
另一套構面）之後，唯一能驗證「有沒有真的把那幾家救回來」的東西就是這支的前後對照。

用法：
  python tools/score_sweep.py --limit 150            # 羅素 1000 前 150 檔
  python tools/score_sweep.py --tickers JPM,BAC,PGR  # 指定幾檔
  python tools/score_sweep.py --sic 6000-6799        # 只掃這段 SIC（金融業）
  python tools/score_sweep.py --report               # 只重讀快取重新彙總（秒級）
  python tools/score_sweep.py --save before.json     # 存一份當基準，之後 --diff 比

前置：dev server 要開著（預設 http://localhost:3000）。
結果快取在 tools/sweep_out/score/，--refresh 才重抓。
"""
import argparse
import gzip
import io
import json
import os
import re
import sys
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
CACHE = os.path.join(OUT, "score")
BASE = os.environ.get("BAMHI_API", "http://localhost:3000")
UNIVERSE = os.environ.get(
    "BAMHI_UNIVERSE", os.path.join(os.path.expanduser("~"), "Downloads", "羅素1000.xlsx"))


def fetch(ticker: str, years: int, refresh: bool) -> dict | None:
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, f"{ticker.replace('/', '.')}.json.gz")
    if os.path.exists(path) and not refresh:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass
    url = f"{BASE}/api/signals?ticker={urllib.parse.quote(ticker)}&years={years}"
    try:
        with urllib.request.urlopen(url, timeout=300) as r:
            d = json.load(r)
    except urllib.error.HTTPError as e:
        return {"_error": f"HTTP {e.code}", "ticker": ticker}
    except Exception as e:  # 連不上 dev server 也要留下痕跡，不要靜靜少一家
        return {"_error": str(e)[:80], "ticker": ticker}
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(d, f)
    return d


def slim(d: dict) -> dict:
    """只留彙總要用的欄位。整份回應約 90KB，留著會讓報表跑不動。"""
    if d.get("_error"):
        return {"ticker": d.get("ticker"), "error": d["_error"]}
    s = d["score"]
    return {
        "ticker": d.get("ticker"),
        "company": d.get("company"),
        "sic": d.get("sic"),
        "model": s.get("model"),
        "modelZh": s.get("modelZh"),
        "total": s.get("total"),
        "grade": (s.get("grade") or {}).get("zh"),
        "coverage": s.get("coverage"),
        "counted": s.get("counted"),
        "itemsTotal": s.get("itemsTotal"),
        "peerAnchored": s.get("peerAnchored"),
        "arrow": (s.get("arrow") or {}).get("delta"),
        "dims": [
            {"id": x["id"], "score": x["score"], "coverage": x["coverage"], "counted": x["counted"]}
            for x in s["dimensions"]
        ],
        "items": [
            {"metric": it["metric"], "reason": it["reason"], "dim": x["id"],
             "anchor": it.get("anchorSource")}
            for x in s["dimensions"] for it in x["items"]
        ],
    }


def report(rows: list[dict]) -> None:
    ok = [r for r in rows if not r.get("error")]
    err = [r for r in rows if r.get("error")]
    scored = [r for r in ok if r["total"] is not None]
    unscored = [r for r in ok if r["total"] is None]

    print(f"\n{'='*72}")
    print(f"{len(ok)} 家取得回應（{len(err)} 家失敗）；"
          f"{len(scored)} 家給得出總分、{len(unscored)} 家覆蓋率不足")
    if err:
        print("  失敗：" + ", ".join(f"{r['ticker']}({r['error']})" for r in err[:10]))

    by_model = Counter(r.get("model") or "general" for r in ok)
    print("  模型分布：" + "、".join(f"{k} {v} 家" for k, v in by_model.most_common()))

    if scored:
        tots = sorted(r["total"] for r in scored)
        covs = sorted(r["coverage"] for r in scored)
        def q(a, p): return a[min(len(a) - 1, int(len(a) * p))]
        print(f"  總分 p10/中位/p90 = {q(tots,.1)}/{q(tots,.5)}/{q(tots,.9)}；"
              f"覆蓋率中位 {q(covs,.5):.1%}")
        print("  評級：" + "、".join(f"{k} {v}" for k, v in
                                   Counter(r["grade"] for r in scored).most_common()))

    # 計分項的留白理由：missing 才是我們的錯（該申報卻抓不到）
    reasons = Counter()
    per_metric = defaultdict(Counter)
    for r in ok:
        for it in r["items"]:
            reasons[it["reason"]] += 1
            per_metric[it["metric"]][it["reason"]] += 1
    tot = sum(reasons.values()) or 1
    print("\n計分項留白理由（" + str(tot) + " 格）")
    for k, v in reasons.most_common():
        print(f"  {k:15} {v:6}  {v/tot:6.1%}")

    print("\nmissing 最多的計分項（該申報卻抓不到＝我們漏標籤）")
    worst = sorted(per_metric.items(), key=lambda kv: -kv[1]["missing"])[:12]
    for m, c in worst:
        if c["missing"]:
            print(f"  {m:34} missing {c['missing']:4}  ok {c['ok']:4}  "
                  f"inapplicable {c['inapplicable']:4}")

    if unscored:
        print(f"\n拿不到總分的 {len(unscored)} 家")
        for r in sorted(unscored, key=lambda x: x["coverage"]):
            dims = " ".join(f"{d['id'][:4]}={d['coverage']:.0%}" for d in r["dims"])
            print(f"  {r['ticker']:6} sic={r.get('sic') or '----':5} "
                  f"cov={r['coverage']:5.1%} {r['counted']:2}/{r['itemsTotal']}  {dims}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=UNIVERSE)
    ap.add_argument("--tickers", help="逗號分隔，給了就不讀 universe")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--years", type=int, default=5)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--refresh", action="store_true", help="忽略快取重抓")
    ap.add_argument("--report", action="store_true", help="只讀快取重新彙總")
    ap.add_argument("--save", help="把彙總前的逐家結果存成 JSON（當前後對照的基準）")
    ap.add_argument("--diff", help="與這份基準比對，只印分數與覆蓋率有變的公司")
    args = ap.parse_args()

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        tickers = [u["ticker"] for u in load_universe(args.universe)]
    if args.limit:
        tickers = tickers[:args.limit]

    rows: list[dict] = []
    if args.report:
        for t in tickers:
            p = os.path.join(CACHE, f"{t.replace('/', '.')}.json.gz")
            if not os.path.exists(p):
                continue
            with gzip.open(p, "rt", encoding="utf-8") as f:
                rows.append(slim(json.load(f)))
    else:
        done = 0
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            for d in ex.map(lambda t: fetch(t, args.years, args.refresh), tickers):
                done += 1
                if d:
                    rows.append(slim(d))
                if done % 25 == 0:
                    print(f"  ... {done}/{len(tickers)}")

    report(rows)

    if args.save:
        json.dump(rows, open(args.save, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n逐家結果已存：{args.save}")

    if args.diff:
        base = {r["ticker"]: r for r in json.load(open(args.diff, encoding="utf-8"))}
        print(f"\n與 {os.path.basename(args.diff)} 的差異")
        n = 0
        for r in rows:
            b = base.get(r["ticker"])
            if not b or r.get("error") or b.get("error"):
                continue
            if r["total"] != b["total"] or abs((r["coverage"] or 0) - (b["coverage"] or 0)) > 1e-9:
                n += 1
                print(f"  {r['ticker']:6} 總分 {b['total']} → {r['total']}   "
                      f"覆蓋率 {b['coverage']:.1%} → {r['coverage']:.1%}   "
                      f"模型 {b.get('model') or 'general'} → {r.get('model') or 'general'}")
        if not n:
            print("  （沒有任何一家的總分或覆蓋率改變）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
