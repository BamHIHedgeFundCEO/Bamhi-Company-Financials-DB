import { defineEventHandler, setHeader } from 'h3'

/**
 * GET /api/f13leaders
 *
 * 首頁的「本季機構在做什麼」。與 /api/funds 同一批離線產物
 * （`tools/f13.py` → `config/f13/_leaders.json`），執行期零 SEC 請求。
 *
 * 榜單本身在批次就排好了，這裡只負責把榜上代號換成明細列 —— 排序邏輯屬於
 * 資料層（要跟著母體、分割正規化一起改），不該散在前端。
 */

export interface LeaderRow {
  ticker: string
  company: string
  holders: number
  holdersPrev: number
  opened: number
  closed: number
  increased: number
  decreased: number
  totalValue: number
  /** 機構合計持股的季增率（已做分割正規化） */
  netShares: number
}
export interface LeadersResult {
  available: boolean
  period?: string
  periodPrev?: string
  generated?: string
  /** 這一季偵測到分割並已正規化的代號 → 倍數 */
  splits?: Record<string, number>
  boards?: Record<string, LeaderRow[]>
}

let cache: LeadersResult | null = null

export default defineEventHandler(async (event): Promise<LeadersResult> => {
  setHeader(event, 'Cache-Control', 'public, s-maxage=86400, stale-while-revalidate=604800')
  if (cache) return cache

  const doc = (await useStorage('assets:config').getItem('f13/_leaders.json')) as {
    period: string
    periodPrev: string
    generated: string
    splits: Record<string, number>
    rows: Record<string, [string, number, number, number, number, number, number, number, number]>
    boards: Record<string, string[]>
  } | null
  if (!doc) return { available: false }

  const row = (t: string): LeaderRow | null => {
    const r = doc.rows[t]
    if (!r) return null
    return {
      ticker: t, company: r[0], holders: r[1], holdersPrev: r[2], opened: r[3],
      closed: r[4], increased: r[5], decreased: r[6], totalValue: r[7], netShares: r[8],
    }
  }
  const boards: Record<string, LeaderRow[]> = {}
  for (const [k, list] of Object.entries(doc.boards)) {
    boards[k] = list.map(row).filter(Boolean) as LeaderRow[]
  }
  cache = {
    available: true,
    period: doc.period,
    periodPrev: doc.periodPrev,
    generated: doc.generated,
    splits: doc.splits,
    boards,
  }
  return cache
})
