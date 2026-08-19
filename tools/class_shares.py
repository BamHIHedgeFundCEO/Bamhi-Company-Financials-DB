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
  3. 都沒有 → 各股別直接相加（1:1 經濟權益，ADT / COKE / F1 / STZ 那幾家）

再兩道，都在 `upc_drop()` / `undo_splits()`：

  4. Up-C 的 Class B/D 是子公司的 LLC 單位，不分享母公司損益，**不能加進去**
     （UWM 加了 Ishbia 的 12.6 億 Class D，市值高估 4.8 倍）
  5. 這裡的數字是逐份申報抄的原文、沒有還原分割。倍數只認公司自己申報的
     `StockholdersEquityNoteStockSplitConversionRatio1`，界線由數列跳點決定

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
# 公司自己申報的分割倍數。這裡**只取倍數、不取日期** —— 事實掛的 instant 有時是
# 董事會宣告日而不是生效日（可口可樂裝瓶宣告 2025-03-04、生效在 5 月底），
# 拿它當界線會漏掉中間那一季。界線改由數列自己的跳點決定，見 `undo_splits()`。
SPLIT_TAG = "StockholdersEquityNoteStockSplitConversionRatio1"
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
    splits: set[float] = set()

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
        if tag == SPLIT_TAG and not dims and val > 1:
            splits.add(val)
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
            "eps_by_period": eps_by_period, "ni_by_period": ni_by_period, "wavg": wavg,
            "splits": splits}


def split_basis(raw: dict[str, dict[str, float]], splits: set[float]) -> tuple[set[str], set[str], float]:
    """
    判斷**每一份申報**是分割前還是分割後的基準。回傳 (分割前, 分割後, 倍數)。

    這裡的數字是逐份申報抄下來的原文，沒有還原分割：可口可樂裝瓶 2025 年 10 股換
    1 股，同一列裡 2024 年寫 937 萬、2025 年下半年寫 8,710 萬，市值圖憑空跳一個數量級。
    companyfacts 那側的偵測救不了這種公司 —— 多股別公司的無維度加權平均股數
    早就整個消失了（COKE 停在 2020 年），跨申報同期比對根本沒有樣本。

    **倍數只認公司自己申報的** `StockholdersEquityNoteStockSplitConversionRatio1`，
    不從數列去猜「這個跳點像幾倍」—— 真的增資、併購換股也會跳，猜錯就是把正確的
    歷史數字乘上一個假倍數。事實上掛的日期不能當界線（可口可樂裝瓶掛的是宣告日
    2025-03-04、生效在 5 月底）。

    ⚠️ 界線也**不能用「數列上哪裡跳了 10 倍」**。基準跟時間順序不一致：Strategy 的
    2022 年那格來自 2025 年的 10-K（已是分割後），2023 年那幾格卻來自 2024 年的
    10-Q（還是分割前）。照數列位置把「跳點以前」全部乘 10，會把 2022 年那格
    再乘一次 → 11.3 億股。
    判準改成同一期跨申報：同一個期末日被兩份申報寫成 10 倍關係，早的那份就是分割前、
    晚的那份就是分割後。沒有直接證據的申報再用申報日推（比某份已證實的分割後申報還
    晚 → 分割後）；連推都推不出來的，那些日期整個丟掉，不猜。
    """
    pre: set[str] = set()
    post: set[str] = set()
    hits: set[float] = set()
    for byf in raw.values():
        fs = sorted(byf)
        for i in range(len(fs)):
            for j in range(i + 1, len(fs)):
                a, b = byf[fs[i]], byf[fs[j]]
                if a <= 0 or b <= 0:
                    continue
                hit = next((s for s in splits if abs((b / a) / s - 1) < 0.03), None)
                if hit:
                    pre.add(fs[i])
                    post.add(fs[j])
                    hits.add(hit)
    if len(hits) != 1 or (pre & post):
        return set(), set(), 1.0  # 一個視窗內兩次分割、或證據自相矛盾 → 不動
    return pre, post, hits.pop()


