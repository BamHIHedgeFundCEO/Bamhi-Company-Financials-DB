import { secFetchJson } from './secFetch'
import { notApplicableFor } from './applicability'
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
  facts: { 'us-gaap'?: FactTags; 'ifrs-full'?: FactTags; dei?: FactTags }
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
  /** 該科目「沒申報」通常代表 0（如當期無一年內到期債務）→ 缺口補 0，避免財務結構指標間歇 n/a */
  zero_if_absent?: boolean
  tags: string[]
  tags_ifrs?: string[]
  /**
   * 這些標籤取到的值要乘 -1。用在「同一個科目、不同標籤的正負號慣例相反」的情況：
   * `InterestIncomeExpenseNet` 是利息收入(費用)**淨額**，正值代表淨收入，跟
   * `InterestExpense`（正值代表費用）剛好相反。實測 1000 家有 248 家走前者，
   * 不翻號的話「利息費用」那一列會混著兩種慣例，跨公司比較全錯。
   */
  negate_tags?: string[]
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
  filed?: string // 取值來源申報日（filed 最新，可能是後續重編）
  origFiled?: string // 原始申報日（該期最早申報，供稽核對照）
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
  /** false = 這個科目對該公司的產業本來就不存在（銀行沒有存貨、控股公司沒有毛利）。
   *  缺值時要寫「—」不是 n/a —— 兩種留白的意思完全不同，見 applicability.ts。
   *  只影響缺值的呈現，不影響任何有值的格子 */
  applicable?: boolean
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
  /** 偵測到上市/SPAC 借殼前的期（股數基礎不可比，已清為 n/a）；供 UI/Excel 標註 */
  preIpoBefore?: string
  /** 估值倍數（需股價，另行計算後掛上；SEC 資料本身不含） */
  valuation?: import('./valuation').Valuation
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

interface ClassShares {
  version: string
  companies: Record<string, {
    ticker: string
    name: string
    basis: string
    shares: Record<string, number>
    /** 加權平均股數（期間結束日 → 股數）。只有無維度值本身就錯的公司才有 */
    wavg?: { basic?: Record<string, number>; diluted?: Record<string, number> }
  }>
}

let cachedClassShares: ClassShares | null = null
/**
 * 多股別公司的期末流通股數（`config/class_shares.json`，由 `tools/class_shares.py` 離線產生）。
 *
 * 波克夏、Visa、ADT、Ryan Specialty…把封面股數按股別拆、帶了維度，companyfacts 只收
 * 無維度事實 → 整個標籤消失、期末股數整欄 n/a、估值分頁跟著連鎖 n/a。數字只在申報的
 * XBRL instance 裡，但「單 ticker 查詢 ≤ 2 次 SEC 請求」是硬規則，線上解析一定超標，
 * 所以離線算好放設定資產、執行期零 SEC 請求。檔案裡沒有的期就維持 n/a。
 */
async function loadClassShares(): Promise<ClassShares> {
  if (cachedClassShares) return cachedClassShares
  const raw = await useStorage('assets:config').getItem('class_shares.json')
  cachedClassShares = ((typeof raw === 'string' ? JSON.parse(raw) : raw) as ClassShares) ?? {
    version: '',
    companies: {},
  }
  return cachedClassShares
}

