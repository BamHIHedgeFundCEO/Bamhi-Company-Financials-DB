#!/usr/bin/env python3
"""
多股別公司的期末流通股數 —— 產生 `config/class_shares.json`。

**為什麼需要離線預算**

`companyfacts` API 只收無維度事實。多股別公司（波克夏 A/B、Visa A/B-1/B-2/B-3/C、
自由媒體 F1 集團 A/B/C、Up-C 架構的 Ryan/Planet Fitness/Hamilton Lane…）把封面股數
`dei:EntityCommonStockSharesOutstanding` **按股別拆**，帶了 `StatementClassOfStockAxis`
維度 → 整個標籤在 companyfacts 裡消失。連加權平均股數也常常一起帶維度消失，
於是期末股數整欄 n/a、市值／本益比／每股淨值整頁跟著連鎖 n/a。

數字只在申報的 XBRL instance 裡。但 CLAUDE.md 硬規則：**三大報表單 ticker 查詢
≤ 2 次 SEC 請求**，線上解析 instance 一定超標。所以在這裡離線算好、存成設定資產，
執行期零 SEC 請求。已申報財報不可變 → 舊期永久有效；新的一季要重跑這支才會有
（沒重跑的期就維持 n/a，是誠實的留白，不是錯的數字）。

**加總不是「把各股別相加」那麼簡單**

  波克夏：A 股每股可轉 1500 股 B 股。直接相加 488,450 + 1,408,035,161 = 14.1 億，
          正確的 B 股當量是 488,450×1500 + 1,408,035,161 = **21.4 億**，差 34%。
  Visa：  B-1/B-2/B-3/C 各有不同轉換率，直接相加 17.8 億，公司自己申報的
          當量總數是 **18.8 億**，差 5%。

兩家的換算資訊其實都申報在 XBRL 裡，只是一個是標準標籤、一個是公司自訂標籤：

  1. 公司自己就報了「當量基礎」的**無維度總數**（Visa 的
     `SharesOutstandingAsConvertedBasis`）→ 直接採用，最準
  2. 公司報了轉換率（波克夏的 us-gaap:
     `NumberOfSharesObtainableFromConvertingOneShareFromOneClassToAnotherClass`
     掛在 B 股成員上 = 1500，意思是「1 股 A 可換 1500 股 B」）→ 以帶率的那個股別
     為單位換算。只在**剛好兩個股別**時採用，三個以上換算方向不唯一，寧可不猜
  3. 都沒有 → 各股別直接相加（1:1 經濟權益，ADT / COKE / F1 / STZ / Up-C 那幾家）

用法：
  python tools/class_shares.py                      # 掃全 universe 找缺股數的公司
  python tools/class_shares.py BRK.B V ADT          # 只算指定幾家
  python tools/class_shares.py --filings 12         # 往前多抓幾份（預設 8）
  python tools/class_shares.py --dry-run            # 只印不寫檔

SEC 請求量：每家 1（submissions）+ 每份 2（index.json + instance）。離線一次性成本。
"""
import argparse
import gzip
import io
import json
import os
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "tools"))
from segment import fetch, resolve_cik, instance_url, parse_instance  # noqa: E402

OUT_PATH = os.path.join(ROOT, "config", "class_shares.json")
API_CACHE = os.path.join(ROOT, "tools", "sweep_out", "api")

CLASS_AXIS = "StatementClassOfStockAxis"
COVER_TAG = "EntityCommonStockSharesOutstanding"
BS_TAG = "CommonStockSharesOutstanding"
RATIO_TAG = "NumberOfSharesObtainableFromConvertingOneShareFromOneClassToAnotherClass"
# 「當量基礎」的股數 —— 標準分類法沒有這個概念，各家自訂命名，用形狀比對
ASCONV_RE = re.compile(r"SharesOutstanding.*AsConverted|AsConverted.*SharesOutstanding", re.I)
# 特別股不是普通股，不能算進普通股當量總數
PREF_RE = re.compile(r"Preferred", re.I)
EPS_RE = re.compile(r"^EarningsPerShare(Basic|Diluted|BasicAndDiluted)$")
WAVG = {
    "WeightedAverageNumberOfSharesOutstandingBasic": "basic",
    "WeightedAverageNumberOfDilutedSharesOutstanding": "diluted",
}


