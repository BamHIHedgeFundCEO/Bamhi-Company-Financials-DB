import { secFetchJson, secFetchText } from './secFetch'
import { cached } from './blobCache'
import { loadMap } from './financials'
import type { CompanyRef } from './cik'

/**
 * 資料流 C：XBRL instance → 分部（segment）資料。
 *
 * 為什麼不能沿用 companyfacts：那支 API **不含維度**（dimension）。實測 AAPL
 * 的 25,046 筆 fact 只帶 accn/end/filed/form/fp/frame/fy/start/val，沒有任何
 * 軸/成員欄位。所以 NVDA 的 Data Center 對 Gaming、AAPL 的服務對硬體，只能
 * 回頭剖析申報的 XBRL instance 檔。
 *
 * 三個實測得來的關鍵設計（19 家跨產業取樣，見 tools/segment.py）：
 *
 * 1. **軸靠標準分類法、成員一律正規化**。19/19 家的軸都是 us-gaap/srt/ifrs-full
 *    標準軸，可以當錨點；但成員幾乎都是自訂（NVDA 9/9、TSLA 15/15、JNJ 57/58），
 *    而且會跨期改名（NVDA 的 ComputeAndNetworkingMember → …SegmentMember），
 *    所以砍尾綴正規化，不做窮舉對照表。
 *
 * 2. **一定要過濾 ConsolidationItemsAxis**。分部數字和「公司未分攤/跨部門沖銷」
 *    共用同一組科目，不濾會把調節項當分部。
 *
 * 3. **成員有父子關係，不能盲目加總**。AAPL 的 product 本身就含 iPhone/Mac/iPad，
 *    全加會得 723B 但實際總營收只有 416B（多算 74%）。用同一份 instance 裡
 *    「無維度 context」的合併總額反推剔除父層 —— 不必額外打 API。
 */

/** 分部軸設定（config/segment_axes.json） */
interface AxisDef {
  axis: string
  role: 'segment' | 'product' | 'geography'
  zh: string
  en: string
  priority: number
  taxonomy: 'us-gaap' | 'ifrs'
}

interface SegmentAxesConfig {
  version: string
  axes: AxisDef[]
  consolidation: {
    axes: string[]
    include_members: string[]
    exclude_patterns: string[]
  }
  member_normalize: {
    strip_suffixes: string[]
    lowercase: boolean
    strip_non_alnum: boolean
  }
  member_aliases: { map: Record<string, string[]> }
  hierarchy: { strategy: string; tolerance_pct: number }
  concepts: {
    include: string[]
    /**
     * ASC 280 要求必須調節到合併數的科目（營收、資產）。這些對不上就是真異常，
     * 一律留 true/false。其餘科目允許自訂衡量方式，見 SegmentCell.verified。
     */
    must_reconcile?: string[]
    derived: { id: string; zh: string; en: string; formula: string; format: string }[]
  }
  /** `en` 只給少數自動拆字會拆壞的成員補英文，查無時仍走駝峰拆字 */
  member_labels: { map: Record<string, string>; en?: Record<string, string> }
}

export interface SegmentCell {
  value: number
  /**
   * 「分部加總 = 合併總額」的校驗結果。三態，`null` 是關鍵：
   *
   * - `true`  對得上
   * - `false` 對不上 —— 值得懷疑，UI 標色
   * - `null`  **無法校驗**，不是校驗失敗
   *
   * ASC 280 允許公司用自己的分部利潤定義，也允許只揭露部分分部的費用。
   * ORCL 三個分部的營業利益加起來 29.1B、合併只有 13.1B（分部數不含股酬與攤銷）；
   * PFE 兩個分部裡只有一個揭露營業成本。這些**永遠**不可能對上總額，那是揭露規則
   * 使然，不是數字有問題。判定方式見 reconcileConcept 末段：一個科目若在所有可比
   * 期間都對不上，就是結構性的，標成 null；只有部分期間對不上才是真的異常。
   */
  verified: boolean | null
  /**
   * 這個成員是上層匯總（如 Apple 的 Product 涵蓋 iPhone/Mac/iPad/穿戴）。
   * 上層數字照樣呈現 —— 它是公司真的揭露的、而且往往最有價值（Apple 的硬體 vs
   * 服務毛利率就只有在這一層才算得出來）—— 但不能列入合計，否則重複計算。
   */
  isParent: boolean
}

export interface SegmentMemberRow {
  key: string
  zh: string
  en: string
  /** period → concept → 值 */
  values: Record<string, Record<string, SegmentCell>>
}

export interface SegmentAxisBlock {
  axis: string
  role: string
  zh: string
  en: string
  /** 這個軸實際揭露了哪些科目（ASC 280：CODM 看什麼才揭露什麼，各家不同） */
  concepts: string[]
  members: SegmentMemberRow[]
}

/**
 * 期間種類。`A` = 年度、`Q` = 單一季度。
 *
 * 這個欄位不是裝飾用的 —— 它是正確性的一部分。10-Q 的 instance 裡，同一個期末日
 * **同時**存在單季（90 天）與累計（YTD，177–273 天）兩組事實，實測 AAPL / KO /
 * TSLA / MSFT 皆如此。只用期末日當 key 的話兩者會互相覆蓋，得到的數字是隨機的
 * 單季或累計，例如 AAPL 2025-06-28 的 Product 可能是 666 億（單季）也可能是
 * 2,333 億（九個月累計）。所以期間 key 一律帶種類後綴。
 */
