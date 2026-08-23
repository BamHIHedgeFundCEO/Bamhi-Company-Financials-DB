import { defineEventHandler, setHeader } from 'h3'
import { readFileSync, existsSync } from 'node:fs'
import { resolve } from 'node:path'

const SITE = process.env.SITE_URL || 'https://bamhi-company-financials.vercel.app'

export default defineEventHandler((event) => {
  let tickers: string[] = ['NVDA', 'AAPL', 'TSLA', 'MSFT', 'AMZN', 'GOOGL', 'META', 'AMD', 'TSM', 'PLTR']
  const p = resolve(process.cwd(), '.static-tickers.json')
  if (existsSync(p)) {
    try {
      tickers = JSON.parse(readFileSync(p, 'utf-8'))
    } catch { /* fallback */ }
  }
  // 個股頁的五個分頁都各自有標題與內容，分開收錄
  const SUBPAGES = ['', '/profile', '/financials', '/funds', '/insider']
  const urls = ['/', ...tickers.flatMap((t) => SUBPAGES.map((s) => `/stock/${t}${s}`))]
    .map((u) => `  <url><loc>${SITE}${u}</loc></url>`)
    .join('\n')
  setHeader(event, 'Content-Type', 'application/xml')
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>`
})
