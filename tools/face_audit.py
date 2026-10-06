#!/usr/bin/env python3
"""「優先序挑到的標籤」對「報表上印的那一行」的全市場普查（零 SEC 請求）。

    python tools/face_audit.py ~/Downloads/dera/2026q2.zip [--top 15]

為什麼要這一份：營收（2026-10）與營業成本（同月）都踩過同一個坑 —— `tags` 是優先序，
逐期取第一個有值的；但有些公司把排在前面的標籤拿去標附註裡的分項，損益表上的合計
掛在後面的標籤。Caterpillar 2025 Q1 的營業成本取到 2,700 萬（實際 89.65 億）、
Bloom Energy 每季只有幾百萬。每次都是做簡報時撞到才修，修一個科目漏其他科目。
這支一次掃所有科目：

  對每份申報、每個有多個標籤的科目：
    挑到的 ＝ 照 tags 順序第一個在該期有無維度值的標籤（模擬執行期）
    表上的 ＝ 該科目的已對照標籤裡，出現在 pre.txt 該張報表上的
  挑到的不在表上、表上有別的已對照標籤、而且同一期兩者數字差 > 2% → 疑似抓錯

只看申報書的「本期」（ddate ＝ sub.period），流量取最短的期間長度（10-Q 單季、10-K 全年；
現金流量表 10-Q 只有累計，就比累計）。已經有 face_preferred_tags 的科目執行期會自己改正，
另外列一欄「已由規則處理」。
"""
import argparse
import csv
import io
import json
import os
import sys
import zipfile
from collections import defaultdict, Counter

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
csv.field_size_limit(10**9)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zips", nargs="+")
    ap.add_argument("--top", type=int, default=12)
    ap.add_argument("--tol", type=float, default=0.02)
    ap.add_argument("--json", help="把逐筆結果寫成 JSON")
    a = ap.parse_args()

    m = json.load(open(os.path.join(ROOT, "config", "xbrl_zh_map.json"), encoding="utf-8"))
    concepts = [c for c in m["concepts"] if len(c.get("tags") or []) >= 2 and not c.get("internal")]
    tag2c = defaultdict(list)
    for c in concepts:
        for i, t in enumerate(c["tags"]):
            tag2c[t].append((c["id"], i))
    stmt = {c["id"]: c.get("statement") for c in concepts}
    prio = {c["id"]: c["tags"] for c in concepts}
    ruled = {c["id"] for c in concepts if c.get("face_preferred_tags")}
    negate = {c["id"]: set(c.get("negate_tags") or []) for c in concepts}

    hits = []
    checked = Counter()
    for zp in a.zips:
        zf = zipfile.ZipFile(os.path.expanduser(zp))
        sub = {}
        for r in csv.DictReader(io.TextIOWrapper(zf.open("sub.txt"), "utf-8", errors="replace"), delimiter="\t"):
            if r["form"] in ("10-Q", "10-K"):
                sub[r["adsh"]] = (r["cik"], r["name"], r["form"], r["period"])
        face = defaultdict(set)   # adsh -> {(stmt, tag)}
        for r in csv.DictReader(io.TextIOWrapper(zf.open("pre.txt"), "utf-8", errors="replace"), delimiter="\t"):
            if r["adsh"] in sub and r["tag"] in tag2c:
                face[r["adsh"]].add((r["stmt"], r["tag"]))
        vals = defaultdict(dict)  # (adsh, tag) -> {qtrs: value} at period, no dims
        for r in csv.DictReader(io.TextIOWrapper(zf.open("num.txt"), "utf-8", errors="replace"), delimiter="\t"):
            s = sub.get(r["adsh"])
            if not s or r["tag"] not in tag2c or r["segments"] or r["coreg"] or not r["value"]:
                continue
            if r["ddate"] != s[3] or not r["version"].startswith(("us-gaap", "ifrs")):
                continue
            try:
                vals[(r["adsh"], r["tag"])][int(r["qtrs"])] = float(r["value"])
            except ValueError:
                pass

        for adsh, (cik, name, form, period) in sub.items():
            for c in concepts:
                cid = c["id"]
                avail = [t for t in prio[cid] if vals.get((adsh, t))]
                if not avail:
                    continue
                checked[cid] += 1
                picked = avail[0]
                on_face = {t for (st, t) in face[adsh] if st == stmt[cid] and t in prio[cid]}
                if not on_face or picked in on_face:
                    continue
                # 表上有別的已對照標籤 —— 同一期間長度比數字
                fv = None
                for ft in prio[cid]:
                    if ft in on_face and vals.get((adsh, ft)):
                        common = set(vals[(adsh, picked)]) & set(vals[(adsh, ft)])
                        if common:
                            q = min(common)
                            pv, fv = vals[(adsh, picked)][q], vals[(adsh, ft)][q]
                            if picked in negate[cid]:
                                pv = -pv
                            if ft in negate[cid]:
                                fv = -fv
                            face_tag = ft
                            break
                if fv is None:
                    continue
                scale = max(abs(pv), abs(fv))
                if scale == 0 or abs(pv - fv) <= a.tol * scale:
                    continue
                hits.append({"concept": cid, "cik": cik, "name": name, "form": form, "period": period,
                             "picked": picked, "picked_val": pv, "face": face_tag, "face_val": fv,
                             "ratio": (pv / fv) if fv else None, "ruled": cid in ruled})

    by = defaultdict(list)
    for h in hits:
        by[h["concept"]].append(h)
    print(f"{'科目':<26}{'檢查':>8}{'疑似抓錯':>10}{'已由規則處理':>14}   常見組合（挑到 → 表上）")
    for cid in sorted(by, key=lambda k: -len(by[k])):
        hs = by[cid]
        combos = Counter((h["picked"], h["face"]) for h in hs).most_common(3)
        print(f"{cid:<26}{checked[cid]:>8}{len(hs):>10}{('是' if cid in ruled else ''):>14}   "
              + "；".join(f"{p} → {f}（{n}）" for (p, f), n in combos))
    print()
    for cid in sorted(by, key=lambda k: -len(by[k])):
        if cid in ruled:
            continue
        print(f"== {cid}")
        for h in sorted(by[cid], key=lambda h: -abs((h["ratio"] or 0) - 1))[: a.top]:
            r = h["ratio"]
            print(f"   {h['name'][:34]:<34} {h['form']} {h['period']}  挑到 {h['picked']}={h['picked_val']/1e6:,.0f}M"
                  f"  表上 {h['face']}={h['face_val']/1e6:,.0f}M  比值 {r:.3f}" if r is not None else "")
    if a.json:
        json.dump(hits, open(a.json, "w", encoding="utf-8"), ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())
