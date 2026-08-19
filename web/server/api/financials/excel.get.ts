import { defineEventHandler, getQuery, createError, sendRedirect, setHeader } from 'h3'
import { resolveCompany } from '../../utils/cik'
import { getFinancials, loadMap, loadClassSharesVersion } from '../../utils/financials'
import { parseTickers, parseRange, clampPeriods, clampWithLookback } from '../../utils/params'
import { computeValuation } from '../../utils/valuation'
import { getFilings } from '../../utils/filings'
import { getSegments, loadSegmentAxes } from '../../utils/segments'
import type { SegmentsResult } from '../../utils/segments'

/**
 * GET /api/financials/excel?ticker=AAPL&from=2021Q1&to=2026Q2
 *
 * 快取 key：{ticker}_{from}_{to}_{mapVersion}_s{segVersion}_c{classSharesVersion}
 * （對照表、分部設定、多股別股數預算檔改版都會自動失效舊檔）。
 * 流程：R2 已有 → 直接 302 到 R2 URL（Cloud Run 不被喚醒）；
 *       沒有 → 呼叫 Cloud Run excel-service 生成並上傳 R2，再 302。
 * 本 route 不落地任何檔案。
 */
export default defineEventHandler(async (event) => {
  const query = getQuery(event)
  const ticker = parseTickers(query.ticker)[0]
  const range = parseRange(query as Record<string, unknown>)

  const serviceUrl = process.env.EXCEL_SERVICE_URL
  if (!serviceUrl) {
    throw createError({
      statusCode: 503,
      statusMessage: 'Service Unavailable',
      message: 'Excel 服務尚未設定（EXCEL_SERVICE_URL）',
    })
  }

  const ref = await resolveCompany(ticker)
  if (!ref) {
    throw createError({ statusCode: 404, statusMessage: 'Not Found', message: `找不到「${ticker}」` })
  }

  const mapVersion = (await loadMap()).version
  const segVersion = (await loadSegmentAxes()).version
  // 多股別股數的預算檔重跑（新的一季）也要讓舊活頁簿失效，否則使用者下載到的
  // 還是「期末股數 n/a、估值分頁整片空白」的那一版
  const shVersion = (await loadClassSharesVersion()) || '0'
  const from = `${range.fromFy}Q${range.fromQ}`
  const to = `${range.toFy}Q${range.toQ}`
  const cacheKey = `${ref.ticker}_${from}_${to}_${mapVersion}_s${segVersion}_c${shVersion}.xlsx`

  // R2 命中判斷（單純的檔案存在性檢查，不是快取系統）
  const r2Base = process.env.R2_PUBLIC_BASE_URL
  if (r2Base) {
    const head = await fetch(`${r2Base}/${cacheKey}`, { method: 'HEAD' })
    if (head.ok) return sendRedirect(event, `${r2Base}/${cacheKey}`, 302)
  }

  // 未命中 → 轉呼叫 Cloud Run：資料在此準備好，excel-service 不碰 SEC
  // 多抓一年（fromFy-1）供估值 TTM 與 YoY 回溯，算完估值後保留 4 季 lookback（Excel 隱藏），
  // 讓 TTM/YoY 公式從第一顯示欄即可算
  const fin = await getFinancials(ref, range.fromFy - 1, range.toFy)
  fin.valuation = (await computeValuation(fin)) ?? undefined
  clampWithLookback(fin, range, 4)

  // 分部數據：companyfacts 沒有維度，得另外剖析 XBRL instance。
  //
  // 年報 2 份（自帶 3 年比較數 → 約 4–6 個年度）+ 10-Q 4 份（自帶去年同季 →
  // 約 8 個季度）= 12 次 SEC 請求。看起來多，但已申報的財報不可變，解析結果
  // 進持久快取後永久命中，這是每家公司一次性的成本。
  //
  // 這段刻意獨立 try/catch：分部只是加值分頁，抓不到也不該讓整本活頁簿生不出來。
  let segments: SegmentsResult | null = null
  try {
    const today = new Date().toISOString().slice(0, 10)
    const all = await getFilings(ref, '2015-01-01', today)
    const annual = all.filings
      .filter((f) => ['10-K', '20-F', '40-F'].includes(f.form))
      .slice(0, 2)
    const quarters = all.filings.filter((f) => f.form === '10-Q').slice(0, 4)
    const picked = [...annual, ...quarters]
    if (picked.length) segments = await getSegments(ref, picked, all.company)
  } catch (err) {
    console.warn(`[excel] ${ref.ticker} 分部資料略過：`, (err as Error).message)
  }

  const res = await fetch(`${serviceUrl}/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cacheKey, financials: fin, segments, from, to }),
  })
  if (!res.ok) {
    throw createError({
      statusCode: 502,
      statusMessage: 'Bad Gateway',
      message: `Excel 生成失敗（${res.status}）`,
    })
  }
  const ct = res.headers.get('content-type') ?? ''
  if (ct.includes('json')) {
    const { url } = (await res.json()) as { url: string }
    return sendRedirect(event, url, 302)
  }
  // R2 未設定（本地開發）：excel-service 直接串流 .xlsx，原樣轉傳
  setHeader(event, 'Content-Type', 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
  setHeader(event, 'Content-Disposition', `attachment; filename="${cacheKey}"`)
  return new Uint8Array(await res.arrayBuffer())
})