def apply_basis(raw: dict[str, tuple[float, str]], basis_of, ratio: float) -> dict[str, float]:
    """
    `raw` 是 {日期: (值, 這個值出自哪一份申報)}，套上分割還原。

    跨申報證據只能把分割框在「某兩份申報之間」，中間那幾份的基準沒有直接證據
    （Strategy 2024-08 到 2025-08 之間有五份）。那幾份不能丟 —— 都是真的數字，
    丟掉就是憑空少掉一年半的市值。改用連續性判斷：季與季之間的真實變動不會到兩倍，
    而分割倍數至少兩倍，「乘」與「不乘」哪個接得上時間最近的已知期，一眼可分。
    """
    out = {d: v * m for d, (v, f) in raw.items() if (m := basis_of(f)) is not None}
    if not out or len(out) == len(raw):
        return dict(sorted(out.items()))
    for d, (v, _f) in sorted(raw.items()):
        if d in out:
            continue
        ref = min(out, key=lambda k: abs((date.fromisoformat(k) - date.fromisoformat(d)).days))
        best = min((v, v * ratio), key=lambda c: abs(c / out[ref] - 1))
        if 0.55 <= best / out[ref] <= 1.9:  # 連最近的一期都接不上就不猜
            out[d] = best
    return dict(sorted(out.items()))


def drop_stale(series: dict[str, float]) -> dict[str, float]:
    """
    同一個數值出現在兩個相隔很遠的期，而中間的期不是這個值 → 早的那個是套錯期間。

    AST SpaceMobile 的加權平均股數在 2022-03-31 寫 121,447,138，跟 2024-03-31
    一模一樣，中間 2022-12 到 2023-12 卻是 5,443 萬到 8,182 萬。留著會讓 2022 年
    第一季的市值比實際高一倍多。

    ⚠️ 條件裡「中間要有**差很多**的值」兩個字都不能省。真的多年沒增減的公司整列本來
    就都一樣（U-Haul 連續 14 期都是 196,077,880），不看中間會整家清空；而「差一點點
    就算數」也不行 —— 可口可樂裝瓶 2022–2024 一路 93,740,000，只有 2023-12 是
    93,737,000（差萬分之三），照樣會誤刪一整期。
    """
    ds = sorted(series)
    bad: set[str] = set()
    for i, d in enumerate(ds):
        later = [x for x in ds[i + 1:] if series[x] == series[d]]
        if not later:
            continue
        gap = (date.fromisoformat(later[-1]) - date.fromisoformat(d)).days
        between = [series[x] for x in ds[i + 1:] if x < later[-1]]
        if gap > 400 and any(abs(v / series[d] - 1) > 0.05 for v in between if series[d]):
            bad.add(d)
    return {d: v for d, v in series.items() if d not in bad}


def clean_classes(classes: dict) -> dict:
    """
    同一日的股別數字清一遍：丟掉 0，**同一個數值只留一份**。

    U-Haul 的投票權普通股在申報裡有三個名字 —— `AmercoCommonStockMember`、
    `CommonStockMember`、`CommonClassAMember`，全都是 19,607,788 股。2024-09-30
    那份同時出現兩個，照加變 2.16 億（實際 1.96 億）。`norm_class()` 救不了，
    那三個名字去掉結構字之後是 amerco／空／a，仍然不同。
    改用數值判同一性：大數字剛好完全相等的機率可以忽略，而且下面「各股別數值全部
    相同＝同一個總數重複掛」本來就是同一個推論。
    """
    out: dict[str, float] = {}
    seen: set[float] = set()
    for c, v in sorted(classes.items()):
        if not v or v in seen:
            continue
        seen.add(v)
        out[c] = v
    # 有的成員裝的是**小計**不是某一個股別：Figure 2025-09-30 除了 Class A
    # （1.749 億）與 Class B（3,789 萬），還有一個 `CommonClassAAndClassBMember`
    # 寫 2.127 億 —— 剛好是前兩者相加。照加變 4.25 億，市值整整多一倍。
    total = sum(out.values())
    for c, v in list(out.items()):
        if len(out) > 2 and abs(v / (total - v) - 1) < 0.005:
            del out[c]
    return out


