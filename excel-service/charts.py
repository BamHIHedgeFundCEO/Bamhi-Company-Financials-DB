"""
依 chart_spec.json 程式化建圖。資料範圍由 n_quarters 動態計算，不寫死。
統一樣式（不用 openpyxl 預設）：固定色盤、淺灰虛線橫格線、圖例置下、
雙軸圖長條走主軸（金額）、折線走次軸（比率）。
"""
from openpyxl.chart import BarChart, LineChart, Reference, Series
from openpyxl.chart.axis import ChartLines
from openpyxl.drawing.line import LineProperties
from openpyxl.utils import get_column_letter

from config_loader import theme
from formulas import FIRST_DATA_COL


def _hex(c: str) -> str:
    return c.lstrip("#").upper()


def _style_chart(chart, th: dict):
    ch = th["chart"]
    chart.legend.position = ch.get("legend_position", "b")
    chart.legend.overlay = False
    # 淺灰虛線橫向格線；移除粗重外框
    gl = ChartLines()
    gl.graphicalProperties = None
    chart.y_axis.majorGridlines = ChartLines()
    chart.y_axis.majorGridlines.graphicalProperties = _dash_line(ch["gridline_color"])
    chart.x_axis.majorGridlines = None
    chart.graphical_properties = None


def _dash_line(color: str):
    from openpyxl.chart.shapes import GraphicalProperties
    gp = GraphicalProperties()
    gp.line = LineProperties(solidFill=_hex(color), prstDash="dash", w=9525)
    return gp


def _color_series(series_list, th: dict):
    palette = th["palette"]["chart_series"]
    for i, s in enumerate(series_list):
        c = _hex(palette[i % len(palette)])
        if isinstance(s, Series):
            from openpyxl.chart.shapes import GraphicalProperties
            gp = GraphicalProperties(solidFill=c)
            gp.line = LineProperties(solidFill=c, w=19050)
            s.graphicalProperties = gp


def _series_ref(ws, row: int, n_quarters: int) -> Reference:
    # 含 B 欄（英文科目名）作為系列名稱
    return Reference(ws, min_col=2, max_col=FIRST_DATA_COL - 1 + n_quarters, min_row=row, max_row=row)


def _cats_ref(ws, n_quarters: int) -> Reference:
    return Reference(ws, min_col=FIRST_DATA_COL, max_col=FIRST_DATA_COL - 1 + n_quarters, min_row=1, max_row=1)


def build_chart(spec: dict, ws, row_of: dict[str, int], n_quarters: int):
    """spec: chart_spec.json 單一圖表物件。row_of: concept/metric id → 該分頁列號。回 chart 或 None。"""
    th = theme()

    def rows(ids):
        return [row_of[i] for i in ids if i in row_of]

    kind = spec["type"]
    title = spec["title"]
    cats = _cats_ref(ws, n_quarters)

    if kind in ("bar", "stacked_bar"):
        chart = BarChart()
        chart.type = "col"
        if kind == "stacked_bar":
            chart.grouping = "stacked"
            chart.overlap = 100
        for r in rows(spec.get("series", [])):
            chart.add_data(_series_ref(ws, r, n_quarters), titles_from_data=True)
        chart.set_categories(cats)
    elif kind == "line":
        chart = LineChart()
        for r in rows(spec.get("series", [])):
            chart.add_data(_series_ref(ws, r, n_quarters), titles_from_data=True)
        chart.set_categories(cats)
    elif kind == "bar+line":
        # 長條主軸（金額）+ 折線次軸（比率）
        chart = BarChart()
        chart.type = "col"
        for r in rows(spec.get("bars", [])):
            chart.add_data(_series_ref(ws, r, n_quarters), titles_from_data=True)
        chart.set_categories(cats)
        line = LineChart()
        for r in rows(spec.get("line", [])):
            line.add_data(_series_ref(ws, r, n_quarters), titles_from_data=True)
        line.set_categories(cats)
        if spec.get("secondary_axis"):
            line.y_axis.axId = 200
            line.y_axis.crosses = "max"
        chart += line
    else:
        return None

    if not chart.series:
        return None
    chart.title = title
    chart.width = th["chart"]["width_cols"] * 1.85   # 約 12 欄寬
    chart.height = th["chart"]["height_rows"] * 0.55  # 約 18 列高
    chart.y_axis.title = th["chart"].get("money_axis_unit") if kind != "line" else None
    _style_chart(chart, th)
    _color_series(chart.series, th)
    return chart


def place_charts(ws, specs: list[dict], row_of: dict[str, int], n_quarters: int, anchor_row: int):
    """統一放在資料區下方固定位置，直向排列。"""
    r = anchor_row
    for spec in specs:
        chart = build_chart(spec, ws, row_of, n_quarters)
        if chart is None:
            continue
        ws.add_chart(chart, f"{get_column_letter(FIRST_DATA_COL)}{r}")
        r += 20
