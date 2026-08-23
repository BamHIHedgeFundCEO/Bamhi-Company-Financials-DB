import { defineEventHandler, getQuery, createError } from 'h3'
import { resolveCompany } from '../utils/cik'
import { getInsider } from '../utils/insider'

/**
 * GET /api/insider?ticker=AAPL&limit=60
 * → Form 3/4/5 交易明細 + 高管名冊（公司簡介分頁的高管來源也是這支）
 */
export default defineEventHandler(async (event) => {
  const q = getQuery(event)
  const t = String(q.ticker || '').trim().toUpperCase()
  if (!t) throw createError({ statusCode: 400, message: '缺少 ticker' })

  const ref = await resolveCompany(t)
  if (!ref) {
    throw createError({
      statusCode: 404,
      message: `找不到「${t}」。請確認 ticker 拼寫；已下市公司與多數 ETF 不在 SEC 申報名單內。`,
    })
  }
  const limit = Math.min(120, Math.max(10, Number(q.limit) || 60))
  return await getInsider(ref, limit)
})