def bare(qname: str) -> str:
    return qname.split(":")[-1]


def pretty(member: str) -> str:
    """`CommonClassBMember` → `Class B`，給人看的短名。"""
    m = re.search(r"Class([A-Z]\d?)", member)
    return f"Class {m.group(1)}" if m else member.replace("Member", "")


def norm_class(member: str) -> str:
    """
    股別成員正規化 —— 同一個股別在同一份申報裡常有兩個名字。

    ADT 的封面股數掛 `CommonStockUndefinedMember`、每股盈餘卻掛 `CommonStockMember`，
    照字面比對會判定「這個股別沒有盈餘」，把 6.76 億股的主普通股整個丟掉、
    只剩 5,474 萬股 B 股（實際兩個股別的 EPS 都是 0.72，盈餘是均分的）。
    去掉 Common/Stock/Class/Undefined/Series/Member 這些純結構字之後，
    兩者都變成空字串 → 認得出是同一個股別。
    """
    s = member.lower()
    for w in ("member", "undefined", "common", "stock", "class", "series"):
        s = s.replace(w, "")
    return s


def scan_missing() -> list[str]:
    """從 api_sweep 的快取找「期末股數整欄 n/a」的公司。"""
    out = []
    if not os.path.isdir(API_CACHE):
        return out
    for fn in sorted(os.listdir(API_CACHE)):
        if not fn.endswith(".json.gz"):
            continue
        try:
            with gzip.open(os.path.join(API_CACHE, fn), "rt", encoding="utf-8") as f:
                d = json.load(f)
        except Exception:
            continue
        periods = d.get("periods") or []
        if not periods:
            continue
        li = next((x for x in d.get("lineItems", []) if x["id"] == "shares_outstanding"), None)
        got = sum(1 for p in periods if li and (li["values"].get(p) or {}).get("value") is not None)
        if got == 0:
            out.append(fn.split(".n.json.gz")[0])
    return out


def recent_filings(cik10: str, limit: int) -> tuple[str, list[dict]]:
    """
    最近 `limit` 份財報，**純粹依申報日排序**。

    不能用 segment.py 的 `pick_filings`：那支為了分部揭露把年報排在季報前面，
    limit=8 會全部吃成 10-K，季末的股數一份都拿不到（波克夏實測最新只到 1 月）。
    這裡要的是「時間上最近的申報」，年報季報一視同仁。
    """
    sub = fetch(f"https://data.sec.gov/submissions/CIK{cik10}.json")
    r = sub["filings"]["recent"]
    rows = [
        {"form": f, "accn": r["accessionNumber"][i], "filed": r["filingDate"][i]}
        for i, f in enumerate(r["form"])
        if f in ("10-K", "10-Q", "20-F", "40-F")
    ]
    rows.sort(key=lambda x: x["filed"], reverse=True)
    return sub.get("name", ""), rows[:limit]


