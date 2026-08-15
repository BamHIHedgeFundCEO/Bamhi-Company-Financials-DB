"""
依 chart_spec.json 程式化建圖。資料範圍由 n_quarters 動態計算，不寫死。
統一樣式（不用 openpyxl 預設）：固定色盤、淺灰虛線橫格線、圖例置下、
雙軸圖長條走主軸（金額）、折線走次軸（比率）。

⚠️ 系列一律用 Series(values_ref, title=中文名) 建立，不用 add_data：
add_data 預設把「每欄」當一條系列（要 from_rows），之前因此整張圖亂掉。
"""
import re

from openpyxl.chart import BarChart, LineChart, Reference, Series
from openpyxl.chart.axis import ChartLines
from openpyxl.chart.marker import DataPoint
from openpyxl.chart.shapes import GraphicalProperties
from openpyxl.drawing.line import LineProperties
from openpyxl.utils import get_column_letter

from config_loader import theme
from formulas import FIRST_DATA_COL


def _hex(c: str) -> str:
    return c.lstrip("#").upper()


def _dash_line(color: str):
    gp = GraphicalProperties()
    gp.line = LineProperties(solidFill=_hex(color), prstDash="dash", w=9525)
    return gp


def _style_chart(chart, th: dict):
    ch = th["chart"]
    chart.legend.position = ch.get("legend_position", "b")
    chart.legend.overlay = False
    chart.y_axis.majorGridlines = ChartLines()
    chart.y_axis.majorGridlines.graphicalProperties = _dash_line(ch["gridline_color"])
    chart.x_axis.majorGridlines = None
    # 座標軸刻度數字必須可見（openpyxl 預設 delete/tickLblPos 會讓 Excel 隱藏刻度）
    for ax in (chart.x_axis, chart.y_axis):
        ax.delete = False
        ax.tickLblPos = "nextTo"
        ax.majorTickMark = "out"
    chart.x_axis.tickLblPos = "low"  # 有負值時 X 軸標籤仍留在下方
    # 移除繪圖區/整體粗框
    chart.graphical_properties = GraphicalProperties()
    chart.graphical_properties.line = LineProperties(noFill=True)


def _values_ref(ws, row: int, n_quarters: int) -> Reference:
    """只含數據欄（C 起），不含科目名欄。"""
    return Reference(ws, min_col=FIRST_DATA_COL, max_col=FIRST_DATA_COL - 1 + n_quarters,
                     min_row=row, max_row=row)


def _cats_ref(ws, n_quarters: int) -> Reference:
    return Reference(ws, min_col=FIRST_DATA_COL, max_col=FIRST_DATA_COL - 1 + n_quarters,
                     min_row=1, max_row=1)


def _make_series(ws, row: int, n_quarters: int, color: str, is_line: bool) -> Series:
    s = Series(_values_ref(ws, row, n_quarters), title=str(ws.cell(row=row, column=1).value or ""))
    c = _hex(color)
    gp = GraphicalProperties()
    if is_line:
        gp.line = LineProperties(solidFill=c, w=22225)  # 1.75pt
        gp.noFill = True
        s.smooth = False
    else:
        gp.solidFill = c
        gp.line = LineProperties(noFill=True)
        s.invertIfNegative = False  # 負值長條沿用同色，不要翻成白色
    s.graphicalProperties = gp
    return s


def _period_labels(cats_ws, n_quarters: int) -> list[str]:
    return [str(cats_ws.cell(row=1, column=FIRST_DATA_COL + i).value or "") for i in range(n_quarters)]


