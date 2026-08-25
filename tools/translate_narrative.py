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
  python tools/translate_narrative.py --emit AAPL,NVDA --ollama  # 指定幾家
  python tools/translate_narrative.py --top 500 --ollama         # 市值前 500 大，可續跑
  python tools/translate_narrative.py --all --ollama             # 全母體
  python tools/translate_narrative.py --emit AAPL --ollama --model qwen2.5-coder:7b  # 換模型
  python tools/translate_narrative.py --emit AAPL --api          # 需 ANTHROPIC_API_KEY（要錢）
  python tools/translate_narrative.py --apply tools/translate_out/pending-AAPL.json
  python tools/translate_narrative.py --status
"""
import argparse
import concurrent.futures
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
# 預設打**線上站**，不是本機 dev server。這支是離線批次工具，可能跑好幾天，
# 綁在本機伺服器上等於多一個會半夜掛掉的依賴。打線上還有個副作用是好的：
# 每翻一家就順便把那家的年報解析結果寫進線上的 Blob 快取，真的使用者就不用等。
# 本機開發要改回來：BAMHI_API=http://localhost:3000
API_BASE = os.environ.get("BAMHI_API", "https://bamhi-company-financials.vercel.app")
COVERAGE = os.path.join(ROOT, "tools", "sweep_out", "coverage.jsonl")
UA = os.environ.get("SEC_USER_AGENT", "BamHI frank940702@gmail.com")

SECTIONS = ("business", "mdna", "risk")
MODEL = os.environ.get("BAMHI_TRANSLATE_MODEL", "claude-opus-5")
OLLAMA = os.environ.get("OLLAMA_HOST", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("BAMHI_OLLAMA_MODEL", "qwen3:8b")

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
    # 推理型模型（qwen3 等）會先吐一段 <think>…</think>，裡面常有中括號，
    # 不先剝掉的話下面找 "[" 會抓到推理過程而不是答案
    while "<think>" in text and "</think>" in text:
        text = text[:text.index("<think>")] + text[text.index("</think>") + 8:]
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
    # 台灣寫「佔比／佔營收」；「占卜」是唯一常見的例外
    (re.compile(r"占(?!卜)"), "佔"),
    # 准／準 是兩個字：批准、核准、獲准用「准」，標準、準確用「準」。
    # opencc 不是純逐字轉換、它有詞條，會把「核准」轉成「覈準」、「獲准」轉成
    # 「獲準」（但「批准」又不動）。與其猜它的詞表，不如把轉錯的那幾個轉回來。
    (re.compile(r"覈"), "核"),
    (re.compile(r"批準"), "批准"),
    (re.compile(r"核準"), "核准"),
    (re.compile(r"獲準"), "獲准"),
    (re.compile(r"照準"), "照准"),
    (re.compile(r"準許"), "准許"),
    # 云/雲 也是兩個字：人云亦云用「云」，雲端用「雲」。opencc 的詞條在
    # 「所有云端應用程式」這種句子裡不會轉，只好自己指名幾個複合詞
    (re.compile(r"云端"), "雲端"),
    (re.compile(r"云計算"), "雲端運算"),
    (re.compile(r"云服務"), "雲端服務"),
    (re.compile(r"云原生"), "雲原生"),
    # 了/瞭：「瞭解」是台灣正字要留著，但 opencc 會把動詞後的「了」也轉成「瞭」
    # （「以下討論說明了可能影響…」變成「說明瞭可能」）。只修這個明確的誤轉
    (re.compile(r"說明瞭(?!解)"), "說明了"),
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


# opencc 把這些字當成簡體，但它們在繁體中文裡本來就存在、意思也不同：
#   台（平台，不寫平臺；TW_FIXES 還刻意產生它） 游（游泳 ≠ 遊歷） 里（公里 ≠ 裡面）
#   准（批准／核准；該轉成「準」的情形上面 TW_FIXES 已經處理掉了）
# 不排除的話警示會對正確的字一直亮。這個檢查是提示不是關卡，寧可少報也不要吵。
# 這些字**在繁體中文裡本來就存在**，而且與 opencc 想轉成的那個字意思不同。
# opencc 的 s2t 表把它們全當簡體，所以每次都會誤報。列在這裡不是放行簡體，
# 真正的簡體字（产业务国际经济时间…）一個都不在裡面。看到新的就往下加。
#   台/臺 平台        游/遊 游泳      里/裡 公里      准/準 批准
#   干/幹乾 干擾      后/後 皇后      面/麵 表面      松/鬆 松樹
#   志/誌 志願        制/製 制度      系/係繫 系統    表/錶 表格
#   才/纔 人才        谷/穀 山谷      丑/醜 丑角      卜/蔔 占卜
#   曲/麴 歌曲        沖/衝 沖洗
#   佣/傭 佣金        皂/皁 香皂      托/託 摩托車    岩/巖 岩石      斗/鬥 北斗
TW_OK = set("台游里准干后面松志制系表才谷丑卜曲沖佣皂托岩斗")


def simplified_left(x: str) -> set:
    """還殘留哪些簡體字。判準是「這個字經逐字轉換後會變成別的字」，不是靠字表猜"""
    if not _cc:
        return set()
    return {c for c in x if c not in TW_OK and _cc.convert(c) != c}


def universe() -> list:
    """
    要翻的代號，**依規模由大到小**。先翻大公司是因為流量集中在那裡；
    全母體要跑很久，順序不對等於白等。

    ⚠️ **不要用 SEC 的 `company_tickers.json` 當排序。** 它只有開頭幾千筆大致依
    市值排，尾巴是任意順序 —— 實測美光（MU）排在第 **7,080** 名，夾在幾檔封閉式
    基金中間，所以「前 500 大」整批漏掉了它。`generate-static.ts` 的註解寫
    「該檔已依市值排序」，那是錯的。

    改用 `config/f13/` 的機構持股總市值當規模代理：那是 13F 申報加總出來的
    真實金額，離線就有、零請求，而且排出來的前 25 名與市值排名一致
    （MU 回到第 20 名）。沒有 13F 資料的（多為多股別代號）排在後面。
    """
    uni = []
    with io.open(COVERAGE, encoding="utf-8") as f:
        for line in f:
            uni.append(json.loads(line)["ticker"])

    f13 = os.path.join(ROOT, "config", "f13")
    size = {}
    if os.path.isdir(f13):
        for fn in os.listdir(f13):
            if fn.startswith("_"):
                continue
            try:
                d = json.load(io.open(os.path.join(f13, fn), encoding="utf-8"))
                size[d["ticker"]] = d.get("totalValue") or 0
            except Exception:
                pass
    if not size:
        print("  找不到 config/f13/，改用母體原順序（先跑 tools/f13.py 可得規模排序）")
        return uni
    return sorted(uni, key=lambda t: -size.get(t, -1))


def done_key(cik: str, accession: str) -> str:
    return os.path.join(OUT_DIR, f"{cik}-{accession}.json")


def already_done(ticker: str) -> bool:
    """
    這家是不是已經翻過**這一份**年報。

    比對的是申報書號不是代號 —— 公司出新的 10-K 就該重翻，舊譯文對不上新內容。
    """
    p = os.path.join(WORK_DIR, f"pending-{ticker}.json")
    if not os.path.exists(p):
        return False
    try:
        d = json.load(io.open(p, encoding="utf-8"))
    except Exception:
        return False
    out = done_key(d.get("cik", ""), d.get("accession", ""))
    if not os.path.exists(out):
        return False
    try:
        z = json.load(io.open(out, encoding="utf-8"))
    except Exception:
        return False
    for sid, sec in d.get("sections", {}).items():
        t = z.get("sections", {}).get(sid)
        if not t:
            return False
        for field in ("headings", "paragraphs"):
            if len([x for x in (t.get(field) or []) if x]) < len(sec[field]):
                return False
    return True


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
        # 同一份年報之前翻到一半的話要接下去，不是從頭再翻一遍
        p_old = os.path.join(WORK_DIR, f"pending-{t}.json")
        if os.path.exists(p_old):
            try:
                prev = json.load(io.open(p_old, encoding="utf-8"))
                if prev.get("accession") == doc["accession"]:
                    doc["zh"] = prev.get("zh") or {}
                    doc["translator"] = prev.get("translator")
            except Exception:
                pass
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
    payload = {
        "model": OLLAMA_MODEL, "prompt": _prompt(items), "stream": False,
        # 推理型模型預設會先產一大段思考再回答，翻譯用不到而且拖慢好幾倍。
        # 不支援這個參數的模型會回 400，下面接住後重送一次不帶它的版本。
        "think": False,
        "options": {"temperature": 0.2, "num_predict": 6000},
    }

    def post(p):
        req = urllib.request.Request(
            OLLAMA + "/api/generate", data=json.dumps(p).encode("utf-8"),
            headers={"content-type": "application/json"})
        return urllib.request.urlopen(req, timeout=1800).read().decode("utf-8")

    try:
        raw = post(payload)
    except urllib.error.HTTPError:
        payload.pop("think", None)
        raw = post(payload)
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


def fill(path: str, batch: int, engine, tag: str, jobs: int = 1) -> None:
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
            chunks = [todo[i:i + batch] for i in range(0, len(todo), batch)]
            # 併發只在「同一個欄位的多個批次」之間做 —— 批次之間沒有順序相依，
            # 結果照原順序接回去即可。實測 8GB 顯卡上 2 執行緒約 1.6 倍，
            # 4 執行緒沒有更快（模型與 KV cache 已經吃滿顯存）
            for g in range(0, len(chunks), jobs):
                group = chunks[g:g + jobs]
                print(f"    {sid}.{field} {len(done) + 1}–"
                      f"{len(done) + sum(len(c) for c in group)}／{len(src)} …", flush=True)
                if len(group) == 1:
                    got = [robust(engine, group[0])]
                else:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(group)) as ex:
                        got = list(ex.map(lambda c: robust(engine, c), group))
                for r in got:
                    done = done + r
                zh[field] = done
                doc["translator"] = tag
                with io.open(path, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, indent=1)
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


def run_batch(targets: list, args, engine, tag: str) -> None:
    """
    一個指令跑完一整批，**中途出錯不中斷**、**重跑會接續**。

    單一公司失敗的原因大多與其他公司無關（那份年報抓不到章節、網站剛好重啟、
    模型某批吐不出合法 JSON）。整批中止的話等於前面幾小時的進度都要重來，
    所以每家包一層 try，失敗記下來最後一起報。
    """
    total = len(targets)
    print(f"章節來源 {API_BASE}／翻譯 {OLLAMA_MODEL} @ {OLLAMA}／共 {total} 家")
    skipped = failed = ok = empty = 0
    t0 = time.time()
    for i, t in enumerate(targets, 1):
        # --recheck：不吃 pending 快照的快速路徑，一律重新抽一次章節。
        # 抽取器改進之後（例如新支援了頁首式標題）章節會變多，但 already_done
        # 比對的是上一輪存下來的快照，會誤判成「已翻完」而跳過。
        # 重抽之後 emit 仍會把既有譯文接過去，所以只有新增的條目要翻。
        if not args.redo and not args.recheck and already_done(t):
            skipped += 1
            continue
        el = time.time() - t0
        rate = el / max(1, ok)
        eta = f"，預估剩 {(total - i) * rate / 3600:.1f} 小時" if ok >= 2 else ""
        print(f"[{i}/{total}] {t}（已完成 {ok}、跳過 {skipped}、失敗 {failed}{eta}）", flush=True)
        try:
            made = emit([t], args.paras)
            if not made:
                # 網站掛掉的話每一家都會抽不到字串，然後這支工具會安靜地
                # 「跳過」五百家跑完收工。連續空手太多次就停下來講清楚，
                # 不要讓一場 28 小時的批次變成什麼都沒做
                empty += 1
                if empty >= 15:
                    raise SystemExit(
                        f"連續 {empty} 家都抽不到章節 —— 網站（{API_BASE}）還活著嗎？已中止")
                continue
            empty = 0
            if not engine:
                continue
            fill(made[0], args.batch, engine, tag, max(1, args.jobs))
            apply(made[0])
            ok += 1
        except SystemExit as e:
            print(f"  {t} 中止：{e}")
            failed += 1
        except Exception as e:
            print(f"  {t} 失敗：{type(e).__name__} {e}")
            failed += 1
    mins = (time.time() - t0) / 60
    print(f"\n完成 {ok}、跳過 {skipped}（已翻過）、失敗 {failed}，共 {mins:.0f} 分鐘")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--emit", help="代號清單（逗號分隔），抽出待翻譯字串")
    ap.add_argument("--top", type=int, metavar="N",
                    help="改翻市值前 N 大（依 SEC company_tickers.json 排序）")
    ap.add_argument("--all", action="store_true", help="翻全母體（羅素 3000）")
    ap.add_argument("--jobs", type=int, default=2, help="同時送幾個批次給模型（預設 2）")
    ap.add_argument("--redo", action="store_true", help="已翻過的也重翻（預設跳過）")
    ap.add_argument("--recheck", action="store_true",
                    help="重新抽章節再比對（抽取器改進後用；既有譯文會保留）")
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
    targets = None
    if args.emit:
        targets = [t.strip().upper() for t in args.emit.split(",") if t.strip()]
    elif args.top or args.all:
        targets = universe()
        if args.top:
            targets = targets[:args.top]
    if targets is not None:
        run_batch(targets, args, engine, tag)
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
