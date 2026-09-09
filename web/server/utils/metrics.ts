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
 * 缺值分三種，**不能混成同一個符號**（見 applicability.ts 與 workbook.py 的同一條規則）：
 *   missing        該申報卻抓不到 → 頁面寫 n/a，讀者要自己去 EDGAR 對
 *   inapplicable   這家公司本來就沒有這一行（銀行沒有存貨）→ 頁面寫「—」
 *   window         比較基期落在所選期間之外（第一年的年增率）→ 頁面寫「—」
 */
import type { DerivedMetric, LineItem } from './financials'

export type MetricReason = 'ok' | 'missing' | 'inapplicable' | 'window'

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

/** 兩個缺值理由合併：window（基期不在區間內）比 missing 更具體，優先保留 */
function worse(a: MetricReason, b: MetricReason): MetricReason {
  if (a === 'inapplicable' || b === 'inapplicable') return 'inapplicable'
  if (a === 'window' || b === 'window') return 'window'
  if (a === 'missing' || b === 'missing') return 'missing'
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
      if (n.op === '/' && r.value === 0) return { value: null, reason: 'missing', isEstimated: est }
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
): Map<string, MetricSeries> {
  const byConcept = new Map<string, LineItem>(lineItems.map((li) => [li.id, li]))
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
          return { value: null, reason: li.applicable === false ? 'inapplicable' : 'missing', isEstimated: false }
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
