import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
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
export function loadMap(): XbrlMap {
  if (cachedMap) return cachedMap
  // config/ 位於 repo 根層（web/ 的上一層）
  const p = resolve(process.cwd(), '..', 'config', 'xbrl_zh_map.json')
  cachedMap = JSON.parse(readFileSync(p, 'utf-8')) as XbrlMap
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
 * key：Q:{fy}:{q}（單季/存量快照）、A:{fy}（全年累計，推 Q4 用）。
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
      if (days > 80 && days < 100) {
        const { fy, q } = fiscalOf(p.end, fyeMonth)
        put(`Q:${fy}:${q}`, p)
      } else if (days > 300 && days < 400) {
        put(`A:${fiscalOf(p.end, fyeMonth).fy}`, p)
      }
      // 半年/九月累計不用
    } else {
      // 存量：期末快照
      const { fy, q } = fiscalOf(p.end, fyeMonth)
      put(`Q:${fy}:${q}`, p)
    }
  }
  return best
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
  const map = loadMap()
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

  const allPeriods = new Set<string>()
  const lineItems: LineItem[] = []

  for (const concept of map.concepts) {
    const flow = isFlow(concept)
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
      for (const n of [1, 2, 3, 4] as const) {
        const p = best.get(`Q:${fy}:${n}`)
        if (p) values[periodKey(fy, n)] = toCell(p, p._tag)
      }
      if (flow && !values[periodKey(fy, 4)]) {
        // Edge case 2：Q4 通常只有全年累計 → Q4 = FY − Q1 − Q2 − Q3，標記推算
        const annual = best.get(`A:${fy}`)
        const q = [1, 2, 3].map((n) => best.get(`Q:${fy}:${n}`))
        if (annual && q[0] && q[1] && q[2]) {
          values[periodKey(fy, 4)] = {
            value: annual.val - q[0]!.val - q[1]!.val - q[2]!.val,
            isEstimated: true,
            sourceTag: annual._tag,
            accessionOrForm: annual.form,
            filed: annual.filed,
            endDate: annual.end,
          }
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
