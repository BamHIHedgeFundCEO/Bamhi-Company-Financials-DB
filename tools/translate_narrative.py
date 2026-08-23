#!/usr/bin/env python3
"""
公司簡介敘述段落的繁中翻譯 —— **離線批次**，執行期零成本、零延遲、可人工覆核。

為什麼是離線不是即時：翻譯是這個專案裡唯一會說謊的一層。數字有 companyfacts
可以對帳，散文的翻譯沒有。所以它必須是「跑一次、存下來、可以被人打開看」的產物，
而不是每次查詢即時生成、沒人看過就直接給讀者。

  英文原文  →  tools/translate_out/pending-{TICKER}.json   （--emit）
              ↓ 翻譯（--api 走 Anthropic API，或自己填、或交給別的工具）
  繁中譯文  →  config/narrative_zh/{CIK10}-{ACCESSION}.json （--apply）

執行期由 `web/server/utils/narrative.ts` 讀 `config/narrative_zh/`，
**查不到就顯示英文原文**，不會因為沒翻譯就少一塊內容。

譯文檔綁「申報書號」：公司出新的 10-K 就是新的書號 → 舊譯文自動失效、
頁面退回英文，不會拿去年的翻譯套今年的財報。

用法：
  python tools/translate_narrative.py --emit AAPL,NVDA
  python tools/translate_narrative.py --emit AAPL --api        # 需要 ANTHROPIC_API_KEY
  python tools/translate_narrative.py --apply tools/translate_out/pending-AAPL.json
  python tools/translate_narrative.py --status
"""
import argparse
import io
import json
import os
import sys
import time
import urllib.request

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT_DIR = os.path.join(ROOT, "config", "narrative_zh")
WORK_DIR = os.path.join(ROOT, "tools", "translate_out")
API_BASE = os.environ.get("BAMHI_API", "http://localhost:3000")

SECTIONS = ("business", "mdna", "risk")
MODEL = os.environ.get("BAMHI_TRANSLATE_MODEL", "claude-opus-5")

PROMPT = """你是財經文件翻譯者。把下列美股 10-K 的段落翻成**繁體中文（台灣用語）**。

規則：
1. 逐條翻譯，不要合併、不要摘要、不要加註解、不要省略任何一條
2. 專有名詞（公司名、產品名、法規名如 Regulation S-K）保留英文原文
3. 金融科目用台灣慣用語：revenue→營業收入、gross margin→毛利率、
   supply chain→供應鏈、reportable segment→應報告分部
4. 語氣維持申報文件的中性陳述，不要加強語氣、不要加形容詞
5. 輸出**純 JSON 陣列**，長度與輸入完全相同，第 i 個對應輸入第 i 個，不要有其他文字

輸入（JSON 陣列）：
%s"""


