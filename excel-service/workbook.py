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

from charts import place_charts, set_data_start
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
    if mid in ("revenue_per_share", "book_value_per_share", "cash_plus_sti_per_share",
               "fcf_per_share", "ocf_per_share"):
        return nf["per_share"]  # 每股金額 0.00
    return nf["ratio"]


_THIN = Side(style="thin", color="E3E5E1")
_INK = Side(style="medium", color="15171A")
_BOX = Side(style="thin", color="8C9199")
ROW_BORDER = Border(bottom=_THIN)
HEADER_BORDER = Border(bottom=_INK)
# 全框（說明分頁表格用，四邊細灰框）
CELL_BOX = Border(left=_BOX, right=_BOX, top=_BOX, bottom=_BOX)


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
    lookback = int(fin.get("lookbackCount", 0))  # 前面幾欄是 lookback（隱藏，供 YoY/TTM 公式）
    n_display = n - lookback
    data_start = FIRST_DATA_COL + lookback  # 圖表與顯示的第一欄
    set_data_start(data_start)
    pre_ipo = fin.get("preIpoBefore")  # 上市/借殼前的期界線
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
        *([("上市前資料", f"{pre_ipo} 之前的季度已顯示為 n/a。此公司經 SPAC 借殼／IPO 上市，"
            "上市前為私有公司，股數基礎與上市後不可比（每股數值會嚴重失真），故不列出。")]
          if pre_ipo else []),
        ("圖表", "各報表分頁：前段為 chart_spec.json 定義的組合圖，後段為每一科目各一張圖。"
         "要自訂組合圖，修改 repo 的 config/chart_spec.json 即可，不需改程式。"),
        ("免責聲明", DISCLAIMER),
    ]
    meta_fill = PatternFill("solid", fgColor=th["palette"]["header_fill"].lstrip("#"))
    for i, (k, v) in enumerate(meta_rows, start=4):
        kc = info.cell(row=i, column=1, value=k)
        kc.font = Font(bold=True)
        kc.border = CELL_BOX
        kc.fill = meta_fill
        kc.alignment = Alignment(vertical="top")
        vc = info.cell(row=i, column=2, value=v)
        vc.border = CELL_BOX
        vc.alignment = Alignment(wrap_text=True, vertical="top")
    r = len(meta_rows) + 5
    info.cell(row=r, column=1, value="指標定義總表").font = Font(bold=True, size=12)
    info.cell(row=r + 1, column=4, value="資料來源：SEC EDGAR companyfacts XBRL。每個數字對應標籤見「原始資料」分頁。").font = Font(
        size=9, color="8C9199", italic=True)
    r += 1
    for j, h in enumerate(["指標", "英文", "定義（公式）", "判讀說明"], start=1):
        c = _header_cell_at(info, r, j, h, th)
    for m in fin["derived"]:
        r += 1
        for j, val in enumerate([m["zh"], m["en"], m["formula"], m["desc"]], start=1):
            c = info.cell(row=r, column=j, value=val)
            c.border = CELL_BOX
            c.alignment = Alignment(wrap_text=True, vertical="top")
            if j == 1:
                c.font = Font(bold=True)

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
                                     v.get("origFiled"), v.get("filed"), v.get("endDate"), li["unit"],
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
            if m["id"] == "ccc":
                # 現金轉換循環：存貨不適用（軟體公司）時 DIO 以 0 計，仍算得出 DSO−DPO
                dso, dio, dpo = (resolver.cell(x, col) for x in ("dso", "dio", "dpo"))
                f = (f'IFERROR({dso},0)+IFERROR({dio},0)-IFERROR({dpo},0)'
                     if dso and dpo else None)
            else:
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
    # 只保留 ① chart_spec 精選比較圖（2-3 條，可控）② 每個指標各自一張（自動縮放、看得清）。
    # 移除「組內全指標」比較圖：虧損股的極端比率（如利息保障 -106x）會把其他線壓成一團。
    metric_specs = list(spec.get(METRICS_SHEET, []))
    for m in fin["derived"]:
        metric_specs.append(
            {"type": "line", "title": f"{m['zh']} {m['en']}", "series": [m["id"]], "y_format": yfmt_of[m["id"]]})
    chart_jobs.append((ws, metric_specs, row + 3))

    # ── 5b. 估值倍數（模型：股價為輸入格，倍數全公式）─────────
    valuation = fin.get("valuation")
    if valuation and valuation.get("rows"):
        _build_valuation_sheet(wb, valuation, locations, periods, th,
                               data_start, n_display, chart_jobs)

    # 指標列已定位，統一放圖（圖表可跨分頁引用系列）；收集圖表位置做「說明」目錄
    locate = locations.get
    chart_index: list = []
    for job_ws, job_specs, anchor in chart_jobs:
        place_charts(job_ws, job_specs, locate, n_display, anchor_row=anchor, index=chart_index)

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

    # 每個報表/指標分頁的圖表快捷：在該頁圖表區頂端（資料表下方、圖表左側 A/B 空白處）
    # 集中放「所有圖表的文字連結」，點一下直接跳到該圖——不再每張圖各放一個回頂端。
    from collections import OrderedDict
    per_sheet: "OrderedDict[str, list]" = OrderedDict()
    for sheet_name, title, r in chart_index:
        per_sheet.setdefault(sheet_name, []).append((title, r))
    for sheet_name, items in per_sheet.items():
        sh = wb[sheet_name]
        top = items[0][1]  # 第一張圖的列 = 圖表區頂端
        # 第 1 列（凍結，永遠可見）放一個「跳到圖表目錄」
        sc = FIRST_DATA_COL + n + 1
        q = sh.cell(row=1, column=sc, value="► 跳到圖表目錄")
        q.hyperlink = f"#'{sheet_name}'!A{top}"
        q.font = Font(color="0E6B5A", underline="single", bold=True, size=10)
        sh.column_dimensions[_gcl(sc)].width = 16
        # 圖表區頂端的文字連結目錄（A 欄，縱向排列所有圖）
        hdr = sh.cell(row=top, column=1, value="■ 本頁圖表目錄")
        hdr.font = Font(bold=True, color=th["palette"]["accent"].lstrip("#"))
        back = sh.cell(row=top, column=2, value="回說明")
        back.hyperlink = f"#'說明'!{_gcl(jump_col)}1"
        back.font = Font(color="0E6B5A", underline="single", size=10)
        for k, (title, r) in enumerate(items, start=1):
            c = sh.cell(row=top + k, column=1, value=f"　{title}")
            c.hyperlink = f"#'{sheet_name}'!{_gcl(FIRST_DATA_COL)}{r}"
            c.font = Font(color="0E6B5A", underline="single", size=10)

    # ── 6. 原始資料 ─────────────────────────────────────────
    ws = wb.create_sheet("原始資料")
    headers = ["科目", "Line Item", "季別", "數值", "XBRL 標籤", "表單",
               "原始申報日", "取值來源申報日", "期末日", "單位", "備註"]
    for j, h in enumerate(headers, start=1):
        c = _header_cell_at(ws, 1, j, h, th)
        if h in ("原始申報日", "取值來源申報日"):
            c.comment = Comment(
                "原始申報日＝該期首次申報；取值來源申報日＝實際取值的那份（可能是後續重編，取 filed 最新）。"
                "兩者不同代表該數字曾被重編。", "BamHI", height=120, width=320)
    for i, rr in enumerate(raw_rows, start=2):
        for j, v in enumerate(rr, start=1):
            ws.cell(row=i, column=j, value=v)
    ws.freeze_panes = "A2"
    widths = [24, 28, 12, 16, 44, 10, 12, 14, 12, 10, 8]
    for j, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(j)].width = w

    # 隱藏 lookback 欄：資料在、公式引用得到（YoY/TTM 從第一顯示欄即活），但使用者看不到
    if lookback > 0:
        for sname in list(STATEMENT_SHEETS.values()) + [METRICS_SHEET, "估值倍數"]:
            if sname in wb.sheetnames:
                sh = wb[sname]
                for c in range(FIRST_DATA_COL, data_start):
                    sh.column_dimensions[get_column_letter(c)].hidden = True

    buf = BytesIO()
    wb.save(buf)
    data = buf.getvalue()

    # 估值倍數：關掉 Excel 背景錯誤檢查（P/E、P/S 等公式左上角的綠色三角）。
    # openpyxl 3.1 不序列化 ignoredErrors，故存檔後直接改寫該分頁 XML。
    if valuation and valuation.get("rows") and "估值倍數" in wb.sheetnames:
        sheet_no = wb.sheetnames.index("估值倍數") + 1
        end_col = get_column_letter(max(data_start + n_display - 1, data_start + 3))
        data = _inject_ignored_errors(data, sheet_no, f"A1:{end_col}33")
    return data