export type PeriodKind = 'A' | 'Q'

/** 期間 key：`2025-09-27#A`。日期與種類都在裡面，跨粒度不會撞。 */
export function periodKey(end: string, kind: PeriodKind): string {
  return `${end}#${kind}`
}

export function splitPeriod(key: string): { end: string; kind: PeriodKind } {
  const i = key.lastIndexOf('#')
  return i < 0
    ? { end: key, kind: 'A' }
    : { end: key.slice(0, i), kind: key.slice(i + 1) as PeriodKind }
}

export interface SegmentsResult {
  company: string
  cik: string
  ticker: string
  configVersion: string
  /** 已排序：年度欄在前、季度欄在後，各自依日期遞增 */
  periods: string[]
  axes: SegmentAxisBlock[]
  /** 抓不到 instance、對不上總額等情形，據實回報而不是安靜吞掉 */
  warnings: string[]
}

let cachedCfg: SegmentAxesConfig | null = null
export async function loadSegmentAxes(): Promise<SegmentAxesConfig> {
  if (cachedCfg) return cachedCfg
  const raw = await useStorage('assets:config').getItem('segment_axes.json')
  cachedCfg = (typeof raw === 'string' ? JSON.parse(raw) : raw) as SegmentAxesConfig
  if (!cachedCfg?.axes) throw new Error('segment_axes.json 載入失敗')
  return cachedCfg
}

// ── instance 檔定位 ────────────────────────────────────────────────────────

interface DirIndex {
  directory: { item: { name: string }[] }
}

/**
 * 找出這份申報的 XBRL instance 檔。
 *
 * ⚠️ 不要用 primaryDocument 的 `.htm` → `_htm.xml` 字串代換。外國發行人的
 * 20-F/6-K 常常對不上直接 404（TSM、ASML 實測），一定要走 index.json。
 */
export async function instanceUrl(cik: number, accession: string): Promise<string | null> {
  const a = accession.replace(/-/g, '')
  const base = `https://www.sec.gov/Archives/edgar/data/${cik}/${a}`
  const idx = await secFetchJson<DirIndex>(`${base}/index.json`)
  const names = (idx.directory?.item ?? []).map((i) => i.name)

  // 1) inline XBRL 的萃取產出
  const inline = names.find((n) => n.endsWith('_htm.xml'))
  if (inline) return `${base}/${inline}`

  // 2) 傳統 instance：xxx-20240930.xml（排除 linkbase 與 schema）
  const legacy = names.find(
    (n) => /-\d{8}\.xml$/.test(n) && !/(_cal|_def|_lab|_pre|_ref)\.xml$/.test(n),
  )
  return legacy ? `${base}/${legacy}` : null
}

// ── instance 剖析（零依賴） ────────────────────────────────────────────────

interface Ctx {
  dims: { axis: string; member: string }[]
  start: string
  end: string
}

interface RawFact {
  tag: string
  ctx: string
  val: number
}

const RE_CONTEXT = /<(?:[\w.-]+:)?context\s[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/(?:[\w.-]+:)?context>/g
const RE_MEMBER = /<(?:[\w.-]+:)?explicitMember\s+dimension="([^"]+)"[^>]*>([^<]+)</g
const RE_START = /<(?:[\w.-]+:)?startDate>([^<]+)</
const RE_END = /<(?:[\w.-]+:)?endDate>([^<]+)</
/**
 * 只掃「帶 unitRef 的事實」= 數值型。
 * 這一條件很重要：TextBlock 類的事實內嵌整段跳脫過的 HTML，動輒數 MB，
 * 掃進來純粹浪費 CPU 與記憶體（JPM 的 10-K instance 有 14MB）。
 */
const RE_FACT = /<([\w.-]+:[\w.-]+)\s+([^>]*unitRef="[^"]*"[^>]*)>([^<]*)</g

export function parseInstance(xml: string): { contexts: Map<string, Ctx>; facts: RawFact[] } {
  const contexts = new Map<string, Ctx>()
  for (const m of xml.matchAll(RE_CONTEXT)) {
    const [, id, body] = m
    const dims = [...body.matchAll(RE_MEMBER)].map((d) => ({
      axis: d[1].trim(),
      member: d[2].trim(),
    }))
    contexts.set(id, {
      dims,
      start: RE_START.exec(body)?.[1] ?? '',
      end: RE_END.exec(body)?.[1] ?? '',
    })
  }

  const facts: RawFact[] = []
  for (const m of xml.matchAll(RE_FACT)) {
    const [, tag, attrs, text] = m
    const ctx = /contextRef="([^"]+)"/.exec(attrs)?.[1]
    if (!ctx) continue
    const val = Number(text)
    if (!Number.isFinite(val)) continue
    facts.push({ tag, ctx, val })
  }
  return { contexts, facts }
}

// ── 成員正規化與顯示名 ────────────────────────────────────────────────────

/**
 * 成員正規化：砍尾綴 → 小寫 → 去非英數。
 * 這一步就解掉了跨期改名：NVDA 的 ComputeAndNetworkingMember 與
 * ComputeAndNetworkingSegmentMember 都會變成 "computeandnetworking"。
 */
