/**
 * build 前跑：抓 SEC company_tickers.json，取前 500 大（該檔已依市值排序），
 * 寫入 web/.static-tickers.json 供 nuxt.config prerender 與 sitemap 使用。
 *
 *   npx tsx scripts/generate-static.ts
 *
 * GitHub Actions 每週跑一次後 nuxt generate 重新產出靜態頁。
 */
import { writeFileSync } from 'node:fs'
import { resolve } from 'node:path'

const UA = process.env.SEC_USER_AGENT
if (!UA) {
  console.error('SEC_USER_AGENT 未設定')
  process.exit(1)
}

const res = await fetch('https://www.sec.gov/files/company_tickers.json', {
  headers: { 'User-Agent': UA },
})
if (!res.ok) {
  console.error(`SEC ${res.status}`)
  process.exit(1)
}
const raw = (await res.json()) as Record<string, { ticker: string }>
const tickers = Object.values(raw)
  .slice(0, 500)
  .map((r) => r.ticker.toUpperCase())

const out = resolve(import.meta.dirname, '..', '.static-tickers.json')
writeFileSync(out, JSON.stringify(tickers, null, 2))
console.log(`寫入 ${tickers.length} 檔 → ${out}`)
