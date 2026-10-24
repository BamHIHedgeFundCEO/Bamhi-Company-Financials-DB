import { defineEventHandler, setHeader } from 'h3'
import preview from '../../../config/preview_tickers.json'

// 本站併入股譜：對外網址是 www.tickifu.com/financials（股譜的 sitemap 會引用這一份）
const SITE = process.env.SITE_URL || 'https://www.tickifu.com/financials'

export default defineEventHandler((event) => {
  // 只收錄不用登入就看得到的標的；其他標的對搜尋引擎只是一頁登入頁
  const tickers = preview.tickers.filter((t) => t !== 'GOOG')
  // 個股頁的六個分頁都各自有標題與內容，分開收錄
  const SUBPAGES = ['', '/profile', '/financials', '/signals', '/funds', '/insider']
  const urls = ['/', ...tickers.flatMap((t) => SUBPAGES.map((s) => `/stock/${t}${s}`))]
    .map((u) => `  <url><loc>${SITE}${u}</loc></url>`)
    .join('\n')
  setHeader(event, 'Content-Type', 'application/xml')
  return `<?xml version="1.0" encoding="UTF-8"?>\n<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n${urls}\n</urlset>`
})