export function normalizeMember(qname: string, cfg: SegmentAxesConfig): string {
  let bare = qname.includes(':') ? qname.slice(qname.indexOf(':') + 1) : qname
  for (const suf of cfg.member_normalize.strip_suffixes) {
    if (bare.endsWith(suf) && bare.length > suf.length) {
      bare = bare.slice(0, -suf.length)
      break
    }
  }
  if (cfg.member_normalize.lowercase) bare = bare.toLowerCase()
  if (cfg.member_normalize.strip_non_alnum) bare = bare.replace(/[^a-z0-9]/g, '')
  return bare
}

/**
 * 駝峰拆字：ComputeAndNetworking → Compute And Networking（查無中文時的英文備援）。
 *
 * 拆完要把落單的單一字母併回下一個字，否則 IPhoneMember 會變成 "I Phone"、
 * OEMAndOther 會被切壞。注意 aapl:WearablesHomeandAccessoriesMember、
 * jpm:AssetandWealthManagementMember 這種「Homeand」「Assetand」是發行人自己
 * 在標籤裡少了大寫，屬於原始資料，不在這裡硬修。
 */
function humanize(qname: string): string {
  const bare = qname.includes(':') ? qname.slice(qname.indexOf(':') + 1) : qname
  const words = bare
    .replace(/(Segments?)?Member$/, '')
    .replace(/([a-z0-9])([A-Z])/g, '$1 $2')
    .replace(/([A-Z]+)([A-Z][a-z])/g, '$1 $2')
    .trim()
    .split(/\s+/)
    .filter(Boolean)

  const out: string[] = []
  for (const w of words) {
    if (out.length && out[out.length - 1].length === 1) out[out.length - 1] += w
    else out.push(w)
  }
  return out.join(' ')
}

// ── 分部抽取 ──────────────────────────────────────────────────────────────

interface ExtractedFilingSegments {
  /** period（期末日）→ axis → memberKey → conceptId → 值 */
  data: Record<string, Record<string, Record<string, Record<string, number>>>>
  /** period → conceptId → 合併總額（無維度 context） */
  totals: Record<string, Record<string, number>>
  /**
   * 調節項：只掛 ConsolidationItemsAxis、**沒有**任何分部軸維度的列
   * （公司未分攤、分部間沖銷…）。period → memberKey → conceptId → 值。
   *
   * 這些數字是合併總額的一部分，但申報沒說它屬於哪一個分部軸，所以先擺這裡，
   * 由 reconcileConcept 用「加進去才對得上總額」反推該掛到哪個軸。
   */
  residual: Record<string, Record<string, Record<string, number>>>
  labels: Record<string, string>
}

/** 期間長度（天）。instant（無起日）回 null。 */
function durationDays(ctx: Ctx): number | null {
  if (!ctx.start || !ctx.end) return null
  const d = (Date.parse(ctx.end) - Date.parse(ctx.start)) / 86_400_000
  return Number.isFinite(d) ? Math.round(d) : null
}

/**
 * 這筆事實的期間長度符不符合我們要的粒度。
 *
 * 年度取 300–400 天（會計年度 52/53 週制會落在 358–371），季度取 80–100 天
 * （實測 MSFT 是 89 天、多數是 90–92）。**中間的累計期一律丟掉** —— 半年報
 * （177–184 天）與九個月（272–273 天）長得跟單季很像但意義完全不同，混進來就是
 * 錯的數字。instant（資產類，無起日）沒有期間，隨申報本身的粒度走。
 */
function matchesKind(ctx: Ctx, want: PeriodKind): boolean {
  const d = durationDays(ctx)
  if (d === null) return true // instant：期末資產餘額，粒度由申報決定
  return want === 'A' ? d >= 300 && d <= 400 : d >= 80 && d <= 100
}

/** 申報表單 → 期間粒度。10-Q 給季、年報給年。 */
export function kindOfForm(form: string): PeriodKind {
  return form.startsWith('10-Q') ? 'Q' : 'A'
}

/**
 * 單份申報 → 分部資料。回傳體積很小（幾 KB），適合進持久快取；
 * 原始 instance（0.7–14MB）刻意不快取，見 blobCache.ts 的設計說明。
 *
 * `wantKind` 決定收哪一種期間長度的事實，見 matchesKind 的說明。
 */
