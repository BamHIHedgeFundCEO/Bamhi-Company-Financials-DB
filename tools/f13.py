#!/usr/bin/env python3
"""
13F 機構持股：離線批次，執行期零 SEC 請求。

為什麼一定要離線：財報、公司簡介、內部人買賣都是「以公司為索引」（給一個 CIK，
submissions.json 就列出那家的申報）；13F 相反，是「以基金為索引」——
每份 13F-HR 是某家機構列出自己持有的幾百檔。要回答「誰持有 NVDA」，
必須把全市場數千份持股表**反轉**建索引。與 `class_shares.py` 同一個模式。

資料源（兩個都實際打過確認存在）：

  持股表      https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets
              一季一包 ~99MB，內含 INFOTABLE.tsv（~396MB）
  CUSIP↔代號  https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data
              交割失敗檔，半個月一包 ~1.2MB，欄位有 CUSIP 與 SYMBOL

⚠️ FTD 檔只收錄「當期有交割失敗」的證券，所以**要取多期聯集**才夠用
（單一檔 13,025 個代號，BRK.A 就不在裡面）。仍缺的用 13F 自帶的
NAMEOFISSUER 做名稱比對補。

四個必須做對、做錯會產生假數字的地方：

1. **只收 13F-HR**。13F-NT 是「持股由他人代為申報」的通知，沒有持股表
2. **修正案要分兩種**。`AMENDMENTTYPE=RESTATEMENT` 是整份取代 → 只採最新那份；
   `NEW HOLDINGS` 是補充 → 要與原申報相加。混為一談會重複計算或漏算
3. **PUTCALL 非空的是選擇權部位**，不是股票；`SSHPRNAMTTYPE != SH` 是債券本金。
   兩者都不能計入股數
4. **一包 zip 是「申報日落在某區間」的申報**，不是「某一季的持股」。
   同一包裡有多個 PERIODOFREPORT（含遲交與修正的舊季），所以要按
   PERIODOFREPORT 分組，且要抓連續多包才湊得出可比較的兩季

用法：
  python tools/f13.py                    # 抓最近 3 包 + 建索引 + 產出 config/f13/
  python tools/f13.py --quarters 4
  python tools/f13.py --tickers AAPL,NVDA --dry-run   # 只印結果不寫檔
"""
import argparse
import csv
import io
import json
import os
import re
import sys
import time
import urllib.request
import zipfile
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "config", "f13")
COVERAGE = os.path.join(ROOT, "tools", "sweep_out", "coverage.jsonl")
CACHE = os.environ.get("BAMHI_13F_CACHE",
                       os.path.join(os.path.expanduser("~"), ".bamhi-13f-cache"))
UA = os.environ.get("SEC_USER_AGENT", "BamHI frank940702@gmail.com")

F13_PAGE = "https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets"
FTD_PAGE = "https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data"
BASE = "https://www.sec.gov"

MIN_INTERVAL = 0.15  # SEC 限速
_last = [0.0]


def fetch(url: str) -> bytes:
    wait = MIN_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    # 不要送 Accept-Encoding：urllib 不會自動解壓，HTML 會變成一包看不懂的位元組
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=900) as r:
        data = r.read()
    _last[0] = time.time()
    return data


def download(url: str, path: str) -> str:
    if os.path.exists(path) and os.path.getsize(path) > 1000:
        return path
    print(f"    下載 {url.rsplit('/', 1)[-1]} …", end="", flush=True)
    data = fetch(url)
    with open(path, "wb") as f:
        f.write(data)
    print(f" {len(data) / 1e6:.1f} MB")
    return path


def links(page_url: str, pattern: str) -> list:
    html = fetch(page_url).decode("utf-8", "replace")
    return [BASE + h for h in re.findall(rf'href="(/files/[^"]*{pattern}[^"]*\.zip)"', html)]


# Windows 的保留裝置名稱：`CON.json` 這種檔名在 Windows 上建不起來
# （母體裡真的有一檔代號叫 CON）。加底線避開，執行期的 funds.get.ts 用同一套規則
RESERVED = ({"CON", "PRN", "AUX", "NUL", "CLOCK$"}
            | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})


