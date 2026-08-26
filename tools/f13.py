#!/usr/bin/env python3
"""
13F 機構持股：離線批次，執行期零 SEC 請求。

為什麼一定要離線：財報、公司簡介、內部人買賣都是「以公司為索引」（給一個 CIK，
submissions.json 就列出那家的申報）；13F 相反，是「以基金為索引」——
每份 13F-HR 是某家機構列出自己持有的幾百檔。要回答「誰持有 NVDA」，
必須把全市場數千份持股表**反轉**建索引。與 `class_shares.py` 同一個模式。

資料源（三個都實際打過確認存在）：

  持股表(批次)  https://www.sec.gov/data-research/sec-markets-data/form-13f-data-sets
                一包 ~99MB，內含 INFOTABLE.tsv（~396MB）
  持股表(即時)  https://www.sec.gov/Archives/edgar/full-index/{年}/QTR{季}/form.idx
                → 每份申報的完整送件檔（.txt，內含 primary_doc 與 informationTable）
  CUSIP↔代號    https://www.sec.gov/data-research/sec-markets-data/fails-deliver-data
                交割失敗檔，半個月一包 ~1.2MB，欄位有 CUSIP 與 SYMBOL

⚠️ FTD 檔只收錄「當期有交割失敗」的證券，所以**要取多期聯集**才夠用
（單一檔 13,025 個代號，BRK.A 就不在裡面）。仍缺的用 13F 自帶的
NAMEOFISSUER 做名稱比對補。

## 為什麼需要 --live（批次資料集會落後整整一季）

SEC 的 13F Data Sets 不是一季一包，是**滾動三個月的申報視窗**：
`01mar2026-31may2026` ＝「申報日落在 3/1–5/31 的申報」。2026Q2（期別 30-JUN-2026）
的截止日是 8/14，落在下一包 `01jun2026-31aug2026` 裡，而那一包要等 9 月才發布。
也就是說每年有四段各約一個月的期間，只靠資料集的話網站會停在**上一季**，
而所有其他網站都已經在顯示新一季 —— 實例：TCI Fund Management 在 2026Q2
建倉 VMC 2,447,004 股（$721.9M），但 2026Q1 完全沒有這一檔。

--live 改走 EDGAR 申報索引：一季約 9,500 份 13F，gzip 後合計僅 ~60MB，
限速 0.15 秒序列約 25 分鐘，解析結果永久快取（已申報不可變）。
順帶把「遲交的上一期申報」也一起收進來，補掉資料集視窗被切掉的那一截。

## 五個必須做對、做錯會產生假數字的地方

1. **只收 13F-HR**。13F-NT 是「持股由他人代為申報」的通知，沒有持股表。
   但**要記下誰交了 NT** —— 見第 5 點
2. **修正案要分兩種**。`AMENDMENTTYPE=RESTATEMENT` 是整份取代 → 只採最新那份；
   `NEW HOLDINGS` 是補充 → 要與原申報相加。混為一談會重複計算或漏算。
   ⚠️ 解析**一定要等所有來源都收齊之後才做一次**，不能一包一包各自解析再合併：
   原申報與它的補充申報常常分在兩包（實測 43 組），逐包解析的話那一包只看得到
   補充申報、把它當成整份持股，合併時再被 setdefault 定住 → 那家的持股憑空少一大截
3. **PUTCALL 非空的是選擇權部位**，不是股票；`SSHPRNAMTTYPE != SH` 是債券本金。
   兩者都不能計入股數
4. **持有人一律以 CIK 為 key**。申報人自己填的名稱連大小寫都會變（PNC 這一季
   改成全大寫），名稱進 key 會憑空生出一筆清倉加一筆建倉
5. **「本期沒有申報」不是清倉**。一家機構這一期還沒交（遲交）或改交 13F-NT，
   原始資料上就是「上期有、本期無」，照算的話它會變成一筆清倉 —— 但沒有人賣過。
   實測 VMC 的 147 筆清倉裡有 19 筆（13%）是這樣來的，107 筆建倉裡有 22 筆（21%）。
   判準是硬事實不是猜測（那家這一期到底有沒有交過任何一份 13F），所以可以直接
   從建倉／清倉裡拿掉、另列「本期尚未申報」——這點與重組配對不同，重組是靠名稱
   推測的，只移出排行榜、不動家數

用法：
  python tools/f13.py                    # 只用批次資料集（可能落後一季）
  python tools/f13.py --live             # 加抓 EDGAR 最新一季（推薦，約 +25 分鐘）
  python tools/f13.py --live 2 --quarters 3
  python tools/f13.py --tickers AAPL,NVDA --dry-run
"""
import argparse
import csv
import datetime
import gzip
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