export async function extractFromInstance(
  xml: string,
  cfg: SegmentAxesConfig,
  tagToConcept: Map<string, string>,
  wantKind: PeriodKind = 'A',
): Promise<ExtractedFilingSegments> {
  const { contexts, facts } = parseInstance(xml)
  const axisByName = new Map(cfg.axes.map((a) => [a.axis, a]))
  const consAxes = new Set(cfg.consolidation.axes)
  const consInclude = new Set(cfg.consolidation.include_members)
  const consExclude = cfg.consolidation.exclude_patterns.map((p) => new RegExp(p, 'i'))
  const wantedConcepts = new Set(cfg.concepts.include)

  const out: ExtractedFilingSegments = { data: {}, totals: {}, residual: {}, labels: {} }

  for (const f of facts) {
    const ctx = contexts.get(f.ctx)
    if (!ctx || !ctx.end) continue
    const bare = f.tag.slice(f.tag.indexOf(':') + 1)
    const cid = tagToConcept.get(bare)
    if (!cid || !wantedConcepts.has(cid)) continue

    // 只收指定粒度：年度收 ~365 天、季度收 ~90 天，累計期（半年/九個月）丟掉
    if (!matchesKind(ctx, wantKind)) continue
    const period = periodKey(ctx.end, wantKind)

    if (ctx.dims.length === 0) {
      // 無維度 = 合併總額，拿來做階層校驗
      const t = (out.totals[period] ??= {})
      // 同期同科目多筆（重編）→ 取較大絕對值以外的處理交給呼叫端；這裡取最後一筆
      t[cid] = f.val
      continue
    }

    // 找出唯一一個「分部軸」，其餘維度只允許是 ConsolidationItems 的營運分部
    const segDims = ctx.dims.filter((d) => axisByName.has(d.axis))

    if (segDims.length === 0) {
      // 沒有分部軸、只有一個 ConsolidationItemsAxis 維度 → 調節項（見 residual）。
      // 限定「剛好一個維度」是刻意的：QCOM 的 MaterialReconcilingItems 還疊了
      // 訴訟案件軸，那是某一件官司的金額，不是可以直接加進分部的調節數。
      if (ctx.dims.length !== 1 || !consAxes.has(ctx.dims[0].axis)) continue
      const rk = normalizeMember(ctx.dims[0].member, cfg)
      out.labels[rk] ??= humanize(ctx.dims[0].member)
      const byMem = (out.residual[period] ??= {})
      ;(byMem[rk] ??= {})[cid] = f.val
      continue
    }
    if (segDims.length !== 1) continue
    const seg = segDims[0]

    let ok = true
    for (const d of ctx.dims) {
      if (d.axis === seg.axis) continue
      if (!consAxes.has(d.axis)) {
        ok = false // 帶了其他無關維度（如避險關係、公允價值層級）→ 不是分部主數字
        break
      }
      // ConsolidationItems：只收營運分部，排除公司未分攤/沖銷/調節項
      const bareMember = d.member.slice(d.member.indexOf(':') + 1)
      if (!consInclude.has(d.member) || consExclude.some((re) => re.test(bareMember))) {
        ok = false
        break
      }
    }
    if (!ok) continue

    // 分部軸自己的成員也可能是調節項（如 us-gaap:AllOtherSegmentsMember）
    const segBare = seg.member.slice(seg.member.indexOf(':') + 1)
    if (consExclude.some((re) => re.test(segBare))) continue

    const key = normalizeMember(seg.member, cfg)
    out.labels[key] ??= humanize(seg.member)
    const byAxis = (out.data[period] ??= {})
    const byMember = (byAxis[seg.axis] ??= {})
    const byConcept = (byMember[key] ??= {})
    byConcept[cid] = f.val
  }

  return out
}

/**
 * ── 上層匯總（parent）的判定 ────────────────────────────────────────────────
 *
 * 申報檔沒有直接標父子關係，但可以反推：成員全部加總若對不上合併總額，就表示裡面
 * 混了上層小計。實測 AAPL 認出 product（307B，含 iPhone/Mac/iPad/穿戴）、NVDA 認出
 * datacenter（194B，含 compute+networking）、MSFT 認出 product+service，零硬編碼。
 *
 * ⚠️ 只「標記」不「刪除」。上層數字是公司真的揭露的資料，而且常常是最有價值的
 * 那一層 —— Apple 的成本只揭露到產品/服務這層，若把 product 從營收裡刪掉，
 * 硬體與服務的毛利率差距就永遠算不出來了。呈現時照列，只是不進合計。
 */

/** 位元遮罩窮舉的上限。2^18 ≈ 26 萬次累加，每次呼叫仍在毫秒級。 */
const SUBSET_MAX_MEMBERS = 18

/**
 * 找出「加總等於合併總額」且成員數最多的所有子集合，回傳其位元遮罩陣列。
 * 沒有任何子集合對得上時回空陣列。
 *
 * 為什麼要挑**最多**成員的那一組：同一份申報常常同時揭露兩層完整的拆法，兩層加
 * 起來都等於總額。MSFT 的產品軸就是 {產品, 服務} 與 {伺服器與雲, M365, LinkedIn,
 * Windows, Dynamics, 遊戲, 搜尋…} 兩組並存，各自都對得上 331.8B。取成員多的那組
 * 當子項，資訊量最大；粗的那層自動落成上層匯總。
 *
 * 用 i & (i-1) 遞推子集合和，整體是 O(2^n) 而不是 O(2^n · n)。
 */
function largestSubsetsMatching(vals: number[], total: number, tol: number): number[] {
  const n = vals.length
  const size = 1 << n
  const sums = new Float64Array(size)
  let best = -1
  let out: number[] = []
  for (let i = 1; i < size; i++) {
    const low = i & -i
    const rest = i ^ low
    // Math.log2 對 2 的冪是精確的，拿來取最低位的索引
    sums[i] = sums[rest] + vals[Math.log2(low)]
    if (Math.abs(sums[i] - total) <= tol) {
      let bits = 0
      for (let m = i; m; m &= m - 1) bits++
      if (bits > best) {
        best = bits
        out = [i]
      } else if (bits === best) {
        out.push(i)
      }
    }
  }
  return out
}

/**
 * 單一期間 → 候選上層集合（可能不只一組，見 reconcileConcept 的說明）。
 * 空陣列代表這期對不上總額；`[[]]` 代表這期本來就是單層、沒有上層。
 */
