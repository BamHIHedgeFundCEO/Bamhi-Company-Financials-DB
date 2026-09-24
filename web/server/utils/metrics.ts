/**
 * 指標公式求值器：`xbrl_zh_map.json` 的 `derived[].formula` → 逐期數值。
 *
 * 這是 `excel-service/formulas.py` 的 TypeScript 對照實作。**兩邊的語意必須一致**，
 * 否則同一個指標在網頁與 Excel 會給出不同的數字，而且沒有任何地方會報錯：
 *
 *   identifier            該期的科目值，或前面已定義指標的該期值
 *   identifier[t-4]       往前推 4 期（外國發行人年度模式：推 1 期）
 *   avg(identifier)       (本期 + 前一期) / 2；沒有前一期時退回本期
 *   + - * / ( ) 數字
 *
 * 年度模式（IFRS 外國發行人，一欄＝一整年）的三個轉換與 Excel 端逐字相同：
 *   [t-1]（上一季）無意義 → 整個指標不產出
 *   [t-4] → 前 1 欄
 *   「× 4 年化」→ × 1、週轉天數的 91.25 → 365
 *
 * 缺值分五種，**不能混成同一個符號**（見 applicability.ts 與 workbook.py 的同一條規則）：
 *   missing        該申報卻抓不到 → 頁面寫 n/a，讀者要自己去 EDGAR 對
 *   inapplicable   這家公司本來就沒有這一行（銀行沒有存貨）→ 頁面寫「—」
 *   window         比較基期落在所選期間之外（第一年的年增率）→ 頁面寫「—」
 *   dimension_only 報表上有這一行，但公司**只用維度揭露** → 頁面寫「僅維度揭露」
 *   undisclosed    這家公司**別的期別有值、這一期沒有** → 頁面寫「本期未揭露」
 *   zero_divisor   分母是公司自己申報的 0 → 比值無定義 → 頁面寫「分母為 0」
 *
 * 後兩種是從 missing 裡拆出來的，理由是它們都不是「我們漏抓」：
 *
 *   undisclosed：BJ／BMRN／APPF／AMCR 實測合約負債連續申報了十幾期之後停掉，
 *     最新幾期整片空白。寫 n/a 等於叫讀者去 EDGAR 找一個公司沒寫的數字；寫「—」
 *     又會說謊 ——「不適用」在 metrics 這一層是**整個指標**作廢，連算得出來的
 *     期別一起清掉（AAON／AXTA 那條規則）。它要的是一個逐格的第四種符號。
 *   zero_divisor：ALGM 與 AUR 的合約負債是公司自己申報的 0（companyfacts 上
 *     六個年度都是 0，不是抓不到），年增率因此是 0/0。那是「無定義」不是「缺資料」，
 *     混進 n/a 會讓「我們漏抓」的統計憑空多出幾家。
 */
import type { DerivedMetric, LineItem } from './financials'

export type MetricReason = 'ok' | 'missing' | 'inapplicable' | 'window'
  | 'undisclosed' | 'zero_divisor' | 'dimension_only'

export interface MetricCell {
  value: number | null
  reason: MetricReason
  /** 該期的輸入含 Q4 推算值（年報 − 前三季）→ 這一格也是推算的 */
  isEstimated: boolean
}

export interface MetricSeries {
  id: string
  zh: string
  en: string
  group: string
  formula: string
  desc: string
  fmt?: string
  /** 整條指標對這家公司不適用（公式用到的任一科目不適用） */
  inapplicable: boolean
  cells: MetricCell[]
}

// ── 解析 ────────────────────────────────────────────────
type Node =
  | { k: 'num'; v: number }
  | { k: 'ref'; id: string; lag: number }
  | { k: 'avg'; id: string }
  | { k: 'bin'; op: string; l: Node; r: Node }
  | { k: 'neg'; x: Node }

class Parser {
  private i = 0
  constructor(private readonly s: string) {}

