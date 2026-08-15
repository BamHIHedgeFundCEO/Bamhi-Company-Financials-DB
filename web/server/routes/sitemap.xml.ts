import { defineEventHandler, setHeader } from 'h3'
import { readFileSync, existsSync } from 'node:fs'
import { resolve } from 'node:path'

const SITE = process.env.SITE_URL || 'https://bamhi-financials.example.com'

export default defineEventHandler((event) => {
  let tickers: string[] = ['NVDA', 'AAPL', 'TSLA', 'MSFT', 'AMZN', 'GOOGL', 'META', 'AMD', 'TSM', 'PLTR']
  const p = resolve(process.cwd(), '.static-tickers.json')
  if (existsSync(p)) {
    try {
      tickers = JSON.parse(readFileSync(p, 'utf-8'))
    } catch { /* fallback */ }
  }
  const urls = ['/', ...tickers.map((t) => `/stock/${t}`)]
    .map((u) => `  <url><loc>${SITE}${u}</loc></url>`)
    .join('\n')
  setHeader(event, 'Content-Type', 'application/xml')
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>`
})
