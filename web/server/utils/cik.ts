import { secFetchJson } from './secFetch'

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

/** 查無回 null（呼叫端負責給明確錯誤訊息，不回空陣列了事） */
export async function resolveTicker(input: string): Promise<CompanyRef | null> {
  const map = await load()
  const t = input.trim().toUpperCase()
  // BRK.A → SEC 檔內為 BRK-A；也接受使用者直接輸入 BRK-A
  return map.get(t) ?? map.get(t.replace(/\./g, '-')) ?? map.get(t.replace(/-/g, '.')) ?? null
}

/* ── 繼任發行人（successor issuer） ─────────────────────────────────────── */

const ANNUAL_FORMS = ['10-K', '20-F', '40-F']
const MAX_CANDIDATES = 3

/** 只取判定要用的兩個欄位，避免 import filings.ts 造成循環相依 */
interface SubsLite {
  name: string
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
 * 端點統一入口：解析 ticker 並在偵測到控股公司重組時改指前身 CIK。
 * submissions 有 24h 行程內快取（見 secFetch），重組是極少數，
 * 正常公司實際只多一次 submissions 請求，且後續查詢直接命中快取。
 */
export async function resolveCompany(input: string): Promise<CompanyRef | null> {
  const ref = await resolveTicker(input)
  if (!ref) return null
  return (await predecessorOf(ref)) ?? ref
}
