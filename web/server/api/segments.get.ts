import { defineEventHandler, getQuery, createError } from 'h3'
import { resolveCompany } from '../utils/cik'
import { getFilings } from '../utils/filings'
import { getSegments } from '../utils/segments'
import { parseTickers } from '../utils/params'

/**
 * GET /api/segments?ticker=AAPL
 * GET /api/segments?ticker=AAPL&filings=3        年報份數（1–6，預設 2）
 * GET /api/segments?ticker=AAPL&quarterly=0      關掉季度分部（預設開）
 * GET /api/segments?ticker=AAPL&quarters=6       10-Q 份數（0–8，預設 4）
 *
 * → { company, cik, ticker, configVersion, periods, axes[], warnings[] }
 *
 * **請求預算**：一份申報要 2 次 SEC 請求（index.json 定位 instance + 抓 instance）。
 *   年報 2 份 = 4 次；10-Q 4 份 = 8 次；預設合計 12 次。
 *
 * 之所以份數可以壓這麼低，是因為申報自帶比較期：一份 10-K 帶 3 個年度、一份 10-Q
 * 帶當季與去年同季。4 份 10-Q 就能湊出約 8 個季度，不必一季抓一份。
 *
 * 已申報的財報不可變 → 解析結果進持久快取後永遠命中，同一份申報一輩子只抓一次，
 * 所以 12 次是「這家公司史上第一次被查」的一次性成本，之後是 0。
 */
export default defineEventHandler(async (event) => {
  const query = getQuery(event)
  const ticker = parseTickers(query.ticker)[0]

  const nFilings = Math.min(6, Math.max(1, Number(query.filings ?? 2) || 2))
  const quarterly = query.quarterly !== '0'
  const nQuarters = quarterly ? Math.min(8, Math.max(0, Number(query.quarters ?? 4) || 0)) : 0

  const ref = await resolveCompany(ticker)
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
  // 每份 10-Q 自帶去年同季，4 份就能湊出約 8 個季度。外國發行人沒有 10-Q，
  // 這裡自然會是空陣列 —— 只有年度分部，不是錯誤。
  const quarters = all.filings.filter((f) => f.form === '10-Q').slice(0, nQuarters)
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
