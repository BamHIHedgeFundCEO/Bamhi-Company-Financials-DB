import { defineEventHandler, getQuery, createError } from 'h3'
import { resolveTicker } from '../utils/cik'
import { getFinancials } from '../utils/financials'
import { parseTickers, parseRange } from '../utils/params'

/**
 * GET /api/financials?ticker=AAPL&years=5
 * GET /api/financials?ticker=AAPL&from=2021Q1&to=2026Q2
 * → { company, cik, mapVersion, periods, lineItems, derived }
 */
export default defineEventHandler(async (event) => {
  const query = getQuery(event)
  const tickers = parseTickers(query.ticker)
  const range = parseRange(query as Record<string, unknown>)

  const results = []
  for (const t of tickers) {
    const ref = await resolveTicker(t)
    if (!ref) {
      throw createError({
        statusCode: 404,
        statusMessage: 'Not Found',
        message: `找不到「${t}」。請確認 ticker 拼寫；已下市公司與多數 ETF 不在 SEC 申報名單內。`,
      })
    }
    const fin = await getFinancials(ref, range.fromFy, range.toFy)
    // 篩掉範圍外季度
    const inRange = (p: string) => {
      const m = p.match(/^FY(\d{4}) Q([1-4])$/)
      if (!m) return false
      const idx = Number(m[1]) * 4 + Number(m[2])
      return idx >= range.fromFy * 4 + range.fromQ && idx <= range.toFy * 4 + range.toQ
    }
    fin.periods = fin.periods.filter(inRange)
    for (const li of fin.lineItems) {
      li.values = Object.fromEntries(Object.entries(li.values).filter(([k]) => inRange(k)))
    }
    results.push(fin)
  }
  return tickers.length === 1 ? results[0] : { results }
})