def _inject_ignored_errors(data: bytes, sheet_no: int, sqref: str) -> bytes:
    """把 <ignoredErrors> 注入指定 worksheet XML（schema 規定在 <drawing> 之前）。"""
    import zipfile

    target = f"xl/worksheets/sheet{sheet_no}.xml"
    frag = (f'<ignoredErrors><ignoredError sqref="{sqref}" '
            f'formula="1" formulaRange="1" emptyCellReferences="1" '
            f'numberStoredAsText="1" unlockedFormula="1"/></ignoredErrors>')
    zin = zipfile.ZipFile(BytesIO(data))
    out = BytesIO()
    zout = zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED)
    for it in zin.infolist():
        raw = zin.read(it.filename)
        if it.filename == target and b"<ignoredErrors" not in raw:
            xml = raw.decode("utf-8")
            pos = xml.find("<drawing ")
            if pos == -1:
                pos = xml.rfind("</worksheet>")
            raw = (xml[:pos] + frag + xml[pos:]).encode("utf-8")
        zout.writestr(it, raw)
    zin.close()
    zout.close()
    return out.getvalue()


BLUE_INPUT = Font(color="0000FF")           # 藍字＝可改假設
BLUE_FILL = PatternFill("solid", fgColor="FFFFCC")  # 黃底
GREEN_LINK = Font(color="0E6B5A")           # 綠字＝跨表連結


