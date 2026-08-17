#!/usr/bin/env python3
"""
分部資料（segment）診斷工具 —— 迭代 config/segment_axes.json 的核心。

companyfacts API **不含維度資料**，分部數字只存在於申報的 XBRL instance 檔裡。
這支工具直接抓 instance、拆 context 的 explicitMember，回答三件事：

  1. 這家公司揭露了哪些「軸」（Axis）？哪些是標準軸、哪些是自訂軸？
  2. 每個軸底下有哪些「成員」（Member）？跨期有沒有改名？
  3. 帶分部維度的事實裡，有哪些科目（營收/成本/營業利益/…）？

用法：
  python tools/segment.py AAPL                    # 單檔，看最新一份申報
  python tools/segment.py AAPL NVDA JPM           # 多檔比對，找系統性差異
  python tools/segment.py AAPL --filings 8        # 往前抓 8 份，驗證成員改名
  python tools/segment.py --scan tickers.txt      # 批次（每行一個 ticker）
  python tools/segment.py AAPL --json out.json    # 輸出原始結果供設定層參考
  python tools/segment.py AAPL --values           # 連同數值一起印（驗算用）

不需要跑伺服器，直接打 SEC。設計對齊 tools/coverage.py。
"""
import argparse
import io
import json
import os
import re
import sys
import time
import urllib.request
from collections import defaultdict, OrderedDict
from xml.etree import ElementTree as ET

# Windows 主控台預設 cp950，強制 UTF-8 才能印中文與符號
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
else:
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")

UA = os.environ.get("SEC_USER_AGENT", "BamHI frank940702@gmail.com")
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAP_PATH = os.path.join(ROOT, "config", "xbrl_zh_map.json")
AXES_PATH = os.path.join(ROOT, "config", "segment_axes.json")

XBRLI = "{http://www.xbrl.org/2003/instance}"
XBRLDI = "{http://xbrl.org/2006/xbrldi}"

# 標準分類法的 namespace URI 前綴 —— 用來區分「標準軸/成員」與「公司自訂」
STANDARD_NS = (
    "http://fasb.org/us-gaap/",
    "http://fasb.org/srt/",
    "http://xbrl.sec.gov/",          # dei, country, stpr, currency…
    "http://xbrl.ifrs.org/taxonomy/",
    "http://www.xbrl.org/",
)

# 財報類型：優先年報（含 3 年比較數）→ 季報。6-K 常無 XBRL，放最後
FORM_PRIORITY = ("10-K", "20-F", "40-F", "10-Q", "6-K")

_last_req = 0.0


def fetch(url: str, as_json: bool = True):
    """SEC 合規：帶 User-Agent + 最小間隔 110ms（官方上限 10 req/s）。"""
    global _last_req
    gap = time.time() - _last_req
    if gap < 0.11:
        time.sleep(0.11 - gap)
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=90) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip":
            import gzip
            raw = gzip.decompress(raw)
    _last_req = time.time()
    return json.loads(raw) if as_json else raw


def resolve_cik(ticker: str) -> str | None:
    data = fetch("https://www.sec.gov/files/company_tickers.json")
    t = ticker.upper()
    for row in data.values():
        if row["ticker"].upper() in (t, t.replace(".", "-"), t.replace("-", ".")):
            return str(row["cik_str"]).zfill(10)
    return None


def pick_filings(cik10: str, limit: int) -> tuple[str, list[dict]]:
    """回傳 (公司名, 申報清單)。只取有 XBRL 的財報，年報優先。"""
    sub = fetch(f"https://data.sec.gov/submissions/CIK{cik10}.json")
    name = sub.get("name", "")
    r = sub["filings"]["recent"]
    rows = []
    for i, form in enumerate(r["form"]):
        if form not in FORM_PRIORITY:
            continue
        rows.append({
            "form": form,
            "accn": r["accessionNumber"][i],
            "date": r["reportDate"][i],
            "filed": r["filingDate"][i],
            "doc": r["primaryDocument"][i],
            "xbrl": r.get("isXBRL", [1] * len(r["form"]))[i],
        })
    rows.sort(key=lambda x: (FORM_PRIORITY.index(x["form"]), -int(x["filed"].replace("-", ""))))
    return name, rows[:limit]


