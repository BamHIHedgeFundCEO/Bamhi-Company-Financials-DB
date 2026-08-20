/**
 * 科目適用性：判斷某個科目對某個產業是不是「本來就沒有」。
 *
 * 三大報表原本只有一種留白（n/a），對讀者說的是「這個數字我抓不到，你自己去查」。
 * 但 JPM 沒有存貨、波克夏沒有毛利、蘋果沒有非利息收入 —— 那不是抓不到，是**不適用**，
 * 查也查不到。分部分頁早就分「n/a」與「—」兩種留白了，三大報表沒分，
 * 所以銀行/控股公司的活頁簿看起來像壞掉（實測 JPM 19.0%、BRK.A 23.8% 的格子是 n/a）。
 *
 * 判準不是手寫的，是 `tools/fsds_coverage.py` 從 SEC DERA Financial Statement
 * Data Sets 全市場 5,542 家離線盤點出來的：某產業有 >=85% 的公司從不申報某科目，
 * 就把該科目對這個產業標為不適用。產物是 `config/concept_applicability.json`。
 *
 * 兩道護欄，拆掉會開始說謊：
 *
 * 1. **只在值本來就缺時才生效。** 這個模組不改任何數字，只改「缺值怎麼寫」。
 *    低分產業裡真的有申報的公司，值早就抓到了，根本走不到這裡。
 * 2. **不知道就不要猜。** SIC 缺失或落在區間外的公司一律維持 n/a。
 *    產表時同樣排除「未分類」「其他」兩桶 —— 實測未分類會產出 26 個不適用
 *    （幾乎整張報表），那是拿「不知道」當「不適用」，會把真缺口洗掉。
 */

interface Applicability {
  version: string
  threshold: number
  sic_groups: { lo: number; hi: number; name: string }[]
  not_applicable: Record<string, string[]>
}

let cached: Applicability | null = null

/** ⚠️ module-level 快取載入後永不失效。改 config 後 dev server 不重啟吃不到 */
async function load(): Promise<Applicability | null> {
  if (cached) return cached
  const raw = await useStorage('assets:config').getItem('concept_applicability.json')
  const parsed = (typeof raw === 'string' ? JSON.parse(raw) : raw) as Applicability | null
  if (!parsed?.not_applicable) return null // 設定檔不在就整個功能靜默關閉，維持 n/a
  cached = parsed
  return cached
}

/** SIC 四碼 → 產業組名。區間有重疊（3674 半導體 ⊂ 3600-3699 電子元件），取最窄的 */
function groupOf(cfg: Applicability, sic?: string): string | null {
  if (!sic || !/^\d+$/.test(sic)) return null
  const n = Number(sic)
  let best: { span: number; name: string } | null = null
  for (const g of cfg.sic_groups) {
    if (n < g.lo || n > g.hi) continue
    const span = g.hi - g.lo
    if (!best || span < best.span) best = { span, name: g.name }
  }
  return best?.name ?? null
}

/**
 * 回傳這家公司「不適用」的科目 id 集合。
 * 判不出產業就回空集合 —— 不知道時一律維持 n/a，寧可多顯示 n/a 也不要假裝不適用。
 */
export async function notApplicableFor(sic?: string): Promise<Set<string>> {
  const cfg = await load()
  if (!cfg) return new Set()
  const g = groupOf(cfg, sic)
  if (!g) return new Set()
  return new Set(cfg.not_applicable[g] ?? [])
}
