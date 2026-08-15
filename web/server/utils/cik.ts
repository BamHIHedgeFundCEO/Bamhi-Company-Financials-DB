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
