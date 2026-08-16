import { defineEventHandler, getQuery, createError } from 'h3'
import { resolveTicker } from '../utils/cik'
import { getFinancials } from '../utils/financials'
import { parseTickers, parseRange, clampPeriods } from '../utils/params'
import { computeValuation } from '../utils/valuation'

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
    const fin = clampPeriods(await getFinancials(ref, range.fromFy, range.toFy), range)
    if (query.valuation !== '0') fin.valuation = (await computeValuation(fin)) ?? undefined
    results.push(fin)
  }
  return tickers.length === 1 ? results[0] : { results }
})
