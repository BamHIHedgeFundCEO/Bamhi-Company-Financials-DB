/**
 * 股價來源：Yahoo Finance chart API（免 key、免 library，serverless 上比 yfinance 穩）。
 * 用 adjclose（已還原分割與股利）→ 與本站 split-adjusted 股數同基準，市值計算一致。
 * SEC 不提供股價，估值倍數（PE/PS/PB/EV…）唯一的外部相依就在這裡。
 */

export interface PriceSeries {
  currency: string
  current: number | null
  /** 由舊到新的 [YYYY-MM-DD, adjClose]，月線 */
  monthly: [string, number][]
}

const cache = new Map<string, { at: number; data: PriceSeries }>()
const TTL = 6 * 3600 * 1000

export async function getPrices(ticker: string): Promise<PriceSeries | null> {
  const key = ticker.toUpperCase()
  const hit = cache.get(key)
  if (hit && Date.now() - hit.at < TTL) return hit.data

  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(key)}?range=10y&interval=1mo`
  try {
    const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } })
    if (!res.ok) return null
    const j = (await res.json()) as any
    const r = j?.chart?.result?.[0]
    if (!r) return null
    const ts: number[] = r.timestamp ?? []
    const adj: (number | null)[] = r.indicators?.adjclose?.[0]?.adjclose ?? r.indicators?.quote?.[0]?.close ?? []
    const monthly: [string, number][] = []
    for (let i = 0; i < ts.length; i++) {
      const v = adj[i]
      if (v != null) monthly.push([new Date(ts[i] * 1000).toISOString().slice(0, 10), v])
    }
    const data: PriceSeries = {
      currency: r.meta?.currency ?? 'USD',
      current: r.meta?.regularMarketPrice ?? (monthly.at(-1)?.[1] ?? null),
      monthly,
    }
    cache.set(key, { at: Date.now(), data })
    return data
  } catch {
    return null
  }
}

/** 取 <= 目標日期的最近月線收盤（該季末當時的股價）。 */
export function priceAt(series: PriceSeries, date: string): number | null {
  let best: number | null = null
  for (const [d, v] of series.monthly) {
    if (d <= date) best = v
    else break
  }
  return best
}