def extract(parsed: dict) -> dict:
    """
    從一份 instance 抽出 {日期: {"classes": {股別: 股數}, "asconv": 當量總數}}
    以及 {股別: 轉換率}。
    """
    ctxs = parsed["contexts"]
    per_date: dict[str, dict] = defaultdict(lambda: {"classes": {}, "asconv": None})
    ratios: dict[str, float] = {}
    eps_classes: set[str] = set()
    eps_by_period: dict[str, float] = {}
    ni_by_period: dict[str, float] = {}
    wavg: dict[str, dict[str, dict[str, float]]] = {}

    for f in parsed["facts"]:
        if f["unit"] is None:
            continue
        ctx = ctxs.get(f["ctx"])
        if not ctx:
            continue
        # 封面股數與資產負債表股數都是「時點」事實
        d = ctx.get("instant")
        tag = bare(f["tag"])
        try:
            val = float(f["val"].replace(",", ""))
        except ValueError:
            continue

        dims = ctx["dims"]
        cls = next((bare(x["member"]) for x in dims if bare(x["axis"]) == CLASS_AXIS), None)
        other_dims = [x for x in dims if bare(x["axis"]) != CLASS_AXIS]

        if tag == RATIO_TAG and cls:
            ratios[cls] = val
            continue
        end = ctx.get("end")
        if tag == "EarningsPerShareBasic" and end and not other_dims and val:
            if cls:
                eps_classes.add(cls)
            eps_by_period.setdefault(end, val)
            continue
        if EPS_RE.match(tag):
            if cls:
                eps_classes.add(cls)
            continue
        if tag == "NetIncomeLoss" and end and not dims:
            ni_by_period.setdefault(end, val)
            continue
        # 加權平均是「期間」事實（掛 end，沒有 instant），要在 instant 檢查之前處理
        if tag in WAVG and cls and end and not other_dims and not PREF_RE.search(cls):
            wavg.setdefault(WAVG[tag], {}).setdefault(end, {})[cls] = val
            continue
        if not d:
            continue
        if ASCONV_RE.search(tag) and not dims:
            # 無維度的當量總數 —— 公司自己算好的，最準
            per_date[d]["asconv"] = val
            continue
        if tag in (COVER_TAG, BS_TAG) and cls and not other_dims:
            if PREF_RE.search(cls):
                continue
            # 同一份申報同一日期同一股別可能重複出現，取一致值即可
            per_date[d]["classes"][cls] = val

    return {"per_date": dict(per_date), "ratios": ratios, "eps_classes": eps_classes,
            "eps_by_period": eps_by_period, "ni_by_period": ni_by_period, "wavg": wavg}


def total_for(entry: dict, ratios: dict, eps_classes: set, implied: float | None) -> tuple[float, str] | None:
    """回傳 (股數, 依據說明)。算不出來就 None —— 寧可留白不猜。"""
    classes = entry.get("classes") or {}
    if not classes:
        return None

    # 各股別的數值完全相同 → 那不是各股別的數字，是同一個**總數**被重複掛在每個
    # 股別成員上（Shift4 2026 年的加權平均股數就是這樣，A 與 C 都寫 73,641,439）。
    # 照加會變成兩倍。
    vals = list(classes.values())
    if len(vals) > 1 and len(set(vals)) == 1:
        return vals[0], "公司把同一個總數掛在每個股別（不重複計算）"

    """
    Up-C（傘型合夥）架構的陷阱：Class B 對應的是子公司的 LLC 單位、不是母公司股權。
    母公司損益表的 `NetIncomeLoss` **只含 Class A 那一份**，NCI 另外列。把 B 股一起
    加進分母，本益比就會憑空放大 —— Ryan Specialty 2025 年報：淨利 6,340 萬
    （＝Class A 那份）、NCI 1 億 5,076 萬，A 股 1.22 億、B 股 1.34 億。
    加總 2.56 億當分母 → 本益比正好高一倍。

    但「只留有申報 EPS 的股別」這個判準單獨用會誤殺：
      ADT   封面股數掛 CommonStockUndefinedMember、EPS 掛 CommonStockMember —— 同一個
            股別兩個名字（norm_class 處理掉了）
      自由媒體 F1  A/B/C 三個系列**共用一個 EPS**，而那個成員名剛好等於 A 系列，
            照字面判會只剩 2,399 萬股、實際 2.51 億股

    所以最後由公司自己的算術裁決：**隱含股數 = 淨利 ÷ 每股盈餘**，
    「只留盈餘股別」和「全部相加」哪個接近就用哪個。這是公司自己在財報上做的除法，
    不是我們猜的。
    """
    earning = {norm_class(c) for c in eps_classes}
    keep = [c for c in classes if norm_class(c) in earning]
    total_all = sum(classes.values())
    if keep and len(keep) != len(classes) and implied:
        total_keep = sum(classes[c] for c in keep)
        if abs(total_keep - implied) < abs(total_all - implied):
            dropped = sorted(c for c in classes if c not in keep)
            return total_keep, f"僅計盈餘歸屬的股別，不計 {'／'.join(pretty(c) for c in dropped)}"

    if len(classes) == 1:
        return next(iter(classes.values())), "單一股別"
    conv = {c: r for c, r in ratios.items() if c in classes and r and r != 1}
    if conv:
        # 只處理「兩個股別、其中一個帶轉換率」：率掛在目標股別上（波克夏掛在 B 股
        # ＝1 股 A 換 1500 股 B），所以另一個股別要乘上這個率。三個股別以上換算
        # 方向不唯一，不猜。
        if len(classes) != 2 or len(conv) != 1:
            return None
        target, r = next(iter(conv.items()))
        other = next(c for c in classes if c != target)
        return classes[other] * r + classes[target], f"各股別合計（{pretty(other)} 按 {r:g}:1 換算）"
    return total_all, "各股別合計"


