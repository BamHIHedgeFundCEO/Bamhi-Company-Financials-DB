"""
離線批次的時效體檢（零 SEC 請求、零網路，只讀本地檔）。

    python tools/check_staleness.py

`config/` 裡有一半的檔案不是手寫的，是離線批次算出來的：適用性表、同業錨點、
多股別股數、13F。它們各自把輸入的版本蓋在產物裡（`map_version`、`source`、
`percentiles`…），但**沒有任何地方比對過那些戳記**——輸入改版、產物停在舊版，
程式照跑、頁面照顯示，只是數字悄悄變成錯的。

2026-09 就這樣過了一輪：適用性表停在 map v1.22、map 走到 v1.26，新加的 15 個
產業限定科目在表上根本不存在，於是保險經紀的綜合成本率、非銀行放款業者的存放比
全部顯示「該申報卻抓不到」。那不是抓不到，是我們沒重跑。

這支把三件事攔在提交前：
  1. 產物戳記的輸入版本 != 現在的輸入版本（改了 map 沒重跑）
  2. 產物的生成參數 != 定案的參數（跑批次時忘了帶旗標，預設值悄悄換掉判準）
  3. 資料季別落後今天（新的一季出了沒重跑）
判準全部寫在 `config/batch_manifest.json`，這支只負責比對。
"""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

from _digest import config_digest

# Windows 的主控台預設 cp950，勾勾（U+2713）直接讓整支在最後一行印出時炸掉 ——
# 體檢跑完了、結果卻沒印出來，看起來像工具壞了。輸出端強制 UTF-8。
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

ROOT = Path(__file__).resolve().parent.parent
MONTHS = ["JAN", "FEB", "MAR", "APR", "MAY", "JUN",
          "JUL", "AUG", "SEP", "OCT", "NOV", "DEC"]


def load(rel: str):
    return json.loads((ROOT / rel).read_text(encoding="utf-8"))


def dig(obj, path: str, drop_null: bool = False):
    """`a.b`、`a[].b` —— `[]` 表示對清單逐項往下取。缺鍵回 None，不丟例外。"""
    cur, segs = obj, path.split(".")
    for i, seg in enumerate(segs):
        if seg.endswith("[]"):
            cur = cur.get(seg[:-2]) if isinstance(cur, dict) else cur[seg[:-2]]
            rest = ".".join(segs[i + 1:])
            cur = [dig(x, rest) for x in cur] if rest else list(cur)
            break
        cur = cur.get(seg) if isinstance(cur, dict) else cur[seg]
    if drop_null and isinstance(cur, list):
        cur = [v for v in cur if v is not None]
    return cur


def quarter_end(y: int, q: int) -> date:
    m = q * 3
    return date(y, m, 31 if m in (3, 12) else 30)


def latest_quarter(today: date, lag_days: int) -> tuple[int, int]:
    """季末 + lag_days 已經過了的最近一季。"""
    y, q = today.year, (today.month - 1) // 3 + 1
    for _ in range(8):
        if quarter_end(y, q) + timedelta(days=lag_days) <= today:
            return y, q
        q -= 1
        if q == 0:
            y, q = y - 1, 4
    raise RuntimeError("找不到已發布的季別")


def parse_zip_quarter(name: str) -> tuple[int, int]:
    stem = name.split(".")[0].lower()
    y, _, q = stem.partition("q")
    return int(y), int(q)


def parse_dmy(s: str) -> date:
    d, m, y = s.split("-")
    return date(int(y), MONTHS.index(m.upper()) + 1, int(d))


def check_vintage(v: dict, art: dict, today: date, cad: dict, errs: list, out: str) -> str:
    kind = v["kind"]
    if kind == "dera_quarters":
        src = art[v["field"]]
        qs = sorted(parse_zip_quarter(s) for s in src)
        want = latest_quarter(today, cad["dera_lag_days"])
        if len(qs) < v.get("min_quarters", 1):
            errs.append(f"{out}：只用了 {len(qs)} 包 DERA zip，"
                        f"少於 {v['min_quarters']} 包 —— 一季只有 10-Q，"
                        f"年報才揭露的行會被誤判成不適用")
        for a, b in zip(qs, qs[1:]):
            nxt = (a[0] + 1, 1) if a[1] == 4 else (a[0], a[1] + 1)
            if nxt != b:
                errs.append(f"{out}：DERA 季別不連續 {a[0]}q{a[1]} -> {b[0]}q{b[1]}")
        if qs[-1] < want:
            errs.append(f"{out}：最新只到 {qs[-1][0]}q{qs[-1][1]}，"
                        f"{want[0]}q{want[1]} 應該已經發布了 —— 要重跑")
        return f"{qs[0][0]}q{qs[0][1]}–{qs[-1][0]}q{qs[-1][1]}"
    if kind == "quarter_end":
        got = parse_dmy(art[v["field"]])
        wy, wq = latest_quarter(today, cad["filing_lag_days"])
        if got < quarter_end(wy, wq):
            errs.append(f"{out}：期別停在 {art[v['field']]}，"
                        f"{wy}Q{wq} 的申報期限已經過了 —— 要重跑")
        return art[v["field"]]
    if kind == "generated_date":
        got = date.fromisoformat(art[v["field"]])
        wy, wq = latest_quarter(today, v["lag_days"])
        if got < quarter_end(wy, wq):
            errs.append(f"{out}：停在 {got}，{wy}Q{wq} 的季報應該送件完畢了 —— 要重跑")
        return str(got)
    raise RuntimeError(f"未知的 vintage.kind：{kind}")


