/**
 * 科目適用性：判斷某個科目對這家公司是不是「本來就沒有」。
 *
 * 兩層，逐家優先、產業墊底：
 *
 *   1. `company_applicability.json`  這家公司自己的報表上有沒有語意相當的那一行
 *   2. `concept_applicability.json`  同產業有沒有 >=85% 的公司從不申報這個科目
 *
 * 第一層才是準的。產業表解決不了同業裡的異類：波克夏掛 SIC 6331（保險），
 * 但它的資產負債表不分流動／非流動，行為像控股公司；同組多數小型保險公司有分，
 * 所以「流動資產合計」在保險組沒跌破門檻，波克夏那格就繼續寫 n/a。
 * 逐家表直接看波克夏自己的資產負債表 —— 上面沒有那一行，判不適用。
 *
 * 第一層查不到（該公司近四季沒申報、或三張報表看不清楚）才退回第二層。
 * 兩層都查不到就維持 n/a。
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
  structural_threshold: number
  sic_groups: { lo: number; hi: number; name: string }[]
  not_applicable: Record<string, string[]>
  /** 更嚴的一份（同業申報率 < 8%），一律與逐家判斷取聯集，見 notApplicableFor */
  structural: Record<string, string[]>
}

/** 逐家表。體積考量存成索引：concepts 是科目 id 陣列，companies 的值是逗號分隔的索引 */
interface CompanyApplicability {
  version: string
  concepts: string[]
  companies: Record<string, string>
}

let cached: Applicability | null = null
let cachedCo: Map<string, Set<string>> | null = null

/** ⚠️ module-level 快取載入後永不失效。改 config 後 dev server 不重啟吃不到 */
async function load(): Promise<Applicability | null> {
  if (cached) return cached
  const raw = await useStorage('assets:config').getItem('concept_applicability.json')
  const parsed = (typeof raw === 'string' ? JSON.parse(raw) : raw) as Applicability | null
  if (!parsed?.not_applicable) return null // 設定檔不在就整個功能靜默關閉，維持 n/a
  cached = parsed
  return cached
}

/** 逐家表一次全展開成 Map。7 千家 × 平均 6 個科目，建一次約 4 萬個字串，之後都是 O(1) */
async function loadCompany(): Promise<Map<string, Set<string>>> {
  if (cachedCo) return cachedCo
  const m = new Map<string, Set<string>>()
  const raw = await useStorage('assets:config').getItem('company_applicability.json')
  const p = (typeof raw === 'string' ? JSON.parse(raw) : raw) as CompanyApplicability | null
  if (p?.companies && Array.isArray(p.concepts)) {
    for (const [cik, packed] of Object.entries(p.companies)) {
      const ids = packed.split(',').map(i => p.concepts[Number(i)]).filter(Boolean)
      if (ids.length) m.set(cik, new Set(ids))
    }
  }
  cachedCo = m
  return m
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
 *
 *   逐家表收錄了  ->  逐家 ∪ 該產業的 structural
 *   沒收錄        ->  該產業的 not_applicable（較寬的那份）
 *   產業也判不出  ->  空集合，維持 n/a
 *
 * **只有 structural 那份可以蓋過逐家判斷。**一般的 not_applicable（<15%）不行：
 * 逐家表是看這家公司自己的報表判的，產業表是同業統計，衝突時逐家才對。實測用
 * 15% 取聯集會把 JPM／美銀的應收帳款、波克夏的營業成本與毛利寫成「—」，
 * 但那些公司的報表上真的有那一行 —— 那是把真缺口藏起來。
 *
 * structural（<8%）是例外，因為逐家判斷有一個結構性弱點：它靠標籤詞元比對，
 * 對「淨利息收入」這種詞元全是泛用字（interest／income／expense）的科目分不出來。
 * 幾乎每張損益表都有含 income+expense 的標籤，於是 7,093 家裡只有 110 家被判
 * 不適用 —— 整片非金融業的淨利息收入都顯示 n/a。同業申報率 <8% 是壓倒性的統計
 * 證據，這種時候讓產業說了算。8% 那批翻面逐一查證全對，15% 那批就開始說謊。
 *
 * @param cik10 SEC 的十碼零填 CIK。逐家表的 key 是去零的整數字串
 */
export async function notApplicableFor(sic?: string, cik10?: string): Promise<Set<string>> {
  const cfg = await load()
  const g = cfg ? groupOf(cfg, sic) : null
  const per = cik10 ? (await loadCompany()).get(String(Number(cik10))) : undefined
  if (per) {
    const veto = (g && cfg?.structural?.[g]) || []
    return veto.length ? new Set([...per, ...veto]) : per
  }
  if (!cfg || !g) return new Set()
  return new Set(cfg.not_applicable[g] ?? [])
}
