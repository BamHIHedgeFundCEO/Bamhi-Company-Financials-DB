import { defineEventHandler, setHeader } from 'h3'
import { PREVIEW, getUser } from '../utils/auth'

/** GET /api/me —— 前端用：登入了沒、哪些代號不用登入就能看。依 cookie 而變，不可快取 */
export default defineEventHandler(async (event) => {
  setHeader(event, 'Cache-Control', 'private, no-store')
  const user = await getUser(event)
  return { loggedIn: !!user, preview: [...PREVIEW] }
})