def safe_name(ticker: str) -> str:
    return f"{ticker}_" if ticker.upper() in RESERVED else ticker


# ── CUSIP ↔ 代號 ────────────────────────────────────────────
def build_cusip_map(n_files: int) -> dict:
    """回傳 {cusip9: ticker}。多期聯集 —— 單一 FTD 檔只涵蓋當期有交割失敗的證券"""
    urls = links(FTD_PAGE, "cnsfails")[:n_files]
    out = {}
    for u in urls:
        p = download(u, os.path.join(CACHE, u.rsplit("/", 1)[-1]))
        with zipfile.ZipFile(p) as z:
            name = z.namelist()[0]
            with z.open(name) as f:
                t = io.TextIOWrapper(f, encoding="latin-1")
                t.readline()
                for line in t:
                    p_ = line.split("|")
                    if len(p_) < 4:
                        continue
                    cusip, sym = p_[1].strip(), p_[2].strip()
                    # 同一個 CUSIP 只會對到一個代號；後見的不覆蓋先見的（先見＝較新）
                    if cusip and sym and cusip not in out:
                        out[cusip] = sym
    return out


# ── 13F 一包 ────────────────────────────────────────────────
def read_tsv(z, name):
    with z.open(name) as f:
        t = io.TextIOWrapper(f, encoding="latin-1", newline="")
        r = csv.reader(t, delimiter="\t")
        head = next(r)
        idx = {c: i for i, c in enumerate(head)}
        for row in r:
            if len(row) >= len(head):
                yield idx, row


def load_quarter(path: str, wanted_cusips: set, names: dict) -> dict:
    """
    回傳 {period: {cik: {cusip: (shares, value)}}}，修正案已解析完；
    `names` 就地補上 {cik: 最新的機構名稱}。

    ⚠️ **一定要以 CIK 當 key，不能把名稱放進 key。** 申報人自己填的名稱連大小寫
    都會變（PNC 這一季從 `PNC Financial Services Group, Inc.` 變成全大寫），
    名稱進 key 會讓同一家同時算成一筆清倉加一筆建倉 —— 憑空生出兩筆假交易。
    """
    subs = {}          # accession → (cik, period, type)
    with zipfile.ZipFile(path) as z:
        for idx, row in read_tsv(z, "SUBMISSION.tsv"):
            typ = row[idx["SUBMISSIONTYPE"]]
            if not typ.startswith("13F-HR"):
                continue          # 13F-NT 是「由他人代為申報」的通知，沒有持股表
            subs[row[idx["ACCESSION_NUMBER"]]] = (
                row[idx["CIK"]], row[idx["PERIODOFREPORT"]], typ,
                row[idx["FILING_DATE"]],
            )
        cover = {}
        for idx, row in read_tsv(z, "COVERPAGE.tsv"):
            acc = row[idx["ACCESSION_NUMBER"]]
            if acc in subs:
                cover[acc] = (row[idx["FILINGMANAGER_NAME"]].strip(),
                              row[idx["ISAMENDMENT"]].strip().upper(),
                              row[idx["AMENDMENTTYPE"]].strip().upper())

        rows = defaultdict(dict)   # accession → {cusip: (shares, value)}
        n = 0
        for idx, row in read_tsv(z, "INFOTABLE.tsv"):
            n += 1
            cusip = row[idx["CUSIP"]].strip()
            if cusip not in wanted_cusips:
                continue
            acc = row[idx["ACCESSION_NUMBER"]]
            if acc not in subs:
                continue
            if row[idx["PUTCALL"]].strip():
                continue          # 選擇權部位不是持股
            if row[idx["SSHPRNAMTTYPE"]].strip().upper() != "SH":
                continue          # PRN = 債券本金
            try:
                sh = float(row[idx["SSHPRNAMT"]] or 0)
                val = float(row[idx["VALUE"]] or 0)
            except ValueError:
                continue
            prev = rows[acc].get(cusip)
            rows[acc][cusip] = (sh + prev[0], val + prev[1]) if prev else (sh, val)
        print(f"      INFOTABLE {n:,} 列，命中 {sum(len(v) for v in rows.values()):,} 列")

    # 修正案解析：RESTATEMENT 整份取代（取最新一份）；NEW HOLDINGS 與原申報相加
    by_filer = defaultdict(list)
    for acc, (cik, period, typ, filed) in subs.items():
        name, isamd, amdtype = cover.get(acc, ("", "", ""))
        by_filer[(period, cik)].append((filed, acc, name, isamd == "Y", amdtype))

    out = defaultdict(dict)
    for (period, cik), lst in by_filer.items():
        lst.sort()                         # 依申報日
        restate = [x for x in lst if x[3] and x[4] == "RESTATEMENT"]
        use = [restate[-1]] if restate else [x for x in lst if not x[3] or x[4] != "RESTATEMENT"]
        holdings = {}
        name = ""
        for _, acc, mgr, _, _ in use:
            name = mgr or name
            for cusip, (sh, val) in rows.get(acc, {}).items():
                p = holdings.get(cusip)
                holdings[cusip] = (sh + p[0], val + p[1]) if p else (sh, val)
        if holdings:
            out[period][cik] = holdings
            if name:
                names[cik] = name
    return out


