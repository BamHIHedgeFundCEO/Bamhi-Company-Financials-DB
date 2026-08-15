"""
指標公式翻譯器：xbrl_zh_map.json 的 formula 字串 → Excel 公式。
關鍵指標分頁必須寫公式不能寫算好的數值（使用者改假設整張表要能重算）。

支援語法：
  identifier            → 該 concept / 前面已定義 metric 的同欄儲存格
  identifier[t-4]       → 往前推 4 欄（不足時整條公式回 None → n/a）
  avg(identifier)       → (本欄 + 前一欄)/2；無前一欄時退回本欄
  + - * / ( ) 數字
所有除法整條包 IFERROR(..., "n/a")。
"""
import re
from openpyxl.utils import get_column_letter

_TOKEN = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)(\[t(?:-(\d+))?\])?|(\d+\.?\d*)|([()+\-*/])|(\s+)")

FIRST_DATA_COL = 3  # C 欄起為季度


class RefResolver:
    """id → (工作表名, 列號)。concepts 指向三大報表分頁；先前定義的 metric 指向關鍵指標分頁。"""

    def __init__(self):
        self.refs: dict[str, tuple[str, int]] = {}

    def add(self, item_id: str, sheet: str, row: int):
        self.refs[item_id] = (sheet, row)

    def cell(self, item_id: str, col: int) -> str | None:
        if item_id not in self.refs or col < FIRST_DATA_COL:
            return None
        sheet, row = self.refs[item_id]
        return f"'{sheet}'!{get_column_letter(col)}{row}"


def translate(formula: str, resolver: RefResolver, col: int) -> str | None:
    """回傳不含開頭 = 的公式；引用解析失敗（缺科目或期數不足）回 None。"""
    out: list[str] = []
    pos = 0
    has_div = "/" in formula
    while pos < len(formula):
        m = _TOKEN.match(formula, pos)
        if not m:
            return None
        ident, bracket, lag, num, op, ws = m.groups()
        pos = m.end()
        if ws:
            continue
        if num:
            out.append(num)
        elif op:
            out.append(op)
        elif ident == "avg":
            # avg(x) → (x本欄 + x前一欄)/2
            m2 = re.match(r"\(\s*([A-Za-z_][A-Za-z0-9_]*)\s*\)", formula[pos:])
            if not m2:
                return None
            pos += m2.end()
            cur = resolver.cell(m2.group(1), col)
            prev = resolver.cell(m2.group(1), col - 1)
            if cur is None:
                return None
            out.append(f"(({cur}+{prev})/2)" if prev else cur)
        elif ident:
            lag_n = int(lag) if lag else 0
            ref = resolver.cell(ident, col - lag_n)
            if ref is None:
                return None
            out.append(ref)
        else:
            return None
    expr = "".join(out)
    return f'IFERROR({expr},"n/a")' if has_div else expr
