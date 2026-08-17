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
    derived: { id: string; zh: string; en: string; formula: string; format: string }[]
  }
  member_labels: { map: Record<string, string> }
}

export interface SegmentCell {
  value: number
  /** 該列是否通過「分部加總 = 合併總額」校驗 */
  verified: boolean
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

  const out: ExtractedFilingSegments = { data: {}, totals: {}, labels: {} }

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
 * 跨期一次決定「誰是上層匯總」，而不是每期各自判斷。
 *
 * 為什麼不能逐期判斷：同一期常常有**好幾組**成員都剛好加得出總額。Tesla 的產品軸
 * 就有 {車輛銷售, 監管積分, 車輛租賃, 儲能, 服務及其他} 與 {車輛銷售, 監管積分,
 * 車輛租賃, 儲能, 儲能銷售} 兩組都是 5 個成員、都對得上總額。逐期挑就會這期選這組、
 * 下期選那組 —— 同一個成員在 FY2025 欄算子項、在 Q2 欄卻算上層，合計列就會有的欄
 * 多算、有的欄少算。
 *
 * 作法：每期各自算出「最多成員」的候選解，把對應的上層集合拿去跨期計票，取得票
 * 最高的一組（同票取上層較少的，寧可平鋪也不要多藏數字），然後**同一組套用到所有
 * 期間**，再逐期驗算加總對不對得上，對不上的那一期照實標為未校驗。
 */
export function reconcileConcept(
  byPeriod: Record<string, Record<string, Record<string, number>>>,
  totals: Record<string, Record<string, number>>,
  periods: string[],
  conceptId: string,
  tolerancePct: number,
): { parents: Set<string>; verified: Record<string, boolean> } {
  const votes = new Map<string, { set: string[]; count: number }>()

  for (const p of periods) {
    const members = byPeriod[p]
    const total = totals[p]?.[conceptId]
    if (!members || total === undefined) continue
    const keys = Object.keys(members).filter((k) => typeof members[k][conceptId] === 'number')
    if (keys.length === 0) continue
    const vals = keys.map((k) => members[k][conceptId])
    const tol = Math.abs(total) * (tolerancePct / 100)
    // 候選可能不只一組（成員數相同、也都對得上總額）→ 各投一票，交給跨期票數決定
    for (const set of candidateParents(keys, vals, total, tol)) {
      const id = set.join('|')
      const v = votes.get(id) ?? { set, count: 0 }
      v.count++
      votes.set(id, v)
    }
  }

  let winner: string[] = []
  let bestCount = -1
  for (const { set, count } of votes.values()) {
    if (count > bestCount || (count === bestCount && set.length < winner.length)) {
      winner = set
      bestCount = count
    }
  }
  const parents = new Set(winner)

  const verified: Record<string, boolean> = {}
  for (const p of periods) {
    const members = byPeriod[p]
    const total = totals[p]?.[conceptId]
    if (!members || total === undefined) {
      verified[p] = false
      continue
    }
    let sum = 0
    let any = false
    for (const [k, byC] of Object.entries(members)) {
      const v = byC[conceptId]
      if (typeof v !== 'number' || parents.has(k)) continue
      sum += v
      any = true
    }
    verified[p] = any && Math.abs(sum - total) <= Math.abs(total) * (tolerancePct / 100)
  }
  return { parents, verified }
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

  const merged: ExtractedFilingSegments = { data: {}, totals: {}, labels: {} }

  for (const f of filings) {
    // v2：期間 key 加上 A/Q 種類後綴（見 periodKey）。舊快取的 key 沒有後綴，
    // 直接沿用會把季度與年度混在一起，所以換路徑而不是原地覆寫。
    const key = `seg/v2/${ref.cik10}/${f.accessionNumber}.json`
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

    // 上層匯總「跨期一次決定」，逐期各判會前後不一致（見 reconcileConcept）
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
        ),
      )
    }

    const rows = new Map<string, SegmentMemberRow>()
    for (const p of periods) {
      const byMember = merged.data[p]?.[def.axis]
      if (!byMember) continue
      for (const cid of conceptIds) {
        const { parents, verified: verifiedByPeriod } = hier.get(cid)!
        const verified = verifiedByPeriod[p] ?? false
        for (const [mk, byC] of Object.entries(byMember)) {
          const v = byC[cid]
          if (typeof v !== 'number') continue
          const label = cfg.member_labels.map[mk]
          const row =
            rows.get(mk) ??
            rows
              .set(mk, {
                key: mk,
                zh: label ?? merged.labels[mk] ?? mk,
                // 設定檔給的若本身是英文（iPhone / iPad / Mac），英文欄也用它 ——
                // 駝峰自動拆字會拆成 "IPhone"，正式產品名不該長那樣
                en: (label && /^[\x20-\x7E]+$/.test(label) ? label : merged.labels[mk]) ?? mk,
                values: {},
              })
              .get(mk)!
          ;(row.values[p] ??= {})[cid] = { value: v, verified, isParent: parents.has(mk) }
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
