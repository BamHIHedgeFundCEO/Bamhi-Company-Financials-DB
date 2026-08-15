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

interface CompanyFacts {
  entityName: string
  facts: { 'us-gaap'?: Record<string, { units: Record<string, FactPoint[]> }> }
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
 * 從單一 tag 的 point 陣列整理出各 fiscal period 的值。
 * 回傳 quarterly: Map<periodKey, CellValue>；annual: Map<fy, CellValue>（流量科目推 Q4 用）
 */
function collect(points: FactPoint[], flow: boolean) {
  // 同期間多筆 → filed 最新
  const best = new Map<string, FactPoint>()
  for (const p of points) {
    if (!p.fp || !p.fy) continue
    if (flow) {
      const days = spanDays(p)
      if (days === null) continue
      const isQuarter = days < 100
      const isAnnual = days > 300
      if (!isQuarter && !isAnnual) continue // 半年/九月累計不用
      const key = isAnnual ? `A:${p.fy}` : `Q:${p.fy}:${p.fp}`
      if (isQuarter && !['Q1', 'Q2', 'Q3', 'Q4'].includes(p.fp)) continue
      const prev = best.get(key)
      if (!prev || p.filed > prev.filed) best.set(key, p)
    } else {
      // 存量：每個 fp（Q1/Q2/Q3/FY）都是期末快照
      const key = `S:${p.fy}:${p.fp}`
      const prev = best.get(key)
      if (!prev || p.filed > prev.filed) best.set(key, p)
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
  const gaap = facts.facts['us-gaap'] ?? {}

  const allPeriods = new Set<string>()
  const lineItems: LineItem[] = []

  for (const concept of map.concepts) {
    const flow = isFlow(concept)
    const unitKey = unitKeyOf(concept.unit)

    // tags 依序 fallback：第一個有資料的即停止
    let chosenTag: string | null = null
    let best: Map<string, FactPoint> | null = null
    for (const tag of concept.tags) {
      const units = gaap[tag]?.units
      const points = units?.[unitKey]
      if (points?.length) {
        chosenTag = tag
        best = collect(points, flow)
        break
      }
    }

    const values: Record<string, CellValue> = {}
    if (best && chosenTag) {
      for (let fy = fromFy; fy <= toFy; fy++) {
        if (flow) {
          const q: (FactPoint | undefined)[] = [1, 2, 3].map((n) => best!.get(`Q:${fy}:Q${n}`))
          const q4direct = best.get(`Q:${fy}:Q4`)
          const annual = best.get(`A:${fy}`)
          for (const n of [1, 2, 3] as const) {
            const p = q[n - 1]
            if (p) values[periodKey(fy, n)] = toCell(p, chosenTag)
          }
          if (q4direct) {
            values[periodKey(fy, 4)] = toCell(q4direct, chosenTag)
          } else if (annual && q[0] && q[1] && q[2]) {
            // Edge case 2：Q4 = FY − Q1 − Q2 − Q3，標記推算
            values[periodKey(fy, 4)] = {
              value: annual.val - q[0].val - q[1].val - q[2].val,
              isEstimated: true,
              sourceTag: chosenTag,
              accessionOrForm: annual.form,
              filed: annual.filed,
              endDate: annual.end,
            }
          } else if (annual) {
            // 湊不齊三季無法推算 → Q4 維持缺值，僅提供全年於原始資料
          }
        } else {
          // 存量：Q1-Q3 用 fp=Qn，Q4 用 fp=FY 期末快照
          for (const n of [1, 2, 3] as const) {
            const p = best.get(`S:${fy}:Q${n}`)
            if (p) values[periodKey(fy, n)] = toCell(p, chosenTag)
          }
          const fyEnd = best.get(`S:${fy}:FY`)
          if (fyEnd) values[periodKey(fy, 4)] = toCell(fyEnd, chosenTag)
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

  const periods = [...allPeriods].sort() // FY2023 Q1 < FY2023 Q2 < ... 字典序即正確
  return {
    company: facts.entityName || ref.name,
    cik: ref.cik10,
    ticker: ref.ticker,
    mapVersion: map.version,
    periods,
    lineItems,
    derived: map.derived,
  }
}
