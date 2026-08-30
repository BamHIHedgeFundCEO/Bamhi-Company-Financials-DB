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
6. **數字一律原樣照抄，嚴禁換算單位或改變位數**。
   million 譯成「百萬」、billion 譯成「十億」、thousand 譯成「千」，數字本身不動：
     $224 million   →  224 百萬美元    （不可寫成 224 億、2.24 億）
     $17.8 billion  →  17.8 十億美元   （不可寫成 178 億）
     720,000        →  720,000        （不可寫成 72 萬）
   百分比、日期、股數、每股金額同理，一個位數都不能動。
7. 輸出**純 JSON 陣列**，長度與輸入完全相同，第 i 個對應輸入第 i 個，不要有其他文字

輸入（JSON 陣列）：
%s"""

STRICT_SUFFIX = """

⚠ 上一次翻譯把這些數字換算或寫錯了：%s
這一次**把數字連同單位字原封不動抄過去**，不要做任何換算。"""


def _prompt(items: list, strict: str = "") -> str:
    base = PROMPT % (GLOSSARY, json.dumps(items, ensure_ascii=False))
    return base + (STRICT_SUFFIX % strict if strict else "")


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


def translate_ollama(items: list, strict: str = "") -> list:
    """
    走本機 Ollama —— **零成本、零帳號、資料不離開這台機器**。
    實測 qwen2.5-coder:7b 模型載入後約 0.6 秒/條（一家公司 59 條約 40 秒）。
    """
    payload = {
        "model": OLLAMA_MODEL, "prompt": _prompt(items, strict), "stream": False,
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


def translate_api(items: list, strict: str = "") -> list:
    """走 Anthropic API。沒有金鑰就直接說，不要靜默失敗"""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key:
        raise SystemExit("--api 需要環境變數 ANTHROPIC_API_KEY")
    body = json.dumps({
        "model": MODEL,
        "max_tokens": 8000,
        "messages": [{"role": "user", "content": _prompt(items, strict)}],
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages", data=body,
        headers={"content-type": "application/json", "x-api-key": key,
                 "anthropic-version": "2023-06-01"})
    with urllib.request.urlopen(req, timeout=600) as r:
        out = json.loads(r.read().decode("utf-8"))
    text = "".join(b.get("text", "") for b in out.get("content", []))
    return to_traditional(_parse_array(text, len(items)))


# ── 數字守門員 ────────────────────────────────────────────
#
# 翻譯是這個專案裡唯一會說謊的一層，而它最會說的謊是**單位換算**。
# 實測（qwen3:8b，396 份譯文、2,724 個帶單位的金額）：**24.2% 換算錯誤**，
# 幾乎都是整整差 10 倍或 100 倍 ——
#   AAL  $224 million    → 「224 億美元」   （100 倍）
#   AIR  $2,384.1 million→ 「2,384.1 億美元」（100 倍）
#   NE   $951.7 million  → 「95.17 億美元」 （10 倍）
#   PSA  28.2 million 平方英呎 → 「28.2 億平方英呎」（100 倍）
# 錯了不會報錯、讀者也查不出來，因為旁邊沒有可以對帳的東西。
#
# Prompt 裡叫模型別換算只是「請求」，這裡才是「保證」：譯完逐條驗算，
# 對不上就帶著錯在哪重譯一次，還是對不上就留空 —— 那一條在頁面上顯示英文原文
# 並標「原文」，比一句數字錯掉的中文安全得多。

EN_SCALE = {"thousand": 1e3, "million": 1e6, "billion": 1e9, "trillion": 1e12}
ZH_SCALE = {"兆": 1e12, "百億": 1e10, "十億": 1e9, "億": 1e8,
            "千萬": 1e7, "百萬": 1e6, "十萬": 1e5, "萬": 1e4, "千": 1e3}
NUM_TOKEN = re.compile(r"\d[\d,]*(?:\.\d+)?")
EN_AFTER = re.compile(r"\s*(thousand|million|billion|trillion)\b", re.I)
ZH_AFTER = re.compile(r"\s*(兆|百億|十億|億|千萬|百萬|十萬|萬|千)")
# 沒帶單位字又小於這個值的數字不驗：日期的「31」、季別 Q3（中文寫「第三季」）
# 會一直誤報。校準時這一條把誤報從 5.7% 壓到 3.6%，而抽樣看到的每一筆都是真錯。
MIN_BARE = 1000
NUM_TOL = 0.005


def _num(tok: str):
    try:
        return float(tok.replace(",", ""))
    except ValueError:
        return None


def en_numbers(text: str) -> list:
    """[(量值, 有沒有帶單位字, 原始數字串)]"""
    out = []
    for m in NUM_TOKEN.finditer(text):
        v = _num(m.group(0))
        if v is None:
            continue
        s = EN_AFTER.match(text, m.end())
        raw = m.group(0).replace(",", "")
        out.append((v * EN_SCALE[s.group(1).lower()], True, raw) if s else (v, False, raw))
    return out


def zh_magnitudes(text: str) -> list:
    """
    中文側的量值。三件事一定要處理，否則誤報會蓋過真正的錯：
    ① **複合數詞**：「2億1800萬美元」是 2e8 ＋ 1.8e7，逐段讀會變成兩個不相干的數
    ② **英文單位字殘留**：譯文照抄「€704.3 million」正是我們要的行為，不能反判它錯
    ③ 量詞與數字之間可能有一個空白（「28.2 億」）
    """
    out, i, n = [], 0, len(text)
    while i < n:
        m = NUM_TOKEN.search(text, i)
        if not m:
            break
        v = _num(m.group(0))
        i = m.end()
        if v is None:
            continue
        s = ZH_AFTER.match(text, i)
        if s:
            total, i = v * ZH_SCALE[s.group(1)], s.end()
            while True:                                  # ① 複合數詞
                m2 = NUM_TOKEN.match(text, i)
                if not m2:
                    break
                s2 = ZH_AFTER.match(text, m2.end())
                v2 = _num(m2.group(0)) if s2 else None
                if not s2 or v2 is None or ZH_SCALE[s2.group(1)] >= ZH_SCALE[s.group(1)]:
                    break
                total += v2 * ZH_SCALE[s2.group(1)]
                s, i = s2, s2.end()
            out.append(total)
            continue
        e = EN_AFTER.match(text, i)                      # ② 英文單位字原樣留著
        if e:
            out.append(v * EN_SCALE[e.group(1).lower()])
            i = e.end()
            continue
        out.append(v)
    return out


def numeric_drift(en: str, zh: str) -> list:
    """回傳對不上的英文數字 [(原始串, 量值)]；空清單＝通過。譯文留空視為通過（本來就退回原文）"""
    if not zh or not zh.strip():
        return []
    zmag = zh_magnitudes(zh)
    zraw = {m.group(0).replace(",", "").rstrip(".") for m in NUM_TOKEN.finditer(zh)}
    bad = []
    for mag, scaled, raw in en_numbers(en):
        if not scaled and mag < MIN_BARE and "." not in raw:
            continue
        if any(abs(z - mag) <= max(abs(mag) * NUM_TOL, 1e-9) for z in zmag):
            continue
        if not scaled and raw in zraw:      # 沒帶單位字的，數字原樣出現就算過
            continue
        bad.append((raw, mag))
    return bad


def numeric_gate(engine, items: list, got: list) -> list:
    """逐條驗算 → 帶著錯在哪重譯一次 → 還是不對就留空（頁面顯示英文原文）"""
    out = list(got)
    for i, (en, zh) in enumerate(zip(items, out)):
        bad = numeric_drift(en, zh)
        if not bad:
            continue
        hint = "、".join(r for r, _ in bad[:6])
        try:
            again = engine([en], hint)
        except Exception:
            again = None
        if again and not numeric_drift(en, again[0]):
            out[i] = again[0]
            print(f"      數字重譯成功（{hint}）")
        else:
            out[i] = ""
            print(f"      數字對不上，留英文原文（{hint}）")
    return out


def robust(engine, items: list) -> list:
    """
    條數對不上時**對半拆開重試**，不要原封不動地重送。

    本機模型對某幾批就是會固定多吐一條（實測 KO 那批連送三次都是「送 10 收 11」），
    重試同一批只是把同一個錯誤做三遍。拆到單條就一定是 1 對 1；單條還失敗就
    留空字串 —— 那一條在頁面上顯示英文原文，比硬塞一句對不上的譯文好。

    條數對上之後還要過**數字守門員**（`numeric_gate`）：單位換算錯掉的譯文
    條數是對的、讀起來也順，但金額差 10 倍或 100 倍。
    """
    return numeric_gate(engine, items, _split_retry(engine, items))


def _split_retry(engine, items: list) -> list:
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
        return _split_retry(engine, items[:mid]) + _split_retry(engine, items[mid:])


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
            done = list(zh.get(field) or [])
            done += [""] * (len(src) - len(done))
            done = done[:len(src)]
            # **依索引續跑，不是依長度**。守門員擋下的那一條會被清成空字串留在
            # 原位（不是砍掉尾巴），只看 len(done) 的話它永遠不會被重譯。
            todo_idx = [i for i, x in enumerate(done) if not x]
            if not todo_idx:
                continue
            chunks = [todo_idx[i:i + batch] for i in range(0, len(todo_idx), batch)]
            # 併發只在「同一個欄位的多個批次」之間做 —— 批次之間沒有順序相依，
            # 結果照原順序接回去即可。實測 8GB 顯卡上 2 執行緒約 1.6 倍，
            # 4 執行緒沒有更快（模型與 KV cache 已經吃滿顯存）
            for g in range(0, len(chunks), jobs):
                group = chunks[g:g + jobs]
                flat = [i for c in group for i in c]
                print(f"    {sid}.{field} 第 {flat[0] + 1}–{flat[-1] + 1} 條"
                      f"（待譯 {len(todo_idx)}／全 {len(src)}）…", flush=True)
                texts = [[src[i] for i in c] for c in group]
                if len(group) == 1:
                    got = [robust(engine, texts[0])]
                else:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=len(group)) as ex:
                        got = list(ex.map(lambda t: robust(engine, t), texts))
                for c, r in zip(group, got):
                    for i, x in zip(c, r):
                        done[i] = x
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


def _blank_output(doc: dict, holes: dict) -> None:
    """把 config/narrative_zh/ 那一份的同樣索引清成空字串（頁面顯示英文原文）"""
    out = done_key(doc.get("cik", ""), doc.get("accession", ""))
    if not os.path.exists(out):
        return
    try:
        with io.open(out, encoding="utf-8") as f:
            d = json.load(f)
    except Exception:
        return
    for sid, fields in holes.items():
        sec = d.get("sections", {}).get(sid)
        if not sec:
            continue
        for field, idxs in fields.items():
            arr = sec.get(field) or []
            for i in idxs:
                if i < len(arr):
                    arr[i] = ""
            sec[field] = arr
    with io.open(out, "w", encoding="utf-8") as f:
        json.dump(d, f, ensure_ascii=False, separators=(",", ":"))


def revalidate(paths: list, write: bool = True) -> tuple:
    """
    把**已經翻好**的譯文用數字守門員掃一遍，對不上的清成空字串。

    守門員是後來才加的，先前翻好的 1,137 份沒有經過它。整批重翻要好幾個鐘頭，
    但實測只有 3.6% 的段落有問題 —— 清掉那 3.6% 讓下一輪 `--all` 補回來就好，
    正確的譯文不必重做。清空而不是砍掉，是因為 `fill()` 依索引續跑，
    空字串留在原位才找得回來（砍掉會讓後面每一條都錯位）。
    """
    bad_items = bad_files = 0
    for path in paths:
        try:
            with io.open(path, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            continue
        zh, hit, holes = doc.get("zh") or {}, 0, {}
        for sid in SECTIONS:
            sec, t = doc["sections"].get(sid), zh.get(sid)
            if not sec or not t:
                continue
            for field in ("headings", "paragraphs"):
                src, got = sec[field], list(t.get(field) or [])
                for i in range(min(len(src), len(got))):
                    if got[i] and numeric_drift(src[i], got[i]):
                        got[i] = ""
                        holes.setdefault(sid, {}).setdefault(field, []).append(i)
                        hit += 1
                t[field] = got
        if hit:
            bad_files += 1
            bad_items += hit
            if write:
                with io.open(path, "w", encoding="utf-8") as f:
                    json.dump(doc, f, ensure_ascii=False, indent=1)
                # 已產出的譯文檔**同樣位置清空就好，不要整份刪掉**：
                # 刪掉的話那家在網站上會整個章節退回英文，而問題只有幾條。
                # 清空之後 already_done 會看到「非空條數 < 原文條數」→ 下一輪自動補譯。
                _blank_output(doc, holes)
            print(f"  {doc['ticker']}: 清掉 {hit} 條數字對不上的譯文")
    print(f"revalidate：{len(paths)} 份裡 {bad_files} 份有問題，共清掉 {bad_items} 條")
    return bad_files, bad_items


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


class BatchAbort(Exception):
    """整批中止 —— **不能用 SystemExit**：run_batch 對每一家包了 `except SystemExit`
    來吸收單一公司的錯誤，熔斷器丟 SystemExit 會被同一個 handler 接住，
    結果是「已中止」印了 230 次然後整批繼續跑完。實測 2026-08 那一輪就是這樣。"""


PROBE_TICKER = "AAPL"


def site_alive() -> bool:
    """
    網站到底活著沒有 —— **用硬事實判斷，不要靠連續幾家沒章節去猜**。

    「連續 N 家抽不到章節」有兩種完全不同的原因：
      ① 母體尾巴本來就有一整段沒有 10-K 的小公司（真的沒有年報）
      ② 網站暫時抽不出 narrative，卻回報成「無年報」
    猜錯的代價不對稱：把 ② 當成 ① 會安靜地漏掉幾百家 —— 實測 252 家，
    其中 TFSL／BBSI／DAKT／SENEA／SAFE 事後重查全部都有年報。
    所以碰到門檻就去問一家一定有年報的公司，答案是事實不是推測。
    """
    try:
        d = api_get(f"/api/profile?ticker={PROBE_TICKER}")
    except Exception:
        return False
    n = d.get("narrative")
    return bool(n and n.get("sections"))


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
    no_section: list = []
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
                no_section.append(t)
                if empty >= 15:
                    if site_alive():
                        print(f"  （連續 {empty} 家沒有章節，但 {PROBE_TICKER} 正常 —— "
                              f"網站活著，這一段是真的沒有年報，繼續）")
                        empty = 0
                    else:
                        raise BatchAbort(
                            f"連續 {empty} 家抽不到章節，連 {PROBE_TICKER} 也抽不到 —— "
                            f"網站（{API_BASE}）掛了，已中止")
                continue
            empty = 0
            if not engine:
                continue
            fill(made[0], args.batch, engine, tag, max(1, args.jobs))
            apply(made[0])
            ok += 1
        except BatchAbort:
            raise
        except SystemExit as e:
            print(f"  {t} 中止：{e}")
            failed += 1
        except Exception as e:
            print(f"  {t} 失敗：{type(e).__name__} {e}")
            failed += 1
    mins = (time.time() - t0) / 60
    print(f"\n完成 {ok}、跳過 {skipped}（已翻過）、失敗 {failed}，共 {mins:.0f} 分鐘")
    if no_section:
        # 這份名單裡混著「真的沒有 10-K」與「當下網站抽不出來」，肉眼分不出來。
        # 直接印成可以貼回去重跑的形式，重跑一次就知道是哪一種。
        print(f"沒有可翻譯章節的 {len(no_section)} 家（要確認的話重跑一次）：")
        print("  --emit " + ",".join(no_section))


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
    ap.add_argument("--revalidate", action="store_true",
                    help="用數字守門員重掃既有譯文，對不上的清空（之後再跑 --all 補回來）")
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
    if args.revalidate:
        revalidate(sorted(os.path.join(WORK_DIR, x) for x in os.listdir(WORK_DIR)
                          if x.startswith("pending-") and x.endswith(".json")))
        return
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
