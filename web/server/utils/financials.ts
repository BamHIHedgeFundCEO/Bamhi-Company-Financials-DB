import { secFetchJson } from './secFetch'
import type { CompanyRef } from './cik'

/**
 * 資料流 B：companyfacts → 三大報表季度時間序列。
 * - 對每個 concept 依 xbrl_zh_map.json 的 tags 順序找第一個有資料的標籤（fallback）
 * - 同一期間多筆 frame（重編）→ 取 filed 最新
 * - Q4 = FY − Q1 − Q2 − Q3（IS/CF 流量科目），isEstimated 標記；BS 存量科目直接用 FY 期末值
 * - 缺值 = null（前端/Excel 顯示 n/a，絕不是 0）
 */

interface FactPoint {
  start?: string
  end: string
  val: number
  fy: number
  fp: string // Q1 Q2 Q3 FY
  form: string
  filed: string
  frame?: string
}

type FactTags = Record<string, { units: Record<string, FactPoint[]> }>

interface CompanyFacts {
  entityName: string
  facts: { 'us-gaap'?: FactTags; 'ifrs-full'?: FactTags }
}

export interface MapConcept {
  id: string
  zh: string
  en: string
  statement: 'IS' | 'BS' | 'CF'
  unit: string
  sign: string
  note?: string
  derivable?: string
  /** 抓不到直接標籤時，用其他科目推算：如 "total_assets - equity"、"revenue - cogs" */
  derive?: string
  tags: string[]
  tags_ifrs?: string[]
}

export interface DerivedMetric {
  id: string
  zh: string
  en: string
  group: string
  formula: string
  desc: string
}

export interface XbrlMap {
  version: string
  concepts: MapConcept[]
  derived: DerivedMetric[]
}

export interface CellValue {
  value: number | null
  isEstimated: boolean // Q4 推算
  sourceTag?: string
  accessionOrForm?: string
  filed?: string
  endDate?: string
}

export interface LineItem {
  id: string
  zh: string
  en: string
  statement: string
  unit: string
  sign: string
  sourceTag: string | null
  values: Record<string, CellValue> // key: "FY2026 Q2"
}

export interface FinancialsResult {
  company: string
  cik: string
  ticker: string
  mapVersion: string
  /** quarterly（us-gaap，期別如 FY2026 Q2）或 annual（IFRS 外國發行人，期別如 FY2024） */
  periodicity: 'quarterly' | 'annual'
  /** 數值幣別。IFRS filer 優先取 20-F 的 USD 便利換算，否則為申報幣別（如 TWD） */
  currency: string
  periods: string[] // 由舊到新
  lineItems: LineItem[]
  derived: DerivedMetric[]
}

let cachedMap: XbrlMap | null = null
export async function loadMap(): Promise<XbrlMap> {
  if (cachedMap) return cachedMap
  // nitro serverAssets（nuxt.config serverAssets: config → repo 根層 config/）
  // 打包進 serverless bundle，本地與 Vercel 皆可讀
  const raw = await useStorage('assets:config').getItem('xbrl_zh_map.json')
  cachedMap = (typeof raw === 'string' ? JSON.parse(raw) : raw) as XbrlMap
  if (!cachedMap?.concepts) throw new Error('xbrl_zh_map.json 載入失敗')
  return cachedMap
}

/** "FY2026 Q2" 排序鍵 */
function periodKey(fy: number, q: number): string {
  return `FY${fy} Q${q}`
}

function unitKeyOf(unit: string): string {
  return unit // USD | shares | USD/shares，與 companyfacts units key 一致
}

/** 期間長度（天）— 區分單季（~91）與累計（半年/九月/全年） */
function spanDays(p: FactPoint): number | null {
  if (!p.start) return null
  return (Date.parse(p.end) - Date.parse(p.start)) / 86400_000
}

const isFlow = (c: MapConcept) => c.statement === 'IS' || c.statement === 'CF'

/**
 * ⚠️ companyfacts 的 fy/fp 是「申報件」的年度/期別，不是數據本身的期間
 * （同一季數字會以比較期身分出現在後續多份申報，fy/fp 各不同）。
 * 期別一律由數據自己的 start/end 日期 + 公司會計年度末月份推得。
 */

/** 由 end 日期推 (fy, q)。fyeMonth = 會計年度末月份（1-12）。 */
function fiscalOf(end: string, fyeMonth: number): { fy: number; q: number } {
  const [y, m] = end.split('-').map(Number)
  const diff = (fyeMonth - m + 12) % 12
  const fy = new Date(y, m - 1 + diff).getFullYear()
  const q = 4 - Math.round(diff / 3)
  return { fy, q }
}