function candidateParents(keys: string[], vals: number[], total: number, tol: number): string[][] {
  if (Math.abs(vals.reduce((a, b) => a + b, 0) - total) <= tol) return [[]]

  if (keys.length <= SUBSET_MAX_MEMBERS) {
    return largestSubsetsMatching(vals, total, tol).map((mask) =>
      keys.filter((_, i) => !(mask & (1 << i))).sort(),
    )
  }

  // 成員太多（JNJ 這類上百個成員）→ 退回逐一剔除的貪婪法，避免 2^n 爆炸
  const valOf = new Map(keys.map((k, i) => [k, vals[i]]))
  const parents: string[] = []
  let live = [...keys]
  let sum = vals.reduce((a, b) => a + b, 0)
  for (let guard = 0; guard < 8 && Math.abs(sum - total) > tol && live.length > 2; guard++) {
    const parent = live.find((k) => Math.abs(sum - valOf.get(k)! - total) <= tol)
    if (!parent) break
    live = live.filter((k) => k !== parent)
    parents.push(parent)
    sum -= valOf.get(parent)!
  }
  // 沒對上就不能宣稱誰是上層 —— 寧可全部平鋪，也不要憑猜測藏數字
  return Math.abs(sum - total) <= tol ? [parents.sort()] : []
}

/**
 * 決定「誰是上層匯總」—— **年度欄與季度欄分開決定，同一種粒度之內一次決定**。
 *
 * 為什麼不能逐期各判：同一期常常有**好幾組**成員都剛好加得出總額。Tesla 的產品軸
 * 就有 {車輛銷售, 監管積分, 車輛租賃, 儲能, 服務及其他} 與 {車輛銷售, 監管積分,
 * 車輛租賃, 儲能, 儲能銷售} 兩組都是 5 個成員、都對得上總額。逐期各挑就會這期選
 * 這組、下期選那組，合計列有的欄多算有的欄少算。成員一多更糟：LLY 的產品軸每期
 * 十幾種藥、容差是總額的 0.5%，隨便挑幾個剔掉就能落進容差，逐欄各判等於硬湊。
 *
 * 為什麼也不能全部期間一次決定：**10-K 與 10-Q 揭露的層數本來就不同**。ORCL 的
 * 地區軸年報有 8 個成員（3 大區與 5 個國家，兩層都完整、各自都等於營收總額），
 * 10-Q 只有 3 大區。混在一起投票，季度 7 期的「沒有上層」會用 7:4 蓋過年度的
 * 「3 大區是上層」，年度欄兩層全加、變成總額的兩倍。
 *
 * 所以切在粒度上：同一種粒度內每期各自算出「最多成員」的候選解（可能不只一組），
 * 計票取最高的一組（同票取上層較少的，寧可平鋪也不要多藏數字），套用到該粒度的
 * 所有欄。真正該守的不變式是**每一欄的合計都等於合併總額**；兩份 10-K 之間隨機
 * 翻臉不是揭露差異，是巧合，那種欄位就照實標未校驗。
 *
 * 決定完上層之後，再用同一套投票決定要不要補調節項（`residualByPeriod`）——
 * 見 pickResidual。
 */
