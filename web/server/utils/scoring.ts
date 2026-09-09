/**
 * 量化評分引擎：把 `config/scoring.json` 的錨點套在既有指標上，算出構面分與總分。
 *
 * 為什麼敢給分數：**每一分都能拆回原始數字**。
 *   總分 → 構面分 → 逐項（指標原始值、bad／good 錨點、線性得分、權重）
 * 頁面把這條鏈整個攤開，讀者不同意某個錨點就自己重讀那一格。分數是整理，不是結論。
 *
 * 五條規則決定它會不會說謊：
 *
 * 1. **缺項移出分母，不是給 0 分。** 銀行沒有毛利與存貨，給 0 等於把「不適用」
 *    當成「很爛」。可用權重重新歸一，並回報覆蓋率。
 * 2. **構面覆蓋率不足就整個構面移出總分**（`dimension_coverage_floor`）。
 *    只靠一項撐起 25% 權重的構面不是「這層的分數」，是那一項的分數戴了構面的帽子
 *    —— 實測 JPM 的「營收品質」只有現金含量算得出來（銀行的營運現金流本來就會
 *    隨交易性資產大幅擺盪），那一項 0 分就把整個 25% 拉成 0。
 * 3. **產業上沒有意義的項目要明確排除**（`not_meaningful_sic`），而不是讓它算出
 *    一個很難看的分數。銀行的自由現金流、總資產週轉率都屬於這一類：那不是「表現差」，
 *    是這個指標對這門生意沒有定義。
 * 4. **總分是純水準分，不混趨勢分。** 一半的指標本身就是變化量（營收加速度、
 *    毛利率年變化、應收成長差），再套一層趨勢分＝二階微分，同一件事算兩次。
 *    方向另外用「本期總分 − 去年同期總分」表示。
 * 5. **方向箭頭只能比可比的項目。** 兩期之間可用項目不同的話，分數差測到的是
 *    「資料變多了」而不是「公司變好了」—— 實測 JPM 會因此報出 +28 分的假轉好。
 *    箭頭一律取兩期**都算得出來**的項目重算兩邊，並回報用了幾項。
 *
 * 錨點來源兩層，逐項獨立決定並回報用了哪一層（`anchorSource`）：
 *   1. **同業百分位**（`config/peer_stats.json`，`tools/peer_stats.py` 從 SEC DERA
 *      季度資料集離線算）—— 先找 4 位 SIC，樣本不足退 2 位大類
 *   2. 找不到就用 `config/scoring.json` 的**跨產業絕對錨點**
 * 公式與評分程式完全不動，只換 bad／good 兩個數字。
 *
 * 為什麼要分同業：毛利率的絕對 good 訂在 0.75，對軟體公司合理，對半導體設備、電信、
 * 能源是永遠拿不到的分數 —— 那樣分數裡混進的是「這是什麼產業」，不是公司好壞。
 * **但半調子的同業錨點比絕對錨點更危險**（看起來像有根據），所以樣本數不足就整組
 * 不給，寧可退回絕對值，並在頁面上標出這一項用的是哪一層。
 */
import type { MetricSeries } from './metrics'

export interface ScoreItemCfg {
  metric: string
  metric_annual?: string
  weight: number
  bad: number
  good: number
  invalid_if_nonpositive?: string[]
  invalid_if_nonpositive_annual?: string[]
  /** 這些 SIC 區間內的公司不計這一項（不是缺資料，是這個指標對那門生意沒有定義） */
  not_meaningful_sic?: [number, number][]
  not_meaningful_note?: string
}
export interface DimensionCfg {
  id: string
  zh: string
  weight: number
  desc: string
  items: ScoreItemCfg[]
}
/** tools/peer_stats.py 的產出：metrics[計分項][SIC 或 g+2位大類] = [bad, good] */
export interface PeerStats {
  version: string
  generated: string
  map_version?: string
  percentiles: [number, number]
  min_companies: number
  metrics: Record<string, Record<string, [number, number]>>
}

export interface ScoringConfig {
  version: string
  note: string
  coverage_floor: number
  dimension_coverage_floor: number
  grades: { lt?: number; id: string; zh: string }[]
  arrow: { lt?: number; id: string; zh: string }[]
  dimensions: DimensionCfg[]
}