def fetch(url: str, gz: bool = False) -> bytes:
    wait = MIN_INTERVAL - (time.time() - _last[0])
    if wait > 0:
        time.sleep(wait)
    # 抓 HTML／索引時不要送 Accept-Encoding：urllib 不會自動解壓，
    # 回來的會是一包看不懂的位元組。抓送件檔時要送 —— 13F 的 XML 壓縮比約 10:1
    h = {"User-Agent": UA}
    if gz:
        h["Accept-Encoding"] = "gzip"
    req = urllib.request.Request(url, headers=h)
    with urllib.request.urlopen(req, timeout=900) as r:
        data = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            data = gzip.decompress(data)
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


# ── 收件匣：所有來源都寫進同一組結構，最後才一次解析修正案 ────
class Inbox:
    """
    sub[acc]   = {cik, period, filed, amd(bool), amdtype, name}
    rows[acc]  = {cusip: [shares, value]}      只留母體內的 cusip
    filed[per] = {cik, ...}                    這一期交過**任何一份** 13F 的機構
    """

    def __init__(self):
        self.sub = {}
        self.rows = defaultdict(dict)
        self.nt = defaultdict(set)       # 13F-NT：有交，但沒有持股表

    def add_row(self, acc, cusip, sh, val):
        d = self.rows[acc]
        p = d.get(cusip)
        d[cusip] = [sh + p[0], val + p[1]] if p else [sh, val]

    def drop(self, acc):
        self.sub.pop(acc, None)
        self.rows.pop(acc, None)

    def filed_map(self) -> dict:
        """{period: {cik}} —— 交過任何一份 13F 的機構。
        **丟掉的殘缺申報不算交過**（見 is_stub），所以這裡是從存活的申報回推的"""
        out = defaultdict(set)
        for p, ciks in self.nt.items():
            out[p] |= ciks
        for s in self.sub.values():
            out[s["period"]].add(s["cik"])
        return out


def is_stub(conf: bool, entry_total: int, nrows: int) -> bool:
    """
    殘缺申報：宣告「保密略去」而且揭露的列數遠少於自己申報的總數。

    Norges Bank 每年 Q1／Q3 都這樣 —— 交一份只有 1 列的 13F-HR，
    自報 `TABLEENTRYTOTAL=1507`、`ISCONFIDENTIALOMITTED=Y`，滿一年後才補交
    完整的修正案。照原樣算的話它會在 Q1 變成清倉、Q2 又變成建倉，
    2026Q2 光是 AAPL 就是 1.9 億股的假建倉、NVDA 3.3 億股 —— 但沒有人買賣過。

    門檻不是憑感覺：2026Q1 那一包 9,846 份申報裡宣告保密的只有 99 份，
    其中 86 份揭露 ≥95%（只藏一兩檔，無害）、2 份揭露 <5%、11 份自報總數為 0。
    **揭露 ≥10 列而不到自報一半的一份都沒有**，所以 50% 這條線落在空白地帶，
    怎麼挪都不會改變結果。真正被判定殘缺的都只揭露 1 列。

    自報總數不能單獨當判準：填錯的很多（Prospect Financial 自報 108,308 實際 63、
    Thurston 自報 214,784 實際 18,176），而 JPMorgan 原申報只有 378 列是送件出錯，
    他們自己補了一份 RESTATEMENT 全表 33,063 列，修正案機制已經處理掉了。
    """
    if not conf:
        return False
    if entry_total:
        return nrows < entry_total * 0.5
    return nrows <= 2


# ── 來源一：批次資料集 ───────────────────────────────────────
def read_tsv(z, name):
    with z.open(name) as f:
        t = io.TextIOWrapper(f, encoding="latin-1", newline="")
        r = csv.reader(t, delimiter="\t")
        head = next(r)
        idx = {c: i for i, c in enumerate(head)}
        for row in r:
            if len(row) >= len(head):
                yield idx, row


