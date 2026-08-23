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
  python tools/translate_narrative.py --emit AAPL,NVDA --ollama  # 免費，跑本機模型
  python tools/translate_narrative.py --emit AAPL --api          # 需 ANTHROPIC_API_KEY（要錢）
  python tools/translate_narrative.py --apply tools/translate_out/pending-AAPL.json
  python tools/translate_narrative.py --status
"""
import argparse
import io
import json
import os
import re
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
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("BAMHI_OLLAMA_MODEL", "qwen2.5-coder:7b")

GLOSSARY = ("component→零組件、gross margin→毛利率、net sales→淨銷售額、revenue→營業收入、"
            "macroeconomic→總體經濟、supply chain→供應鏈、outsourcing partner→委外夥伴、"
            "reportable segment→應報告分部、the Company→本公司、"
            "intellectual property→智慧財產權、inventory→存貨、software→軟體、"
            "information technology→資訊科技、carrier→電信業者")

PROMPT = """你是財經文件翻譯者。把下列美股 10-K 的段落翻成**繁體中文（台灣用語）**。

規則：
1. 逐條翻譯，不要合併、不要摘要、不要加註解、不要省略任何一條
2. 專有名詞（公司名、產品名、法規名如 Regulation S-K）保留英文原文
3. 用語對照：%s
4. 語氣維持申報文件的中性陳述，不要加強語氣、不要加形容詞
5. **一律使用繁體字**，不得出現任何簡體字
6. 輸出**純 JSON 陣列**，長度與輸入完全相同，第 i 個對應輸入第 i 個，不要有其他文字

輸入（JSON 陣列）：
%s"""


def _prompt(items: list) -> str:
    return PROMPT % (GLOSSARY, json.dumps(items, ensure_ascii=False))


def _parse_array(text: str, n: int) -> list:
    """本機模型很愛加 code fence 與前言，寬鬆一點剖析，但**條數一定要對**"""
    t = text.strip()
    if "```" in t:
        t = t.split("```")[1]
        t = t[4:] if t.startswith("json") else t
    i, j = t.find("["), t.rfind("]")
    if i >= 0 and j > i:
        t = t[i:j + 1]
    got = json.loads(t)
    if not isinstance(got, list) or len(got) != n:
        raise ValueError("譯文條數不符（送 %d 收 %s）" % (n, len(got) if isinstance(got, list) else "非陣列"))
    return [str(x) for x in got]


_cc = None
_cc_warned = [False]

# 對岸用語 → 台灣用語。**每一條都必須是冪等的**（套兩次結果不變），
# 因為模型的輸出本來就大半是正確的繁體，正規化會一再跑過同一段文字。
TW_FIXES = [
    (re.compile(r"(?<!演)算法"), "演算法"),
    (re.compile(r"合作伙伴"), "合作夥伴"),
    (re.compile(r"軟件"), "軟體"),
    (re.compile(r"硬件"), "硬體"),
    (re.compile(r"網絡"), "網路"),
    (re.compile(r"信息技術"), "資訊科技"),
    (re.compile(r"數據中心"), "資料中心"),
    (re.compile(r"服務器"), "伺服器"),
    (re.compile(r"芯片"), "晶片"),
    (re.compile(r"視頻"), "影片"),
    (re.compile(r"平臺"), "平台"),
    (re.compile(r"存貨週轉"), "存貨週轉"),
]


def to_traditional(xs: list) -> list:
    """
    簡→繁正規化。**這一步不能省**：本機 7B 模型會混進簡體字，逐條用肉眼抓不出來。

    ⚠️ 用 `s2t`（逐字）**不能用 `s2twp`**。`s2twp` 帶詞彙表，套在「本來就是繁體」的
    文字上會二次轉換 —— 實測它把模型正確產出的「演算法」變成「演演算法」，
    NVDA 那份出現 5 次。逐字轉換對已是繁體的字是恆等的，安全。
    台灣用語的修正改用上面那張手工檢查過的表，每條都可重複套用。
    """
    global _cc
    if _cc is None:
        try:
            from opencc import OpenCC
            _cc = OpenCC("s2t")
        except ImportError:
            _cc = False
    if _cc is False:
        if not _cc_warned[0]:
            print("    ⚠ 沒有 opencc，簡繁正規化跳過。裝法：pip install opencc-python-reimplemented")
            _cc_warned[0] = True
        return xs
    out = []
    for x in xs:
        y = _cc.convert(x)
        for pat, rep in TW_FIXES:
            y = pat.sub(rep, y)
        out.append(y)
    return out


# opencc 把這些字當成簡體，但它們在台灣是標準寫法（平台不寫平臺），
# 上面的 TW_FIXES 還刻意產生它們 —— 不排除的話警示會永遠亮著
TW_OK = set("台")


def simplified_left(x: str) -> set:
    """還殘留哪些簡體字。判準是「這個字經逐字轉換後會變成別的字」，不是靠字表猜"""
    if not _cc:
        return set()
    return {c for c in x if c not in TW_OK and _cc.convert(c) != c}


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


def translate_ollama(items: list) -> list:
    """
    走本機 Ollama —— **零成本、零帳號、資料不離開這台機器**。
    實測 qwen2.5-coder:7b 模型載入後約 0.6 秒/條（一家公司 59 條約 40 秒）。
    """
    body = json.dumps({
        "model": OLLAMA_MODEL, "prompt": _prompt(items), "stream": False,
        "options": {"temperature": 0.2, "num_predict": 6000},
    }).encode("utf-8")
    req = urllib.request.Request(OLLAMA + "/api/generate", data=body,
                                 headers={"content-type": "application/json"})
    try:
        raw = urllib.request.urlopen(req, timeout=1800).read().decode("utf-8")
    except OSError as e:
        raise SystemExit("連不上 Ollama（%s）：%s。先確認 ollama 有在跑" % (OLLAMA, e))
    return to_traditional(_parse_array(json.loads(raw).get("response", ""), len(items)))


def translate_api(items: list) -> list:
    """走 Anthropic API。沒有金鑰就直接說，不要靜默失敗"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("--api 需要環境變數 ANTHROPIC_API_KEY")
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": _prompt(items)}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.loads(r.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in out.get("content", []))
    return to_traditional(_parse_array(text, len(items)))


