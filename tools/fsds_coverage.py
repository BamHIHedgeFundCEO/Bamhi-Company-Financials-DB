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

真缺口普查（--na-gaps）
----------------------
  python tools/fsds_coverage.py 2026q1.zip 2026q2.zip --na-gaps

適用性表回答的是「這格該不該留白」，普查回答的是**剩下的 n/a 該怎麼修**。
它把三層適用性套進來，模擬產品實際渲染的結果，只留下真的會顯示 n/a 的格子：

    有值（直接／derive）        -> 不是 n/a，跳過
    company_applicability 判不適用 -> 顯示「—」，跳過
    該產業 structural 判不適用     -> 顯示「—」，跳過
    其餘                        -> **產品上真的顯示 n/a**，就是這裡要數的

然後對這些格子問「這家公司報表上有哪個我們沒對照的標籤」，按可修家數排名。
排名前面的才值得動手 —— 補一個 tag 修 300 家，跟修 3 家，成本一樣。

輸出分三類，只有第一類補 map 有用：
    候選標籤    報表上有標籤、我們沒對照   -> 補進 tags
    僅維度      標籤有但只有帶維度的版本   -> companyfacts 看不到，補 map 沒用
    無候選      分不出對應哪個科目         -> 要人工看 --cik

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


def load_applicability():
    """把執行期 web/server/utils/applicability.ts 的三層判斷搬到離線。

    兩邊必須一致，否則普查數出來的 n/a 格數是幻覺 —— 會去修一批產品上其實
    顯示「—」的格子。回傳 na_of(cik, sic) -> 該公司不適用的科目 id 集合。
    """
    def read(name):
        p = os.path.join(ROOT, "config", name)
        if not os.path.exists(p):
            return None
        with open(p, encoding="utf-8") as f:
            return json.load(f)

    cfg = read("concept_applicability.json") or {}
    co = read("company_applicability.json") or {}
    ids = co.get("concepts") or []
    per = {cik: {ids[int(i)] for i in packed.split(",") if i}
           for cik, packed in (co.get("companies") or {}).items()}
    groups = cfg.get("sic_groups") or []
    structural = cfg.get("structural") or {}
    not_app = cfg.get("not_applicable") or {}

    def group_of(sic):
        if not sic or not sic.isdigit():
            return None
        n, best = int(sic), None
        for g in groups:
            if g["lo"] <= n <= g["hi"]:
                span = g["hi"] - g["lo"]
                if best is None or span < best[0]:
                    best = (span, g["name"])
        return best[1] if best else None

    def na_of(cik, sic):
        g = group_of(sic)
        p = per.get(str(int(cik))) if cik else None
        if p is not None:
            veto = set(structural.get(g, ())) if g else set()
            return p | veto
        return set(not_app.get(g, ())) if g else set()

    return na_of, len(per)


def load_map():
    with open(MAP_PATH, encoding="utf-8") as f:
        m = json.load(f)
    # 內部科目（internal）不輸出成報表列，也就沒有「這格是不是 n/a」可言。
    # 更重要的是**不能進 vocab** —— 它們不是候選對照的競爭者，卻會墊高
    # equity / interest / amount 這些共用詞的 df，把 idf 壓低，靜靜翻掉別的科目
    # 上千格適用性判定（見 --company-applicability 的回歸警告）。
    concepts = [c for c in m["concepts"] if not c.get("internal")]
    # ⚠️ tag2concepts 要含內部科目：它們在執行期**確實會取到值**並餵給 derive
    # （total_liabilities 靠 equity_total）。少收的話工具會以為那條 derive 從不成立，
    # 憑空多報幾百家 n/a —— 模型與管線脫節就是這樣來的。
    tag2concepts = defaultdict(set)
    for c in m["concepts"]:
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
    """'interest_income_net + noninterest_income' -> 用到哪些科目（只回**必要**的）。

    覆蓋率只在乎「輸入都拿得到嗎」，不管運算子。帶 `?` 的是選用項
    （`total_assets - equity_total - temporary_equity?`），執行期缺值當 0、
    不會讓整條式子失效，所以這裡也不能把它算成必要輸入。
    """
    if not expr:
        return []
    out = []
    for t in re.split(r"[\s+\-*/()]+", expr):
        if not t or t.isdigit() or t.endswith("?"):
            continue
        out.append(t)
    return out


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