def add_pack(path: str, wanted: set, box: Inbox) -> None:
    with zipfile.ZipFile(path) as z:
        # 先讀摘要頁 —— 判斷一份申報是不是殘缺（保密略去）要靠它，
        # 而且要在收持股之前就知道，才不會白收一份不能用的
        summ = {}
        for idx, row in read_tsv(z, "SUMMARYPAGE.tsv"):
            try:
                tot = int(row[idx["TABLEENTRYTOTAL"]] or 0)
            except ValueError:
                tot = 0
            summ[row[idx["ACCESSION_NUMBER"]]] = (
                tot, row[idx["ISCONFIDENTIALOMITTED"]].strip().upper() == "Y")

        nrows = defaultdict(int)
        for idx, row in read_tsv(z, "INFOTABLE.tsv"):
            nrows[row[idx["ACCESSION_NUMBER"]]] += 1

        new_acc, stubs = set(), []
        for idx, row in read_tsv(z, "SUBMISSION.tsv"):
            typ = row[idx["SUBMISSIONTYPE"]]
            if not typ.startswith("13F"):
                continue
            acc, cik = row[idx["ACCESSION_NUMBER"]], row[idx["CIK"]]
            period = row[idx["PERIODOFREPORT"]]
            if not typ.startswith("13F-HR"):
                box.nt[period].add(cik)   # NT 沒有持股表，但確實交了
                continue
            if acc in box.sub:
                continue          # 同一份申報出現在兩個來源，只收一次（否則持股加倍）
            tot, conf = summ.get(acc, (0, False))
            if is_stub(conf, tot, nrows.get(acc, 0)):
                stubs.append(acc)
                continue          # 殘缺申報：整份丟掉，連「他有交」都不算
            new_acc.add(acc)
            box.sub[acc] = {"cik": cik, "period": period,
                            "filed": row[idx["FILING_DATE"]], "amd": False,
                            "amdtype": "", "name": "", "conf": conf,
                            "entryTotal": tot, "nrows": nrows.get(acc, 0)}
        if stubs:
            print(f"      丟掉 {len(stubs)} 份殘缺申報（宣告保密略去、揭露遠少於自報總數）")
        for idx, row in read_tsv(z, "COVERPAGE.tsv"):
            acc = row[idx["ACCESSION_NUMBER"]]
            if acc in new_acc:
                box.sub[acc]["name"] = row[idx["FILINGMANAGER_NAME"]].strip()
                box.sub[acc]["amd"] = row[idx["ISAMENDMENT"]].strip().upper() == "Y"
                box.sub[acc]["amdtype"] = row[idx["AMENDMENTTYPE"]].strip().upper()
        for idx, row in read_tsv(z, "INFOTABLE.tsv"):
            cusip = row[idx["CUSIP"]].strip().upper()
            if cusip not in wanted:
                continue
            acc = row[idx["ACCESSION_NUMBER"]]
            if acc not in new_acc:
                continue
            if row[idx["PUTCALL"]].strip():
                continue          # 選擇權部位不是持股
            if row[idx["SSHPRNAMTTYPE"]].strip().upper() != "SH":
                continue          # PRN = 債券本金
            try:
                box.add_row(acc, cusip, float(row[idx["SSHPRNAMT"]] or 0),
                            float(row[idx["VALUE"]] or 0))
            except ValueError:
                continue
        print(f"      INFOTABLE {sum(nrows.values()):,} 列，新增 {len(new_acc):,} 份申報")


# ── 來源二：EDGAR 即時索引 ──────────────────────────────────
RE_TYPE = re.compile(r"^CONFORMED SUBMISSION TYPE:\s*(\S+)", re.M)
RE_PER = re.compile(r"^CONFORMED PERIOD OF REPORT:\s*(\d{8})", re.M)
RE_FILED = re.compile(r"^FILED AS OF DATE:\s*(\d{8})", re.M)
RE_CIK = re.compile(r"^\s*CENTRAL INDEX KEY:\s*(\d+)", re.M)
RE_AMD = re.compile(r"<(?:\w+:)?isAmendment>\s*(?:true|1)\s*<", re.I)
RE_AMDT = re.compile(r"<(?:\w+:)?amendmentType>\s*([^<]*)<", re.I)
RE_MGR = re.compile(r"<(?:\w+:)?filingManager>.*?<(?:\w+:)?name>\s*([^<]*)<", re.I | re.S)
RE_INFO = re.compile(r"<(?:\w+:)?infoTable[ >](.*?)</(?:\w+:)?infoTable>", re.I | re.S)
RE_CONF = re.compile(r"<(?:\w+:)?isConfidentialOmitted>\s*(?:true|1)\s*<", re.I)
RE_ENTRY = re.compile(r"<(?:\w+:)?tableEntryTotal>\s*(\d+)\s*<", re.I)