def robust(engine, items: list) -> list:
    """
    條數對不上時**對半拆開重試**，不要原封不動地重送。

    本機模型對某幾批就是會固定多吐一條（實測 KO 那批連送三次都是「送 10 收 11」），
    重試同一批只是把同一個錯誤做三遍。拆到單條就一定是 1 對 1；單條還失敗就
    留空字串 —— 那一條在頁面上顯示英文原文，比硬塞一句對不上的譯文好。
    """
    try:
        return engine(items)
    except (ValueError, json.JSONDecodeError):
        if len(items) == 1:
            try:
                return engine(items)
            except Exception:
                print("      放棄一條（維持英文原文）")
                return [""]
        mid = len(items) // 2
        return robust(engine, items[:mid]) + robust(engine, items[mid:])


def fill(path: str, batch: int, engine, tag: str) -> None:
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
                print(f"    {sid}.{field} {len(done) + 1}–{len(done) + len(chunk)}"
                      f"／{len(src)} …", flush=True)
                done = done + robust(engine, chunk)
                zh[field] = done
                doc["translator"] = tag
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
    left = set()
    for v in out["sections"].values():
        for k in ("headings", "paragraphs"):
            for x in v[k]:
                left |= simplified_left(x)
    warn = f"  ⚠ 仍有簡體字 {''.join(sorted(left))}" if left else ""
    print(f"  {doc['ticker']}: {n} 條 → config/narrative_zh/{os.path.basename(p)}{warn}")


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
    ap.add_argument("--api", action="store_true",
                    help="用 Anthropic API 翻譯（需 ANTHROPIC_API_KEY，要錢）")
    ap.add_argument("--ollama", action="store_true",
                    help="用本機 Ollama 翻譯（免費、免帳號、資料不出機器）")
    ap.add_argument("--model", help="覆寫模型名稱")
    ap.add_argument("--paras", type=int, default=14, help="每個章節最多翻幾段")
    ap.add_argument("--batch", type=int, default=20, help="每次送幾條給模型")
    ap.add_argument("--status", action="store_true")
    args = ap.parse_args()

    if args.status:
        status()
        return
    if args.model:
        globals()["MODEL"] = args.model
        globals()["OLLAMA_MODEL"] = args.model
    engine = translate_ollama if args.ollama else (translate_api if args.api else None)
    tag = (OLLAMA_MODEL + "（本機）") if args.ollama else MODEL
    if args.emit:
        made = emit([t.strip().upper() for t in args.emit.split(",") if t.strip()], args.paras)
        if engine:
            for p in made:
                print("  翻譯 %s（%s）" % (os.path.basename(p), tag))
                fill(p, args.batch, engine, tag)
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
