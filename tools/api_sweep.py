#!/usr/bin/env python3
"""
期間層級 n/a 掃描 —— 打**真的** /api/financials，不重寫管線。

和 sweep.py 的分工：
  sweep.py      只問「config 有沒有標籤」。答案是每家公司每個科目一個 yes/no
  api_sweep.py  問「使用者實際看到的那張表，哪一格是 n/a」。同一個科目可能
                2022 年有值、2025 年變 n/a（公司改標籤、或最新一季還沒申報）

差別很重要：sweep.py 說 AAPL「研發費用有」，但真表上可能只有 15/19 期有值。
那 4 個洞 sweep.py 永遠看不到，關鍵指標分頁卻會整欄跟著 n/a。

用法：
  python tools/api_sweep.py --limit 30              # 先試 30 檔
  python tools/api_sweep.py                         # 全掃（快取在 tools/sweep_out/api/）
  python tools/api_sweep.py --report                # 只重讀快取重新彙總（秒級）
  python tools/api_sweep.py --valuation --limit 150 # 連估值倍數一起看（會打 Yahoo）

前置：dev server 要開著（預設 http://[::1]:3000，Nitro 只綁 IPv6）。
"""
import argparse
import gzip
import io
import json
import os
import re
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

MAP_PATH = os.path.join(ROOT, "config", "xbrl_zh_map.json")
OUT = os.path.join(ROOT, "tools", "sweep_out")
API_CACHE = os.path.join(OUT, "api")
BASE = os.environ.get("BAMHI_API", "http://[::1]:3000")
UNIVERSE = os.environ.get(
    "BAMHI_UNIVERSE", os.path.join(os.path.expanduser("~"), "Downloads", "羅素1000.xlsx"))