def instance_url(cik10: str, accn: str) -> str | None:
    """
    用 index.json 找 XBRL instance。

    **不要**用 primaryDocument 的 `.htm` → `_htm.xml` 字串代換：外國發行人
    （TSM/ASML 的 6-K/20-F）常常對不上，會直接 404。
    """
    cikn = str(int(cik10))
    a = accn.replace("-", "")
    base = f"https://www.sec.gov/Archives/edgar/data/{cikn}/{a}"
    try:
        idx = fetch(f"{base}/index.json")
    except Exception:
        return None
    items = idx.get("directory", {}).get("item", [])
    names = [it["name"] for it in items]
    # 1) 標準 inline-XBRL 產出：<doc>_htm.xml
    for n in names:
        if n.endswith("_htm.xml"):
            return f"{base}/{n}"
    # 2) 傳統 instance：xxx-20240930.xml（排除 linkbase / schema）
    cand = [n for n in names
            if n.endswith(".xml")
            and not re.search(r"(_cal|_def|_lab|_pre|_ref)\.xml$", n)
            and not n.endswith(("R.xml", "FilingSummary.xml"))]
    for n in cand:
        if re.search(r"-\d{8}\.xml$", n):
            return f"{base}/{n}"
    return None


def parse_instance(raw: bytes) -> dict:
    """
    拆 instance。回傳 contexts / facts / 前綴對應。

    contexts: {id: {"dims": [(axis_q, member_q)], "start":, "end":}}
    facts:    [(tag_q, context_id, value, decimals)]
    """
    ns = {}
    root = None
    for ev, obj in ET.iterparse(io.BytesIO(raw), events=("start-ns", "start")):
        if ev == "start-ns":
            ns[obj[0]] = obj[1]
        else:
            root = obj
            break
    tree = ET.parse(io.BytesIO(raw))
    root = tree.getroot()
    uri2pre = {v: k for k, v in ns.items()}

    def qname(uri_tag: str) -> str:
        """{uri}local → prefix:local（前綴取自檔案自身宣告，不猜）。"""
        if not uri_tag.startswith("{"):
            return uri_tag
        uri, local = uri_tag[1:].split("}", 1)
        return f"{uri2pre.get(uri, uri)}:{local}"

    def is_standard(uri: str) -> bool:
        return any(uri.startswith(p) for p in STANDARD_NS)

    contexts = {}
    for c in root.iter(XBRLI + "context"):
        cid = c.get("id")
        dims = []
        for m in c.iter(XBRLDI + "explicitMember"):
            axis_raw = m.get("dimension") or ""
            mem_raw = (m.text or "").strip()
            axis_uri = ns.get(axis_raw.split(":")[0], "")
            mem_uri = ns.get(mem_raw.split(":")[0], "")
            dims.append({
                "axis": axis_raw,
                "member": mem_raw,
                "axis_std": is_standard(axis_uri),
                "member_std": is_standard(mem_uri),
            })
        per = c.find(XBRLI + "period")
        start = end = instant = None
        if per is not None:
            s = per.find(XBRLI + "startDate")
            e = per.find(XBRLI + "endDate")
            i = per.find(XBRLI + "instant")
            start = s.text if s is not None else None
            end = e.text if e is not None else None
            instant = i.text if i is not None else None
        contexts[cid] = {"dims": dims, "start": start, "end": end, "instant": instant}

    facts = []
    for el in root:
        tag = el.tag
        if tag.startswith(XBRLI) or "}link" in tag or el.get("contextRef") is None:
            continue
        facts.append({
            "tag": qname(tag),
            "ctx": el.get("contextRef"),
            "unit": el.get("unitRef"),
            "val": (el.text or "").strip(),
        })
    return {"contexts": contexts, "facts": facts, "ns": ns}


def load_concept_index() -> dict:
    """{裸標籤: concept_id} —— 用 xbrl_zh_map 既有的 tags/tags_ifrs 反查。"""
    m = json.load(open(MAP_PATH, encoding="utf-8"))
    idx = {}
    for c in m["concepts"]:
        for t in c.get("tags", []) + c.get("tags_ifrs", []):
            idx.setdefault(t, c["id"])
    return idx


