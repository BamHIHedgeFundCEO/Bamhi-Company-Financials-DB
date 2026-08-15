import { defineEventHandler, getQuery, createError, sendRedirect } from 'h3'
import { resolveTicker } from '../../utils/cik'
import { getFinancials, loadMap } from '../../utils/financials'
import { parseTickers, parseRange } from '../../utils/params'

/**
 * GET /api/financials/excel?ticker=AAPL&from=2021Q1&to=2026Q2
 *
 * 快取 key：{ticker}_{from}_{to}_{mapVersion}（mapVersion 變更自動失效舊檔）。
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

  const ref = await resolveTicker(ticker)
  if (!ref) {
    throw createError({ statusCode: 404, statusMessage: 'Not Found', message: `找不到「${ticker}」` })
  }

  const mapVersion = loadMap().version
  const from = `${range.fromFy}Q${range.fromQ}`
  const to = `${range.toFy}Q${range.toQ}`
  const cacheKey = `${ref.ticker}_${from}_${to}_${mapVersion}.xlsx`

  // R2 命中判斷（單純的檔案存在性檢查，不是快取系統）
  const r2Base = process.env.R2_PUBLIC_BASE_URL
  if (r2Base) {
    const head = await fetch(`${r2Base}/${cacheKey}`, { method: 'HEAD' })
    if (head.ok) return sendRedirect(event, `${r2Base}/${cacheKey}`, 302)
  }

  // 未命中 → 轉呼叫 Cloud Run：資料在此準備好，excel-service 不碰 SEC
  const fin = await getFinancials(ref, range.fromFy, range.toFy)
  const res = await fetch(`${serviceUrl}/generate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cacheKey, financials: fin, from, to }),
  })
  if (!res.ok) {
    throw createError({
      statusCode: 502,
      statusMessage: 'Bad Gateway',
      message: `Excel 生成失敗（${res.status}）`,
    })
  }
  const { url } = (await res.json()) as { url: string }
  return sendRedirect(event, url, 302)
})