def complete_only(by_date: dict[str, dict]) -> dict[str, dict]:
    """
    清乾淨之後，**少了「重要股別」的日期整個丟掉**。

    申報端常常某幾期只 tag 其中一個股別：U-Haul 有 9 期只寫了無投票權那類的
    1.76 億，少了投票權的 1,961 萬。照加會得到 1.76 億 —— 看起來完全正常的數字，
    跟正確的 1.96 億只差 10%，任何量級檢查都抓不到，市值就這樣長期少算一成。

    ⚠️ 判準不能是「股別數要等於最完整那一期」。多股別公司幾乎家家都有一個佔比
    千分之幾的零頭股別，時有時無：波克夏 A 股只佔 0.03%（少了它 13 期全滅、
    整列變成 143 萬股 A 股當量）、自由媒體 F1 的 B 系列 0.95%、Dutch Bros 的 C 股 0.9%。
    所以先算出「佔比 5% 以上的股別有幾個」，只要求那個數量齊全 —— 零頭缺不缺無所謂，
    缺了對市值的影響本來就在雜訊裡。
    """
    cleaned = {d: clean_classes(c) for d, c in by_date.items()}
    if not cleaned:
        return {}
    # 基準取**最新那一期**，不是股別最多那一期。股別會退場（Shift4 的 B/C 股歸零、
    # Ryan 早期多一個 CommonStock 成員），拿舊結構當標準會反過來把最近幾期全丟掉。
    ref = cleaned[sorted(cleaned)[-1]]
    total = sum(ref.values())
    need = sum(1 for v in ref.values() if total and v / total >= 0.05)
    return {d: c for d, c in cleaned.items() if len(c) >= need}


def upc_drop(classes: dict, ratios: dict, implied: float | None) -> str | None:
    """
    Up-C 的非經濟股別偵測。回傳要丟掉的股別，判不出來就 None（照原規則走）。

    `total_for` 裡「只留有申報 EPS 的股別」那條要「有股別標了 EPS、又有股別沒標」
    才成立。Up-C 公司多半**整份申報只報一個無維度的 EPS**（分子本來就只有 Class A
    那一份），於是 eps_classes 是空的、規則整條跳過 → 照加。實測 UWM 把 Ishbia 的
    12.6 億 Class D 加進去，總數 16.0 億，淨利÷EPS 卻只有 3.36 億，**市值高估 4.8 倍**；
    Dutch Bros 高 31%。

    判準是一個**兩邊都要成立的窄窗**：照加要差 25% 以上，拿掉那個股別後要落進 10% 以內。
    「比較近就換」與「誤差減半就換」都試過，兩個都會誤殺：

      Strategy   一直增發，期末 3.84 億 vs 隱含（加權平均）3.36 億差 14%，
                 拿掉 1,964 萬股 Class B 會「比較近」—— 但那是真的普通股。
                 照加只差 14% < 25%，這一關就擋掉了
      波克夏     隱含股數是 **A 股當量** 140 萬，照加 14.08 億差 1000 倍、
                 只留 A 股 48.8 萬又差 65%，兩邊都不合格 → 不動，
                 交給下面的轉換率規則算出正確的 21.6 億

    另外，有轉換率的公司（波克夏 1500:1）整條規則跳過 —— 各股別本來就不是同一個單位，
    拿相加的結果去比隱含股數沒有意義。

    ⚠️ 判斷**整家只做一次**，用最新那一期。逐期各判會出事：隱含股數只有最新一期，
    高成長公司三年前的股數本來就只有現在的三分之一，逐期比會把正常的歷史數字誤判成 Up-C。

    ⚠️ 對不上也**不留白**。隱含股數本身就可能是壞的：Shift4 的無維度 EPS 其實是
    Class C 的，自由媒體 F1 三個系列共用一個 EPS。拿一個可疑的錨去砍掉整家公司，
    代價比留著原本就對的合計大。
    """
    total_all = sum(classes.values())
    if not implied or len(classes) < 2 or not total_all:
        return None
    if any(c in ratios and ratios[c] not in (0, 1) for c in classes):
        return None

    def err(v: float) -> float:
        return abs(v - implied) / implied

    if err(total_all) <= 0.25:
        return None
    best_v, best_c = min(((total_all - v, c) for c, v in classes.items()), key=lambda x: err(x[0]))
    return best_c if err(best_v) < 0.10 else None