def analyse(ticker: str, n_filings: int, want_values: bool) -> dict:
    cik = resolve_cik(ticker)
    if not cik:
        return {"ticker": ticker, "error": "ticker 查無 CIK"}
    name, filings = pick_filings(cik, n_filings)
    out = {"ticker": ticker, "cik": cik, "name": name, "filings": [],
           "axes": OrderedDict(), "concepts": defaultdict(set), "bytes": 0}
    cidx = load_concept_index()

    for f in filings:
        url = instance_url(cik, f["accn"])
        if not url:
            out["filings"].append({**f, "error": "找不到 instance"})
            continue
        try:
            raw = fetch(url, as_json=False)
        except Exception as e:
            out["filings"].append({**f, "error": f"{type(e).__name__}"})
            continue
        out["bytes"] += len(raw)
        try:
            d = parse_instance(raw)
        except Exception as e:
            out["filings"].append({**f, "error": f"parse: {e}"})
            continue
        out["filings"].append({**f, "kb": round(len(raw) / 1024),
                               "contexts": len(d["contexts"]), "facts": len(d["facts"]),
                               "url": url})

        # 軸 → 成員
        for c in d["contexts"].values():
            for dim in c["dims"]:
                a = out["axes"].setdefault(
                    dim["axis"], {"std": dim["axis_std"], "members": {}})
                a["members"].setdefault(
                    dim["member"], {"std": dim["member_std"], "n": 0})["n"] += 1

        # 帶維度的事實用了哪些科目
        for fact in d["facts"]:
            ctx = d["contexts"].get(fact["ctx"])
            if not ctx or not ctx["dims"]:
                continue
            bare = fact["tag"].split(":")[-1]
            cid = cidx.get(bare)
            if cid:
                for dim in ctx["dims"]:
                    out["concepts"][cid].add(dim["axis"])
    out["concepts"] = {k: sorted(v) for k, v in out["concepts"].items()}
    return out


def report(r: dict, show_members: bool) -> None:
    if r.get("error"):
        print(f"\n=== {r['ticker']} ===  ❌ {r['error']}")
        return
    print(f"\n=== {r['ticker']} · {r['name']} (CIK {r['cik']}) ===")
    for f in r["filings"]:
        if f.get("error"):
            print(f"  ❌ {f['form']:5} {f['date']}  {f['error']}")
        else:
            print(f"  ✓ {f['form']:5} {f['date']}  {f['kb']:>6} KB  "
                  f"{f['contexts']:>5} contexts  {f['facts']:>6} facts")
    std = [(a, v) for a, v in r["axes"].items() if v["std"]]
    cus = [(a, v) for a, v in r["axes"].items() if not v["std"]]
    print(f"  軸：{len(r['axes'])} 個（標準 {len(std)} / 自訂 {len(cus)}）"
          f"　下載 {round(r['bytes']/1048576, 1)} MB")
    for a, v in sorted(std, key=lambda x: -len(x[1]["members"])):
        n_cus = sum(1 for m in v["members"].values() if not m["std"])
        print(f"    {a:<58} {len(v['members']):>3} 成員（自訂 {n_cus}）")
        if show_members:
            for m in sorted(v["members"]):
                print(f"        - {m}")
    if cus:
        print(f"    －－ 自訂軸 {len(cus)} 個：{', '.join(a for a, _ in cus[:6])}"
              + (" …" if len(cus) > 6 else ""))
    if r["concepts"]:
        print("  帶維度的科目：" + "、".join(sorted(r["concepts"])))


def main() -> None:
    ap = argparse.ArgumentParser(description="分部資料診斷")
    ap.add_argument("tickers", nargs="*")
    ap.add_argument("--filings", type=int, default=1, help="每檔往前抓幾份申報（預設 1）")
    ap.add_argument("--scan", help="批次檔，每行一個 ticker")
    ap.add_argument("--json", help="輸出原始結果到檔案")
    ap.add_argument("--members", action="store_true", help="列出每個軸的全部成員")
    ap.add_argument("--values", action="store_true", help="保留（未來印數值驗算）")
    a = ap.parse_args()

    tickers = list(a.tickers)
    if a.scan:
        tickers += [l.strip() for l in open(a.scan, encoding="utf-8") if l.strip()]
    if not tickers:
        ap.error("至少給一個 ticker，或用 --scan")

    results = []
    for t in tickers:
        try:
            r = analyse(t, a.filings, a.values)
        except Exception as e:
            r = {"ticker": t, "error": f"{type(e).__name__}: {e}"}
        results.append(r)
        report(r, a.members)

    if a.json:
        ser = json.loads(json.dumps(results, default=lambda o: sorted(o) if isinstance(o, set) else str(o)))
        json.dump(ser, open(a.json, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
        print(f"\n已寫入 {a.json}")


if __name__ == "__main__":
    main()
