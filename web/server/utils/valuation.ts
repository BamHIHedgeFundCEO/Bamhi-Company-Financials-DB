import { applyDerives, loadMap, type FinancialsResult } from './financials'
import { computeMetrics, type MetricSeries } from './metrics'
import { getPrices, rawCloseAt } from './prices'

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
  const annual = fin.periodicity === 'annual'
  const li = new Map(fin.lineItems.map((x) => [x.id, x]))
  const endDate = (p: string) =>
    li.get('total_assets')?.values[p]?.endDate ?? li.get('revenue')?.values[p]?.endDate ?? null

  // TTM（近四季合計）；任一季缺 → null
  //
  // ⚠ 外國發行人（20-F，`periodicity: 'annual'`）一欄就是一整年，再加四欄等於四年。
  //   不分流的話 P/E 會變成實際的四分之一，而且看起來完全正常：SHEL FY2025 顯示
  //   4.26 倍（實際約 17 倍）。整條估值分頁的流量科目都吃這個函式，錯一次錯全部。
  const step = annual ? 1 : 4
  const idx = (p: string) => periods.indexOf(p)
  // 指標（ebitda／fcf／net_debt）一律取 `xbrl_zh_map.json` 的 `derived` 算出來的值，
  // 與 Excel 估值分頁引用的是同一列。原本這裡自己寫一份：EBITDA 曾經是營業利益 ＋ 折舊
  // （關鍵指標那邊早已改成 EBIT ＋ 折舊），有息負債版 EV 至今少算長期租賃 —— 同一個網站
  // 兩種 EV，而且兩邊都看起來正常。在下面背書補值之後才算（`metricSeries`）
  let metricSeries = new Map<string, MetricSeries>()
  const val = (id: string, p: string): number | null => {
    const cell = li.get(id)?.values[p]
    if (cell) return cell.value ?? null
    const m = metricSeries.get(id)
    return m ? (m.cells[idx(p)]?.value ?? null) : null
  }
  // TTM（近四季合計）；任一季缺 → null。與 workbook.py 的 `ttm()` 同一條規則
  //
  // ⚠ 外國發行人（20-F，`periodicity: 'annual'`）一欄就是一整年，再加四欄等於四年。
  //   不分流的話 P/E 會變成實際的四分之一，而且看起來完全正常：SHEL FY2025 顯示
  //   4.26 倍（實際約 17 倍）。整條估值分頁的流量科目都吃這個函式，錯一次錯全部。
  const ttm = (id: string, p: string): number | null => {
    const i = idx(p)
    if (annual) return val(id, p)
    if (i < 3) return null
    let s = 0
    for (let k = i - 3; k <= i; k++) {
      const v = val(id, periods[k])
      if (v == null) return null
      s += v
    }
    return s
  }

  const price: Record<string, number | null> = {}
  const marketcap: Record<string, number | null> = {}
  for (const p of periods) {
    const d = endDate(p)
    // 原始成交價 ÷ 股數實際套用、而且在該交易日之後才除權的分割 → 與股數同一個基準
    const hit = d ? rawCloseAt(series, d) : null
    let pr: number | null = null
    if (hit) {
      let f = 1
      for (const s of fin.splitBasis ?? []) if (s.exDate > hit[0]) f *= s.factor
      pr = hit[1] / f
    }
    price[p] = pr
    const sh = val('shares_outstanding', p)
    marketcap[p] = pr != null && sh != null ? pr * sh : null
  }

  /**
   * 缺標籤的債務科目「以上限背書」補 0。**寫回 fin.lineItems**（Excel 的關鍵指標與
   * 估值分頁都是公式，讀的就是這幾格；只改這裡才能讓淨負債／ROIC／EV 一起活過來）。
   *
   * 論證是恆等式不是估計：長期債必定屬於非流動負債，所以看不到的那筆
   * **必定 ≤ 負債總計 − 流動負債合計**。上限佔市值夠小 → 補 0 對估值的影響有界；
   * 上限算不出來（銀行的資產負債表不分流動）或太大 → 維持 n/a。
   *
   * 這是先前 `zero_if_sibling`（只看「另一段有申報」）失敗後的替代：那條在 JPM 身上
   * 補 0 補到 22 期全錯，因為它沒有任何量化界線。這條在 JPM 直接算不出上限。
   */
  const map = await loadMap()
  for (const concept of map.concepts) {
    const rule = concept.zero_if_bounded
    if (!rule) continue
    const target = li.get(concept.id)
    if (!target) continue
    for (const p of periods) {
      if (target.values[p]?.value != null) continue
      const mc = marketcap[p]
      if (mc == null || mc <= 0) continue
      let bound = 0
      let ok = true
      for (const id of rule.bound_plus) {
        const v = val(id, p)
        if (v == null) { ok = false; break }
        bound += v
      }
      // 扣項缺值就當 0：上限只會變鬆，論證仍然成立（加項缺值才是真的算不出上限）
      if (ok) for (const id of rule.bound_minus) bound -= val(id, p) ?? 0
      if (!ok || bound < 0) continue // 上限算不出來就不猜（銀行沒有「流動負債合計」）
      const share = bound / mc
      if (share > rule.max_share_of_market_cap) continue
      target.values[p] = {
        value: 0,
        isEstimated: true,
        sourceTag: `無標籤，上限背書視為 0（未解釋非流動負債 ${(share * 100).toFixed(1)}% 市值）`,
        endDate: endDate(p) ?? undefined,
      }
    }
  }

  // 背書補完要把 derive 再跑一遍。`debt_total` 是 `long_term_debt + short_term_debt?`
  // 推算出來的，而 financials.ts 那一遍跑在背書之前 —— 那時這兩格還是空的，
  // 於是無負債公司（ANET／ALGN／APPF／ALAB／AUR 實測）的有息負債合計永遠是 n/a，
  // 負債權益比整排落空。applyDerives 只補空格、不覆蓋已有值，重跑是安全的
  applyDerives(map, li, periods)
  metricSeries = computeMetrics(
    map.derived.filter((d) => VAL_METRICS.includes(d.id)), fin.lineItems, periods, annual)

  const pos = (x: number | null) => (x != null && x > 0 ? x : null) // 分母須為正
  const pe: Record<string, number | null> = {}
  const ps: Record<string, number | null> = {}
  const pb: Record<string, number | null> = {}
  const pfcf: Record<string, number | null> = {}
  const ev: Record<string, number | null> = {}
  const evDebt: Record<string, number | null> = {}
  const evEbitda: Record<string, number | null> = {}
  const peg: Record<string, number | null> = {}
  for (const p of periods) {
    const mc = marketcap[p]
    pe[p] = mc != null ? divPos(mc, ttm('net_income', p)) : null
    ps[p] = mc != null ? divPos(mc, ttm('revenue', p)) : null
    const eq = val('equity', p)
    pb[p] = mc != null && pos(eq) ? mc / (eq as number) : null
    pfcf[p] = mc != null ? divPos(mc, ttm('fcf', p)) : null
    /**
     * EV 兩種定義都給，因為它們回答的是不同問題：
     *
     * `ev`（主列，永遠算得出來）＝ 市值 + **負債總計** − 現金 − 短期投資。
     *   買下整間公司要扛下的是資產負債表右邊的全部，不只有息借款。
     *   負債總計缺標籤時本身就有 `total_assets - equity_total` 的推算，所以不會沒有。
     *   代價：與 Bloomberg／CapIQ 口徑不同（它們只算有息負債），
     *   銀行更誇張 —— JPM 的負債總計是幾兆的**存款**。
     *
     * `ev_debt`（有息負債版）＝ 市值 + 淨負債（`net_debt`：有息負債含長期租賃 − 現金 − 短期投資），
     *   與外部資料商可比，也與 Excel 估值分頁引用同一列。債務不明就 n/a（`?? 0` 會把
     *   「查不到」講成「沒有」，JPM 的長期債只在帶維度的事實裡，那樣會少算幾千億還看起來很正常）。
     */
    const cash = val('cash', p)
    const sti = val('short_term_investments', p) ?? 0
    const totalLiab = val('total_liabilities', p)
    ev[p] = mc != null && cash != null && totalLiab != null ? mc + totalLiab - cash - sti : null
    const nd = val('net_debt', p)
    evDebt[p] = mc != null && nd != null ? mc + nd : null
    evEbitda[p] = ev[p] != null ? divPos(ev[p]!, ttm('ebitda', p)) : null
    // PEG = PE / (TTM 淨利年增率 %)；成長須為正
    const niN = ttm('net_income', p)
    const i = idx(p)
    // 去年同期：季度往前四欄、年度往前一欄（PEG 的成長率同樣不能寫死 4）
    const niPrev = i >= step ? ttm('net_income', periods[i - step]) : null
    const g = niN != null && niPrev != null && niPrev > 0 ? (niN / niPrev - 1) * 100 : null
    peg[p] = pe[p] != null && pos(g) ? pe[p]! / (g as number) : null
  }

  // PS 相對自身歷史中位數：< 1 代表比歷史便宜（可能是價值窪地或基本面惡化，需搭配判讀）
  const psMed = median(Object.values(ps).filter((x): x is number => x != null))
  const psVsMedian: Record<string, number | null> = {}
  for (const p of periods) psVsMedian[p] = ps[p] != null && psMed ? ps[p]! / psMed : null

  const rows: ValRow[] = [
    { id: 'price', zh: '期末股價', en: 'Price', unit: 'USD', values: price, desc: '該季末當時股價（Yahoo 日線，季末當日或前一交易日收盤；已還原分割、未還原股利）。' },
    { id: 'marketcap', zh: '市值', en: 'Market Cap', unit: 'USD', values: marketcap, desc: '期末股價 × 期末流通股數。' },
    { id: 'pe', zh: '本益比', en: 'P/E (TTM)', unit: 'x', values: pe, desc: '市值 ÷ 近四季淨利。虧損時 n/a。' },
    { id: 'ps', zh: '股價營收比', en: 'P/S (TTM)', unit: 'x', values: ps, desc: '市值 ÷ 近四季營收。營收最難美化，虧損股也適用。' },
    { id: 'pb', zh: '股價淨值比', en: 'P/B', unit: 'x', values: pb, desc: '市值 ÷ 股東權益。' },
    { id: 'pfcf', zh: '股價自由現金流比', en: 'P/FCF (TTM)', unit: 'x', values: pfcf, desc: '市值 ÷ 近四季自由現金流。燒錢時 n/a。' },
    { id: 'ev', zh: '企業價值', en: 'Enterprise Value', unit: 'USD', values: ev, desc: '市值 + 負債總計 − 現金 − 短期投資。買下整間公司要扛下的全部負債，不只有息借款。' },
    { id: 'ev_debt', zh: '企業價值（僅有息負債）', en: 'EV (Interest-Bearing Debt)', unit: 'USD', values: evDebt, desc: '市值 + 淨負債（有息負債含長期租賃 − 現金 − 短期投資，與關鍵指標分頁同一列）。與外部資料可比；債務查不到時為 n/a。' },
    { id: 'ev_ebitda', zh: 'EV／EBITDA', en: 'EV/EBITDA (TTM)', unit: 'x', values: evEbitda, desc: '排除資本結構的估值倍數。' },
    { id: 'peg', zh: '本益成長比', en: 'PEG (trailing)', unit: 'x', values: peg, desc: 'P/E ÷ 近四季淨利年增率(%)。< 1 常視為成長相對便宜。虧損/衰退時 n/a。' },
    { id: 'ps_vs_median', zh: 'PS／歷史中位數', en: 'P/S vs 5Y Median', unit: 'ratio', values: psVsMedian, desc: `目前 PS 相對自身歷史中位數（中位數≈${psMed?.toFixed(2) ?? 'n/a'}）。< 1 比歷史便宜，需搭配基本面判讀。` },
  ]

  return { currency: series.currency, currentPrice: series.current, rows }
}

function divPos(num: number, den: number | null): number | null {
  return den != null && den > 0 ? num / den : null
}
/** 估值用到的指標（定義在 xbrl_zh_map.json 的 derived，與 Excel 估值分頁引用的列相同） */
const VAL_METRICS = ['ebitda', 'fcf', 'net_debt']
