#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
離線盤點 config/xbrl_zh_map.json 在全市場的覆蓋率。

吃一包（或多包）SEC DERA Financial Statement Data Sets 季度 zip，回答兩個問題：

  第一部分  分產業的摘要頁欄位建議
            —— 哪些科目在哪個產業密度夠高，放上網頁保證不出現 n/a
  第二部分  真缺口候選
            —— 公司明明有報這條，我們卻沒對照到的標籤

**per-company 零 SEC 請求。**

資料來源
--------
https://www.sec.gov/files/dera/data/financial-statement-data-sets/2025q3.zip  (~128MB)
內含 sub.txt / num.txt(542MB) / pre.txt(89MB) / tag.txt

用法
----
  python tools/fsds_coverage.py 2025q3.zip
  python tools/fsds_coverage.py 2025q?.zip 2024q?.zip     # 多季合併，覆蓋率更準
  python tools/fsds_coverage.py 2025q3.zip --concept cogs # 只看一個科目
  python tools/fsds_coverage.py 2025q3.zip --cik 320193   # 單一公司實際用的標籤
  python tools/fsds_coverage.py 2025q3.zip --json out.json

適用性表（判斷缺值該寫 n/a 還是「—」）
--------------------------------------
  # 產業層：某產業 >=85% 的公司從不申報某科目 -> 該產業不適用
  python tools/fsds_coverage.py 2025q3.zip --applicability config/concept_applicability.json

  # 逐家層（優先順位高，只讀 pre.txt，不碰 542MB 的 num.txt）。**要給四季**，
  # 一季只有 10-Q，20-F 只有 94 家，會把年報才揭露的行判成不適用
  python tools/fsds_coverage.py 2025q3.zip 2025q4.zip 2026q1.zip 2026q2.zip \
      --company-applicability config/company_applicability.json

  # 誤判稽核：改 map 或改門檻之後一定要跑，見下方「唯一會說謊的方向」
  python tools/fsds_coverage.py *.zip --company-applicability x --audit-na
  python tools/fsds_coverage.py *.zip --company-applicability x --explain-cik 1067983

════════════════════════════════════════════════════════════════════════
最重要的一件事：低命中率不等於抓不到
════════════════════════════════════════════════════════════════════════

「生技製藥的非利息收入 0%」是**正確**的 —— 生技公司本來就沒有非利息收入。
「軟體業的存貨 12%」也是正確的。這種格子該顯示「—」（不適用），不是 n/a。

所以覆蓋率報告一定要把兩種零分開：

  不適用  該公司的報表上根本沒有語意相當的那一條  -> 摘要頁不要放這個欄位
  真缺口  報表上有語意相當的標籤，但不在我們的 tags 清單裡  -> 補 alias

本工具用「候選標籤與科目的詞元重疊」自動區分兩者（見 `candidates()`）。
第二部分列出來的才是要動手修的；第一部分的低分只代表該產業不該顯示該欄位。

════════════════════════════════════════════════════════════════════════
為什麼不能用更簡單的做法
════════════════════════════════════════════════════════════════════════

1. **不能用 tag.txt 的 version 前綴判斷「哪家公司定義了這個自訂標籤」。**
   自訂標籤的 version 是「定義該標籤的申報書號」，前綴是**申報代理商**的號碼，
   不是公司 CIK。用前綴比對會把 NVDA 算成 1 個、TSLA 算成 0 個。
   正確做法是三表 join：num.adsh -> sub.cik。

2. **不能只看 num.txt 裡有沒有出現該 tag。** 執行期的管線還有三層過濾，
   掃描要一起模擬，否則假陰性滿天飛：
     - companyfacts **不含維度** -> `segments` 欄非空的事實在管線裡根本看不到
     - companyfacts 只有母公司 -> `coreg` 欄非空（共同申報人）要排除
     - 期間長度要對 -> 資產負債表科目 qtrs=0（時點），損益/現金流 qtrs>=1（期間）
   一個標籤「存在但只有帶維度的版本」是完全不同的病，要分開看（輸出的「僅維度」欄）。

3. **denominator 不能用全部公司。** 沒申報那張報表的公司不該算進分母。
   分母 = 該張報表上至少命中一個已對照科目的公司數。

4. **一包 zip 只有一季，所以第二部分的單位是「期」不是「家」。**
   companyfacts 涵蓋一家公司十幾年的歷史，這包 zip 只有一季。
   同一家公司可能這一季用 A 標籤、三年前用 B 標籤。

   問「這家公司歷史上有沒有用過我們收的標籤」-> 2026-08-20 抽驗 30 家，87% 是假警報
   問「這家公司在**這幾期**有沒有用我們收的標籤」-> 同一批 30 家，只有 3% 是假警報

   後者才是對的問法：網站上呈現的是特定期間的欄位，那一期抓不到就是那一欄 n/a。
   所以「1,274 家」要讀成「1,274 家至少有一期會 n/a」，不是「1,274 家整家掛掉」。
   驗證缺口時務必比對期間（`ddate` vs companyfacts 的 `end`），不要只比對標籤存在與否。

════════════════════════════════════════════════════════════════════════
適用性判斷：唯一會說謊的方向
════════════════════════════════════════════════════════════════════════

兩種錯的代價完全不對稱，所有門檻都要往這個方向倒：

  偽陽性（明明不適用，判成有申報）  -> 格子留在 n/a。難看，但誠實。
  偽陰性（明明有申報，判成不適用）  -> 真缺口被寫成「—」。**這是說謊。**

所以 NA_MIN_SCORE 刻意調寬，寬到有已知偽陽性（蘋果的「淨利息收入」顯示 n/a
而不是「—」）。試過兩種收緊法都更糟，細節見 NA_MIN_SCORE 的註解 —— 不要再試。