export interface ScoredItem {
  metric: string
  zh: string
  formula: string
  desc: string
  value: number | null
  /** 0–100；沒算到為 null */
  score: number | null
  weight: number
  bad: number
  good: number
  /** ok | missing | inapplicable | window | invalid | not_meaningful */
  reason: string
  /** 這一項的 bad／good 是哪裡來的：同業 4 位 SIC／同業大類／跨產業絕對值 */
  anchorSource: 'peer4' | 'peer2' | 'absolute'
  /** 同業錨點時：用的是哪個 SIC 群 */
  anchorScope?: string
  note?: string
  isEstimated: boolean
}
export interface ScoredDimension {
  id: string
  zh: string
  desc: string
  weight: number
  score: number | null
  coverage: number
  /** 覆蓋率不足 → 這個構面不進總分（分數照給，讀者仍看得到算得出來的部分） */
  counted: boolean
  items: ScoredItem[]
}
export interface ScoreResult {
  /** 26 項裡有幾項用到同業錨點（頁面要說得出這個分數的錨點來自哪裡） */
  peerAnchored: number
  total: number | null
  grade: { id: string; zh: string } | null
  coverage: number
  counted: number
  itemsTotal: number
  dimensions: ScoredDimension[]
}

/** 線性映射並飽和。good < bad 時（越低越好）公式不必改：分母為負，方向自動反過來 */
function anchorScore(v: number, bad: number, good: number): number | null {
  if (good === bad) return null
  return Math.max(0, Math.min(1, (v - bad) / (good - bad))) * 100
}

function pick<T extends { lt?: number }>(v: number, bands: T[]): T | null {
  for (const b of bands) if (b.lt == null || v < b.lt) return b
  return bands.at(-1) ?? null
}

function sicIn(sic: string | undefined, ranges?: [number, number][]): boolean {
  if (!ranges?.length || !sic || !/^\d+$/.test(sic)) return false
  const n = Number(sic)
  return ranges.some(([lo, hi]) => n >= lo && n <= hi)
}

export interface ScoreCtx {
  metrics: Map<string, MetricSeries>
  annual: boolean
  sic?: string
  /** 同業百分位錨點；沒有就整組退回絕對錨點 */
  peer?: PeerStats | null
  /** 逐期原始科目值（作廢守門員要看單一科目，不只指標） */
  rawAt: (id: string, idx: number) => number | null
}

/** 逐項評分（不做彙總）。彙總拆出去，方向箭頭才能用「兩期都算得出來」的子集合重算 */
/**
 * 錨點解析：同業 4 位 SIC → 同業 2 位大類 → 跨產業絕對值。
 *
 * `bad === good` 的同業組一律跳過：那會讓 anchorScore 的分母為 0，得分整欄變 null
 * 而且不會有人發現（peer_stats.py 那端也擋一次，這裡是最後一道）。
 */
function anchorsFor(cfg: ScoreCtx, ci: ScoreItemCfg):
{ bad: number; good: number; source: 'peer4' | 'peer2' | 'absolute'; scope?: string } {
  const table = cfg.peer?.metrics?.[ci.metric]
  const sic = cfg.sic
  if (table && sic) {
    for (const [key, source] of [[sic, 'peer4'], [`g${sic.slice(0, 2)}`, 'peer2']] as const) {
      const a = table[key]
      if (a && a[0] !== a[1]) return { bad: a[0], good: a[1], source, scope: key }
    }
  }
  return { bad: ci.bad, good: ci.good, source: 'absolute' }
}

function scoreItems(cfg: ScoringConfig, ctx: ScoreCtx, idx: number): ScoredDimension[] {
  return cfg.dimensions.map((d) => {
    const items: ScoredItem[] = d.items.map((ci) => {
      const metricId = (ctx.annual && ci.metric_annual) || ci.metric
      const m = ctx.metrics.get(metricId)
      const cell = m?.cells[idx]

      const notMeaningful = sicIn(ctx.sic, ci.not_meaningful_sic)
      const guards = ((ctx.annual && ci.invalid_if_nonpositive_annual)
        || ci.invalid_if_nonpositive) ?? []
      const invalid = !notMeaningful && cell?.reason === 'ok'
        && guards.some((g) => {
          const gm = ctx.metrics.get(g)
          const v = gm ? (gm.cells[idx]?.reason === 'ok' ? gm.cells[idx]!.value : null) : ctx.rawAt(g, idx)
          return v == null || v <= 0
        })

      const usable = !notMeaningful && !invalid
        && !!cell && cell.reason === 'ok' && cell.value != null
      const an = anchorsFor(ctx, ci)
      return {
        metric: metricId,
        zh: m?.zh ?? metricId,
        formula: m?.formula ?? '',
        desc: m?.desc ?? '',
        value: usable ? cell!.value : null,
        score: usable ? anchorScore(cell!.value!, an.bad, an.good) : null,
        weight: ci.weight,
        bad: an.bad,
        good: an.good,
        anchorSource: an.source,
        anchorScope: an.scope,
        reason: notMeaningful ? 'not_meaningful' : invalid ? 'invalid' : (cell?.reason ?? 'missing'),
        note: notMeaningful ? ci.not_meaningful_note : undefined,
        isEstimated: !!cell?.isEstimated,
      }
    })
    return { id: d.id, zh: d.zh, desc: d.desc, weight: d.weight, score: null, coverage: 0, counted: false, items }
  })
}

