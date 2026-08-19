import type { FinancialsResult } from './financials'
import { getPrices, priceAt, type PriceSeries } from './prices'

/**
 * 估值倍數：需要股價（SEC 不提供）。市值 = 期末股價 × 期末流通股數。
 * 流量科目用 TTM（近四季合計）以消除季節性。虧損 / 分母非正 → n/a（誠實，不給誤導值）。
 * 成長率（PEG 用）採 TTM 淨利年增率（近四季 vs 去年近四季）。
 */

export interface ValRow {
  id: string
  zh: string
  en: string
  unit: 'USD' | 'x' | 'ratio'
  values: Record<string, number | null>
  desc?: string
}

export interface Valuation {
  currency: string
  currentPrice: number | null
  rows: ValRow[]
}

function median(xs: number[]): number | null {
  const a = xs.filter((x) => Number.isFinite(x)).sort((x, y) => x - y)
  if (!a.length) return null
  const m = Math.floor(a.length / 2)
  return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2
}

export async function computeValuation(fin: FinancialsResult): Promise<Valuation | null> {
  const series = await getPrices(fin.ticker)
  if (!series) return null

  // 股價幣別和財報幣別不同就整頁不做。ADR 的股價是美元、但 TM 的財報是日圓、
  // BABA 是人民幣 —— 市值＝美元股價×股數、本益比＝美元市值／日圓淨利，算得出數字
  // 但完全沒有意義。這種「看起來正常的錯數字」比留白危險，寧可不出這張分頁。
  if (series.currency && fin.currency && series.currency !== fin.currency) return null

  const periods = fin.periods
  const li = new Map(fin.lineItems.map((x) => [x.id, x]))
  const val = (id: string, p: string) => li.get(id)?.values[p]?.value ?? null
  const endDate = (p: string) =>
    li.get('total_assets')?.values[p]?.endDate ?? li.get('revenue')?.values[p]?.endDate ?? null

  // TTM（近四季合計）；任一季缺 → null
  const idx = (p: string) => periods.indexOf(p)
  const ttm = (id: string, p: string): number | null => {
    const i = idx(p)
    if (i < 3) return null
    let s = 0
    for (let k = i - 3; k <= i; k++) {
      const v = val(id, periods[k])
      if (v == null) return null
      s += v
    }
    return s
  }
  const ebitdaTtm = (p: string): number | null => {
    const oi = ttm('operating_income', p)
    const da = ttm('dna', p)
    return oi != null && da != null ? oi + da : null
  }

  const price: Record<string, number | null> = {}
  const marketcap: Record<string, number | null> = {}
  for (const p of periods) {
    const d = endDate(p)
    const pr = d ? priceAt(series, d) : null
    price[p] = pr
    const sh = val('shares_outstanding', p)
    marketcap[p] = pr != null && sh != null ? pr * sh : null
  }

  const pos = (x: number | null) => (x != null && x > 0 ? x : null) // 分母須為正
  const pe: Record<string, number | null> = {}
  const ps: Record<string, number | null> = {}
  const pb: Record<string, number | null> = {}
  const pfcf: Record<string, number | null> = {}
  const ev: Record<string, number | null> = {}
  const evEbitda: Record<string, number | null> = {}
  const peg: Record<string, number | null> = {}
  for (const p of periods) {
    const mc = marketcap[p]
    pe[p] = mc != null ? divPos(mc, ttm('net_income', p)) : null
    ps[p] = mc != null ? divPos(mc, ttm('revenue', p)) : null
    const eq = val('equity', p)
    pb[p] = mc != null && pos(eq) ? mc / (eq as number) : null
    pfcf[p] = mc != null ? divPos(mc, ttmFcf(ttm, p)) : null
    const debt = (val('short_term_debt', p) ?? 0) + (val('long_term_debt', p) ?? 0)
    const cash = val('cash', p)
    const sti = val('short_term_investments', p) ?? 0
    ev[p] = mc != null && cash != null ? mc + debt - cash - sti : null
    evEbitda[p] = ev[p] != null ? divPos(ev[p]!, ebitdaTtm(p)) : null
    // PEG = PE / (TTM 淨利年增率 %)；成長須為正
    const niN = ttm('net_income', p)
    const i = idx(p)
    const niPrev = i >= 4 ? ttm('net_income', periods[i - 4]) : null
    const g = niN != null && niPrev != null && niPrev > 0 ? (niN / niPrev - 1) * 100 : null
    peg[p] = pe[p] != null && pos(g) ? pe[p]! / (g as number) : null
  }

  // PS 相對自身歷史中位數：< 1 代表比歷史便宜（可能是價值窪地或基本面惡化，需搭配判讀）
  const psMed = median(Object.values(ps).filter((x): x is number => x != null))
  const psVsMedian: Record<string, number | null> = {}
  for (const p of periods) psVsMedian[p] = ps[p] != null && psMed ? ps[p]! / psMed : null

  const rows: ValRow[] = [
    { id: 'price', zh: '期末股價', en: 'Price', unit: 'USD', values: price, desc: '該季末當時股價（Yahoo 月線，已還原分割/股利）。' },
    { id: 'marketcap', zh: '市值', en: 'Market Cap', unit: 'USD', values: marketcap, desc: '期末股價 × 期末流通股數。' },
    { id: 'pe', zh: '本益比', en: 'P/E (TTM)', unit: 'x', values: pe, desc: '市值 ÷ 近四季淨利。虧損時 n/a。' },
    { id: 'ps', zh: '股價營收比', en: 'P/S (TTM)', unit: 'x', values: ps, desc: '市值 ÷ 近四季營收。營收最難美化，虧損股也適用。' },
    { id: 'pb', zh: '股價淨值比', en: 'P/B', unit: 'x', values: pb, desc: '市值 ÷ 股東權益。' },
    { id: 'pfcf', zh: '股價自由現金流比', en: 'P/FCF (TTM)', unit: 'x', values: pfcf, desc: '市值 ÷ 近四季自由現金流。燒錢時 n/a。' },
    { id: 'ev', zh: '企業價值', en: 'Enterprise Value', unit: 'USD', values: ev, desc: '市值 + 總負債 − 現金 − 短期投資。' },
    { id: 'ev_ebitda', zh: 'EV／EBITDA', en: 'EV/EBITDA (TTM)', unit: 'x', values: evEbitda, desc: '排除資本結構的估值倍數。' },
    { id: 'peg', zh: '本益成長比', en: 'PEG (trailing)', unit: 'x', values: peg, desc: 'P/E ÷ 近四季淨利年增率(%)。< 1 常視為成長相對便宜。虧損/衰退時 n/a。' },
    { id: 'ps_vs_median', zh: 'PS／歷史中位數', en: 'P/S vs 5Y Median', unit: 'ratio', values: psVsMedian, desc: `目前 PS 相對自身歷史中位數（中位數≈${psMed?.toFixed(2) ?? 'n/a'}）。< 1 比歷史便宜，需搭配基本面判讀。` },
  ]

  return { currency: series.currency, currentPrice: series.current, rows }
}

function divPos(num: number, den: number | null): number | null {
  return den != null && den > 0 ? num / den : null
}
function ttmFcf(ttm: (id: string, p: string) => number | null, p: string): number | null {
  const cfo = ttm('cfo', p)
  const capex = ttm('capex', p)
  return cfo != null && capex != null ? cfo - capex : null
}
