#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
同業百分位錨點：把評分的 bad／good 從「跨產業拍腦袋的絕對值」換成「同業分布」。

    python tools/peer_stats.py 2025q3.zip 2025q4.zip 2026q1.zip 2026q2.zip \
        --out config/peer_stats.json

為什麼要這個
------------
v1 的錨點是跨產業絕對值。毛利率的 good 訂在 0.75：對軟體公司合理，對半導體設備、
電信、能源就是**永遠拿不到的分數**。分數裡混進了「這是什麼產業」，而那不是我們要
量的東西。實測 NVDA 100 分、公用事業一律偏低，有一部分純粹是產業效應。

v2 改成同業百分位：同一個 SIC 裡，指標的 25 百分位當 bad、75 百分位當 good
（低越好的指標反過來）。**公式與評分程式完全不動，只換兩個數字。**

零 SEC 請求（吃離線的 DERA Financial Statement Data Sets），一季重跑一次。

資料怎麼取
----------
DERA 的 num.txt 每列是一個事實：adsh（申報書號）、tag、ddate（期末日）、
qtrs（0＝時點、1＝單季、4＝年度）、segments/coreg（維度）、value。

- 流量科目取 `qtrs=4`（一整年），存量科目取 `qtrs=0`（時點）→ **直接就是年度數字**，
  不必重建滾動四季。評分的年度版公式（`metric_annual`）本來就是為 20-F 發行人寫的，
  這裡沿用同一套語意
- 帶維度的事實一律丟掉（那是分部數字，不是合併數）
- 一份 10-K 自帶比較期，所以一家公司通常一次就拿到 2–3 個年度 → 年變化型的指標
  （營收年增率、毛利率年變化…）也算得出來

三個會讓錨點說謊的陷阱
----------------------
① **同一個 ddate 會有多份申報**（原申報 + 修正案 + 下一年的比較期）。取 adsh 排序
   最後的那一份會拿到「最早申報的版本」還是「最新的」全看運氣 → 一律以 sub.txt 的
   `filed` 最新者為準，與執行期「同一期間多筆 frame 取 filed 最新」同一條規則
② **極端值會把百分位拉歪**，但**不能直接丟掉**：虧損公司的負毛利率是真的，丟掉等於
   假裝產業裡沒有賠錢的人。作法是取百分位（本來就抗極端值），只擋掉除零與無限大
③ **樣本太小的產業不給錨點**：4 位 SIC 不足 `--min-companies` 就退到 2 位大類，
   再不足就整個不給 → 那個產業維持絕對錨點。半調子的同業錨點比絕對錨點更危險，
   因為它看起來像是有根據的
