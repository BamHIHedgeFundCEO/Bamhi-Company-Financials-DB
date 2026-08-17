import { defineEventHandler, getQuery, createError } from 'h3'
import { resolveTicker } from '../utils/cik'
import { getFilings } from '../utils/filings'
import { getSegments } from '../utils/segments'
import { parseTickers } from '../utils/params'

/**
 * GET /api/segments?ticker=AAPL
 * GET /api/segments?ticker=AAPL&filings=3        年報份數（1–6，預設 2）
 * GET /api/segments?ticker=AAPL&quarterly=1      額外納入季報（分部的季度數字）
 *
 * → { company, cik, ticker, configVersion, periods, axes[], warnings[] }
 *
 * **請求預算**：一份申報要 2 次 SEC 請求（index.json 定位 instance + 抓 instance）。
 * 預設只抓 2 份年報 = 4 次請求，但一份 10-K 自帶 3 年比較數，所以能蓋到約 6 年的
 * 年度分部資料。已申報的財報不可變 → 解析結果進持久快取後永遠命中，同一份申報
 * 一輩子只抓一次。季度分部要 quarterly=1，成本高很多（每季一份 10-Q）。
 */
export default defineEventHandler(async (event) => {
  const query = getQuery(event)
  const ticker = parseTickers(query.ticker)[0]

  const nFilings = Math.min(6, Math.max(1, Number(query.filings ?? 2) || 2))
  const quarterly = query.quarterly === '1'

  const ref = await resolveTicker(ticker)
  if (!ref) {
    throw createError({
      statusCode: 404,
      statusMessage: 'Not Found',
      message: `找不到「${ticker}」。請確認 ticker 拼寫；已下市公司與多數 ETF 不在 SEC 申報名單內。`,
    })
  }

  const today = new Date().toISOString().slice(0, 10)
  const all = await getFilings(ref, '2015-01-01', today)

  // 年報優先：一份 10-K/20-F 就帶 3 年比較數，抓兩份即涵蓋約 6 年，最省請求
  const annualForms = ['10-K', '20-F', '40-F']
  const annual = all.filings.filter((f) => annualForms.includes(f.form)).slice(0, nFilings)
  const quarters = quarterly
    ? all.filings.filter((f) => f.form === '10-Q').slice(0, 4 * nFilings)
    : []
  const picked = [...annual, ...quarters]

  if (picked.length === 0) {
    throw createError({
      statusCode: 404,
      statusMessage: 'Not Found',
      message: `${ref.ticker} 在 SEC 沒有可解析的年報`,
    })
  }

  return getSegments(ref, picked, all.company)
})