def _tag(block: str, name: str) -> str:
    m = re.search(rf"<(?:\w+:)?{name}>\s*([^<]*)<", block, re.I)
    return m.group(1).strip() if m else ""


def parse_live(txt: str) -> dict:
    """一份完整送件檔 → 與批次資料集同構的記錄。回傳 None ＝不是我們要的東西"""
    m = RE_TYPE.search(txt)
    if not m or not m.group(1).startswith("13F"):
        return None
    typ = m.group(1)
    per, fil, cik = RE_PER.search(txt), RE_FILED.search(txt), RE_CIK.search(txt)
    if not (per and cik):
        return None
    d = datetime.datetime.strptime(per.group(1), "%Y%m%d")
    amdt = RE_AMDT.search(txt)
    mgr = RE_MGR.search(txt)
    rec = {
        "type": typ,
        "cik": cik.group(1).zfill(10),
        "period": d.strftime("%d-%b-%Y").upper(),
        "filed": (datetime.datetime.strptime(fil.group(1), "%Y%m%d")
                  .strftime("%d-%b-%Y").upper() if fil else ""),
        "amd": bool(RE_AMD.search(txt)),
        "amdtype": amdt.group(1).strip().upper() if amdt else "",
        "name": mgr.group(1).strip() if mgr else "",
        "conf": bool(RE_CONF.search(txt)),
        "entryTotal": int(RE_ENTRY.search(txt).group(1)) if RE_ENTRY.search(txt) else 0,
        "rows": [],
    }
    if not typ.startswith("13F-HR"):
        return rec                      # NT：只要知道「他有交」
    for b in RE_INFO.finditer(txt):
        blk = b.group(1)
        if _tag(blk, "putCall"):
            continue
        if _tag(blk, "sshPrnamtType").upper() not in ("SH", ""):
            continue
        cusip = _tag(blk, "cusip").strip().upper()
        if len(cusip) != 9:
            continue
        try:
            rec["rows"].append([cusip, float(_tag(blk, "sshPrnamt") or 0),
                                float(_tag(blk, "value") or 0)])
        except ValueError:
            continue
    return rec


