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
    args = ap.parse_args()

    sys.stdout.reconfigure(encoding="utf-8")
    m, concepts, tag2concepts, vocab = load_map()
    stmt_of = {c["id"]: c.get("statement") for c in concepts}
    zh_of = {c["id"]: c.get("zh") for c in concepts}
    # zero_if_absent 的科目不列入覆蓋率：「缺就是 0」是設計決定，不是抓不到
    skip_zero = {c["id"] for c in concepts if c.get("zero_if_absent")}
    mapped_tags = set(tag2concepts)

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
        na = {}
        for g in big:
            # 「未分類」= submissions 沒給 SIC；「其他」= SIC 落在所有區間之外。
            # 這兩桶不是產業，是「不知道」。標成不適用等於用猜的把 n/a 洗掉，
            # 實測未分類會產出 26 個不適用（幾乎整張報表），絕對不能收。
            if g in ("未分類", "其他"):
                continue
            ids = []
            for r in rows:
                gd = [k for k in denom[r["stmt"]] if cik2group[k] == g]
                if len(gd) < args.min_companies:
                    continue
                if sum(1 for k in gd if got(k, r["id"])) / len(gd) < args.na_threshold:
                    ids.append(r["id"])
            na[g] = ids
        out = {
            "version": "1.0",
            "generated": __import__("datetime").date.today().isoformat(),
            "source": [os.path.basename(p) for p in args.zips],
            "map_version": m.get("version"),
            "threshold": args.na_threshold,
            "note": ("由 tools/fsds_coverage.py --applicability 產生。"
                     "某產業有 >=85% 的公司從不申報某科目 -> 該科目對這個產業視為「不適用」，"
                     "值缺時寫「—」而不是 n/a。**只在值本來就缺時才生效**，"
                     "不會蓋掉任何真數字，也不會藏住真缺口。"),
            "sic_groups": [{"lo": lo, "hi": hi, "name": nm} for lo, hi, nm in SIC_GROUPS],
            "not_applicable": na,
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
