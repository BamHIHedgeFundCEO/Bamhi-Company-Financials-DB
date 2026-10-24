/**
 * 站內網址加上站台前綴（本站掛在股譜的 `/financials/` 底下）。
 * `$fetch` 會自己加，但 `<a href>`、`location.href`、`<img src>` 不會 —— 漏加的話
 * 下載連結會打到股譜根目錄的 /api，回 404 而且看起來像我們的 API 壞了。
 */
export function withBase(path: string): string {
  const base = useRuntimeConfig().app.baseURL.replace(/\/$/, '')
  return `${base}${path}`
}
