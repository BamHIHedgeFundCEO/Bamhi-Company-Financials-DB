"""
每次從零生成整本活頁簿（不用範本檔——openpyxl 重存會遺失圖表）。
6 分頁：說明 / 損益表 / 資產負債表 / 現金流量表 / 關鍵指標 / 原始資料。
版面：A 欄中文、B 欄英文、C 欄起季度；凍結 C2；缺值 n/a 絕不寫 0；
Q4 推算值淺橘底；關鍵指標全公式。
"""
from datetime import datetime, timezone
from io import BytesIO

from openpyxl import Workbook
from openpyxl.comments import Comment
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter

from charts import place_charts
from config_loader import chart_spec, theme, xbrl_map
from formulas import FIRST_DATA_COL, RefResolver, translate

STATEMENT_SHEETS = {"IS": "損益表", "BS": "資產負債表", "CF": "現金流量表"}
METRICS_SHEET = "關鍵指標"

DISCLAIMER = (
    "資料來源為美國 SEC EDGAR 公開資料（companyfacts XBRL API），本工具與 SEC 無任何隸屬關係。"
    "缺值以 n/a 表示——SEC 無該標籤不代表數值為零。僅供參考，不構成投資建議。"
)


def _fmt(unit: str, th: dict) -> str:
    nf = th["number_formats"]
    return {
        "USD": nf["usd"],
        "shares": nf["shares"],
        "USD/shares": nf["per_share"],
    }.get(unit, nf["usd"])


def _metric_fmt(formula: str, mid: str, th: dict) -> str:
    nf = th["number_formats"]
    if mid in ("dso", "dio", "dpo", "ccc"):
        return nf["days"]
    if mid in ("ocf_to_net_income", "net_debt_to_ebitda", "interest_coverage",
               "current_ratio", "quick_ratio", "debt_to_equity", "asset_turnover"):
        return nf["multiple"]
    if mid in ("ebitda", "net_debt", "fcf", "shareholder_return"):
        return nf["usd"]
    return nf["ratio"]


_THIN = Side(style="thin", color="E3E5E1")
_INK = Side(style="medium", color="15171A")
ROW_BORDER = Border(bottom=_THIN)
HEADER_BORDER = Border(bottom=_INK)


def _header_cell(ws, col: int, text: str, th: dict):
    c = ws.cell(row=1, column=col, value=text)
    c.font = Font(bold=True, size=th["fonts"]["header_size"], color=th["palette"]["header_font"].lstrip("#"))
    c.fill = PatternFill("solid", fgColor=th["palette"]["header_fill"].lstrip("#"))
    c.alignment = Alignment(horizontal="center")
    c.border = HEADER_BORDER


def _init_sheet(ws, periods: list[str], th: dict, tab_color: str | None = None):
    _header_cell(ws, 1, "科目", th)
    _header_cell(ws, 2, "Line Item", th)
    for i, p in enumerate(periods):
        _header_cell(ws, FIRST_DATA_COL + i, p, th)
    ws.freeze_panes = th["layout"]["freeze_panes"]  # C2
    ws.sheet_view.showGridLines = False
    if tab_color:
        ws.sheet_properties.tabColor = tab_color.lstrip("#")
    ws.column_dimensions["A"].width = 26
    ws.column_dimensions["B"].width = 30
    for i in range(len(periods)):
        ws.column_dimensions[get_column_letter(FIRST_DATA_COL + i)].width = 14