# ── 比較兩季 ────────────────────────────────────────────────
def top(items, key, n=10):
    return sorted(items, key=key, reverse=True)[:n]


SUFFIX = re.compile(r"(LLC|L\.L\.C|INC|LP|L\.P|LTD|CO|CORP|CORPORATION|PLC|AB|AG|SA|NV|"
                    r"GROUP|HOLDINGS|HOLDING|COMPANY|TRUST|BANK|MANAGEMENT|ADVISORS|ADVISERS|"
                    r"CAPITAL|ASSET|WEALTH|INVESTMENTS?|PARTNERS|FUND|FINANCIAL|SERVICES)")
# 這些字當第一個詞完全不具辨識度，配對會亂牽
GENERIC = {"FIRST", "NATIONAL", "AMERICAN", "GLOBAL", "UNITED", "NORTH", "SOUTH", "NEW",
           "PACIFIC", "ATLANTIC", "CENTRAL", "PUBLIC", "PRIVATE", "INDEPENDENT", "SUMMIT",
           "PINNACLE", "HERITAGE", "LEGACY", "CORNERSTONE", "PARK", "MAIN", "STATE"}


def stem(name: str) -> str:
    """機構名稱的辨識詞：去標點、去法人後綴，取第一個有意義的詞"""
    n = SUFFIX.sub(" ", re.sub(r"[^A-Z ]", " ", name.upper()))
    for tok in n.split():
        if len(tok) >= 4 and tok not in GENERIC:
            return tok
    return ""


def pair_reorgs(new: list, exited: list) -> list:
    """
    申報主體重組：同一集團換 CIK 申報，會同時生出一筆巨額建倉與一筆巨額清倉。

    2026 Q1 的 Vanguard 就是這樣 —— `VANGUARD GROUP INC`（CIK 0000102909）
    清掉 14.3 億股 AAPL，同時 `VANGUARD CAPITAL MANAGEMENT LLC`（新 CIK）
    建倉 9.5 億股。那不是有人買賣了 14 億股蘋果，是同一家把申報拆成好幾個主體。
    照原樣呈現的話「本季前 10 大建倉機構」整張表都會是這種東西。

    只用來把配對到的項目移出 top 榜、另外列成「申報主體重組」，
    **家數統計不動**（配對是啟發式的，不該讓它去改真正的數字）。
    """
    out = []
    by_stem: dict = {}
    for x in exited:
        st = stem(x["name"])
        # 同一個辨識詞留最大的那一筆（拆分的來源一定是本來最大的那個主體）
        if st and (st not in by_stem or x["shares"] > by_stem[st]["shares"]):
            by_stem[st] = x
    for n in new:
        st = stem(n["name"])
        # **多對一**：一個主體可以拆成好幾個。Vanguard 2026Q1 就是 1 進 6 出，
        # 只配一對的話另外五個照樣霸佔「前 10 大建倉」
        if st and st in by_stem:
            out.append({"into": n, "outof": by_stem[st]})
    return out


