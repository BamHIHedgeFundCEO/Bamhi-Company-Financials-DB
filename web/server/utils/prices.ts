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
  /** 交易所紀錄的分割除權事件（由舊到新）。與 SEC 完全獨立，用來仲裁 computeSplits */
  splits: SplitFact[]
  /**
   * 雅虎「看得到」的起點＝上市日與本次請求視窗的較晚者。
   * 沒有這個日期就分不出「雅虎說沒有」與「雅虎根本沒涵蓋」——
   * 改名或重新上市的公司會被誤當成前者而誤刪真事件。
   */
  coverStart: string | null
}

export interface SplitFact {
  /** 除權日（YYYY-MM-DD）。比 SEC 申報界線早 0–120 天 */
  date: string
  /** 新/舊 股數比（正向>1，反向<1） */
  factor: number
}

const cache = new Map<string, { at: number; data: PriceSeries }>()
const TTL = 6 * 3600 * 1000

export async function getPrices(ticker: string): Promise<PriceSeries | null> {
  const key = ticker.toUpperCase()
  const hit = cache.get(key)
  if (hit && Date.now() - hit.at < TTL) return hit.data

  // 日線、近 10 年（涵蓋 40 季上限）；adjclose 已還原分割/股利
  // `events=split` 在**同一個請求**裡多回除權日與確切比例，零額外外部請求
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(key)}?range=10y&interval=1d&events=split`
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
    const splits: SplitFact[] = []
    for (const ev of Object.values<any>(r.events?.splits ?? {})) {
      const num = Number(ev?.numerator)
      const den = Number(ev?.denominator)
      if (!isCleanRatio(num, den)) continue
      splits.push({ date: epochDay(ev.date), factor: num / den })
    }
    splits.sort((a, b) => (a.date < b.date ? -1 : 1))

    const firstTrade = r.meta?.firstTradeDate != null ? epochDay(r.meta.firstTradeDate) : null
    const windowStart = daily[0]?.[0] ?? null
    const data: PriceSeries = {
      currency: r.meta?.currency ?? 'USD',
      current: r.meta?.regularMarketPrice ?? (daily.at(-1)?.[1] ?? null),
      daily,
      splits,
      coverStart:
        firstTrade && windowStart
          ? firstTrade > windowStart
            ? firstTrade
            : windowStart
          : (firstTrade ?? windowStart),
    }
    cache.set(key, { at: Date.now(), data })
    return data
  } catch {
    return null
  }
}

/**
 * 分拆造成的價格調整也走 splits 事件回來，且比例是零碎的
 * （HON 的五筆全是這種：10000:9947、1011:1000、1032:1000、1061:1000、1907:2000）。
 * 真分割的分子分母都是小整數 —— 這就是把它們分開的判準。
 */
function isCleanRatio(num: number, den: number): boolean {
  if (!num || !den || !isFinite(num) || !isFinite(den)) return false
  if (Math.abs(num - Math.round(num)) > 0.01 || Math.abs(den - Math.round(den)) > 0.01) return false
  const n = Math.round(num)
  const d = Math.round(den)
  return n >= 1 && d >= 1 && n !== d && Math.max(n, d) <= 100
}

/**
 * epoch 秒 → YYYY-MM-DD。**不要換成會踩平台限制的寫法** ——
 * 1970 年前上市的公司 `firstTradeDate` 是負數（HON 是 −252322200＝1962），
 * 羅素 3000 有 28 檔老牌大型股是這樣。
 */
function epochDay(sec: number): string {
  return new Date(Number(sec) * 1000).toISOString().slice(0, 10)
}

/**
 * 分割事件的獨立證人。**與 `getPrices` 共用同一個請求與快取** ——
 * 呼叫這支不會多打任何一次外部請求。
 */
export async function getSplitFacts(
  ticker: string,
): Promise<{ splits: SplitFact[]; coverStart: string | null } | null> {
  const s = await getPrices(ticker)
  return s ? { splits: s.splits, coverStart: s.coverStart } : null
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
