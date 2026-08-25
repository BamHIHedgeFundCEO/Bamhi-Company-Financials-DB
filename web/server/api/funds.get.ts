import { defineEventHandler, getQuery, createError, setHeader } from 'h3'
import { resolveCompany } from '../utils/cik'

/**
 * GET /api/funds?ticker=NVDA
 *
 * 13F 機構持股。**執行期零 SEC 請求** —— 全部讀離線批次的產物
 * `config/f13/`（`tools/f13.py` 產生，一季重跑一次），與 class_shares 同模式。
 *
 * 為什麼不能即時算：13F 是「以基金為索引」的，要回答「誰持有 NVDA」得把
 * 全市場 6,483 份持股表（單季 INFOTABLE 380 萬列）反轉建索引。
 */

export interface FundHolder {
  cik: string
  name: string
  shares: number
  value: number
  delta?: number
}
export interface FundsResult {
  available: boolean
  reason?: string
  ticker: string
  company?: string
  cusip?: string
  period?: string
  periodPrev?: string
  generated?: string
  filers?: number
  holders?: number
  holdersPrev?: number
  totalShares?: number
  totalValue?: number
  increased?: number
  decreased?: number
  opened?: number
  closed?: number
  unchanged?: number
  reorgs?: number
  topOpened?: FundHolder[]
  topClosed?: FundHolder[]
  topIncreased?: FundHolder[]
  topDecreased?: FundHolder[]
  topReorgs?: { into: FundHolder; outof: FundHolder }[]
}

/** Windows 的保留裝置名稱建不出 `CON.json`（母體裡真的有一檔叫 CON），
 *  `tools/f13.py` 寫檔時加了底線，這裡要用同一套規則才找得到 */
const RESERVED = new Set([
  'CON', 'PRN', 'AUX', 'NUL', 'CLOCK$',
  ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`),
])
const safeName = (t: string) => (RESERVED.has(t) ? `${t}_` : t)

let indexCache: Record<string, unknown> | null = null

export default defineEventHandler(async (event): Promise<FundsResult> => {
  // 13F 一季才換一次資料 —— CDN 放一天、過期後一週內先給舊的再背景更新
  setHeader(event, 'Cache-Control', 'public, s-maxage=86400, stale-while-revalidate=604800')
  const q = getQuery(event)
  const t = String(q.ticker || '').trim().toUpperCase()
  if (!t) throw createError({ statusCode: 400, message: '缺少 ticker' })
  if (!/^[A-Z]{1,6}(\.[A-Z])?$/.test(t)) {
    throw createError({ statusCode: 400, message: '代號格式不正確' })
  }

  const store = useStorage('assets:config')
  if (!indexCache) {
    indexCache = (await store.getItem('f13/_index.json')) as Record<string, unknown> | null
  }
  const meta = indexCache ?? {}

  const doc = (await store.getItem(`f13/${safeName(t)}.json`)) as Record<string, unknown> | null
  if (!doc) {
    // 公司存在但沒有 13F 資料 vs 代號根本不存在，是兩件事，訊息要分開
    const ref = await resolveCompany(t).catch(() => null)
    return {
      available: false,
      ticker: t,
      company: ref?.name,
      generated: meta.generated as string | undefined,
      period: meta.period as string | undefined,
      reason: ref
        ? `${t} 在本季的 13F 索引裡沒有紀錄。可能是：這一檔沒有任何機構申報持有、`
          + '或它的 CUSIP 不在對照表裡（對照表來自 SEC 交割失敗檔，只涵蓋近期有交割失敗的證券）。'
        : `找不到代號「${t}」。`,
    }
  }

  return {
    available: true,
    generated: meta.generated as string | undefined,
    filers: meta.filers as number | undefined,
    ...(doc as object),
  } as FundsResult
})
