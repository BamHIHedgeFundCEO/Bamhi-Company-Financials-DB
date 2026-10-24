import { defineEventHandler, getQuery, createError, setHeader } from 'h3'
import { resolveCompany } from '../utils/cik'
import { getFinancials } from '../utils/financials'
import { parseTickers } from '../utils/params'

/**
 * GET /api/summary?ticker=NVDA —— 股譜標的頁（/e/nvda）側欄的財報卡。
 *
 * 只回最近 8 期的四個數字（營收、毛利率、營業利益率、淨利），回應約 1KB。
 * 與 /api/financials 同一條路、同一層快取，零額外 SEC 請求。
 * 比率在這裡算：只有分子分母都是申報值（或標了推算）時才給，缺一個就是 null ——
 * 圖上 null 斷線，不補 0（SEC 無標籤 ≠ 零）。
 */
const N = 8

export default defineEventHandler(async (event) => {
  const ticker = parseTickers(getQuery(event).ticker)[0]
  setHeader(event, 'Cache-Control', 'public, s-maxage=3600, stale-while-revalidate=86400')
  const ref = await resolveCompany(ticker)
  if (!ref) throw createError({ statusCode: 404, statusMessage: 'Not Found', message: `找不到「${ticker}」` })

  const y = new Date().getFullYear()
  const fin = await getFinancials(ref, y - 3, y + 1)
  const periods = fin.periods.slice(-N)
  const row = (id: string) => {
    const li = fin.lineItems.find((l) => l.id === id)
    return periods.map((p) => {
      const v = li?.values[p]?.value
      return typeof v === 'number' && Number.isFinite(v) ? v : null
    })
  }
  const revenue = row('revenue')
  const ratio = (num: (number | null)[]) =>
    num.map((n, i) => (n != null && revenue[i] != null && revenue[i]! > 0 ? n / revenue[i]! : null))

  return {
    company: fin.company,
    ticker: fin.ticker,
    currency: fin.currency,
    periodicity: fin.periodicity,
    periods,
    revenue,
    grossMargin: ratio(row('gross_profit')),
    operatingMargin: ratio(row('operating_income')),
    netIncome: row('net_income'),
  }
})
