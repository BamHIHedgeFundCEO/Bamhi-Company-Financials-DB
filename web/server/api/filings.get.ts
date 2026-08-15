import { defineEventHandler, getQuery, createError } from 'h3'
import { resolveTicker } from '../utils/cik'
import { getFilings } from '../utils/filings'
import { parseTickers, parseRange } from '../utils/params'

/**
 * GET /api/filings?ticker=AAPL&years=5&forms=10-K,10-Q
 * GET /api/filings?ticker=AAPL&from=2021Q1&to=2026Q2
 * → { results: [{ company, cik, isForeignIssuer, filings: [...] }] }
 */
export default defineEventHandler(async (event) => {
  const query = getQuery(event)
  const tickers = parseTickers(query.ticker)
  const range = parseRange(query as Record<string, unknown>)
  const forms = query.forms
    ? String(query.forms).split(',').map((f) => f.trim()).filter(Boolean)
    : undefined

  const results = []
  for (const t of tickers) {
    const ref = await resolveTicker(t)
    if (!ref) {
      // Edge case 8：明確錯誤訊息，不回空陣列
      throw createError({
        statusCode: 404,
        statusMessage: 'Not Found',
        message: `找不到「${t}」。請確認 ticker 拼寫；已下市公司與多數 ETF 不在 SEC 申報名單內。`,
      })
    }
    results.push(await getFilings(ref, range.fromDate, range.toDate, forms))
  }
  return tickers.length === 1 ? results[0] : { results }
})
