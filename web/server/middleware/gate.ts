import { defineEventHandler, getQuery, getRequestURL, createError } from 'h3'
import { allPreview, getUser, logToolEvent } from '../utils/auth'

/**
 * 登入門檻（2026-10 發起人決定）：預覽名單裡的代號誰都能看，其他代號要登入股譜帳號（免費）。
 *
 * 擋在 API，不是只擋頁面 —— 頁面擋得再好，有人直接打 /api 就繞過去了。
 * 帶 ticker 的端點都在這裡判斷；不帶 ticker 的（13F 全市場榜、數據回報、健康檢查）不擋。
 *
 * 401 的 message 固定是 `login_required`，前端靠它顯示註冊牆。
 *
 * **要登入才看得到的回應不能進 CDN 快取**：各端點自己設的是 `public, s-maxage`，
 * 照舊的話第一個登入的人查完 AMD，CDN 就把那份回應發給下一個沒登入的人。
 * 標記 `event.context.gated`，由 `plugins/gate-cache.ts` 在送出前改成 private。
 */

// 有記錄價值的動作：查詢（文件清單）與下載。分頁的資料請求會被預先載入灌水，不記
const LOGGED: Record<string, string> = {
  filings: 'search',
  'financials/excel': 'excel',
  'financials/csv': 'csv',
}

export default defineEventHandler(async (event) => {
  const path = getRequestURL(event).pathname
  const m = path.match(/\/api\/(.+?)\/?$/)
  if (!m) return
  const raw = getQuery(event).ticker
  if (raw === undefined) return
  const tickers = String(raw)
    .split(',')
    .map((t) => t.trim().toUpperCase())
    .filter(Boolean)
  if (allPreview(tickers)) return

  event.context.gated = true
  const user = await getUser(event)
  if (!user) throw createError({ statusCode: 401, statusMessage: 'Unauthorized', message: 'login_required' })

  const action = LOGGED[m[1]]
  if (action) await logToolEvent(user, tickers.join(','), action)
})
