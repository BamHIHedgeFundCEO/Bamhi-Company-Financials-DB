#!/usr/bin/env python3
"""
分部「靜默漏抓」排行榜 —— 找出**申報明明有分部、輸出卻沒寫上去**的公司。

和另外兩支的分工：
  segment_sweep.py 量「畫出來的表對不對」（對得上／對不上／無法校驗）
  segment_gaps.py  量「該畫的有沒有被畫出來」—— 表全部對得上但整個軸不見的情形

為什麼需要獨立一支：被抽取器丟掉的事實**永遠不會出現在 API 回應裡**。BW 的回應是
`warnings: []`、每一格 verified 都是 true，看起來完美，實際上整個產品軸
（Parts/Projects/Construction）從來沒出現、三個分部的 2025Q1/Q2 也整排消失。
只看輸出的掃描工具對這類問題一律回報「乾淨」。所以偵測器裝在 segments.ts 的
抽取器內部（見 detectGaps），本工具只負責把它吐出的 gaps 聚合排名。

**排行榜的單位是「揭露型態」，不是公司。** 同一種型態的公司會一起中招 ——
交叉維度標註的公司全部漏掉一個軸，新申報退化的公司全部被鎖掉舊拆法。
看型態排名一次修一整類，不必一家一家排查。

用法：
  python tools/segment_gaps.py                    # 讀 sweep_out/seg 現有快取
  python tools/segment_gaps.py --dir seg_v5       # 指定快取目錄
  python tools/segment_gaps.py --top 20 --code axis_dropped
"""
import argparse
import gzip
import io
import json
import os
import re
import sys
from collections import Counter, defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "tools", "sweep_out")

CODE_ZH = {
    "axis_dropped": "整個軸被丟掉",
    "degenerate_axis": "退化軸（單一成員＝合併總額）",
    "period_hole": "季度中間挖洞",
    "dropped_facts": "事實被丟棄",
}

def mask_custom(tag: str) -> str:
    """自訂命名空間的成員（bw:BWMember）跨公司不可比 → 只留命名空間。"""
    out = []
    for part in re.split(r"([@+×])", tag):
        if ":" in part and part.split(":", 1)[0] not in ("us-gaap", "srt", "ifrs-full", "dei", "country"):
            part = f"{part.split(':', 1)[0]}:<自訂>"
        out.append(part)
    return "".join(out)


def pattern_of(g: dict) -> str:
    """把一筆 gap 壓成跨公司可比的型態字串。"""
    code = g.get("code", "?")
    axis = g.get("axis", "?")
    if code == "axis_dropped":
        # detail 尾巴帶「（主因 reason：tag）」。**軸不見的原因才是型態** ——
        # 同樣是地區軸消失，被 ConcentrationRisk 擋掉和被交叉維度丟掉要分開修
        m = re.search(r"（主因 (.+?)）", g.get("detail", ""))
        if not m:
            return f"axis_dropped / {axis} ← 原因不明"
        # 「主因」是 `reason：tag`，reason 不是 qname，先切開再遮罩才不會被誤判
        reason, _, tag = m.group(1).partition("：")
        return f"axis_dropped / {axis} ← {reason}：{mask_custom(tag)}"
    if code == "dropped_facts":
        # detail 形如 `cross_axis：srt:Xxx×us-gaap:Yyy 丟掉 N 筆…`，取軸組合
        m = re.search(r"：(.+?) 丟掉", g.get("detail", ""))
        return f"{axis} / {mask_custom(m.group(1) if m else '?')}"
    return f"{code} / {axis}"


def load_dir(dirname: str):
    d = os.path.join(OUT, dirname)
    if not os.path.isdir(d):
        sys.exit(f"找不到快取目錄 {d}")
    for fn in sorted(os.listdir(d)):
        if not fn.endswith(".json.gz"):
            continue
        t = fn.split(".json")[0]
        try:
            with gzip.open(os.path.join(d, fn), "rt", encoding="utf-8") as f:
                yield t, json.load(f)
        except Exception as e:
            print(f"  ! {t} 讀取失敗：{e}", file=sys.stderr)


def money(v: float) -> str:
    return f"{v / 1e9:,.1f}B" if abs(v) >= 1e9 else f"{v / 1e6:,.0f}M"


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="seg")
    ap.add_argument("--top", type=int, default=15)
    ap.add_argument("--code", help="只看某一種訊號")
    args = ap.parse_args()

    stale, clean, err = [], [], []
    by_pattern = defaultdict(lambda: {"tickers": set(), "amount": 0.0, "n": 0})
    by_ticker = defaultdict(lambda: {"amount": 0.0, "gaps": []})
    code_count = Counter()

    for t, d in load_dir(args.dir):
        if d.get("_error"):
            err.append(t)
            continue
        # v4 之前的快取沒有 gaps 欄位。**不能當成沒問題** —— 那是假陰性，
        # 比沒有偵測器更糟，所以單獨列出來要求重跑
        if "gaps" not in d:
            stale.append(t)
            continue
        gaps = [g for g in d["gaps"] if not args.code or g.get("code") == args.code]
        if not gaps:
            clean.append(t)
            continue
        for g in gaps:
            p = pattern_of(g)
            box = by_pattern[p]
            box["tickers"].add(t)
            box["amount"] += g.get("amount", 0) or 0
            box["n"] += 1
            code_count[g.get("code", "?")] += 1
            by_ticker[t]["amount"] += g.get("amount", 0) or 0
            by_ticker[t]["gaps"].append(g)

    n_scanned = len(clean) + len(by_ticker)
    print(f"快取目錄 {args.dir}：{n_scanned} 家有 gaps 資料"
          f"（乾淨 {len(clean)}／有訊號 {len(by_ticker)}）")
    if stale:
        print(f"⚠ {len(stale)} 家是 v4 之前的舊快取、沒有 gaps 欄位，"
              f"**不代表乾淨**，要 --refresh 重跑：{', '.join(stale[:12])}"
              + (" …" if len(stale) > 12 else ""))
    if err:
        print(f"  {len(err)} 家取數失敗：{', '.join(err[:12])}")

    print("\n【段 1】訊號種類")
    for c, n in code_count.most_common():
        print(f"  {CODE_ZH.get(c, c):<28}{n:>5} 筆")
    if not code_count:
        print("  （無）")

    print("\n【段 2】揭露型態排行榜 —— 同型態一起修，這才是修法的優先序")
    print(f"  {'型態':<80}{'家數':>5}{'涉及金額':>12}")
    rows = sorted(by_pattern.items(), key=lambda kv: (-len(kv[1]["tickers"]), -kv[1]["amount"]))
    for p, box in rows[: args.top]:
        print(f"  {p[:78]:<80}{len(box['tickers']):>5}{money(box['amount']):>12}")
    if not rows:
        print("  （無）")

    print("\n【段 3】中招最重的公司（依涉及金額）")
    print(f"  {'ticker':<9}{'金額':>11}  訊號")
    for t, box in sorted(by_ticker.items(), key=lambda kv: -kv[1]["amount"])[: args.top]:
        codes = ", ".join(sorted({CODE_ZH.get(g["code"], g["code"]) for g in box["gaps"]}))
        print(f"  {t:<9}{money(box['amount']):>11}  {codes}")
    if not by_ticker:
        print("  （無）")

    print("\n【段 4】各型態的代表公司（拿去 tools/segment.py 看 instance 用）")
    for p, box in rows[: args.top]:
        print(f"  {p[:78]}")
        print(f"      {', '.join(sorted(box['tickers'])[:10])}")


if __name__ == "__main__":
    main()
