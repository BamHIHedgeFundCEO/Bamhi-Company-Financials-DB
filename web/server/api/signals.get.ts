import { defineEventHandler, getQuery, createError, setHeader } from 'h3'
import { resolveCompany } from '../utils/cik'
import { getFinancials } from '../utils/financials'
import { computeMetrics, type MetricCell } from '../utils/metrics'
import { parseTickers, parseRange, clampWithLookback } from '../utils/params'
import { scoreAt, scoreSeries, arrowOf, pickModel, type ScoringConfig, type PeerStats } from '../utils/scoring'

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
 * 評分（`scoring.json` + `scoring.ts`）走同一份指標，**構面按 SIC 分五套**
 * （通用／銀行／產險／壽險／REIT）。銀行沒有毛利與存貨、壽險的保費與給付不對稱、
 * REIT 的淨利被折舊啃掉 —— 拿通用模型的 26 個計分項去套，這幾類公司只有 7～8 項
 * 算得出來、全部落在「無法評分」。那不是它們體質差，是我們拿錯了尺。
 *
 * 產業限定的科目與指標（存款、放款、已賺保費…）帶 `sector` 旗標：
 * 它們**不進三大報表也不進 Excel**，只在這裡接回指標的名字空間。
 *
 * 兩件不做：
 *   1. **不預測、不給買賣建議。**每個訊號回答的是一個可以當場核對的問題。
 *   2. **不把三種留白混成一種。**n/a（該申報卻抓不到）／—（不適用或基期不在區間內）
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
  // 產業限定科目與指標（銀行的存款、保險的已賺保費…）在這裡接回同一個名字空間。
  // 順序不能調換：sectorDerived 引用了 net_income_ttm／cfo_ttm 這些前面定義的指標，
  // 求值器是照陣列順序算的，放到前面會整條解不出來（check_signals.py 會擋）
  const lineItems = [...fin.lineItems, ...fin.sectorItems]
  const metrics = computeMetrics([...fin.derived, ...fin.sectorDerived], lineItems, fin.periods, annual, lookback)
  const cfg = await loadSignals()
  const scfg = await loadScoring()
  const peer = await loadPeer()

  // 比值的分母為負時整格作廢：淨利為負的營運現金流對淨利比、EBITDA 為負的淨負債倍數
  // 算得出漂亮的數字，但那個數字是反的。作廢寫成 n/a 並附理由，不寫 0 也不寫「—」
  const guardValue = (id: string, i: number): number | null => {
    const m = metrics.get(id)
    if (m) return m.cells[i]?.reason === 'ok' ? m.cells[i]!.value : null
    const li = lineItems.find((x) => x.id === id)
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
  // 產業模型只挑一次：銀行／保險／REIT 各有自己的一套構面，對不到區間就用通用模型。
  // 頁面要寫出用的是哪一套 —— 換模型等於換構面與換錨點，兩套之間的分數不可比
  // 第三個參數是「這個科目有沒有任何一期有值」——SIC 6211 裡券商與資產管理公司混在一起，
  // 靠存款與放款這兩個事實分流（見 ScoreModel.fallback_if_absent）
  const valuesOf = (id: string) => lineItems.find((x) => x.id === id)?.values ?? {}
  /**
   * 事實分流問的是「這家公司**現在**做的是哪一門生意」，所以只看最近四期。
   *
   * 「有沒有任何一期有值」會拿早就退出的業務決定今天的模型：Annaly 的商用不動產
   * 部門在 2021 年賣掉，最後一筆投資性不動產停在 2020-12-31，卻足以讓它繼續套
   * 權益型 REIT 模型 —— 14 項只有 4 項算得出來、覆蓋率 23.6%、拿不到總分。
   * 四期是因為年度模式（20-F 一欄＝一整年）下這就是四年，季度模式下是一整年。
   */
  const RECENT = 4
  const recentPeriods = fin.periods.slice(-RECENT)
  const modelFacts = {
    has: (id: string) => {
      const v = valuesOf(id)
      return recentPeriods.some((p) => v[p]?.value != null)
    },
    /** 最近一期「兩邊都有值」的佔比。放款佔資產是「這到底是不是放款業者」的判準 */
    shareOfAssets: (id: string) => {
      const v = valuesOf(id)
      const a = valuesOf('total_assets')
      for (let i = fin.periods.length - 1; i >= 0; i--) {
        const p = fin.periods[i]!
        const x = v[p]?.value
        const y = a[p]?.value
        if (x != null && y != null && y !== 0) return x / y
      }
      return null
    },
  }
  const model = pickModel(scfg, ref.sic, modelFacts)
  const ctx = {
    metrics,
    annual,
    sic: ref.sic,
    model,
    peer,
    rawAt: (id: string, i: number) => lineItems.find((x) => x.id === id)?.values[fin.periods[i]!]?.value ?? null,
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
      // 級距要送到前端：總分走勢圖的背景就是這幾條線 —— 走勢的意義是
      // 「跨過了哪一條」，不是「上升了幾像素」
      grades: scfg.grades,
      arrow,
      totalSeries,
      dimSeries: Object.fromEntries(
        Object.entries(seriesAll.dims).map(([k, v]) => [k, v.slice(lookback)]),
      ),
    },
    company: fin.company,
    cik: fin.cik,
    ticker: fin.ticker,
    // 評分模型是按 SIC 選的，頁面要說得出「你看的是哪一套」→ 代號一起回
    sic: ref.sic ?? null,
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