def _build_valuation_sheet(wb, valuation, locations, periods, th, data_start, n_display, chart_jobs):
    """
    估值分頁＝財務模型：股價是唯一硬值（藍字輸入格），市值/PE/PS/EV… 全部公式。
    版面：假設區(前瞻用) → 反推目標價 → 歷史逐季倍數(全公式，TTM 引用含 lookback 欄)。
    """
    from openpyxl.utils import get_column_letter as g

    vws = wb.create_sheet("估值倍數")
    vws.sheet_view.showGridLines = False
    vws.sheet_properties.tabColor = "C25A18"
    vws.column_dimensions["A"].width = 26
    vws.column_dimensions["B"].width = 26
    for i in range(len(periods)):
        vws.column_dimensions[g(FIRST_DATA_COL + i)].width = 13

    dcol = g(data_start)          # 第一顯示欄字母（假設輸入放這欄，永遠可見）
    last_disp = data_start + n_display - 1
    lastcol = g(last_disp)        # 最後一季（最新）

    def sheet_row(cid):
        loc = locations.get(cid)
        return (loc[0].title, loc[1]) if loc else (None, None)

    rev_s, rev_r = sheet_row("revenue")
    ni_s, ni_r = sheet_row("net_income")
    epsd_s, epsd_r = sheet_row("eps_diluted")
    so_s, so_r = sheet_row("shares_outstanding")
    eq_s, eq_r = sheet_row("equity")
    fcf_s, fcf_r = sheet_row("fcf")
    ebitda_s, ebitda_r = sheet_row("ebitda")
    nd_s, nd_r = sheet_row("net_debt")

    def ref(sheet, row, col):
        return f"'{sheet}'!{g(col)}{row}"

    def ttm(sheet, row, col):
        return f"SUM('{sheet}'!{g(col - 3)}{row}:{g(col)}{row})"

    px = valuation.get("currentPrice")
    ps_med = None
    for r in valuation["rows"]:
        if r["id"] == "ps_vs_median":
            import re as _re
            m = _re.search(r"([\d.]+)", r.get("desc", ""))
            if m:
                ps_med = float(m.group(1))

    # ── 三大區塊：橫向並排（左右各一組「標籤｜數值」），開檔即全見不折疊 ──
    # 欄位相對 data_start 動態：假設區 A/B、前瞻估值 dcol/dcol+1、反推目標價 dcol+2/dcol+3。
    # ⚠ 估值圖 category 讀第 1 列（charts._cats_ref, min_row=1）→ 區塊一律從第 2 列起，
    #    第 1 列（dcol 起）必須淨空，否則區塊標題會變成圖表 X 軸標籤。
    accent = th["palette"]["accent"].lstrip("#")
    TITLE_F = Font(bold=True, color=accent)
    LABEL_F = Font(color="15171A")
    NOTE_F = Font(size=9, color="8C9199")

    c2l, c2v = data_start, data_start + 1        # 前瞻：標籤 / 數值欄
    c3l, c3v = data_start + 2, data_start + 3    # 反推：標籤 / 數值欄
    L2, L3 = g(c2v), g(c3v)

    def vtitle(col, zh):
        vws.cell(row=2, column=col, value=zh).font = TITLE_F

    def vlab(col, row, zh):
        vws.cell(row=row, column=col, value=zh).font = LABEL_F

    def vval(col, row, val, fmt, kind="formula"):
        if val is None:
            return
        c = vws.cell(row=row, column=col, value=val)
        c.number_format = fmt
        if kind == "input":
            c.font = BLUE_INPUT
            c.fill = BLUE_FILL
        elif kind == "shares":
            c.font = GREEN_LINK

    # 加寬區塊用到的欄（同時是歷史表前幾季欄，只加寬不縮窄）
    vws.column_dimensions["A"].width = 26
    vws.column_dimensions["B"].width = 20
    vws.column_dimensions[g(c2l)].width = 19
    vws.column_dimensions[g(c2v)].width = 18
    vws.column_dimensions[g(c3l)].width = 20
    vws.column_dimensions[g(c3v)].width = 13

    # 假設區（A 標籤 / B 數值）— 輸入格藍字黃底、股數綠字跨表
    vtitle(1, "■ 假設區　Assumptions")
    vlab(1, 3, "目前股價");         vval(2, 3, round(px, 2) if px else None, '$0.00', "input")
    vlab(1, 4, "流通股數（最新）");   vval(2, 4, f"={ref(so_s, so_r, last_disp)}" if so_s else None, "#,##0", "shares")
    vlab(1, 5, "FY+1 預估 EPS");    vval(2, 5, None, '0.00', "input")
    vlab(1, 6, "FY+2 預估 EPS");    vval(2, 6, None, '0.00', "input")
    vlab(1, 7, "目標本益比");        vval(2, 7, 20, '0.0"x"', "input")
    vlab(1, 8, "目標股價營收比");     vval(2, 8, round(ps_med, 1) if ps_med else 5, '0.0"x"', "input")

    # 前瞻估值（dcol 標籤 / dcol+1 數值）
    vtitle(c2l, "■ 前瞻估值　Forward Valuation")
    vlab(c2l, 3, "市值");            vval(c2v, 3, '=IFERROR($B$3*$B$4,"n/a")', "#,##0")
    vlab(c2l, 4, "前瞻本益比 FY+1");  vval(c2v, 4, '=IFERROR($B$3/$B$5,"n/a")', '0.0"x"')
    vlab(c2l, 5, "前瞻本益比 FY+2");  vval(c2v, 5, '=IFERROR($B$3/$B$6,"n/a")', '0.0"x"')
    vlab(c2l, 6, "預估 EPS 成長率");  vval(c2v, 6, '=IFERROR($B$6/$B$5-1,"n/a")', '0.0%')
    vlab(c2l, 7, "前瞻 PEG");         vval(c2v, 7, f'=IFERROR(${L2}$4/(${L2}$6*100),"n/a")', '0.00')
    vlab(c2l, 8, "目前本益比（TTM）"); vval(c2v, 8, f'=IFERROR($B$3/{ttm(epsd_s, epsd_r, last_disp)},"n/a")' if epsd_s else None, '0.0"x"')

    # 反推目標價（dcol+2 標籤 / dcol+3 數值）
    vtitle(c3l, "■ 反推目標價　Reverse: Target Price")
    vlab(c3l, 3, "目標價（P/E × FY+1 EPS）");  vval(c3v, 3, '=IFERROR($B$7*$B$5,"n/a")', '$0.00')
    vlab(c3l, 4, "目標價（P/E × FY+2 EPS）");  vval(c3v, 4, '=IFERROR($B$7*$B$6,"n/a")', '$0.00')
    # 目標價（P/S × TTM 營收）÷ 股數：股數引用 $B$4（原本誤用帶「=」的 p3 → 產生 /=... 壞公式）
    vlab(c3l, 5, "目標價（P/S × TTM 營收）");  vval(c3v, 5, f'=IFERROR($B$8*{ttm(rev_s, rev_r, last_disp)}/$B$4,"n/a")' if rev_s else None, '$0.00')
    vlab(c3l, 6, "上漲空間（FY+1）");          vval(c3v, 6, f'=IFERROR(${L3}$3/$B$3-1,"n/a")', '0.0%')
    vlab(c3l, 7, "上漲空間（FY+2）");          vval(c3v, 7, f'=IFERROR(${L3}$4/$B$3-1,"n/a")', '0.0%')

    # 上漲空間紅綠條件式（正綠負紅）
    from openpyxl.formatting.rule import CellIsRule
    red = Font(color="C0392B"); grn = Font(color="0E6B5A")
    for rr in (6, 7):
        cc = f"{L3}{rr}"
        vws.conditional_formatting.add(cc, CellIsRule(operator="greaterThan", formula=["0"], font=grn))
        vws.conditional_formatting.add(cc, CellIsRule(operator="lessThan", formula=["0"], font=red))

    # 說明備註
    vws.cell(row=10, column=1, value="藍字＝可修改輸入；黑字＝公式；綠字＝跨表連結").font = NOTE_F
    vws.cell(row=11, column=1, value="FY+1 / FY+2 EPS 需自行填入（SEC 不提供分析師預估）").font = NOTE_F

    # ── 歷史逐季倍數（全公式）──
    hdr = 24
    _header_cell_at(vws, hdr, 1, "歷史逐季倍數", th)
    _header_cell_at(vws, hdr, 2, "TTM／公式", th)
    for i in range(n_display):
        col = data_start + i
        _header_cell_at(vws, hdr, col, periods[(len(periods) - n_display) + i], th)
    # 凍結：只鎖 A/B 標籤欄 + 上方區塊（第 1–8 列）；捲到下方歷史表/圖表不再被高表頭擋
    vws.freeze_panes = "C9"

    price_vals = next((r["values"] for r in valuation["rows"] if r["id"] == "price"), {})
    rows_spec = [
        ("期末股價", "Price", '$0.00', None),
        ("市值", "Market Cap", '#,##0', "mc"),
        ("本益比 P/E", "P/E (TTM)", '0.0"x"', "pe"),
        ("股價營收比 P/S", "P/S (TTM)", '0.0"x"', "ps"),
        ("股價淨值比 P/B", "P/B", '0.0"x"', "pb"),
        ("股價自由現金流比 P/FCF", "P/FCF (TTM)", '0.0"x"', "pfcf"),
        ("企業價值 EV", "Enterprise Value", '#,##0', "ev"),
        ("EV／EBITDA", "EV/EBITDA (TTM)", '0.0"x"', "eve"),
        ("PS／歷史中位數", "P/S vs Median", '0.00"倍"', "psmed"),
    ]
    row0 = hdr + 1
    rrows = {}
    for k, (zh, en, fmt, key) in enumerate(rows_spec):
        r = row0 + k
        rrows[key or "price"] = r
        vws.cell(row=r, column=1, value=zh).border = ROW_BORDER
        vws.cell(row=r, column=2, value=en).border = ROW_BORDER
    # 全期（含 lookback）都填股價，讓 TTM 公式引用得到；但只有顯示欄可見
    all_price_row = rrows["price"]
    for i, p in enumerate(periods):
        col = FIRST_DATA_COL + i
        c = vws.cell(row=all_price_row, column=col)
        c.border = ROW_BORDER
        pv = price_vals.get(p)
        if pv is not None:
            c.value = round(pv, 2)
            c.font = BLUE_INPUT
            c.fill = BLUE_FILL
            c.number_format = '$0.00'
        else:
            c.value = missing
    pr = all_price_row
    for i in range(n_display):
        col = data_start + i
        L = g(col)
        mc = f"{L}{rrows['mc']}"
        ev = f"{L}{rrows['ev']}"
        cells = {
            "mc": f"={L}{pr}*{ref(so_s, so_r, col)}" if so_s else None,
            "pe": f'=IFERROR({mc}/{ttm(ni_s, ni_r, col)},"n/a")' if ni_s else None,
            "ps": f'=IFERROR({mc}/{ttm(rev_s, rev_r, col)},"n/a")' if rev_s else None,
            "pb": f'=IFERROR({mc}/{ref(eq_s, eq_r, col)},"n/a")' if eq_s else None,
            "pfcf": f'=IFERROR({mc}/{ttm(fcf_s, fcf_r, col)},"n/a")' if fcf_s else None,
            "ev": f"={mc}+{ref(nd_s, nd_r, col)}" if nd_s else None,
            "eve": f'=IFERROR({ev}/{ttm(ebitda_s, ebitda_r, col)},"n/a")' if ebitda_s else None,
        }
        for key, f in cells.items():
            c = vws.cell(row=rrows[key], column=col)
            c.border = ROW_BORDER
            c.number_format = dict(rows_spec_map(rows_spec))[key]
            c.value = f if f else missing
    # PS／歷史中位數：引用整條 PS 顯示範圍的 MEDIAN
    ps_row = rrows["ps"]; med_row = rrows["psmed"]
    rng = f"{g(data_start)}{ps_row}:{g(last_disp)}{ps_row}"
    for i in range(n_display):
        col = data_start + i
        c = vws.cell(row=med_row, column=col)
        c.border = ROW_BORDER
        c.number_format = '0.00"倍"'
        c.value = f'=IFERROR({g(col)}{ps_row}/MEDIAN({rng}),"n/a")'

    # 估值圖：PE/PS/PB 各一張折線（引用歷史列）
    for key in ("pe", "ps", "pb", "pfcf", "eve"):
        locations[f"val_{key}"] = (vws, rrows[key])
    vspecs = [{"type": "line", "title": t, "series": [f"val_{k}"], "y_format": None}
              for k, t in [("pe", "本益比 P/E"), ("ps", "股價營收比 P/S"), ("pb", "股價淨值比 P/B"),
                           ("pfcf", "P/FCF"), ("eve", "EV/EBITDA")]]
    chart_jobs.append((vws, vspecs, row0 + len(rows_spec) + 3))


def rows_spec_map(rows_spec):
    return [((key or "price"), fmt) for zh, en, fmt, key in rows_spec]


def _header_cell_at(ws, row: int, col: int, text: str, th: dict):
    c = ws.cell(row=row, column=col, value=text)
    c.font = Font(bold=True, size=th["fonts"]["header_size"], color=th["palette"]["header_font"].lstrip("#"))
    c.fill = PatternFill("solid", fgColor=th["palette"]["header_fill"].lstrip("#"))
    c.border = CELL_BOX
    return c
