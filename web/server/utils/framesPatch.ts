/**
 * companyfacts 還沒收錄的申報，從離線盤點的 frames 事實補回來（`config/frames_patch.json`，
 * `tools/frames_patch.py` 產生）。執行期零 SEC 請求。
 *
 * SEC 的 companyfacts 是逐家彙整檔，彙整會落後而且會漏：Bunge（BG）2026-07-29 交了 Q2 10-Q，
 * 兩個月後 companyfacts 還停在 Q1，連 2025-09-30 與 2026-03-31 的總資產都沒有。同一份申報的
 * 數字 frames API 已經有了 —— 兩者都是 SEC 從 XBRL 解析出來的結構化資料，數字同源。
 *
 * 併法：同一個標籤、同一個期間、同一份申報書號在 companyfacts 已經有的就不併，其他的照
 * companyfacts 的格式加進去，後面整條管線（優先序、差分、報表上印的標籤…）照原樣跑。
 * **不改共用快取裡的物件**：secFetch 的行程內快取是共用的，這裡一律回傳新物件。
 */

type Row = [string | null, string, number, number] // start, end, val, filings 索引
interface Patch {
  generated?: string
  filings?: [string, string, string][] // 申報書號, 表單別, 申報日
  companies?: Record<string, Record<string, Record<string, Row[]>>>
}
interface Point {
  start?: string
  end: string
  val: number
  fy: number
  fp: string
  form: string
  filed: string
  accn?: string
}
type Tags = Record<string, { units: Record<string, Point[]> }>

let cached: Patch | null = null

async function load(): Promise<Patch> {
  if (!cached) {
    const raw = await useStorage('assets:config').getItem('frames_patch.json')
    cached = ((typeof raw === 'string' ? JSON.parse(raw) : raw) as Patch | null) ?? {}
  }
  return cached
}

/** 盤點表的產生日。Excel 快取 key 要帶它：重跑會補進新的數字，map 版號卻不動 */
export async function framesPatchVersion(): Promise<string> {
  return (await load()).generated ?? ''
}

export async function applyFramesPatch<T extends Tags | undefined>(cik10: string | undefined, gaap: T): Promise<T> {
  if (!cik10) return gaap
  const p = await load()
  const per = p.companies?.[String(Number(cik10))]
  if (!per || !p.filings) return gaap
  const out: Tags = { ...(gaap ?? {}) }
  for (const [tag, units] of Object.entries(per)) {
    const prevUnits = out[tag]?.units ?? {}
    const nextUnits: Record<string, Point[]> = { ...prevUnits }
    for (const [unit, rows] of Object.entries(units)) {
      const existing = prevUnits[unit] ?? []
      const seen = new Set(existing.map((x) => `${x.accn}|${x.start ?? ''}|${x.end}`))
      const added: Point[] = []
      for (const [start, end, val, i] of rows) {
        const f = p.filings[i]
        if (!f) continue
        const [accn, form, filed] = f
        if (seen.has(`${accn}|${start ?? ''}|${end}`)) continue
        // fy/fp 只有 dei 封面股數那條路在用，這裡補的全是 us-gaap
        added.push({ ...(start ? { start } : {}), end, val, accn, form, filed, fy: 0, fp: '' })
      }
      if (added.length) nextUnits[unit] = [...existing, ...added]
    }
    out[tag] = { ...(out[tag] ?? {}), units: nextUnits }
  }
  return out as T
}