def fetch_api(ticker: str, valuation: bool, refresh: bool) -> dict | None:
    """打真 API，結果落地。加 valuation 的存不同檔名，避免兩種模式互相污染。"""
    os.makedirs(API_CACHE, exist_ok=True)
    tag = "v" if valuation else "n"
    path = os.path.join(API_CACHE, f"{ticker.replace('/', '.')}.{tag}.json.gz")
    if os.path.exists(path) and not refresh:
        try:
            with gzip.open(path, "rt", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            pass  # 快取壞了就重抓

    url = (f"{BASE}/api/financials?ticker={urllib.parse.quote(ticker)}&years=5"
           + ("" if valuation else "&valuation=0"))
    for attempt in range(3):
        try:
            with urllib.request.urlopen(url, timeout=180) as r:
                data = json.load(r)
            break
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"_error": f"404 {ticker}"}
            if attempt == 2:
                return {"_error": f"HTTP {e.code}"}
            time.sleep(2 * (attempt + 1))
        except Exception as e:
            if attempt == 2:
                return {"_error": str(e)[:80]}
            time.sleep(2 * (attempt + 1))
    with gzip.open(path, "wt", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    return data


# ---------------------------------------------------------------- 指標可得性

DERIVED_IDS: set[str] = set()


def parse_deps(formula: str) -> list[tuple[str, int, bool]]:
    """
    把公式拆成 (科目 id, 期數偏移, 是否需要期初期末平均)。

    `avg(equity)` 需要 t 與 t-1 兩期（期初期末平均），少一期整格就是 n/a ——
    這是第一期永遠算不出 ROE 的原因，不是資料缺。
    """
    deps = []
    for m in re.finditer(r"avg\(\s*([a-z_][a-z0-9_]*)\s*\)", formula):
        deps.append((m.group(1), 0, True))
    rest = re.sub(r"avg\(\s*[a-z_][a-z0-9_]*\s*\)", " ", formula)
    for m in re.finditer(r"([a-z_][a-z0-9_]*)\s*(?:\[\s*t\s*(-\s*\d+)?\s*\])?", rest):
        name = m.group(1)
        if not name or name in ("avg", "t"):
            continue
        off = int(m.group(2).replace(" ", "")) if m.group(2) else 0
        deps.append((name, off, False))
    return deps


def metric_ok(mid: str, pi: int, avail: dict[str, list[bool]], fml: dict[str, str],
              n: int, seen: frozenset = frozenset()) -> bool:
    """該指標在第 pi 期算不算得出來。指標可以引用指標（fcf → cfo−capex）故遞迴。"""
    if mid in seen:
        return False
    if mid in avail:
        return 0 <= pi < n and avail[mid][pi]
    if mid not in fml:
        return True  # 常數（91.25、4）落不到這裡，保險用
    for dep, off, need_avg in parse_deps(fml[mid]):
        if dep not in avail and dep not in fml:
            continue  # 數字或已被 re 濾掉的雜訊
        idxs = [pi + off] + ([pi - 1] if need_avg else [])
        for i in idxs:
            if not metric_ok(dep, i, avail, fml, n, seen | {mid}):
                return False
    return True


# ---------------------------------------------------------------- 逐檔分析

def analyse(row: dict, fin: dict, concepts: list[dict], derived: list[dict]) -> dict:
    periods = fin["periods"]
    n = len(periods)
    li = {x["id"]: x for x in fin["lineItems"]}
    avail = {
        c["id"]: [li.get(c["id"], {}).get("values", {}).get(p, {}).get("value") is not None
                  for p in periods]
        for c in concepts
    }
    # 估計值分四種，混在一起看不出問題：Q4 推算與恆等式推算是合理的，
    # 「缺申報視為 0」是把沒揭露當成零，「沿用前期」則會隨拖延期數變得越來越假。
    est = defaultdict(lambda: defaultdict(int))
    stale = {}  # 沿用前期最多拖了幾期
    for c in concepts:
        vals = li.get(c["id"], {}).get("values", {})
        last_real = None
        worst = 0
        for i, p in enumerate(periods):
            cell = vals.get(p)
            if not cell or cell.get("value") is None:
                continue
            tag = cell.get("sourceTag") or ""
            if not cell.get("isEstimated"):
                last_real = i
                continue
            kind = ("zero" if "視為 0" in tag else "carry" if "沿用前期" in tag
                    else "approx" if "近似" in tag else "derive" if "推算" in tag else "q4")
            est[c["id"]][kind] += 1
            if kind == "carry":
                if last_real is not None:
                    worst = max(worst, i - last_real)
            else:
                last_real = i
        if worst:
            stale[c["id"]] = worst

    # 科目層級：全缺 / 部分缺（破洞）。破洞才是這一階段要找的東西
    full_miss, holes = [], {}
    for cid, flags in avail.items():
        got = sum(flags)
        if got == 0:
            full_miss.append(cid)
        elif got < n:
            holes[cid] = {
                "missing": n - got,
                # 洞的形狀：只缺最新幾期 = 還沒申報；只缺最舊幾期 = 五年前沒揭露；
                # 中間有洞 = 公司換標籤，這種才是 config 補得動的
                "shape": hole_shape(flags),
            }

    fml = {d["id"]: d["formula"] for d in derived}
    mna = {}
    for d in derived:
        bad = [p for i, p in enumerate(periods) if not metric_ok(d["id"], i, avail, fml, n)]
        if bad:
            mna[d["id"]] = len(bad)

    out = {
        "ticker": row["ticker"], "sector": row.get("sector", ""),
        "industry": row.get("industry", ""), "company": fin.get("company", ""),
        "periods": n, "full_miss": sorted(full_miss), "holes": holes,
        "metric_na": mna, "estimated": {k: dict(v) for k, v in est.items() if v},
        "stale": stale,
    }
    val = fin.get("valuation")
    if val:
        out["valuation_na"] = {
            r["id"]: sum(1 for p in periods if r["values"].get(p) is None)
            for r in val["rows"]
        }
        out["price_missing"] = out["valuation_na"].get("price", 0)
    return out


def hole_shape(flags: list[bool]) -> str:
    n = len(flags)
    first = flags.index(True)
    last = n - 1 - flags[::-1].index(True)
    inner = flags[first:last + 1]
    if all(inner):
        if first > 0 and last == n - 1:
            return "只缺最舊"
        if last < n - 1 and first == 0:
            return "只缺最新"
        return "頭尾都缺"
    return "中間有洞"


# ---------------------------------------------------------------- 彙總

def report(rows: list[dict], concepts: list[dict], derived: list[dict], valuation: bool):
    zh = {c["id"]: c["zh"] for c in concepts}
    mzh = {d["id"]: d["zh"] for d in derived}
    n = len(rows)
    print(f"\n{'='*72}\n真 API 掃描 {n} 家\n{'='*72}")

    tot_cells = sum(r["periods"] * len(concepts) for r in rows)
    na_cells = sum(r["periods"] * len(r["full_miss"])
                   + sum(h["missing"] for h in r["holes"].values()) for r in rows)
    hole_cells = sum(sum(h["missing"] for h in r["holes"].values()) for r in rows)
    print(f"三大報表格子總數 {tot_cells:,}，n/a {na_cells:,}（{na_cells/tot_cells:.1%}）")
    print(f"  其中整列缺 {na_cells-hole_cells:,} 格、**部分期破洞 {hole_cells:,} 格**")

    print(f"\n【段 1】部分期破洞排行（整列缺是階段 1 的事，這裡只看破洞）")
    cnt = defaultdict(int)
    cells = defaultdict(int)
    shapes = defaultdict(Counter)
    for r in rows:
        for cid, h in r["holes"].items():
            cnt[cid] += 1
            cells[cid] += h["missing"]
            shapes[cid][h["shape"]] += 1
    for cid, c in sorted(cnt.items(), key=lambda x: -x[1])[:20]:
        sh = "、".join(f"{k}{v}" for k, v in shapes[cid].most_common())
        print(f"  {cid:24s} {zh.get(cid,''):14s} {c:4d} 家 / {cells[cid]:5d} 格   {sh}")

    print(f"\n【段 2】關鍵指標 n/a（36 個指標，分母是 {n} 家 × 各自期數）")
    mcnt = defaultdict(int)
    mcell = defaultdict(int)
    for r in rows:
        for mid, c in r["metric_na"].items():
            mcnt[mid] += 1
            mcell[mid] += c
    tot_m = sum(r["periods"] for r in rows)
    for mid, c in sorted(mcell.items(), key=lambda x: -x[1])[:20]:
        print(f"  {mid:22s} {mzh.get(mid,''):18s} {mcnt[mid]:4d} 家 / "
              f"{c:6d} 格（{c/tot_m:5.1%}）")

    print(f"\n【段 3】填補值分類（不是 n/a，但填法的可信度差很多）")
    ecnt = defaultdict(lambda: defaultdict(int))
    for r in rows:
        for cid, kinds in r["estimated"].items():
            for k, c in kinds.items():
                ecnt[cid][k] += c
    print(f"  {'科目':24s} {'':14s} {'視為0':>8s} {'沿用前期':>8s} {'推算':>8s} "
          f"{'近似股數':>8s} {'Q4推算':>8s}")
    # 依「視為 0 + 沿用前期」排序：Q4 推算是設計上就會有的（Q4 沒有 10-Q），
    # 前兩種才是拿假設當數字，要盯的是它們
    for cid, k in sorted(ecnt.items(), key=lambda x: -(x[1]["zero"] + x[1]["carry"]))[:16]:
        print(f"  {cid:24s} {zh.get(cid,''):14s} {k['zero']:8d} {k['carry']:8d} "
              f"{k['derive']:8d} {k['approx']:8d} {k['q4']:8d}")

    print(f"\n【段 3b】沿用前期拖最久的（拖 4 期以上＝拿一年前的數字充當本季）")
    worst = defaultdict(int)
    who = {}
    for r in rows:
        for cid, lag in r.get("stale", {}).items():
            if lag > worst[cid]:
                worst[cid], who[cid] = lag, r["ticker"]
    bad = sorted(((l, c) for c, l in worst.items() if l >= 4), reverse=True)
    for lag, cid in bad[:12]:
        n_bad = sum(1 for r in rows if r.get("stale", {}).get(cid, 0) >= 4)
        print(f"  {cid:24s} {zh.get(cid,''):14s} 最久 {lag:2d} 期（{who[cid]}），"
              f"拖≥4 期的公司 {n_bad} 家")
    if not bad:
        print("  無")

    if valuation:
        print(f"\n【段 4】估值倍數 n/a")
        vcnt = defaultdict(int)
        vhave = 0
        for r in rows:
            if "valuation_na" not in r:
                continue
            vhave += 1
            for vid, c in r["valuation_na"].items():
                vcnt[vid] += c
        noval = n - vhave
        print(f"  完全沒有估值分頁：{noval} 家（抓不到股價）")
        for vid, c in sorted(vcnt.items(), key=lambda x: -x[1]):
            print(f"  {vid:16s} {c:6d} 格")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=UNIVERSE)
    ap.add_argument("--limit", type=int)
    ap.add_argument("--tickers", help="逗號分隔，蓋過 universe")
    ap.add_argument("--valuation", action="store_true", help="連估值倍數一起抓（會打 Yahoo）")
    ap.add_argument("--refresh", action="store_true", help="忽略快取重抓")
    ap.add_argument("--report", action="store_true", help="只讀既有 JSONL 重新彙總")
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--out", default="api_coverage.jsonl")
    args = ap.parse_args()

    m = json.load(open(MAP_PATH, encoding="utf-8"))
    concepts, derived = m["concepts"], m["derived"]
    outp = os.path.join(OUT, args.out)

    if args.report:
        rows = [json.loads(l) for l in open(outp, encoding="utf-8")]
        report(rows, concepts, derived, args.valuation)
        return

    if args.tickers:
        universe = [{"ticker": t.strip(), "sector": "", "industry": ""}
                    for t in args.tickers.split(",")]
    else:
        universe = load_universe(args.universe)
    if args.limit:
        universe = universe[:args.limit]

    os.makedirs(OUT, exist_ok=True)
    rows, errs = [], []
    t0 = time.time()
    done = [0]

    def work(row):
        fin = fetch_api(row["ticker"], args.valuation, args.refresh)
        done[0] += 1
        if done[0] % 25 == 0:
            el = time.time() - t0
            print(f"  {done[0]}/{len(universe)}  {el:.0f}s  "
                  f"（估計還要 {el/done[0]*(len(universe)-done[0]):.0f}s）", flush=True)
        if not fin or "_error" in (fin or {}):
            return ("err", row["ticker"], (fin or {}).get("_error", "?"))
        return ("ok", analyse(row, fin, concepts, derived))

    with ThreadPoolExecutor(max_workers=args.workers) as ex:
        for kind, *rest in ex.map(work, universe):
            if kind == "ok":
                rows.append(rest[0])
            else:
                errs.append((rest[0], rest[1]))

    with open(outp, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    print(f"\n成功 {len(rows)} 家、失敗 {len(errs)} 家，耗時 {time.time()-t0:.0f}s → {outp}")
    for t, e in errs[:20]:
        print(f"  ✗ {t}: {e}")
    report(rows, concepts, derived, args.valuation)


if __name__ == "__main__":
    main()