def _color_bars_by_year(series: Series, labels: list[str], palette: list[str]):
    """單一科目長條圖：同一會計年度的季用同一色，不同年換色 → 一眼看出年度分界。"""
    fy_order: list[str] = []
    for lb in labels:
        m = re.match(r"FY(\d{4})", lb)
        fy = m.group(1) if m else lb
        if fy not in fy_order:
            fy_order.append(fy)
    color_of = {fy: _hex(palette[i % len(palette)]) for i, fy in enumerate(fy_order)}
    series.invertIfNegative = False  # 負值長條也要上色（否則翻白）
    pts = []
    for i, lb in enumerate(labels):
        m = re.match(r"FY(\d{4})", lb)
        fy = m.group(1) if m else lb
        gp = GraphicalProperties(solidFill=color_of[fy])
        gp.line = LineProperties(noFill=True)
        dp = DataPoint(idx=i, spPr=gp)
        dp.invertIfNegative = False
        pts.append(dp)
    series.data_points = pts


def build_chart(spec: dict, cats_ws, locate, n_quarters: int):
    """spec: chart_spec.json 單一圖表物件。locate(id) → (ws, row) 或 None（全活頁簿定位）。"""
    th = theme()
    palette = th["palette"]["chart_series"]
    ci = 0

    def series_for(ids, is_line):
        nonlocal ci
        out = []
        for i in ids:
            loc = locate(i)
            if loc:
                out.append(_make_series(loc[0], loc[1], n_quarters, palette[ci % len(palette)], is_line))
                ci += 1
        return out

    kind = spec["type"]
    cats = _cats_ref(cats_ws, n_quarters)

    if kind in ("bar", "stacked_bar"):
        chart = BarChart()
        chart.type = "col"
        chart.gapWidth = 60
        if kind == "stacked_bar":
            chart.grouping = "stacked"
            chart.overlap = 100
        chart.series = series_for(spec.get("series", []), is_line=False)
        chart.set_categories(cats)
        # 單一科目長條圖 → 依會計年度上色（成本費用結構這類多科目堆疊圖維持各科目一色）
        if len(chart.series) == 1:
            _color_bars_by_year(chart.series[0], _period_labels(cats_ws, n_quarters), palette)
        is_money = True
    elif kind == "line":
        chart = LineChart()
        chart.series = series_for(spec.get("series", []), is_line=True)
        chart.set_categories(cats)
        is_money = False
    elif kind == "bar+line":
        chart = BarChart()
        chart.type = "col"
        chart.gapWidth = 60
        chart.series = series_for(spec.get("bars", []), is_line=False)
        chart.set_categories(cats)
        if len(chart.series) == 1:
            _color_bars_by_year(chart.series[0], _period_labels(cats_ws, n_quarters), palette)
        line = LineChart()
        line.series = series_for(spec.get("line", []), is_line=True)
        line.set_categories(cats)
        if spec.get("secondary_axis"):
            line.y_axis.axId = 200
            line.y_axis.crosses = "max"
            line.y_axis.majorGridlines = None
            line.y_axis.delete = False
            line.y_axis.tickLblPos = "nextTo"
        chart += line
        is_money = True
    else:
        return None

    if not chart.series:
        return None
    # 金額圖：單位放標題（不放 Y 軸標題——旋轉軸標題會擋住刻度數字）
    chart.title = spec["title"] + ("（百萬美元）" if is_money else "")
    chart.width = th["chart"]["width_cols"] * 1.85   # 約 22cm
    chart.height = th["chart"]["height_rows"] * 0.5  # 約 9cm
    if is_money:
        chart.y_axis.number_format = th["number_formats"]["usd_millions_axis"]
    _style_chart(chart, th)
    return chart


def place_charts(ws, specs: list[dict], locate, n_quarters: int, anchor_row: int) -> int:
    """資料區下方直向排列，每張圖獨立一段，留足間距不重疊。回傳下一個可用列。"""
    th = theme()
    # 圖高（cm）→ 佔用列數（預設列高約 0.53cm），再加 4 列間隔
    step = int(th["chart"]["height_rows"] * 0.5 / 0.53) + 5
    r = anchor_row
    for spec in specs:
        chart = build_chart(spec, ws, locate, n_quarters)
        if chart is None:
            continue
        ws.add_chart(chart, f"{get_column_letter(FIRST_DATA_COL)}{r}")
        r += step
    return r
