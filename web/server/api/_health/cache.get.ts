import { defineEventHandler } from 'h3'
import { cacheGet, cacheSet } from '../../utils/blobCache'

/**
 * GET /api/_health/cache — 持久快取健檢。
 *
 * 存在理由：Blob 走 OIDC 認證，只在 preview/production 生效，本機開發環境
 * 打不到（會回 "OIDC is enabled for this project, but not for the development
 * environment"）。所以快取到底有沒有活，只能在部署後從這裡確認。
 *
 * 只寫入固定的小 key、不吃任何使用者輸入、只回傳布林狀態，不外洩憑證或資料。
 */
export default defineEventHandler(async () => {
  const key = '_health/probe.json'
  const payload = { probe: true, at: new Date().toISOString() }

  let wrote = false
  let readBack: unknown = null
  let error: string | null = null

  try {
    await cacheSet(key, payload)
    wrote = true
    readBack = await cacheGet<typeof payload>(key)
  } catch (err) {
    error = (err as Error).message
  }

  const roundTrip =
    readBack !== null && (readBack as { probe?: boolean }).probe === true

  return {
    // 認證方式：連結 Blob store 走 OIDC（BLOB_STORE_ID）；靜態 token 為備援
    auth: process.env.BLOB_READ_WRITE_TOKEN
      ? 'static-token'
      : process.env.BLOB_STORE_ID
        ? 'oidc'
        : 'none',
    enabled: Boolean(process.env.BLOB_READ_WRITE_TOKEN || process.env.BLOB_STORE_ID),
    wrote,
    roundTrip,
    error,
  }
})
