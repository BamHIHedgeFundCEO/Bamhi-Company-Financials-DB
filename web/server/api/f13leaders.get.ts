import { defineEventHandler, getQuery, setHeader } from 'h3'

/**
 * GET /api/f13leaders?size=all|big|mid|small
 *
 * 首頁的「本季機構在做什麼」。資料來自離線批次
 * （`tools/f13.py` → `config/f13/_leaders.json`），執行期零 SEC 請求。
 *
 * 排序在這裡算而不是在批次算：規模級距是使用者可以切換的篩選器，
 * 先算好的話 12 張榜 × 4 個級距要存 48 份，而且改門檻就得重跑整批 13F。
 * 原始列只有 7,034 筆、0.8 MB，讀一次留在記憶體，排序是毫秒級的事。
 */

type Raw = [string, number, number, number, number, number, number, number, number]

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
  /** 機構合計持股的季增率（分割與股票股利已正規化） */
  netShares: number
  /** 這張榜的排序依據 —— 一定要送到前端，否則畫面上看起來像沒排序 */
  metric: number
}
export interface Board {
  key: string
  /** 排序依據的欄名與格式，前端照這個畫那一欄 */
  metricLabel: string
  metricKind: 'count' | 'pct'
  rows: LeaderRow[]
}
export interface LeadersResult {
  available: boolean
  period?: string
  periodPrev?: string
  generated?: string
  size?: string
  /** 每個級距各有幾檔，讓使用者知道自己在看多大的池子 */
  counts?: Record<string, number>
  splits?: Record<string, number>
  boards?: Record<string, Board>
}

/** 規模用**機構申報持股市值**分段，不是市值 —— 在外流通股數那筆事實不可靠
 *  （companyfacts 裡 Visa 最新的一筆是 2010 年的），而 13F 自己就帶市值 */
const SIZE: Record<string, (v: number) => boolean> = {
  all: () => true,
  big: (v) => v >= 1e10,
  mid: (v) => v >= 1e9 && v < 1e10,
  small: (v) => v < 1e9,
}

let cache: { doc: any; rows: LeaderRow[] } | null = null

function load(doc: any): LeaderRow[] {
  return Object.entries(doc.rows as Record<string, Raw>).map(([t, r]) => ({
    ticker: t, company: r[0], holders: r[1], holdersPrev: r[2], opened: r[3],
    closed: r[4], increased: r[5], decreased: r[6], totalValue: r[7], netShares: r[8],
    metric: 0,
  }))
}

export default defineEventHandler(async (event): Promise<LeadersResult> => {
  setHeader(event, 'Cache-Control', 'public, s-maxage=86400, stale-while-revalidate=604800')

  if (!cache) {
    const doc = await useStorage('assets:config').getItem('f13/_leaders.json')
    if (!doc) return { available: false }
    cache = { doc, rows: load(doc) }
  }
  const { doc, rows } = cache

  const q = getQuery(event)
  const size = SIZE[String(q.size || 'all')] ? String(q.size || 'all') : 'all'
  const fits = SIZE[size]

  // 上季持有不到 20 家的一律排除：3 家變 6 家就是 +100%，而且分拆出來的新公司
  // （HONA 上季 0 家、本季 2,055 家）會霸佔建倉榜。那一類另立一張 fresh
  const pool = rows.filter((r) => r.holdersPrev >= 20 && fits(r.totalValue))
  const fresh = rows.filter((r) => r.holdersPrev < 20 && r.holders >= 50 && fits(r.totalValue))
  // 清倉比例榜要擋掉破產殼股（代號帶 Q 的那些）—— 確實被清光，但那是下市
  const alive = pool.filter((r) => r.totalValue >= 5e7)
  // 合計持股是股數的季增率，微型股會爆掉（PMI 上季 39.6 萬股 → 本季 760 萬股，
  // 持有 41 家其中 21 家建倉 19 家清倉）。要有基底才看得出訊號
  const deep = pool.filter((r) => r.holdersPrev >= 100 && r.totalValue >= 3e8)

  const mk = (
    key: string, metricLabel: string, metricKind: 'count' | 'pct',
    src: LeaderRow[], f: (r: LeaderRow) => number, n = 20,
  ): [string, Board] => {
    const ranked = src
      .map((r) => ({ ...r, metric: f(r) }))
      .sort((a, b) => b.metric - a.metric)
      .slice(0, n)
    return [key, { key, metricLabel, metricKind, rows: ranked }]
  }

  const boards = Object.fromEntries([
    mk('openedAbs', '建倉家數', 'count', pool, (r) => r.opened),
    mk('openedRel', '建倉÷上季持有', 'pct', pool, (r) => r.opened / r.holdersPrev),
    mk('closedAbs', '清倉家數', 'count', pool, (r) => r.closed),
    mk('closedRel', '清倉÷上季持有', 'pct', alive, (r) => r.closed / r.holdersPrev),
    mk('netHolders', '持有家數淨增', 'count', pool, (r) => r.holders - r.holdersPrev),
    mk('netHoldersDown', '持有家數淨減', 'count', pool, (r) => r.holdersPrev - r.holders),
    mk('netSharesUp', '合計持股季增率', 'pct', deep, (r) => r.netShares),
    mk('netSharesDown', '合計持股季減率', 'pct', deep, (r) => -r.netShares),
    mk('fresh', '本季持有家數', 'count', fresh, (r) => r.holders),
  ])

  return {
    available: true,
    period: doc.period,
    periodPrev: doc.periodPrev,
    generated: doc.generated,
    size,
    counts: Object.fromEntries(
      Object.entries(SIZE).map(([k, f]) => [
        k, rows.filter((r) => r.holdersPrev >= 20 && f(r.totalValue)).length,
      ]),
    ),
    splits: doc.splits,
    boards,
  }
})