def scan_pre(z, subs, mapped_tags, cik_tags, tag_label, quiet=False, cik_ctags=None):
    """串流掃 pre.txt，記下每家公司在三大報表上用了哪些「我們沒對照」的標準標籤。

    pre.txt 帶 stmt 欄與 plabel（申報人自己寫的行標題），比 num.txt 更適合
    回答「這家公司的損益表上到底放了什麼」。

    給了 cik_ctags 就順便把**自訂命名空間**的標籤收進去。補進 map 沒用（別家不會
    用，而且 companyfacts 把自訂命名空間整個剝掉），但它回答一個關鍵問題：
    這格 n/a 是「我們漏對照」還是「這家公司用自訂標籤報，誰也抓不到」。
    後者的 n/a 是正確的，不該再花力氣修。
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
            if not p[i_ver].startswith(STD_NS):
                if cik_ctags is not None:
                    cik_ctags[(sub["cik"], stmt)].add(tag)
                    tag_label[tag][p[i_lab]] += 1
                continue
            # 自訂標籤補進 map 也沒用（別家不會用），只看標準分類法
            if tag in mapped_tags:
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


def _idf(vocab):
    """詞元的反向文件頻率，文件＝科目。`income`、`liability` 這種橫跨多個科目的詞
    要壓低權重，`goodwill`、`inventory` 才有份量。

    ⚠ 這個表隨 map 的 tags 變動。在 A 科目加一個 alias 會改變共用詞的 idf，
    連帶改掉 B 科目對幾百家公司的判定 —— 見 --company-applicability 的回歸警告。
    """
    import math
    df = Counter()
    for v in vocab.values():
        df.update(v)
    n = max(len(vocab), 2)
    return {t: math.log(n / c) for t, c in df.items()}


MIN_SCORE = 4.0     # 低於此分不算候選（單一稀有詞約 3.8 分，兩個普通詞約 3 分）
MIN_MARGIN = 1.25   # 最佳科目至少要贏第二名這個倍數，否則視為分不清楚，不報
# 行標題閘門（--na-gaps 專用，見 build_label_scorer）。比 MIN_SCORE 低是因為
# plabel 通常只有兩三個字（"Capital expenditures"），拿不到長標籤那種分數
LABEL_MIN_SCORE = 3.0

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
    idf = _idf(vocab)

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


# ══════════════════════════════════════════════════════════════════════
# 第三個訊號：共現＝不同行
# ══════════════════════════════════════════════════════════════════════
#
# 前兩個訊號（標籤詞元、行標題）都在問「這個標籤看起來像不像那個科目」。
# 兩個都答不出來的時候（行標題含糊，"Other income/(expense), net" 剝掉停用詞
# 只剩 income+expense，對四個科目同分），需要一個不靠字面的訊號。
#
# 這個訊號問的是完全不同的問題：**這兩個標籤會不會出現在同一張報表上？**
#
#   會 -> 它們是**兩行**。`OperatingLeaseLiabilityNoncurrent` 和
#         `LongTermDebtNoncurrent` 同時出現在幾千張資產負債表上，所以前者
#         永遠不該被當成「這家公司有長期負債那一行」的證據 —— 有長期負債的
#         公司會直接報長期負債，不會拿租賃負債來代替。
#   不會 -> 它們是**替代品**。`CashAndDueFromBanks` 幾乎不與
#          `CashAndCashEquivalentsAtCarryingValue` 同時出現（銀行用前者、
#          其他公司用後者），所以前者確實是「現金那一行」的證據。
#
# 這是純結構訊號，跟標籤怎麼命名、公司怎麼寫行標題都無關，所以和前兩個獨立。
#
# 撤銷要有足夠觀測數才算數（COEXIST_MIN_OBS）：只被兩三家公司用過的冷門標籤，
# 共現率是雜訊。觀測不足就不撤銷 —— 一樣是「不知道就不要猜」。

COEXIST_MAX = 0.50      # 共現率高於此 -> 判定為不同行，撤銷證據資格
COEXIST_MIN_OBS = 30    # 觀測數不足此值不撤銷（冷門標籤的共現率是雜訊）


def build_weak(vocab, stmt_of):
    """(標籤, 報表) -> 靠詞元分數搆得上 NA_MIN_SCORE 的科目集合（不套任何閘門）。

    共現統計要的是「原本會被當成證據的那些配對」，所以這裡刻意不套行標題閘門。
    """
    idf = _idf(vocab)
    by_stmt = defaultdict(list)
    for cid, v in vocab.items():
        by_stmt[stmt_of[cid]].append((cid, v))
    memo = {}

    def weak(tag, stmt):
        key = (tag, stmt)
        r = memo.get(key)
        if r is None:
            tt = tokens(tag)
            r = memo[key] = frozenset(
                cid for cid, v in by_stmt[stmt]
                if (tt & v) and sum(idf[t] for t in (tt & v)) >= NA_MIN_SCORE)
        return r

    return weak


def scan_pre_cooccur(z, subs, weak, tag2concepts, stmt_of, seen_direct, seen_cand,
                     quiet=False):
    """第一遍掃 pre.txt：記下每家公司每張報表上「哪些科目有直接對照的標籤」
    與「出現了哪些候選標籤」。跨季取聯集，與 evidence 的算法一致。"""
    n = 0
    with z.open("pre.txt") as fh:
        rd = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        ix = {c: i for i, c in enumerate(rd.readline().rstrip("\r\n").split("\t"))}
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
            direct = {c for c in tag2concepts.get(tag, ()) if stmt_of[c] == stmt}
            if direct:
                seen_direct[key] |= direct
            w = weak(tag, stmt) - direct
            if w:
                seen_cand[key].add(tag)
    return n


def build_revoked(seen_direct, seen_cand, weak, tag2concepts, stmt_of):
    """算出要撤銷的 (標籤, 科目) 配對。回傳 {(tag, stmt): frozenset(撤掉的科目)}"""
    co, alone = defaultdict(int), defaultdict(int)
    for (cik, stmt), tags in seen_cand.items():
        direct = seen_direct.get((cik, stmt), frozenset())
        for tag in tags:
            for cid in weak(tag, stmt):
                if cid in tag2concepts.get(tag, ()):
                    continue
                if cid in direct:
                    co[(tag, stmt, cid)] += 1
                else:
                    alone[(tag, stmt, cid)] += 1
    out = defaultdict(set)
    stats = []
    for k in set(co) | set(alone):
        c, a = co.get(k, 0), alone.get(k, 0)
        if c + a < COEXIST_MIN_OBS:
            continue
        rate = c / (c + a)
        if rate >= COEXIST_MAX:
            tag, stmt, cid = k
            out[(tag, stmt)].add(cid)
            stats.append((c + a, rate, tag, cid))
    return {k: frozenset(v) for k, v in out.items()}, stats


def build_label_scorer(vocab):
    """用申報人自己寫的行標題（pre.txt 的 plabel）替候選標籤把關。

    標籤詞元一個人判會出這種錯（實測，2026q1+q2）：
      DeferredIncomeTaxLiabilitiesNet -> 合約負債       1,033 家（遞延所得稅，完全無關）
      ProceedsFromSaleOfPropertyPlantAndEquipment -> 資本支出  95 家（處分收入，方向相反）
    兩個都是被 `deferred`／`property plant equipment` 這種共同詞帶過門檻的。

    plabel 是**這家公司的報表上真的印出來的那行字**，比標籤名更接近人怎麼讀它。
    `PaymentsToAcquireOtherPropertyPlantAndEquipment` 的 plabel 直接就是
    "Capital expenditures" —— 那就是它。而 "Deferred income taxes"、
    "Proceeds from sale of property" 跟目標科目的字面幾乎不重疊，會被擋掉。

    兩道閘取交集（標籤詞元 and 行標題），因為兩者錯的地方不一樣：
    標籤名被分類法的命名慣例帶偏，行標題被公司自己的簡寫帶偏（"Other"、"Total"）。
    一起看才留得下真的。
    """
    idf = _idf(vocab)

    def score(text, cid):
        ov = tokens(text) & vocab[cid]
        return sum(idf[t] for t in ov)

    return score


def build_evidence(concepts, vocab, stmt_of, tag2concepts, near=False, revoked=None):
    """(標籤, 報表, 行標題) -> 這個標籤可以當作「哪些科目有申報」的證據。

    與 build_scorer 的差別：不套贏者全拿、門檻用 NA_MIN_SCORE。
    一個標籤同時當三個科目的證據是可以接受的 —— 多算證據只會多留 n/a。

    ── 行標題撤銷（第二個參數 plabel）─────────────────────────────────
    只看標籤詞元的話，`OperatingLeaseLiabilityNoncurrent` 會因為
    {liability, lease, term} 之類的重疊被當成「這家公司有長期負債那一行」的證據，
    於是真的沒有長期負債的公司也維持 n/a。實測這種偽陽性佔殘餘 n/a 的 64.5%
    （27,219 格），是目前最大的一塊。

    修法**不是**把詞元門檻調嚴 —— 那試過兩次都反效果（見 NA_MIN_SCORE 註解）。
    改用一個獨立訊號：申報人自己印在報表上的那行字。規則刻意只往單一方向作用：

        行標題明確指名**別的**科目（對別的科目 >= LABEL_MIN_SCORE、對這個不到）
            -> 撤銷這條證據。"Operating lease liabilities" 不是長期負債。
        行標題對這個科目也夠分  -> 留著，兩個訊號一致。
        行標題對每個科目都不夠分（"Other"、"Total"、公司自己的簡寫）
            -> **留著**。含糊不代表沒有，拿含糊當「不適用」就是說謊。

    第三條是安全閥。少了它，terse label 的公司會整片被判不適用。

    直接對照（標籤就在 map 的 tags 裡）**不受此閘門影響**：那已經是確定的答案，
    公司把 Inventory 那一行叫什麼名字都不改變它是存貨。

    near=True 時反過來回傳「差一點」的科目（分數落在 NA_NEAR_SCORE 與
    NA_MIN_SCORE 之間），給 --audit-na 稽核誤判用；稽核模式不套行標題撤銷，
    因為它本來就是要把可疑的全部攤開來看。
    """
    idf = _idf(vocab)
    by_stmt = defaultdict(list)
    for cid, v in vocab.items():
        by_stmt[stmt_of[cid]].append((cid, v))

    memo = {}
    lmemo = {}

    def claims(plabel, stmt):
        """行標題「指名」了哪些科目。空集合＝含糊，不撤銷任何東西。

        分開 memo：行標題的相異字串數遠少於 (標籤 × 行標題) 的組合數，
        全市場四季下來前者約數萬、後者數百萬。
        """
        key = (plabel, stmt)
        r = lmemo.get(key)
        if r is None:
            lt = tokens(plabel)
            r = lmemo[key] = frozenset(
                cid for cid, v in by_stmt[stmt]
                if lt & v and sum(idf[t] for t in (lt & v)) >= LABEL_MIN_SCORE)
        return r

    def evidence(tag, stmt, plabel=""):
        key = (tag, stmt, plabel)
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
            # 直接對照不受任何閘門影響
            out = {c for c in tag2concepts.get(tag, ()) if stmt_of[c] == stmt}
            weak = {cid for s, cid in scored if s >= NA_MIN_SCORE}
            if revoked:
                weak -= revoked.get((tag, stmt), frozenset())  # 共現＝不同行
            cl = claims(plabel, stmt) if plabel else frozenset()
            out |= (weak & cl) if cl else weak
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
        i_adsh, i_stmt, i_tag, i_lab = ix["adsh"], ix["stmt"], ix["tag"], ix["plabel"]
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
            key = (sub["cik"], stmt)
            tag = p[i_tag]
            seen[key].add(hash(tag))
            e = evidence(tag, stmt, p[i_lab])
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
    ap.add_argument("--na-gaps", action="store_true",
                    help="真缺口普查：套進三層適用性，只列產品上真的會顯示 n/a 的格子，"
                         "按「補這個標籤可修幾家」排名")
    ap.add_argument("--gap-json", metavar="PATH",
                    help="--na-gaps 的完整結果寫成 JSON（含每個候選的 CIK 清單，"
                         "拿去做 live companyfacts 期間比對驗證）")
    ap.add_argument("--gap-top", type=int, default=25,
                    help="--na-gaps 每個科目列幾個候選標籤（預設 25）")
    ap.add_argument("--audit-na", action="store_true",
                    help="搭配上一項：列出「判成不適用但報表上有差一點的標籤」的組合，"
                         "用來抓誤判（誤判成不適用＝把真缺口洗成「—」＝說謊）")
    args = ap.parse_args()

    # stderr 也要設：Windows 主控台預設 cp950，進度與警告裡的中文會變亂碼，
    # 而回歸警告正好只印在 stderr —— 看不懂等於沒有警告
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    m, concepts, tag2concepts, vocab = load_map()
    # stmt_of 要含內部科目（tag2concepts 會吐出它們的 id），否則 scan_num 的
    # 期間長度過濾對它們整個失效，資產負債表科目會被 IS/CF 的區間事實誤記成有值
    stmt_of = {c["id"]: c.get("statement") for c in m["concepts"]}
    zh_of = {c["id"]: c.get("zh") for c in m["concepts"]}
    # zero_if_absent 的科目不列入覆蓋率：「缺就是 0」是設計決定，不是抓不到
    skip_zero = {c["id"] for c in concepts if c.get("zero_if_absent")}
    mapped_tags = set(tag2concepts)

    # ────────── 逐家適用性：只讀 sub.txt + pre.txt，不碰 542MB 的 num.txt ──────────
    if args.company_applicability:
        # 第一遍：統計共現，算出「這個標籤對這個科目不算證據」的配對
        weak = build_weak(vocab, stmt_of)
        sd, sc = defaultdict(set), defaultdict(set)
        names, sics = {}, {}
        for path in args.zips:
            print(f"共現統計 {os.path.basename(path)}", file=sys.stderr)
            z = zipfile.ZipFile(path)
            subs = read_sub(z)
            for s in subs.values():
                names[s["cik"]] = s["name"]
                sics[s["cik"]] = s["sic"]
            scan_pre_cooccur(z, subs, weak, tag2concepts, stmt_of, sd, sc)
        revoked, costats = build_revoked(sd, sc, weak, tag2concepts, stmt_of)
        sd.clear(); sc.clear()
        print(f"\n共現撤銷 {sum(len(v) for v in revoked.values()):,} 組（標籤,科目）"
              f"，門檻共現率 >= {COEXIST_MAX:.0%}、觀測 >= {COEXIST_MIN_OBS}",
              file=sys.stderr)
        for n, rate, tag, cid in sorted(costats, reverse=True)[:10]:
            print(f"    {n:>6} 家  共現 {rate:>5.0%}  {tag[:52]:<54}"
                  f"{zh_of.get(cid, cid)}", file=sys.stderr)

        # 第二遍：套三個閘門算證據
        evidence = build_evidence(concepts, vocab, stmt_of, tag2concepts, revoked=revoked)
        evid, seen = defaultdict(set), defaultdict(set)
        for path in args.zips:
            print(f"讀取 {os.path.basename(path)}", file=sys.stderr)
            z = zipfile.ZipFile(path)
            subs = read_sub(z)
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

        # ── 覆寫前先跟現有那份比，把新增的「不適用」報出來 ──────────────
        #
        # 這是本工具最容易被忽略、也最會出事的地方：**map 的 tags 詞元會餵進
        # 適用性判斷的 idf**，所以在 A 科目加一個 alias，會靜靜地改掉 B 科目
        # 對幾百家公司的判定，而且方向是說謊的那一邊。
        #
        # 實測（2026-08-20，v1.7 -> v1.8）：為了修 5 家 LLC 發行人的股東權益，
        # 加了 LimitedLiabilityCompanyLlcMembersEquity… ，它的 LimitedLiability
        # 貢獻 liability 一詞 -> liability 的 idf 3.22 掉到 2.81 -> 靠
        # {liability, lease}、{liability, deferred} 得分的標籤整批跌破 3.0 ->
        # 長期租賃負債 808 家、合約負債 814 家從 n/a 翻成「—」，抽查 10 家有
        # 4–5 家其實有申報。修 5 家賠 1,622 格。
        #
        # 沒有這段輸出的話，這件事在覆蓋率、命中率、n/a 總數上全都看不出來
        # （總數還會變好看，因為「—」不算 n/a）。收回的方向是安全的，不用管；
        # **新增的方向每次都要抽樣打真 API 查**。
        if os.path.exists(args.company_applicability):
            try:
                with open(args.company_applicability, encoding="utf-8") as f:
                    prev = json.load(f)
                pi = prev["concepts"]
                old_na = {k: {pi[int(i)] for i in v.split(",") if i}
                          for k, v in prev["companies"].items()}
                add, rem = Counter(), Counter()
                for cik, ids in na.items():
                    add.update(set(ids) - old_na.get(cik, set()))
                for cik, ids in old_na.items():
                    rem.update(set(ids) - set(na.get(cik, ())))
                if sum(add.values()):
                    print(f"\n⚠ 與現有 {os.path.basename(args.company_applicability)} 相比，"
                          f"新增判不適用 {sum(add.values()):,} 格、收回 {sum(rem.values()):,} 格",
                          file=sys.stderr)
                    print("  新增＝原本寫 n/a 的格子改寫「—」。**這是唯一會說謊的方向**，"
                          "每個科目抽 10 家打 companyfacts 確認真的沒申報：", file=sys.stderr)
                    for cid, n in add.most_common(8):
                        print(f"    {zh_of.get(cid, cid):<20}{n:>7,} 家", file=sys.stderr)
            except Exception as e:
                print(f"（比對舊檔失敗，跳過：{e}）", file=sys.stderr)

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
    cik_ctags = defaultdict(set) if args.na_gaps else None
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
        n2 = scan_pre(z, subs, mapped_tags, cik_tags, tag_label, cik_ctags=cik_ctags)
        print(f"  num.txt {n1:,} 列 / pre.txt {n2:,} 列", file=sys.stderr)

    # 「只有帶維度的版本」定義是乾淨版本不存在
    for cik, s in dimonly.items():
        s -= hit.get(cik, set())

    derived = apply_derive(concepts, hit)
    denom = {st: {c for c, s in filed_stmt.items() if st in s} for st in ("IS", "BS", "CF")}

    cik2group, cik2name, cik2sic = {}, {}, {}
    for s in subs_all.values():
        cik2group[s["cik"]] = sic_group(s["sic"])
        cik2name[s["cik"]] = s["name"]
        cik2sic[s["cik"]] = s["sic"]

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

    # ────────── 真缺口普查 ──────────
    if args.na_gaps:
        na_of, n_per = load_applicability()
        lab_score = build_label_scorer(vocab)
        all_ciks = denom["IS"] | denom["BS"] | denom["CF"]
        # 每個科目的分母只能算「有申報那張表」的公司，否則只交 10-K 摘要的
        # 空殼會把每個科目的 n/a 家數灌大一倍
        cand = defaultdict(set)      # (科目, 標籤) -> 有這格 n/a 且報表上有這個標籤的公司
        stat = defaultdict(Counter)  # 科目 -> {na, dash, dimonly, nocand}
        nocand_ex = defaultdict(list)
        for cik in all_ciks:
            dash = na_of(cik, cik2sic.get(cik, ""))
            for c in concepts:
                cid = c["id"]
                if cid in skip_zero or cik not in denom[stmt_of[cid]]:
                    continue
                if got(cik, cid):
                    continue
                if cid in dash:
                    stat[cid]["dash"] += 1
                    continue
                stat[cid]["na"] += 1
                if cid in dimonly.get(cik, ()):
                    stat[cid]["dimonly"] += 1
                    continue
                ts = [t for t in cik_tags.get((cik, stmt_of[cid]), ())
                      if tag_owner.get((t, stmt_of[cid])) == cid
                      and lab_score(label(t), cid) >= LABEL_MIN_SCORE]
                if not ts:
                    # 「無候選」有兩種，處置完全不同：
                    #   自訂標籤報的  companyfacts 剝掉自訂命名空間 -> 誰也抓不到，
                    #                 n/a 是正確的，不要再花力氣
                    #   什麼都沒有    適用性判太寬 -> 這格其實該顯示「—」
                    if any(lab_score(label(t), cid) >= LABEL_MIN_SCORE
                           for t in cik_ctags.get((cik, stmt_of[cid]), ())):
                        stat[cid]["custom"] += 1
                    else:
                        stat[cid]["nocand"] += 1
                        if len(nocand_ex[cid]) < 5:
                            nocand_ex[cid].append(cik2name.get(cik, cik))
                    continue
                for t in ts:
                    cand[(cid, t)].add(cik)

        tot_na = sum(s["na"] for s in stat.values())
        tot_cell = sum(s["na"] + s["dash"] for s in stat.values()) + \
            sum(1 for cik in all_ciks for c in concepts
                if c["id"] not in skip_zero and cik in denom[stmt_of[c["id"]]]
                and got(cik, c["id"]))
        print("\n" + "=" * 92)
        print(f"真缺口普查　map v{m.get('version')}　{len(all_ciks):,} 家　"
              f"逐家適用性收錄 {n_per:,} 家")
        print("=" * 92)
        print(f"套進三層適用性後，全市場 {tot_cell:,} 個科目格子裡有 "
              f"{tot_na:,} 個顯示 n/a（{tot_na/max(tot_cell,1)*100:.1f}%）\n")

        print("可補標籤＝補 map 就會有值　僅維度／自訂＝companyfacts 看不到，n/a 正確"
              "　判太寬＝該顯示「—」")
        print(f"{'科目':<20}{'表':<5}{'n/a家數':>9}{'可補標籤':>10}{'僅維度':>8}"
              f"{'自訂標籤':>10}{'判太寬':>8}")
        agg = Counter()
        for cid in sorted(stat, key=lambda c: -stat[c]["na"]):
            s = stat[cid]
            if not s["na"]:
                continue
            fix = s["na"] - s["dimonly"] - s["nocand"] - s["custom"]
            for k, v in (("fix", fix), ("dimonly", s["dimonly"]),
                         ("custom", s["custom"]), ("nocand", s["nocand"])):
                agg[k] += v
            print(f"{zh_of[cid]:<20}{'':<1}{stmt_of[cid]:<4}{s['na']:>9,}"
                  f"{fix:>10,}{s['dimonly']:>8,}{s['custom']:>10,}{s['nocand']:>8,}")
        print(f"\n{'合計':<20}{'':<5}{tot_na:>9,}{agg['fix']:>10,}{agg['dimonly']:>8,}"
              f"{agg['custom']:>10,}{agg['nocand']:>8,}")

        print("\n" + "=" * 92)
        print("補這個標籤可以修幾家（只列真的會顯示 n/a 的公司，已扣掉「—」）")
        print("=" * 92)
        print("**逐一查證再補**。名字像不代表是同一條，要用 live companyfacts 做期間比對，")
        print("確認補進去真的取到值、而且沒有搶走既有標籤的期。\n")
        rank = sorted(cand.items(), key=lambda kv: -len(kv[1]))
        by_c = defaultdict(list)
        for (cid, t), ciks in rank:
            by_c[cid].append((len(ciks), t, ciks))
        for cid in sorted(by_c, key=lambda c: -sum(n for n, _, _ in by_c[c])):
            print(f"\n{zh_of[cid]}（{cid}，{stmt_of[cid]}，n/a {stat[cid]['na']:,} 家）")
            for n, t, ciks in by_c[cid][:args.gap_top]:
                if n < 2:
                    continue
                eg = "、".join(sorted(cik2name.get(c, c) for c in ciks)[:2])
                print(f"    {n:>5} 家  {t:<58}{label(t)[:26]:<28}{eg[:40]}")
            if stat[cid]["nocand"]:
                print(f"    （無候選 {stat[cid]['nocand']:,} 家，例："
                      f"{'、'.join(nocand_ex[cid][:3])}）")

        if args.gap_json:
            with open(args.gap_json, "w", encoding="utf-8") as f:
                json.dump({
                    "map_version": m.get("version"),
                    "source": [os.path.basename(p) for p in args.zips],
                    "total_cells": tot_cell, "total_na": tot_na,
                    "stat": {c: dict(s) for c, s in stat.items()},
                    "candidates": [
                        {"concept": cid, "tag": t, "label": label(t), "n": len(ciks),
                         "ciks": sorted(ciks, key=int)[:40],
                         "names": sorted(cik2name.get(c, c) for c in ciks)[:6]}
                        for (cid, t), ciks in rank if len(ciks) >= 2],
                }, f, ensure_ascii=False)
            print(f"\n已寫出 {args.gap_json}")
        return

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