def live_quarter(year: int, qtr: int, box: Inbox, wanted: set) -> int:
    """
    抓 EDGAR 某一季**所有** 13F 送件檔。解析結果存成一支 gzip JSONL，
    已申報不可變 → 永久快取，重跑只補新的那幾份。
    """
    cache = os.path.join(CACHE, f"live_{year}q{qtr}.jsonl.gz")
    def take(r):
        """一份申報 → 收件匣。**讀進來就先濾掉母體外的持股、整筆丟掉**：
        一季 380 萬列全留在記憶體會吃掉 1GB 以上"""
        acc = r["acc"]
        if not r["type"].startswith("13F-HR"):
            box.nt[r["period"]].add(r["cik"])
            return
        if acc in box.sub:
            return
        # `conf` 不在舊版快取裡 → 留 None，之後由 check_stubs() 補查
        conf = r.get("conf")
        n = len(r["rows"])
        if conf is not None and is_stub(conf, r.get("entryTotal", 0), n):
            return                       # 殘缺申報：整份丟掉，連「他有交」都不算
        box.sub[acc] = {"cik": r["cik"], "period": r["period"], "filed": r["filed"],
                        "amd": r["amd"], "amdtype": r["amdtype"], "name": r["name"],
                        "conf": conf, "entryTotal": r.get("entryTotal", 0), "nrows": n}
        for cusip, shv, val in r["rows"]:
            if cusip in wanted:
                box.add_row(acc, cusip, shv, val)

    done = set()
    if os.path.exists(cache):
        # 中途被中斷的話，最後一個 gzip member 會缺結束標記 → 讀到那裡會丟 EOFError。
        # 不能讓它把整份快取廢掉（前面幾千份是好的），讀到哪算哪，缺的下次補
        try:
            with gzip.open(cache, "rt", encoding="utf-8") as f:
                for line in f:
                    try:
                        r = json.loads(line)
                    except ValueError:
                        continue      # 上次寫到一半的那一行
                    if r["acc"] not in done:
                        done.add(r["acc"])
                        take(r)
        except EOFError:
            print(f"    （{os.path.basename(cache)} 上次未寫完，已讀回 {len(done):,} 份）")
    idx_url = f"{BASE}/Archives/edgar/full-index/{year}/QTR{qtr}/form.idx"
    try:
        idx = fetch(idx_url).decode("latin-1")
    except Exception as e:
        print(f"    {year}Q{qtr} 索引讀不到（{e}）—— 這一季可能還沒開始")
        return 0
    todo = []
    for line in idx.splitlines():
        if not line.startswith("13F-"):
            continue
        path = line.split()[-1]
        acc = path.rsplit("/", 1)[-1][:-4]
        if acc not in done:
            todo.append((acc, path))
    print(f"    {year}Q{qtr}：索引 {len(done) + len(todo):,} 份 13F，"
          f"快取已有 {len(done):,}，要抓 {len(todo):,}")
    if todo:
        t0 = time.time()
        with gzip.open(cache, "at", encoding="utf-8") as out:
            for i, (acc, path) in enumerate(todo, 1):
                try:
                    txt = fetch(f"{BASE}/Archives/{path}", gz=True).decode("latin-1")
                    rec = parse_live(txt)
                except Exception as e:
                    print(f"      {acc} 失敗：{e}")
                    continue
                if not rec:
                    continue
                rec["acc"] = acc
                done.add(acc)
                take(rec)
                out.write(json.dumps(rec, ensure_ascii=False, separators=(",", ":")) + "\n")
                if i % 500 == 0:
                    el = time.time() - t0
                    print(f"      {i:,}/{len(todo):,}  已耗 {el / 60:.1f} 分，"
                          f"預估剩 {(el / i * (len(todo) - i)) / 60:.1f} 分", flush=True)
    return len(done)


def check_stubs(box: Inbox) -> None:
    """
    批次資料集自帶 SUMMARYPAGE，一眼就知道哪份是殘缺申報；EDGAR 的送件檔要自己解，
    而**舊版快取解的時候還沒有這個欄位**。與其為了兩個旗標重抓 11,454 份（90 分鐘），
    只補查「揭露 ≤5 檔」的那幾百份 —— 實測所有被判定殘缺的申報都只揭露 1 列，
    5 這條線離它們很遠，而 primary_doc.xml 只有 2KB。

    查到的旗標存 `stub_flags.json`，之後不再重查。
    """
    todo = [a for a, s in box.sub.items()
            if s.get("conf") is None and s.get("nrows", 99) <= 5]
    if not todo:
        return
    path = os.path.join(CACHE, "stub_flags.json")
    flags = {}
    if os.path.exists(path):
        with io.open(path, encoding="utf-8") as f:
            flags = json.load(f)
    need = [a for a in todo if a not in flags]
    print(f"    補查保密旗標：{len(todo):,} 份候選，快取已有 {len(todo) - len(need):,}，"
          f"要抓 {len(need):,}")
    for i, acc in enumerate(need, 1):
        cik = int(box.sub[acc]["cik"])
        url = f"{BASE}/Archives/edgar/data/{cik}/{acc.replace('-', '')}/primary_doc.xml"
        try:
            x = fetch(url, gz=True).decode("utf-8", "replace")
            m = RE_ENTRY.search(x)
            flags[acc] = [bool(RE_CONF.search(x)), int(m.group(1)) if m else 0]
        except Exception:
            flags[acc] = [False, 0]      # 讀不到就當正常件，不憑空丟掉別人的申報
        if i % 200 == 0:
            print(f"      {i:,}/{len(need):,}", flush=True)
    with io.open(path, "w", encoding="utf-8") as f:
        json.dump(flags, f, separators=(",", ":"))
    dropped = 0
    for acc in todo:
        conf, tot = flags.get(acc, [False, 0])
        box.sub[acc]["conf"], box.sub[acc]["entryTotal"] = conf, tot
        if is_stub(conf, tot, box.sub[acc]["nrows"]):
            box.drop(acc)
            dropped += 1
    print(f"    丟掉 {dropped} 份殘缺申報")