/**
 * 彙總。`allowed` 給定時只採計這些指標（方向箭頭比較兩期時用，確保兩邊同一組項目）。
 * 會就地填回每個構面的 score / coverage / counted。
 */
function aggregate(cfg: ScoringConfig, dims: ScoredDimension[], allowed?: Set<string>): ScoreResult {
  let wSum = 0
  let wScore = 0
  let covNum = 0
  let covDen = 0
  let counted = 0
  let itemsTotal = 0
  let peerAnchored = 0

  for (const d of dims) {
    let iw = 0
    let iws = 0
    let iwAll = 0
    for (const it of d.items) {
      iwAll += it.weight
      itemsTotal++
      const ok = it.score != null && (!allowed || allowed.has(it.metric))
      if (ok && it.anchorSource !== 'absolute') peerAnchored++
      if (ok) {
        iw += it.weight
        iws += it.weight * it.score!
        counted++
      }
    }
    d.score = iw > 0 ? iws / iw : null
    d.coverage = iwAll > 0 ? iw / iwAll : 0
    // 覆蓋率不足的構面整個移出總分：只靠一兩項撐起 25% 權重的不是「這一層的分數」
    d.counted = d.score != null && d.coverage >= cfg.dimension_coverage_floor
    covDen += d.weight
    covNum += d.weight * d.coverage
    if (d.counted) {
      wSum += d.weight
      wScore += d.weight * d.score!
    }
  }

  const coverage = covDen > 0 ? covNum / covDen : 0
  const total = wSum > 0 && coverage >= cfg.coverage_floor ? Math.round(wScore / wSum) : null
  const grade = total != null ? pick(total, cfg.grades) : null
  return {
    peerAnchored,
    total,
    grade: grade ? { id: grade.id, zh: grade.zh } : null,
    coverage,
    counted,
    itemsTotal,
    dimensions: dims,
  }
}

export function scoreAt(cfg: ScoringConfig, ctx: ScoreCtx, idx: number): ScoreResult {
  return aggregate(cfg, scoreItems(cfg, ctx, idx))
}

/**
 * 逐期總分（畫分數走勢用）。只回總分與構面分，不帶逐項明細
 * —— 22 期 × 26 項的明細會讓回應肥四倍，而頁面只需要最新一期的明細。
 */
export function scoreSeries(cfg: ScoringConfig, ctx: ScoreCtx, n: number): {
  total: (number | null)[]
  dims: Record<string, (number | null)[]>
} {
  const total: (number | null)[] = []
  const dims: Record<string, (number | null)[]> = {}
  for (const d of cfg.dimensions) dims[d.id] = []
  for (let i = 0; i < n; i++) {
    const r = aggregate(cfg, scoreItems(cfg, ctx, i))
    total.push(r.total)
    for (const d of r.dimensions) dims[d.id]!.push(d.score)
  }
  return { total, dims }
}

export interface Arrow {
  delta: number | null
  now: number | null
  from: number | null
  /** 兩期都算得出來的項數 */
  comparableItems: number
  id: string
  zh: string
}

/**
 * 方向＝本期總分 − 去年同期總分，**只用兩期都算得出來的項目重算兩邊**。
 *
 * 直接拿兩期的公開總分相減是錯的：可用項目不同的話，差額測到的是資料多寡。
 * 實測 JPM 會報 +28「明顯轉好」，其實只是上一期少算了幾項。
 */
export function arrowOf(cfg: ScoringConfig, ctx: ScoreCtx, n: number): Arrow {
  const lag = ctx.annual ? 1 : 4
  const i = n - 1
  const j = i - lag
  if (j < 0) return { delta: null, now: null, from: null, comparableItems: 0, id: 'na', zh: '期數不足' }

  const cur = scoreItems(cfg, ctx, i)
  const prev = scoreItems(cfg, ctx, j)
  const scored = (dims: ScoredDimension[]) =>
    new Set(dims.flatMap((d) => d.items.filter((x) => x.score != null).map((x) => x.metric)))
  const both = scored(cur)
  const prevSet = scored(prev)
  for (const k of [...both]) if (!prevSet.has(k)) both.delete(k)

  const a = aggregate(cfg, cur, both)
  const b = aggregate(cfg, prev, both)
  if (a.total == null || b.total == null) {
    return { delta: null, now: a.total, from: b.total, comparableItems: both.size, id: 'na', zh: '可比項目不足' }
  }
  const delta = a.total - b.total
  const band = pick(delta, cfg.arrow)!
  return { delta, now: a.total, from: b.total, comparableItems: both.size, id: band.id, zh: band.zh }
}
