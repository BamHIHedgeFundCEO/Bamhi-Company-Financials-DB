#!/usr/bin/env python3
"""
把人工譯文疊回機器譯文上（同一份年報、同一個檔）。

用途：機器批次會把整份重跑一遍，若某幾條先前已經人工翻過（品質較好），
不該被蓋掉。規則是**人工優先、逐項對齊、只覆蓋人工有值的那幾條**，
機器多翻的部分保留。

用法：
  python tools/merge_zh.py <人工譯文檔> <目標譯文檔>
  git show <commit>:config/narrative_zh/X.json > /tmp/manual.json   # 從歷史取回
"""
import io
import json
import sys

SECTIONS = ("business", "mdna", "risk")


def main() -> None:
    if len(sys.argv) != 3:
        print(__doc__)
        raise SystemExit(1)
    manual = json.load(io.open(sys.argv[1], encoding="utf-8"))
    target = json.load(io.open(sys.argv[2], encoding="utf-8"))
    if manual.get("accession") != target.get("accession"):
        raise SystemExit(f"申報書號不同，不能疊：{manual.get('accession')} vs {target.get('accession')}")

    n = 0
    for sid in SECTIONS:
        m, t = manual["sections"].get(sid), target["sections"].get(sid)
        if not m or not t:
            continue
        for field in ("headings", "paragraphs"):
            for i, v in enumerate(m.get(field) or []):
                # 只覆蓋人工有值、且索引在目標範圍內的那幾條
                if v.strip() and i < len(t[field]):
                    if t[field][i] != v:
                        n += 1
                    t[field][i] = v
    src = manual.get("translator", "人工")
    target["translator"] = f"{src} + {target.get('translator', '')}".strip(" +")
    with io.open(sys.argv[2], "w", encoding="utf-8") as f:
        json.dump(target, f, ensure_ascii=False, separators=(",", ":"))
    print(f"疊回 {n} 條 → {sys.argv[2]}")


if __name__ == "__main__":
    main()