  private ws() { while (this.i < this.s.length && /\s/.test(this.s[this.i]!)) this.i++ }
  private peek() { this.ws(); return this.s[this.i] }
  private eat(ch: string) { this.ws(); if (this.s[this.i] !== ch) return false; this.i++; return true }

  parse(): Node | null {
    const n = this.expr()
    this.ws()
    return n && this.i >= this.s.length ? n : null
  }

  private expr(): Node | null {
    let l = this.term()
    if (!l) return null
    for (;;) {
      const c = this.peek()
      if (c !== '+' && c !== '-') return l
      this.i++
      const r = this.term()
      if (!r) return null
      l = { k: 'bin', op: c, l, r }
    }
  }

  private term(): Node | null {
    let l = this.factor()
    if (!l) return null
    for (;;) {
      const c = this.peek()
      if (c !== '*' && c !== '/') return l
      this.i++
      const r = this.factor()
      if (!r) return null
      l = { k: 'bin', op: c, l, r }
    }
  }

  private factor(): Node | null {
    if (this.eat('-')) {
      const x = this.factor()
      return x ? { k: 'neg', x } : null
    }
    if (this.eat('(')) {
      const x = this.expr()
      return x && this.eat(')') ? x : null
    }
    this.ws()
    const num = /^\d+(?:\.\d+)?/.exec(this.s.slice(this.i))
    if (num) {
      this.i += num[0].length
      return { k: 'num', v: Number(num[0]) }
    }
    const ident = /^[A-Za-z_][A-Za-z0-9_]*/.exec(this.s.slice(this.i))
    if (!ident) return null
    this.i += ident[0].length
    const id = ident[0]
    if (id === 'avg') {
      if (!this.eat('(')) return null
      this.ws()
      const inner = /^[A-Za-z_][A-Za-z0-9_]*/.exec(this.s.slice(this.i))
      if (!inner) return null
      this.i += inner[0].length
      return this.eat(')') ? { k: 'avg', id: inner[0] } : null
    }
    // identifier[t] / identifier[t-N]
    const br = /^\[t(?:-(\d+))?\]/.exec(this.s.slice(this.i))
    if (br) {
      this.i += br[0].length
      return { k: 'ref', id, lag: br[1] ? Number(br[1]) : 0 }
    }
    return { k: 'ref', id, lag: 0 }
  }
}

/** 公式用到的科目／指標 id（`avg` 是語法字，不算） */
export function formulaInputs(formula: string): string[] {
  const out = new Set<string>()
  for (const m of formula.matchAll(/[A-Za-z_][A-Za-z0-9_]*/g)) {
    if (m[0] !== 'avg' && m[0] !== 't') out.add(m[0])
  }
  return [...out]
}

/** 年度模式的字面轉換：與 formulas.py 的 translate(annual=True) 逐字相同 */
function annualize(formula: string): string {
  return formula.replace(/\*\s*4\b/g, '* 1').replaceAll('91.25', '365')
}

// ── 求值 ────────────────────────────────────────────────
interface Env {
  /** id → 逐期格子（科目與先前算好的指標共用同一個名字空間） */
  get(id: string, idx: number): MetricCell | undefined
}

const OK = (value: number | null, isEstimated: boolean): MetricCell =>
  value == null || !Number.isFinite(value)
    ? { value: null, reason: 'missing', isEstimated }
    : { value, reason: 'ok', isEstimated }

/**
 * 兩個缺值理由合併。順序就是「誰比較具體」：
 *   inapplicable > window > missing > undisclosed > zero_divisor
 * **missing 要壓過 undisclosed**：只要有任一個輸入是真的抓不到，整格就不能說成
 * 「這家公司本期沒揭露」—— 那會把我們自己的缺口講成公司的選擇。
 */
