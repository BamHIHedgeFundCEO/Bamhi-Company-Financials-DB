#!/usr/bin/env python3
"""
分割偵測 × 雅虎除權事件的全母體普查 —— 產出仲裁六格表的實際數字。

為什麼要有這支：`computeSplits`（web/server/utils/financials.ts）與雅虎 chart API 的
`events=split` 是**兩個獨立證人**，兩邊都有獨有事件、也都出過錯（HON 只有偵測器看到、
BRO 是偵測器生的假 1:50）。要決定仲裁規則就得先量「兩邊各自獨有多少、形狀是什麼」。

  python tools/split_census.py                      # 全掃（可續跑，重跑吃快取）
  python tools/split_census.py --limit 200          # 先試 200 檔
  python tools/split_census.py --tickers HON,BRO,CVNA,HURA
  python tools/split_census.py --report             # 只讀既有結果重新彙總（秒級）

三個階段各自落地、各自可續跑：
  1. 偵測器（`det/`）：讀 ~/.bamhi-facts-cache 的 companyfacts，**零 SEC 請求**
  2. 雅虎（`yh/`）：每檔一個 chart 請求，URL 與 prices.ts 完全相同再加 `&events=split`
  3. 彙總：join 後印六格表

雅虎那一支刻意用 `range=10y`（與 prices.ts 一致）而不是 `range=max`——普查要量的是
**上線後真的會發生什麼**，不是雅虎知道多少。10y 之外的事件在線上本來就看不到，
必須落進「沒涵蓋 → 保留」那一格，用 max 量會把這格藏起來。
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
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))

CACHE = os.environ.get("BAMHI_FACTS_CACHE",
                       os.path.join(os.path.expanduser("~"), ".bamhi-facts-cache"))
OUT = os.path.join(ROOT, "tools", "sweep_out", "splits")
DET_DIR = os.path.join(OUT, "det")
YH_DIR = os.path.join(OUT, "yh")
LUT_PATH = os.path.join(OUT, "cik_lut.json")
UNIVERSE = os.environ.get(
    "BAMHI_UNIVERSE", os.path.join(os.path.expanduser("~"), "Downloads", "羅素3000.xlsx"))
UA = os.environ.get("SEC_USER_AGENT", "BamHI frank940702@gmail.com")

DAY = 86400.0


# ─────────────────────────────────────────────────────────────────────────────
# financials.ts 的 Python 鏡像。**逐行對抄，不要「順手改良」**——
# 這裡量到的必須是線上那份程式的行為，鏡像跑贏或跑輸都只是量錯。
# ─────────────────────────────────────────────────────────────────────────────

def span_days(p: dict) -> float | None:
    if not p.get("start"):
        return None
    return (date.fromisoformat(p["end"]) - date.fromisoformat(p["start"])).days


def fiscal_of(end: str, fye_month: int) -> tuple[int, int]:
    """financials.ts:204 fiscalOf"""
    et = date.fromisoformat(end)
    y = et.year
    TOL = timedelta(days=20)
    fye_year, fye_t = y, _last_day(y, fye_month)
    for yy in (y - 1, y, y + 1, y + 2):
        t = _last_day(yy, fye_month)
        if t >= et - TOL:
            fye_year, fye_t = yy, t
            break
    days_before = (fye_t - et).days
    q_back = round(days_before / 91.31) % 4
    q = 4 if q_back == 0 else 4 - q_back
    return fye_year, q


def _last_day(y: int, m: int) -> date:
    """日曆月 m 的最後一天（TS 的 Date.UTC(yy, fyeMonth, 0)）"""
    return date(y + m // 12, m % 12 + 1, 1) - timedelta(days=1)


def infer_fye_month(gaap: dict) -> int:
    """financials.ts:239 inferFyeMonth"""
    per_end: dict[str, int] = {}
    for tag in gaap.values():
        for points in tag.get("units", {}).values():
            for p in points:
                d = span_days(p)
                if d is not None and 300 < d < 400:
                    per_end[p["end"]] = per_end.get(p["end"], 0) + 1
    if not per_end:
        return 12
    latest = max(per_end)
    cutoff = date.fromisoformat(latest) - timedelta(days=6 * 365)
    count: dict[int, int] = {}
    for end, n in per_end.items():
        t = date.fromisoformat(end)
        if t < cutoff:
            continue
        m = (t - timedelta(days=5)).month
        count[m] = count.get(m, 0) + n
    best_m, best_n = 12, 0
    for m, n in count.items():
        if n > best_n:
            best_n, best_m = n, m
    return best_m


CLEAN = [2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 50]


def detect_split(ratio: float) -> float | None:
    """financials.ts:356 detectSplit"""
    if ratio >= 1.5:
        for s in CLEAN:
            if abs(ratio - s) / s < 0.08:
                return float(s)
    elif 0 < ratio <= 0.67:
        for s in CLEAN:
            if abs(1 / ratio - s) / s < 0.08:
                return 1 / s
    return None


def instant_shares(ns: dict) -> dict[str, list[dict]]:
    """financials.ts:464 instantShares"""
    pts = (ns.get("CommonStockSharesOutstanding") or {}).get("units", {}).get("shares")
    if pts is None:
        pts = (ns.get("CommonStockSharesIssued") or {}).get("units", {}).get("shares")
    by_end: dict[str, dict[str, float]] = defaultdict(dict)
    for p in pts or []:
        if span_days(p) is not None or p["val"] <= 0:
            continue
        by_end[p["end"]].setdefault(p["filed"], p["val"])
    out = {}
    for end, m in by_end.items():
        out[end] = sorted(({"filed": f, "val": v} for f, v in m.items()),
                          key=lambda x: x["filed"])
    return out


def collect_cross_filing_splits(pts, fye_month: int, per_share: bool, out: list) -> None:
    """financials.ts:484 collectCrossFilingSplits"""
    if not pts:
        return
    by_period: dict[str, list[dict]] = defaultdict(list)
    for p in pts:
        days = span_days(p)
        if per_share:
            ok = days is not None and (days < 100 or 300 < days < 400)
        else:
            ok = days is not None and 80 < days < 100
        if not ok or p["val"] <= 0:
            continue
        fy, q = fiscal_of(p["end"], fye_month)
        dur = "A" if (per_share and days > 300) else "Q"
        by_period[f"{dur}:FY{fy} Q{q}"].append({"filed": p["filed"], "val": p["val"]})
    for lst in by_period.values():
        lst.sort(key=lambda x: x["filed"])
        for i in range(len(lst) - 1):
            ratio = (lst[i]["val"] / lst[i + 1]["val"]) if per_share \
                else (lst[i + 1]["val"] / lst[i]["val"])
            f = detect_split(ratio)
            if f:
                out.append({"lo": lst[i]["filed"], "hi": lst[i + 1]["filed"],
                            "factor": f, "fromShares": not per_share, "ratio": ratio})


def shares_across(pts, fye_month: int, instant: dict, threshold: str) -> list[float]:
    """financials.ts:516 sharesAcross"""
    by_period: dict[str, list[dict]] = defaultdict(list)
    for p in pts or []:
        days = span_days(p)
        if days is None or days <= 80 or days >= 100 or p["val"] <= 0:
            continue
        fy, q = fiscal_of(p["end"], fye_month)
        by_period[f"Q:FY{fy} Q{q}"].append({"filed": p["filed"], "val": p["val"]})
    for end, lst in instant.items():
        by_period[f"I:{end}"] = lst

    out = []
    for lst in by_period.values():
        s = sorted(lst, key=lambda x: x["filed"])
        for i in range(len(s)):
            for j in range(i + 1, len(s)):
                if s[i]["filed"] < threshold <= s[j]["filed"]:
                    out.append(s[j]["val"] / s[i]["val"])
    return out


def compute_splits(ns: dict, fye_month: int) -> list[dict]:
    """financials.ts:392 computeSplits。**多回傳一個 `confirmed`**——
    仲裁六格表的橫軸就是它（裁判找到跨界線同期股數且比值等於倍數）。
    線上那份把這個訊息丟掉了，這裡留著。"""
    raw: list[dict] = []

    share_pts = (ns.get("WeightedAverageNumberOfSharesOutstandingBasic") or {}) \
        .get("units", {}).get("shares")
    if share_pts is None:
        share_pts = (ns.get("WeightedAverageNumberOfDilutedSharesOutstanding") or {}) \
            .get("units", {}).get("shares")
    collect_cross_filing_splits(share_pts, fye_month, False, raw)

    eps = (ns.get("EarningsPerShareBasic") or {}).get("units", {}).get("USD/shares")
    collect_cross_filing_splits(eps, fye_month, True, raw)

    instant = instant_shares(ns)
    for lst in instant.values():
        for i in range(len(lst) - 1):
            ratio = lst[i + 1]["val"] / lst[i]["val"]
            f = detect_split(ratio)
            if f and lst[i]["filed"] < lst[i + 1]["filed"]:
                raw.append({"lo": lst[i]["filed"], "hi": lst[i + 1]["filed"],
                            "factor": f, "fromShares": True, "ratio": ratio})

    clusters: list[list[dict]] = []
    for forward in (True, False):
        pend = sorted((s for s in raw if (s["factor"] > 1) == forward and s["lo"] < s["hi"]),
                      key=lambda s: s["hi"])
        while pend:
            point = pend[0]["hi"]
            inside = [s for s in pend if s["lo"] < point <= s["hi"]]
            clusters.append(inside)
            pend = [s for s in pend if not (s["lo"] < point <= s["hi"])]

    out = []
    for c in clusters:
        threshold = c[0]["hi"]
        votes: dict[float, int] = defaultdict(int)
        for s in c:
            votes[s["factor"]] += 2 if s["fromShares"] else 1
        ranked = [f for f, _ in sorted(votes.items(), key=lambda kv: (-kv[1], -kv[0]))]

        across = shares_across(share_pts, fye_month, instant, threshold)
        confirmed = next((f for f in ranked
                          if any(abs(r / f - 1) < 0.08 for r in across)), None)

        def tele(f: float, conf: bool) -> dict:
            """額外遙測（不改行為）：把「證據有多貼」量出來。
            8% 是個很寬的門，HON 的 0.500000 與 HURA 的 7.68/8 都算通過，
            但兩者一個是真的一個不是 —— 沒量就分不出來。"""
            devs = [abs(r / f - 1) for r in across] if conf else []
            raws = [abs(s["ratio"] / f - 1) for s in c if s["factor"] == f]
            return {"threshold": threshold, "factor": f, "confirmed": conf,
                    "dev": min(devs) if devs else None,
                    "devRaw": min(raws) if raws else None,
                    "nobs": len(c), "nAcross": len(across)}

        if confirmed is not None:
            out.append(tele(confirmed, True))
            continue
        if any(abs(r - 1) < 0.08 for r in across):
            continue
        out.append(tele(ranked[0], False))
    return sorted(out, key=lambda s: s["threshold"])


# ─────────────────────────────────────────────────────────────────────────────
# 階段 1：偵測器（離線）
# ─────────────────────────────────────────────────────────────────────────────

def det_one(args: tuple[str, str]) -> dict:
    ticker, cik = args
    path = os.path.join(DET_DIR, f"{ticker}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    rec = {"ticker": ticker, "cik": cik, "events": [], "filed": [], "err": None}
    fpath = os.path.join(CACHE, f"CIK{cik}.json.gz")
    if not os.path.exists(fpath):
        rec["err"] = "no-facts-cache"
    else:
        try:
            with gzip.open(fpath, "rt", encoding="utf-8") as f:
                facts = json.load(f).get("facts", {})
            gaap = facts.get("us-gaap") or {}
            ifrs = facts.get("ifrs-full") or {}
            if not gaap or (len(gaap) < 20 and ifrs):
                rec["err"] = "ifrs"          # 線上 useIfrs 分支：splits 一律空
            else:
                fye = infer_fye_month(gaap)
                rec["fye"] = fye
                rec["events"] = compute_splits(gaap, fye)
                # 「雅虎獨有」要換算成申報界線，需要全部申報日
                rec["filed"] = sorted({p["filed"]
                                       for tag in gaap.values()
                                       for pts in tag.get("units", {}).values()
                                       for p in pts})
        except Exception as e:  # noqa: BLE001
            rec["err"] = f"{type(e).__name__}: {e}"
    os.makedirs(DET_DIR, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(rec, f)
    return rec


# ─────────────────────────────────────────────────────────────────────────────
# 階段 2：雅虎（線上，URL 與 prices.ts 相同 + &events=split）
# ─────────────────────────────────────────────────────────────────────────────

YH_URL = ("https://query1.finance.yahoo.com/v8/finance/chart/{t}"
          "?range=10y&interval=1d&events=split")


def yh_one(ticker: str, retries: int = 3) -> dict:
    path = os.path.join(YH_DIR, f"{ticker}.json")
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    rec = {"ticker": ticker, "splits": [], "firstTrade": None,
           "rangeStart": None, "err": None}
    url = YH_URL.format(t=urllib.parse.quote(ticker.replace(".", "-")))
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=30) as r:
                j = json.load(r)
            res = (j.get("chart") or {}).get("result") or []
            if not res:
                rec["err"] = "no-result"
                break
            r0 = res[0]
            meta = r0.get("meta") or {}
            ft = meta.get("firstTradeDate")
            rec["firstTrade"] = _iso(ft) if ft else None
            ts = r0.get("timestamp") or []
            rec["rangeStart"] = _iso(ts[0]) if ts else None
            for ev in ((r0.get("events") or {}).get("splits") or {}).values():
                num, den = ev.get("numerator"), ev.get("denominator")
                if not num or not den:
                    continue
                rec["splits"].append({"date": _iso(ev["date"]),
                                      "num": num, "den": den,
                                      "ratio": ev.get("splitRatio")})
            rec["splits"].sort(key=lambda s: s["date"])
            break
        except urllib.error.HTTPError as e:
            rec["err"] = f"HTTP {e.code}"
            if e.code in (404, 400):
                break
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:  # noqa: BLE001
            rec["err"] = f"{type(e).__name__}"
            time.sleep(1.0 * (attempt + 1))
    os.makedirs(YH_DIR, exist_ok=True)
    # 只有拿到有效回應才落地；網路錯不寫，下次續跑重試
    if rec["splits"] or rec["firstTrade"] or rec["err"] in ("no-result", "HTTP 404", "HTTP 400"):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(rec, f)
    return rec


def _iso(epoch) -> str:
    # `datetime.fromtimestamp` 在 Windows 遇到負 epoch 會丟 OSError，而 1970 年前上市的
    # 公司（HON −252322200＝1962）firstTradeDate 就是負的 —— 羅素 3000 裡 28 檔全是
    # 老牌大型股，整批被吞掉。用 epoch 起點加減天數繞過平台限制。
    return (datetime(1970, 1, 1, tzinfo=timezone.utc)
            + timedelta(seconds=int(epoch))).date().isoformat()


# ─────────────────────────────────────────────────────────────────────────────
# 階段 3：join → 六格表
# ─────────────────────────────────────────────────────────────────────────────

# 除權日到「第一份新基準申報」之間的落差。實測 0–120 天（季報週期），留 200 天餘裕。
MATCH_WINDOW = 200


def yh_factor(s: dict) -> float:
    return s["num"] / s["den"]


def yh_is_clean(s: dict) -> bool:
    """分拆造成的價格調整（GE 1.0408、ZBH 1.03）也走 splits 事件回來。
    真分割的分子分母都是整數；分拆調整不是。"""
    num, den = s["num"], s["den"]
    if abs(num - round(num)) > 0.01 or abs(den - round(den)) > 0.01:
        return False
    if round(num) == round(den) or round(num) < 1 or round(den) < 1:
        return False
    return max(round(num), round(den)) <= 100


def covered(yh: dict, threshold: str) -> bool:
    """雅虎「涵蓋」這個候選＝**整個可能的除權日區間**都在視窗內。少了 firstTradeDate，
    改名／重新上市的公司會被誤當成「雅虎說沒有」而誤刪。

    比對的是 threshold−MATCH_WINDOW 而不是 threshold 本身：除權日比申報界線早
    0–120 天，只看界線的話，界線落在視窗開頭 120 天內的事件，除權日其實在視窗外，
    雅虎根本看不到卻會被記成「雅虎否認」（BF.A/BF.B 2016-08-31 就是，視窗起點
    2016-08-22）。"""
    starts = [d for d in (yh.get("firstTrade"), yh.get("rangeStart")) if d]
    if not starts:
        return False
    earliest_ex = (date.fromisoformat(threshold) - timedelta(days=MATCH_WINDOW)).isoformat()
    return earliest_ex >= max(starts)


# 同一次分割被偵測器切成兩個事件時，兩個 threshold 相隔多遠。HURA 的 1:10 被切成
# 2019-05-14 與 2019-09-09（相隔 118 天），共用雅虎那一筆 2019-05-08。
DUP_WINDOW = 540

# 「證據貼不貼」的門檻。真分割會把同一期的股數**原封重述成整數倍**，比值本來就該是
# 乾淨的；0.1% 的餘裕是留給申報單位捨入（HON 637,500,000→318,800,000＝0.50008）。
# 現行 detectSplit 的 8% 容差寬到會把發股、換股、SPAC 增資一起收進來 ——
# 全母體實測：雅虎背書的 A1 有 268/279 落在 0.1% 內，雅虎否認的 A2 只有 16/79。
EXACT = 0.001


def exact(ev: dict) -> bool:
    """dev 與 devRaw 兩個都要貼。只看 dev 會漏掉 NSSC 型：
    NAPCO 只做過一次 2:1（2022-01），偵測器卻在 2023-02 與 2023-09 各生一個 ×2；
    `sharesAcross` 拿任何跨界線的配對當證據，2022 那次的重述配對照樣跨得過
    2023 的界線 → dev＝0，但生出這個群的原始觀測 devRaw 是 4.5%／6.3%。"""
    xs = [x for x in (ev.get("dev"), ev.get("devRaw")) if x is not None]
    return bool(xs) and max(xs) < EXACT


def join_one(det: dict, yh: dict | None) -> list[dict]:
    """回傳每個「事件」的一列，cell 是格子代號。"""
    rows = []
    ysp = [s for s in (yh or {}).get("splits", []) if yh_is_clean(s)] if yh else []
    used: dict[int, str] = {}
    pending = []
    for ev in sorted(det.get("events", []), key=lambda e: e["threshold"]):
        th, f = ev["threshold"], ev["factor"]
        hit = None
        for i, s in enumerate(ysp):
            if i in used:
                continue
            lo = (date.fromisoformat(th) - timedelta(days=MATCH_WINDOW)).isoformat()
            if lo <= s["date"] <= th and abs(yh_factor(s) / f - 1) < 0.08:
                hit = i
                break
        if hit is not None:
            used[hit] = th
            rows.append(_row(det, ev, "A1" if ev["confirmed"] else "B1", ysp[hit]["date"]))
        else:
            pending.append(ev)

    for ev in pending:
        th, f = ev["threshold"], ev["factor"]
        # 先問「這是不是已經配對過的那一次分割被切成兩半」。只在**沒配到**的事件上問，
        # 所以 Copart 2022／2023 連兩次真 2:1（各自配到自己的雅虎事件）不會被誤併。
        dup = None
        for i, s in enumerate(ysp):
            if i not in used or abs(yh_factor(s) / f - 1) >= 0.08:
                continue
            if abs((date.fromisoformat(th) - date.fromisoformat(s["date"])).days) <= DUP_WINDOW:
                dup = s["date"]
                break
        if dup:
            rows.append(_row(det, ev, "D1" if ev["confirmed"] else "D2", dup))
        elif not yh or yh.get("err"):
            rows.append(_row(det, ev, "A4" if ev["confirmed"] else "B4", None))
        elif covered(yh, th):
            if ev["confirmed"]:
                rows.append(_row(det, ev, "A2a" if exact(ev) else "A2b", None))
            else:
                rows.append(_row(det, ev, "B2", None))
        else:
            rows.append(_row(det, ev, "A3" if ev["confirmed"] else "B3", None))

    for i, s in enumerate(ysp):
        if i in used:
            continue
        # 偵測器完全沒偵到 → 新增。界線＝除權日當天或之後的第一份申報
        th = next((d for d in det.get("filed", []) if d >= s["date"]), None)
        rows.append({"ticker": det["ticker"], "cell": "C1", "threshold": th,
                     "factor": yh_factor(s), "confirmed": None, "yh": s["date"],
                     "dev": None, "devRaw": None, "nobs": 0})
    return rows


def _row(det: dict, ev: dict, cell: str, yh_date: str | None) -> dict:
    return {"ticker": det["ticker"], "cell": cell, "threshold": ev["threshold"],
            "factor": ev["factor"], "confirmed": ev["confirmed"], "yh": yh_date,
            "dev": ev.get("dev"), "devRaw": ev.get("devRaw"), "nobs": ev.get("nobs", 0)}


CELL_DESC = {
    "A1": "裁判確認 ＋ 雅虎有            → 採用",
    "A2a": "裁判確認(貼合) ＋ 雅虎涵蓋卻沒有 → 採用（HON 型，雅虎不得否決）",
    "A2b": "裁判確認(鬆) ＋ 雅虎涵蓋卻沒有   → 丟棄（HURA ×8／SIRI 1:12 型）",
    "A3": "裁判確認 ＋ 雅虎沒涵蓋        → 採用",
    "A4": "裁判確認 ＋ 雅虎抓不到        → 採用",
    "B1": "沒確認   ＋ 雅虎有            → 採用（兩個弱證據互補）",
    "B2": "沒確認   ＋ 雅虎涵蓋卻沒有    → 丟棄（BRO 型）",
    "B3": "沒確認   ＋ 雅虎沒涵蓋        → 保留，不知道就不猜",
    "B4": "沒確認   ＋ 雅虎抓不到        → 保留",
    "C1": "偵測器沒偵到 ＋ 雅虎有        → 新增（CVNA／HURA 型）",
    "D1": "與已配對的同倍數事件重複(確認) → 合併（雅虎當去重器）",
    "D2": "與已配對的同倍數事件重複(沒確認) → 合併",
}


def _pct(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    return s[min(len(s) - 1, int(q * len(s)))]


def report() -> None:
    dets, yhs = {}, {}
    for fn in os.listdir(DET_DIR) if os.path.isdir(DET_DIR) else []:
        with open(os.path.join(DET_DIR, fn), encoding="utf-8") as f:
            d = json.load(f)
        dets[d["ticker"]] = d
    for fn in os.listdir(YH_DIR) if os.path.isdir(YH_DIR) else []:
        with open(os.path.join(YH_DIR, fn), encoding="utf-8") as f:
            y = json.load(f)
        yhs[y["ticker"]] = y

    rows, cells = [], Counter()
    firms = defaultdict(set)
    no_yh = 0
    for t, d in dets.items():
        if d.get("err") in ("no-facts-cache", "ifrs"):
            continue
        y = yhs.get(t)
        if y is None:
            no_yh += 1
            continue
        r = join_one(d, y)
        rows.extend(r)
        for x in r:
            cells[x["cell"]] += 1
            firms[x["cell"]].add(t)

    out_path = os.path.join(OUT, "census.jsonl")
    with open(out_path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    print(f"\n偵測器結果 {len(dets)} 檔／雅虎結果 {len(yhs)} 檔"
          f"／兩邊齊全 {len(dets) - no_yh} 檔（缺雅虎 {no_yh}）")
    print(f"事件總數 {len(rows)}   明細：{out_path}\n")
    order = ("A1", "A2a", "A2b", "A3", "A4", "B1", "B2", "B3", "B4", "C1", "D1", "D2")
    print(f"{'格':<4}{'事件':>6}{'家數':>6}  說明")
    print("─" * 78)
    for k in order:
        print(f"{k:<4}{cells[k]:>6}{len(firms[k]):>6}  {CELL_DESC[k]}")
    print("─" * 78)
    keep = sum(cells[k] for k in ("A1", "A2a", "A3", "A4", "B1", "B3", "B4"))
    drop = cells["A2b"] + cells["B2"]
    print(f"採用/保留 {keep}   丟棄 {drop}   新增 {cells['C1']}   "
          f"合併 {cells['D1'] + cells['D2']}")

    # A1（雅虎背書）與 A2（雅虎涵蓋卻否認）的證據貼合度分布。
    # 若 A2 明顯比 A1 鬆，代表「沒有第二證人時該提高門檻」站得住腳。
    print("\n證據貼合度 dev＝|實測比值/倍數−1|（8% 是現行門檻）")
    print(f"{'格':<4}{'n':>5}{'中位':>9}{'p75':>9}{'p90':>9}{'≥2%':>7}{'≥4%':>7}")
    for k in ("A1", "A2a", "A2b", "A3", "B1", "B2", "C1"):
        d = [r["dev"] for r in rows if r["cell"] == k and r.get("dev") is not None]
        if not d:
            continue
        print(f"{k:<4}{len(d):>5}{_pct(d, .5):>9.4f}{_pct(d, .75):>9.4f}"
              f"{_pct(d, .9):>9.4f}{sum(x >= .02 for x in d):>7}"
              f"{sum(x >= .04 for x in d):>7}")

    for k in ("A2a", "A2b", "B2", "C1", "D1"):
        sample = [r for r in rows if r["cell"] == k]
        if not sample:
            continue
        print(f"\n── {k} 抽樣（共 {len(sample)}）──")
        for r in sample[:15]:
            dev = f"{r['dev']:.4f}" if r.get("dev") is not None else "  n/a"
            print(f"  {r['ticker']:<7} threshold={r['threshold']} "
                  f"factor={r['factor']:<9.4f} dev={dev} yh={r['yh']}")


# ─────────────────────────────────────────────────────────────────────────────

def load_lut() -> dict[str, str]:
    if os.path.exists(LUT_PATH):
        with open(LUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    from sweep import build_cik_lut
    lut = build_cik_lut()
    os.makedirs(OUT, exist_ok=True)
    with open(LUT_PATH, "w", encoding="utf-8") as f:
        json.dump(lut, f)
    return lut


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--universe", default=UNIVERSE)
    ap.add_argument("--tickers")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--report", action="store_true")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    if args.report:
        report()
        return

    if args.tickers:
        tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    else:
        from sweep import load_universe
        tickers = [r["ticker"].upper() for r in load_universe(args.universe)]
    if args.limit:
        tickers = tickers[:args.limit]

    from sweep import resolve
    lut = load_lut()
    pairs = []
    for t in tickers:
        cik = resolve(t, lut)
        if cik:
            pairs.append((t, cik))
    print(f"母體 {len(tickers)} 檔，解析到 CIK {len(pairs)} 檔")

    os.makedirs(DET_DIR, exist_ok=True)
    os.makedirs(YH_DIR, exist_ok=True)

    t0 = time.time()
    todo = [p for p in pairs if not os.path.exists(os.path.join(DET_DIR, f"{p[0]}.json"))]
    print(f"\n階段 1／偵測器（離線）：待跑 {len(todo)}／{len(pairs)}")
    if todo:
        with ProcessPoolExecutor(max_workers=args.workers) as ex:
            for i, _ in enumerate(ex.map(det_one, todo, chunksize=4), 1):
                if i % 200 == 0 or i == len(todo):
                    el = time.time() - t0
                    print(f"  {i}/{len(todo)}  {el:.0f}s"
                          f"（估計還要 {el / i * (len(todo) - i):.0f}s）", flush=True)

    t1 = time.time()
    ytodo = [t for t, _ in pairs if not os.path.exists(os.path.join(YH_DIR, f"{t}.json"))]
    print(f"\n階段 2／雅虎（{len(ytodo)} 個請求，可中斷續跑）")
    if ytodo:
        done = [0]

        def work(t):
            r = yh_one(t)
            done[0] += 1
            if done[0] % 200 == 0 or done[0] == len(ytodo):
                el = time.time() - t1
                print(f"  {done[0]}/{len(ytodo)}  {el:.0f}s"
                      f"（估計還要 {el / done[0] * (len(ytodo) - done[0]):.0f}s）", flush=True)
            return r

        with ThreadPoolExecutor(max_workers=6) as ex:
            list(ex.map(work, ytodo))

    print(f"\n階段 3／彙總（總耗時 {time.time() - t0:.0f}s）")
    report()


if __name__ == "__main__":
    main()