export function reconcileConcept(
  byPeriod: Record<string, Record<string, Record<string, number>>>,
  totals: Record<string, Record<string, number>>,
  periods: string[],
  conceptId: string,
  tolerancePct: number,
  residualByPeriod: Record<string, Record<string, Record<string, number>>> = {},
  mustReconcile = true,
): {
  parents: Set<string>
  parentsByPeriod: Record<string, Set<string>>
  residual: Set<string>
  verified: Record<string, boolean | null>
} {
  // 年度欄與季度欄各自投票、各自決定。**分開的理由**：同一家公司在 10-K 揭露的
  // 層數常常比 10-Q 多（ORCL 年報給 3 大區＋5 個國家兩層，10-Q 只給 3 大區），
  // 混在一起投票的話季度期數多就會蓋過年度（7:4），年度欄兩層全加、變成總額的
  // 兩倍。但**同一種粒度之內仍然一次決定**，不逐欄各判 —— 逐欄各判會變成硬湊，
  // 見下方 LLY 的說明。
  const votes = new Map<PeriodKind, Map<string, number>>()
  const candsByKind = new Map<PeriodKind, string[][]>()

  for (const p of periods) {
    const members = byPeriod[p]
    const total = totals[p]?.[conceptId]
    if (!members || total === undefined) continue
    const keys = Object.keys(members).filter((k) => typeof members[k][conceptId] === 'number')
    if (keys.length === 0) continue
    const vals = keys.map((k) => members[k][conceptId])
    const tol = Math.abs(total) * (tolerancePct / 100)
    const kind = splitPeriod(p).kind
    const box = votes.get(kind) ?? new Map<string, number>()
    votes.set(kind, box)
    const bag = candsByKind.get(kind) ?? []
    candsByKind.set(kind, bag)
    // 候選可能不只一組（成員數相同、也都對得上總額）→ 各投一票，交給同粒度的票數決定
    for (const set of candidateParents(keys, vals, total, tol)) {
      box.set(set.join('|'), (box.get(set.join('|')) ?? 0) + 1)
      bag.push(set)
    }
  }

  /**
   * 同粒度內票數高者勝，同票取上層較少的（寧可平鋪也不要多藏數字），再同就取
   * 字典序 —— 必須是全序，否則同樣的輸入會給出不一樣的輸出。
   *
   * ⚠️ 這裡一定要是「一種粒度一組」，不能細到逐欄各判。LLY 的產品軸每期揭露
   * 十幾種藥，容差是總額的 0.5%（約 5,000 萬美元）—— 從 17 個成員裡挑幾個剔掉、
   * 剛好落進容差，純屬巧合的組合多得是。實測逐欄各判會讓四個年度欄分別剔掉
   * FY2022 的 Collaboration、FY2023 的 Jardiance＋Olumiant＋TYVYT、FY2024 的
   * Collaboration＋TYVYT…，然後全部「校驗通過」。那不是各期揭露層級不同，是把
   * 真正對不上的事實藏起來 —— 比不修還糟。10-K 與 10-Q 的揭露層數不同是真的，
   * 兩份 10-K 之間隨機翻臉不是。
   */
  const winnerOf = (kind: PeriodKind): string[] => {
    const box = votes.get(kind)
    let best: string[] | null = null
    for (const set of candsByKind.get(kind) ?? []) {
      if (best === null) { best = set; continue }
      const va = box!.get(set.join('|')) ?? 0
      const vb = box!.get(best.join('|')) ?? 0
      if (va > vb || (va === vb && set.length < best.length) ||
          (va === vb && set.length === best.length && set.join('|') < best.join('|'))) best = set
    }
    return best ?? []
  }

  const byKind = new Map<PeriodKind, Set<string>>()
  const parentsByPeriod: Record<string, Set<string>> = {}
  const parents = new Set<string>()
  for (const p of periods) {
    const kind = splitPeriod(p).kind
    let set = byKind.get(kind)
    if (!set) {
      set = new Set(winnerOf(kind))
      byKind.set(kind, set)
    }
    parentsByPeriod[p] = set
    for (const k of set) parents.add(k)
  }

  /** 這一期扣掉上層之後的子項加總；沒有任何子項時回 null */
  const childSum = (p: string): number | null => {
    const members = byPeriod[p]
    if (!members) return null
    const par = parentsByPeriod[p] ?? new Set<string>()
    let sum = 0
    let any = false
    for (const [k, byC] of Object.entries(members)) {
      const v = byC[conceptId]
      if (typeof v !== 'number' || par.has(k)) continue
      sum += v
      any = true
    }
    return any ? sum : null
  }

  const verifyWith = (chosen: Set<string>): Record<string, boolean> => {
    const out: Record<string, boolean> = {}
    for (const p of periods) {
      const total = totals[p]?.[conceptId]
      const sum = childSum(p)
      if (total === undefined || sum === null) {
        out[p] = false
        continue
      }
      const extra = residualSum(residualByPeriod[p], conceptId, chosen)
      out[p] = Math.abs(sum + extra - total) <= Math.abs(total) * (tolerancePct / 100)
    }
    return out
  }

  const bare = verifyWith(new Set())
  const picked = pickResidual(residualByPeriod, totals, periods, conceptId, tolerancePct, childSum)
  const withResidual = verifyWith(picked)

  // 調節項是跨期一次決定、套用到所有期間的（理由同上層匯總），所以有可能修好某幾期
  // 卻弄壞另外幾期 —— UNH 的分部間沖銷就是這樣。**只有淨增加對得上的期數才採用**，
  // 否則寧可不補：把本來對得上的欄位弄成未校驗，比少補一塊更糟。
  const score = (v: Record<string, boolean>) => periods.filter((p) => v[p]).length
  const useResidual = picked.size > 0 && score(withResidual) > score(bare)
  const settled = useResidual ? withResidual : bare

  // 非必須調節的科目（分部利潤、費用）若在**所有**可比期間都對不上，那是揭露規則
  // 使然而不是數字有問題，標成「無法校驗」而不是「校驗沒過」—— 見 SegmentCell.verified。
  //
  // ⚠️ 營收與資產（must_reconcile）**不套這條**。修 corporate 之前 PG 的營收正是
  // 全期都對不上，那是真的 bug；把它併進「無法校驗」等於自己把警訊關掉。
  const comparable = periods.filter(
    (p) => totals[p]?.[conceptId] !== undefined && childSum(p) !== null,
  )
  const structural =
    !mustReconcile && comparable.length > 0 && comparable.every((p) => !settled[p])

  const verified: Record<string, boolean | null> = {}
  for (const p of periods) verified[p] = structural ? null : settled[p]

  return {
    parents,
    parentsByPeriod,
    residual: useResidual ? picked : new Set<string>(),
    verified,
  }
}

/** 選定的調節項在這一期的合計 */
function residualSum(
  byMember: Record<string, Record<string, number>> | undefined,
  conceptId: string,
  chosen: Set<string>,
): number {
  if (!byMember || chosen.size === 0) return 0
  let s = 0
  for (const k of chosen) {
    const v = byMember[k]?.[conceptId]
    if (typeof v === 'number') s += v
  }
  return s
}

/** 調節項通常只有一兩個，2^12 已經遠遠夠用 */
const RESIDUAL_MAX_MEMBERS = 12

