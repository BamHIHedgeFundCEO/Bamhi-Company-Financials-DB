import { secFetchJson, secFetchText } from './secFetch'
import { cacheGet, cacheSet } from './blobCache'
import type { CompanyRef } from './cik'

/**
 * Form 3 / 4 / 5（內部人持股申報）。
 *
 * 為什麼可以做：Form 4 本身就是**結構化 XML**，不是 HTML，跟硬規則 #3 沒有衝突
 * （那條講的是三大報表數字不得解析 10-K/10-Q HTML）。單檔只有 2–4 KB。
 *
 * **判讀規則（不內建就一定會誤導讀者）：**
 * 多數 Form 4 是 `A`（股權獎勵入帳）與 `M`→`S`（選擇權行權後依 10b5-1 預定計畫賣出）。
 * 那**不是**看空訊號，把它們算進「內部人賣出金額」等於製造假訊號。
 * 真訊號是 `P`（公開市場買入）與 `aff10b5One != true` 的 `S`（自主賣出）。
 * 因此本模組把交易分成三類（`open` / `plan` / `comp`），UI 必須分開呈現。
 *
 * SEC 請求預算：submissions 1 次（與其他端點共用行程內快取）＋ 每份 Form 4 一次。
 * 上限 `MAX_FILINGS` 份，且**逐份永久快取**（已申報文件不可變）→ 每家公司一次性成本。
 */

export type Bucket = 'open' | 'plan' | 'comp' | 'other'

export interface InsiderTxn {
  /** 交易日（不是申報日） */
  date: string
  filed: string
  accession: string
  owner: string
  ownerCik: string
  title: string
  isOfficer: boolean
  isDirector: boolean
  isTenPercent: boolean
  security: string
  /** SEC 交易代碼：P 買入 / S 賣出 / A 獎勵 / M 行權 / F 扣稅代繳 / G 贈與… */
  code: string
  /** A = 取得，D = 處分 */
  ad: 'A' | 'D' | ''
  shares: number | null
  price: number | null
  value: number | null
  sharesAfter: number | null
  /** true = 依 Rule 10b5-1 預定交易計畫執行 */
  planned: boolean
  derivative: boolean
  bucket: Bucket
  url: string
}

export interface InsiderOfficer {
  owner: string
  ownerCik: string
  title: string
  isOfficer: boolean
  isDirector: boolean
  isTenPercent: boolean
  /** 最近一次申報後的持股（僅直接持有部位；間接持有 SEC 分開申報） */
  sharesAfter: number | null
  lastDate: string
  buys: number
  sells: number
}

export interface InsiderResult {
  company: string
  cik: string
  ticker: string
  /** 讀了幾份 Form 3/4/5 */
  filingsRead: number
  /** submissions 裡總共有幾份（讀取上限之外的沒讀） */
  filingsTotal: number
  fromDate: string
  transactions: InsiderTxn[]
  officers: InsiderOfficer[]
  notes: string[]
}

const MAX_FILINGS = 60
const OWNERSHIP_FORMS = new Set(['3', '4', '5', '3/A', '4/A', '5/A'])

/** 行程內記憶（同一個 serverless 實例會服務很多次請求）。key 已綁最新書號，
 *  所以不需要 TTL —— 有新申報時 key 自然就不同了。 */
const MEMO_MAX = 24
const memo = new Map<string, InsiderResult>()
function memoGet(k: string): InsiderResult | null {
  const v = memo.get(k)
  if (v) { memo.delete(k); memo.set(k, v) }   // 重設插入序 = 最近用到的最後淘汰
  return v ?? null
}
function memoSet(k: string, v: InsiderResult): void {
  memo.set(k, v)
  while (memo.size > MEMO_MAX) memo.delete(memo.keys().next().value as string)
}

const CODE_ZH: Record<string, string> = {
  P: '公開市場買入', S: '公開市場賣出', A: '股權獎勵取得', D: '對發行人處分',
  F: '扣繳稅款交回', M: '選擇權行權', C: '可轉換證券轉換', G: '贈與',
  V: '自願提前申報', X: '選擇權失效前行使', J: '其他取得或處分', K: '權益交換',
}
export function codeZh(code: string): string {
  return CODE_ZH[code] ?? code
}