`--audit-na` 就是把偽陰性攤開來人工檢查的工具。**改 map 或改門檻之後一定要跑。**
第一次跑就抓到 `Cash` 與 `CashAndDueFromBanks` 不在 map 裡（877 家的資產負債表
只用這兩個標籤），會讓 481 家的現金格子被寫成「—」。已補進 map v1.7。
"""

import argparse
import io
import json
import os
import re
import sys
import zipfile
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(ROOT, "config", "xbrl_zh_map.json")

# 只盤點真正的定期財報。8-K / S-1 之類不進分母。
WANTED_FORMS = {"10-K", "10-Q", "20-F", "40-F", "10-K/A", "10-Q/A", "20-F/A", "40-F/A"}

STD_NS = ("us-gaap/", "ifrs-full/", "srt/")

# SIC 產業分組。目的是讓「同一類公司一起中招」看得出來
# （金融、REIT、公用事業是最常見的三類）。由窄到寬取最具體的區間。
SIC_GROUPS = [
    (6020, 6199, "銀行與信貸"),
    (6200, 6299, "券商與交易所"),
    (6300, 6499, "保險"),
    (6500, 6599, "REIT 與不動產"),
    (6700, 6799, "控股與投資機構"),
    (4900, 4999, "公用事業"),
    (4000, 4799, "運輸"),
    (1000, 1499, "礦業與油氣"),
    (1500, 1799, "營建"),
    (2000, 2199, "食品菸草"),
    (2833, 2836, "生技製藥"),
    (2800, 2899, "化學"),
    (3570, 3579, "電腦硬體"),
    (3674, 3674, "半導體"),
    (3600, 3699, "電子元件"),
    (7370, 7379, "軟體與資訊服務"),
    (8000, 8099, "醫療服務"),
    (5200, 5999, "零售"),
    (5000, 5199, "批發"),
    (2000, 3999, "其他製造業"),
    (7000, 8999, "其他服務業"),
]

# 詞元比對用的停用詞：這些字在標籤名裡到處都是，留著會讓任何兩個標籤看起來相似
STOP = {
    "net", "total", "of", "and", "the", "other", "at", "carrying", "value",
    "including", "excluding", "from", "to", "for", "in", "by", "or", "per",
    "current", "noncurrent", "gross", "gain", "loss", "amount", "abstract",
    "assessed", "tax", "share", "shares", "common", "period", "amounts",
    "nonoperating", "operating",  # 太泛，單獨命中沒有意義
}

_CAMEL = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+|\d+")


def tokens(s: str) -> set:
    return {t.lower() for t in _CAMEL.findall(s or "")} - STOP


def sic_group(sic: str) -> str:
    if not sic or not sic.isdigit():
        return "未分類"
    n = int(sic)
    best = None
    for lo, hi, name in SIC_GROUPS:
        if lo <= n <= hi:
            span = hi - lo
            if best is None or span < best[0]:
                best = (span, name)
    return best[1] if best else "其他"


def load_map():
    with open(MAP_PATH, encoding="utf-8") as f:
        m = json.load(f)
    concepts = m["concepts"]
    tag2concepts = defaultdict(set)
    for c in concepts:
        for t in (c.get("tags") or []) + (c.get("tags_ifrs") or []):
            tag2concepts[t].add(c["id"])
    # 科目的「語意詞元」= 所有已對照標籤的詞元 + 英文名的詞元
    vocab = {}
    for c in concepts:
        v = set()
        for t in (c.get("tags") or []) + (c.get("tags_ifrs") or []):
            v |= tokens(t)
        v |= tokens(c.get("en", ""))
        vocab[c["id"]] = v
    return m, concepts, tag2concepts, vocab


def parse_derive(expr):
    """'interest_income_net + noninterest_income' -> 用到哪些科目。
    覆蓋率只在乎「輸入都拿得到嗎」，不管運算子。"""
    if not expr:
        return []
    return [t for t in re.split(r"[\s+\-*/()]+", expr) if t and not t.isdigit()]


def read_sub(z):
    subs = {}
    with z.open("sub.txt") as fh:
        rd = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        cols = rd.readline().rstrip("\r\n").split("\t")
        ix = {c: i for i, c in enumerate(cols)}
        for line in rd:
            p = line.rstrip("\r\n").split("\t")
            if len(p) < len(cols) or p[ix["form"]] not in WANTED_FORMS:
                continue
            subs[p[ix["adsh"]]] = {
                "cik": p[ix["cik"]], "name": p[ix["name"]],
                "sic": p[ix["sic"]], "form": p[ix["form"]],
            }
    return subs


def scan_num(z, subs, tag2concepts, stmt_of, hit, dimonly, filed_stmt, quiet=False):
    """串流掃 num.txt，模擬管線的三層過濾。就地累加到傳入的 dict（支援多包合併）。"""
    n = 0
    with z.open("num.txt") as fh:
        rd = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        cols = rd.readline().rstrip("\r\n").split("\t")
        ix = {c: i for i, c in enumerate(cols)}
        i_adsh, i_tag, i_ver = ix["adsh"], ix["tag"], ix["version"]
        i_qtrs, i_seg, i_cor, i_val = ix["qtrs"], ix["segments"], ix["coreg"], ix["value"]
        for line in rd:
            n += 1
            if not quiet and n % 2_000_000 == 0:
                print(f"    num.txt {n:,} 列…", file=sys.stderr)
            p = line.rstrip("\r\n").split("\t")
            if len(p) <= i_val:
                continue
            cids = tag2concepts.get(p[i_tag])
            if not cids:
                continue
            sub = subs.get(p[i_adsh])
            if sub is None or p[i_val] == "" or not p[i_ver].startswith(STD_NS):
                continue
            cik = sub["cik"]
            has_dim = p[i_seg] != "" or p[i_cor] != ""
            qtrs = p[i_qtrs]
            for cid in cids:
                st = stmt_of.get(cid)
                if st == "BS" and qtrs != "0":
                    continue
                if st in ("IS", "CF") and qtrs == "0":
                    continue
                if has_dim:
                    dimonly[cik].add(cid)
                else:
                    hit[cik].add(cid)
                    filed_stmt[cik].add(st)
    return n


def scan_pre(z, subs, mapped_tags, cik_tags, tag_label, quiet=False):
    """串流掃 pre.txt，記下每家公司在三大報表上用了哪些「我們沒對照」的標準標籤。

    pre.txt 帶 stmt 欄與 plabel（申報人自己寫的行標題），比 num.txt 更適合
    回答「這家公司的損益表上到底放了什麼」。
    """
    n = 0
    with z.open("pre.txt") as fh:
        rd = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        cols = rd.readline().rstrip("\r\n").split("\t")
        ix = {c: i for i, c in enumerate(cols)}
        i_adsh, i_stmt, i_tag, i_ver, i_lab = (
            ix["adsh"], ix["stmt"], ix["tag"], ix["version"], ix["plabel"])
        for line in rd:
            n += 1
            if not quiet and n % 2_000_000 == 0:
                print(f"    pre.txt {n:,} 列…", file=sys.stderr)
            p = line.rstrip("\r\n").split("\t")
            if len(p) <= i_lab:
                continue
            stmt = p[i_stmt]
            if stmt not in ("IS", "BS", "CF"):
                continue
            sub = subs.get(p[i_adsh])
            if sub is None:
                continue
            tag = p[i_tag]
            # 自訂標籤補進 map 也沒用（別家不會用），只看標準分類法
            if tag in mapped_tags or not p[i_ver].startswith(STD_NS):
                continue
            cik_tags[(sub["cik"], stmt)].add(tag)
            tag_label[tag][p[i_lab]] += 1
    return n


def apply_derive(concepts, hit):
    """把 derive fallback 一起算進覆蓋率 —— 不算的話 gross_profit 會被報成慘不忍睹，
    但實際上管線用 revenue - cogs 就補上了。derive 可能鏈式，跑到收斂為止。"""
    derived = defaultdict(set)
    order = [c for c in concepts if c.get("derive")]
    for _ in range(4):
        changed = False
        for c in order:
            inputs = parse_derive(c["derive"])
            for cik, got in hit.items():
                if c["id"] in got or c["id"] in derived[cik]:
                    continue
                if all((i in got or i in derived[cik]) for i in inputs):
                    derived[cik].add(c["id"])
                    changed = True
        if not changed:
            break
    return derived


MIN_SCORE = 4.0     # 低於此分不算候選（單一稀有詞約 3.8 分，兩個普通詞約 3 分）
MIN_MARGIN = 1.25   # 最佳科目至少要贏第二名這個倍數，否則視為分不清楚，不報

# 適用性判斷用的**寬鬆**門檻，刻意比 MIN_SCORE 低，而且不套贏者全拿。
# 兩邊要的東西相反：
#   缺口候選要精準 —— 報錯了會叫人去補一個不該補的 alias，所以寧可漏報
#   適用性要保守 —— 只要報表上「看起來有點像」的行存在，就當作這條有申報、維持 n/a
# 也就是說這裡的偽陽性（誤判成有）只會讓格子留在 n/a，是安全的方向；
# 偽陰性（誤判成沒有）才會把真缺口洗成「—」，那是說謊。所以門檻往寬的一邊調。
NA_MIN_SCORE = 3.0
#
# 這個門檻是**刻意調寬**的，寬到會有已知的偽陽性。試過兩種收緊法，兩種都更糟：
#
#   核心詞（該科目 60% 以上的已對照標籤都含有、且 idf >= 2.0 的詞）
#     動機：`income`(1.8) + `expense`(1.6) = 3.44 就過門檻，於是
#     `NonoperatingIncomeExpense` 被當成「蘋果有申報淨利息收入」的證據。
#     結果：長期負債的核心詞是 long/term，`NotesPayableNoncurrent`、
#     `LoansPayableNoncurrent` 這些同義標籤一個都不含 -> 判不適用從 85 家
#     暴增到 2,743 家。合約負債 871 -> 4,501。那是把真缺口整片洗成「—」。
#
#   贏面（分數不到最高分科目的 1/1.25 就不算）
#     動機：`IncomeLossFromContinuingOperationsBefore...NoncontrollingInterest`
#     含有 interest（來自 NoncontrollingInterest），對稅前淨利 17.5 分、對淨利息收入
#     只有 4.1 分。結果：營業收入判不適用從 59 家暴增到 923 家。
#
# 兩者都是拿「偽陰性」換「偽陽性」，方向反了。偽陽性（誤判成有申報）只會讓格子
# 留在 n/a，難看但誠實；偽陰性（誤判成不適用）會把真缺口寫成「—」，那是說謊。
# 所以維持寬門檻，代價是蘋果／再生元的「淨利息收入」顯示 n/a 而不是「—」
# （產業表判得對、逐家表判錯的已知案例，見 --audit-na 的說明）。
# 稽核用的下限：分數落在 [NA_NEAR_SCORE, NA_MIN_SCORE) 的標籤叫「差一點」，
# 由 --audit-na 全部列出來人工看過。誤判成不適用是唯一會說謊的方向，
# 這份清單就是把那個方向攤開來檢查的工具，不是可有可無的裝飾。
NA_NEAR_SCORE = 1.5
# 該公司該張報表上至少要看到這麼多個不同標籤，才敢說「這張表我看得夠清楚」。
# 實測 2025q3 各表最少的 5% 分別是 IS 11 / BS 22 / CF 15 行，8 只擋掉病態案例。
NA_MIN_TAGS = 8


def build_scorer(concepts, vocab, stmt_of):
    """把每個未對照標籤指派給「最像的那一個科目」，而且只指派一個。

    兩層防呆，都是第一版壞掉的地方：

    1. **IDF 加權**。`income`、`expense`、`interest` 這種詞在幾十個科目裡都出現，
       單純算交集大小的話，`NetIncomeLossAttributableToNoncontrollingInterest`
       會因為 {income, interest} 兩個爛詞就被當成「淨利息收入」的候選。
       用 df 加權之後，稀有詞（goodwill、inventory、treasury）才有份量。

    2. **贏者全拿**。一個標籤只會出現在它最像的那個科目底下。
       沒有這層的話，`InterestIncomeOperating` 會同時掛在營業收入、利息費用、
       淨利息收入三個科目下面，報告變成三份一樣的噪音。
       而且要贏第二名 MIN_MARGIN 倍，分不出來的就不報 —— 寧可漏報也不要假候選。
    """
    import math
    df = Counter()
    for v in vocab.values():
        df.update(v)
    n = max(len(vocab), 2)
    idf = {t: math.log(n / c) for t, c in df.items()}

    def best(tag, stmt):
        tt = tokens(tag)
        scored = []
        for cid, v in vocab.items():
            if stmt_of[cid] != stmt:
                continue
            ov = tt & v
            if ov:
                scored.append((sum(idf[t] for t in ov), cid))
        if not scored:
            return None
        scored.sort(reverse=True)
        top, cid = scored[0]
        if top < MIN_SCORE:
            return None
        if len(scored) > 1 and scored[1][0] > 0 and top < scored[1][0] * MIN_MARGIN:
            return None
        return cid

    return best


# ══════════════════════════════════════════════════════════════════════
# 逐家適用性（--company-applicability）
# ══════════════════════════════════════════════════════════════════════
#
# 產業表（--applicability）解決不了同業裡的異類：波克夏掛 SIC 6331（保險），
# 但它的資產負債表不分流動／非流動，行為像控股公司。同組多數小型保險公司有分，
# 所以「流動資產合計」在保險組沒跌破門檻 -> 波克夏那格繼續寫 n/a，其實是不適用。
#
# 逐家的判準改成問這家公司自己的報表：
#
#   這家公司的損益表／資產負債表／現金流量表上，有沒有語意相當的那一行？
#     有 -> 真缺口，維持 n/a（我們沒抓到，讀者該去查）
#     沒有 -> 不適用，寫「—」
#
# 資料來源是 pre.txt（表達鏈結庫），它帶 stmt 欄，直接回答「這家公司的損益表上
# 到底放了什麼」。**不能用 num.txt**：num 沒有報表歸屬，附註裡的數字會混進來。
#
# 三個和產業表不同的關鍵設定：
#
#   1. **自訂命名空間也算數。** 覆蓋率報告只看 us-gaap/ifrs/srt（自訂標籤補進 map
#      也沒用，別家不會用），但適用性要問的是「這一行存不存在」。公司用自訂標籤報
#      存貨，那一行就是存在 —— 而且 companyfacts 抓不到它，那是**真缺口**，
#      要留 n/a 讓讀者知道。當成不適用會把這個洞藏起來。
#   2. **多季 union。** 一季只有 10-Q，10-Q 的報表比 10-K 短（研發費用之類可能
#      只在年報單列）。用四季（含年報季）取聯集才不會把年報才有的行判成不適用。
#   3. **看不清楚就不判。** 三張表任一張看到的標籤數不足 NA_MIN_TAGS，整家跳過，
#      退回產業表。沒申報過的公司在這裡完全不會出現，不會被判成「什麼都不適用」。


def build_evidence(concepts, vocab, stmt_of, tag2concepts, near=False):
    """(標籤, 報表) -> 這個標籤可以當作「哪些科目有申報」的證據。

    與 build_scorer 的差別：不套贏者全拿、門檻用 NA_MIN_SCORE。
    一個標籤同時當三個科目的證據是可以接受的 —— 多算證據只會多留 n/a。

    near=True 時反過來回傳「差一點」的科目（分數落在 NA_NEAR_SCORE 與
    NA_MIN_SCORE 之間），給 --audit-na 稽核誤判用。
    """
    import math
    df = Counter()
    for v in vocab.values():
        df.update(v)
    n = max(len(vocab), 2)
    idf = {t: math.log(n / c) for t, c in df.items()}
    by_stmt = defaultdict(list)
    for cid, v in vocab.items():
        by_stmt[stmt_of[cid]].append((cid, v))

    memo = {}

    def evidence(tag, stmt):
        key = (tag, stmt)
        r = memo.get(key)
        if r is not None:
            return r
        tt = tokens(tag)
        scored = []
        for cid, v in by_stmt[stmt]:
            ov = tt & v
            if ov:
                scored.append((sum(idf[t] for t in ov), cid))
        if near:
            # 稽核模式套贏者全拿：`Assets`（總資產）對「無形資產」「短期投資」
            # 「流動資產合計」都有一點分數，全列出來的話 3,000 列都是同一個雜訊。
            # 只留這個標籤**最像**的那個科目，清單才看得完、才可能人工判。
            out = set()
            if scored:
                top, cid = max(scored)
                if NA_NEAR_SCORE <= top < NA_MIN_SCORE:
                    out = {cid}
        else:
            out = {c for c in tag2concepts.get(tag, ()) if stmt_of[c] == stmt}
            out |= {cid for s, cid in scored if s >= NA_MIN_SCORE}
        r = memo[key] = frozenset(out)
        return r

    return evidence


def scan_pre_evidence(z, subs, evidence, evid, seen, quiet=False):
    """掃 pre.txt，累加每家公司每張報表上「有證據的科目」與「看到幾個不同標籤」。

    seen 存 hash(tag) 而不是 tag 本身：四季下來會有數百萬個標籤字串，
    存整數集合省下大量記憶體，碰撞機率對「數量夠不夠」這個用途可忽略。
    """
    n = 0
    with z.open("pre.txt") as fh:
        rd = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        cols = rd.readline().rstrip("\r\n").split("\t")
        ix = {c: i for i, c in enumerate(cols)}
        i_adsh, i_stmt, i_tag = ix["adsh"], ix["stmt"], ix["tag"]
        for line in rd:
            n += 1
            if not quiet and n % 2_000_000 == 0:
                print(f"    pre.txt {n:,} 列…", file=sys.stderr)
            p = line.rstrip("\r\n").split("\t")
            if len(p) <= i_tag:
                continue
            stmt = p[i_stmt]
            if stmt not in ("IS", "BS", "CF"):
                continue
            sub = subs.get(p[i_adsh])
            if sub is None:
                continue
            key = (sub["cik"], stmt)
            tag = p[i_tag]
            seen[key].add(hash(tag))
            e = evidence(tag, stmt)
            if e:
                evid[key] |= e
    return n


def scan_pre_audit(z, subs, nearmiss, na, hits, quiet=False):
    """第二遍掃 pre.txt：把「已經判成不適用、但報表上有差一點的標籤」全撈出來。

    第一遍算 na，第二遍才稽核 —— 因為要先知道哪些 (公司, 科目) 判成不適用，
    才不用把全市場的近似配對都存下來。
    """
    n = 0
    with z.open("pre.txt") as fh:
        rd = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        cols = rd.readline().rstrip("\r\n").split("\t")
        ix = {c: i for i, c in enumerate(cols)}
        i_adsh, i_stmt, i_tag, i_lab = ix["adsh"], ix["stmt"], ix["tag"], ix["plabel"]
        for line in rd:
            n += 1
            p = line.rstrip("\r\n").split("\t")
            if len(p) <= i_lab:
                continue
            stmt = p[i_stmt]
            if stmt not in ("IS", "BS", "CF"):
                continue
            sub = subs.get(p[i_adsh])
            if sub is None:
                continue
            cik = sub["cik"]
            bad = na.get(cik)
            if not bad:
                continue
            for cid in nearmiss(p[i_tag], stmt):
                if cid in bad:
                    hits[(cid, p[i_tag], p[i_lab])].add(cik)
    return n


def build_company_na(concepts, stmt_of, skip_zero, evid, seen, min_tags):
    """每家公司的不適用科目清單。回傳 {cik: [concept_id, ...]}。"""
    na = {}
    for cik in {k[0] for k in seen}:
        known = {st for st in ("IS", "BS", "CF")
                 if len(seen.get((cik, st), ())) >= min_tags}
        # 三張表沒有全部看清楚就整家不判，退回產業表。
        # 只判其中一兩張的話，另外那張的科目會因為「沒證據」被誤判成不適用。
        if len(known) < 3:
            continue
        got = set()
        for st in known:
            got |= evid.get((cik, st), set())
        # derive 的輸入都在 -> 這條算得出來，所以它缺就是真缺口，不是不適用
        for _ in range(4):
            changed = False
            for c in concepts:
                if not c.get("derive") or c["id"] in got:
                    continue
                if all(i in got for i in parse_derive(c["derive"])):
                    got.add(c["id"])
                    changed = True
            if not changed:
                break
        miss = [c["id"] for c in concepts
                if c["id"] not in skip_zero and c["id"] not in got]
        if miss:
            na[cik] = miss
    return na


def main():
    ap = argparse.ArgumentParser(description="離線盤點 xbrl_zh_map 的全市場覆蓋率")
    ap.add_argument("zips", nargs="+", help="FSDS 季度 zip，可給多包合併")
    ap.add_argument("--min-hit", type=float, default=0.90,
                    help="摘要頁門檻：該產業命中率需達此值才建議上網頁（預設 0.90）")
    ap.add_argument("--min-companies", type=int, default=25,
                    help="產業樣本數低於此值不單獨列出（預設 25）")
    ap.add_argument("--concept", help="只看單一科目")
    ap.add_argument("--cik", help="列出單一公司的對照結果與實際用的標籤")
    ap.add_argument("--top", type=int, default=8, help="每個缺口列幾個候選（預設 8）")
    ap.add_argument("--json", help="把完整結果寫成 JSON")
    ap.add_argument("--applicability", metavar="PATH",
                    help="產出 config/concept_applicability.json（執行期判斷「—」用）")
    ap.add_argument("--na-threshold", type=float, default=0.15,
                    help="產適用性表用：該產業命中率低於此值視為「該產業不適用」（預設 0.15）")
    ap.add_argument("--structural-threshold", type=float, default=0.08,
                    help="低於此值視為「這個產業結構上就沒有這一條」，會覆蓋逐家判斷"
                         "（預設 0.08，見 --applicability 輸出的 structural）")
    ap.add_argument("--company-applicability", metavar="PATH",
                    help="產出 config/company_applicability.json（逐家判斷，只讀 pre.txt）")
    ap.add_argument("--explain-cik", help="搭配上一項：印出這家公司每個科目的判定理由")
    ap.add_argument("--audit-na", action="store_true",
                    help="搭配上一項：列出「判成不適用但報表上有差一點的標籤」的組合，"
                         "用來抓誤判（誤判成不適用＝把真缺口洗成「—」＝說謊）")
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    m, concepts, tag2concepts, vocab = load_map()
    stmt_of = {c["id"]: c.get("statement") for c in concepts}
    zh_of = {c["id"]: c.get("zh") for c in concepts}
    # zero_if_absent 的科目不列入覆蓋率：「缺就是 0」是設計決定，不是抓不到
    skip_zero = {c["id"] for c in concepts if c.get("zero_if_absent")}
    mapped_tags = set(tag2concepts)

    # ────────── 逐家適用性：只讀 sub.txt + pre.txt，不碰 542MB 的 num.txt ──────────
    if args.company_applicability:
        evidence = build_evidence(concepts, vocab, stmt_of, tag2concepts)
        evid, seen = defaultdict(set), defaultdict(set)
        names, sics = {}, {}
        for path in args.zips:
            print(f"讀取 {os.path.basename(path)}", file=sys.stderr)
            z = zipfile.ZipFile(path)
            subs = read_sub(z)
            for s in subs.values():
                names[s["cik"]] = s["name"]
                sics[s["cik"]] = s["sic"]
            print(f"  定期財報 {len(subs):,} 份，掃 pre.txt…", file=sys.stderr)
            n = scan_pre_evidence(z, subs, evidence, evid, seen)
            print(f"  pre.txt {n:,} 列", file=sys.stderr)

        na = build_company_na(concepts, stmt_of, skip_zero, evid, seen, NA_MIN_TAGS)
        order = [c["id"] for c in concepts]
        idx = {cid: i for i, cid in enumerate(order)}

        if args.audit_na:
            nas = {k: set(v) for k, v in na.items()}
            nearmiss = build_evidence(concepts, vocab, stmt_of, tag2concepts, near=True)
            hits = defaultdict(set)
            for path in args.zips:
                print(f"稽核 {os.path.basename(path)}", file=sys.stderr)
                z = zipfile.ZipFile(path)
                scan_pre_audit(z, read_sub(z), nearmiss, nas, hits)
            print("\n" + "=" * 92)
            print(f"誤判稽核：判成「不適用」但該公司報表上有分數 "
                  f"{NA_NEAR_SCORE}–{NA_MIN_SCORE} 的標籤")
            print("=" * 92)
            print("這裡每一列都要人工看過。真的是同一條 -> 補進 map 的 tags（順便修好資料），"
                  "\n不是同一條 -> 忽略。列在這裡不代表壞掉，代表「值得懷疑」。\n")
            by_c = defaultdict(list)
            for (cid, tag, lab), ciks in hits.items():
                by_c[cid].append((len(ciks), tag, lab))
            for cid in sorted(by_c, key=lambda c: -sum(n for n, _, _ in by_c[c])):
                tot = len({c for (a, t, l), s in hits.items() if a == cid for c in s})
                print(f"\n{zh_of[cid]}（{cid}，判不適用 {len(na and [1 for v in na.values() if cid in v]):,} 家，"
                      f"其中 {tot:,} 家有可疑標籤）")
                for n, tag, lab in sorted(by_c[cid], reverse=True)[:6]:
                    print(f"    {n:>5} 家  {tag:<52}{lab[:34]}")
            return

        if args.explain_cik:
            cik = str(int(args.explain_cik))
            print(f"\n{names.get(cik, '?')}  CIK {cik}  SIC {sics.get(cik, '?')}"
                  f"  ({sic_group(sics.get(cik, '')) })")
            for st in ("IS", "BS", "CF"):
                print(f"  {st}: 看到 {len(seen.get((cik, st), ()))} 個標籤")
            miss = set(na.get(cik, ()))
            for c in concepts:
                cid = c["id"]
                if cid in skip_zero:
                    mark = "·  zero_if_absent"
                elif cid in miss:
                    mark = "—  報表上沒有語意相當的行 -> 不適用"
                else:
                    mark = "n/a 報表上有這一行 -> 缺值就是真缺口"
                print(f"    {zh_of[cid]:<18}{stmt_of[cid]:<4}{mark}")
            return

        out = {
            "version": "1.0",
            "generated": __import__("datetime").date.today().isoformat(),
            "source": [os.path.basename(p) for p in args.zips],
            "map_version": m.get("version"),
            "min_tags": NA_MIN_TAGS,
            "min_score": NA_MIN_SCORE,
            "note": ("由 tools/fsds_coverage.py --company-applicability 產生，判準是"
                     "**這家公司自己的報表**：pre.txt 的 IS/BS/CF 上有沒有語意相當的行。"
                     "沒有 -> 該科目對這家公司不適用，缺值寫「—」；有 -> 維持 n/a。"
                     "比產業表精準（波克夏掛保險但用未分類資產負債表這類異類才判得對）。"
                     "companies 沒收錄的公司退回 concept_applicability.json 的產業表。"
                     "concepts 是索引表，companies 的值是逗號分隔的索引。"),
            "concepts": order,
            "companies": {k: ",".join(str(idx[i]) for i in v) for k, v in sorted(na.items(), key=lambda x: int(x[0]))},
        }
        with open(args.company_applicability, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
        size = os.path.getsize(args.company_applicability)
        print(f"\n已寫出 {args.company_applicability}　{size/1024:.0f} KB")
        print(f"收錄 {len(na):,} 家（掃到 {len({k[0] for k in seen}):,} 家，"
              f"三張表看不清楚而跳過 {len({k[0] for k in seen}) - len(na):,} 家）")
        cnt = Counter(len(v) for v in na.values())
        print("每家不適用科目數分布：" +
              "、".join(f"{k}個{n}家" for k, n in sorted(cnt.items())[:12]))
        top = Counter(i for v in na.values() for i in v)
        print("\n最常不適用的科目：")
        for cid, n in top.most_common(15):
            print(f"  {zh_of[cid]:<18}{stmt_of[cid]:<4}{n:>6} 家 "
                  f"({n/len(na)*100:.0f}%)")
        return

    hit, dimonly, filed_stmt = defaultdict(set), defaultdict(set), defaultdict(set)
    cik_tags, tag_label = defaultdict(set), defaultdict(Counter)
    subs_all = {}
    for path in args.zips:
        print(f"讀取 {os.path.basename(path)}", file=sys.stderr)
        z = zipfile.ZipFile(path)
        subs = read_sub(z)
        subs_all.update(subs)
        print(f"  定期財報 {len(subs):,} 份", file=sys.stderr)
        print("  掃 num.txt（模擬維度／共同申報人／期間長度過濾）…", file=sys.stderr)
        n1 = scan_num(z, subs, tag2concepts, stmt_of, hit, dimonly, filed_stmt)
        print(f"  掃 pre.txt（收集未對照的標準標籤）…", file=sys.stderr)
        n2 = scan_pre(z, subs, mapped_tags, cik_tags, tag_label)
        print(f"  num.txt {n1:,} 列 / pre.txt {n2:,} 列", file=sys.stderr)

    # 「只有帶維度的版本」定義是乾淨版本不存在
    for cik, s in dimonly.items():
        s -= hit.get(cik, set())

    derived = apply_derive(concepts, hit)
    denom = {st: {c for c, s in filed_stmt.items() if st in s} for st in ("IS", "BS", "CF")}

    cik2group, cik2name = {}, {}
    for s in subs_all.values():
        cik2group[s["cik"]] = sic_group(s["sic"])
        cik2name[s["cik"]] = s["name"]

    def label(tag):
        c = tag_label.get(tag)
        return c.most_common(1)[0][0] if c else ""

    def got(cik, cid):
        return cid in hit.get(cik, ()) or cid in derived.get(cik, ())

    # 每個未對照標籤只歸屬一個科目，先算好備查（見 build_scorer 的兩層防呆）
    best_concept = build_scorer(concepts, vocab, stmt_of)
    tag_owner = {}
    for t in tag_label:
        for st in ("IS", "BS", "CF"):
            owner = best_concept(t, st)
            if owner:
                tag_owner[(t, st)] = owner

    # ────────── 單一公司模式 ──────────
    if args.cik:
        cik = str(int(args.cik))
        print(f"\n{cik2name.get(cik, '?')}  CIK {cik}  產業：{cik2group.get(cik, '?')}\n")
        for c in concepts:
            cid = c["id"]
            if cid in hit.get(cik, ()):
                mark, why = "✓", ""
            elif cid in derived.get(cik, ()):
                mark, why = "≈", f"（derive: {c['derive']}）"
            elif cid in skip_zero:
                mark, why = "·", "（zero_if_absent，缺就是 0）"
            elif cid in dimonly.get(cik, ()):
                mark, why = "✗", "← 標籤有，但只有帶維度的版本，companyfacts 看不到"
            else:
                cand = [t for t in cik_tags.get((cik, stmt_of[cid]), ())
                        if best_concept(t, stmt_of[cid]) == cid]
                why = ("← 候選：" + "、".join(sorted(cand)[:3])) if cand \
                    else "（報表上無語意相當的行 -> 不適用）"
                mark = "✗"
            print(f"  {mark} {cid:<26}{stmt_of[cid]:<4}{zh_of[cid]:<18}{why}")
        return

    # ────────── 全市場命中率 ──────────
    rows = []
    for c in concepts:
        cid = c["id"]
        if cid in skip_zero:
            continue
        d = denom[stmt_of[cid]]
        if not d:
            continue
        direct = sum(1 for k in d if cid in hit.get(k, ()))
        viad = sum(1 for k in d if cid not in hit.get(k, ()) and cid in derived.get(k, ()))
        dimo = sum(1 for k in d if not got(k, cid) and cid in dimonly.get(k, ()))
        rows.append({"id": cid, "zh": zh_of[cid], "stmt": stmt_of[cid], "denom": len(d),
                     "direct": direct, "derived": viad, "dimonly": dimo,
                     "rate": (direct + viad) / len(d)})
    rows.sort(key=lambda r: -r["rate"])

    print("\n" + "=" * 92)
    print(f"全市場命中率　map v{m.get('version')}　{len(cik2name):,} 家公司"
          f"　來源 {', '.join(os.path.basename(p) for p in args.zips)}")
    print("=" * 92)
    print(f"{'科目':<20}{'表':<5}{'命中率':>8}{'直接':>8}{'derive':>8}{'僅維度':>8}{'分母':>8}")
    for r in rows:
        print(f"{r['zh']:<20}{'':<1}{r['stmt']:<4}{r['rate']*100:>7.1f}%"
              f"{r['direct']:>8}{r['derived']:>8}{r['dimonly']:>8}{r['denom']:>8}")

    # ────────── 第一部分：分產業的摘要頁欄位建議 ──────────
    all_ciks = denom["IS"] | denom["BS"] | denom["CF"]
    groups = Counter(cik2group[k] for k in all_ciks)
    big = [g for g, n in groups.most_common() if n >= args.min_companies]

    print("\n" + "=" * 92)
    print(f"第一部分　分產業摘要頁欄位建議（該產業命中率 ≥ {args.min_hit*100:.0f}% 才列入）")
    print("=" * 92)
    print("低分不代表 map 壞掉，多半代表該產業本來就沒這一條 —— 那種格子該顯示「—」不是 n/a。")
    per_group = {}
    for g in big:
        keep, drop = [], []
        for r in rows:
            gd = [k for k in denom[r["stmt"]] if cik2group[k] == g]
            if len(gd) < args.min_companies:
                continue
            rate = sum(1 for k in gd if got(k, r["id"])) / len(gd)
            (keep if rate >= args.min_hit else drop).append((r["zh"], r["id"], rate))
        per_group[g] = {"n": groups[g], "keep": keep, "drop": drop}
        print(f"\n▶ {g}（{groups[g]} 家）　可上摘要頁 {len(keep)} 個")
        print("    " + "、".join(z for z, _, _ in keep))

    universal = set.intersection(*[{i for _, i, _ in per_group[g]["keep"]} for g in big]) if big else set()
    print(f"\n▶▶ 所有產業都過門檻的科目（{len(universal)} 個，可以做成通用摘要頁）：")
    print("    " + "、".join(zh_of[r["id"]] for r in rows if r["id"] in universal))

    # ────────── 第二部分：真缺口候選 ──────────
    targets = [r for r in rows if not args.concept or r["id"] == args.concept]
    print("\n" + "=" * 92)
    print("第二部分　真缺口候選（該公司報表上有語意相當的標籤，我們卻沒對照）")
    print("=" * 92)
    findings = []
    for r in targets:
        cid, st = r["id"], r["stmt"]
        by_group = defaultdict(Counter)
        n_co = 0
        for k in denom[st]:
            if got(k, cid):
                continue
            cand = [t for t in cik_tags.get((k, st), ()) if tag_owner.get((t, st)) == cid]
            if not cand:
                continue  # 報表上沒有語意相當的行 -> 不適用，不是缺口
            n_co += 1
            for t in cand:
                by_group[cik2group[k]][t] += 1
        if not n_co:
            continue
        findings.append((n_co, cid, r, by_group))
    findings.sort(reverse=True, key=lambda x: x[0])

    gap_json = []
    for n_co, cid, r, by_group in findings[:20]:
        print(f"\n{r['zh']}（{cid}，全市場 {r['rate']*100:.1f}%）"
              f"　{n_co} 家有語意相當的行卻沒抓到")
        merged = Counter()
        for c in by_group.values():
            merged.update(c)
        for tag, n in merged.most_common(args.top):
            where = "、".join(f"{g}{c[tag]}" for g, c in
                              sorted(by_group.items(), key=lambda x: -x[1][tag]) if c[tag])[:46]
            print(f"    {n:>5} 家  {tag:<56}{label(tag)[:34]}")
            print(f"{'':>12}{where}")
            gap_json.append({"concept": cid, "tag": tag, "companies": n,
                             "label": label(tag)})

    if args.applicability:
        # 執行期只需要「哪些科目在哪個產業視為不適用」，不需要整份覆蓋率。
        # 附上 SIC 區間表，讓 TS 端不必重複一套產業判斷邏輯。
        na, structural = {}, {}
        for g in big:
            # 「未分類」= submissions 沒給 SIC；「其他」= SIC 落在所有區間之外。
            # 這兩桶不是產業，是「不知道」。標成不適用等於用猜的把 n/a 洗掉，
            # 實測未分類會產出 26 個不適用（幾乎整張報表），絕對不能收。
            if g in ("未分類", "其他"):
                continue
            ids, hard = [], []
            for r in rows:
                gd = [k for k in denom[r["stmt"]] if cik2group[k] == g]
                if len(gd) < args.min_companies:
                    continue
                rate = sum(1 for k in gd if got(k, r["id"])) / len(gd)
                if rate < args.na_threshold:
                    ids.append(r["id"])
                if rate < args.structural_threshold:
                    hard.append(r["id"])
            na[g] = ids
            structural[g] = hard
        out = {
            "version": "1.0",
            "generated": __import__("datetime").date.today().isoformat(),
            "source": [os.path.basename(p) for p in args.zips],
            "map_version": m.get("version"),
            "threshold": args.na_threshold,
            "structural_threshold": args.structural_threshold,
            "note": ("由 tools/fsds_coverage.py --applicability 產生。"
                     "某產業有 >=85% 的公司從不申報某科目 -> 該科目對這個產業視為「不適用」，"
                     "值缺時寫「—」而不是 n/a。**只在值本來就缺時才生效**，"
                     "不會蓋掉任何真數字，也不會藏住真缺口。"),
            "note_structural": (
                "structural 是更嚴的一份（同業申報率 < structural_threshold），"
                "用途不同：not_applicable 只在 company_applicability.json 沒收錄該公司時當退路，"
                "structural 則**一律與逐家判斷取聯集**。理由是逐家判斷靠標籤詞元比對，"
                "對「淨利息收入」這種詞元全是泛用字（interest/income/expense）的科目分不出來，"
                "實測 7,093 家裡只有 110 家被判不適用，等於整片非金融業都顯示 n/a。"
                "同業申報率 <8% 是壓倒性的統計證據，這種時候該讓產業說了算。"
                "門檻不能放寬到 15%：實測 15% 會把 JPM／美銀的應收帳款、波克夏的營業成本與毛利"
                "寫成「—」，但那些公司的報表上真的有那一行 —— 那是說謊。8% 的 23 個翻面逐一查證全對。"),
            "sic_groups": [{"lo": lo, "hi": hi, "name": nm} for lo, hi, nm in SIC_GROUPS],
            "not_applicable": na,
            "structural": structural,
        }
        with open(args.applicability, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
        print(f"\n已寫出 {args.applicability}")
        for g, ids in na.items():
            print(f"  {g:<16}{len(ids):>3} 個不適用：" + "、".join(zh_of[i] for i in ids))

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"source": [os.path.basename(p) for p in args.zips],
                       "map_version": m.get("version"),
                       "companies": len(cik2name),
                       "concepts": rows,
                       "per_group": {g: {"n": v["n"],
                                         "keep": [i for _, i, _ in v["keep"]],
                                         "drop": [[i, round(rt, 3)] for _, i, rt in v["drop"]]}
                                     for g, v in per_group.items()},
                       "universal": sorted(universal),
                       "gap_candidates": gap_json}, f, ensure_ascii=False, indent=1)
        print(f"\n已寫出 {args.json}")

    print("\n注意：這是靜態盤點，**不是**真 API 的回應。"
          "\n     ① 第二部分的家數要讀成「至少有一期會 n/a」，不是「整家掛掉」。"
          "\n     ② 採信任何一條之前先打真 SEC 驗 2–3 家，而且要比對**期間**"
          "\n        （FSDS 的 ddate vs companyfacts 的 end）；只比對標籤存不存在的話，"
          "\n        假警報率是 87%，比對期間之後是 3%。")


if __name__ == "__main__":
    main()