# ── 一次解析修正案 ──────────────────────────────────────────
def resolve(box: Inbox, names: dict) -> dict:
    """
    {period: {cik: {cusip: (sh, val)}}}

    在**所有來源都收齊之後**才做，理由見檔頭第 2 點：原申報與它的
    `NEW HOLDINGS` 補充申報常常分在不同的包／不同的來源。
    """
    def key(a):
        f = box.sub[a]["filed"]
        return (time.strptime(f, "%d-%b-%Y") if f else time.gmtime(0), a)

    by_filer = defaultdict(list)
    for acc, s in box.sub.items():
        by_filer[(s["period"], s["cik"])].append(acc)
    out = defaultdict(dict)
    for (period, cik), accs in by_filer.items():
        accs.sort(key=key)
        restate = [a for a in accs
                   if box.sub[a]["amd"] and box.sub[a]["amdtype"] == "RESTATEMENT"]
        use = [restate[-1]] if restate else [
            a for a in accs
            if not box.sub[a]["amd"] or box.sub[a]["amdtype"] != "RESTATEMENT"]
        holdings, name = {}, ""
        for a in use:
            name = box.sub[a]["name"] or name
            for cusip, (sh, val) in box.rows.get(a, {}).items():
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


SUFFIX = re.compile(r"(LLC|L\.L\.C|INC|LP|L\.P|LTD|CO|CORP|CORPORATION|PLC|AB|AG|SA|NV|"
                    r"GROUP|HOLDINGS|HOLDING|COMPANY|TRUST|BANK|MANAGEMENT|ADVISORS|ADVISERS|"
                    r"CAPITAL|ASSET|WEALTH|INVESTMENTS?|PARTNERS|FUND|FINANCIAL|SERVICES)")
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
    **家數統計不動**（配對是靠名稱推測的，不該讓推測值去改真正的數字）。
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


def compare(cur: dict, prev: dict, cusip: str, names: dict,
            filed_cur: set, filed_prev: set) -> dict:
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

    # ① 先做重組配對 —— 重組掉的舊主體本來就不會再申報，
    #    不先抓出來的話它會全部掉進第 ② 步的「尚未申報」裡
    reorg = pair_reorgs(new, exited)
    moved_in = {id(r["into"]) for r in reorg}
    moved_out = {id(r["outof"]) for r in reorg}
    new_rest = [x for x in new if id(x) not in moved_in]
    ex_rest = [x for x in exited if id(x) not in moved_out]

    # ② 「這一期根本沒交任何一份 13F」不是建倉／清倉，是還沒交（或改由他人代報）。
    #    這是硬事實不是推測，所以連家數一起改（與重組配對的處理不同）
    opened = [x for x in new_rest if x["cik"] in filed_prev]
    pend_in = [x for x in new_rest if x["cik"] not in filed_prev]
    closed = [x for x in ex_rest if x["cik"] in filed_cur]
    pend_out = [x for x in ex_rest if x["cik"] not in filed_cur]

    return {
        "holders": len(c),
        "holdersPrev": len(p),
        "totalShares": sum(v[0] for v in c.values()),
        "totalValue": sum(v[1] for v in c.values()),
        "increased": len(inc), "decreased": len(dec),
        "opened": len(opened), "closed": len(closed), "unchanged": flat,
        "pendingIn": len(pend_in), "pendingOut": len(pend_out),
        "reorgs": len(reorg),
        "topOpened": top(opened, lambda x: x["value"]),
        "topClosed": top(closed, lambda x: x["value"]),
        "topIncreased": top(inc, lambda x: x["delta"]),
        "topDecreased": top(dec, lambda x: -x["delta"]),
        "topPendingOut": top(pend_out, lambda x: x["value"], 5),
        "topReorgs": sorted(reorg, key=lambda r: -r["outof"]["shares"])[:5],
    }


