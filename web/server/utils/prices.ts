/**
 * 股價來源：Yahoo Finance chart API（免 key、免 library，serverless 上比 yfinance 穩）。
 * 用 quote.close（雅虎 chart API 的 close 已還原分割、**未還原股利**）→ 與本站 split-adjusted
 * 股數同基準，市值＝當時真正的股價 × 股數。**不能用 adjclose**：它連股利也還原，越早的
 * 股價被往下調越多 —— 配息公司的歷史市值、P/E、P/S、EV 會系統性偏低（Caterpillar
 * 2025-03-31 顯示 $324.10，那天實際收盤價更高；一年前的倍數低估約一個殖利率）。
 * SEC 不提供股價，估值倍數（PE/PS/PB/EV…）唯一的外部相依就在這裡。
 */

export interface PriceSeries {
  currency: string
  current: number | null
  /** 由舊到新的 [YYYY-MM-DD, close]，日線（貼近季末當日收盤；已還原分割、未還原股利） */
  daily: [string, number][]
  /** 交易所紀錄的分割除權事件（由舊到新）。與 SEC 完全獨立，用來仲裁 computeSplits */
  splits: SplitFact[]
  /**
   * 雅虎拿來回溯調整 `close` 的**全部**事件，含分拆造成的零碎比例（`splits` 濾掉的那些）。
   * 估值要用它把收盤價還原成當天實際成交價（`rawCloseAt`）—— 見那支的註解
   */
  adjEvents: SplitFact[]
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

  // 日線、近 10 年（涵蓋 40 季上限）；close 已還原分割、未還原股利
  // `events=split` 在**同一個請求**裡多回除權日與確切比例，零額外外部請求
  const url = `https://query1.finance.yahoo.com/v8/finance/chart/${encodeURIComponent(key)}?range=10y&interval=1d&events=split`
  try {
    const res = await fetch(url, { headers: { 'User-Agent': 'Mozilla/5.0' } })
    if (!res.ok) return null
    const j = (await res.json()) as any
    const r = j?.chart?.result?.[0]
    if (!r) return null
    const ts: number[] = r.timestamp ?? []
    const adj: (number | null)[] = r.indicators?.quote?.[0]?.close ?? []
    const daily: [string, number][] = []
    for (let i = 0; i < ts.length; i++) {
      const v = adj[i]
      if (v != null) daily.push([new Date(ts[i] * 1000).toISOString().slice(0, 10), v])
    }
    const splits: SplitFact[] = []
    const adjEvents: SplitFact[] = []
    for (const ev of Object.values<any>(r.events?.splits ?? {})) {
      const num = Number(ev?.numerator)
      const den = Number(ev?.denominator)
      if (num > 0 && den > 0 && isFinite(num) && isFinite(den))
        adjEvents.push({ date: epochDay(ev.date), factor: num / den })
      if (!isCleanRatio(num, den)) continue
      splits.push({ date: epochDay(ev.date), factor: num / den })
    }
    splits.sort((a, b) => (a.date < b.date ? -1 : 1))
    adjEvents.sort((a, b) => (a.date < b.date ? -1 : 1))

    const firstTrade = r.meta?.firstTradeDate != null ? epochDay(r.meta.firstTradeDate) : null
    const windowStart = daily[0]?.[0] ?? null
    const data: PriceSeries = {
      currency: r.meta?.currency ?? 'USD',
      current: r.meta?.regularMarketPrice ?? (daily.at(-1)?.[1] ?? null),
      daily,
      splits,
      adjEvents,
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
): Promise<{ splits: SplitFact[]; adjEvents: SplitFact[]; coverStart: string | null } | null> {
  const s = await getPrices(ticker)
  return s ? { splits: s.splits, adjEvents: s.adjEvents, coverStart: s.coverStart } : null
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

/**
 * <= 目標日期的最近交易日收盤，**還原成那一天實際的成交價**（乘回之後每一次雅虎調整過的事件）。
 * 回傳 [交易日, 原始價]。
 *
 * 為什麼不能直接用 `close`：雅虎的 close 是「以今天的股數基準」回溯調整的，而我們的股數
 * 只套用 `arbitrateSplits` 認定的那幾次分割。兩邊的事件集合不同，市值就錯一個倍數 ——
 * 而且看起來完全正常。13F 逐家「申報市值 ÷ 股數」對 396 檔 2026-06-30 收盤價實測：
 * 392 檔一分不差，錯的 4 檔全是**季末之後**的公司行動：
 *   - APH 2026-09-03 的 2:1：除權後還沒有任何 SEC 申報，股數不調，價格卻已被砍半
 *     → 市值 1,087 億（實際 2,174 億）、本益比少一半，**整段歷史每一期都是**
 *   - REZI 2026-08-04 的分拆（1437:1000）：零碎比例本來就不進股數正規化，價格卻被調低三成
 *   - HON 2026-06-29 分拆夾帶 1:2 反向分割（雅虎記成 1907:2000）：股數照 1:2 正規化，
 *     價格只調了 5% → 分拆前每一期市值少一半
 * 解法是讓價格與股數吃**同一份事件清單**：先還原成原始成交價，再由呼叫端除以網站
 * 真正套用到股數的那幾次分割（`FinancialsResult.splitBasis`）。
 */
export function rawCloseAt(series: PriceSeries, date: string): [string, number] | null {
  const a = series.daily
  let lo = 0
  let hi = a.length - 1
  let best = -1
  while (lo <= hi) {
    const mid = (lo + hi) >> 1
    if (a[mid]![0] <= date) {
      best = mid
      lo = mid + 1
    } else {
      hi = mid - 1
    }
  }
  if (best < 0) return null
  const [td, close] = a[best]!
  let f = 1
  // 除權日當天的收盤已經是新基準，所以只乘回「交易日之後」的事件
  for (const e of series.adjEvents) if (e.date > td) f *= e.factor
  return [td, close * f]
}
