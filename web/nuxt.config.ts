import preview from '../config/preview_tickers.json'

/**
 * 本站併入股譜（2026-10）：網址是 www.tickifu.com/financials/…，由股譜的 Next.js 轉過來，
 * 所以整個站掛在 `/financials/` 底下（頁面、API、靜態檔都是）。
 *
 * SSG：build 時預先產生首頁與預覽標的的個股頁。其他標的要登入才看得到，
 * 預先產生也只會產生一頁註冊牆，不再為 500 檔產生靜態頁。
 */
const staticTickers = (): string[] => preview.tickers

export default defineNuxtConfig({
  compatibilityDate: '2026-01-01',
  css: ['~/assets/css/main.css'],
  app: {
    baseURL: '/financials/',
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
      // `nuxt generate` 會開 crawlLinks，於是爬蟲跟著財報頁上的 Excel 下載連結
      // 去打 /api/financials/excel?... —— 那支要 excel-service（Python）在跑，
      // build 環境沒有它，每一檔都回 503 而且 prerender 把它當致命錯誤 → 整個
      // generate 失敗（實測 1,002 筆 503）。API 路由本來就不該被預先產生。
      //
      // ⚠ ignore 的字串是**前綴比對，不是 glob**：寫 '/api/**' 永遠比對不到
      // （那會被當成字面上以 "/api/**" 開頭），錯誤訊息和沒設一模一樣。
      ignore: ['/api/'],
    },
  },
})