function worse(a: MetricReason, b: MetricReason): MetricReason {
  if (a === 'inapplicable' || b === 'inapplicable') return 'inapplicable'
  if (a === 'window' || b === 'window') return 'window'
  if (a === 'missing' || b === 'missing') return 'missing'
  if (a === 'dimension_only' || b === 'dimension_only') return 'dimension_only'
  if (a === 'undisclosed' || b === 'undisclosed') return 'undisclosed'
  if (a === 'zero_divisor' || b === 'zero_divisor') return 'zero_divisor'
  return 'ok'
}

function evalNode(n: Node, idx: number, env: Env): MetricCell {
  switch (n.k) {
    case 'num':
      return { value: n.v, reason: 'ok', isEstimated: false }
    case 'neg': {
      const x = evalNode(n.x, idx, env)
      return x.reason === 'ok' && x.value != null
        ? { ...x, value: -x.value }
        : x
    }
    case 'ref': {
      if (idx - n.lag < 0) return { value: null, reason: 'window', isEstimated: false }
      const c = env.get(n.id, idx - n.lag)
      return c ?? { value: null, reason: 'missing', isEstimated: false }
    }
    case 'avg': {
      const cur = env.get(n.id, idx)
      if (!cur || cur.reason !== 'ok' || cur.value == null) {
        return cur ?? { value: null, reason: 'missing', isEstimated: false }
      }
      // 前一期缺就退回本期單點（與 Excel 端一致：沒有前一欄時 avg(x) = x）
      const prev = idx > 0 ? env.get(n.id, idx - 1) : undefined
      const est = cur.isEstimated || !!prev?.isEstimated
      if (!prev || prev.reason !== 'ok' || prev.value == null) {
        return { value: cur.value, reason: 'ok', isEstimated: est }
      }
      return { value: (cur.value + prev.value) / 2, reason: 'ok', isEstimated: est }
    }
    case 'bin': {
      const l = evalNode(n.l, idx, env)
      const r = evalNode(n.r, idx, env)
      if (l.reason !== 'ok' || r.reason !== 'ok' || l.value == null || r.value == null) {
        return { value: null, reason: worse(l.reason, r.reason), isEstimated: false }
      }
      const est = l.isEstimated || r.isEstimated
      // 除以 0 一律回缺值。Excel 端靠 IFERROR 吃掉 #DIV/0!，這裡要自己擋，
      // 不然 Infinity 會一路傳下去變成畫得出來的假線
      // 分母是公司自己申報的 0 → 比值無定義，不是「抓不到」
      if (n.op === '/' && r.value === 0) return { value: null, reason: 'zero_divisor', isEstimated: est }
      const v = n.op === '+' ? l.value + r.value
        : n.op === '-' ? l.value - r.value
          : n.op === '*' ? l.value * r.value
            : l.value / r.value
      return OK(v, est)
    }
  }
}

/**
 * 逐期算出所有指標。
 *
 * `derived` 的順序就是相依順序（指標可以引用**前面**定義過的指標，如
 * 毛利率年變化引用毛利率），與 Excel 端把指標寫成同一張表的上下列是同一件事。
 *
 * @param notApplicable 科目層的不適用集合（`lineItems[].applicable === false`）
 */
