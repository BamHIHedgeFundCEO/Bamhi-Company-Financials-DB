import { readFileSync, existsSync } from 'node:fs'
import { resolve } from 'node:path'

/**
 * SSG：build 時預先生成熱門/前 500 大 ticker 的 /stock/{ticker} 靜態頁。
 * 清單由 scripts/generate-static.ts 產出（.static-tickers.json）；
 * 沒跑過 script 時退回內建熱門清單，dev 不受影響。
 * 每週 GitHub Actions 重新 generate 一次；使用者操作仍以 client-side API 取最新狀態。
 */
const fallbackTickers = ['NVDA', 'AAPL', 'TSLA', 'MSFT', 'AMZN', 'GOOGL', 'META', 'AMD', 'TSM', 'PLTR']

function staticTickers(): string[] {
  const p = resolve(__dirname, '.static-tickers.json')
  if (existsSync(p)) {
    try {
      const list = JSON.parse(readFileSync(p, 'utf-8')) as string[]
      if (Array.isArray(list) && list.length) return list
    } catch { /* 損壞時退回內建清單 */ }
  }
  return fallbackTickers
}

export default defineNuxtConfig({
  compatibilityDate: '2026-01-01',
  css: ['~/assets/css/main.css'],
  app: {
    head: {
      htmlAttrs: { lang: 'zh-Hant' },
      meta: [
        { name: 'viewport', content: 'width=device-width, initial-scale=1' },
        { charset: 'utf-8' },
      ],
      link: [
        { rel: 'preconnect', href: 'https://fonts.googleapis.com' },
        { rel: 'preconnect', href: 'https://fonts.gstatic.com', crossorigin: '' },
        {
          rel: 'stylesheet',
          href: 'https://fonts.googleapis.com/css2?family=IBM+Plex+Sans:wght@400;500;600&family=JetBrains+Mono:wght@400;500;700&family=Noto+Sans+TC:wght@400;500;700&display=swap',
        },
      ],
    },
  },
  nitro: {
    // config/ 在 repo 根層（web/ 之外）；打包成 server asset 讓 Vercel serverless 也讀得到
    serverAssets: [{ baseName: 'config', dir: '../../config' }],
    prerender: {
      routes: ['/', '/sitemap.xml', ...staticTickers().map((t) => `/stock/${t}`)],
    },
  },
})
