import { createError } from 'h3'

/** 查詢參數共用解析。上限：5 檔 ticker、40 季（≈10 年），訊息用友善文案。 */

export const MAX_TICKERS = 5
export const MAX_QUARTERS = 40

export function parseTickers(raw: unknown): string[] {
  const list = String(raw ?? '')
    .split(',')
    .map((t) => t.trim().toUpperCase())
    .filter(Boolean)
  if (!list.length) {
    throw createError({ statusCode: 400, statusMessage: 'Bad Request', message: '請輸入至少一個 ticker' })
  }
  if (list.length > MAX_TICKERS) {
    throw createError({
      statusCode: 400,
      statusMessage: 'Bad Request',
      message: `單次比較上限 ${MAX_TICKERS} 檔，可分批查詢`,
    })
  }
  return list
}

export interface QuarterRange {
  fromFy: number
  fromQ: number
  toFy: number
  toQ: number
  fromDate: string // 概略日期範圍（過濾 reportDate 用）
  toDate: string
  nQuarters: number
}

/** "2021Q1" 之類。years=N 為捷徑（往回 N 年）。 */
export function parseRange(query: Record<string, unknown>): QuarterRange {
  const now = new Date()
  let fromFy: number, fromQ: number, toFy: number, toQ: number

  const qre = /^(\d{4})Q([1-4])$/i
  const from = String(query.from ?? '')
  const to = String(query.to ?? '')
  if (qre.test(from) && qre.test(to)) {
    ;[, fromFy, fromQ] = from.match(qre)!.map(Number) as unknown as [number, number, number]
    ;[, toFy, toQ] = to.match(qre)!.map(Number) as unknown as [number, number, number]
  } else {
    const years = Math.min(Number(query.years ?? 5) || 5, 10)
    toFy = now.getFullYear() + 1 // 會計年度可能超前曆年（NVDA FY2026 在 2025）
    toQ = 4
    fromFy = toFy - years
    fromQ = 1
  }

  const n = (toFy - fromFy) * 4 + (toQ - fromQ) + 1
  if (n <= 0) {
    throw createError({ statusCode: 400, statusMessage: 'Bad Request', message: '起始季度不可晚於結束季度' })
  }
  if (n > MAX_QUARTERS) {
    throw createError({
      statusCode: 400,
      statusMessage: 'Bad Request',
      message: `單次查詢上限 ${MAX_QUARTERS} 季（約 10 年），可分批查詢`,
    })
  }

  // 日期界線放寬一年涵蓋錯開的會計年度
  const fromDate = `${fromFy - 1}-01-01`
  const toDate = `${toFy + 1}-12-31`
  return { fromFy, fromQ, toFy, toQ, fromDate, toDate, nQuarters: n }
}

/**
 * 裁到 range，但保留起點前 lookback 季（供 YoY[t-4]、TTM 滾動四季的 Excel 公式從第一顯示欄即可算）。
 * 回傳同時標記 lookbackCount = 前面幾欄是 lookback（Excel 隱藏之）。
 */
export function clampWithLookback<
  T extends {
    periods: string[]
    lineItems: { values: Record<string, unknown> }[]
    valuation?: { rows: { values: Record<string, unknown> }[] }
    lookbackCount?: number
  },
>(fin: T, range: QuarterRange, lookback = 4): T {
  const lo = range.fromFy * 4 + range.fromQ - lookback
  const hi = range.toFy * 4 + range.toQ
  const idxOf = (p: string) => {
    const m = p.match(/^FY(\d{4})(?: Q([1-4]))?$/)
    return m ? Number(m[1]) * 4 + (m[2] ? Number(m[2]) : 4) : -1
  }
  const inRange = (p: string) => {
    const i = idxOf(p)
    return i >= lo && i <= hi
  }
  fin.periods = fin.periods.filter(inRange)
  for (const li of fin.lineItems) {
    li.values = Object.fromEntries(Object.entries(li.values).filter(([k]) => inRange(k)))
  }
  if (fin.valuation) {
    for (const r of fin.valuation.rows) {
      r.values = Object.fromEntries(Object.entries(r.values).filter(([k]) => inRange(k)))
    }
  }
  const displayFrom = range.fromFy * 4 + range.fromQ
  fin.lookbackCount = fin.periods.filter((p) => idxOf(p) < displayFrom).length
  return fin
}

/** 把 financials 結果裁到 range 的起訖「季度」（getFinancials 只吃整年）。 */
export function clampPeriods<T extends { periods: string[]; lineItems: { values: Record<string, unknown> }[] }>(
  fin: T,
  range: QuarterRange,
): T {
  const lo = range.fromFy * 4 + range.fromQ
  const hi = range.toFy * 4 + range.toQ
  const inRange = (p: string) => {
    const m = p.match(/^FY(\d{4})(?: Q([1-4]))?$/)
    if (!m) return false
    // 年度模式（"FY2024"）視為 Q4 位置
    const idx = Number(m[1]) * 4 + (m[2] ? Number(m[2]) : 4)
    return idx >= lo && idx <= hi
  }
  fin.periods = fin.periods.filter(inRange)
  for (const li of fin.lineItems) {
    li.values = Object.fromEntries(Object.entries(li.values).filter(([k]) => inRange(k)))
  }
  // 估值倍數（若已算）同步裁到顯示範圍——TTM 已在較寬窗口算好，此處只裁顯示
  const val = (fin as { valuation?: { rows: { values: Record<string, unknown> }[] } }).valuation
  if (val) {
    for (const r of val.rows) {
      r.values = Object.fromEntries(Object.entries(r.values).filter(([k]) => inRange(k)))
    }
  }
  return fin
}
