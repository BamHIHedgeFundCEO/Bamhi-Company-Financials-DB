#!/usr/bin/env python3
"""「這家公司在報表上印的是哪一個標籤」的離線盤點（零 SEC 請求）。

    python tools/face_tags.py 2025q3.zip … 2026q2.zip --out config/face_tags.json

為什麼要這一份：`tags` 是優先序，逐期取第一個有值的。營收的前兩名是
`RevenueFromContractWithCustomer…`（ASC 606 客戶合約收入），第三名才是 `Revenues`。
對大多數公司兩者相等，但 606 **只涵蓋客戶合約**，以下幾類公司的營收大半不在裡面：

  - 商品貿易：ADM／Bunge 的穀物買賣多半是 ASC 815 衍生性合約 → 606 只有三成
  - 租賃：REIT 的租金是 ASC 842 → AvalonBay 的 606 收入只有總營收的 1/400
  - 保險：保費是 ASC 944
  - 塔台：American Tower 的租金收入 97 億、606 收入 9 億

這些公司在損益表上印的是 `Revenues`（合計），606 那個標籤只在附註的收入拆分表。
優先序照抄的話我們拿到的是**附註裡的一個分項**，而且看起來完全正常 ——
AVB 的營收顯示 0.01B，毛利率、週轉率、每股營收全部跟著錯。

本機 2,870 份 companyfacts 實測：同時有兩個標籤的 630 家裡，245 家 `Revenues`
比 606 大超過 1%，其中 217 家的損益表上印的是 `Revenues`。**但不能一律改成
`Revenues` 優先**，反方向也有 18 家：606 標籤才是損益表上那一行、`Revenues`
是別的東西（Verra Mobility、Cooper-Standard、Kopin…）。也不能「取兩者較大」：
Republic Services／Hasbro 只在表上印 606 標籤，`Revenues` 卻大了 14–15%。
**分得出來的只有「報表上印的是哪一個」**，那是 pre.txt 才有的事實。

只盤點 map 裡帶 `face_preferred_tags` 的科目，只收「偏好標籤確實印在該張報表上」
的公司。輸出的是該科目**所有**印在表上的已對照標籤 —— 執行期要知道 606 那個
是否也在表上（兩個都在 → 偏好標籤是合計那一行，取較大者）。

這份表**不產生任何數字**，只決定兩個申報值裡取哪一個。
"""
import argparse
import csv
import io
import json
import os
import sys
import zipfile
from collections import defaultdict

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(ROOT, "config/xbrl_zh_map.json")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("zips", nargs="+", help="DERA FSDS 季度 zip")
    ap.add_argument("--out", default="config/face_tags.json")
    args = ap.parse_args()

    with open(MAP_PATH, encoding="utf-8") as f:
        m = json.load(f)
    # 只看有偏好標籤的科目；tag → [(科目, 報表別)]
    tag2c: dict[str, list[tuple[str, str]]] = defaultdict(list)
    preferred: dict[str, set[str]] = {}
    for c in m["concepts"]:
        pref = c.get("face_preferred_tags") or []
        comps = c.get("face_components") or []
        if not pref and not comps:
            continue
        # 組成項只要第一項在表上就要記（執行期靠它判斷「表上印的是 Cash」）
        preferred[c["id"]] = set(pref) | set(comps[:1])
        for t in list(c.get("tags") or []) + [t for t in comps if t not in (c.get("tags") or [])]:
            tag2c[t].append((c["id"], c.get("statement")))

    face = defaultdict(lambda: defaultdict(set))   # cik -> cid -> {tag}
    for z in args.zips:
        print(f"→ {z}", file=sys.stderr)
        with zipfile.ZipFile(z) as zf:
            sub = {}
            with zf.open("sub.txt") as fh:
                for r in csv.DictReader(io.TextIOWrapper(fh, "utf-8", errors="replace"),
                                        delimiter="\t"):
                    if r["form"] in ("10-K", "10-Q", "20-F", "10-K/A", "10-Q/A"):
                        sub[r["adsh"]] = str(int(r["cik"]))
            with zf.open("pre.txt") as fh:
                for r in csv.DictReader(io.TextIOWrapper(fh, "utf-8", errors="replace"),
                                        delimiter="\t"):
                    hits = tag2c.get(r["tag"])
                    if not hits:
                        continue
                    cik = sub.get(r["adsh"])
                    if cik is None:
                        continue
                    for cid, stmt in hits:
                        if r["stmt"] == stmt:
                            face[cik][cid].add(r["tag"])

    tags = sorted({t for v in face.values() for s in v.values() for t in s})
    idx = {t: i for i, t in enumerate(tags)}
    out, n = {}, defaultdict(int)
    for cik, per in face.items():
        row = {}
        for cid, ts in per.items():
            if ts & preferred[cid]:
                row[cid] = ",".join(str(idx[t]) for t in sorted(ts))
                n[cid] += 1
        if row:
            out[cik] = row

    payload = {
        "version": "1.0",
        "generated": __import__("datetime").date.today().isoformat(),
        "source": [os.path.basename(p) for p in args.zips],
        "map_version": m.get("version"),
        "note": ("由 tools/face_tags.py 產生。只收 map 裡帶 face_preferred_tags 的科目，"
                 "且偏好標籤確實印在該張報表上（pre.txt）的公司。值是該科目所有印在表上的"
                 "已對照標籤（tags 陣列的索引）。執行期據此決定兩個申報值取哪一個，"
                 "不產生任何數字。"),
        "tags": tags,
        "companies": dict(sorted(out.items())),
    }
    dst = os.path.join(ROOT, args.out) if not os.path.isabs(args.out) else args.out
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(out):,} 家公司 → {args.out}")
    for cid, k in sorted(n.items()):
        print(f"  {cid:20} {k:,}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