/**
 * ── 調節項該不該補進來 ──────────────────────────────────────────────────────
 *
 * ASC 280 底下有些金額不屬於任何一個分部（總部費用、分部間沖銷），公司會用
 * ConsolidationItemsAxis 單獨揭露，**不掛任何分部軸**。它們是合併總額的一部分：
 * PG FY2026 五個分部加總 86,112M，加上 corporate 919M 才等於合併的 87,032M；
 * KO 則要同時補上沖銷 -1,009M 與 corporate 144M 才等於 47,941M。
 *
 * 因為申報沒說這筆該掛哪個軸，判定條件只有一個：**加進去之後才對得上總額**。
 * 所以本來就對得上的軸不會被硬塞（PG 的地區軸 41,700+45,300 已經等於總額，
 * 補上去反而會多算 919M），對不上又補不起來的軸則維持未校驗，不會亂加。
 *
 * 同樣跨期計票再統一套用，理由跟上層匯總一樣：同一個成員不能這期算進合計、
 * 下期不算。
 *
 * 候選之間取**誤差最小**的那一組，同誤差才取成員少的。不能只看「成員最少」：
 * 容差是總額的 0.5%，KO 的缺口是 -865M，光補沖銷 -1,009M 就已經落在容差內，
 * 挑成員最少會停在那裡、留 144M 的 corporate 沒交代；補兩筆才剛好是 -865M。
 * 反過來也不必擔心硬湊，所有候選本來就得先通過容差，NVDA 那種本身等於總額的
 * `OperatingSegmentsMember` 誤差大到根本進不了候選。
 */
function pickResidual(
  residualByPeriod: Record<string, Record<string, Record<string, number>>>,
  totals: Record<string, Record<string, number>>,
  periods: string[],
  conceptId: string,
  tolerancePct: number,
  childSum: (p: string) => number | null,
): Set<string> {
  const votes = new Map<string, { set: string[]; count: number }>()

  for (const p of periods) {
    const total = totals[p]?.[conceptId]
    const sum = childSum(p)
    if (total === undefined || sum === null) continue
    const tol = Math.abs(total) * (tolerancePct / 100)
    if (Math.abs(sum - total) <= tol) continue // 這一期本來就對得上 → 不需要調節項

    const byMember = residualByPeriod[p]
    if (!byMember) continue
    const keys = Object.keys(byMember).filter((k) => typeof byMember[k][conceptId] === 'number')
    if (keys.length === 0 || keys.length > RESIDUAL_MAX_MEMBERS) continue
    const vals = keys.map((k) => byMember[k][conceptId])

    const need = total - sum
    let best: string[] | null = null
    let bestErr = Infinity
    const size = 1 << keys.length
    const sums = new Float64Array(size)
    for (let i = 1; i < size; i++) {
      const low = i & -i
      sums[i] = sums[i ^ low] + vals[Math.log2(low)]
      const err = Math.abs(sums[i] - need)
      if (err > tol) continue
      let bits = 0
      for (let m = i; m; m &= m - 1) bits++
      if (best !== null && (err > bestErr || (err === bestErr && bits >= best.length))) continue
      best = keys.filter((_, j) => i & (1 << j)).sort()
      bestErr = err
    }
    if (!best) continue
    const id = best.join('|')
    const v = votes.get(id) ?? { set: best, count: 0 }
    v.count++
    votes.set(id, v)
  }

  let winner: string[] = []
  let bestCount = 0
  for (const { set, count } of votes.values()) {
    if (count > bestCount || (count === bestCount && set.length < winner.length)) {
      winner = set
      bestCount = count
    }
  }
  return new Set(winner)
}

/** 反查表：裸標籤 → concept id（沿用 xbrl_zh_map 既有的 tags/tags_ifrs） */
async function buildTagIndex(): Promise<Map<string, string>> {
  const map = await loadMap()
  const idx = new Map<string, string>()
  for (const c of map.concepts) {
    for (const t of [...(c.tags ?? []), ...(c.tags_ifrs ?? [])]) {
      if (!idx.has(t)) idx.set(t, c.id)
    }
  }
  return idx
}

/**
 * 取得一家公司的分部資料。
 *
 * 每份申報是一次 SEC 請求（index.json + instance = 2 次），但**已申報的財報
 * 不可變**，所以解析結果進持久快取後永遠命中，同一份申報一輩子只抓一次。
 */
