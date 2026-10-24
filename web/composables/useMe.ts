/**
 * 股譜的登入狀態與預覽名單（`/api/me`）。整個瀏覽過程只問一次；
 * 伺服器端渲染時用 useRequestFetch 把使用者的 cookie 帶過去。
 */
export type Me = { loggedIn: boolean; preview: string[] }

export async function useMe(): Promise<Me> {
  const me = useState<Me | null>('me', () => null)
  if (!me.value) {
    try {
      me.value = await useRequestFetch()<Me>('/api/me')
    } catch {
      me.value = { loggedIn: false, preview: [] }
    }
  }
  return me.value
}

/** 股譜的登入頁，登入完回到本站的這一頁（path 不含 /financials 前綴） */
export const loginHref = (path: string) => `/login?next=${encodeURIComponent(withBase(path))}`
