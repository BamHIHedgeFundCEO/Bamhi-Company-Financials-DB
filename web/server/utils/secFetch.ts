import { rateLimiter } from './rateLimiter'

/**
 * 所有對 sec.gov / data.sec.gov 的請求唯一入口。
 * - 必帶 User-Agent（SEC 合規，缺了會 403 並可能封 IP）
 * - rateLimiter.acquire() 控制最小間隔
 * - 429 指數退避（最多 3 次重試）
 * - 行程內回應快取：已提交的財報不可變，company_tickers / submissions 給 24h TTL
 */

const RETRY_MAX = 3
const cache = new Map<string, { at: number; ttl: number; data: unknown }>()

function userAgent(): string {
  // 預設為營運者聯絡方式（SEC 合規要求）；部署環境可用 SEC_USER_AGENT 覆蓋
  return process.env.SEC_USER_AGENT || 'BamHI frank940702@gmail.com'
}

/** 共用的取回流程：限速 → 429 指數退避 → 非 2xx 拋錯。 */
async function secFetchRaw(url: string): Promise<Response> {
  for (let attempt = 0; ; attempt++) {
    await rateLimiter.acquire()
    const res = await fetch(url, {
      headers: { 'User-Agent': userAgent(), 'Accept-Encoding': 'gzip, deflate' },
    })
    if (res.status === 429 && attempt < RETRY_MAX) {
      await new Promise((r) => setTimeout(r, 500 * 2 ** attempt))
      continue
    }
    if (!res.ok) throw new Error(`SEC ${res.status}: ${url}`)
    return res
  }
}

export async function secFetchJson<T>(url: string, ttlMs = 24 * 3600 * 1000): Promise<T> {
  const hit = cache.get(url)
  if (hit && Date.now() - hit.at < hit.ttl) return hit.data as T

  const data = (await (await secFetchRaw(url)).json()) as T
  cache.set(url, { at: Date.now(), ttl: ttlMs, data })
  return data
}

/**
 * 抓純文字（XBRL instance XML 用）。
 *
 * 刻意「不」進 `cache`：instance 單檔 0.7–13MB，塞進行程內 Map 會把函式記憶體吃爆。
 * 呼叫端應該解析完之後，用 `blobCache.cached()` 只快取「解析後的小結果」。
 */
export async function secFetchText(url: string): Promise<string> {
  return (await secFetchRaw(url)).text()
}
