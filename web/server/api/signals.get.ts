import { defineEventHandler, getQuery, createError, setHeader } from 'h3'
import { resolveCompany } from '../utils/cik'
import { getFinancials } from '../utils/financials'
import { computeMetrics, type MetricCell } from '../utils/metrics'
import { parseTickers, parseRange, clampWithLookback } from '../utils/params'
import { scoreAt, scoreSeries, arrowOf, type ScoringConfig, type PeerStats } from '../utils/scoring'

/**
 * GET /api/signals?ticker=NVDA&years=5
 *
 * 轉折訊號：把 companyfacts 既有的數字轉成「在變好還是變壞」的判讀。
 *
 * **零額外 SEC 請求**：走的是 `getFinancials` 同一條路（同一份 companyfacts、
 * 同一層快取），與財務報表分頁共用。這一頁沒有任何自己的資料來源。
 *
 * 公式一律取自 `config/xbrl_zh_map.json` 的 `derived`（與 Excel 關鍵指標分頁同一份），
 * 門檻與判讀文案取自 `config/signals.json`。新增一個訊號＝改兩份設定，不動程式。
 *
 * 三件不做：
 *   1. **不做綜合評分。**十七個訊號壓成一個分數要靠權重，那是我們憑空造的數字，
 *      SEC 沒說過。頁面只給逐項判讀與「幾項亮警訊」的計數。
 *   2. **不預測、不給買賣建議。**每個訊號回答的是一個可以當場核對的問題。
 *   3. **不把三種留白混成一種。**n/a（該申報卻抓不到）／—（不適用或基期不在區間內）
 *      各自保留理由，前端照理由寫字。
 */

interface Band { lt?: number; state: string; zh: string }
interface SignalDef {
  id: string
  metric: string
  /** 外國發行人（20-F，一欄＝一整年）改用的指標。滾動四季的指標在年度資料上沒有意義 */
  metric_annual?: string
  layer: string
  unit: string
  question?: string
  read: string
  bands?: Band[]
  pairs_with?: string[]
  /** 這幾個科目／指標 ≤ 0 時，本訊號的比值沒有意義（淨利為負時的現金含量、
   *  EBITDA 為負時的淨負債倍數 —— 後者會算出負數而「看起來很安全」） */
  invalid_if_nonpositive?: string[]
  invalid_if_nonpositive_annual?: string[]
  /** 只給背景、不判好壞（資本支出強度、商譽佔比：跨產業沒有共同門檻） */
  state_override?: string
}
interface SignalsConfig {
  version: string
  note: string
  layers: { id: string; zh: string; source: string; desc: string }[]
  signals: SignalDef[]
}

let cachedCfg: SignalsConfig | null = null
/** ⚠️ module-level 快取載入後永不失效。改 config/signals.json 後 dev server 不重啟吃不到 */
async function loadSignals(): Promise<SignalsConfig> {
  if (cachedCfg) return cachedCfg
  const raw = await useStorage('assets:config').getItem('signals.json')
  const parsed = (typeof raw === 'string' ? JSON.parse(raw) : raw) as SignalsConfig | null
  if (!parsed?.signals?.length) throw new Error('signals.json 載入失敗')
  cachedCfg = parsed
  return cachedCfg
}

let cachedPeer: PeerStats | null | undefined
/**
 * ⚠️ 同上，module-level 快取。
 * 讀不到就是 `null`（整組退回絕對錨點），不是錯誤 —— peer_stats.json 是選用的，
 * 沒跑過批次的環境照樣要能評分。
 */
async function loadPeer(): Promise<PeerStats | null> {
  if (cachedPeer !== undefined) return cachedPeer
  try {
    const raw = await useStorage('assets:config').getItem('peer_stats.json')
    const parsed = (typeof raw === 'string' ? JSON.parse(raw) : raw) as PeerStats | null
    cachedPeer = parsed?.metrics ? parsed : null
  } catch {
    cachedPeer = null
  }
  return cachedPeer
}

let cachedScore: ScoringConfig | null = null
/** ⚠️ 同上，module-level 快取；改 config/scoring.json 後 dev server 不重啟吃不到 */
async function loadScoring(): Promise<ScoringConfig> {
  if (cachedScore) return cachedScore
  const raw = await useStorage('assets:config').getItem('scoring.json')
  const parsed = (typeof raw === 'string' ? JSON.parse(raw) : raw) as ScoringConfig | null
  if (!parsed?.dimensions?.length) throw new Error('scoring.json 載入失敗')
  cachedScore = parsed
  return cachedScore
}

/** 值落在哪一級。bands 依 `lt` 由小到大排，最後一段不寫 lt ＝ 以上全收 */
function classify(v: number, bands: Band[]): Band | null {
  for (const b of bands) {
    if (b.lt == null || v < b.lt) return b
  }
  return bands.at(-1) ?? null
}