/** 供 Excel 快取 key 用：預算檔改版 → 舊活頁簿失效 */
export async function loadClassSharesVersion(): Promise<string> {
  return (await loadClassShares()).version
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

/**
 * 由 end 日期推 (fy, q)。fyeMonth = 會計年度末月份（1-12）。
 * 用「距最近會計年度末的天數」判季，容忍 52/53 週會計年度的日期漂移
 * （零售/消費股年末在月底附近跨月漂移，純用月份相減會把季別分錯 → BS 科目某季消失）。
 */
function fiscalOf(end: string, fyeMonth: number): { fy: number; q: number } {
  const et = Date.parse(end + 'T00:00:00Z')
  const [y] = end.split('-').map(Number)
  const TOL = 20 * 86400_000 // 年末後 20 天內仍算該年度末（漂移容忍）
  // 該日期所屬會計年度 = 結束於「>= 日期−容忍」的最近一個年度末
  let fyeYear = y
  let fyeT = Date.UTC(y, fyeMonth, 0)
  for (const yy of [y - 1, y, y + 1, y + 2]) {
    const t = Date.UTC(yy, fyeMonth, 0) // 日曆月 fyeMonth 的最後一天
    if (t >= et - TOL) {
      fyeYear = yy
      fyeT = t
      break
    }
  }
  const daysBefore = (fyeT - et) / 86400_000 // >0：日期在年度末之前（較早的季）
  const qBack = ((Math.round(daysBefore / 91.31) % 4) + 4) % 4
  const q = qBack === 0 ? 4 : 4 - qBack
  return { fy: fyeYear, q }
}

/**
 * 從 XBRL 年度期間（span 300–400 天）的 end 月份取眾數 → 會計年度末月份。
 *
 * 兩個修正，少任何一個都會讓整家公司的季別錯格：
 * 1. **只看最近 6 個年度**。改過會計年度的公司，舊制度的票數會壓過現行制度
 *    （L3Harris 2019 年前 6 月結算、現在 12 月底，全歷史取眾數得到 6 月 → 近年每季
 *    都對不上，表上只剩 Q1 有值。HRB 2022 年 4 月改 6 月、Oshkosh 9 月改 12 月、
 *    Under Armour 12 月改 3 月、Royal Gold 6 月改 12 月同理，實測 1000 家中 14 家）。
 * 2. **月份用 end−5 天算**。52/53 週會計年度的年末會漂過月界，Leidos／L3Harris 年末
 *    落在 1/2、Broadcom 落在 11/2，直接取月份會得到「1 月結算」「11 月結算」這種
 *    不存在的制度。
 * 票數用「事實筆數」而不是「不同年末日數」：Amazon 有少量 TTM 揭露（每季末都有一筆
 * 12 個月期間），一個日期一票的話會被這些雜訊灌成 3 月結算。
 */
function inferFyeMonth(gaap: Record<string, { units: Record<string, FactPoint[]> }>): number {
  const perEnd = new Map<string, number>()
  for (const tag of Object.values(gaap)) {
    for (const points of Object.values(tag.units)) {
      for (const p of points) {
        const days = spanDays(p)
        if (days !== null && days > 300 && days < 400) {
          perEnd.set(p.end, (perEnd.get(p.end) ?? 0) + 1)
        }
      }
    }
  }
  if (!perEnd.size) return 12
  let latest = ''
  for (const end of perEnd.keys()) if (end > latest) latest = end
  const cutoff = Date.parse(latest + 'T00:00:00Z') - 6 * 365 * 86400_000

  const count = new Map<number, number>()
  for (const [end, n] of perEnd) {
    const t = Date.parse(end + 'T00:00:00Z')
    if (t < cutoff) continue
    const m = new Date(t - 5 * 86400_000).getUTCMonth() + 1
    count.set(m, (count.get(m) ?? 0) + n)
  }
  let bestM = 12
  let bestN = 0
  for (const [m, n] of count) if (n > bestN) { bestN = n; bestM = m }
  return bestM
}

/**
 * 由年度期間（300–400 天）反推每個會計年度「真正的」結束日。
 *
 * 52/53 週會計年度的年末會在月底附近漂移（AVGO 2025 年末是 11/2、BBY 是 2/1），
 * 用「日曆月最後一天」當年初推算會差到快一個月，YTD 事實就會被誤判成單季。
 * 同一年度多筆 end（重編、不同 tag）取眾數。
 */
function inferFyEnds(gaap: FactTags, fyeMonth: number): Map<number, number> {
  const votes = new Map<number, Map<string, number>>()
  for (const tag of Object.values(gaap)) {
    for (const points of Object.values(tag.units)) {
      for (const p of points) {
        const days = spanDays(p)
        if (days === null || days <= 300 || days >= 400) continue
        const { fy, q } = fiscalOf(p.end, fyeMonth)
        // 只收落在年度末位置的：改過會計年度的公司，舊制度的年報期間會被歸到新制度的
        // 某個季（Under Armour 由 12 月改 3 月，2021 曆年年報落在 FY2022 Q3），
        // 拿它當年末會讓年初推算整整差一季，該年度的年報事實全被當成非累計而丟掉。
        if (q !== 4) continue
        const bucket = votes.get(fy) ?? new Map<string, number>()
        bucket.set(p.end, (bucket.get(p.end) ?? 0) + 1)
        votes.set(fy, bucket)
      }
    }
  }
  const out = new Map<number, number>()
  for (const [fy, bucket] of votes) {
    let bestEnd = ''
    let bestN = 0
    for (const [end, n] of bucket) if (n > bestN) { bestN = n; bestEnd = end }
    if (bestEnd) out.set(fy, Date.parse(bestEnd + 'T00:00:00Z'))
  }
  return out
}

/**
 * 從單一 tag 的 point 陣列整理出各 fiscal period 的值。
 * key：
 *   Q:{fy}:{q} — 單季（流量）或期末快照（存量）
 *   C:{fy}:{q} — 年初至第 q 季末的累計（現金流量表在 10-Q 只申報累計，靠差分還原單季）
 *   A:{fy}     — 全年
 * 同期間多筆（重編）→ filed 最新。
 */
function collect(points: FactPoint[], flow: boolean, fyeMonth: number,
                 fyEnds: Map<number, number>) {
  const best = new Map<string, FactPoint & { _origFiled?: string }>()
  const put = (key: string, p: FactPoint) => {
    const prev = best.get(key)
    // 取 filed 最新（重編值）；同時記該期最早申報日（原始申報，供稽核）
    const orig = prev?._origFiled && prev._origFiled < p.filed ? prev._origFiled : p.filed
    if (!prev || p.filed > prev.filed) best.set(key, { ...p, _origFiled: orig })
    else if (prev) prev._origFiled = orig
  }
  for (const p of points) {
    if (!p.end) continue
    if (flow) {
      const days = spanDays(p)
      if (days === null) continue
      if (days < 45 || days > 400) continue // 月報/多年期間，兩者都不是我們要的粒度
      const { fy, q } = fiscalOf(p.end, fyeMonth)
      // 累計 vs 單季看「起始日是不是會計年度第一天」，不用天數區間硬切。
      // 零售/食品業的 52/53 週曆常是 16-12-12-12 週，Q1 長 112 天，落在舊規則的
      // 「單季 80–100」與「半年 150–200」之間 → 整筆丟掉。實測 ACI（Albertsons）
      // 每年 Q1 欄變成 0、Q2 欄放的是 28 週累計，等於憑空生出「首季零營收」。
      const prevEnd = fyEnds.get(fy - 1) ?? Date.UTC(fy - 1, fyeMonth, 0)
      const fyStartT = prevEnd + 86400_000
      const cumulative = Math.abs(Date.parse(p.start!) - fyStartT) <= 15 * 86400_000
      if (days > 300) {
        if (cumulative) put(`A:${fy}`, p) // 非年初起算的 300+ 天是 TTM 之類的揭露，不採
      } else if (cumulative) {
        put(`C:${fy}:${q}`, p)
        if (q === 1) put(`Q:${fy}:1`, p) // 首季累計即首季單季
        if (q === 4) put(`A:${fy}`, p) // 年初到年末＝全年（極少數公司只申報這種）
      } else if (days <= 130) {
        put(`Q:${fy}:${q}`, p) // 單季
      }
      // 其餘：不從年初起算、又跨兩三季的區間（如 Q2+Q3），無法定位，丟棄
    } else {
      // 存量：期末快照
      const { fy, q } = fiscalOf(p.end, fyeMonth)
      put(`Q:${fy}:${q}`, p)
    }
  }
  return best
}

/** 比值接近哪個常見分割倍數（8% 容差），否則 null。回傳「乾淨倍數」與方向。 */
function detectSplit(ratio: number): number | null {
  // 25（Booking 2026）與 50（Chipotle 2024）都真的發生過。倍數愈大，8% 的相對容差
  // 就愈寬（50 的容差是 ±4），所以只收公司實際用過的整數，不是「每個整數都放進來」。
  const CLEAN = [2, 3, 4, 5, 6, 7, 8, 10, 12, 15, 20, 25, 30, 50]
  if (ratio >= 1.5) {
    for (const s of CLEAN) if (Math.abs(ratio - s) / s < 0.08) return s // 正向：factor = s
  } else if (ratio > 0 && ratio <= 0.67) {
    for (const s of CLEAN) if (Math.abs(1 / ratio - s) / s < 0.08) return 1 / s // 反向：factor = 1/s
  }
  return null
}

interface SplitEvent {
  threshold: string // 申報日界線：filed < threshold 者為分割前基準
  factor: number // 新/舊 股數比（正向>1，反向<1）
}

/**
 * 一次候選分割的「觀測」。同一次分割會被很多期、很多訊號各看到一次，
 * 每次只能把時點框在一個區間內：lo 那份申報還是舊基準，hi 那份已是新基準，
 * 所以真正的界線落在 (lo, hi]。
 */
interface RawSplit {
  lo: string
  hi: string
  factor: number // 新/舊 股數比（正向>1，反向<1）
  fromShares: boolean // 股數訊號比每股盈餘訊號可信
}

/**
 * 偵測股票分割（正向與反向皆可）。
 *
 * 穩健訊號：**同一期在不同申報間，股數突然差一個乾淨倍數**——只有分割會如此
 * （發股/回購會改變該期真實股數，不會把同一期重編成 10 倍）。這避免把 SPAC 增資、
 * 大量發股誤判為分割。分割後公司重編舊期，重編值（filed 較晚）已是新基準。
 */
function computeSplits(ns: FactTags, fyeMonth: number): SplitEvent[] {
  const raw: RawSplit[] = []

  // 訊號 A：單季加權股數同期跨申報差乾淨倍數
  const sharePts =
    ns['WeightedAverageNumberOfSharesOutstandingBasic']?.units?.['shares'] ??
    ns['WeightedAverageNumberOfDilutedSharesOutstanding']?.units?.['shares']
  collectCrossFilingSplits(sharePts, fyeMonth, false, raw)

  // 訊號 B：每股盈餘同期跨申報（分割後每股值變小）——
  // 補足只用單季股數偵測不到的情況（如 GOOGL 只在分割後才 tag 單季股數）
  collectCrossFilingSplits(ns['EarningsPerShareBasic']?.units?.['USD/shares'], fyeMonth, true, raw)

  // 訊號 C：期末流通股數（instant）同一個期末日跨申報。這是時點最緊的一種證據——
  // 每份申報都會重報上一個會計年度末的股數，所以界線能框到「相鄰兩份申報之間」。
  // Palo Alto 2022 年 9 月的 3:1 只有這個訊號抓得到；少了它，界線會晚半年落在
  // 2023-05-24，中間那兩季本來就是新基準，卻被再乘一次 3。
  const instant = instantShares(ns)
  for (const list of instant.values())
    for (let i = 0; i + 1 < list.length; i++) {
      const f = detectSplit(list[i + 1].val / list[i].val)
      if (f && list[i].filed < list[i + 1].filed)
        raw.push({ lo: list[i].filed, hi: list[i + 1].filed, factor: f, fromShares: true })
    }

  // 同一次分割會被多期／多訊號重複偵測到，倍數與時點都不見得一致
  // （EPS 訊號會摻進重編造成的假倍數，跨兩次分割的年度值還會給出兩者的乘積）。
  // 用區間交集分群：取還沒歸類的觀測中最早的 hi 當界線，凡是區間罩得住它的都是同一次。
  // 舊版是「倍數不同就算另一次分割」然後連乘，DXCM 一次 4:1 被算成 4×3×4。
  // 用時間窗分群也不行：Copart 2022、2023 連兩次 2:1 只差一年，會被併成一次。
  const clusters: RawSplit[][] = []
  for (const forward of [true, false]) {
    let pend = raw
      .filter((s) => s.factor > 1 === forward && s.lo < s.hi) // 正向與反向分割不混為一談
      .sort((a, b) => (a.hi < b.hi ? -1 : 1))
    while (pend.length) {
      const point = pend[0].hi
      const inside = (s: RawSplit) => s.lo < point && point <= s.hi
      clusters.push(pend.filter(inside))
      pend = pend.filter((s) => !inside(s))
    }
  }

  const out: SplitEvent[] = []
  for (const c of clusters) {
    const threshold = c[0].hi
    const votes = new Map<number, number>()
    for (const s of c) votes.set(s.factor, (votes.get(s.factor) ?? 0) + (s.fromShares ? 2 : 1))
    const ranked = [...votes].sort((a, b) => b[1] - a[1] || b[0] - a[0]).map(([f]) => f)

    // 用「跨過界線的同期股數」當裁判：真分割一定會把舊期股數重編成乾淨倍數，
    // 只改分子的事件（分拆、停業單位重分類）則股數原封不動。
    const across = sharesAcross(sharePts, fyeMonth, instant, threshold)
    const confirmed = ranked.find((f) => across.some((r) => Math.abs(r / f - 1) < 0.08))
    if (confirmed != null) {
      out.push({ threshold, factor: confirmed })
      continue
    }
    // 沒有任何一期股數變成該倍數，卻有期間股數幾乎沒動 → 不是分割，整群丟掉。
    // Crane NXT 分拆後重編 EPS（4.60→0.86、7.11→3.61）剛好落在 5 倍與 2 倍附近，
    // 股數卻始終是 5,700 萬；照舊版會連乘 10 倍。
    if (across.some((r) => Math.abs(r - 1) < 0.08)) continue
    out.push({ threshold, factor: ranked[0] })
  }
  return out.sort((a, b) => (a.threshold < b.threshold ? -1 : 1))
}

/**
 * 期末流通股數（instant），依期末日分組、同一日同一份申報只留一筆。
 * 只取一個標籤：Outstanding 與 Issued 同一天申報但數值不同（差庫藏股），
 * 混在一起會產生「同日前後」的假觀測。
 */
function instantShares(ns: FactTags): Map<string, { filed: string; val: number }[]> {
  const pts =
    ns['CommonStockSharesOutstanding']?.units?.['shares'] ??
    ns['CommonStockSharesIssued']?.units?.['shares']
  const byEnd = new Map<string, Map<string, number>>()
  for (const p of pts ?? []) {
    if (spanDays(p) !== null || p.val <= 0) continue // 只要 instant
    const m = byEnd.get(p.end) ?? byEnd.set(p.end, new Map()).get(p.end)!
    if (!m.has(p.filed)) m.set(p.filed, p.val)
  }
  const out = new Map<string, { filed: string; val: number }[]>()
  for (const [end, m] of byEnd)
    out.set(
      end,
      [...m].map(([filed, val]) => ({ filed, val })).sort((a, b) => (a.filed < b.filed ? -1 : 1)),
    )
  return out
}

/** 同一期跨申報偵測分割。perShare=true 時值變小，方向相反。 */
function collectCrossFilingSplits(
  pts: FactPoint[] | undefined,
  fyeMonth: number,
  perShare: boolean,
  out: RawSplit[],
): void {
  if (!pts?.length) return
  const byPeriod = new Map<string, { filed: string; val: number }[]>()
  for (const p of pts) {
    const days = spanDays(p)
    // 股數用單季；每股值用單季或年度（EPS 年度值重編後也帶乾淨倍數）
    const ok = perShare ? days !== null && (days < 100 || (days > 300 && days < 400)) : days !== null && days > 80 && days < 100
    if (!ok || p.val <= 0) continue
    const { fy, q } = fiscalOf(p.end, fyeMonth)
    const dur = perShare && days! > 300 ? 'A' : 'Q'
    const k = `${dur}:FY${fy} Q${q}`
    ;(byPeriod.get(k) ?? byPeriod.set(k, []).get(k)!).push({ filed: p.filed, val: p.val })
  }
  for (const list of byPeriod.values()) {
    list.sort((a, b) => (a.filed < b.filed ? -1 : 1))
    for (let i = 0; i + 1 < list.length; i++) {
      const ratio = perShare ? list[i].val / list[i + 1].val : list[i + 1].val / list[i].val
      const f = detectSplit(ratio)
      if (f) out.push({ lo: list[i].filed, hi: list[i + 1].filed, factor: f, fromShares: !perShare })
    }
  }
}

/**
 * 同一期股數在界線前後都申報過的「後值/前值」清單（單季加權平均 ＋ 期末流通）。
 * 用來裁決一個候選分割是真的（比值＝倍數）還是假的（比值≈1，股數根本沒變）。
 */
function sharesAcross(
  pts: FactPoint[] | undefined,
  fyeMonth: number,
  instant: Map<string, { filed: string; val: number }[]>,
  threshold: string,
): number[] {
  const byPeriod = new Map<string, { filed: string; val: number }[]>()
  for (const p of pts ?? []) {
    const days = spanDays(p)
    if (days === null || days <= 80 || days >= 100 || p.val <= 0) continue
    const { fy, q } = fiscalOf(p.end, fyeMonth)
    const k = `Q:FY${fy} Q${q}`
    ;(byPeriod.get(k) ?? byPeriod.set(k, []).get(k)!).push({ filed: p.filed, val: p.val })
  }
  for (const [end, list] of instant) byPeriod.set(`I:${end}`, list)

  const out: number[] = []
  for (const list of byPeriod.values()) {
    const sorted = [...list].sort((a, b) => (a.filed < b.filed ? -1 : 1))
    for (let i = 0; i + 1 < sorted.length; i++)
      for (let j = i + 1; j < sorted.length; j++)
        if (sorted[i].filed < threshold && threshold <= sorted[j].filed)
          out.push(sorted[j].val / sorted[i].val)
  }
  return out
}

/** 依申報日把值正規化到最新基準：股數乘 factor、每股除 factor。filed < threshold 者套用。 */
function splitAdjust(val: number, filed: string, unit: string, splits: SplitEvent[]): number {
  let f = 1
  for (const s of splits) if (filed < s.threshold) f *= s.factor
  if (f === 1) return val
  return unit === 'shares' ? val * f : val / f
}

function toCell(p: FactPoint & { _origFiled?: string }, tag: string, estimated = false): CellValue {
  return {
    value: p.val,
    isEstimated: estimated,
    sourceTag: tag,
    accessionOrForm: p.form,
    filed: p.filed,
    origFiled: p._origFiled ?? p.filed,
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
  const fyEnds = inferFyEnds(ns, fyeMonth)

  /**
   * 年度模式（只有 FY 欄，沒有季度欄）**看資料本身有沒有季度長度的事實，不看命名空間**。
   *
   * 原本靠 `useIfrs` 判斷，等於假設「外國發行人＝用 ifrs-full 標籤」。實際上 ASML、
   * 豐田（TM）、阿里巴巴用的是 **us-gaap 標籤但只申報 20-F 年報**，於是被當成季度公司
   * 丟進單季還原：年度事實填進 cum[4]、cum[1..3] 全缺，補洞防呆（見
   * quarterly-period-reconstruction-traps）正確地拒絕生假數字 → **整張表一格都沒有**。
   *
   * 門檻用**比例**不用絕對筆數：只申報年報的公司仍會有零星的短期間事實（處分損益、
   * 期後事項、匯率揭露）。實測季度長度事實佔比 —— SHOP（真季報）19.5%、
   * ASML 0.3%、TM 0.0%，5% 落在中間很寬的空隙裡。
   */
  const annualMode =
    useIfrs ||
    (() => {
      let total = 0
      let quarterly = 0
      for (const tag of Object.values(ns)) {
        for (const points of Object.values(tag.units)) {
          for (const p of points) {
            if (!p.start) continue
            total++
            const d = (Date.parse(p.end) - Date.parse(p.start)) / 86_400_000
            if (d >= 45 && d <= 130) quarterly++
          }
        }
      }
      return total === 0 || quarterly / total < 0.05
    })()

  // 幣別：見 inferCurrency。**只取這一種幣別**，不做「這個科目沒有就退回另一種」的
  // fallback —— 同一張損益表混著歐元與美元的列，看起來完全正常但整份是錯的，
  // 比缺一列危險得多。
  const currency = inferCurrency(ns)
  const unitPrefs = (unit: string): string[] => {
    if (unit === 'USD') return [currency]
    if (unit === 'USD/shares') return [`${currency}/shares`]
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
    const negate = new Set(concept.negate_tags ?? [])
    const best = new Map<string, FactPoint & { _tag: string }>()
    for (const tag of tags) {
      const units = ns[tag]?.units
      let points = unitPrefs(concept.unit).map((u) => units?.[u]).find((p) => p?.length)
      if (!points) continue
      // 翻號要在 collect 之前做，這樣單季差分、Q4 推算、沿用前期全都跟著正確
      if (negate.has(tag)) points = points.map((p) => ({ ...p, val: -p.val }))
      for (const [key, p] of collect(points, flow, fyeMonth, fyEnds)) {
        if (!best.has(key)) best.set(key, { ...p, _tag: tag })
      }
    }

    /**
     * 這一列的「來源標籤」＝**實際供應最多期數**的那個標籤，不是清單裡第一個查得到的。
     * 舊寫法 `chosenTag ??= tag` 會被「存在但一格都用不上」的標籤佔住：XEL 的
     * RevenueFromContractWithCustomerExcludingAssessedTax 只有帶維度的零星事實，
     * 數字其實全部來自 RegulatedAndUnregulatedOperatingRevenue，標籤卻顯示前者 ——
     * 使用者拿這個去 EDGAR 對帳會對不到。逐格的 sourceTag 本來就是對的，這裡修的是列層級。
     *
     * 計票只算**真的出現在畫面上那幾欄**的格子，不算 `best` 的全歷史：Apple 2018 年
     * 以前用 SalesRevenueNet，全歷史計票會讓它贏過近年實際在用的
     * RevenueFromContractWithCustomerExcludingAssessedTax。
     */
    const values: Record<string, CellValue> = {}
    for (let fy = fromFy; fy <= toFy; fy++) {
      if (annualMode) {
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
        // Q1-Q4 直接快照/單季（期末流通股數是 BS 快照，Q4 年末快照存在）
        for (const n of [1, 2, 3, 4] as const) {
          const p = best.get(`Q:${fy}:${n}`)
          if (p) values[periodKey(fy, n)] = adj(p)
        }
        // 加權股數 Q4：用 10-K 年度加權平均近似（股數緩慢變動；非減法）。
        // EPS Q4 不在此推算——每股/加權平均項不可加減，改由後續 derive「淨利÷股數」算。
        if (concept.unit === 'shares' && !values[periodKey(fy, 4)]) {
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
      else if (qd(4) && cum[3] != null) {
        const q4 = qd(4)!
        // 10-K 一定會申報全年數字。該年度找不到「年度長度」的事實、卻有一筆來自 10-K 的
        // 單季事實，而且金額還大於前三季累計 → 幾乎一定是申報端把全年金額掛在 Q4 的
        // 期間上（L3Harris FY2024、FY2025 連兩年這樣：start 寫成 10/04 只有 90 天，
        // 值卻是全年 218.65 億）。當成單季相加會得到全年 380.8 億、Q4 比整年還大。
        const misTagged = q4.form?.startsWith('10-K') && Math.abs(q4.val) > Math.abs(cum[3]!)
        setCum(4, misTagged ? q4.val : cum[3]! + q4.val, q4)
      }

      // 最後一個有值的累計季（超過此季視為尚未申報，不輸出）
      let lastKnown = 0
      for (let q = 1; q <= 4; q++) if (cum[q] != null) lastKnown = q
      // 補內部缺口：向前補值（假設該季無活動）→ 一次性金額落到下一個可量測季
      const filled = [false, false, false, false, false]
      for (let q = 1; q <= lastKnown; q++) {
        if (cum[q] == null) {
          cum[q] = cum[q - 1]
          filled[q] = true
        }
      }

      for (let q = 1; q <= lastKnown; q++) {
        // 單季金額 = 兩端累計相減，任一端是補出來的就「算不出來」，不是「等於零」。
        // 舊版一律往前補 0 再相減：AAON 只在年報揭露研發費用（SEC 完全沒有季度事實），
        // 表上就變成 Q1–Q3 研發 0、Q4 一次 5,820 萬，而且 isEstimated 只標 Q4，
        // 前三個 0 看起來像申報值。實測 150 家有 92 家中招、1,890 格假 0。
        // 例外是 zero_if_absent 那組（庫藏股買回、舉債償債、股利）：那些科目缺申報
        // 確實通常代表當季沒有這筆活動，准補，但一律標成估算。
        const guessed = filled[q] || filled[q - 1]
        if (guessed && !concept.zero_if_absent) continue
        const anchor = src[q] ?? src[lastKnown]!
        values[periodKey(fy, q)] = {
          value: cum[q]! - cum[q - 1]!,
          isEstimated: guessed || (q === 4 && !qd(4)), // Q4 由全年推算 → 橘底
          sourceTag: filled[q]
            ? '缺申報視為 0'
            : filled[q - 1]
              ? '含前期未申報金額' // 前一季沒申報 → 那筆金額累加到這一季
              : anchor._tag,
          accessionOrForm: anchor.form,
          filed: anchor.filed,
          endDate: anchor.end,
        }
      }
    }
    for (const k of Object.keys(values)) allPeriods.add(k)

    const tagUse = new Map<string, number>()
    for (const c of Object.values(values)) {
      if (c.value != null && c.sourceTag) tagUse.set(c.sourceTag, (tagUse.get(c.sourceTag) ?? 0) + 1)
    }
    let chosenTag: string | null = null
    let chosenN = 0
    for (const [t, n] of tagUse) if (n > chosenN) { chosenN = n; chosenTag = t }

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

  const byId = new Map(lineItems.map((li) => [li.id, li]))

  // 期末流通股數補齊：部分公司（如 NVDA）的 us-gaap:CommonStockSharesOutstanding
  // 只在年度末申報 → 只有 Q4。dei:EntityCommonStockSharesOutstanding（申報封面股數）
  // 每季都有，用 fy/fp 對應補上缺的季（us-gaap 精確值優先，不覆蓋）。
  const soLi = byId.get('shares_outstanding')
  const deiPts = facts.facts.dei?.['EntityCommonStockSharesOutstanding']?.units?.['shares']
  if (soLi) {
    const best = new Map<string, { val: number; filed: string }>()
    for (const p of deiPts ?? []) {
      if (!p.fy || !p.fp) continue
      // 年度模式（20-F 外國發行人）也要跑這段。舊版整個 if 被 `!annualMode` 擋掉，
      // 於是 On Holding 這種「us-gaap/ifrs 股數標籤都帶維度、只剩 dei 封面股數」的
      // 20-F 公司期末股數整欄 n/a，估值分頁跟著整頁不出。
      let key: string
      if (annualMode) {
        if (p.fp !== 'FY') continue
        key = `FY${p.fy}`
      } else {
        const q = p.fp === 'FY' ? 4 : Number(String(p.fp).replace('Q', ''))
        if (!(q >= 1 && q <= 4)) continue
        key = periodKey(p.fy, q)
      }
      const prev = best.get(key)
      if (!prev || p.filed > prev.filed) best.set(key, { val: p.val, filed: p.filed })
    }
    for (const [key, { val, filed }] of best) {
      if (soLi.values[key]?.value != null) continue // us-gaap 已有（較精確）
      if (!allPeriods.has(key)) continue
      soLi.values[key] = {
        value: splitAdjust(val, filed, 'shares', splits),
        isEstimated: false,
        sourceTag: 'dei:EntityCommonStockSharesOutstanding',
        filed,
      }
    }
    /**
     * 多股別公司：封面股數帶了 `StatementClassOfStockAxis` 維度 → companyfacts 收不到，
     * 連加權平均股數也常常一起消失。從離線預算好的 `config/class_shares.json` 補
     * （產生方式與各家的合併依據見 `tools/class_shares.py`）。
     *
     * 對期方式：檔案的 key 是**申報封面的股數截止日**，通常落在季末後 2–6 週
     * （波克夏 2026-06-30 那季寫的是 2026-07-29）。所以取「期末日之後 75 天內最早」
     * 的那一筆；Visa 之類把當量總數掛在季末當日的，同一條規則也涵蓋。
     * 找不到就留 n/a —— 這個檔案不會自動更新，最新一季在重跑工具前本來就該是空的。
     */
    const cs = (await loadClassShares()).companies[String(Number(ref.cik10))]
    if (cs) {
      const endOf = new Map<string, string>()
      for (const li of lineItems) {
        for (const [p, c] of Object.entries(li.values)) {
          if (c.endDate && !endOf.has(p)) endOf.set(p, c.endDate)
        }
      }

      /**
       * 加權平均股數也可能整列是錯的，而且錯得很難發現：Shift4 申報時把 Class C 的
       * 133 萬股**同時也標成無維度**，companyfacts 就把它當成全公司的加權平均
       * （實際 Class A 是 6,673 萬）。這種錯與每股盈餘自洽（因為每股盈餘也是按股別
       * 申報、無維度那筆同樣是 Class C 的），任何跨科目檢查都抓不到。
       * 預算檔裡的值是從申報原文逐股別加起來的，直接覆蓋。
       */
      for (const [kind, id] of [['basic', 'shares_basic'], ['diluted', 'shares_diluted']] as const) {
        const byDate = cs.wavg?.[kind]
        const li = byId.get(id)
        if (!byDate || !li) continue
        const ds = Object.keys(byDate).sort()
        for (const p of allPeriods) {
          const end = endOf.get(p)
          if (!end) continue
          const t = Date.parse(end)
          const hit = ds.find((d) => Math.abs(Date.parse(d) - t) <= 5 * 86_400_000)
          if (!hit) continue
          li.values[p] = {
            ...li.values[p],
            value: byDate[hit],
            isEstimated: true,
            sourceTag: '申報各股別加權平均股數合計',
            endDate: hit,
          }
        }
      }

      const dates = Object.keys(cs.shares).sort()
      for (const p of allPeriods) {
        if (soLi.values[p]?.value != null) continue
        const end = endOf.get(p)
        if (!end) continue
        const t = Date.parse(end)
        const hit = dates.find((d) => {
          const gap = (Date.parse(d) - t) / 86_400_000
          return gap >= 0 && gap <= 75
        })
        if (!hit) continue
        soLi.values[p] = {
          value: cs.shares[hit],
          isEstimated: true,
          sourceTag: `申報封面各股別股數（${cs.basis}）`,
          endDate: hit,
        }
      }
    }

    // 仍缺的期（us-gaap 年度末、dei、預算檔都沒有，如 NVDA 2021Q1/Q2）→ 退回加權平均
    // 股數近似（股數變化緩，兩者接近；避免整欄估值因股數缺而連鎖 n/a）。
    //
    // ⚠️ 這段**不能**包在「dei 有資料」的條件裡。最需要它的正是連 dei 都沒有的公司：
    // 多股別（ABNB、AMH、APPF 等）申報封面股數會按股別拆、帶了維度，而 companyfacts
    // 只收無維度事實 → dei 整個標籤消失。
    //
    // 基本、稀釋要**逐期**各自看有沒有值，不能寫成 `basic ?? diluted`：那個 `??` 判的是
    // 「有沒有這一列」，而列永遠都在（只是整欄 n/a），於是 diluted 永遠輪不到。
    // Tyson Foods 就是這樣 —— 它的基本股數帶了 A/B 股維度收不到，稀釋股數
    // WeightedAverageNumberOfDilutedSharesOutstanding 是無維度的 3.57 億、26 筆全在，
    // 卻因為這個 `??` 一格都沒用上，期末股數整欄 n/a。
    const wBasic = byId.get('shares_basic')
    const wDil = byId.get('shares_diluted')
    const wavgAt = (p: string) => wBasic?.values[p]?.value ?? wDil?.values[p]?.value ?? null
    if (wBasic || wDil) {
      for (const p of allPeriods) {
        if (soLi.values[p]?.value != null) continue
        const w = wavgAt(p)
        if (w != null) {
          soLi.values[p] = { value: w, isEstimated: true, sourceTag: '近似：加權平均股數' }
        }
      }

    }
  }

  /**
   * 股數的申報單位防呆。三列（期末／基本／稀釋）一起做。
   *
   * 申報端寫錯 scale 比想像中常見，而且畫面上完全看不出來：
   *   麥當勞  `WeightedAverageNumberOfSharesOutstandingBasic` 申報成 **709.1 股**
   *           （實際 7.091 億，漏寫百萬）→ 每股盈餘、股數稀釋率整列變垃圾
   *   Repligen／Bruker  2022–2024 那幾期以「千股」申報（55,353 / 150）
   *   Freedom Holding   反過來，加權平均股數多寫 1000 倍（595 億股）
   *   Viking Holdings   `NumberOfSharesOutstanding` 寫成 442,721,700,000（實際 4.43 億）
   * 實測羅素 1000 有 34 家中招。
   *
   * 錨用**公司自己的除法**：淨利 ÷ 每股盈餘 = 它算每股盈餘時用的股數。
   * 沒有每股盈餘可用時退回同期加權平均股數（IFRS 早期、虧損轉盈那幾期）。
   *
   * 差距剛好是 1000 的次方（±20%）→ 判定為單位寫錯，按比例還原並標成估算；
   * 其他對不上的一律改 n/a。不猜、不硬湊 —— 這種「看起來正常的假數字」比留白危險。
   */
  {
    const soLi2 = byId.get('shares_outstanding')
    const wBasic = byId.get('shares_basic')
    const wDil = byId.get('shares_diluted')
    const ni = byId.get('net_income')
    const eps = byId.get('eps_basic')
    // 每股盈餘只申報到「分」。EPS 0.01 的那一期真值可能是 0.005～0.014，倒推出來的
    // 股數會差到兩倍，拿這種錨去砍好資料反而製造洞。只在 |EPS| ≥ 0.10 時採用
    // （此時四捨五入誤差 ≤5%），否則退回加權平均股數。
    const impliedAt = (p: string): number | null => {
      const n = ni?.values[p]?.value
      const c = eps?.values[p]
      const e = c?.value
      // **只認公司自己申報的每股盈餘**。我們自己推算的那格是 net_income ÷ shares_basic，
      // 拿它回頭當股數的錨是循環論證：麥當勞 FY2022 Q4 的股數本身少寫了百萬，
      // 推算出的每股盈餘就變成 2,583,842 元，倒推的股數接近 0，反而把正確的封面股數
      // （7.31 億）「還原」成 731.5 股。
      if (n == null || e == null || Math.abs(e) < 0.1) return null
      if (c?.sourceTag?.startsWith('推算')) return null
      const imp = Math.abs(n / e)
      return imp >= 1000 ? imp : null
    }
    const anchorAt = (p: string): number | null => {
      const imp = impliedAt(p)
      if (imp != null) return imp
      const w = wBasic?.values[p]?.value ?? wDil?.values[p]?.value
      return w != null && w !== 0 ? Math.abs(w) : null
    }
    // **順序很重要**：先修加權平均，再修期末股數。期末股數缺值時會退回用加權平均補、
    // 也用加權平均當錨；加權平均自己是錯的話，錯的錨會把錯的值驗證成「沒問題」
    // （Tempus AI 整列以千股申報，18.9 萬 vs 實際 1.789 億，實測就是這樣漏掉的）。
    const sorted = [...allPeriods].sort()
    for (const li of [wBasic, wDil, soLi2]) {
      if (!li) continue
      const useImpliedOnly = li === wBasic || li === wDil // 自己不能當自己的錨

      // 股數 0 一律當缺值。公司不可能有 0 股流通在外 —— SiriusXM FY2023 Q4 的
      // `CommonStockSharesOutstanding` 就是 0，而 0 會被後面的「沿用前期」一路帶到
      // 最新幾季，表上出現 0 股、市值 0。
      for (const p of allPeriods) if (li.values[p]?.value === 0) delete li.values[p]

      // 第一輪：有錨的期各自判斷要不要還原、還原幾個數量級
      const pow = new Map<string, number>()
      const drop = new Set<string>()
      for (const p of sorted) {
        const v = li.values[p]?.value
        if (v == null || v === 0) continue
        // 錨分兩級。**丟不丟得看錨可不可信**，不然一定會砍錯邊：
        //   強錨＝公司自報的每股盈餘倒推（淨利÷EPS，公司自己做的除法）
        //   弱錨＝只是同期的加權平均股數
        // Dexcom FY2022 期末股數 47.09 億（實際 3.9 億，市值高估 12 倍）、
        // Crane NXT 5.67 億（實際 5,670 萬）—— 兩家都有自報 EPS，強錨說得準，該砍。
        // 反過來 Shift4 沒有無維度的每股盈餘（它按股別申報），只剩弱錨，而它的
        // 加權平均股數本身就是錯的（申報時把 Class C 的 133 萬也標成無維度，
        // 實際 Class A 是 6,673 萬）→ 拿這種錨去砍正確的封面股數 7,900 萬就是
        // 用錯的驗對的。所以弱錨只在差 100 倍以上才動手。
        const strong = impliedAt(p)
        const a = strong ?? (useImpliedOnly ? null : anchorAt(p))
        if (a == null) continue
        const r = Math.abs(v) / a
        if (r <= 10 && r >= 0.1) {
          pow.set(p, 0)
          continue
        }
        const e = Math.round(Math.log10(r) / 3) * 3 // 最近的 1000 次方
        if (e !== 0 && Math.abs(v / 10 ** e / a - 1) <= 0.2) pow.set(p, e)
        else if (strong != null || r > 100 || r < 0.01) drop.add(p)
      }

      // 先把有錨的期改好，第二輪才有正確的鄰期可以比
      for (const p of sorted) {
        const cell = li.values[p]
        const e = pow.get(p)
        if (!cell || cell.value == null || !e) continue
        li.values[p] = { ...cell, value: cell.value / 10 ** e, isEstimated: true, sourceTag: '申報單位還原' }
      }

      /**
       * 第二輪：沒有錨的期（虧損、每股盈餘太小、或那格每股盈餘是我們自己推算的）
       * 改用**數值連續性**判定 —— 拿最近一個已確定的期當基準。
       *
       * 不能改用「繼承鄰期的倍率」：申報端換單位慣例的那一季，前後剛好一邊舊一邊新、
       * 距離一樣近，怎麼挑都會錯一邊 —— 麥當勞 FY2022 Q4 屬於新慣例（百萬）、
       * Repligen FY2024 Q4 屬於舊慣例（千股），兩家的交界期方向相反。
       * 股數是慢變量，「哪個倍率讓它接近鄰期」是可靠得多的訊號。
       */
      const settled = sorted.filter((p) => pow.has(p) && li.values[p]?.value != null)
      for (const p of sorted) {
        const v = li.values[p]?.value
        if (v == null || v === 0 || pow.has(p) || drop.has(p)) continue
        if (!settled.length) continue
        const i = sorted.indexOf(p)
        let best = settled[0]
        for (const q of settled) {
          if (Math.abs(sorted.indexOf(q) - i) < Math.abs(sorted.indexOf(best) - i)) best = q
        }
        const ref = Math.abs(li.values[best]!.value!)
        const r = Math.abs(v) / ref
        if (r <= 10 && r >= 0.1) continue
        const e = Math.round(Math.log10(r) / 3) * 3
        if (e !== 0 && Math.abs(v / 10 ** e / ref - 1) <= 0.2) pow.set(p, e)
        else if (r > 100 || r < 0.01) drop.add(p) // 同第一輪：不是乾淨的 1000 次方就別動
      }

      for (const p of sorted) {
        const cell = li.values[p]
        if (drop.has(p)) {
          delete li.values[p]
          continue
        }
        if (!cell || cell.value == null || settled.includes(p)) continue
        const e = pow.get(p)
        if (e) {
          li.values[p] = { ...cell, value: cell.value / 10 ** e, isEstimated: true, sourceTag: '申報單位還原' }
        }
      }

      /**
       * 收尾：列內離群值一律丟掉，只丟不改。股數是慢變量，同一列不可能有一格跟
       * 基準差 100 倍。
       *
       * 前面每一道都靠「跟同期的每股盈餘對得上」，但有些公司**整組一起錯**：
       * SiriusXM 2022–2023 那幾期股數 2.28 萬、每股盈餘 13,824 元，兩者相乘剛好等於
       * 淨利，自洽到任何跨科目檢查都抓不到。只能靠同一列的其他期間看出來。
       *
       * 基準取**最近四期**的中位數 —— 不能用整列的中位數，也不能用錨值的中位數。
       * SiriusXM 併購前後是兩個不同實體，18 期裡有 9 期是併購前基礎，兩種中位數
       * 都會被前半段拉走，反過來把後半段正確的 3.37 億全部刪掉。最近幾期是使用者
       * 真正在看的、也是前面被每股盈餘驗證過的，拿它當基準在構造上就不可能誤刪近期資料。
       * 併購前那段換算不回來（不是單位問題，是實體不同），n/a 才誠實。
       */
      const recent = sorted
        .map((p) => li.values[p]?.value)
        .filter((v): v is number => v != null && v !== 0)
        .slice(-4)
        .map(Math.abs)
        .sort((x, y) => x - y)
      if (recent.length >= 3) {
        const med = recent[Math.floor(recent.length / 2)]
        for (const p of sorted) {
          const v = li.values[p]?.value
          if (v == null || v === 0) continue
          const r = Math.abs(v) / med
          if (r > 100 || r < 0.01) delete li.values[p]
        }
      }
    }

    /**
     * 每股盈餘的同一種病：SiriusXM 申報 13,824 元、Loar 申報 −36,860 元。
     * 錨用「淨利 ÷ 加權平均股數」（股數這時已經修好了）。差 10 倍以上就不採用，
     * 剛好是 1000 的次方才還原數量級 —— 其餘留白。
     * 容差留得寬是因為每股盈餘的分子本來就不完全等於淨利（歸屬母公司、特別股股利、
     * 兩級法），差個一兩成很正常，那不是錯。
     */
    const epsRows = [byId.get('eps_basic'), byId.get('eps_diluted')]
    for (const li of epsRows) {
      if (!li) continue
      for (const p of allPeriods) {
        const cell = li.values[p]
        const v = cell?.value
        const n = ni?.values[p]?.value
        const sh = wBasic?.values[p]?.value ?? wDil?.values[p]?.value
        if (v == null || v === 0 || n == null || sh == null || sh === 0) continue
        if (cell?.sourceTag?.startsWith('推算')) continue // 本來就是這樣算出來的
        const a = Math.abs(n / sh)
        if (a === 0) continue
        const r = Math.abs(v) / a
        if (r <= 10 && r >= 0.1) continue
        const e = Math.round(Math.log10(r) / 3) * 3
        if (e !== 0 && Math.abs(Math.abs(v) / 10 ** e / a - 1) <= 0.3) {
          li.values[p] = { ...cell, value: v / 10 ** e, isEstimated: true, sourceTag: '申報單位還原' }
        } else {
          delete li.values[p]
        }
      }
    }
  }

  // zero_if_absent：該科目缺申報通常代表公司沒有此項目 = 0（如無配息、無庫藏股、
  // 無一年內到期債務）。以「資產負債表有申報」（total_assets 有值）為錨補 0，
  // 避免財務結構/股東回饋等指標間歇或整條 n/a。只補公司確實有申報財報的期。
  const anchor = byId.get('total_assets')
  for (const concept of map.concepts) {
    if (!concept.zero_if_absent) continue
    const li = byId.get(concept.id)
    if (!li || !anchor) continue
    for (const p of allPeriods) {
      if (li.values[p]?.value != null) continue
      if (anchor.values[p]?.value == null) continue // 該期沒申報財報 → 不捏造
      li.values[p] = { value: 0, isEstimated: true, sourceTag: '缺申報視為 0' }
    }
  }

  // 資產負債表科目 + 加權平均股數沿用前期：部分公司（如 Apple 租賃資產）只在年報申報，
  // 10-Q 不報 → 季中缺。BS/股數為緩慢變動，沿用最近一期為合理估計。
  //
  // 最多沿用 CARRY_MAX 季，且尾端也補（不只補中間的洞）：
  //   - 尾端要補的理由和中間一樣。只補中間會讓「只在年報揭露租賃」的公司，
  //     最新那一兩欄變 n/a，流動比率／速動比率跟著整欄沒有 —— 而那正是使用者最看的一欄。
  //     實測 150 家有 41 家使用權資產卡在這個情況。
  //   - 但要有上限。沒有上限時，某個科目停報後會一路沿用到序列末端
  //     （實測 ASTS 存貨、BAM 應付帳款被拖了 7 季），拿兩年前的數字當本季就不合理了。
  // 放在 derive 之前 → EPS 可用補好的股數推算。
  const CARRY_MAX = 3
  {
    const sorted = [...allPeriods].sort()
    for (const concept of map.concepts) {
      if (concept.statement !== 'BS' && concept.unit !== 'shares') continue // EPS 波動大不沿用
      const li = byId.get(concept.id)
      if (!li) continue
      const knownIdx = sorted
        .map((p, i) => (li.values[p]?.value != null ? i : -1))
        .filter((i) => i >= 0)
      if (knownIdx.length < 2) continue
      let prev: CellValue | null = null
      let lag = 0
      const end = Math.min(sorted.length - 1, knownIdx[knownIdx.length - 1] + CARRY_MAX)
      for (let i = knownIdx[0]; i <= end; i++) {
        const p = sorted[i]
        if (li.values[p]?.value != null) {
          prev = li.values[p]
          lag = 0
          continue
        }
        if (++lag > CARRY_MAX) prev = null
        if (prev != null) {
          li.values[p] = {
            value: prev.value,
            isEstimated: true,
            sourceTag: '沿用前期（該季未申報）',
            endDate: prev.endDate,
          }
        }
      }
    }
  }

  // 推算 fallback：抓不到直接標籤的科目（如 AMZN 無「負債總計」標籤），
  // 用其他科目算（total_liabilities = total_assets − equity）。只補缺的期，不覆蓋已有值。
  for (const concept of map.concepts) {
    if (!concept.derive) continue
    const li = byId.get(concept.id)
    if (!li) continue
    const m = concept.derive.match(/^(\w+)\s*([+\-*/])\s*(\w+)$/)
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
      if ((op === '/' || op === '*') && bv === 0) continue
      const value =
        op === '-' ? av - bv : op === '+' ? av + bv : op === '*' ? av * bv : av / bv
      li.values[p] = {
        value,
        isEstimated: true, // 推算值（非直接申報）
        sourceTag: `推算：${concept.derive}`,
        endDate: a.values[p]?.endDate,
      }
    }
  }

  // 上市／SPAC 借殼前偵測：股數序列早期出現一次「非分割」的大跳增（借殼或 IPO 增資），
  // 之前的期屬私有公司股數基礎，與上市後不可比（EPS 等會嚴重失真）→ 清為 n/a 並標註。
  let preIpoBefore: string | undefined
  if (!annualMode) {
    const sharesLi = byId.get('shares_basic') ?? byId.get('shares_diluted')
    const seq = [...allPeriods]
      .sort()
      .map((p) => ({ p, v: sharesLi?.values[p]?.value ?? null }))
      .filter((x) => x.v != null) as { p: string; v: number }[]
    const latest = seq.length ? seq[seq.length - 1].v : 0
    for (let i = 1; i < seq.length && i <= 8; i++) {
      // 跳增 >2.5 倍，且跳增前股數 < 最新的 40%（確保是新創上市，不是成熟公司的一般增發）
      if (seq[i].v >= 2.5 * seq[i - 1].v && seq[i - 1].v < 0.4 * latest) {
        preIpoBefore = seq[i].p
        break
      }
    }
    if (preIpoBefore) {
      // 直接移除上市前的期（連欄位一起拿掉，不只是 n/a）→ 下載檔不含這些季
      for (const p of [...allPeriods]) {
        if (p < preIpoBefore) {
          allPeriods.delete(p)
          for (const li of lineItems) delete li.values[p]
        }
      }
    }
  }

  if (allPeriods.size === 0) {
    console.warn('[financials] 產不出任何期間', ref.ticker, ref.cik10, {
      fyeMonth, annualMode, fromFy, toFy, nsTags: Object.keys(ns).length, useIfrs,
    })
  }
  const periods = [...allPeriods].sort() // FY2023 Q1 < FY2023 Q2 < ...（年度模式 FY2023 < FY2024）字典序即正確

  // 標出「該產業本來就沒有」的科目，讓缺值能寫「—」而不是 n/a。
  // 放在最後：期末股數等後處理補值跑完才判，否則會把補得到的科目誤標成不適用。
  // 這裡只加旗標、不動任何數值。
  const na = await notApplicableFor(ref.sic)
  if (na.size) {
    for (const li of lineItems) {
      if (na.has(li.id)) li.applicable = false
    }
  }

  return {
    company: facts.entityName || ref.name,
    cik: ref.cik10,
    ticker: ref.ticker,
    mapVersion: map.version,
    periodicity: annualMode ? 'annual' : 'quarterly',
    currency,
    periods,
    lineItems,
    derived: map.derived,
    preIpoBefore,
  }
}

/**
 * 申報幣別＝事實筆數最多的那個幣別 unit key。
 *
 * ⚠️ 不能「有 USD 就用 USD」。20-F 的 USD 便利換算只是附錄，涵蓋率很低：
 * TSM 的 USD 只有 TWD 的 22%、BABA 的 USD 只有 CNY 的 20%、SAP 的 USD 只有
 * 2017 一年（EUR 有 2021–2025）。舊寫法 `if (count.has('USD')) return 'USD'`
 * 讓 SAP／UL 整張表**一格都沒有、也沒有任何訊息**。
 *
 * ⚠️ 也不能只對 IFRS filer 做這件事。TM（日圓）、BABA（人民幣）、ASML（歐元）
 * 都是用 **us-gaap 標籤但以本國貨幣申報**，原本 us-gaap 一律寫死 USD，同樣整張空白。
 *
 * 所以：一律取申報幣別、一律只用這一種幣別（見 unitPrefs），前端與 Excel 都標出幣別。
 * 純美國公司只有 USD，行為不變。
 */
function inferCurrency(ns: FactTags): string {
  const count = new Map<string, number>()
  for (const tag of Object.values(ns)) {
    for (const [u, points] of Object.entries(tag.units)) {
      if (/^[A-Z]{3}$/.test(u)) count.set(u, (count.get(u) ?? 0) + points.length)
    }
  }
  let bestU = 'USD'
  let bestN = -1
  for (const [u, n] of count) if (n > bestN) { bestN = n; bestU = u }
  return bestU
}