def recent_quarters(n: int) -> list:
    today = datetime.date.today()
    y, q = today.year, (today.month - 1) // 3 + 1
    out = []
    for _ in range(n):
        out.append((y, q))
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    return out


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", type=int, default=3, help="抓最近幾包 13F 批次資料集")
    ap.add_argument("--live", type=int, nargs="?", const=1, default=0,
                    help="另外從 EDGAR 申報索引抓最近幾季（不給數字＝1）")
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

    box = Inbox()
    live_used = False

    if args.live:
        print("── EDGAR 即時索引")
        for y, q in recent_quarters(args.live):
            if live_quarter(y, q, box, wanted_cusips):
                live_used = True

    print("── 13F 批次資料集")
    for u in links(F13_PAGE, "form13f")[:args.quarters]:
        p = download(u, os.path.join(CACHE, u.rsplit("/", 1)[-1]))
        print(f"    解析 {os.path.basename(p)}")
        add_pack(p, wanted_cusips, box)

    check_stubs(box)
    filed = box.filed_map()

    names: dict = {}
    periods = resolve(box, names)

    order = sorted(periods, key=lambda d: time.strptime(d, "%d-%b-%Y"), reverse=True)
    print(f"    取得期別：{', '.join(order[:6])}")
    if len(order) < 2:
        print("    只有一季，算不出增減 —— 加大 --quarters")
        return
    cur_p, prev_p = order[0], order[1]
    # 最新那一期常常只有少數幾家早交，拿它當「本季」會嚴重低估家數
    while len(order) > 2 and len(periods[cur_p]) < len(periods[prev_p]) * 0.5:
        print(f"    {cur_p} 只有 {len(periods[cur_p])} 家申報（{prev_p} 有 "
              f"{len(periods[prev_p])} 家）→ 尚未申報完，改用 {prev_p} 當本季")
        order = order[1:]
        cur_p, prev_p = order[0], order[1]
    print(f"    本季 {cur_p}（{len(periods[cur_p])} 家申報）"
          f" vs 上季 {prev_p}（{len(periods[prev_p])} 家）")
    filed_cur, filed_prev = filed[cur_p], filed[prev_p]
    print(f"    交過任一份 13F：本季 {len(filed_cur):,} 家、上季 {len(filed_prev):,} 家")

    if not args.dry_run:
        os.makedirs(OUT_DIR, exist_ok=True)
    written = 0
    for ticker, cusip in sorted(cusip_of.items()):
        r = compare(periods[cur_p], periods[prev_p], cusip, names, filed_cur, filed_prev)
        if not r["holders"] and not r["holdersPrev"]:
            continue
        doc = {"ticker": ticker, "cusip": cusip,
               "period": cur_p, "periodPrev": prev_p, **r}
        if args.dry_run:
            print(f"\n{ticker} {cusip} 持有人 {r['holders']}（上季 {r['holdersPrev']}）"
                  f" 增持 {r['increased']} 減持 {r['decreased']}"
                  f" 建倉 {r['opened']} 清倉 {r['closed']}"
                  f"（尚未申報 進 {r['pendingIn']} 出 {r['pendingOut']}）")
            for x in r["topReorgs"][:2]:
                print(f"    重組         {x['outof']['name'][:30]:32} → {x['into']['name'][:30]:32}"
                      f" {x['outof']['shares']:>14,.0f}")
            for x in r["topOpened"][:3]:
                print(f"    建倉         {x['name'][:38]:40} {x['shares']:>14,.0f}")
            for x in r["topIncreased"][:3]:
                print(f"    增持         {x['name'][:38]:40} {x['delta']:>+14,.0f}")
        else:
            with io.open(os.path.join(OUT_DIR, f"{safe_name(ticker)}.json"), "w",
                         encoding="utf-8") as f:
                json.dump(doc, f, ensure_ascii=False, separators=(",", ":"))
        written += 1

    if not args.dry_run:
        with io.open(os.path.join(OUT_DIR, "_index.json"), "w", encoding="utf-8") as f:
            json.dump({
                "generated": time.strftime("%Y-%m-%d"),
                "period": cur_p, "periodPrev": prev_p,
                "filers": len(periods[cur_p]),
                "filedAny": len(filed_cur),
                "tickers": written,
                "live": live_used,
                "source": ("SEC EDGAR 申報索引（即時）＋ Form 13F Data Sets"
                           if live_used else "SEC Form 13F Data Sets")
                          + " + CNS Fails-to-Deliver（CUSIP 對照）",
            }, f, ensure_ascii=False, indent=1)
        print(f"\n寫出 {written} 檔 → config/f13/")
        print(f"總大小 {sum(os.path.getsize(os.path.join(OUT_DIR, x)) for x in os.listdir(OUT_DIR)) / 1e6:.1f} MB")


if __name__ == "__main__":
    main()
