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
  /**
   * 「可以無視」的維度：軸 → 允許的成員白名單。
   *
   * 有些公司把地區營收寫在集中度風險那一段，於是數字除了地區軸還疊了
   * ConcentrationRisk 兩個軸；不放行的話整個地區軸生不出任何成員（TXN）。
   * **一定要精確到成員** —— 同一個軸底下的 CustomerConcentrationRiskMember
   * 是客戶佔比、不是分部拆解，放進來會憑空多出假成員。
   */
  passthrough_dims?: { map: Record<string, string[]>; patterns?: Record<string, string[]> }
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
  /**
   * 「單一應報告分部」—— **有值的每一期**都只有一個成員、而且等於合併總額。
   * 這不是漏抓：LLY、BKNG、SNOW、AKAM 的分部軸底下真的只有
   * `xxx:ReportableSegmentMember`（實測 10-K 的 instance），它們的業務拆解在
   * 產品軸或地區軸上。照畫會生出一個唯一列是「應報告分部合計」＝合併總額的
   * 區塊，讀者看到一整塊等於總營收的表，只會以為抓壞了。呈現層據此略過並改寫一行說明。
   *
   * 只有「全部有值的期都退化」才算。BW（改組後併成一個分部，舊結構還有 4 期）、
   * GPN（改組中途 2 期）不算 —— 它們有真的分部要畫。
   */
  singleSegment?: boolean
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

/**
 * ── 靜默漏抓訊號 ────────────────────────────────────────────────────────────
 *
 * 「申報明明有分部、輸出卻沒寫上去」這類問題不會產生任何錯誤：BW 的回應是
 * `warnings: []`、每一格都校驗通過，產品軸卻整個不見。所以要另外算出結構性的
 * 可疑跡象，讓機器排出待查清單，而不是一家一家人工翻。
 *
 * 三個訊號都是純結構判定、與個別公司無關：
 * - `axis_dropped`     instance 有這個軸的目標科目事實，輸出卻沒有這個軸 → 全軸漏掉
 * - `degenerate_axis`  某軸某期只有一個成員、而且等於合併總額 → 資訊量為零
 * - `period_hole`      同軸同成員的季度序列中間空一格、前後都有 → 覆蓋斷洞
 * - `dropped_facts`    抽取器丟掉的目標科目事實，按原因與金額排序
 */
export interface SegmentGap {
  code: 'axis_dropped' | 'degenerate_axis' | 'period_hole' | 'dropped_facts'
  /** 涉及的軸；`dropped_facts` 放原因碼 */
  axis: string
  detail: string
  /** 排序用的嚴重性：涉及的營收金額（美元）。估不出來時 0 */
  amount: number
  periods?: string[]
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
  /** 靜默漏抓訊號，依 amount 遞減排序。給 tools/segment_gaps.py 掃全市場用 */
  gaps: SegmentGap[]
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
  /** 解析後的計量單位（`iso4217:USD`、`shares`、`pure`…）。查不到單位定義時為空字串 */
  unit: string
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
/**
 * 單位定義：`<unit id="usd"><measure>iso4217:USD</measure></unit>`。
 *
 * 事實上的 `unitRef` 只是個 id，光看 id 猜不出是錢還是比率 —— 各家取名沒有規則
 * （usd / U001 / Unit1）。要判斷「這筆是不是金額」一定要回頭解析 unit 元素。
 * 帶 `<divide>` 的是每股盈餘那種複合單位，不是純金額。
 */
const RE_UNIT = /<(?:[\w.-]+:)?unit\s[^>]*id="([^"]+)"[^>]*>([\s\S]*?)<\/(?:[\w.-]+:)?unit>/g
const RE_MEASURE = /<(?:[\w.-]+:)?measure>([^<]+)</

/** 這個單位是不是「某種貨幣的金額」。幣別不預設美元 —— 外國發行人報 EUR/TWD。 */
export function isMonetaryUnit(unit: string): boolean {
  return unit.startsWith('iso4217:')
}

export function parseInstance(xml: string): { contexts: Map<string, Ctx>; facts: RawFact[] } {
  const units = new Map<string, string>()
  for (const m of xml.matchAll(RE_UNIT)) {
    // 複合單位（iso4217:USD / xbrli:shares）留空字串，讓它過不了 isMonetaryUnit
    units.set(m[1], /<(?:[\w.-]+:)?divide>/.test(m[2]) ? '' : (RE_MEASURE.exec(m[2])?.[1]?.trim() ?? ''))
  }

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
    const unitRef = /unitRef="([^"]+)"/.exec(attrs)?.[1] ?? ''
    facts.push({ tag, ctx, val, unit: units.get(unitRef) ?? '' })
  }
  return { contexts, facts }
}

// ── 成員正規化與顯示名 ────────────────────────────────────────────────────

/**
 * 成員正規化：砍尾綴 → 小寫 → 去非英數。
 * 這一步就解掉了跨期改名：NVDA 的 ComputeAndNetworkingMember 與
 * ComputeAndNetworkingSegmentMember 都會變成 "computeandnetworking"。
 */
/**
 * 別名反查表（正規化後的別名 → 正式鍵）。`member_aliases` 這個設定欄位一直宣告著
 * 卻沒有任何程式讀它 —— 所以它才會一直是空的。接上之後才談得上填。
 */
