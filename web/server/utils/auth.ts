import { getCookie, getHeader, parseCookies, type H3Event } from 'h3'
import { createRemoteJWKSet, jwtVerify } from 'jose'
import preview from '../../../config/preview_tickers.json'

/**
 * 股譜帳號的登入狀態（2026-10 起本站併入股譜，網址 www.tickifu.com/financials）。
 *
 * 股譜用 Supabase 登入，session 存在同網域的 cookie（`sb-<專案>-auth-token`）。
 * 這裡**不需要任何密鑰**：Supabase 用非對稱金鑰（ES256）簽發 access token，
 * 公鑰在 JWKS 端點公開，本機驗簽即可。驗不過一律當作沒登入。
 *
 * 只信 access token 本身（簽章、iss、aud、到期），不信 cookie 裡其他欄位
 * —— cookie 是瀏覽器送來的，任何人都能改。
 */

const SUPABASE_URL = process.env.SUPABASE_URL || 'https://wnajrpckqqgpszyefbsw.supabase.co'
const ISSUER = `${SUPABASE_URL}/auth/v1`
const JWKS = createRemoteJWKSet(new URL(`${ISSUER}/.well-known/jwks.json`))
const COOKIE = `sb-${new URL(SUPABASE_URL).hostname.split('.')[0]}-auth-token`

export const PREVIEW = new Set<string>(preview.tickers)

/** 每個代號都在預覽名單裡（多檔比較時要全部都是） */
export const allPreview = (tickers: string[]) => tickers.length > 0 && tickers.every((t) => PREVIEW.has(t))

/**
 * 從 cookie 取出 access token。@supabase/ssr 的格式：
 * - 太長時拆成 `name.0`、`name.1`… 依序串接
 * - 值可能是 `base64-` 開頭的 base64url，或直接是 JSON
 */
function tokenFromCookie(event: H3Event): string | null {
  let raw = getCookie(event, COOKIE)
  if (!raw) {
    const all = parseCookies(event)
    const parts: string[] = []
    for (let i = 0; all[`${COOKIE}.${i}`] !== undefined; i++) parts.push(all[`${COOKIE}.${i}`])
    raw = parts.join('') || undefined
  }
  if (!raw) return null
  try {
    const json = raw.startsWith('base64-') ? Buffer.from(raw.slice(7), 'base64url').toString('utf8') : raw
    const session = JSON.parse(json) as { access_token?: unknown }
    return typeof session.access_token === 'string' ? session.access_token : null
  } catch {
    return null
  }
}

export type AuthUser = { id: string; token: string }

/** 目前的股譜使用者；沒登入或 token 無效／過期回 null。同一個請求只驗一次。 */
export async function getUser(event: H3Event): Promise<AuthUser | null> {
  if (event.context.user !== undefined) return event.context.user as AuthUser | null
  const bearer = getHeader(event, 'authorization')?.match(/^Bearer (.+)$/)?.[1]
  const token = bearer ?? tokenFromCookie(event)
  let user: AuthUser | null = null
  if (token) {
    try {
      const { payload } = await jwtVerify(token, JWKS, { issuer: ISSUER, audience: 'authenticated' })
      if (typeof payload.sub === 'string') user = { id: payload.sub, token }
    } catch {
      user = null
    }
  }
  event.context.user = user
  return user
}

/**
 * 使用紀錄寫進股譜的資料庫（`log_tool_event`）。用使用者自己的 token 呼叫，
 * 資料庫函式以 auth.uid() 認人，這裡不需要 service role key。
 * 沒設 SUPABASE_ANON_KEY（公開金鑰）就不記；記錄失敗不影響使用者。
 */
export async function logToolEvent(user: AuthUser, ticker: string, action: string) {
  const anon = process.env.SUPABASE_ANON_KEY
  if (!anon) return
  try {
    await fetch(`${SUPABASE_URL}/rest/v1/rpc/log_tool_event`, {
      method: 'POST',
      headers: { apikey: anon, Authorization: `Bearer ${user.token}`, 'Content-Type': 'application/json' },
      body: JSON.stringify({ p_tool: 'financials', p_ticker: ticker.slice(0, 12), p_action: action }),
      signal: AbortSignal.timeout(1500),
    })
  } catch {
    /* 記錄失敗不擋下載 */
  }
}