function tagAll(xml: string, tag: string): string[] {
  const re = new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`, 'g')
  const out: string[] = []
  let m: RegExpExecArray | null
  while ((m = re.exec(xml))) out.push(m[1])
  return out
}
function tag1(xml: string, tag: string): string | null {
  const m = xml.match(new RegExp(`<${tag}(?:\\s[^>]*)?>([\\s\\S]*?)</${tag}>`))
  return m ? m[1] : null
}
/** Form 4 大量欄位長成 `<x><value>…</value></x>`，且可能夾 `<footnoteId/>` */
function valOf(xml: string | null, tag: string): string | null {
  if (!xml) return null
  const inner = tag1(xml, tag)
  if (inner == null) return null
  const v = tag1(inner, 'value')
  const s = (v ?? inner).replace(/<[^>]*>/g, '').trim()
  return s || null
}
function numOf(xml: string | null, tag: string): number | null {
  const s = valOf(xml, tag)
  if (s == null) return null
  const n = Number(s.replace(/,/g, ''))
  return Number.isFinite(n) ? n : null
}
function boolOf(xml: string | null, tag: string): boolean {
  const s = valOf(xml, tag)
  return s === 'true' || s === '1'
}

/**
 * 交易分類。這是本模組最重要的一段 —— 分錯就會把例行的薪酬入帳
 * 講成「內部人大舉買進」。
 */
export function classify(code: string, ad: string, planned: boolean): Bucket {
  if (code === 'P') return 'open'
  if (code === 'S') return planned ? 'plan' : 'open'
  if (code === 'A' || code === 'M' || code === 'F' || code === 'C' || code === 'X') return 'comp'
  if (code === 'G' || code === 'D' || code === 'J' || code === 'K') return 'other'
  return 'other'
}

function parseForm4(xml: string, meta: { accession: string; filed: string; url: string }): InsiderTxn[] {
  const owners = tagAll(xml, 'reportingOwner')
  const first = owners[0] ?? ''
  const rel = tag1(first, 'reportingOwnerRelationship') ?? ''
  const owner = valOf(tag1(first, 'reportingOwnerId'), 'rptOwnerName') ?? '（未載明）'
  const ownerCik = valOf(tag1(first, 'reportingOwnerId'), 'rptOwnerCik') ?? ''
  const isOfficer = boolOf(rel, 'isOfficer')
  const isDirector = boolOf(rel, 'isDirector')
  const isTenPercent = boolOf(rel, 'isTenPercentOwner')
  const title = (valOf(rel, 'officerTitle')
    ?? (isDirector ? '董事' : isTenPercent ? '10% 以上股東' : '')) || ''

  // aff10b5One 是文件層級旗標（不是每筆交易）；沒有這個欄位的舊申報要靠註腳判斷
  const planFlag = (tag1(xml, 'aff10b5One') ?? '').replace(/<[^>]*>/g, '').trim()
  const footnotes = tagAll(xml, 'footnote').join(' ')
  const planned = planFlag === 'true' || planFlag === '1' || /10b5-1/i.test(footnotes)

  const rows: InsiderTxn[] = []
  const push = (t: string, derivative: boolean) => {
    const amounts = tag1(t, 'transactionAmounts')
    const coding = tag1(t, 'transactionCoding')
    const code = (coding ? (tag1(coding, 'transactionCode') ?? '') : '')
      .replace(/<[^>]*>/g, '').trim()
    const ad = (valOf(amounts, 'transactionAcquiredDisposedCode') ?? '') as 'A' | 'D' | ''
    const shares = numOf(amounts, 'transactionShares')
    const price = numOf(amounts, 'transactionPricePerShare')
    const date = valOf(t, 'transactionDate') ?? valOf(t, 'deemedExecutionDate') ?? ''
    rows.push({
      date, filed: meta.filed, accession: meta.accession,
      owner, ownerCik, title, isOfficer, isDirector, isTenPercent,
      security: valOf(t, 'securityTitle') ?? '',
      code, ad, shares, price,
      value: shares != null && price != null ? shares * price : null,
      sharesAfter: numOf(tag1(t, 'postTransactionAmounts'), 'sharesOwnedFollowingTransaction'),
      planned, derivative,
      bucket: classify(code, ad, planned),
      url: meta.url,
    })
  }
  for (const t of tagAll(xml, 'nonDerivativeTransaction')) push(t, false)
  for (const t of tagAll(xml, 'derivativeTransaction')) push(t, true)
  return rows
}

interface RecentBlock {
  accessionNumber: string[]
  filingDate: string[]
  form: string[]
  primaryDocument: string[]
}
interface Submissions {
  name: string
  filings: { recent: RecentBlock }
}

/** `xslF345X06/form4.xml` 是 SEC 的 XSL 轉譯版；去掉前綴才是原始 XML */
function rawDoc(primaryDocument: string): string {
  return primaryDocument.replace(/^xsl[^/]*\//, '')
}

export async function getInsider(ref: CompanyRef, limit = MAX_FILINGS): Promise<InsiderResult> {
  const sub = await secFetchJson<Submissions>(
    `https://data.sec.gov/submissions/CIK${ref.cik10}.json`,
  )
  const r = sub.filings.recent
  const idx: number[] = []
  for (let i = 0; i < r.form.length; i++) if (OWNERSHIP_FORMS.has(r.form[i])) idx.push(i)
  // filings.recent 已依申報日新到舊排序
  const take = idx.slice(0, limit)

  /**
   * 整包結果也快取一份，key 綁「最新一份申報的書號」。
   *
   * 只有逐份快取的話，每次查詢還是要做 30–120 次快取讀取；限速器又是 100ms 序列，
   * 實測 30 份要 3.2 秒 —— 對使用者來說就是「這頁很慢」。綁最新書號之後：
   * 沒有新申報 → 1 次讀取就回；有新申報 → key 變了、重建，但舊的那幾份仍走
   * 逐份快取，只有新的那幾份會真的打 SEC。兩層都要留。
   */
  const aggKey = `form4agg/v1/${ref.cik10}/${limit}/${take.length ? r.accessionNumber[take[0]] : 'none'}.json`
  const memo = memoGet(aggKey)
  if (memo) return memo
  const cached = await cacheGet<InsiderResult>(aggKey)
  if (cached) {
    memoSet(aggKey, cached)
    return cached
  }

  const txns: InsiderTxn[] = []
  const failed: string[] = []
  for (const i of take) {
    const accession = r.accessionNumber[i]
    const nodash = accession.replace(/-/g, '')
    const doc = rawDoc(r.primaryDocument[i] || 'form4.xml')
    const url = `https://www.sec.gov/Archives/edgar/data/${ref.cik}/${nodash}/${doc}`
    const key = `form4/v1/${ref.cik10}/${accession}.json`

    let rows = await cacheGet<InsiderTxn[]>(key)
    if (!rows) {
      try {
        const xml = await secFetchText(url)
        rows = parseForm4(xml, { accession, filed: r.filingDate[i], url })
        await cacheSet(key, rows)
      } catch {
        failed.push(accession)
        continue
      }
    }
    txns.push(...rows)
  }
  txns.sort((a, b) => (a.date < b.date ? 1 : a.date > b.date ? -1 : 0))

  // 高管名冊：同一人取最近一次申報的職稱與持股
  const byOwner = new Map<string, InsiderOfficer>()
  // 持股要**分開追蹤最近一筆非衍生性交易**：董事最後一筆常是 RSU（衍生性），
  // 那個 sharesAfter 是選擇權口數不是股數，混用會把持股寫錯或寫成 n/a
  const holdAt = new Map<string, string>()
  for (const t of txns) {
    const k = t.ownerCik || t.owner
    let o = byOwner.get(k)
    if (!o) {
      o = {
        owner: t.owner, ownerCik: t.ownerCik, title: t.title,
        isOfficer: t.isOfficer, isDirector: t.isDirector, isTenPercent: t.isTenPercent,
        sharesAfter: null, lastDate: '', buys: 0, sells: 0,
      }
      byOwner.set(k, o)
    }
    if (t.date > o.lastDate) {
      o.lastDate = t.date
      if (t.title) o.title = t.title
    }
    if (!t.derivative && t.sharesAfter != null && t.date >= (holdAt.get(k) ?? '')) {
      holdAt.set(k, t.date)
      o.sharesAfter = t.sharesAfter
    }
    if (t.bucket === 'open' && t.ad === 'A') o.buys++
    if (t.bucket !== 'comp' && t.ad === 'D' && (t.code === 'S' || t.code === 'P')) o.sells++
  }
  const officers = [...byOwner.values()].sort((a, b) => (a.lastDate < b.lastDate ? 1 : -1))

  const notes: string[] = []
  if (idx.length > take.length) {
    notes.push(`EDGAR 上共 ${idx.length} 份 Form 3/4/5，本頁只讀最近 ${take.length} 份。`)
  }
  if (failed.length) notes.push(`${failed.length} 份申報的 XML 讀取失敗，已略過。`)

  const out: InsiderResult = {
    company: sub.name || ref.name,
    cik: ref.cik10,
    ticker: ref.ticker,
    filingsRead: take.length,
    filingsTotal: idx.length,
    fromDate: txns.length ? txns[txns.length - 1].date : '',
    transactions: txns,
    officers,
    notes,
  }
  if (!failed.length) {
    // 有讀失敗的就不要存整包 —— 存了會把「這家少幾筆」固定下來
    memoSet(aggKey, out)
    await cacheSet(aggKey, out)
  }
  return out
}