export function computeMetrics(
  derived: DerivedMetric[],
  lineItems: LineItem[],
  periods: string[],
  annual: boolean,
  /**
   * `periods` 前面有幾期是**讀者看不到的** lookback（signals 多取 8 期來算 [t-4]／TTM）。
   * 只影響 undisclosed 的判定，不影響任何數值。
   */
  lookback = 0,
): Map<string, MetricSeries> {
  const byConcept = new Map<string, LineItem>(lineItems.map((li) => [li.id, li]))
  // 「這家公司有沒有申報過這個科目」——只看**讀者看得見的那幾欄**有沒有任何一格有值。
  // 用它把「公司本期沒揭露」從「我們漏抓」裡分出來：整段都沒有才是後者。
  // **不能把 lookback 那幾期算進來**：AMD 的合約負債停在 2020 年、ALK 2020 年之後
  // 改成只用維度揭露，兩家在畫面上的 18 欄都是空的。拿看不見的舊值當「別的期別有值」
  // 的證據，等於請讀者去比對一個這張表上不存在的東西 —— 那兩家維持 n/a 才誠實
  const visible = periods.slice(lookback)
  const everReported = new Set<string>(
    lineItems.filter((li) => visible.some((p) => li.values[p]?.value != null)).map((li) => li.id))
  const out = new Map<string, MetricSeries>()
  const metricIds = new Set(derived.map((m) => m.id))

  // 科目層不適用 → 沿著「指標引用指標」往下傳。工作表那邊叫 _metric_na_map，
  // 同一條規則：公式用到的任一輸入不適用，整個指標就不適用（不是查不到）
  const naConcept = new Set(lineItems.filter((li) => li.applicable === false).map((li) => li.id))
  const naMetric = new Set<string>()
  for (const m of derived) {
    const ins = formulaInputs(m.formula)
    if (ins.some((i) => naConcept.has(i))) naMetric.add(m.id)
  }
  for (let pass = 0; pass < derived.length; pass++) {
    let changed = false
    for (const m of derived) {
      if (naMetric.has(m.id)) continue
      if (formulaInputs(m.formula).some((i) => metricIds.has(i) && naMetric.has(i))) {
        naMetric.add(m.id)
        changed = true
      }
    }
    if (!changed) break
  }

  const env: Env = {
    get(id, idx) {
      const p = periods[idx]
      if (p == null) return undefined
      const li = byConcept.get(id)
      if (li) {
        const cell = li.values[p]
        if (!cell || cell.value == null) {
          // 這家公司別的期別有值、只有這一期沒有 → 是公司本期沒揭露，不是我們漏抓
          // 順序就是「誰比較具體」：看得見的期別有值 → 是公司本期沒揭露（最具體）；
          // 整段都沒有、而且離線盤點說這家只用維度揭露 → 是這條路取不到；都不是才是漏抓
          const reason: MetricReason = li.applicable === false
            ? 'inapplicable'
            : everReported.has(id)
              ? 'undisclosed'
              : li.dimensionOnly
                ? 'dimension_only'
                : 'missing'
          return { value: null, reason, isEstimated: false }
        }
        return { value: cell.value, reason: 'ok', isEstimated: !!cell.isEstimated }
      }
      return out.get(id)?.cells[idx]
    },
  }

  for (const m of derived) {
    const src = annual ? annualize(m.formula) : m.formula
    // [t-1]（上一季）在一欄＝一整年的年度資料上沒有意義，整條不產出
    const parsed = annual && m.formula.includes('[t-1]') ? null : new Parser(src).parse()
    const ast = parsed ? annualLag(parsed, annual) : null
    const inapplicable = naMetric.has(m.id)
    const cells: MetricCell[] = periods.map((_, i) =>
      // 解析不出來（年度模式的季增率）與科目不適用都寫「—」：
      // 兩者都不是「該有卻抓不到」，寫 n/a 會叫讀者去 EDGAR 找一個不存在的東西
      inapplicable || !ast
        ? { value: null, reason: 'inapplicable' as const, isEstimated: false }
        : evalNode(ast, i, env))
    out.set(m.id, {
      id: m.id,
      zh: m.zh,
      en: m.en,
      group: m.group,
      formula: m.formula,
      desc: m.desc,
      fmt: m.fmt,
      inapplicable,
      cells,
    })
  }
  return out
}

/** 年度模式：[t-4]（去年同季）＝前 1 欄。與 Excel 端的 `lag_n // 4` 同義 */
function annualLag(n: Node, annual: boolean): Node {
  if (!annual) return n
  switch (n.k) {
    case 'ref': return n.lag ? { ...n, lag: Math.floor(n.lag / 4) } : n
    case 'neg': return { k: 'neg', x: annualLag(n.x, annual) }
    case 'bin': return { k: 'bin', op: n.op, l: annualLag(n.l, annual), r: annualLag(n.r, annual) }
    default: return n
  }
}
