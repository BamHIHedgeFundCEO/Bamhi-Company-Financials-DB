"""煙霧測試：合成分部 payload → 生成活頁簿 → 讀回驗證分部分頁的硬規則。

鎖住三條容易做錯的不變式：
  1. 上層匯總（Apple 的「產品」含 iPhone/Mac/iPad）不能進合計，否則重複計算
  2. 合計與各項比率一律是 Excel 公式，不是算好的數值
  3. 各公司揭露科目不同 → 列是動態長出來的，不是寫死

python test_segments.py
"""
from io import BytesIO

from openpyxl import load_workbook

from workbook import build_workbook

PERIODS = ["2024-09-28", "2025-09-27"]


def cell(v, verified=True, parent=False):
    return {"value": v, "verified": verified, "isParent": parent}


def member(key, zh, en, vals):
    """vals: {period: {concept: (值, 是否上層)}}"""
    values = {}
    for p, byc in vals.items():
        values[p] = {c: cell(v, parent=par) for c, (v, par) in byc.items()}
    return {"key": key, "zh": zh, "en": en, "values": values}


SEGMENTS = {
    "company": "Test Co", "cik": "0000000001", "ticker": "TEST",
    "configVersion": "1.0", "periods": PERIODS, "warnings": [],
    "axes": [{
        "axis": "srt:ProductOrServiceAxis", "role": "product",
        "zh": "產品與服務", "en": "Product / Service",
        "concepts": ["revenue", "cogs"],
        "members": [
            # 子項：加總 = 合併總額
            member("iphone", "iPhone", "iPhone",
                   {p: {"revenue": (200, False)} for p in PERIODS}),
            member("mac", "Mac", "Mac",
                   {p: {"revenue": (100, False)} for p in PERIODS}),
            member("service", "服務", "Service",
                   {p: {"revenue": (100, False), "cogs": (25, False)} for p in PERIODS}),
            # 上層：本身含 iPhone + Mac，成本只揭露到這一層
            member("product", "產品", "Product",
                   {p: {"revenue": (300, True), "cogs": (200, False)} for p in PERIODS}),
        ],
    }],
}

FIN = {
    "company": "Test Co", "ticker": "TEST", "cik": "0000000001", "mapVersion": "test",
    "periodicity": "quarterly", "currency": "USD", "periods": ["FY2025 Q1"],
    "lineItems": [], "derived": [],
}


def main() -> None:
    data = build_workbook({"financials": FIN, "segments": SEGMENTS})
    wb = load_workbook(BytesIO(data))
    assert "分部數據" in wb.sheetnames, "分部分頁沒生成"
    ws = wb["分部數據"]

    col = {}  # A 欄標籤 → 列號
    for r in range(1, ws.max_row + 1):
        v = ws.cell(row=r, column=1).value
        if isinstance(v, str):
            col.setdefault(v.strip(), r)

    # 1. 上層匯總必須被標示，且排在合計之後
    parent_label = next(k for k in col if k.startswith("產品（上層匯總"))
    total_rows = [r for k, r in col.items() if k == "合計"]
    assert total_rows, "沒有合計列"
    rev_total = min(total_rows)
    assert col[parent_label] > rev_total, "上層匯總必須排在合計之後"

    # 2. 合計是 SUM 公式，且範圍不含上層那一列
    f = ws.cell(row=rev_total, column=3).value
    assert isinstance(f, str) and f.startswith("=SUM("), f"合計不是公式：{f}"
    lo, hi = (int(x) for x in f[f.index("C") + 1:-1].replace("C", "").split(":"))
    assert not (lo <= col[parent_label] <= hi), f"上層被算進合計了：{f}"

    # 3. 合計範圍涵蓋三個子項、剛好等於合併總額 400
    assert hi - lo + 1 == 3, f"合計應涵蓋 3 個子項，實際 {hi - lo + 1}"
    vals = [ws.cell(row=r, column=3).value for r in range(lo, hi + 1)]
    assert sum(vals) == 400, f"子項加總應為 400，實際 {vals}"

    # 4. 比率一律公式（IFERROR 包除法），不能是算好的數值
    for label in ("營收佔比", "分部毛利率"):
        assert label in col, f"缺少 {label} 區塊"
        r = col[label] + 1
        v = ws.cell(row=r, column=3).value
        assert isinstance(v, str) and v.startswith("=IFERROR("), f"{label} 不是公式：{v}"

    # 5. 上層才有成本 → 產品毛利率要算得出來（(300-200)/300），這正是 Apple 的情境
    gm_rows = [r for r in range(col["分部毛利率"] + 1, ws.max_row + 1)
               if isinstance(ws.cell(row=r, column=1).value, str)
               and ws.cell(row=r, column=1).value.strip() == "產品"]
    assert gm_rows, "上層有揭露成本，卻沒算出毛利率"

    # 6. 營收佔比不含上層，否則佔比總和會超過 100%
    share_labels = []
    r = col["營收佔比"] + 1
    while isinstance(ws.cell(row=r, column=1).value, str) and ws.cell(row=r, column=3).value:
        share_labels.append(ws.cell(row=r, column=1).value.strip())
        r += 1
    assert "產品" not in share_labels, f"上層不該出現在營收佔比：{share_labels}"

    print("OK — 上層不進合計 / 合計為公式 / 比率為公式 / 上層毛利率保留 / 佔比排除上層")


if __name__ == "__main__":
    main()
