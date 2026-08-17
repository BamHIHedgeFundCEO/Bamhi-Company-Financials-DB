import { defineEventHandler, getQuery, createError } from 'h3'
import { resolveCompany } from '../utils/cik'
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
    const ref = await resolveCompany(t)
    if (!ref) {
      // Edge case 8：明確錯誤訊息，不回空陣列
      throw createError({
        statusCode: 404,
        statusMessage: 'Not Found',
        message: `找不到「${t}」。請確認 ticker 拼寫；已下市公司與多數 ETF 不在 SEC 申報名單內。`,
      })
    }
    const r = await getFilings(ref, range.fromDate, range.toDate, forms)
    // 以會計季別精準過濾（日期界線因錯開的會計年度刻意放寬）；年報視為 Q4
    const lo = range.fromFy * 4 + range.fromQ
    const hi = range.toFy * 4 + range.toQ
    r.filings = r.filings.filter((f) => {
      const m = f.fiscalPeriod.match(/^FY(\d{4})(?: Q([1-4]))?$/)
      if (!m) return true // 無法解析（如 6-K 無季別）時保留
      const idx = Number(m[1]) * 4 + (m[2] ? Number(m[2]) : 4)
      return idx >= lo && idx <= hi
    })
    results.push(r)
  }
  return tickers.length === 1 ? results[0] : { results }
})