const aliasCache = new WeakMap<SegmentAxesConfig, Map<string, string>>()
function aliasMap(cfg: SegmentAxesConfig): Map<string, string> {
  let m = aliasCache.get(cfg)
  if (m) return m
  m = new Map()
  for (const [canon, alts] of Object.entries(cfg.member_aliases?.map ?? {})) {
    for (const a of alts) m.set(a, canon)
  }
  aliasCache.set(cfg, m)
  return m
}

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
  return aliasMap(cfg).get(bare) ?? bare
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
  /**
   * 交叉維度事實：同一筆數字同時掛了**兩個**分部軸
   * （BW FY2025 起把營收標成 ProductOrService=Parts × BusinessSegments=BW）。
   * period → 軸 → 該軸成員 → 另一軸成員 → conceptId → 值。
   *
   * 目前只收集、不參與合併 —— 收集是為了讓遙測算得出「被丟掉的是多少錢」，
   * 也讓之後的投影修法不必再讓全體公司重新解析一次 instance。
   */
  cross: Record<string, Record<string, Record<string, Record<string, Record<string, number>>>>>
  /**
   * ── 漏抓遙測 ──────────────────────────────────────────────────────────────
   *
   * 抽取器裡每一個 `continue` 都是一次丟棄，而丟掉的事實**永遠不會出現在輸出裡**，
   * 所以只看 API 回應永遠看不見這類漏抓：BW 的產品軸整個消失，回應卻是
   * `warnings: []`、每一格都校驗通過。偵測器必須裝在抽取器內部。
   *
   * key = `原因|涉及的軸或成員`，值 = { n: 筆數, abs: 營收絕對金額合計 }。
   * **金額才是嚴重性的度量，筆數不是** —— 丟 100 筆附註明細不痛，
   * 丟 1 筆 5.9 億營收才痛。所以 abs 只累加 revenue，跨科目相加沒有意義。
   *
   * 原因碼：
   * - `cross_axis`         兩個以上分部軸同時出現（BW 中的就是這個）
   * - `deep_cross`         三個以上分部軸，無法安全投影
   * - `foreign_dim`        帶了設定外的維度（避險關係、公允價值層級…）
   * - `cons_not_operating` ConsolidationItems 成員不在白名單
   * - `member_excluded`    分部軸成員自己命中 exclude_patterns
   * - `residual_multi_dim` 無分部軸、但維度不只一個（QCOM 的訴訟案件軸）
   * - `kind_mismatch`      期間長度不符（YTD 累計）。正常現象，當雜訊基準線用
   */
  dropped: Record<string, { n: number; abs: number }>
  /**
   * 這份 instance 裡「目標科目的事實」曾經出現過哪些已知分部軸（不管最後有沒有被收）。
   * 拿來比對輸出：軸出現過卻沒生出任何成員 = 整個軸被靜默丟掉。
   */
  seenAxes: Record<string, number>
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
  const passthrough = new Map(
    Object.entries(cfg.passthrough_dims?.map ?? {}).map(([ax, ms]) => [ax, new Set(ms)]),
  )
  // 成員自訂時白名單列不完（mp:ExternalCustomersMember），改比對成員裸名的正規式
  const passthroughPat = new Map(
    Object.entries(cfg.passthrough_dims?.patterns ?? {}).map(
      ([ax, ps]) => [ax, ps.map((p) => new RegExp(p, 'i'))] as const,
    ),
  )
  /**
   * `期間|軸|成員|科目` → 這格已經由**單軸乾淨事實**填過。
   * passthrough 只補真正的空格，不准蓋掉分部小計 —— MP 同一份申報裡
   * Materials 既有小計 95,629M，也有「外部客戶」91,966M（不含分部間銷售 3,663M），
   * 文件順序是小計、外部、再一次小計，靠「後寫贏」會變成擲骰子。
   */
  const cleanKeys = new Set<string>()
  const wantedConcepts = new Set(cfg.concepts.include)

  const out: ExtractedFilingSegments = {
    data: {},
    totals: {},
    residual: {},
    labels: {},
    cross: {},
    dropped: {},
    seenAxes: {},
  }

  /** 記一筆丟棄。abs 只累加 revenue，見 ExtractedFilingSegments.dropped 的說明 */
  const drop = (reason: string, tag: string, cid: string, val: number) => {
    const d = (out.dropped[`${reason}|${tag}`] ??= { n: 0, abs: 0 })
    d.n++
    if (cid === 'revenue') d.abs += Math.abs(val)
  }

  for (const f of facts) {
    const ctx = contexts.get(f.ctx)
    if (!ctx || !ctx.end) continue
    const bare = f.tag.slice(f.tag.indexOf(':') + 1)
    const cid = tagToConcept.get(bare)
    if (!cid || !wantedConcepts.has(cid)) continue

    // 目標科目的事實碰過哪些已知分部軸，先記下來再談收不收（見 seenAxes）
    for (const d of ctx.dims) if (axisByName.has(d.axis)) out.seenAxes[d.axis] = (out.seenAxes[d.axis] ?? 0) + 1

    // 只收指定粒度：年度收 ~365 天、季度收 ~90 天，累計期（半年/九個月）丟掉
    if (!matchesKind(ctx, wantKind)) {
      if (ctx.dims.some((d) => axisByName.has(d.axis))) drop('kind_mismatch', String(durationDays(ctx)), cid, f.val)
      continue
    }
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
      if (ctx.dims.length !== 1 || !consAxes.has(ctx.dims[0].axis)) {
        drop('residual_multi_dim', ctx.dims.map((d) => d.axis).sort().join('×'), cid, f.val)
        continue
      }
      const rk = normalizeMember(ctx.dims[0].member, cfg)
      out.labels[rk] ??= humanize(ctx.dims[0].member)
      const byMem = (out.residual[period] ??= {})
      ;(byMem[rk] ??= {})[cid] = f.val
      continue
    }
    /**
     * 其餘維度（非分部軸）只允許是 ConsolidationItems 的營運分部。
     * 抽成函式是為了讓「單軸」與「交叉維度」兩條路走同一套判準。
     */
    // 丟棄要能歸因到「哪個分部軸因此消失」，否則偵測器只說得出軸不見、說不出為什麼。
    // TXN 的地區營收全被 ConcentrationRisk 兩軸擋掉，但丟棄記錄裡只有 ConcentrationRisk
    // 的軸名、沒有 GeographicalAxis，比對不上 → 嚴重性算成 0。所以 tag 一律帶上分部軸。
    const segTag = segDims.map((d) => d.axis).sort().join('+')
    const othersOk = (own: Set<string>): boolean => {
      for (const d of ctx.dims) {
        if (own.has(d.axis)) continue
        if (!consAxes.has(d.axis)) {
          // 設定層允許無視的維度（集中度揭露包住的地區營收，見 passthrough_dims）
          const bareDim = d.member.slice(d.member.indexOf(':') + 1)
          if (passthrough.get(d.axis)?.has(d.member) || passthroughPat.get(d.axis)?.some((re) => re.test(bareDim))) {
            // ⚠️ 放行的前提是這筆真的是金額。集中度揭露最常見的標法是**百分比**
            // （0.38 表示 38%，unitRef 指到 pure），照收會把 0.38 當成 0.38 美元。
            if (isMonetaryUnit(f.unit)) continue
            drop('passthrough_not_monetary', `${d.axis}@${segTag}`, cid, f.val)
            return false
          }
          // 避險關係、公允價值層級…→ 預設不是分部主數字
          drop('foreign_dim', `${d.axis}@${segTag}`, cid, f.val)
          return false
        }
        // ConsolidationItems：只收營運分部，排除公司未分攤/沖銷/調節項
        const bareMember = d.member.slice(d.member.indexOf(':') + 1)
        if (!consInclude.has(d.member) || consExclude.some((re) => re.test(bareMember))) {
          drop('cons_not_operating', `${d.member}@${segTag}`, cid, f.val)
          return false
        }
      }
      return true
    }

    if (segDims.length !== 1) {
      // 兩個分部軸交叉標註（BW：ProductOrService=Parts × BusinessSegments=BW）。
      // 現行合併流程仍然丟掉它，但先把值留在 cross 裡：遙測要靠它算金額，
      // 之後的投影修法也才不必讓全體公司重新解析一次 instance。
      const axes = segDims.map((d) => d.axis).sort().join('×')
      // 三軸以上（JNJ：產品×地區×分部）分開記，好在遙測裡看得出是哪一種；
      // 收集方式相同 —— 投影是「把其餘各軸一起加總掉」，兩軸或多軸沒有差別。
      drop(segDims.length > 2 ? 'deep_cross' : 'cross_axis', axes, cid, f.val)
      const own = new Set(segDims.map((d) => d.axis))
      if (!othersOk(own)) continue
      const excluded = segDims.some((d) => {
        const b = d.member.slice(d.member.indexOf(':') + 1)
        return consExclude.some((re) => re.test(b))
      })
      if (excluded) continue
      for (let i = 0; i < segDims.length; i++) {
        const me = segDims[i]
        // 其餘各軸的成員合成一把 key：投影時對這把 key 加總，每筆事實只會被算一次
        const otherKey = segDims
          .filter((_, j) => j !== i)
          .map((d) => normalizeMember(d.member, cfg))
          .sort()
          .join('|')
        const mk = normalizeMember(me.member, cfg)
        out.labels[mk] ??= humanize(me.member)
        const byAxis = (out.cross[period] ??= {})
        const byMember = ((byAxis[me.axis] ??= {})[mk] ??= {})
        ;(byMember[otherKey] ??= {})[cid] = f.val
      }
      continue
    }
    const seg = segDims[0]

    if (!othersOk(new Set([seg.axis]))) continue

    // 分部軸自己的成員也可能是調節項（如 us-gaap:AllOtherSegmentsMember）
    const segBare = seg.member.slice(seg.member.indexOf(':') + 1)
    if (consExclude.some((re) => re.test(segBare))) {
      drop('member_excluded', `${seg.member}@${seg.axis}`, cid, f.val)
      continue
    }

    const key = normalizeMember(seg.member, cfg)
    out.labels[key] ??= humanize(seg.member)
    const byAxis = (out.data[period] ??= {})
    const byMember = (byAxis[seg.axis] ??= {})
    const byConcept = (byMember[key] ??= {})
    // 這筆是靠 passthrough 才進得來的（帶了分部軸與 ConsolidationItems 以外的維度）
    const viaPassthrough = ctx.dims.some((d) => d.axis !== seg.axis && !consAxes.has(d.axis))
    const ck = `${period}|${seg.axis}|${key}|${cid}`
    if (viaPassthrough) {
      if (cleanKeys.has(ck)) {
        drop('passthrough_shadowed', `${seg.axis}|${key}`, cid, f.val)
        continue
      }
    } else {
      cleanKeys.add(ck)
    }
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

  /** 這一期扣掉上層之後的子項加總；沒有任何子項時回 null */
  const childSumWith =
    (pbp: Record<string, Set<string>>) =>
    (p: string): number | null => {
      const members = byPeriod[p]
      if (!members) return null
      const par = pbp[p] ?? new Set<string>()
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

  /**
   * 給定一組上層匯總，把「要不要補調節項」也一起決定完，回傳最終校驗結果。
   *
   * 調節項是跨期一次決定、套用到所有期間的（理由同上層匯總），所以有可能修好某幾期
   * 卻弄壞另外幾期 —— UNH 的分部間沖銷就是這樣。**只有淨增加對得上的期數才採用**，
   * 否則寧可不補：把本來對得上的欄位弄成未校驗，比少補一塊更糟。
   */
  const evaluate = (pbp: Record<string, Set<string>>) => {
    const childSum = childSumWith(pbp)
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
    const score = (v: Record<string, boolean>) => periods.filter((p) => v[p]).length
    const useResidual = picked.size > 0 && score(withResidual) > score(bare)
    return {
      settled: useResidual ? withResidual : bare,
      residual: useResidual ? picked : new Set<string>(),
      childSum,
    }
  }

  const kinds = [...new Set(periods.map((p) => splitPeriod(p).kind))]
  const byKind = new Map<PeriodKind, Set<string>>(
    kinds.map((k) => [k, new Set(winnerOf(k))] as const),
  )
  const spread = (m: Map<PeriodKind, Set<string>>): Record<string, Set<string>> => {
    const out: Record<string, Set<string>> = {}
    for (const p of periods) out[p] = m.get(splitPeriod(p).kind) ?? new Set<string>()
    return out
  }

  /**
   * ⚠️ 票數不能單獨決定上層匯總 —— 候選是「剔掉某些成員之後剛好落進容差」湊出來的，
   * 而容差是總額的 0.5%，**巧合會發生**。AAPL 的地區分部營業利益：FY2023 五個地區
   * 加總 150,888、合併 114,301，剔掉歐洲剛好是 114,790（差 489，容差 571）→ 歐洲被
   * 判成上層。其餘三個年度都湊不出來、一票也沒有，於是這唯一的一票贏下全部年度欄，
   * 歐洲被排除在合計外，四個年度欄全部對不上（正解是「沒有上層，補上未分攤的
   * corporate 才等於合併數」，季度欄正是這樣過的）。
   *
   * 所以跟調節項套同一條規則：**採用上層匯總之後，該粒度對得上的欄位要變多才算數**。
   * 同分取不設上層（寧可平鋪也不要多藏數字）。候選本來就至少有投票的那一期會過，
   * 不會出現兩邊都是 0 的僵局。
   */
  for (const k of kinds) {
    if (!byKind.get(k)!.size) continue
    const kp = periods.filter((p) => splitPeriod(p).kind === k)
    const score = (v: Record<string, boolean>) => kp.filter((p) => v[p]).length
    const off = new Map(byKind)
    off.set(k, new Set<string>())
    if (score(evaluate(spread(off)).settled) >= score(evaluate(spread(byKind)).settled)) {
      byKind.set(k, new Set<string>())
    }
  }

  const parentsByPeriod = spread(byKind)
  const parents = new Set<string>()
  for (const set of byKind.values()) for (const k of set) parents.add(k)

  const { settled, residual, childSum } = evaluate(parentsByPeriod)

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
    residual,
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

/**
 * ── 靜默漏抓偵測 ────────────────────────────────────────────────────────────
 *
 * 為什麼要有這東西：BW 的 `/api/segments` 回應是 `warnings: []`、每一格 `verified`
 * 都是 true，看起來完美 —— 但整個產品軸（Parts/Projects/Construction）從來沒出現，
 * 三個分部的 2025Q1/Q2 也整排消失。被丟掉的事實不會進到回應裡，所以**看輸出永遠
 * 看不見這類漏抓**。這裡把抽取器記下來的丟棄遙測和輸出結構對照，算出可疑跡象。
 *
 * 判定全部是結構性的、與個別公司無關，所以同一種揭露型態的公司會一起被抓出來 ——
 * 修一個型態就修掉一整類，不必一家一家排查。
 */
export function detectGaps(
  cfg: SegmentAxesConfig,
  merged: ExtractedFilingSegments,
  claimedTotals: Map<string, Record<string, number> | undefined>,
  periods: string[],
  blocks: SegmentAxisBlock[],
): SegmentGap[] {
  const gaps: SegmentGap[] = []
  const tolPct = cfg.hierarchy.tolerance_pct / 100
  const axisZh = new Map(cfg.axes.map((a) => [a.axis, a.zh]))
  const totalOf = (p: string, ax: string): number | undefined =>
    (claimedTotals.get(`${p}|${ax}`) ?? merged.totals[p])?.['revenue']

  // ── S1 整個軸被丟掉：instance 有這個軸的目標科目事實，輸出卻沒有這個軸
  const emitted = new Set(blocks.map((b) => b.axis))
  /** 已經以 axis_dropped 回報過的軸，供 S4 去重用 */
  const droppedAxes = new Set<string>()
  for (const [ax, n] of Object.entries(merged.seenAxes)) {
    if (emitted.has(ax)) continue
    // 金額優先用交叉維度裡的實際數字（那才是真的被丟掉的營收），
    // 沒有的話退回遙測累計的絕對金額（會跨申報重複計、只當嚴重性排序用）
    let amount = 0
    for (const p of Object.keys(merged.cross)) {
      for (const byOther of Object.values(merged.cross[p]?.[ax] ?? {})) {
        for (const byC of Object.values(byOther)) amount += Math.abs(byC['revenue'] ?? 0)
      }
    }
    // 這個軸是被什麼原因擋掉的 —— 取金額最大的那一條。沒有這個歸因，排行榜只會說
    // 「軸不見了」，說不出該修哪裡；有了它，同一種原因的公司會自動歸成同一個型態。
    let why = ''
    for (const [k, d] of Object.entries(merged.dropped)) {
      if (k.startsWith('kind_mismatch|') || !k.includes(ax)) continue
      amount += d.abs
      if (!why || d.abs > (merged.dropped[why]?.abs ?? 0)) why = k
    }
    // 都算不出來時，改用「有多少營收在這個軸上完全沒有拆分」當嚴重性 ——
    // 整軸消失本來就是最重的一類，不能因為算不出金額就沉到排行榜底部
    if (!amount) {
      for (const p of periods) amount += Math.abs(merged.totals[p]?.['revenue'] ?? 0)
    }
    droppedAxes.add(ax)
    gaps.push({
      code: 'axis_dropped',
      axis: ax,
      detail:
        `${axisZh.get(ax) ?? ax}：instance 有 ${n} 筆目標科目事實掛在這個軸，輸出卻完全沒有這個軸` +
        (why ? `（主因 ${why.replace('|', '：')}）` : ''),
      amount,
    })
  }

  for (const b of blocks) {
    // ── S2 退化軸：某期只剩一個成員、而且等於合併總額 → 等於沒揭露分部
    //    （ASC 280「單一應報告分部」的標註慣例，讀者會誤以為公司真的只有一塊業務）
    const degenerate: string[] = []
    let degAmount = 0
    for (const p of periods) {
      const withVal = b.members.filter((m) => typeof m.values[p]?.['revenue']?.value === 'number')
      if (withVal.length !== 1) continue
      const v = withVal[0].values[p]['revenue'].value
      const t = totalOf(p, b.axis)
      if (t === undefined || Math.abs(v - t) > Math.abs(t) * tolPct) continue
      degenerate.push(p)
      degAmount += Math.abs(t)
    }
    // 有值的期全部退化 → 這家公司就是單一應報告分部，不是我們漏抓
    const withData = periods.filter((p) =>
      b.members.some((m) => typeof m.values[p]?.['revenue']?.value === 'number'),
    )
    if (withData.length && degenerate.length === withData.length) b.singleSegment = true
    if (degenerate.length) {
      gaps.push({
        code: 'degenerate_axis',
        axis: b.axis,
        detail: `${b.zh}：${degenerate.length} 期只有單一成員且等於合併總額，資訊量為零`,
        amount: degAmount,
        periods: degenerate,
      })
    }

    // ── S3 期間破洞：同一成員的季度序列中間空一格、前後都有值
    //    改組（成員整組換掉）不會長這樣 —— 那是尾端截斷，不是中間挖洞。
    //    中間挖洞代表較新的申報把較舊申報的拆法蓋掉了（BW 的 2025Q1/Q2）。
    const q = periods.filter((p) => splitPeriod(p).kind === 'Q')
    const holes = new Set<string>()
    let holeAmount = 0
    for (const m of b.members) {
      const hit = q.map((p) => typeof m.values[p]?.['revenue']?.value === 'number')
      const first = hit.indexOf(true)
      const last = hit.lastIndexOf(true)
      if (first < 0 || last - first < 2) continue
      for (let i = first + 1; i < last; i++) {
        if (hit[i]) continue
        holes.add(q[i])
        // 缺的那一格值多少無從得知，用前後兩個有值的季度取平均當規模估計
        let lo = i - 1
        while (lo > first && !hit[lo]) lo--
        let hi = i + 1
        while (hi < last && !hit[hi]) hi++
        holeAmount +=
          (Math.abs(m.values[q[lo]]['revenue'].value) + Math.abs(m.values[q[hi]]['revenue'].value)) / 2
      }
    }
    if (holes.size) {
      gaps.push({
        code: 'period_hole',
        axis: b.axis,
        detail: `${b.zh}：${[...holes].length} 個季度夾在有值的季度之間卻整排空白`,
        amount: holeAmount,
        periods: [...holes].sort(),
      })
    }
  }

  /**
   * ── S4 被丟掉的事實 ──────────────────────────────────────────────────────
   *
   * **只列「可以救回來」的原因**。抽取器丟東西大多是對的：`foreign_dim`（客戶
   * 集中度、權益法投資、公允價值層級）、`cons_not_operating`（公司未分攤、沖銷）、
   * `kind_mismatch`（YTD 累計）都是刻意的過濾，把它們當漏抓會直接淹掉真訊號 ——
   * 實測 UNH 的 MajorCustomersAxis 一家就報 8,000B，排行榜整個沒法看。
   * 完整的丟棄明細仍留在 ExtractedFilingSegments.dropped 裡供除錯。
   */
  const RECOVERABLE = new Set(['cross_axis', 'deep_cross', 'member_excluded'])
  for (const [k, d] of Object.entries(merged.dropped)) {
    const [reason, tag] = k.split('|')
    if (!RECOVERABLE.has(reason) || d.abs === 0) continue
    if (reason === 'cross_axis' || reason === 'deep_cross') {
      const axes = tag.split('×')
      // 抽取時丟掉、合併時又被投影救回來的不算漏抓。遙測記在抽取層（那時還不知道
      // 救不救得回來），所以要在這裡回頭核銷，否則 BW 修好之後仍會報 31.7 億漏抓
      if (axes.every((ax) => emitted.has(ax))) continue
      // 已經有 axis_dropped 在講同一件事了，別讓同一個問題在排行榜上占兩格
      // （WMT 的產品軸同時報 79 億與 33 億，其實是同一筆交叉表）
      if (axes.some((ax) => droppedAxes.has(ax))) continue
    }
    // 交叉維度的金額改從 cross 取：dropped.abs 是逐份申報累加的，同一期被多份
    // 申報揭露就會重複計；cross 在合併時已經去重，而且每筆只取一個方向才不會算兩次
    let amount = d.abs
    if (reason === 'cross_axis') {
      const first = tag.split('×')[0]
      let dedup = 0
      for (const p of Object.keys(merged.cross)) {
        for (const byOther of Object.values(merged.cross[p]?.[first] ?? {})) {
          for (const byC of Object.values(byOther)) dedup += Math.abs(byC['revenue'] ?? 0)
        }
      }
      if (dedup) amount = dedup
    }
    gaps.push({
      code: 'dropped_facts',
      axis: reason,
      detail: `${reason}：${tag} 丟掉 ${d.n} 筆目標科目事實`,
      amount,
    })
  }

  return gaps.sort((a, b) => b.amount - a.amount)
}

/**
 * ── 交叉表投影 ──────────────────────────────────────────────────────────────
 *
 * 有些公司不是「一個軸標一組數字」，而是把兩個軸交叉標在同一筆事實上。BW 從
 * FY2025 起就是這樣：
 *
 *   srt:ProductOrServiceAxis=bw:PartsMember | us-gaap:StatementBusinessSegmentsAxis=bw:BWMember
 *
 * 這種事實兩個軸都不是單獨可用的，抽取器只好整筆丟掉 —— 於是產品軸一個成員都沒有，
 * 分部軸只剩一個等於總額的 bw。實際上把另一個軸加總掉就還原得出產品軸的三個成員。
 *
 * 兩道護欄，缺一不可：
 *
 * 1. **只救「整個軸完全不存在」的公司**，而且要看合併完所有申報之後的結果。
 *    軸本來就有直接資料時，交叉表提供的是「更細一層的補充揭露」——**那是不同粒度的
 *    另一層**，混進同一個軸，上層匯總的跨期投票就會挑出一組只適用部分期間的答案，
 *    把本來對得上的欄位弄成對不上（ORCL 的地區軸年報兩層、10-Q 一層，已經踩過同一個
 *    陷阱，見 reconcileConcept）。要支援那種補充揭露是另一個題目，不能順手混進來。
 *
 * 2. **投影結果必須對得上合併總額。** 交叉表的另一個軸若含上層匯總成員，加總會變成
 *    總額的兩倍，對帳直接擋掉；WMT 的交叉表只涵蓋 Walmart US 與 Sam's Club US
 *    （575,990M vs 合併 706,413M，缺國際部門），也會被擋下來 —— 寧可留白，
 *    不要給一組看起來像全公司拆解、其實少一塊的數字。
 */
export function projectMissingAxes(
  merged: ExtractedFilingSegments,
  cfg: SegmentAxesConfig,
  claimedTotals: Map<string, Record<string, number> | undefined>,
): void {
  const tol = cfg.hierarchy.tolerance_pct / 100
  for (const def of cfg.axes) {
    // 護欄 1：合併完之後這個軸還是一個成員都沒有，才輪得到投影
    const alreadyHas = Object.values(merged.data).some(
      (byAxis) => Object.keys(byAxis[def.axis] ?? {}).length > 0,
    )
    if (alreadyHas) continue

    for (const [p, byAxis] of Object.entries(merged.cross)) {
      const byMem = byAxis[def.axis]
      if (!byMem) continue

      // 把其餘各軸一起加總掉：conceptId → 成員 → 值
      const proj: Record<string, Record<string, number>> = {}
      for (const [mk, byOther] of Object.entries(byMem)) {
        for (const byC of Object.values(byOther)) {
          for (const [cid, v] of Object.entries(byC)) {
            const box = (proj[cid] ??= {})
            box[mk] = (box[mk] ?? 0) + v
          }
        }
      }

      for (const [cid, mem] of Object.entries(proj)) {
        const total = merged.totals[p]?.[cid]
        if (total === undefined) continue
        const sum = Object.values(mem).reduce((a, b) => a + b, 0)
        if (Math.abs(sum - total) > Math.abs(total) * tol) continue // 護欄 2
        const tgt = ((merged.data[p] ??= {})[def.axis] ??= {})
        for (const [mk, v] of Object.entries(mem)) (tgt[mk] ??= {})[cid] = v
        claimedTotals.set(`${p}|${def.axis}`, merged.totals[p])
      }
    }
  }
}

/**
 * 反查表：裸標籤 → concept id（沿用 xbrl_zh_map 既有的 tags/tags_ifrs）。
 * 一併回傳對照表版本 —— 抽取結果是「instance × segment_axes × **xbrl_zh_map**」的函數，
 * 快取 key 少綁後面那個就會靜靜失效（見 getSegments 的 key 註解）。
 */
async function buildTagIndex(): Promise<{ idx: Map<string, string>; version: string }> {
  const map = await loadMap()
  const idx = new Map<string, string>()
  for (const c of map.concepts) {
    for (const t of [...(c.tags ?? []), ...(c.tags_ifrs ?? [])]) {
      if (!idx.has(t)) idx.set(t, c.id)
    }
  }
  return { idx, version: map.version }
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
  const { idx: tagIdx, version: mapVersion } = await buildTagIndex()
  const warnings: string[] = []

  const merged: ExtractedFilingSegments = {
    data: {},
    totals: {},
    residual: {},
    labels: {},
    cross: {},
    dropped: {},
    seenAxes: {},
  }
  /** `期間|軸` → 最新那份申報給的成員集合。後面較舊的申報不得再加新成員，見下方說明 */
  const claimedMembers = new Map<string, Set<string>>()
  /** `期間|軸` → 供應該批成員的那份申報所報的合併總額／調節項（重編過的年度不可混用） */
  const claimedTotals = new Map<string, Record<string, number> | undefined>()
  const claimedResidual = new Map<string, Record<string, Record<string, number>> | undefined>()
  /** `期間|軸` → 交叉表已由較新的申報供應過，不再與較舊的疊加（見下方說明） */
  const claimedCross = new Set<string>()

  for (const f of filings) {
    // v2：期間 key 加上 A/Q 種類後綴（見 periodKey）。
    // v3：多了 residual（調節項）。舊快取沒有這個欄位，沿用會讓 PG 那類公司
    // 永遠補不回 corporate 那一塊，所以換路徑而不是原地覆寫。
    // v4：多了 cross（交叉維度事實）與 dropped/seenAxes（漏抓遙測）。舊快取沒有
    // 這些欄位，沿用的話漏抓偵測會對已快取的公司一律回報「乾淨」—— 那是假陰性，
    // 比沒有偵測器更糟，所以換路徑而不是原地相容。
    // v5：passthrough_dims 讓集中度軸包住的地區營收得以收進來（TXN），抽取結果本身變了。
    //
    // ⚠️ key 一併綁上 config 版本。抽取結果是「instance × 設定檔」的函數 —— 只靠
    // 手動版號的話，改了 config/segment_axes.json 卻忘了改這裡，全體公司就會安靜地
    // 吃舊快取：新增一個軸毫無效果，而且不會有任何錯誤訊息。綁上去之後設定檔改版
    // 即自動失效，不必記得同時改兩個地方。
    // v6：key 補綁 xbrl_zh_map 版本。抽取要不要收一筆事實，取決於它的標籤有沒有
    // 對到 concept —— 那份對照表是 `xbrl_zh_map.json`，不是 `segment_axes.json`。
    // 少綁的後果實測到了：map v1.8（commit 416daf3）補進 15 個標籤，其中
    // `OtherDepreciationAndAmortization` 是 Cummins 申報分部折舊用的標籤，
    // 但快取 key 沒變 → 已解析過的公司永遠拿不到那 62 格，而且不會有任何錯誤訊息。
    const key = `seg/v6-${cfg.version}-m${mapVersion}/${ref.cik10}/${f.accessionNumber}.json`
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


    /**
     * ── 同一期間被多份申報揭露時，新的那份說了算 ────────────────────────────
     *
     * `filings` 是新到舊，所以合併一律「不覆蓋已存在的值」。原本用 Object.assign
     * 是舊的蓋新的，方向反了 —— 重編（停業單位重分類）之後的數字會被舊版蓋回去。
     *
     * 更重要的是**成員集合要鎖在最新那份**。公司改分部結構時，新的年報會把去年
     * 的比較數用**新結構**重編，而去年自己的年報用的是**舊結構**，兩份都在視窗裡：
     * Carrier FY2024 同時有舊的 HVAC/Refrigeration 與新的氣候方案四大區，聯集起來
     * 加總是合併總額的兩倍（營收 450 億 vs 實際 225 億）。所以某個（期間, 軸）一旦
     * 由最新的申報給過成員，後面較舊的申報只能替**同一批成員**補上更多科目，
     * 不准再帶進新的成員 key。結構沒變的公司成員 key 一樣，聯集照舊生效。
     */
    for (const [p, byAxis] of Object.entries(one.data)) {
      const tgt = (merged.data[p] ??= {})
      for (const [ax, byMem] of Object.entries(byAxis)) {
        const claimed = claimedMembers.get(`${p}|${ax}`)
        const t2 = (tgt[ax] ??= {})
        for (const [mk, byC] of Object.entries(byMem)) {
          if (claimed && !claimed.has(mk)) continue
          const cell = (t2[mk] ??= {})
          for (const [cid, v] of Object.entries(byC)) if (!(cid in cell)) cell[cid] = v
        }
        if (!claimed) {
          claimedMembers.set(`${p}|${ax}`, new Set(Object.keys(byMem)))
          // 對帳用的合併總額與調節項，一定要取**供應這批成員的那份申報**的版本。
          // 公司把某條業務轉列停業單位之後，新的年報會把去年的合併營收改成不含
          // 該業務；但去年的分部拆法若只有舊年報有（新年報沒再揭露那個軸），拿舊
          // 拆法去對新總額當然對不上 —— GPN 的地區軸就是這樣被誤標成對不上。
          claimedTotals.set(`${p}|${ax}`, one.totals[p])
          claimedResidual.set(`${p}|${ax}`, one.residual?.[p])
        }
      }
    }
    for (const [p, byC] of Object.entries(one.totals)) {
      const tgt = (merged.totals[p] ??= {})
      for (const [cid, v] of Object.entries(byC)) if (!(cid in tgt)) tgt[cid] = v
    }
    for (const [p, byMem] of Object.entries(one.residual ?? {})) {
      const tgt = (merged.residual[p] ??= {})
      for (const [mk, byC] of Object.entries(byMem)) {
        const cell = (tgt[mk] ??= {})
        for (const [cid, v] of Object.entries(byC)) if (!(cid in cell)) cell[cid] = v
      }
    }
    /**
     * 交叉表也要套「新的申報說了算」，而且是**整組**採用，不能逐格聯集。
     *
     * 逐格聯集會出事，因為兩份申報的另一個軸成員名字不同：BW 的 2026Q2 10-Q 把
     * 2025-06-30 標成 Parts×bw（60,535，對得上重編後的 138,856），2025Q2 自己那份
     * 則按舊三分部拆成 Parts×Thermal 49,620 ＋ Parts×Environmental 10,309 ＋
     * Parts×Renewable 4,851。otherKey 不撞，於是兩組都留下來，投影一加總變成
     * 282,912 —— 對不上總額，整欄被拒、變成空白。基礎不同的兩張交叉表本來就不能疊。
     */
    for (const [p, byAxis] of Object.entries(one.cross ?? {})) {
      const tgt = (merged.cross[p] ??= {})
      for (const [ax, byMem] of Object.entries(byAxis)) {
        if (claimedCross.has(`${p}|${ax}`)) continue
        claimedCross.add(`${p}|${ax}`)
        tgt[ax] = byMem
      }
    }
    for (const [k, d] of Object.entries(one.dropped ?? {})) {
      const t = (merged.dropped[k] ??= { n: 0, abs: 0 })
      t.n += d.n
      t.abs += d.abs
    }
    for (const [ax, n] of Object.entries(one.seenAxes ?? {})) {
      merged.seenAxes[ax] = (merged.seenAxes[ax] ?? 0) + n
    }
    Object.assign(merged.labels, one.labels)
  }

  // 交叉表投影：把「只有交叉標註、因此整個軸都不見」的軸還原出來（BW 的產品軸）。
  // 一定要等所有申報合併完才做 —— 判準是「這個軸完全沒有直接資料」，逐份申報看
  // 會誤判（那份沒有不代表別份沒有），實測會混進不同粒度的成員、淨掉 158 格。
  projectMissingAxes(merged, cfg, claimedTotals)

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

    // 上層匯總逐期各自判定（各期揭露的成員數不同，見 reconcileConcept）。
    // 對帳的總額與調節項按軸取用 —— 同一期的不同軸可能由不同申報供應（見 claimedTotals）
    const byPeriodMembers: Record<string, Record<string, Record<string, number>>> = {}
    const axisTotals: Record<string, Record<string, number>> = {}
    const axisResidual: Record<string, Record<string, Record<string, number>>> = {}
    for (const p of periods) {
      const m = merged.data[p]?.[def.axis]
      if (!m) continue
      byPeriodMembers[p] = m
      // 合併總額：優先取供應這批成員的那份申報（重編前後不可混用 —— GPN 的地區軸
      // 就是拿舊拆法去對新總額才被誤標成對不上）。但那份申報**沒報**該期總額時仍要
      // 退回其他申報：年報最舊的那個比較年度常常沒有無維度事實。實測拿掉這層退路，
      // DIS/IDA/EQT/CLVT/FCX 合計掉兩百多格「對得上」—— 那是缺值，不是基礎不同。
      const t = claimedTotals.get(`${p}|${def.axis}`) ?? merged.totals[p]
      if (t) axisTotals[p] = t
      // ⚠️ 調節項**沒有這層退路**，因為它不是缺值問題：跨申報借用會把別份的分部間
      // 沖銷套到這一份的成員上。BW 的 FY2023 拿 FY2024 10-K 的 −21,391 千元去對
      // FY2025 10-K 重編後的 587,448 千元總額，調節項投票整組被否決，連 FY2022
      // 都跟著掉成未校驗。寧可不補，也不要補一塊別人的。
      const r = claimedResidual.get(`${p}|${def.axis}`)
      if (r) axisResidual[p] = r
    }
    const hier = new Map<string, ReturnType<typeof reconcileConcept>>()
    for (const cid of conceptIds) {
      hier.set(
        cid,
        reconcileConcept(
          byPeriodMembers,
          axisTotals,
          periods,
          cid,
          cfg.hierarchy.tolerance_pct,
          axisResidual,
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
          const v = axisResidual[p]?.[mk]?.[cid]
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

  // 靜默漏抓：只有「整個軸不見」與「季度中間挖洞」兩種會影響讀者對表格的解讀，
  // 進 warnings 讓 UI 說出來；其餘留在 gaps 供全市場掃描排序，不吵使用者。
  const gaps = detectGaps(cfg, merged, claimedTotals, periods, blocks)
  for (const g of gaps) {
    if (g.code === 'axis_dropped' || g.code === 'period_hole') warnings.push(g.detail)
  }

  return {
    company,
    cik: ref.cik10,
    ticker: ref.ticker,
    configVersion: cfg.version,
    periods,
    axes: blocks,
    warnings,
    gaps,
  }
}
