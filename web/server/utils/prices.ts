/**
 * 股價來源：Yahoo Finance chart API（免 key、免 library，serverless 上比 yfinance 穩）。
 * 用 adjclose（已還原分割與股利）→ 與本站 split-adjusted 股數同基準，市值計算一致。
 * SEC 不提供股價，估值倍數（PE/PS/PB/EV…）唯一的外部相依就在這裡。
 */

export interface PriceSeries {
  currency: string
  current: number | null
  /** 由舊到新的 [YYYY-MM-DD, adjClose]，日線（貼近季末當日收盤） */
  daily: [string, number][]
}

const cache = new Map<string, { at: number; data: PriceSeries }>()
const TTL = 6 * 3600 * 1000

export async function getPrices(ticker: string): Promise<PriceSeries | null> {
  const key = ticker.toUpperCase()
  const hit = cache.get(key)
  if (hit && Date.now() - hit.at < TTL) return hit.data

  // 日線、近 10 年（涵蓋 40 季上限）；adjclose 已還原分割/股利
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(key)}?range=10y&interval=1d`
  try {
    const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } })
    if (!res.ok) return null
    const j = (await res.json()) as any
    const r = j?.chart?.result?.[0]
    if (!r) return null
    const ts: number[] = r.timestamp ?? []
    const adj: (number | null)[] = r.indicators?.adjclose?.[0]?.adjclose ?? r.indicators?.quote?.[0]?.close ?? []
    const daily: [string, number][] = []
    for (let i = 0; i < ts.length; i++) {
      const v = adj[i]
      if (v != null) daily.push([new Date(ts[i] * 1000).toISOString().slice(0, 10), v])
    }
    const data: PriceSeries = {
      currency: r.meta?.currency ?? 'USD',
      current: r.meta?.regularMarketPrice ?? (daily.at(-1)?.[1] ?? null),
      daily,
    }
    cache.set(key, { at: Date.now(), data })
    return data
  } catch {
    return null
  }
}

/** 取 <= 目標日期的最近交易日收盤（季末當日或前一交易日）。二分搜尋。 */
export function priceAt(series: PriceSeries, date: string): number | null {
  const a = series.daily
  let lo = 0
  let hi = a.length - 1
  let best: number | null = null
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (a[mid][0] <= date) {
      best = a[mid][1]
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  return best
}
