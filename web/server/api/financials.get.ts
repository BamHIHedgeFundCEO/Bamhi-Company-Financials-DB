import { defineEventHandler, getQuery, createError, setHeader } from 'h3'
import { resolveCompany } from '../utils/cik'
import { getFinancials } from '../utils/financials'
import { parseTickers, parseRange, clampPeriods } from '../utils/params'
import { computeValuation } from '../utils/valuation'

/**
 * 瘦身：拿掉網頁用不到的逐格稽核欄位與指標定義表。
 *
 * 逐格的 accessionOrForm / filed / origFiled / endDate 是給對帳與稽核用的，
 * 財務報表分頁一格都沒用到，卻佔了回應的 44%（BW 116KB → 65KB）。
 * `derived` 是指標的公式與說明，那一頁自己算比率，也用不到。
 * 只在 `lean=1` 時作用，Excel／CSV 與任何其他呼叫端完全不受影響。
 */
function lean(fin: { lineItems: { values: Record<string, Record<string, unknown>> }[]; derived?: unknown }) {
  for (const li of fin.lineItems) {
    for (const cell of Object.values(li.values)) {
      delete cell.accessionOrForm
      delete cell.filed
      delete cell.origFiled
      delete cell.endDate
    }
  }
  fin.derived = []
}

/**
 * GET /api/financials?ticker=AAPL&years=5
 * GET /api/financials?ticker=AAPL&from=2021Q1&to=2026Q2
 * → { company, cik, mapVersion, periods, lineItems, derived }
 */
export default defineEventHandler(async (event) => {
  const query = getQuery(event)
  const tickers = parseTickers(query.ticker)
  const range = parseRange(query as Record<string, unknown>)

  setHeader(event, 'Cache-Control', 'public, s-maxage=3600, stale-while-revalidate=86400')
  const results = []
  for (const t of tickers) {
    const ref = await resolveCompany(t)
    if (!ref) {
      throw createError({
        statusCode: 404,
        statusMessage: 'Not Found',
        message: `找不到「${t}」。請確認 ticker 拼寫；已下市公司與多數 ETF 不在 SEC 申報名單內。`,
      })
    }
    // 多抓一年供估值 TTM 回溯，算完再裁到顯示範圍
    const fin = await getFinancials(ref, range.fromFy - 1, range.toFy)
    if (query.valuation !== '0') fin.valuation = (await computeValuation(fin)) ?? undefined
    clampPeriods(fin, range)
    if (query.lean === '1') lean(fin)
    results.push(fin)
  }
  return tickers.length === 1 ? results[0] : { results }
})