export default defineEventHandler(async (event) => {
  const query = getQuery(event)
  const [ticker] = parseTickers(query.ticker)
  const range = parseRange(query as Record<string, unknown>)

  const ref = await resolveCompany(ticker!)
  if (!ref) {
    throw createError({
      statusCode: 404,
      statusMessage: 'Not Found',
      message: `找不到「${ticker}」。請確認 ticker 拼寫；已下市公司與多數 ETF 不在 SEC 申報名單內。`,
    })
  }

  // 多抓兩年當 lookback。要 8 季不是 4 季：滾動四季的年變化（本期四季 vs 去年同期四季）
  // 最遠踩到 t-7，只留 4 季的話顯示區間的前幾欄會整排「基期不在區間內」。
  // companyfacts 是同一份、同一層快取，多裁幾季不會多打任何一次 SEC
  const fin = await getFinancials(ref, range.fromFy - 2, range.toFy)
  clampWithLookback(fin, range, 8)
  const lookback = (fin as { lookbackCount?: number }).lookbackCount ?? 0

  const annual = fin.periodicity === 'annual'
  const metrics = computeMetrics(fin.derived, fin.lineItems, fin.periods, annual)
  const cfg = await loadSignals()
  const scfg = await loadScoring()
  const peer = await loadPeer()

  // 比值的分母為負時整格作廢：淨利為負的營運現金流對淨利比、EBITDA 為負的淨負債倍數
  // 算得出漂亮的數字，但那個數字是反的。作廢寫成 n/a 並附理由，不寫 0 也不寫「—」
  const guardValue = (id: string, i: number): number | null => {
    const m = metrics.get(id)
    if (m) return m.cells[i]?.reason === 'ok' ? m.cells[i]!.value : null
    const li = fin.lineItems.find((x) => x.id === id)
    return li?.values[fin.periods[i]!]?.value ?? null
  }

  const outPeriods = fin.periods.slice(lookback)
  const signals = cfg.signals.map((s) => {
    // 滾動四季的指標對 20-F 發行人（一欄＝一整年）無意義，改用同名的單期指標
    const metricId = (annual && s.metric_annual) || s.metric
    const m = metrics.get(metricId)
    const cells: MetricCell[] = m ? m.cells.slice(lookback) : outPeriods.map(() => ({
      value: null, reason: 'missing' as const, isEstimated: false,
    }))
    const guards = ((annual && s.invalid_if_nonpositive_annual) || s.invalid_if_nonpositive) ?? []
    const series = cells.map((c, i) => {
      const gi = i + lookback
      const invalid = c.reason === 'ok'
        && guards.some((g) => {
          const v = guardValue(g, gi)
          return v == null || v <= 0
        })
      const cell: MetricCell & { state: string; stateZh?: string; invalid?: boolean } = invalid
        ? { value: null, reason: 'missing', isEstimated: c.isEstimated, state: 'na', invalid: true }
        : { ...c, state: 'na' }
      if (cell.reason === 'ok' && cell.value != null) {
        if (s.state_override) {
          cell.state = s.state_override
        } else if (s.bands?.length) {
          const b = classify(cell.value, s.bands)
          cell.state = b?.state ?? 'flat'
          cell.stateZh = b?.zh
        } else {
          cell.state = 'info'
        }
      }
      return cell
    })
    const latestIdx = series.length - 1
    return {
      id: s.id,
      metric: metricId,
      layer: s.layer,
      unit: s.unit,
      question: s.question ?? null,
      read: s.read,
      pairsWith: s.pairs_with ?? [],
      zh: m?.zh ?? s.metric,
      en: m?.en ?? '',
      formula: m?.formula ?? '',
      desc: m?.desc ?? '',
      inapplicable: !!m?.inapplicable,
      guards,
      series,
      latest: latestIdx >= 0
        ? { period: outPeriods[latestIdx], ...series[latestIdx] }
        : null,
    }
  })

  // ── 量化評分 ────────────────────────────────────────
  // 逐期算，因為方向箭頭＝「本期總分 − 去年同期總分」，用分數自己的歷史，
  // 不另外定義一組沒人能驗證的趨勢分權重
  const ctx = {
    metrics,
    annual,
    sic: ref.sic,
    peer,
    rawAt: (id: string, i: number) => fin.lineItems.find((x) => x.id === id)?.values[fin.periods[i]!]?.value ?? null,
  }
  const seriesAll = scoreSeries(scfg, ctx, fin.periods.length)
  const score = scoreAt(scfg, ctx, fin.periods.length - 1)
  const totalSeries = seriesAll.total.slice(lookback)
  const arrow = arrowOf(scfg, ctx, fin.periods.length)

  setHeader(event, 'Cache-Control', 'public, s-maxage=3600, stale-while-revalidate=86400')
  return {
    score: {
      ...score,
      version: scfg.version,
      coverageFloor: scfg.coverage_floor,
      arrow,
      totalSeries,
      dimSeries: Object.fromEntries(
        Object.entries(seriesAll.dims).map(([k, v]) => [k, v.slice(lookback)]),
      ),
    },
    company: fin.company,
    cik: fin.cik,
    ticker: fin.ticker,
    mapVersion: fin.mapVersion,
    signalsVersion: cfg.version,
    periodicity: fin.periodicity,
    currency: fin.currency,
    periods: outPeriods,
    layers: cfg.layers,
    signals,
    note: cfg.note,
  }
})
