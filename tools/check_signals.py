"""
`config/signals.json` 與 `config/scoring.json` 的體檢（零 SEC 請求，離線跑）。

轉折點分頁把設定當程式在用：訊號指到哪個指標、門檻怎麼分級，全部在設定層。
設定寫錯不會爆炸，只會安靜地給出錯的判讀 —— 指到不存在的指標就整頁 n/a、
門檻順序寫反就永遠落在第一段。這支把那幾種寫錯攔在提交前。

    python tools/check_signals.py
"""
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
SYNTAX_WORDS = {"avg", "t"}


def main() -> int:
    xmap = json.loads((ROOT / "config/xbrl_zh_map.json").read_text(encoding="utf-8"))
    sig = json.loads((ROOT / "config/signals.json").read_text(encoding="utf-8"))
    score = json.loads((ROOT / "config/scoring.json").read_text(encoding="utf-8"))

    concepts = {c["id"] for c in xmap["concepts"]}
    metrics = [m["id"] for m in xmap["derived"]]
    metric_set = set(metrics)
    order = {mid: i for i, mid in enumerate(metrics)}
    errs: list[str] = []

    # 1. 指標公式只能引用「科目」或「排在自己前面的指標」。
    #    Excel 端的參照解析器是逐列往下建的，引用後面的列會解不出來，整列變 n/a；
    #    網頁端的求值器同樣照陣列順序算。順序寫錯兩邊一起錯，而且不會報錯。
    for m in xmap["derived"]:
        for ident in set(IDENT.findall(m["formula"])) - SYNTAX_WORDS:
            if ident in concepts:
                continue
            if ident not in metric_set:
                errs.append(f"指標 {m['id']} 的公式引用了不存在的 id：{ident}")
            elif order[ident] >= order[m["id"]]:
                errs.append(f"指標 {m['id']} 引用了排在它後面的指標 {ident}（Excel 會解不出參照）")

    # 2. 訊號指到的指標要存在
    layer_ids = {l["id"] for l in sig["layers"]}
    seen: set[str] = set()
    for s in sig["signals"]:
        if s["id"] in seen:
            errs.append(f"訊號 id 重複：{s['id']}")
        seen.add(s["id"])
        for key in ("metric", "metric_annual"):
            mid = s.get(key)
            if mid and mid not in metric_set:
                errs.append(f"訊號 {s['id']} 的 {key} 指到不存在的指標：{mid}")
        if s["layer"] not in layer_ids:
            errs.append(f"訊號 {s['id']} 的 layer「{s['layer']}」不在 layers 裡")

        # 3. 門檻必須由小到大，且只有最後一段可以沒有 lt。
        #    classify() 是由前往後找第一個 value < lt，順序寫反的話後面幾段永遠碰不到
        bands = s.get("bands") or []
        lts = [b.get("lt") for b in bands]
        if bands:
            if any(v is None for v in lts[:-1]):
                errs.append(f"訊號 {s['id']}：只有最後一段可以省略 lt")
            if lts[-1] is not None:
                errs.append(f"訊號 {s['id']}：最後一段必須省略 lt（以上全收）")
            fin = [v for v in lts if v is not None]
            if fin != sorted(fin):
                errs.append(f"訊號 {s['id']}：bands 的 lt 必須由小到大 —— {fin}")
        elif not s.get("state_override"):
            errs.append(f"訊號 {s['id']}：沒有 bands 就要寫 state_override（只給背景不判好壞）")

        # 4. 印證關係要指到真的訊號，且不能指到自己
        for p in s.get("pairs_with", []):
            if p == s["id"]:
                errs.append(f"訊號 {s['id']} 的 pairs_with 指到自己")
            elif p not in {x["id"] for x in sig["signals"]}:
                errs.append(f"訊號 {s['id']} 的 pairs_with 指到不存在的訊號：{p}")

        # 5. 作廢守門員指到的也要是真的 id
        for key in ("invalid_if_nonpositive", "invalid_if_nonpositive_annual"):
            for g in s.get(key, []):
                if g not in concepts and g not in metric_set:
                    errs.append(f"訊號 {s['id']} 的 {key} 指到不存在的 id：{g}")

    # 6. 滾動四季的指標用了 [t-1]，在年度模式（20-F，一欄＝一整年）會整條消失，
    #    所以指到那種指標的訊號一定要備一個 metric_annual，否則外國發行人整格空白
    for s in sig["signals"]:
        m = next((x for x in xmap["derived"] if x["id"] == s["metric"]), None)
        if m and "[t-1]" in m["formula"] and not s.get("metric_annual"):
            errs.append(f"訊號 {s['id']} 用了含 [t-1] 的 {m['id']}，年度發行人會空白：要補 metric_annual")

    # ── 評分設定 ───────────────────────────────────────
    wsum = 0.0
    seen_dims: set[str] = set()
    scored_metrics: set[str] = set()
    for dim in score["dimensions"]:
        wsum += dim["weight"]
        if dim["id"] in seen_dims:
            errs.append(f"評分構面 id 重複：{dim['id']}")
        seen_dims.add(dim["id"])
        if not dim["items"]:
            errs.append(f"評分構面 {dim['id']} 沒有任何計分項")
        for it in dim["items"]:
            for key in ("metric", "metric_annual"):
                mid = it.get(key)
                if mid and mid not in metric_set:
                    errs.append(f"評分項 {dim['id']}/{it.get('metric')} 的 {key} 指到不存在的指標：{mid}")
            # bad == good 會讓分母為 0，得分整欄變 null 而且不會有人發現
            if it["bad"] == it["good"]:
                errs.append(f"評分項 {it['metric']} 的 bad 與 good 相同，除數為 0")
            if it["weight"] <= 0:
                errs.append(f"評分項 {it['metric']} 的權重必須為正")
            if it["metric"] in scored_metrics:
                errs.append(f"指標 {it['metric']} 在評分裡出現兩次（同一件事會被算兩遍）")
            scored_metrics.add(it["metric"])
            for key in ("invalid_if_nonpositive", "invalid_if_nonpositive_annual"):
                for g in it.get(key, []):
                    if g not in concepts and g not in metric_set:
                        errs.append(f"評分項 {it['metric']} 的 {key} 指到不存在的 id：{g}")
            for rng in it.get("not_meaningful_sic", []):
                if len(rng) != 2 or rng[0] > rng[1]:
                    errs.append(f"評分項 {it['metric']} 的 not_meaningful_sic 區間不合法：{rng}")
                if not it.get("not_meaningful_note"):
                    errs.append(f"評分項 {it['metric']} 標了 not_meaningful_sic 卻沒寫 note —— "
                                f"頁面要能說出為什麼不計這一項")
            # 滾動四季的指標含 [t-1]，年度模式整條消失 → 一定要備 metric_annual
            m = next((x for x in xmap["derived"] if x["id"] == it["metric"]), None)
            if m and "[t-1]" in m["formula"] and not it.get("metric_annual"):
                errs.append(f"評分項 {it['metric']} 用了含 [t-1] 的指標，年度發行人會整項落空："
                            f"要補 metric_annual")

    if abs(wsum - 1.0) > 1e-9:
        errs.append(f"評分構面權重合計必須是 1.0，現在是 {wsum}")
    for name, bands in (("grades", score["grades"]), ("arrow", score["arrow"])):
        lts = [b.get("lt") for b in bands]
        if any(v is None for v in lts[:-1]) or lts[-1] is not None:
            errs.append(f"評分的 {name}：只有最後一段可以省略 lt")
        fin = [v for v in lts if v is not None]
        if fin != sorted(fin):
            errs.append(f"評分的 {name}：lt 必須由小到大 —— {fin}")

    for e in errs:
        print("✗", e)
    n_items = sum(len(d["items"]) for d in score["dimensions"])
    print(f"{len(sig['signals'])} 個訊號、{len(metrics)} 個指標、"
          f"{len(score['dimensions'])} 個評分構面／{n_items} 個計分項；"
          f"{'全部通過' if not errs else str(len(errs)) + ' 個問題'}")
    return 1 if errs else 0


if __name__ == "__main__":
    sys.exit(main())