def main() -> int:
    man = load("config/batch_manifest.json")
    cad = man["cadence"]
    today = date.today()
    errs: list[str] = []
    rows: list[tuple[str, str]] = []

    for b in man["batches"]:
        out = b["out"]
        path = ROOT / out
        if not path.exists():
            errs.append(f"{out}：批次產物不存在（`{b['cmd']}` 沒跑過）")
            continue
        art = json.loads(path.read_text(encoding="utf-8"))
        bad = 0

        # 1. 輸入版本：產物蓋的戳記要等於現在的輸入。
        #    上下游混在同一個檔裡時（scoring 既餵 peer_stats 又收它的產物）比整份版號
        #    會成環，那種改用 hash_keys 只比對真的餵進去的那幾段
        for dep in b.get("depends", []):
            cfg = load(dep["file"])
            if dep.get("hash_keys"):
                live = config_digest(cfg, dep["hash_keys"])
                how = f"（{dep['file']} 的 {'／'.join(dep['hash_keys'])}）"
            else:
                live, how = cfg[dep["field"]], f"（{dep['file']}）"
            got = art.get(dep["stamp"])
            if got != live:
                bad += 1
                errs.append(f"{out}：{dep['stamp']} 停在 {got}，現在是 {live} {how} —— 要重跑")

        # 2. 生成參數：跑的時候忘了帶旗標，預設值會悄悄換掉判準
        for k, want in (b.get("params") or {}).items():
            got = art.get(k)
            if got != want:
                bad += 1
                errs.append(f"{out}：{k} 是 {got}，定案的是 {want} —— "
                            f"重跑時漏了旗標，判準被預設值換掉了")

        # 3. 資料季別
        vin = "—"
        if b.get("vintage"):
            before = len(errs)
            vin = check_vintage(b["vintage"], art, today, cad, errs, out)
            bad += len(errs) - before

        # 4. 同一批產出的檔案要同一季
        for sib in b.get("siblings", []):
            s = load(sib["file"])
            if s.get(sib["field"]) != art.get(sib["field"]):
                bad += 1
                errs.append(f"{sib['file']} 的 {sib['field']} 是 {s.get(sib['field'])}，"
                            f"{out} 是 {art.get(sib['field'])} —— {sib['note']}")

        rows.append((f"{'✓' if not bad else '✗'} {b['id']}",
                     f"{vin}　產於 {art.get('generated') or art.get('version') or '?'}"))

    # 跨檔一致性
    for c in man["cross_checks"]:
        missing = [r["file"] for r in c["refs"] if not (ROOT / r["file"]).exists()]
        if missing:
            # 檔案不存在上面已經報過了，這裡再報一次只是同一件事說兩遍
            rows.append((f"— {c['id']}", f"跳過（{'、'.join(missing)} 不存在）"))
            continue
        vals = [dig(load(r["file"]), r["path"], r.get("drop_null", False))
                for r in c["refs"]]
        if any(v != vals[0] for v in vals[1:]):
            detail = "；".join(f"{r['file']}:{r['path']}={v}"
                              for r, v in zip(c["refs"], vals))
            errs.append(f"[{c['id']}] {detail} —— {c['note']}")
            rows.append((f"✗ {c['id']}", "對不起來"))
        else:
            rows.append((f"✓ {c['id']}", str(vals[0])))

    w = max(len(r[0]) for r in rows)
    for a, b_ in rows:
        print(f"{a.ljust(w)}  {b_}")
    if errs:
        print()
        for e in errs:
            print("✗", e)
    print(f"\n{len(man['batches'])} 個離線批次、{len(man['cross_checks'])} 項跨檔一致性"
          f"；{'全部通過' if not errs else str(len(errs)) + ' 個問題'}"
          f"（判準見 config/batch_manifest.json）")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
