import { secFetchJson, secFetchText } from './secFetch'

/**
 * ticker ↔ CIK 雙向對照。
 * 來源：https://www.sec.gov/files/company_tickers.json（24h 記憶體快取）
 * - CIK 補零至 10 位（data.sec.gov 路徑用）
 * - 同一 CIK 多 ticker（GOOG/GOOGL）：byTicker 每個 ticker 各有一筆；byCik 保留全部 ticker
 * - BRK.A 等特殊字元：SEC 檔案內寫成 BRK-A，查詢時 '.' 與 '-' 互轉
 */

interface SecTickerRow { cik_str: number; ticker: string; title: string }

export interface CompanyRef {
  cik: number
  cik10: string
  ticker: string
  tickers: string[]
  name: string
}

let byTicker: Map<string, CompanyRef> | null = null
let loadedAt = 0
const TTL = 24 * 3600 * 1000

export function pad10(cik: number): string {
  return String(cik).padStart(10, '0')
}

async function load(): Promise<Map<string, CompanyRef>> {
  if (byTicker && Date.now() - loadedAt < TTL) return byTicker
  const raw = await secFetchJson<Record<string, SecTickerRow>>(
    'https://www.sec.gov/files/company_tickers.json',
  )
  const rows = Object.values(raw)
  const cikTickers = new Map<number, string[]>()
  for (const r of rows) {
    const list = cikTickers.get(r.cik_str) ?? []
    list.push(r.ticker)
    cikTickers.set(r.cik_str, list)
  }
  const map = new Map<string, CompanyRef>()
  for (const r of rows) {
    map.set(r.ticker.toUpperCase(), {
      cik: r.cik_str,
      cik10: pad10(r.cik_str),
      ticker: r.ticker.toUpperCase(),
      tickers: cikTickers.get(r.cik_str)!,
      name: r.title,
    })
  }
  byTicker = map
  loadedAt = Date.now()
  return map
}

/**
 * 名冊漏收時的退路。
 *
 * SEC 的 ticker 名冊會漏 —— 實測 `company_tickers.json` 與
 * `company_tickers_exchange.json` **都沒有 AEP**（American Electric Power，
 * 仍在 Nasdaq 交易、1005 筆申報、submissions 自己的 `tickers` 欄就寫著 AEP）。
 * 名冊漏一家就整家查不到，所以改問 EDGAR 的公司查詢：它吃 ticker、回 atom，
 * 裡面有 CIK 與正式名稱。
 *
 * 只在名冊查不到時才打，正常查詢的請求數不變。查無此 ticker 時 EDGAR 回 503
 * 而不是 404，所以一律把例外當作查無。結果（含查無）快取 24h，避免使用者
 * 打錯字就反覆去問。
 *
 * 這裡的 `name` 只是備援 —— 端點顯示的是 companyfacts 的 `entityName`
 * （見 `financials.ts`），所以不必為了名字再去查 submissions。
 *
 * ⚠️ 擋不掉已下市的公司。SEC 沒有可靠的上市狀態：實測 JHG、NSA 仍在交易，
 * submissions 的 `tickers` / `exchanges` 卻都是空的，拿它當判準會誤殺。四家
 * 名冊漏收的公司也都還在按時申報，「有沒有在申報」同樣分不出來。
 */
const fallback = new Map<string, { at: number; ref: CompanyRef | null }>()

async function resolveViaEdgar(ticker: string): Promise<CompanyRef | null> {
  const hit = fallback.get(ticker)
  if (hit && Date.now() - hit.at < TTL) return hit.ref

  let ref: CompanyRef | null = null
  try {
    const xml = await secFetchText(
      'https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany'
        + `&ticker=${encodeURIComponent(ticker)}&type=10-K&count=1&output=atom`,
    )
    const cik = Number(/<cik>(\d+)<\/cik>/.exec(xml)?.[1])
    if (cik) {
      ref = {
        cik,
        cik10: pad10(cik),
        ticker,
        tickers: [ticker],
        name: /<conformed-name>([^<]+)<\/conformed-name>/.exec(xml)?.[1]?.trim() || ticker,
      }
    }
  } catch {
    // 查無此 ticker → EDGAR 回 503；網路錯誤也走這裡，兩者都當查無
  }
  fallback.set(ticker, { at: Date.now(), ref })
  return ref
}

/** 查無回 null（呼叫端負責給明確錯誤訊息，不回空陣列了事） */
export async function resolveTicker(input: string): Promise<CompanyRef | null> {
  const map = await load()
  const t = input.trim().toUpperCase()
  // BRK.A → SEC 檔內為 BRK-A；也接受使用者直接輸入 BRK-A
  return map.get(t) ?? map.get(t.replace(/\./g, '-')) ?? map.get(t.replace(/-/g, '.'))
    ?? (await resolveViaEdgar(t))
}

/* ── 繼任發行人（successor issuer） ─────────────────────────────────────── */

const ANNUAL_FORMS = ['10-K', '20-F', '40-F']
const MAX_CANDIDATES = 3

/** 只取判定要用的兩個欄位，避免 import filings.ts 造成循環相依 */
interface SubsLite {
  name: string
  /** SEC 認定的掛牌代號。合併申報裡的營運合夥／子公司這裡是空陣列 */
  tickers?: string[]
  filings: { recent: { form: string[]; accessionNumber: string[] } }
}

function subsOf(cik10: string): Promise<SubsLite> {
  return secFetchJson<SubsLite>(`https://data.sec.gov/submissions/CIK${cik10}.json`)
}

