#!/usr/bin/env python3
"""companyfacts 還沒收錄的申報，從 SEC frames API 補回來（離線批次，執行期零 SEC 請求）。

    python tools/frames_patch.py                # 全市場
    python tools/frames_patch.py --ciks 1996862 # 只跑幾家（驗證用）

為什麼要這一份：三大報表一律走 companyfacts（每家公司一份的彙整檔），但 SEC 那邊
彙整會落後。Bunge（BG）2026-07-29 交了 Q2 10-Q、附了 XBRL，兩個月後 companyfacts
還停在 Q1 —— 我們整季營收、淨利、資產負債表全空，而富途早就有 240.41 億。
同一份申報的數字在 **frames API** 裡已經有了（`/api/xbrl/frames/{標籤}/{單位}/{期間}`，
一個標籤、一個期間、全市場一次）。兩者都是 SEC 從 XBRL 解析出來的結構化資料，不是 HTML。

frames 不能在執行期查：它是「全市場一次」的格式，逐家查要每個科目、每個期間打一次，
打破「單一查詢最多 2 次 SEC 請求」。所以離線做，而且只收**真的有缺口**的公司：

  1. 拿 `Assets`（每份 10-Q／10-K 都有）最近 5 季的 instant frames 當哨兵：
     frames 裡有、companyfacts 裡沒有的申報書號 ＝ 缺口
     （每家打一次 companyconcept —— 只有一個標籤，幾 KB）
  2. 缺口公司各打一次 submissions，拿那份申報的表單別與申報日
     （管線要靠 `filed` 擋前瞻附註、靠 `form` 判斷 10-K 的全年值）
  3. 每個已對照的 us-gaap 標籤 × 期間各打一次 frames，只留缺口申報的事實

**frames 的已知限制**：它只收接近日曆期間的單一期間 —— 季度（~91 天）、年度、期末時點，
**沒有年初至今的累計**。現金流量表在 10-Q 只申報累計，所以缺口那一季的現金流量表補不回來
（維持留白）；損益表與資產負債表補得回來。frames 只有一個值給每家公司每個期間（最後申報的
那一份），對缺口申報就是它自己。

執行期（financials.ts）：缺口公司的事實併進 companyfacts 再照原流程跑，同一個標籤同一個
期間已經有同一份申報書號的就不併。
"""
import argparse
import gzip
import io
import json
import os
import sys
import time
import urllib.error
import urllib.request
from collections import defaultdict
from datetime import date

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(ROOT, "config", "xbrl_zh_map.json")
GAP_FORMS = {"10-Q", "10-K", "10-Q/A", "10-K/A", "10-KT", "10-QT"}
MIN_GAP = 0.11   # SEC 限速 10 req/s，與 rateLimiter.ts 同一條


def user_agent() -> str:
    ua = os.environ.get("SEC_USER_AGENT")
    if not ua:
        env = os.path.join(ROOT, "web", ".env")
        if os.path.exists(env):
            for line in open(env, encoding="utf-8"):
                if line.startswith("SEC_USER_AGENT="):
                    ua = line.split("=", 1)[1].strip().strip('"')
    return ua or "BamHI frank940702@gmail.com"


UA = user_agent()
_last = [0.0]


def get_json(url: str):
    """限速 + 429 退避。404 回 None（frames 沒有這個標籤／期間是常態）"""
    for attempt in range(4):
        wait = MIN_GAP - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
        try:
            with urllib.request.urlopen(req, timeout=120) as r:
                raw = r.read()
                if r.headers.get("Content-Encoding") == "gzip":
                    raw = gzip.decompress(raw)
                return json.loads(raw)
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code in (429, 503) and attempt < 3:
                time.sleep(0.5 * 2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError):
            if attempt < 3:
                time.sleep(1 + attempt)
                continue
            raise


def recent_quarters(today: date, n: int) -> list[tuple[int, int]]:
    """最近 n 個已結束的日曆季（含剛結束的那一季：申報才剛開始進來，最容易有缺口）"""
    y, q = today.year, (today.month - 1) // 3   # 本季之前那一季
    if q == 0:
        y, q = y - 1, 4
    out = []
    for _ in range(n):
        out.append((y, q))
        y, q = (y - 1, 4) if q == 1 else (y, q - 1)
    return out[::-1]


