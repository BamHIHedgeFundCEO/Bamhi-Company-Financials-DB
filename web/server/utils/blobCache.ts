/**
 * 持久快取層（Vercel Blob，private store）。
 *
 * 為什麼需要：`secFetch.ts` 原本只有 `new Map()` 行程內快取，Vercel 函式實例
 * 回收就沒了、跨實例也不共用 —— 等於每次查詢都重打 SEC。抓 companyfacts
 * （1 次請求）還好，但分部資料要逐份申報抓 XBRL instance（單檔 0.7–13MB），
 * 沒有持久快取會每次重抓數十 MB。
 *
 * 設計原則：
 * 1. **只存解析後的結果，不存原始檔**。原始 instance 動輒數 MB，Hobby 方案
 *    1GB 額度大概只夠 15 家公司；存解析結果（每季幾 KB）可裝幾千家。
 * 2. **已申報的財報不可變** → immutable 類的 key 永久有效，不設 TTL。
 * 3. **沒有 token 就靜默降級**（跟 excel-service/r2.py 同樣的容錯風格）：
 *    未連結 Blob store 時整條路徑變成 no-op，功能不受影響，只是沒有快取。
 */

let warned = false

/**
 * 兩種認證方式都要支援：
 * - **OIDC**（Vercel 連結 Blob store 的預設）：注入 `BLOB_STORE_ID`，執行期由平台
 *   提供 `VERCEL_OIDC_TOKEN`。連結 store 後 **不會** 產生 `BLOB_READ_WRITE_TOKEN`。
 * - **靜態 token**：手動設定 `BLOB_READ_WRITE_TOKEN`（本機測試或非 Vercel 環境）。
 */
function enabled(): boolean {
  const ok = Boolean(process.env.BLOB_READ_WRITE_TOKEN || process.env.BLOB_STORE_ID)
  if (!ok && !warned) {
    warned = true
    console.warn('[blobCache] 未設定 BLOB_STORE_ID / BLOB_READ_WRITE_TOKEN —— 持久快取停用（僅記憶體快取）')
  }
  return ok
}

/** key 需能安全當作 blob pathname；保留斜線做階層，其餘不安全字元換底線。 */
function safeKey(key: string): string {
  return key.replace(/[^a-zA-Z0-9._/-]/g, '_')
}

/**
 * 讀取快取。找不到 / 未啟用 / 任何錯誤都回 null —— 快取層絕不讓主流程失敗。
 */
export async function cacheGet<T>(key: string): Promise<T | null> {
  if (!enabled()) return null
  try {
    const { get } = await import('@vercel/blob')
    const res = await get(safeKey(key), { access: 'private' })
    if (!res || res.statusCode !== 200) return null
    const text = await new Response(res.stream).text()
    return JSON.parse(text) as T
  } catch (err) {
    console.warn(`[blobCache] 讀取失敗 ${key}:`, (err as Error).message)
    return null
  }
}

/**
 * 寫入快取。失敗只記錄不拋錯 —— 寫不進去頂多下次再抓一遍。
 */
export async function cacheSet(key: string, value: unknown): Promise<void> {
  if (!enabled()) return
  try {
    const { put } = await import('@vercel/blob')
    await put(safeKey(key), JSON.stringify(value), {
      access: 'private',
      contentType: 'application/json',
      // 同一 key 就是同一份不可變資料，覆寫即可，不要加隨機後綴
      addRandomSuffix: false,
      allowOverwrite: true,
    })
  } catch (err) {
    console.warn(`[blobCache] 寫入失敗 ${key}:`, (err as Error).message)
  }
}

/**
 * 讀不到就跑 `compute()` 並寫回。
 * `immutable=false` 的資料（如 submissions 索引）呼叫端自行決定是否用此函式。
 */
export async function cached<T>(key: string, compute: () => Promise<T>): Promise<T> {
  const hit = await cacheGet<T>(key)
  if (hit !== null) return hit
  const fresh = await compute()
  await cacheSet(key, fresh)
  return fresh
}