def build_workbook(payload: dict) -> bytes:
    fin = payload["financials"]
    periods: list[str] = fin["periods"]
    n = len(periods)
    annual = fin.get("periodicity") == "annual"
    th = theme()
    cmap = xbrl_map()
    spec = chart_spec()["sheets"]

    q4_fill = PatternFill("solid", fgColor=th["palette"]["q4_estimated_fill"].lstrip("#"))
    missing = th["layout"]["missing_value"]  # "n/a"

    wb = Workbook()
    wb.remove(wb.active)

    # ── 1. 說明 ─────────────────────────────────────────────
    info = wb.create_sheet("說明")
    info.sheet_view.showGridLines = False
    info.sheet_properties.tabColor = th["palette"]["header_font"].lstrip("#")
    info.column_dimensions["A"].width = 22
    info.column_dimensions["B"].width = 64
    info.column_dimensions["C"].width = 46
    info.column_dimensions["D"].width = 90
    # 標題列
    t = info.cell(row=1, column=1, value=f"{fin['ticker']}　{fin['company']}")
    t.font = Font(bold=True, size=16)
    info.cell(row=2, column=1, value="BamHI 美股財報庫　·　SEC EDGAR 官方資料").font = Font(
        size=10, color="8C9199")
    meta_rows = [
        ("公司", fin["company"]),
        ("Ticker", fin["ticker"]),
        ("CIK", fin["cik"]),
        ("期間", f"{periods[0]} ～ {periods[-1]}" if periods else "—"),
        ("頻率", "年度（外國發行人 20-F，無季報）" if annual else "季度"),
        ("幣別", fin.get("currency", "USD")),
        ("資料來源", "SEC EDGAR companyfacts XBRL API"),
        ("生成時間 (UTC)", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")),
        ("對照表版本", fin["mapVersion"]),
        ("「推算」是什麼", "—（年度資料無推算）" if annual
         else "美股公司一年只申報 3 份 10-Q + 1 份 10-K：第四季沒有單獨的季報，"
              "SEC 上不存在「Q4 單季」這個數字。本檔以 全年 − Q1 − Q2 − Q3 計算出 Q4，"
              "數學上準確、非估計；橘底僅提醒「此數字不是直接抄自某份財報」。"
              "現金流量表的 Q2/Q3 亦由累計值差分還原（10-Q 只申報年初至今累計）。"),
        ("「n/a」是什麼", "該公司該期間沒有申報此科目——通常是公司本來就沒有這個項目"
         "（如未配息、未買回庫藏股、費用未拆分），不代表數值為零。可對照「原始資料」分頁查證。"),
        ("圖表", "各報表分頁：前段為 chart_spec.json 定義的組合圖，後段為每一科目各一張圖。"
         "要自訂組合圖，修改 repo 的 config/chart_spec.json 即可，不需改程式。"),
        ("免責聲明", DISCLAIMER),
    ]
    for i, (k, v) in enumerate(meta_rows, start=4):
        kc = info.cell(row=i, column=1, value=k)
        kc.font = Font(bold=True)
        kc.border = ROW_BORDER
        vc = info.cell(row=i, column=2, value=v)
        vc.border = ROW_BORDER
        vc.alignment = Alignment(wrap_text=True, vertical="top")
    r = len(meta_rows) + 5
    info.cell(row=r, column=1, value="指標定義總表").font = Font(bold=True, size=12)
    r += 1
    for j, h in enumerate(["指標", "英文", "定義", "判讀說明"], start=1):
        _header_cell_at(info, r, j, h, th)
    for m in fin["derived"]:
        r += 1
        info.cell(row=r, column=1, value=m["zh"])
        info.cell(row=r, column=2, value=m["en"])
        info.cell(row=r, column=3, value=m["formula"])
        c = info.cell(row=r, column=4, value=m["desc"])
        c.alignment = Alignment(wrap_text=True, vertical="top")

    # ── 2–4. 三大報表 ───────────────────────────────────────
    resolver = RefResolver()
    # 全活頁簿 id → (worksheet, row)：圖表可跨分頁引用（如損益表圖的毛利率折線在關鍵指標分頁）
    locations: dict[str, tuple] = {}
    chart_jobs: list[tuple] = []  # (ws, specs, anchor_row) — 指標列建立後才放圖
    raw_rows: list[tuple] = []  # 原始資料分頁

    tab_colors = {"IS": "1F3A5F", "BS": "5C7699", "CF": "0E6B5A"}
    na_font = Font(color="8C9199", size=th["fonts"]["size"])
    for stmt, sheet_name in STATEMENT_SHEETS.items():
        ws = wb.create_sheet(sheet_name)
        _init_sheet(ws, periods, th, tab_color=tab_colors[stmt])
        auto_specs = []  # 每個有資料的科目各一張圖（在 chart_spec 的組合圖之後）
        row = 1
        for li in fin["lineItems"]:
            if li["statement"] != stmt:
                continue
            row += 1
            locations[li["id"]] = (ws, row)
            resolver.add(li["id"], sheet_name, row)
            ws.cell(row=row, column=1, value=li["zh"]).border = ROW_BORDER
            ws.cell(row=row, column=2, value=li["en"]).border = ROW_BORDER
            for i, p in enumerate(periods):
                cell = ws.cell(row=row, column=FIRST_DATA_COL + i)
                cell.border = ROW_BORDER
                v = li["values"].get(p)
                if v is None or v.get("value") is None:
                    cell.value = missing  # 絕不寫 0
                    cell.font = na_font
                    cell.alignment = Alignment(horizontal="right")
                else:
                    cell.value = v["value"]  # 原始美元，不除以百萬
                    cell.number_format = _fmt(li["unit"], th)
                    if v.get("isEstimated"):
                        cell.fill = q4_fill
                    raw_rows.append((li["zh"], li["en"], p, v["value"],
                                     v.get("sourceTag"), v.get("accessionOrForm"),
                                     v.get("filed"), v.get("endDate"), li["unit"],
                                     "Q4推算＝全年−前三季" if v.get("isEstimated") else "財報直接申報值"))
            if any(vv.get("value") is not None for vv in li["values"].values()):
                yf = {"USD": "money", "shares": None, "USD/shares": None}.get(li["unit"], "money")
                auto_specs.append({
                    "type": "line" if li["unit"] == "USD/shares" else "bar",
                    "title": f"{li['zh']} {li['en']}",
                    "series": [li["id"]],
                    "y_format": yf,
                })
        chart_jobs.append((ws, spec.get(sheet_name, []) + auto_specs, row + 3))

    # ── 5. 關鍵指標（全公式）────────────────────────────────
    ws = wb.create_sheet(METRICS_SHEET)
    _init_sheet(ws, periods, th, tab_color=th["palette"]["accent"])
    group_fill = PatternFill("solid", fgColor=th["palette"]["header_fill"].lstrip("#"))
    row = 1
    current_group = None
    group_members: dict[str, list[str]] = {}
    for m in fin["derived"]:
        if m["group"] != current_group:
            current_group = m["group"]
            row += 1
            for j in range(1, FIRST_DATA_COL + n):
                gc = ws.cell(row=row, column=j)
                gc.fill = group_fill
                gc.border = HEADER_BORDER
            ws.cell(row=row, column=1, value=f"▍{current_group}").font = Font(
                bold=True, color=th["palette"]["accent"].lstrip("#"))
        group_members.setdefault(m["group"], []).append(m["id"])
        row += 1
        locations[m["id"]] = (ws, row)
        resolver.add(m["id"], METRICS_SHEET, row)
        name_cell = ws.cell(row=row, column=1, value=m["zh"])
        name_cell.border = ROW_BORDER
        ws.cell(row=row, column=2, value=m["en"]).border = ROW_BORDER
        # 滑鼠移入指標名稱顯示判讀說明（desc）
        name_cell.comment = Comment(m["desc"], "BamHI", height=160, width=360)
        fmt = _metric_fmt(m["formula"], m["id"], th)
        for i in range(n):
            col = FIRST_DATA_COL + i
            cell = ws.cell(row=row, column=col)
            cell.border = ROW_BORDER
            f = translate(m["formula"], resolver, col, annual=annual)
            if f is None:
                cell.value = missing
                cell.font = na_font
                cell.alignment = Alignment(horizontal="right")
            else:
                cell.value = f"={f}"
                cell.number_format = fmt
    # 關鍵指標圖表：① chart_spec 重點比較圖
    #   ② 組內比較——但只把「同一種單位」的指標放同一張圖（比率/天數各自比較），
    #      金額型指標（EBITDA、淨負債、FCF…）數量級差太多，混在一起會把比率壓成平線
    #   ③ 每個指標各自一張獨立折線
    fmt_of = {m["id"]: _metric_fmt(m["formula"], m["id"], th) for m in fin["derived"]}
    nf = th["number_formats"]
    # 每種數字格式 → 顯示名 + 圖表 Y 軸單位
    bucket = {
        nf["ratio"]: ("比率", "percent"),
        nf["days"]: ("週轉天數", "days"),
        nf["multiple"]: ("倍數", "multiple"),
        nf["usd"]: ("金額", "money"),
    }
    yfmt_of = {mid: bucket.get(fmt, ("", None))[1] for mid, fmt in fmt_of.items()}
    metric_specs = list(spec.get(METRICS_SHEET, []))
    for grp, ids in group_members.items():
        by_bucket: dict[str, list[str]] = {}
        for mid in ids:
            by_bucket.setdefault(fmt_of[mid], []).append(mid)
        for fmt, mids in by_bucket.items():
            if len(mids) >= 2 and fmt in bucket and bucket[fmt][1] != "money":
                name, yf = bucket[fmt]
                metric_specs.append(
                    {"type": "line", "title": f"{grp}｜{name}比較", "series": mids, "y_format": yf})
    for m in fin["derived"]:
        metric_specs.append(
            {"type": "line", "title": f"{m['zh']} {m['en']}", "series": [m["id"]], "y_format": yfmt_of[m["id"]]})
    chart_jobs.append((ws, metric_specs, row + 3))

    # 指標列已定位，統一放圖（圖表可跨分頁引用系列）；收集圖表位置做「說明」目錄
    locate = locations.get
    chart_index: list = []
    for job_ws, job_specs, anchor in chart_jobs:
        place_charts(job_ws, job_specs, locate, n, anchor_row=anchor, index=chart_index)

    # 「說明」分頁頂端加「圖表快速跳轉」目錄：點連結直接跳到該圖，不用一路往下滑
    from openpyxl.utils import get_column_letter as _gcl
    jump_col = 6  # F 欄，避開左側 meta/指標定義
    info.cell(row=1, column=jump_col, value="圖表快速跳轉").font = Font(bold=True, size=12)
    info.column_dimensions[_gcl(jump_col)].width = 34
    ji = 2
    cur_sheet = None
    for sheet_name, title, r in chart_index:
        if sheet_name != cur_sheet:
            cur_sheet = sheet_name
            info.cell(row=ji, column=jump_col, value=f"▍{sheet_name}").font = Font(
                bold=True, color=th["palette"]["accent"].lstrip("#"))
            ji += 1
        link = info.cell(row=ji, column=jump_col, value=f"　{title}")
        link.hyperlink = f"#'{sheet_name}'!{_gcl(FIRST_DATA_COL)}{r}"
        link.font = Font(color="0E6B5A", underline="single", size=10)
        ji += 1

    # ── 6. 原始資料 ─────────────────────────────────────────
    ws = wb.create_sheet("原始資料")
    headers = ["科目", "Line Item", "季別", "數值", "XBRL 標籤", "表單", "申報日", "期末日", "單位", "備註"]
    for j, h in enumerate(headers, start=1):
        _header_cell_at(ws, 1, j, h, th)
    for i, rr in enumerate(raw_rows, start=2):
        for j, v in enumerate(rr, start=1):
            ws.cell(row=i, column=j, value=v)
    ws.freeze_panes = "A2"
    widths = [24, 28, 12, 16, 44, 10, 12, 12, 10, 8]
    for j, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(j)].width = w

    buf = BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _header_cell_at(ws, row: int, col: int, text: str, th: dict):
    c = ws.cell(row=row, column=col, value=text)
    c.font = Font(bold=True, size=th["fonts"]["header_size"])
    c.fill = PatternFill("solid", fgColor=th["palette"]["header_fill"].lstrip("#"))