export async function getSegments(
  ref: CompanyRef,
  filings: { accessionNumber: string; reportDate: string; form: string }[],
  company: string,
): Promise<SegmentsResult> {
  const cfg = await loadSegmentAxes()
  const tagIdx = await buildTagIndex()
  const warnings: string[] = []

  const merged: ExtractedFilingSegments = { data: {}, totals: {}, residual: {}, labels: {} }

  for (const f of filings) {
    // v2：期間 key 加上 A/Q 種類後綴（見 periodKey）。
    // v3：多了 residual（調節項）。舊快取沒有這個欄位，沿用會讓 PG 那類公司
    // 永遠補不回 corporate 那一塊，所以換路徑而不是原地覆寫。
    const key = `seg/v3/${ref.cik10}/${f.accessionNumber}.json`
    let one: ExtractedFilingSegments | null = null
    try {
      one = await cached(key, async () => {
        const url = await instanceUrl(ref.cik, f.accessionNumber)
        if (!url) throw new Error('找不到 XBRL instance')
        const xml = await secFetchText(url)
        return extractFromInstance(xml, cfg, tagIdx, kindOfForm(f.form))
      })
    } catch (err) {
      warnings.push(`${f.form} ${f.reportDate}：${(err as Error).message}`)
      continue
    }
    if (!one) continue
    for (const [p, byAxis] of Object.entries(one.data)) {
      const tgt = (merged.data[p] ??= {})
      for (const [ax, byMem] of Object.entries(byAxis)) {
        const t2 = (tgt[ax] ??= {})
        for (const [mk, byC] of Object.entries(byMem)) Object.assign((t2[mk] ??= {}), byC)
      }
    }
    for (const [p, byC] of Object.entries(one.totals)) Object.assign((merged.totals[p] ??= {}), byC)
    for (const [p, byMem] of Object.entries(one.residual ?? {})) {
      const tgt = (merged.residual[p] ??= {})
      for (const [mk, byC] of Object.entries(byMem)) Object.assign((tgt[mk] ??= {}), byC)
    }
    Object.assign(merged.labels, one.labels)
  }

  // 年度欄在前、季度欄在後，各自依日期遞增。
  // 刻意不按時間單一排序：把 FY2024 夾在 FY2025 Q1 與 Q2 中間，柱狀圖會變成
  // 一根年度長條旁邊三根季度短條，視覺上就是錯的。分成兩段，圖表也各畫各的。
  const periods = Object.keys(merged.data).sort((a, b) => {
    const pa = splitPeriod(a)
    const pb = splitPeriod(b)
    if (pa.kind !== pb.kind) return pa.kind === 'A' ? -1 : 1
    return pa.end < pb.end ? -1 : pa.end > pb.end ? 1 : 0
  })
  const blocks: SegmentAxisBlock[] = []

  for (const def of cfg.axes.slice().sort((a, b) => a.priority - b.priority)) {
    const memberKeys = new Set<string>()
    const conceptIds = new Set<string>()
    for (const p of periods) {
      for (const [mk, byC] of Object.entries(merged.data[p]?.[def.axis] ?? {})) {
        memberKeys.add(mk)
        for (const c of Object.keys(byC)) conceptIds.add(c)
      }
    }
    if (memberKeys.size === 0) continue

    // 上層匯總逐期各自判定（各期揭露的成員數不同，見 reconcileConcept）
    const byPeriodMembers: Record<string, Record<string, Record<string, number>>> = {}
    for (const p of periods) {
      const m = merged.data[p]?.[def.axis]
      if (m) byPeriodMembers[p] = m
    }
    const hier = new Map<string, ReturnType<typeof reconcileConcept>>()
    for (const cid of conceptIds) {
      hier.set(
        cid,
        reconcileConcept(
          byPeriodMembers,
          merged.totals,
          periods,
          cid,
          cfg.hierarchy.tolerance_pct,
          merged.residual,
          (cfg.concepts.must_reconcile ?? ['revenue']).includes(cid),
        ),
      )
    }

    const rows = new Map<string, SegmentMemberRow>()
    const rowFor = (mk: string): SegmentMemberRow => {
      const existing = rows.get(mk)
      if (existing) return existing
      const label = cfg.member_labels.map[mk]
      const row: SegmentMemberRow = {
        key: mk,
        zh: label ?? merged.labels[mk] ?? mk,
        // 設定檔給的若本身是英文（iPhone / iPad / Mac），英文欄也用它 ——
        // 駝峰自動拆字會拆成 "IPhone"，正式產品名不該長那樣。
        // en 表是給拆字會拆壞的補救：CorporateNonSegmentMember 正規化時被剝掉
        // "SegmentMember"，拆出來只剩 "Corporate Non"
        en:
          cfg.member_labels.en?.[mk] ??
          (label && /^[\x20-\x7E]+$/.test(label) ? label : merged.labels[mk]) ??
          mk,
        values: {},
      }
      rows.set(mk, row)
      return row
    }

    for (const p of periods) {
      for (const cid of conceptIds) {
        const { parentsByPeriod, residual, verified: verifiedByPeriod } = hier.get(cid)!
        const verified = verifiedByPeriod[p] ?? null
        const parents = parentsByPeriod[p] ?? new Set<string>()

        for (const [mk, byC] of Object.entries(merged.data[p]?.[def.axis] ?? {})) {
          const v = byC[cid]
          if (typeof v !== 'number') continue
          ;(rowFor(mk).values[p] ??= {})[cid] = { value: v, verified, isParent: parents.has(mk) }
        }

        // 只輸出這個科目真的採用的調節項。同一筆調節項可能營收有、營業利益沒有，
        // 沒被採用的科目留白（n/a），不要憑空補一個沒進合計的數字上去
        for (const mk of residual) {
          const v = merged.residual[p]?.[mk]?.[cid]
          if (typeof v !== 'number') continue
          ;(rowFor(mk).values[p] ??= {})[cid] = { value: v, verified, isParent: false }
        }
      }
    }

    blocks.push({
      axis: def.axis,
      role: def.role,
      zh: def.zh,
      en: def.en,
      // 依設定檔的順序輸出，不用 Set 的插入順序
      concepts: cfg.concepts.include.filter((c) => conceptIds.has(c)),
      members: [...rows.values()],
    })
  }

  if (blocks.length === 0) warnings.push('這家公司的申報未揭露可辨識的分部維度')

  return {
    company,
    cik: ref.cik10,
    ticker: ref.ticker,
    configVersion: cfg.version,
    periods,
    axes: blocks,
    warnings,
  }
}