function hasAnnual(forms: string[]): boolean {
  return forms.some((f) => ANNUAL_FORMS.some((a) => f.startsWith(a)))
}

/**
 * 控股公司重組後把 ticker 指回前身 CIK。
 *
 * 重組（2026 年的 XOM 就是）會開一個全新 CIK 繼承 ticker：新實體只有一兩份
 * 10-Q、一份年報都沒有，companyfacts 也只剩 94 個科目 / 2 年，而前身 CIK 有
 * 438 個科目 / 18 年。SEC 的 ticker 對照表指向新 CIK 沒有錯，但使用者要的是
 * 這家公司的完整歷史，所以三大報表與分部都應該走前身。
 *
 * 申報書號前 10 碼是遞交者 CIK —— 但那通常是申報代理商（XOM 那份 8-K12B 的
 * 前綴就是 Donnelley），所以只當候選，必須再驗證：
 *   1. 現行 CIK 一份年報都沒有
 *   2. 候選 CIK 自己有年報，且「同一個申報書號」也列在它名下
 * 條件 2 只有合併申報的共同登記人成立，代理商不會（它們根本沒有 submissions）。
 *
 * 找不到就回 null，呼叫端沿用原本的 ref —— 剛 IPO 的年輕公司會走到這裡，
 * 保持現狀比亂猜好。
 */
export async function predecessorOf(ref: CompanyRef): Promise<CompanyRef | null> {
  let sub: SubsLite
  try {
    sub = await subsOf(ref.cik10)
  } catch {
    return null
  }
  const recent = sub.filings.recent
  if (hasAnnual(recent.form)) return null

  const tried = new Set<string>([ref.cik10])
  for (let i = 0; i < recent.form.length && tried.size <= MAX_CANDIDATES; i++) {
    // 只看定期報告：8-K 那類多半由代理商遞交，前綴沒有參考價值
    if (!recent.form[i].startsWith('10-')) continue
    const acc = recent.accessionNumber[i]
    const cand = acc.slice(0, 10) // 前 10 碼即補零後的 CIK
    if (tried.has(cand)) continue
    tried.add(cand)
    try {
      const cs = await subsOf(cand)
      if (!hasAnnual(cs.filings.recent.form)) continue
      if (!cs.filings.recent.accessionNumber.includes(acc)) continue
      return {
        cik: Number(cand),
        cik10: cand,
        ticker: ref.ticker,
        tickers: ref.tickers,
        name: cs.name || ref.name,
      }
    } catch {
      // 候選不是發行人（代理商沒有 submissions）→ 試下一個
    }
  }
  return null
}

/**
 * SEC 名冊把代號指到「不掛牌的共同申報人」時，換回真正掛牌的那個實體。
 *
 * 2026-08 實測：`company_tickers.json` 與 `company_tickers_exchange.json` **都**把
 * EQR 指向 ERP Operating LP（CIK 931182）。那是 Equity Residential（更名為 Vivmark
 * Residential，CIK 906107）底下的營運合夥 —— 兩者一直是合併申報，但只有母公司在標
 * XBRL，931182 的 companyfacts 停在 2015 年。照名冊走的話整個頁面一格數字都沒有。
 *
 * 判準用 SEC 自己的欄位，不猜：`submissions.tickers` 空陣列＝這個實體沒有掛牌代號。
 * 成立時才去翻最新一份 10-K/10-Q 的**申報表頭**（列出所有共同申報人的結構化檔，
 * 不是財報 HTML），挑出自報有這個代號的那一個。
 *
 * 只在極少數情況觸發，多花 2 次請求，submissions 有 24h 快取。
 */
export async function listedSiblingOf(ref: CompanyRef): Promise<CompanyRef | null> {
  let sub: SubsLite
  try {
    sub = await subsOf(ref.cik10)
  } catch {
    return null
  }
  if (sub.tickers?.length) return null // SEC 自己說它有掛牌 → 名冊沒指錯

  const recent = sub.filings.recent
  const i = recent.form.findIndex((f) => f.startsWith('10-'))
  if (i < 0) return null
  const acc = recent.accessionNumber[i]
  const bare = acc.replace(/-/g, '')
  let header: string
  try {
    header = await secFetchText(
      `https://www.sec.gov/Archives/edgar/data/${ref.cik}/${bare}/${acc}-index-headers.html`,
    )
  } catch {
    return null
  }

  const want = ref.ticker.toUpperCase()
  for (const m of header.matchAll(/CENTRAL INDEX KEY:\s*(\d{10})/g)) {
    const cand = m[1]
    if (cand === ref.cik10) continue
    try {
      const cs = await subsOf(cand)
      if (!cs.tickers?.some((t) => t.toUpperCase() === want)) continue
      return {
        cik: Number(cand),
        cik10: cand,
        ticker: ref.ticker,
        tickers: cs.tickers!,
        name: cs.name || ref.name,
      }
    } catch {
      // 候選抓不到 submissions → 試下一個
    }
  }
  return null
}

/**
 * 端點統一入口：解析 ticker 並在偵測到控股公司重組時改指前身 CIK。
 * submissions 有 24h 行程內快取（見 secFetch），重組是極少數，
 * 正常公司實際只多一次 submissions 請求，且後續查詢直接命中快取。
 */
export async function resolveCompany(input: string): Promise<CompanyRef | null> {
  const ref = await resolveTicker(input)
  if (!ref) return null
  const listed = (await listedSiblingOf(ref)) ?? ref
  return (await predecessorOf(listed)) ?? listed
}