def api_get(path: str):
    req = urllib.request.Request(API_BASE + path, headers={"User-Agent": "bamhi-tools"})
    with urllib.request.urlopen(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8"))


def strings_of(sec: dict, max_paras: int) -> dict:
    return {
        "headings": list(sec.get("headings") or []),
        "paragraphs": list(sec.get("paragraphs") or [])[:max_paras],
        "focus": sec.get("focus") or "",
    }


def emit(tickers: list, max_paras: int) -> list:
    os.makedirs(WORK_DIR, exist_ok=True)
    made = []
    for t in tickers:
        try:
            d = api_get(f"/api/profile?ticker={t}")
        except Exception as e:
            print(f"  {t}: 取不到 profile（{e}）—— 網站有在跑嗎？{API_BASE}")
            continue
        n = d.get("narrative")
        if not n or not n.get("sections"):
            print(f"  {t}: 沒有可翻譯的章節（{'；'.join(d.get('notes') or []) or '無年報'}）")
            continue
        doc = {
            "ticker": t, "cik": d["cik"], "accession": n["accession"],
            "form": n["form"], "reportDate": n["reportDate"],
            "sections": {s["id"]: strings_of(s, max_paras) for s in n["sections"]},
            "zh": {},
        }
        p = os.path.join(WORK_DIR, f"pending-{t}.json")
        with io.open(p, "w", encoding="utf-8") as f:
            json.dump(doc, f, ensure_ascii=False, indent=1)
        cnt = sum(len(v["headings"]) + len(v["paragraphs"]) for v in doc["sections"].values())
        print(f"  {t}: {cnt} 條 → {os.path.relpath(p, ROOT)}")
        made.append(p)
    return made


def translate_api(items: list) -> list:
    """走 Anthropic API。沒有金鑰就直接說，不要靜默失敗"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("--api 需要環境變數 ANTHROPIC_API_KEY")
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": PROMPT % json.dumps(items, ensure_ascii=False)}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.loads(r.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in out.get("content", []))
    text = text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    got = json.loads(text)
    if len(got) != len(items):
        raise SystemExit(f"譯文條數不符（送 {len(items)} 收 {len(got)}）—— 不寫入，避免錯位")
    return got


def fill(path: str, batch: int) -> None:
    with io.open(path, encoding="utf-8") as f:
        doc = json.load(f)
    for sid in SECTIONS:
        sec = doc["sections"].get(sid)
        if not sec:
            continue
        zh = doc["zh"].setdefault(sid, {})
        for field in ("headings", "paragraphs"):
            src = sec[field]
            done = zh.get(field) or []
            if len(done) >= len(src):
                continue
            todo = src[len(done):]
            for i in range(0, len(todo), batch):
                chunk = todo[i:i + batch]
                print(f"    {sid}.{field} {len(done) + i + 1}–{len(done) + i + len(chunk)}"
                      f"／{len(src)} …", flush=True)
                done = done + translate_api(chunk)
                zh[field] = done
                with io.open(path, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, indent=1)
                time.sleep(0.5)
    print(f"  完成 → {os.path.relpath(path, ROOT)}（記得再跑 --apply）")


def apply(path: str) -> None:
    with io.open(path, encoding="utf-8") as f:
        doc = json.load(f)
    zh = doc.get("zh") or {}
    if not any(zh.get(s, {}).get("headings") or zh.get(s, {}).get("paragraphs") for s in SECTIONS):
        print(f"  {os.path.basename(path)}: 還沒有任何譯文，跳過")
        return
    out = {
        "ticker": doc["ticker"], "cik": doc["cik"], "accession": doc["accession"],
        "form": doc.get("form"), "reportDate": doc.get("reportDate"),
        "translator": doc.get("translator") or MODEL,
        "date": time.strftime("%Y-%m-%d"),
        "sections": {},
    }
    for sid in SECTIONS:
        src, t = doc["sections"].get(sid), zh.get(sid)
        if not src or not t:
            continue
        # **長度必須一一對齊**，錯位會讓 A 段的譯文掛在 B 段下面
        out["sections"][sid] = {
            "headings": (t.get("headings") or [])[:len(src["headings"])],
            "paragraphs": (t.get("paragraphs") or [])[:len(src["paragraphs"])],
            "focus": t.get("focus") or "",
        }
    os.makedirs(OUT_DIR, exist_ok=True)
    p = os.path.join(OUT_DIR, f"{doc['cik']}-{doc['accession']}.json")
    with io.open(p, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, separators=(",", ":"))
    n = sum(len(v["headings"]) + len(v["paragraphs"]) for v in out["sections"].values())
    print(f"  {doc['ticker']}: {n} 條 → config/narrative_zh/{os.path.basename(p)}")


def status() -> None:
    if not os.path.isdir(OUT_DIR):
        print("config/narrative_zh/ 還不存在 —— 一條譯文都還沒有")
        return
    files = sorted(x for x in os.listdir(OUT_DIR) if x.endswith(".json"))
    total = 0
    print(f"已翻譯 {len(files)} 份年報")
    for x in files:
        with io.open(os.path.join(OUT_DIR, x), encoding="utf-8") as f:
            d = json.load(f)
        n = sum(len(v["headings"]) + len(v["paragraphs"]) for v in d["sections"].values())
        total += n
        secs = "／".join(f"{k} {len(v['headings'])}標+{len(v['paragraphs'])}段"
                         for k, v in d["sections"].items())
        print(f"  {d['ticker']:6} {d.get('reportDate', ''):11} {n:>4} 條  {secs}"
              f"  [{d.get('translator', '?')}]")
    print(f"共 {total} 條")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", help="代號清單（逗號分隔），抽出待翻譯字串")
    ap.add_argument("--apply", help="把 pending 檔的譯文寫進 config/narrative_zh/")
    ap.add_argument("--apply-all", action="store_true", help="套用 translate_out/ 下全部")
    ap.add_argument("--api", action="store_true", help="用 Anthropic API 翻譯（需 ANTHROPIC_API_KEY）")
    ap.add_argument("--paras", type=int, default=14, help="每個章節最多翻幾段")
    ap.add_argument("--batch", type=int, default=20, help="每次送幾條給模型")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        status()
        return
    if args.emit:
        made = emit([t.strip().upper() for t in args.emit.split(",") if t.strip()], args.paras)
        if args.api:
            for p in made:
                print(f"  翻譯 {os.path.basename(p)}")
                fill(p, args.batch)
                apply(p)
        return
    if args.apply:
        apply(args.apply)
        return
    if args.apply_all:
        for x in sorted(os.listdir(WORK_DIR)):
            if x.startswith("pending-") and x.endswith(".json"):
                apply(os.path.join(WORK_DIR, x))
        return
    ap.print_help()


if __name__ == "__main__":
    main()