/** 從 XBRL 年度期間（span>300 天）的 end 月份取眾數 → 會計年度末月份 */
function inferFyeMonth(gaap: Record<string, { units: Record<string, FactPoint[]> }>): number {
  const count = new Map<number, number>()
  for (const tag of Object.values(gaap)) {
    for (const points of Object.values(tag.units)) {
      for (const p of points) {
        const days = spanDays(p)
        if (days !== null && days > 300 && days < 400) {
          const m = Number(p.end.split('-')[1])
          count.set(m, (count.get(m) ?? 0) + 1)
        }
      }
    }
  }
  let bestM = 12
  let bestN = 0
  for (const [m, n] of count) if (n > bestN) { bestN = n; bestM = m }
  return bestM
}

/**
 * 從單一 tag 的 point 陣列整理出各 fiscal period 的值。
 * key：
 *   Q:{fy}:{q} — 單季（流量）或期末快照（存量）
 *   C:{fy}:{q} — 年初至第 q 季末的累計（現金流量表在 10-Q 只申報累計，靠差分還原單季）
 *   A:{fy}     — 全年
 * 同期間多筆（重編）→ filed 最新。
 */
function collect(points: FactPoint[], flow: boolean, fyeMonth: number) {
  const best = new Map<string, FactPoint>()
  const put = (key: string, p: FactPoint) => {
    const prev = best.get(key)
    if (!prev || p.filed > prev.filed) best.set(key, p)
  }
  for (const p of points) {
    if (!p.end) continue
    if (flow) {
      const days = spanDays(p)
      if (days === null) continue
      const { fy, q } = fiscalOf(p.end, fyeMonth)
      if (days > 80 && days < 100) {
        put(`Q:${fy}:${q}`, p) // 單季
        if (q === 1) put(`C:${fy}:1`, p) // 首季亦為累計
      } else if (days > 150 && days < 200) {
        put(`C:${fy}:2`, p) // 半年累計
      } else if (days > 240 && days < 290) {
        put(`C:${fy}:3`, p) // 九月累計
      } else if (days > 300 && days < 400) {
        put(`A:${fy}`, p)
      }
    } else {
      // 存量：期末快照
      const { fy, q } = fiscalOf(p.end, fyeMonth)
      put(`Q:${fy}:${q}`, p)
    }
  }
  return best
}

/** 比值接近哪個常見分割倍數（8% 容差），否則 1 */
function nearestSplit(ratio: number): number {
  for (const s of [2, 3, 4, 5, 6, 7, 8, 10, 20]) {
    if (Math.abs(ratio - s) / s < 0.08) return s
  }
  return 1
}

interface SplitEvent {
  threshold: string // 申報日界線：filed < threshold 者為分割前基準
  factor: number
}

/**
 * 由加權平均股數（基本）序列偵測股票分割。
 * 關鍵：值屬「分割前/後」由其**申報日**決定，不是期別——分割後公司會重編舊期比較數，
 * 那些重編值（filed 較晚）已是新基準，不可再調整。
 */
function computeSplits(ns: FactTags, fyeMonth: number): SplitEvent[] {
  const pts =
    ns['WeightedAverageNumberOfSharesOutstandingBasic']?.units?.['shares'] ??
    ns['WeightedAverageNumberOfDilutedSharesOutstanding']?.units?.['shares']
  if (!pts?.length) return []

  const best = new Map<string, { end: string; val: number; filed: string }>()
  for (const p of pts) {
    const days = spanDays(p)
    if (days === null || !(days > 80 && days < 100)) continue
    const { fy, q } = fiscalOf(p.end, fyeMonth)
    const k = `FY${fy} Q${q}`
    const prev = best.get(k)
    if (!prev || p.filed > prev.filed) best.set(k, { end: p.end, val: p.val, filed: p.filed })
  }
  const ordered = [...best.values()].sort((a, b) => (a.end < b.end ? -1 : 1))
  const splits: SplitEvent[] = []
  for (let i = 0; i + 1 < ordered.length; i++) {
    if (ordered[i].val <= 0) continue
    const f = nearestSplit(ordered[i + 1].val / ordered[i].val) // 後值變大 → 分割
    if (f > 1) splits.push({ threshold: ordered[i + 1].filed, factor: f })
  }
  return splits
}

/** 依申報日把值正規化到最新基準：股數乘、每股除。filed < threshold 的每個分割都套用。 */
function splitAdjust(val: number, filed: string, unit: string, splits: SplitEvent[]): number {
  let f = 1
  for (const s of splits) if (filed < s.threshold) f *= s.factor
  if (f === 1) return val
  return unit === 'shares' ? val * f : val / f
}

