/**
 * 個股頁的登入門檻（API 那一層才是真的擋，見 server/middleware/gate.ts；這裡只是
 * 讓沒登入的人直接看到登入頁，而不是一頁空白的錯誤）。
 *
 * 從股譜其他頁面點進來的整頁載入，股譜的 middleware 會先擋；
 * 這裡擋的是站內切換（預覽頁上的「同產業」連結、分頁之間）。
 */
export default defineNuxtRouteMiddleware(async (to) => {
  const ticker = String(to.params.ticker ?? '').toUpperCase()
  if (!ticker) return
  const me = await useMe()
  if (me.loggedIn || me.preview.includes(ticker)) return
  // 登入頁在股譜（站外），相對路徑讓瀏覽器以目前網域解析
  return navigateTo(loginHref(to.fullPath), { external: true })
})