def unit_path(unit: str) -> str:
    return {"USD/shares": "USD-per-shares"}.get(unit, unit)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quarters", type=int, default=5, help="往回看幾個日曆季")
    ap.add_argument("--ciks", help="逗號分隔，只跑這幾家（驗證用；不寫正式輸出以外的東西）")
    ap.add_argument("--out", default="config/frames_patch.json")
    args = ap.parse_args()

    m = json.load(open(MAP_PATH, encoding="utf-8"))
    tags: dict[tuple[str, str], str] = {}     # (tag, unit) -> statement
    for c in m["concepts"]:
        for t in c.get("tags") or []:
            tags[(t, c.get("unit", "USD"))] = c.get("statement", "IS")

    qs = recent_quarters(date.today(), args.quarters)
    years = sorted({y for y, _ in qs} | {qs[0][0] - 1})
    print(f"期間：{['CY%dQ%d' % q for q in qs]}；年度：{years}", file=sys.stderr)
    only = {int(x) for x in args.ciks.split(",")} if args.ciks else None

    # ── 1. 哨兵：Assets instant frames → 每家的申報書號 ─────────────────────
    frame_accns: dict[int, set[str]] = defaultdict(set)
    for y, q in qs:
        d = get_json(f"https://data.sec.gov/api/xbrl/frames/us-gaap/Assets/USD/CY{y}Q{q}I.json")
        for x in (d or {}).get("data", []):
            if only is None or x["cik"] in only:
                frame_accns[x["cik"]].add(x["accn"])
    print(f"哨兵：{len(frame_accns):,} 家在 frames 裡有 Assets", file=sys.stderr)

    gaps: dict[int, set[str]] = {}
    for i, cik in enumerate(sorted(frame_accns), 1):
        if i % 500 == 0:
            print(f"  companyconcept {i:,}/{len(frame_accns):,}，缺口 {len(gaps)} 家", file=sys.stderr)
        d = get_json(f"https://data.sec.gov/api/xbrl/companyconcept/CIK{cik:010d}/us-gaap/Assets.json")
        have = {x["accn"] for u in ((d or {}).get("units") or {}).values() for x in u}
        miss = frame_accns[cik] - have
        if miss:
            gaps[cik] = miss
    print(f"缺口：{len(gaps)} 家、{sum(map(len, gaps.values()))} 份申報", file=sys.stderr)

    # ── 2. 缺口申報的表單別與申報日 ────────────────────────────────────────
    filings: dict[str, tuple[str, str]] = {}
    for cik in list(gaps):
        d = get_json(f"https://data.sec.gov/submissions/CIK{cik:010d}.json") or {}
        r = (d.get("filings") or {}).get("recent") or {}
        meta = {a: (f, fd) for a, f, fd in zip(r.get("accessionNumber", []), r.get("form", []),
                                                 r.get("filingDate", []))}
        keep = set()
        for a in gaps[cik]:
            if a in meta and meta[a][0] in GAP_FORMS:
                filings[a] = meta[a]
                keep.add(a)
        if keep:
            gaps[cik] = keep
        else:
            del gaps[cik]   # 不是 10-Q／10-K（S-1、8-K 附的財報）就不補

    # ── 3. 每個標籤 × 期間的 frames，只留缺口申報 ──────────────────────────
    periods_by_stmt = {
        "BS": [f"CY{y}Q{q}I" for y, q in qs],
        "flow": [f"CY{y}Q{q}" for y, q in qs] + [f"CY{y}" for y in years],
    }
    facts: dict[int, dict[str, dict[str, list]]] = defaultdict(lambda: defaultdict(lambda: defaultdict(list)))
    n_req = n_fact = 0
    for (tag, unit), stmt in sorted(tags.items()):
        for per in periods_by_stmt["BS" if stmt == "BS" else "flow"]:
            n_req += 1
            d = get_json(f"https://data.sec.gov/api/xbrl/frames/us-gaap/{tag}/{unit_path(unit)}/{per}.json")
            for x in (d or {}).get("data", []):
                acc = gaps.get(x["cik"])
                if not acc or x["accn"] not in acc:
                    continue
                facts[x["cik"]][tag][unit].append([x.get("start"), x["end"], x["val"], x["accn"]])
                n_fact += 1
        if n_req % 200 < 12:
            print(f"  frames {n_req:,} 次請求，{n_fact:,} 筆事實", file=sys.stderr)

    accns = sorted({f[3] for per in facts.values() for t in per.values() for u in t.values() for f in u})
    idx = {a: i for i, a in enumerate(accns)}
    companies = {}
    for cik, per in sorted(facts.items()):
        companies[str(cik)] = {t: {u: [[s, e, v, idx[a]] for s, e, v, a in rows] for u, rows in us.items()}
                               for t, us in per.items()}
    payload = {
        "version": "1.0",
        "generated": date.today().isoformat(),
        "map_version": m.get("version"),
        "quarters": [f"CY{y}Q{q}" for y, q in qs],
        "note": ("由 tools/frames_patch.py 產生。companyfacts 還沒收錄、frames API 已經有的申報"
                 "（以 Assets 當哨兵比對申報書號）。執行期併進 companyfacts 再照原流程跑。"
                 "frames 沒有年初至今累計，缺口季的現金流量表補不回來。"
                 "filings[i] = [申報書號, 表單別, 申報日]；事實列 = [start, end, val, filings 索引]。"),
        "filings": [[a, *filings[a]] for a in accns],
        "companies": companies,
    }
    if only is not None:
        print(json.dumps({k: v for k, v in payload.items() if k != "companies"}, ensure_ascii=False)[:1500])
        print({c: {t: len(next(iter(us.values()))) for t, us in per.items()} for c, per in companies.items()})
        return 0
    dst = os.path.join(ROOT, args.out)
    with open(dst, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, separators=(",", ":"))
    print(f"{len(companies)} 家、{len(accns)} 份申報、{n_fact:,} 筆事實 → {args.out}"
          f"（{os.path.getsize(dst) / 1024:.0f} KB，{n_req:,} 次 frames 請求）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