function toCell(p: FactPoint, tag: string, estimated = false): CellValue {
  return {
    value: p.val,
    isEstimated: estimated,
    sourceTag: tag,
    accessionOrForm: p.form,
    filed: p.filed,
    endDate: p.end,
  }
}

export async function getFinancials(
  ref: CompanyRef,
  fromFy: number,
  toFy: number,
): Promise<FinancialsResult> {
  const map = await loadMap()
  const facts = await secFetchJson<CompanyFacts>(
    `https://data.sec.gov/api/xbrl/companyfacts/CIK${ref.cik10}.json`,
  )
  // 國內發行人 → us-gaap 季度模式；IFRS 外國發行人（TSM/ASML）→ ifrs-full 年度模式
  // （20-F 只有年度 XBRL，6-K 不進 companyfacts）
  const gaap = facts.facts['us-gaap']
  const ifrs = facts.facts['ifrs-full']
  const useIfrs = !gaap || (Object.keys(gaap).length < 20 && !!ifrs)
  const ns: FactTags = (useIfrs ? ifrs : gaap) ?? {}
  const fyeMonth = inferFyeMonth(ns)

  // 幣別：優先 USD（20-F 常附便利換算），否則取 namespace 中最常見的幣別
  const currency = useIfrs ? inferCurrency(ns) : 'USD'
  const unitPrefs = (unit: string): string[] => {
    if (unit === 'USD') return ['USD', currency]
    if (unit === 'USD/shares') return ['USD/shares', `${currency}/shares`]
    return [unit]
  }

  // 股票分割還原：companyfacts 存的是「申報當下」的股數/EPS，分割後舊期只在
  // 分割前的申報出現 → 整條序列出現 ~10x 斷層。以加權股數序列偵測分割倍數，
  // 把舊期正規化到最新基準（股數 ×factor、每股數值 ÷factor）。
  const splits = useIfrs ? [] : computeSplits(ns, fyeMonth)

  const allPeriods = new Set<string>()
  const lineItems: LineItem[] = []

  for (const concept of map.concepts) {
    const flow = isFlow(concept)
    // 每股盈餘、加權股數不可相加：不能做 Q4=全年−前三季，也不做累計差分
    const nonadditive = concept.unit === 'shares' || concept.unit === 'USD/shares'
    const tags = useIfrs ? (concept.tags_ifrs ?? []) : concept.tags

    // tags 依優先序逐期 fallback：高優先標籤已有的期間不被覆蓋，
    // 缺的期間由後續標籤補（公司中途換標籤時——如 NVDA 營收——單一標籤涵蓋不了全期間）
    let chosenTag: string | null = null
    const best = new Map<string, FactPoint & { _tag: string }>()
    for (const tag of tags) {
      const units = ns[tag]?.units
      const points = unitPrefs(concept.unit).map((u) => units?.[u]).find((p) => p?.length)
      if (!points) continue
      chosenTag ??= tag
      for (const [key, p] of collect(points, flow, fyeMonth)) {
        if (!best.has(key)) best.set(key, { ...p, _tag: tag })
      }
    }

    const values: Record<string, CellValue> = {}
    for (let fy = fromFy; fy <= toFy; fy++) {
      if (useIfrs) {
        // 年度模式：流量取全年累計，存量取年度末快照（Q4 位置）
        const p = best.get(flow ? `A:${fy}` : `Q:${fy}:4`)
        if (p) values[`FY${fy}`] = toCell(p, p._tag)
        continue
      }
      if (nonadditive) {
        // 單季直接值（Q1-Q3）。EPS/股數不可相加，不做 Q4=全年−前三季。分割依申報日正規化。
        const adj = (p: FactPoint & { _tag: string }): CellValue => {
          const c = toCell(p, p._tag)
          if (c.value != null) c.value = splitAdjust(p.val, p.filed, concept.unit, splits)
          return c
        }
        for (const n of [1, 2, 3] as const) {
          const p = best.get(`Q:${fy}:${n}`)
          if (p) values[periodKey(fy, n)] = adj(p)
        }
        // Q4：股數用 10-K 年度加權平均（真實申報值，股數變化緩，近似 Q4 水準，供稀釋率計算）；
        // EPS 的年度值 ≠ Q4 單季 → 留 n/a 不誤導
        if (concept.unit === 'shares') {
          const a = best.get(`A:${fy}`)
          if (a) values[periodKey(fy, 4)] = adj(a)
        }
        continue
      }
      if (!flow) {
        for (const n of [1, 2, 3, 4] as const) {
          const p = best.get(`Q:${fy}:${n}`)
          if (p) values[periodKey(fy, n)] = toCell(p, p._tag)
        }
        continue
      }
      // 流量科目：以「年初至今累計」序列重建單季，再差分。
      // 現金流量表在 10-Q 只申報累計（半年/九月/全年），且部分公司缺 Q1 累計
      // （償還債務等一次性項目常如此）→ 用向前補值填內部缺口，把總數落到下一個可量測季。
      const qd = (n: number) => best.get(`Q:${fy}:${n}`)
      const cumC = (n: number) => best.get(`C:${fy}:${n}`)
      const annual = best.get(`A:${fy}`)

      // cum[q] = 年初到第 q 季末的累計值（優先直接申報，其次以單季相加）
      const cum: (number | null)[] = [0, null, null, null, null]
      const src: (FactPoint & { _tag: string } | null)[] = [null, null, null, null, null]
      const setCum = (q: number, val: number, p: FactPoint & { _tag: string }) => {
        cum[q] = val
        src[q] = p
      }
      for (const n of [1, 2, 3] as const) {
        const c = cumC(n)
        const s = qd(n)
        if (c) setCum(n, c.val, c)
        else if (s && cum[n - 1] != null) setCum(n, cum[n - 1]! + s.val, s)
      }
      if (annual) setCum(4, annual.val, annual)
      else if (qd(4) && cum[3] != null) setCum(4, cum[3]! + qd(4)!.val, qd(4)!)

      // 最後一個有值的累計季（超過此季視為尚未申報，不輸出）
      let lastKnown = 0
      for (let q = 1; q <= 4; q++) if (cum[q] != null) lastKnown = q
      // 補內部缺口：向前補值（假設該季無活動）→ 一次性金額落到下一個可量測季
      for (let q = 1; q <= lastKnown; q++) if (cum[q] == null) cum[q] = cum[q - 1]

      for (let q = 1; q <= lastKnown; q++) {
        const anchor = src[q] ?? src[lastKnown]!
        values[periodKey(fy, q)] = {
          value: cum[q]! - cum[q - 1]!,
          isEstimated: q === 4 && !qd(4), // Q4 由全年推算 → 橘底
          sourceTag: anchor._tag,
          accessionOrForm: anchor.form,
          filed: anchor.filed,
          endDate: anchor.end,
        }
      }
    }
    for (const k of Object.keys(values)) allPeriods.add(k)
    lineItems.push({
      id: concept.id,
      zh: concept.zh,
      en: concept.en,
      statement: concept.statement,
      unit: concept.unit,
      sign: concept.sign,
      sourceTag: chosenTag,
      values,
    })
  }

  // 推算 fallback：抓不到直接標籤的科目（如 AMZN 無「負債總計」標籤），
  // 用其他科目算（total_liabilities = total_assets − equity）。只補缺的期，不覆蓋已有值。
  const byId = new Map(lineItems.map((li) => [li.id, li]))
  for (const concept of map.concepts) {
    if (!concept.derive) continue
    const li = byId.get(concept.id)
    if (!li) continue
    const m = concept.derive.match(/^(\w+)\s*([+-])\s*(\w+)$/)
    if (!m) continue
    const [, aId, op, bId] = m
    const a = byId.get(aId)
    const b = byId.get(bId)
    if (!a || !b) continue
    for (const p of allPeriods) {
      if (li.values[p]?.value != null) continue
      const av = a.values[p]?.value
      const bv = b.values[p]?.value
      if (av == null || bv == null) continue
      li.values[p] = {
        value: op === '-' ? av - bv : av + bv,
        isEstimated: true, // 推算值（非直接申報）
        sourceTag: `推算：${concept.derive}`,
        endDate: a.values[p]?.endDate,
      }
    }
  }

  const periods = [...allPeriods].sort() // FY2023 Q1 < FY2023 Q2 < ...（年度模式 FY2023 < FY2024）字典序即正確
  return {
    company: facts.entityName || ref.name,
    cik: ref.cik10,
    ticker: ref.ticker,
    mapVersion: map.version,
    periodicity: useIfrs ? 'annual' : 'quarterly',
    currency,
    periods,
    lineItems,
    derived: map.derived,
  }
}

/** IFRS namespace 內最常見的幣別 unit key（排除 shares/pure）。有 USD 便利換算就回 USD。 */
function inferCurrency(ns: FactTags): string {
  const count = new Map<string, number>()
  for (const tag of Object.values(ns)) {
    for (const u of Object.keys(tag.units)) {
      if (/^[A-Z]{3}$/.test(u)) count.set(u, (count.get(u) ?? 0) + 1)
    }
  }
  if (count.has('USD')) return 'USD'
  let bestU = 'USD'
  let bestN = 0
  for (const [u, n] of count) if (n > bestN) { bestN = n; bestU = u }
  return bestU
}