def total_for(entry: dict, ratios: dict, eps_classes: set, implied: float | None,
              drop: str | None = None) -> tuple[float, str] | None:
    """回傳 (股數, 依據說明)。算不出來就 None —— 寧可留白不猜。"""
    classes = entry.get("classes") or {}
    if drop:
        classes = {c: v for c, v in classes.items() if c != drop}
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
    splits: set[float] = set()
    eps_classes: set[str] = set()
    wavg_all: dict[str, dict[str, dict[str, float]]] = {}
    wsrc: dict[str, dict[str, str]] = {}
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
        splits |= got["splits"]
        eps_classes |= got["eps_classes"]
        for k, v in got["eps_by_period"].items():
            eps_p.setdefault(k, v)
        for k, v in got["ni_by_period"].items():
            ni_p.setdefault(k, v)
        # 加權平均也要「新的申報說了算」。`recent_filings` 是**申報日新到舊**，
        # 直接 update 會讓最舊那份贏 —— Strategy 2024-08 的 10:1 分割因此整列鋸齒狀：
        # 2023-03 是分割前的 987 萬、2023-09 卻是分割後的 9,700 萬，同一欄兩種基準。
        for kind, byd in got["wavg"].items():
            tgt = wavg_all.setdefault(kind, {})
            for d, byc in byd.items():
                tgt.setdefault(d, {})[fl["filed"]] = byc
                if fl["filed"] >= wsrc.setdefault(kind, {}).get(d, ""):
                    wsrc[kind][d] = fl["filed"]
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

    # 分割前後的基準判定。用加權平均那幾列當證據（同一期會被多份申報各寫一次），
    # 判出來的「哪幾份申報是分割前」再套用到封面股數 —— 封面股數每個日期只出現在
    # 一份申報裡，自己沒有跨申報樣本。
    totals = {
        end: {f: sum(clean_classes(byc).values()) for f, byc in byf.items()}
        for byd in wavg_all.values()
        for end, byf in byd.items()
    }
    pre_filings, post_filings, split_ratio = split_basis(totals, splits)
    post_min = min(post_filings, default="")
    pre_max = max(pre_filings, default="")

    def basis_of(filed: str) -> float | None:
        """該份申報要乘的倍數；判不出來回 None（那些日期整個丟掉，不猜）。"""
        if not pre_filings:
            return 1.0
        if filed in pre_filings or filed <= pre_max:
            return split_ratio
        if post_min and filed >= post_min:
            return 1.0
        return None

    if pre_filings:
        print(f"{ticker:8} 還原分割 {split_ratio:g}:1（{pre_max} 以前的申報為舊基準，{post_min} 起為新基準）")

    # 非經濟股別（Up-C 的 Class B/D）判一次就好，用最新那一期。逐期各判會拿高成長
    # 公司三年前的股數去比最新一期的隱含股數，一定誤判。
    keep_cls = complete_only({d: e.get("classes") or {} for d, e in per_date.items()})
    if len(keep_cls) < len(per_date):
        print(f"{ticker:8} 丟掉 {len(per_date) - len(keep_cls)} 個股別不齊的日期")
    per_date = {d: {**per_date[d], "classes": c} for d, c in keep_cls.items()}

    drop = None
    if per_date:
        drop = upc_drop(per_date[sorted(per_date)[-1]]["classes"], ratios, implied)
    if drop:
        print(f"{ticker:8} 判定 Up-C：{pretty(drop)} 不分享母公司損益，不計入")

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
        # 分割還原要在「0.5–2 倍量級篩選」之前，不然分割前的日期會被那關全部丟掉，
        # 明明公司自己申報了倍數、算得回來（COKE 2022–2025Q1 共 10 期）。
        raw_cand: dict[str, tuple[float, str]] = {}
        why: dict[str, str] = {}
        for d in sorted(per_date):
            got = total_for(per_date[d], ratios, eps_classes, implied, drop)
            if got:
                raw_cand[d] = (got[0], src[d])
                why[d] = got[1]
        cand = {d: (v, why[d]) for d, v in apply_basis(raw_cand, basis_of, split_ratio).items()}
        if cand:
            latest = cand[sorted(cand)[-1]][0]
            basis = cand[sorted(cand)[-1]][1]
            if drop:
                basis += f"，不計 {pretty(drop)}（Up-C 非經濟股別）"
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
        newest = {d: byf[wsrc[kind][d]] for d, byf in byd.items() if wsrc[kind].get(d) in byf}
        one: dict[str, tuple[float, str]] = {}
        for d, byc in sorted(complete_only(newest).items()):
            got = total_for({"classes": byc}, ratios, eps_classes, implied, drop)
            if got:
                one[d] = (got[0], wsrc[kind][d])
        fixed = drop_stale(apply_basis(one, basis_of, split_ratio)) if one else {}
        if fixed:
            wavg_out[kind] = fixed

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