def run(ticker: str, limit: int) -> dict | None:
    cik = resolve_cik(ticker)
    if not cik:
        print(f"{ticker:8} 找不到 CIK")
        return None
    name, filings = recent_filings(cik, limit)
    per_date: dict[str, dict] = {}
    ratios: dict[str, float] = {}
    eps_classes: set[str] = set()
    wavg_all: dict[str, dict[str, dict[str, float]]] = {}
    eps_p: dict[str, float] = {}
    ni_p: dict[str, float] = {}
    src: dict[str, str] = {}
    for fl in filings:
        url = instance_url(cik, fl["accn"])
        if not url:
            continue
        try:
            parsed = parse_instance(fetch(url, as_json=False))
        except Exception as e:
            print(f"{ticker:8} {fl['accn']} 解析失敗：{e}")
            continue
        got = extract(parsed)
        ratios.update(got["ratios"])
        eps_classes |= got["eps_classes"]
        for k, v in got["eps_by_period"].items():
            eps_p.setdefault(k, v)
        for k, v in got["ni_by_period"].items():
            ni_p.setdefault(k, v)
        for kind, byd in got["wavg"].items():
            tgt = wavg_all.setdefault(kind, {})
            for d, byc in byd.items():
                tgt.setdefault(d, {}).update(byc)
        for d, entry in got["per_date"].items():
            # 新的申報說了算（改記帳基礎、追溯調整都以最新為準）
            if d not in per_date or fl["filed"] > src.get(d, ""):
                per_date[d] = entry
                src[d] = fl["filed"]

    # 「公司自報的當量總數」和「各股別加總」是兩套基礎，**不能混用**：Visa 同一份
    # 10-Q 裡當量總數掛在季末（2026-06-30，18.8 億），封面股別股數掛在申報日
    # （2026-07-21，加總 17.8 億），差 5%。混著填會讓同一列的欄與欄之間基礎不同，
    # 市值走勢憑空多出一段跳動。公司只要報過當量總數，整列就只用當量總數。
    both = sorted(set(eps_p) & set(ni_p))
    implied = None
    for d in reversed(both):
        if eps_p[d]:
            implied = ni_p[d] / eps_p[d]
            break

    asconv = {d: e["asconv"] for d, e in per_date.items() if e.get("asconv")}
    shares: dict[str, float] = {}
    detail: dict[str, dict] = {}
    if asconv:
        basis = "公司申報的當量基礎總數"
        shares = dict(sorted(asconv.items()))
    else:
        # 同一列的數字必須是**同一個量級基礎**，否則市值會在欄與欄之間憑空跳一個數量級。
        # 兩個實際踩到的來源：
        #   1. 基礎不同 —— 波克夏季末的 `CommonStockSharesOutstanding` 是「A 股當量」
        #      1,437,251 股，封面的 `EntityCommonStockSharesOutstanding` 是 A/B 兩個
        #      股別、換算後是 B 股當量 21.4 億。差 1500 倍
        #   2. 股票分割 —— 可口可樂裝瓶 2025 年 10 股換 1 股，封面股數從 870 萬變 8,690 萬。
        #      這裡的數字沒有還原分割（分割資訊在 companyfacts 那側），舊日期照用會讓
        #      2024 年的市值差 10 倍
        # 兩種都只保留與最新一筆同量級的日期（0.5–2 倍）。被丟掉的舊期是 n/a，
        # 不是錯的數字；真正的買回／增資幅度都在這個帶內（ADT 三年 9.22 億 → 7.31 億）。
        cand = {}
        for d in sorted(per_date):
            got = total_for(per_date[d], ratios, eps_classes, implied)
            if got:
                cand[d] = got
        if cand:
            latest = cand[sorted(cand)[-1]][0]
            basis = cand[sorted(cand)[-1]][1]
            for d, (v, _b) in cand.items():
                if not latest or not 0.5 <= v / latest <= 2:
                    continue
                shares[d] = v
                detail[d] = per_date[d]["classes"]
        else:
            basis = ""

    # 加權平均股數：同樣用股別資訊算，供 companyfacts 的無維度值本身就錯的公司使用
    # （Shift4 把 Class C 的 133 萬也標成無維度，API 拿到的「全公司加權平均」就是它）
    wavg_out: dict[str, dict[str, float]] = {}
    for kind, byd in wavg_all.items():
        for d, byc in sorted(byd.items()):
            got = total_for({"classes": byc}, ratios, eps_classes, implied)
            if got:
                wavg_out.setdefault(kind, {})[d] = got[0]

    if not shares:
        print(f"{ticker:8} 申報裡也找不到可用的股別股數")
        return None
    last = sorted(shares)[-1]
    imp = f"，淨利÷EPS 隱含 {implied/1e6:,.1f}M" if implied else ""
    print(f"{ticker:8} {len(shares):2} 個日期；最新 {last} = {shares[last]:,.0f}{imp}　依據：{basis}")
    for c, v in sorted(detail.get(last, {}).items()):
        print(f"           {c:44} {v:>18,.0f}")
    out = {
        "ticker": ticker,
        "name": name,
        "basis": basis,
        "shares": {d: shares[d] for d in sorted(shares)},
    }
    if wavg_out:
        out["wavg"] = wavg_out
        for kind, byd in wavg_out.items():
            last_d = sorted(byd)[-1]
            print(f"           加權平均（{kind}）{len(byd)} 期，最新 {last_d} = {byd[last_d]:,.0f}")
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--filings", type=int, default=8)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    tickers = a.tickers or scan_missing()
    if not tickers:
        print("沒有需要處理的公司（先跑 tools/api_sweep.py 產生快取，或直接指定 ticker）")
        return
    print(f"目標 {len(tickers)} 家：{' '.join(tickers)}\n")

    out = {}
    for t in tickers:
        try:
            r = run(t, a.filings)
        except Exception as e:
            print(f"{t:8} 失敗：{e}")
            continue
        if r:
            cik = resolve_cik(t)
            out[str(int(cik))] = r

    if a.dry_run:
        print("\n--dry-run，未寫檔")
        return
    doc = {
        "version": date.today().isoformat(),
        "note": (
            "多股別公司的期末流通股數。companyfacts 只收無維度事實，這些公司的封面股數"
            "按股別拆、帶了 StatementClassOfStockAxis 維度而整個消失，只在申報的 XBRL "
            "instance 裡。線上查詢有「≤2 次 SEC 請求」的硬限制，所以離線算好放這裡，"
            "執行期零 SEC 請求。新的一季要重跑 tools/class_shares.py 才會有；沒跑到的期"
            "維持 n/a。日期是申報封面的「股數截止日」，通常落在季末後數週，"
            "financials.ts 會對回最接近的那一期。"
        ),
        "companies": out,
    }
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        json.dump(doc, f, ensure_ascii=False, indent=1)
    print(f"\n寫入 {OUT_PATH}（{len(out)} 家）")


if __name__ == "__main__":
    main()