def compare(cur: dict, prev: dict, cusip: str, names: dict) -> dict:
    """cur/prev: {cik: {cusip: (sh, val)}} → 這一檔的持有人變化"""
    c = {k: v[cusip] for k, v in cur.items() if cusip in v}
    p = {k: v[cusip] for k, v in prev.items() if cusip in v} if prev else {}
    nm = lambda k: names.get(k, k)
    inc, dec, new, exited, flat = [], [], [], [], 0
    for k, (sh, val) in c.items():
        if k not in p:
            new.append({"cik": k, "name": nm(k), "shares": sh, "value": val})
        else:
            d = sh - p[k][0]
            if d > 0:
                inc.append({"cik": k, "name": nm(k), "shares": sh, "delta": d, "value": val})
            elif d < 0:
                dec.append({"cik": k, "name": nm(k), "shares": sh, "delta": d, "value": val})
            else:
                flat += 1
    for k, (sh, val) in p.items():
        if k not in c:
            exited.append({"cik": k, "name": nm(k), "shares": sh, "value": val})

    reorg = pair_reorgs(new, exited)
    moved_in = {id(r["into"]) for r in reorg}
    moved_out = {id(r["outof"]) for r in reorg}
    new_clean = [x for x in new if id(x) not in moved_in]
    ex_clean = [x for x in exited if id(x) not in moved_out]
    return {
        "holders": len(c),
        "holdersPrev": len(p),
        "totalShares": sum(v[0] for v in c.values()),
        "totalValue": sum(v[1] for v in c.values()),
        "increased": len(inc), "decreased": len(dec),
        "opened": len(new), "closed": len(exited), "unchanged": flat,
        "reorgs": len(reorg),
        "topOpened": top(new_clean, lambda x: x["value"]),
        "topClosed": top(ex_clean, lambda x: x["value"]),
        "topIncreased": top(inc, lambda x: x["delta"]),
        "topDecreased": top(dec, lambda x: -x["delta"]),
        "topReorgs": sorted(reorg, key=lambda r: -r["outof"]["shares"])[:5],
    }


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", type=int, default=3, help="抓最近幾包 13F 資料集")
    ap.add_argument("--ftd", type=int, default=8, help="抓最近幾包交割失敗檔建 CUSIP 對照")
    ap.add_argument("--tickers", help="只處理這些代號（逗號分隔）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    os.makedirs(CACHE, exist_ok=True)

    want = None
    if args.tickers:
        want = {t.strip().upper() for t in args.tickers.split(",")}
    universe = []
    with io.open(COVERAGE, encoding="utf-8") as f:
        for line in f:
            d = json.loads(line)
            if want is None or d["ticker"] in want:
                universe.append(d["ticker"])
    print(f"母體 {len(universe)} 檔")

    print("── CUSIP ↔ 代號")
    cmap = build_cusip_map(args.ftd)
    uni = set(universe)
    by_sym = {}
    for cusip, sym in cmap.items():
        by_sym.setdefault(sym, cusip)
    cusip_of = {}
    for t in universe:
        if t in by_sym:
            cusip_of[t] = by_sym[t]
    # 多股別代號在交割失敗檔裡沒有點（BRK.B 寫成 BRKB、BF.A 寫成 BFA）。
    # 只在「去點後的寫法沒有被母體裡另一檔佔用」時才採用，避免 BF.C → BFC 這種認錯
    for t in universe:
        if t in cusip_of or "." not in t:
            continue
        flat = t.replace(".", "")
        if flat in by_sym and flat not in uni:
            cusip_of[t] = by_sym[flat]
    print(f"    對照表 {len(cmap):,} 個代號，母體命中 {len(cusip_of)}／{len(universe)}")
    missing = sorted(uni - set(cusip_of))
    if missing:
        print(f"    對不到 CUSIP 的 {len(missing)} 檔（這些不會有 13F 資料）："
              + " ".join(missing[:20]) + (" …" if len(missing) > 20 else ""))
    wanted_cusips = set(cusip_of.values())
    ticker_of = {v: k for k, v in cusip_of.items()}

    print("── 13F 資料集")
    urls = links(F13_PAGE, "form13f")[:args.quarters]
    periods = defaultdict(dict)
    names: dict = {}
    for u in urls:
        p = download(u, os.path.join(CACHE, u.rsplit("/", 1)[-1]))
        print(f"    解析 {os.path.basename(p)}")
        for period, filers in load_quarter(p, wanted_cusips, names).items():
            # 同一季可能散在兩包（遲交），合併時後見的不覆蓋先見的（先見＝較新那包）
            for k, v in filers.items():
                periods[period].setdefault(k, v)

    order = sorted(periods, key=lambda d: time.strptime(d, "%d-%b-%Y"), reverse=True)
    print(f"    取得期別：{', '.join(order)}")
    if len(order) < 2:
        print("    只有一季，算不出增減 —— 加大 --quarters")
        return
    cur_p, prev_p = order[0], order[1]
    # 最新那一期常常只有少數幾家早交，拿它當「本季」會嚴重低估家數
    if len(periods[cur_p]) < len(periods[prev_p]) * 0.5:
        print(f"    {cur_p} 只有 {len(periods[cur_p])} 家申報（{prev_p} 有 "
              f"{len(periods[prev_p])} 家）→ 尚未申報完，改用 {prev_p} 當本季")
        order = order[1:]
        cur_p, prev_p = order[0], order[1]
    print(f"    本季 {cur_p}（{len(periods[cur_p])} 家申報）"
          f" vs 上季 {prev_p}（{len(periods[prev_p])} 家）")

    if not args.dry_run:
        os.makedirs(OUT_DIR, exist_ok=True)
    written = 0
    for ticker, cusip in sorted(cusip_of.items()):
        r = compare(periods[cur_p], periods[prev_p], cusip, names)
        if not r["holders"] and not r["holdersPrev"]:
            continue
        doc = {"ticker": ticker, "cusip": cusip,
               "period": cur_p, "periodPrev": prev_p, **r}
        if args.dry_run:
            print(f"\n{ticker} {cusip} 持有人 {r['holders']}（上季 {r['holdersPrev']}）"
                  f" 增持 {r['increased']} 減持 {r['decreased']}"
                  f" 建倉 {r['opened']} 清倉 {r['closed']}")
            for x in r["topReorgs"][:2]:
                print(f"    重組         {x['outof']['name'][:30]:32} → {x['into']['name'][:30]:32}"
                      f" {x['outof']['shares']:>14,.0f}")
            for x in r["topOpened"][:3]:
                print(f"    建倉         {x['name'][:38]:40} {x['shares']:>14,.0f}")
            for x in r["topIncreased"][:3]:
                print(f"    增持         {x['name'][:38]:40} {x['delta']:>+14,.0f}")
        else:
            with io.open(os.path.join(OUT_DIR, f"{safe_name(ticker)}.json"), "w", encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        written += 1

    if not args.dry_run:
        with io.open(os.path.join(OUT_DIR, "_index.json"), "w", encoding="utf-8") as f:
            json.dump({
                "generated": time.strftime("%Y-%m-%d"),
                "period": cur_p, "periodPrev": prev_p,
                "filers": len(periods[cur_p]),
                "tickers": written,
                "source": "SEC Form 13F Data Sets + CNS Fails-to-Deliver（CUSIP 對照）",
            }, f, ensure_ascii=False, indent=1)
        print(f"\n寫出 {written} 檔 → config/f13/")
        print(f"總大小 {sum(os.path.getsize(os.path.join(OUT_DIR, x)) for x in os.listdir(OUT_DIR)) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