"""
from __future__ import annotations

import argparse
import io
import json
import re
import sys
import zipfile
from collections import defaultdict
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MAP_PATH = ROOT / "config/xbrl_zh_map.json"
SCORING_PATH = ROOT / "config/scoring.json"
STD_NS = "us-gaap"
WANTED_FORMS = {"10-K", "10-K/A", "20-F", "20-F/A", "40-F", "40-F/A"}


# ── 公式求值（年度語意，對照 web/server/utils/metrics.ts 的 annual 分支）──────
#
# ⚠️ 這是同一套迷你語言的**第三份實作**（TS 給網頁、formulas.py 給 Excel、這裡給批次）。
# 三份不一致的話同一個指標會在三個地方給不同數字，而且沒有任何地方會報錯。
# 所以 --verify 會把這裡算出來的值跟執行中的 /api/signals 對帳，不一致就不要出貨。

_TOKEN = re.compile(r"\s*(\d+\.?\d*|[A-Za-z_][A-Za-z0-9_]*(?:\[t(?:-\d+)?\])?|[-+*/()])")


def annualize(formula: str) -> str:
    """年度模式：一欄＝一整年 → 不必年化，週轉天數的季度天數換成整年"""
    return re.sub(r"\*\s*4\b", "* 1", formula).replace("91.25", "365")


class Expr:
    """遞迴下降。文法與 metrics.ts 相同：expr := term (('+'|'-') term)*"""

    def __init__(self, src: str):
        self.toks = [m.group(1) for m in _TOKEN.finditer(src)]
        self.i = 0

    def peek(self):
        return self.toks[self.i] if self.i < len(self.toks) else None

    def eat(self, t):
        if self.peek() == t:
            self.i += 1
            return True
        return False

    def parse(self):
        n = self.expr()
        return n if self.i == len(self.toks) else None

    def expr(self):
        n = self.term()
        while n is not None and self.peek() in ("+", "-"):
            op = self.toks[self.i]
            self.i += 1
            r = self.term()
            if r is None:
                return None
            n = ("bin", op, n, r)
        return n

    def term(self):
        n = self.factor()
        while n is not None and self.peek() in ("*", "/"):
            op = self.toks[self.i]
            self.i += 1
            r = self.factor()
            if r is None:
                return None
            n = ("bin", op, n, r)
        return n

    def factor(self):
        t = self.peek()
        if t is None:
            return None
        if t == "-":
            self.i += 1
            r = self.factor()
            return None if r is None else ("neg", r)
        if t == "(":
            self.i += 1
            n = self.expr()
            return n if self.eat(")") else None
        self.i += 1
        if re.fullmatch(r"\d+\.?\d*", t):
            return ("num", float(t))
        if t == "avg":
            if not self.eat("("):
                return None
            inner = self.peek()
            self.i += 1
            return ("avg", inner) if self.eat(")") else None
        m = re.fullmatch(r"([A-Za-z_][A-Za-z0-9_]*)(?:\[t(?:-(\d+))?\])?", t)
        if not m:
            return None
        lag = int(m.group(2) or 0)
        # 年度模式：[t-4]（去年同季）＝前 1 欄；[t-1]（上一季）在年度模式無意義
        if lag == 1:
            return ("invalid",)
        return ("ref", m.group(1), lag // 4 if lag else 0)


def evaluate(node, series: dict[str, list[float | None]], idx: int) -> float | None:
    """series[id] 是逐年度的值（由舊到新），idx 是要算的那一年"""
    if node is None:
        return None
    k = node[0]
    if k == "num":
        return node[1]
    if k == "invalid":
        return None
    if k == "neg":
        v = evaluate(node[1], series, idx)
        return None if v is None else -v
    if k == "ref":
        s = series.get(node[1])
        j = idx - node[2]
        return s[j] if s is not None and 0 <= j < len(s) else None
    if k == "avg":
        s = series.get(node[1])
        if s is None or not 0 <= idx < len(s) or s[idx] is None:
            return None
        prev = s[idx - 1] if idx >= 1 else None
        # 沒有前一期就退回本期單點（與 TS／Excel 兩端一致）
        return s[idx] if prev is None else (s[idx] + prev) / 2
    if k == "bin":
        a = evaluate(node[2], series, idx)
        b = evaluate(node[3], series, idx)
        if a is None or b is None:
            return None
        if node[1] == "+":
            return a + b
        if node[1] == "-":
            return a - b
        if node[1] == "*":
            return a * b
        return None if b == 0 else a / b
    return None


# ── DERA 讀取 ────────────────────────────────────────────────────────────────

def load_map():
    m = json.loads(MAP_PATH.read_text(encoding="utf-8"))
    concepts = m["concepts"]
    stmt_of = {c["id"]: c.get("statement") for c in concepts}
    # 標籤 → (概念, 優先序)。優先序低的贏，與執行期的 tags 陣列順序同義
    tag_pri: dict[str, list[tuple[str, int]]] = defaultdict(list)
    for c in concepts:
        for i, t in enumerate(c.get("tags") or []):
            tag_pri[t].append((c["id"], i))
    return m, concepts, stmt_of, tag_pri


def read_sub(z):
    """adsh → {cik, sic, filed}。只收年報：季報沒有 qtrs=4 的年度事實"""
    out = {}
    with z.open("sub.txt") as fh:
        rd = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        cols = rd.readline().rstrip("\r\n").split("\t")
        ix = {c: i for i, c in enumerate(cols)}
        for line in rd:
            p = line.rstrip("\r\n").split("\t")
            if len(p) < len(cols) or p[ix["form"]] not in WANTED_FORMS:
                continue
            out[p[ix["adsh"]]] = {
                "cik": p[ix["cik"]],
                "sic": (p[ix["sic"]] or "").strip(),
                "filed": p[ix["filed"]],
                "name": p[ix["name"]],
            }
    return out


def scan_num(z, subs, tag_pri, stmt_of, store, quiet=False):
    """串流累加 store[cik][ddate][concept] = (優先序, filed, 值)

    同一格會被很多份申報宣告（原申報、修正案、下一年的比較期）。留的規則：
    先比標籤優先序（高優先標籤才是我們要的科目），同優先序再比 filed 最新。
    """
    n = 0
    with z.open("num.txt") as fh:
        rd = io.TextIOWrapper(fh, encoding="utf-8", errors="replace", newline="")
        cols = rd.readline().rstrip("\r\n").split("\t")
        ix = {c: i for i, c in enumerate(cols)}
        i_adsh, i_tag, i_ver, i_ddate = ix["adsh"], ix["tag"], ix["version"], ix["ddate"]
        i_qtrs, i_seg, i_cor, i_val = ix["qtrs"], ix["segments"], ix["coreg"], ix["value"]
        for line in rd:
            n += 1
            if not quiet and n % 4_000_000 == 0:
                print(f"    num.txt {n:,} 列…", file=sys.stderr)
            p = line.rstrip("\r\n").split("\t")
            if len(p) <= i_val:
                continue
            cands = tag_pri.get(p[i_tag])
            if not cands:
                continue
            sub = subs.get(p[i_adsh])
            if sub is None or p[i_val] == "" or not p[i_ver].startswith(STD_NS):
                continue
            if p[i_seg] != "" or p[i_cor] != "":
                continue  # 帶維度＝分部數字，不是合併數
            qtrs = p[i_qtrs]
            try:
                val = float(p[i_val])
            except ValueError:
                continue
            ddate, filed = p[i_ddate], sub["filed"]
            for cid, pri in cands:
                st = stmt_of.get(cid)
                if st == "BS":
                    if qtrs != "0":
                        continue
                elif qtrs != "4":      # IS／CF 要整年
                    continue
                cell = store[sub["cik"]][ddate]
                old = cell.get(cid)
                if old is None or pri < old[0] or (pri == old[0] and filed > old[1]):
                    cell[cid] = (pri, filed, val)
    return n


def apply_derive(concepts, series):
    """執行期的推算 fallback（total_liabilities、pretax_income…）。只補缺的年度。"""
    for c in concepts:
        expr = c.get("derive")
        if not expr:
            continue
        m = re.fullmatch(r"(\w+)((?:\s*[-+*/]\s*\w+\??)+)", expr)
        if not m:
            continue
        head = series.get(m.group(1))
        if head is None:
            continue
        terms = [(t.group(1), t.group(2), t.group(3) == "?")
                 for t in re.finditer(r"([-+*/])\s*(\w+)(\??)", m.group(2))]
        if any(series.get(tid) is None and not opt for _, tid, opt in terms):
            continue
        tgt = series.setdefault(c["id"], [None] * len(head))
        for i in range(len(tgt)):
            if tgt[i] is not None or head[i] is None:
                continue
            v = head[i]
            ok = True
            for op, tid, opt in terms:
                s = series.get(tid)
                x = s[i] if s is not None else None
                if x is None:
                    if opt:
                        continue          # 選用項缺值視為 0
                    ok = False
                    break
                if op in "*/" and x == 0:
                    ok = False
                    break
                v = v + x if op == "+" else v - x if op == "-" else v * x if op == "*" else v / x
            if ok:
                tgt[i] = v


def sic_parent(sic: str) -> str:
    return sic[:2] if len(sic) >= 2 else ""


def pctl(vals: list[float], q: float) -> float:
    """線性內插百分位（等同 numpy 的預設），不引入依賴"""
    s = sorted(vals)
    if len(s) == 1:
        return s[0]
    pos = (len(s) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(s) - 1)
    return s[lo] + (s[hi] - s[lo]) * (pos - lo)


def models_of(scoring: dict) -> list[tuple[str, list, list]]:
    """[(模型名, 構面陣列, SIC 區間)]。通用模型排第一，它沒有 SIC 區間（墊底的那一套）。"""
    out = [("general", scoring["dimensions"], [])]
    out += [(m["id"], m["dimensions"], m.get("sic") or []) for m in scoring.get("models", [])]
    return out


def pick_model(scoring: dict, sic: str | None,
               present: set[str] | None = None,
               shares: dict[str, float] | None = None) -> tuple[str, list]:
    """SIC → 模型。與 web/server/utils/scoring.ts 的 pickModel 同一條規則：取最窄的區間，
    再套事實分流（`fallback_if_absent`：那幾個科目全都沒有 → 換一套）。"""
    if not sic or not sic.isdigit():
        return "general", scoring["dimensions"]
    n = int(sic)
    best = None
    for mdl in scoring.get("models", []):
        for lo, hi in mdl.get("sic") or []:
            if lo <= n <= hi and (best is None or hi - lo < best[0]):
                best = (hi - lo, mdl)
    if best is None:
        return "general", scoring["dimensions"]
    mdl = best[1]
    fb = mdl.get("fallback_if_absent")
    if fb and present is not None and fb["concepts"]:
        keep = bool(set(fb["concepts"]) & present)
        rt = fb.get("ratio_of_assets")
        if rt and shares is not None:
            r = shares.get(rt["concept"])
            keep = keep or (r is not None and r >= rt["min"])
        if not keep:
            alt = next((m for m in scoring["models"] if m["id"] == fb["model"]), None)
            if alt:
                return alt["id"], alt["dimensions"]
    return mdl["id"], mdl["dimensions"]


def main() -> int:
    ap = argparse.ArgumentParser(description="同業百分位錨點")
    ap.add_argument("zips", nargs="+", help="DERA FSDS 季度 zip，可給多包")
    ap.add_argument("--out", default="config/peer_stats.json")
    ap.add_argument("--min-companies", type=int, default=30,
                    help="樣本數低於此值的產業不給錨點（預設 30）")
    # 預設就是定案的寬窄：p25／p75 實測 27.6% 的格子飽和在 100 分（四分之一的項目
    # 分不出高下）、p05／p95 把總分壓在 41–74，取 p10／p90 是飽和 4.8%／13.2%。
    # 預設值留在被否決的那一組的話，重跑時漏帶旗標就會安靜地換掉判準而且沒有人會發現
    # —— 2026-09 那份錨點就是這樣停在 p25／p75 的。config/batch_manifest.json 也釘了一份
    ap.add_argument("--lo", type=float, default=0.10, help="bad 端的百分位（定案 p10）")
    ap.add_argument("--hi", type=float, default=0.90, help="good 端的百分位（定案 p90）")
    ap.add_argument("--report", action="store_true", help="印出每個指標的產業分布")
    ap.add_argument("--score-dist", action="store_true",
                    help="用剛算好的錨點跑一次全市場總分，回報分布與建議的評級級距")
    ap.add_argument("--dump-cik", help="印出這家公司算出來的年度指標（跟網頁對帳用）")
    args = ap.parse_args()

    m, concepts, stmt_of, tag_pri = load_map()
    derived = {d["id"]: d for d in m["derived"]}
    scoring = json.loads(SCORING_PATH.read_text(encoding="utf-8"))

    # 要算的指標：**每一套模型**的評分項（通用／銀行／保險／REIT），取年度版本；
    # 方向由 good/bad 的大小決定。
    #
    # 錨點表是以「指標 id × SIC」為 key，不分模型 —— roe_ttm 在通用模型與銀行模型
    # 是同一個指標，只是被不同 SIC 的公司用到，本來就該共用同一張分布表。
    wanted: dict[str, dict] = {}
    for _name, dims, _sic in models_of(scoring):
        for dim in dims:
            for it in dim["items"]:
                mid = it.get("metric_annual") or it["metric"]
                wanted[it["metric"]] = {
                    "annual_metric": mid,
                    "higher_better": it["good"] > it["bad"],
                    "abs": [it["bad"], it["good"]],
                    # 執行期會作廢的那些格子，這裡也要一起排除 —— 否則錨點是用
                    # 「評分根本不會採計的樣本」算出來的（見下面 guards 的註解）
                    "guards": (it.get("invalid_if_nonpositive_annual")
                               or it.get("invalid_if_nonpositive") or []),
                    "not_meaningful_sic": it.get("not_meaningful_sic") or [],
                }

    store: dict[str, dict[str, dict]] = defaultdict(lambda: defaultdict(dict))
    sic_of: dict[str, str] = {}
    name_of: dict[str, str] = {}
    for zp in args.zips:
        print(f"→ {zp}", file=sys.stderr)
        with zipfile.ZipFile(zp) as z:
            subs = read_sub(z)
            for s in subs.values():
                if s["sic"]:
                    sic_of[s["cik"]] = s["sic"]
                name_of[s["cik"]] = s["name"]
            scan_num(z, subs, tag_pri, stmt_of, store)

    # ── 逐家算年度指標 ──────────────────────────────────────────────
    # 指標會引用排在它前面的指標，所以整條 derived 都要算，不能只算評分用到的那 26 條
    chain = []
    for d in m["derived"]:
        ast = Expr(annualize(d["formula"])).parse()
        if ast is None:
            print(f"⚠️ 解析不了：{d['id']} = {d['formula']}", file=sys.stderr)
        chain.append((d["id"], ast))
    asts = {key: cfg["annual_metric"] for key, cfg in wanted.items()}

    # 事實分流要看的科目（所有模型的 fallback_if_absent 聯集）
    fb_concepts = sorted({c for m in scoring.get("models", [])
                          for c in (m.get("fallback_if_absent") or {}).get("concepts", [])})
    fb_ratios = sorted({(m.get("fallback_if_absent") or {}).get("ratio_of_assets", {}).get("concept")
                        for m in scoring.get("models", [])} - {None})

    samples: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    per_co: list[tuple[str, str, dict[str, float], set[str], dict[str, float]]] = []
    n_co = 0
    dump = None
    for cik, byd in store.items():
        ddates = sorted(byd)
        if not ddates:
            continue
        # 相鄰兩個年度期末要真的差一年，否則「去年同期」比到的是半年前
        keep = [ddates[0]]
        for d0 in ddates[1:]:
            gap = (datetime.strptime(d0, "%Y%m%d").date()
                   - datetime.strptime(keep[-1], "%Y%m%d").date()).days
            if gap >= 300:
                keep.append(d0)
        series: dict[str, list[float | None]] = {}
        for cid in {c["id"] for c in concepts}:
            col = [byd[d0].get(cid) for d0 in keep]
            if any(x is not None for x in col):
                series[cid] = [x[2] if x else None for x in col]
        apply_derive(concepts, series)
        n_per = len(keep)
        for mid, ast in chain:
            series[mid] = [evaluate(ast, series, i) for i in range(n_per)]
        # 最後一個年度才進樣本：一家公司只投一票，不然歷史長的公司權重會變大
        idx = len(keep) - 1
        sic = sic_of.get(cik, "")
        n_co += 1
        vals = {}
        sic_n = int(sic) if sic.isdigit() else None
        for key, mid in asts.items():
            col = series.get(mid)
            v = col[idx] if col and 0 <= idx < len(col) else None
            if v is None or v != v or v in (float("inf"), float("-inf")):
                continue
            # 分母為負就整格作廢（`invalid_if_nonpositive`）—— 執行期是這樣做的，
            # 錨點也必須這樣算。不然分布裡混進了淨利為負時的現金含量、EBITDA 為負時
            # 的淨負債倍數：那些值一律是漂亮的負數，會把 bad 端整個往下拉，
            # 於是每一家看起來都在 p10 以上。實測 REIT（6798）的
            # ocf_to_net_income_ttm p10 是 −3.24，而執行期根本不會採計那種格子
            cfg = wanted[key]
            if any((series.get(g) or [None] * n_per)[idx] is None
                   or (series[g][idx] or 0) <= 0 for g in cfg["guards"]):
                continue
            # 產業上沒有定義的項目同理（金融業的自由現金流、總資產週轉率）
            if sic_n is not None and any(lo <= sic_n <= hi
                                         for lo, hi in cfg["not_meaningful_sic"]):
                continue
            vals[key] = v
            if sic:
                samples[key][sic].append(v)
        present = {c for c in fb_concepts
                   if any(v is not None for v in (series.get(c) or []))}
        # 放款佔資產：取最近一期兩邊都有值的（與 signals.get.ts 的 shareOfAssets 同義）
        shares: dict[str, float] = {}
        assets = series.get("total_assets") or []
        for c in fb_ratios:
            col = series.get(c) or []
            for k in range(min(len(col), len(assets)) - 1, -1, -1):
                if col[k] is not None and assets[k]:
                    shares[c] = col[k] / assets[k]
                    break
        per_co.append((cik, sic, vals, present, shares))
        if args.dump_cik and cik == str(int(args.dump_cik)):
            dump = (cik, name_of.get(cik), keep, vals)

    if dump:
        cik, nm, keep, vals = dump
        print(f"\n=== CIK {cik} {nm} 年度期末 {keep} ===")
        for k in sorted(vals):
            print(f"   {k:34s} {vals[k]:.6g}")

    # ── 產業百分位 ─────────────────────────────────────────────────
    out_metrics: dict[str, dict[str, list[float]]] = {}
    stats_rows = []
    for key, bysic in samples.items():
        higher = wanted[key]["higher_better"]
        table: dict[str, list[float]] = {}
        # 先併出 2 位大類，4 位不足時退到它
        parent: dict[str, list[float]] = defaultdict(list)
        for sic, vs in bysic.items():
            parent[sic_parent(sic)].extend(vs)
        for scope, source in (("sic4", bysic), ("sic2", parent)):
            for sic, vs in source.items():
                if len(vs) < args.min_companies or not sic:
                    continue
                lo, hi = pctl(vs, args.lo), pctl(vs, args.hi)
                if lo == hi:
                    continue                       # 分母為 0，得分整欄變 null
                bad, good = (lo, hi) if higher else (hi, lo)
                table[sic if scope == "sic4" else f"g{sic}"] = [round(bad, 6), round(good, 6)]
                stats_rows.append((key, sic, scope, len(vs), bad, good))
        if table:
            out_metrics[key] = table

    # ── 全市場總分分布（--score-dist）────────────────────────────
    #
    # 級距（警戒／轉弱／中性／穩健／強健）本來是照**絕對錨點**的尺度訂的。換成同業
    # 百分位之後尺度整個變了：實測 140 家大型股的總分落在 41–79，`強健 >= 80` 變成
    # 永遠沒有人拿得到的級別。級距必須跟著錨點一起重訂，否則評級會靜靜地失去意義。
    #
    # 這裡把評分的彙總規則（構面加權、覆蓋率門檻）在全市場跑一次，回報實際分布，
    # 級距就用分位數來訂：底 10% ／10–30% ／30–70% ／70–90% ／頂 10%。
    dist = None
    if args.score_dist:
        def anchors_for(metric: str, sic: str):
            table = out_metrics.get(metric)
            if table and sic:
                for key in (sic, "g" + sic_parent(sic)):
                    a = table.get(key)
                    if a and a[0] != a[1]:
                        return a[0], a[1]
            a = wanted[metric]["abs"]
            return a[0], a[1]

        totals = []
        n_none = 0
        by_model: dict[str, list[int]] = defaultdict(list)
        for cik, sic, vals, present, shares in per_co:
            w_sum = w_score = cov_num = cov_den = 0.0
            model_name, dims = pick_model(scoring, sic, present, shares)
            for dim in dims:
                iw = iws = iw_all = 0.0
                for it in dim["items"]:
                    iw_all += it["weight"]
                    v = vals.get(it["metric"])
                    if v is None:
                        continue
                    bad, good = anchors_for(it["metric"], sic)
                    sc = max(0.0, min(1.0, (v - bad) / (good - bad))) * 100
                    iw += it["weight"]
                    iws += it["weight"] * sc
                cov = iw / iw_all if iw_all else 0.0
                cov_den += dim["weight"]
                cov_num += dim["weight"] * cov
                if iw > 0 and cov >= scoring["dimension_coverage_floor"]:
                    w_sum += dim["weight"]
                    w_score += dim["weight"] * (iws / iw)
            coverage = cov_num / cov_den if cov_den else 0.0
            # 三道門檻要與 web/server/utils/scoring.ts 的 aggregate 一致，
            # 否則這裡估出來的級距套到網站上會整體偏移
            if (w_sum > 0 and coverage >= scoring["coverage_floor"]
                    and w_sum >= scoring.get("counted_weight_floor", 0)):
                totals.append(round(w_score / w_sum))
                by_model[model_name].append(round(w_score / w_sum))
            else:
                n_none += 1
                by_model[model_name]  # 讓沒人算得出來的模型也出現在報表裡
        totals.sort()
        cut = [pctl(totals, q) for q in (0.10, 0.30, 0.70, 0.90)]
        dist = {
            "companies_scored": len(totals),
            "companies_unscorable": n_none,
            "quantiles": {f"p{int(q*100)}": round(pctl(totals, q), 1)
                          for q in (0.05, 0.10, 0.25, 0.50, 0.75, 0.90, 0.95)},
            "suggested_grade_lt": [round(c) for c in cut],
        }
        print(f"\n全市場總分：{len(totals):,} 家算得出來、{n_none:,} 家覆蓋率不足")
        for name, ts in sorted(by_model.items(), key=lambda kv: -len(kv[1])):
            if ts:
                ts2 = sorted(ts)
                print(f"   [{name}] {len(ts):,} 家：p10 {pctl(ts2,.1):.0f}／"
                      f"中位 {pctl(ts2,.5):.0f}／p90 {pctl(ts2,.9):.0f}")
        print("   " + "  ".join(f"{k} {v}" for k, v in dist["quantiles"].items()))
        print(f"   建議級距 lt：{dist['suggested_grade_lt']}"
              f"（底 10%／10–30%／30–70%／70–90%／頂 10%）")

    payload = {
        "version": "1.0",
        "generated": date.today().isoformat(),
        "source": [Path(z).name for z in args.zips],
        "map_version": m["version"],
        "scoring_version": scoring["version"],
        "min_companies": args.min_companies,
        "percentiles": [args.lo, args.hi],
        "note": ("同業百分位錨點：bad＝同業 %d 百分位、good＝%d 百分位（低越好的指標對調）。"
                 "key 為 4 位 SIC；`g` 開頭是 2 位大類，4 位樣本不足時用。"
                 "兩者都查不到就退回 config/scoring.json 的絕對錨點。"
                 "評分公式與程式完全不動，只換這兩個數字。"
                 % (args.lo * 100, args.hi * 100)),
        "score_distribution": dist,
        "metrics": out_metrics,
    }
    outp = ROOT / args.out
    outp.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")

    n4 = sum(1 for k, t in out_metrics.items() for s in t if not s.startswith("g"))
    n2 = sum(1 for k, t in out_metrics.items() for s in t if s.startswith("g"))
    print(f"\n{n_co:,} 家公司、{len(out_metrics)}/{len(wanted)} 個指標有同業錨點；"
          f"4 位 SIC {n4} 組、2 位大類 {n2} 組 → {args.out}")

    if args.report:
        print(f"\n{'指標':34s} {'SIC':6s} {'N':>5s} {'bad':>12s} {'good':>12s}   絕對錨點")
        for key, sic, scope, n, bad, good in sorted(stats_rows):
            if scope != "sic2":
                continue
            a = wanted[key]["abs"]
            print(f"{key:34s} g{sic:5s} {n:5d} {bad:12.4f} {good:12.4f}   {a[0]} → {a[1]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
