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

export async function secFetchJson<T>(url: string, ttlMs = 24 * 3600 * 1000): Promise<T> {
  const hit = cache.get(url)
  if (hit && Date.now() - hit.at < hit.ttl) return hit.data as T

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
    const data = (await res.json()) as T
    cache.set(url, { at: Date.now(), ttl: ttlMs, data })
    return data
  }
}
