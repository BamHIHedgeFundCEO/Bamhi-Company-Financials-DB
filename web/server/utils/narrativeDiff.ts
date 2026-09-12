/**
 * 年報敘述章節的逐年對比：今年這份 10-K 的小標，跟去年那份比。
 *
 * **比的是小標，不是全文。**理由有兩層：
 *
 *   1. 風險因子章節的價值本來就全在小標 —— 每一條風險的標題自己就是完整的一句話
 *      （"Our business depends on a limited number of customers"）。新增一條風險，
 *      就是多一個小標；拿掉一條，就是少一個小標。這是分析師本來就在做的事
 *   2. 散文的逐字 diff 會被改寫淹沒。同一段話換個語序、把 "approximately" 改成
 *      "about"、把去年的數字換成今年的，逐字比會全部標成「有變動」——
 *      那等於什麼都沒說。而數字本來就不該從這裡讀（見 CLAUDE.md 的硬規則）
 *
 * 三件刻意不做的事：
 *
 *   - **不做全文 diff**，也不宣稱自己是。`narrative.ts` 抽出來的 `paragraphs`
 *     本來就是節錄（風險 6 段、其他 14 段），拿節錄去比會生出假的「新增」——
 *     那一段去年可能就在，只是沒被選進節錄。所以段落層級只在
 *     「兩年抽到的是同一個小節」時才比，而且標明是節錄對照
 *   - **不判斷變動是好是壞。**新增一條風險不代表公司變差（可能只是律師把既有的
 *     風險拆成兩條），刪掉一條也不代表風險消失。頁面只說「這裡不一樣」
 *   - **不碰數字。**比對只看小標文字，抽出來的段落原文顯示、不轉數值
 *
 * ⚠️ `narrative.ts` 的小標上限是 120 條（JPM 這種風險章節有 269 段的公司會被切）。
 * 被切到的話，尾端那幾條在「去年有、今年沒有」與「超過上限沒收進來」之間分不出來
 * —— 那時一律只給數量摘要、不列清單，並在頁面寫明原因。少一個守門員，
 * 這一頁就會開始報假的「新刪除的風險」。
 */
import type { Narrative, Section } from './narrative'

/** narrative.ts 的 `headings.slice(0, 120)`。兩邊要一起改，所以寫成共用常數的替身 */
export const HEADING_CAP = 120

/** 比對用的正規化：大小寫、標點、條列編號、多餘空白都不算差異 */
export function normHeading(s: string): string {
  return s
    .toLowerCase()
    .replace(/[‘’“”]/g, "'")
    .replace(/^\s*(?:\(?[a-z0-9]{1,3}[.)]|[•·▪—–-])\s+/, '')   // 前導編號／項目符號
    .replace(/[^a-z0-9'\s]/g, ' ')
    .replace(/\s+/g, ' ')
    .trim()
}

/** 這些字在風險小標裡幾乎每條都有，留著會讓任兩條的相似度虛高 */
const STOP = new Set([
  'the', 'a', 'an', 'and', 'or', 'of', 'to', 'in', 'on', 'for', 'with', 'by', 'as',
  'our', 'we', 'us', 'may', 'could', 'can', 'will', 'would', 'be', 'is', 'are', 'that',
  'which', 'if', 'it', 'its', 'their', 'from', 'at', 'have', 'has', 'not', 'any',
])

function tokens(s: string): string[] {
  return normHeading(s).split(' ').filter((w) => w.length > 1 && !STOP.has(w))
}

/**
 * Dice 係數（兩倍交集 ÷ 兩邊字數和），只看實詞。
 *
 * 用集合而不是逐字 diff，是因為小標的改寫多半是語序調整與同義替換
 * （"Cybersecurity incidents could disrupt our operations" →
 *  "Disruptions from cybersecurity incidents"）。逐字比會判成兩條不同的風險，
 * 詞集合比則認得出是同一條。
 */
/**
 * 開頭連續相同的實詞數。
 *
 * 律師改寫風險小標時幾乎都是從中段開始改，開頭那一句主詞不動 —— JPM 實測
 * 「JPMorganChase's businesses are highly regulated and are significantly affected by…」
 * 對上去年的「JPMorganChase's businesses are highly regulated, and the laws, rules and
 * regulations that apply to…」，詞集合相似度只有 0.31（後半整段換掉），
 * 不補這一條的話同一條風險會同時被報成「新增一條」加「刪除一條」。
 */
function leadingMatch(a: string[], b: string[]): number {
  let n = 0
  while (n < a.length && n < b.length && a[n] === b[n]) n++
  return n
}

/** 開頭連續 4 個實詞相同就視為同一條（改寫），不管後半換得多兇 */
const LEAD_SAME = 4

export function similarity(a: string, b: string): number {
  const ta = tokens(a)
  const tb = tokens(b)
  if (!ta.length || !tb.length) return 0
  if (leadingMatch(ta, tb) >= LEAD_SAME) {
    // 給 0.6：穩穩地落在「配得上」與「幾乎沒改」之間 → 一定會被歸到「改寫」
    return Math.max(0.6, dice(ta, tb))
  }
  return dice(ta, tb)
}

function dice(ta: string[], tb: string[]): number {
  const bag = new Map<string, number>()
  for (const w of tb) bag.set(w, (bag.get(w) ?? 0) + 1)
  let hit = 0
  for (const w of ta) {
    const n = bag.get(w) ?? 0
    if (n > 0) { hit++; bag.set(w, n - 1) }
  }
  return (2 * hit) / (ta.length + tb.length)
}

/**
 * 表格的欄位標題會被粗體偵測當成小標收進來（AAPL 的 MD&A 抓到
 * `2025 Change 2024 Change 2023`）。它們每年都會變（因為年份會變），
 * 逐年對比時會穩定地生出一條假的「新增」加一條假的「刪除」。
 *
 * 判準是「這串字裡有沒有話」：至少兩個實詞，而且數字不能佔掉四成以上的字元。
 * 這一步只影響對比，不影響 `narrative.ts` 抽出來的原始小標清單（頁面照舊全列）。
 */
/**
 * 表格的「家具」：期間標題與變動率欄位。它們**每年都會變**（因為年份就寫在裡面），
 * 逐年對比時會穩定地生出一條假新增加一條假刪除 —— 實測 GOOGL 的 MD&A 抓到
 * `Year Ended December 31, 2024` 與 `% Change from Prior Period`。
 * 只擋開頭就是期間／變動率的那種，不擋內文裡剛好提到年份的小標。
 */
const TABLE_FURNITURE = [
  /^(as\s+of|for\s+the\s+)?(year|years|quarter|three|six|nine|twelve)\s+(months?\s+)?(ended|ending)\b/i,
  /^%\s*change/i,
  /^\(in\s+(thousands|millions|billions)/i,   // BW 的 `(in thousands) 2025 2024 $ Change`
  /^change\s+from\s+prior/i,
  /^(increase|decrease)\s*\(decrease\)?\s*$/i,
]

/**
 * 表格被剝成一行時會變成「同一串字重複兩次」：JPM 的
 * `Total Firm 318,512 Total Firm 318,512`（欄位名＋數字，左右兩欄）、
 * GOOGL 的 `Shares Amount Shares Amount`。數字佔比不夠高、實詞也夠兩個，
 * 前面兩道濾網都攔不下來，但它每年都會變（數字會變）。
 */
function isRepeatedFragment(s: string): boolean {
  const t = tokens(s)
  if (t.length < 4 || t.length % 2 !== 0) return false
  const half = t.length / 2
  return t.slice(0, half).join(' ') === t.slice(half).join(' ')
}

export function isRealHeading(s: string): boolean {
  if (TABLE_FURNITURE.some((re) => re.test(s.trim()))) return false
  if (isRepeatedFragment(s)) return false
  const digits = (s.match(/\d/g) ?? []).length
  if (digits / Math.max(1, s.replace(/\s/g, '').length) > 0.4) return false
  const t = tokens(s)
  return t.length >= 2 && t.some((w) => w.length >= 3 && /^[a-z']+$/.test(w))
}

/** 相同（只差標點大小寫）以上不算變動；低於這條就當成兩條不同的小標 */
const SAME = 0.95
const MATCH = 0.55

export interface HeadingPair {
  /** 今年的小標 */
  to: string
  /** 去年的對應小標 */
  from: string
  /** 0–1，越高越像 */
  sim: number
  /** 今年這一條的繁中譯文（離線批次產物，沒翻到就是空字串） */
  zh?: string
}

export interface HeadingDiff {
  curTotal: number
  prevTotal: number
  /** 今年有、去年找不到對應的 */
  added: { text: string; zh?: string }[]
  /** 去年有、今年找不到對應的 */
  removed: { text: string }[]
  /** 配對到但字面改過的 */
  reworded: HeadingPair[]
  /** 配對到且幾乎一字不差的條數 */
  kept: number
  /**
   * 任一年的小標數量碰到 120 上限 → 尾端被切掉，
   * added／removed 分不出「真的變動」與「超過上限」→ 一律清空，只留數量摘要
   */
  capped: boolean
  /**
   * 兩年抽出來的小標數量差 50% 以上。
   *
   * 這是「抽取結果不一樣」的指紋，不一定是年報改了：TSM 的 20-F 實測今年抽到 56 條、
   * 去年只有 21 條，而且 35 條全部算「新增」、刪除 0 條 —— 公司不會一年之間
   * 多寫 35 條風險又一條都沒刪，那是去年那份的粗體標記沒被辨識出來。
   * 清單照列（真的改組時它也會亮），但頁面必須先說「這個差異可能來自抽取」。
   */
  asymmetric: boolean
}

/**
 * 一對一配對。貪婪法：把所有可能的配對按相似度由高到低排，
 * 依序吃掉還沒被用過的兩端。
 *
 * 不用「每條各自取最像的」是因為那會多對一：兩條今年的小標可能都最像去年的同一條，
 * 於是去年那條被用兩次，剩下的那條被誤報成「新增」。
 */
export function diffHeadings(cur0: string[], prev0: string[], curZh0?: string[],
                             filter = true): HeadingDiff {
  const capped = cur0.length >= HEADING_CAP || prev0.length >= HEADING_CAP
  // 先濾掉表格欄位標題那種「沒有話的字串」，再配對（見 isRealHeading）。
  // 譯文是逐項對齊的，濾的時候要一起帶走，不然整排錯位
  const keep = cur0.map((t, i) => i).filter((i) => !filter || isRealHeading(cur0[i]!))
  const cur = keep.map((i) => cur0[i]!)
  const curZh = curZh0 ? keep.map((i) => curZh0[i] ?? '') : undefined
  const prev = filter ? prev0.filter(isRealHeading) : prev0

  const cand: { i: number; j: number; s: number }[] = []
  for (let i = 0; i < cur.length; i++) {
    for (let j = 0; j < prev.length; j++) {
      const s = similarity(cur[i]!, prev[j]!)
      if (s >= MATCH) cand.push({ i, j, s })
    }
  }
  cand.sort((a, b) => b.s - a.s)

  const usedCur = new Set<number>()
  const usedPrev = new Set<number>()
  const reworded: HeadingPair[] = []
  let kept = 0
  for (const c of cand) {
    if (usedCur.has(c.i) || usedPrev.has(c.j)) continue
    usedCur.add(c.i)
    usedPrev.add(c.j)
    if (c.s >= SAME || normHeading(cur[c.i]!) === normHeading(prev[c.j]!)) kept++
    else reworded.push({ to: cur[c.i]!, from: prev[c.j]!, sim: Math.round(c.s * 100) / 100, zh: curZh?.[c.i] || undefined })
  }

  const added = cur.map((t, i) => ({ t, i })).filter((x) => !usedCur.has(x.i))
    .map((x) => ({ text: x.t, zh: curZh?.[x.i] || undefined }))
  const removed = prev.filter((_, j) => !usedPrev.has(j)).map((t) => ({ text: t }))

  const hi = Math.max(cur.length, prev.length)
  const lo = Math.min(cur.length, prev.length)
  return {
    curTotal: cur.length,
    prevTotal: prev.length,
    added: capped ? [] : added,
    removed: capped ? [] : removed,
    reworded: reworded.sort((a, b) => a.sim - b.sim).slice(0, 20),
    kept,
    capped,
    asymmetric: lo > 0 && hi >= lo * 1.5,
  }
}

export interface ParaDiff {
  /** 兩年抽到的是不是同一個小節。false 時不做逐段比對 */
  sameFocus: boolean
  curFocus: string | null
  prevFocus: string | null
  /** 今年節錄裡、去年節錄找不到對應的段落 */
  added: { text: string; zh?: string }[]
  /** 配對到但改寫過的（附去年的版本） */
  reworded: HeadingPair[]
  /** 幾乎一字不差沿用的段數 —— 這個數字本身就是訊息：照抄＝今年沒有新話要說 */
  kept: number
}

/**
 * 段落層級**只在兩年抽到同一個小節時**才比。
 *
 * `narrative.ts` 的段落是節錄（風險 6 段、其他 14 段），而節錄是「從哪個小節開始取」
 * 決定的。兩年取到不同小節的話，逐段比對出來的「新增」其實是「換了一段來節錄」，
 * 那是我們自己的取樣造成的，不是公司改了年報。
 */
export function diffParagraphs(cur: Section, prev: Section, curZh?: string[]): ParaDiff {
  // 兩年都沒找到總覽小節（focus 都是 null）時，比的是「同一條取樣規則取到的章節開頭」
  // —— 那仍然是可比的，但頁面必須說清楚是這種情況，不能寫成「同一個小節」
  const sameFocus = normHeading(cur.focus ?? '') === normHeading(prev.focus ?? '')
  if (!sameFocus) {
    return {
      sameFocus: false,
      curFocus: cur.focus ?? null,
      prevFocus: prev.focus ?? null,
      added: [], reworded: [], kept: 0,
    }
  }
  // 段落不套 isRealHeading：那個濾網是為表格欄位標題寫的，正常的散文段落一定過得了，
  // 但「這一段只有一個實詞」的短句（"Not applicable."）是真的內容，不該被丟掉
  const d = diffHeadings(cur.paragraphs, prev.paragraphs, curZh, false)
  return {
    sameFocus: true,
    curFocus: cur.focus ?? null,
    prevFocus: prev.focus ?? null,
    added: d.added,
    reworded: d.reworded,
    kept: d.kept,
  }
}

export interface SectionDiff {
  id: Section['id']
  zh: string
  anchor: string
  headings: HeadingDiff
  paragraphs: ParaDiff | null
  notes: string[]
}

export function diffNarrative(cur: Narrative, prev: Narrative): SectionDiff[] {
  const out: SectionDiff[] = []
  for (const cs of cur.sections) {
    const ps = prev.sections.find((x) => x.id === cs.id)
    const notes: string[] = []
    if (!ps) {
      notes.push(`去年那份 ${prev.form} 抽不到這個章節，無法對比。`)
      out.push({
        id: cs.id, zh: cs.zh, anchor: cs.anchor, notes,
        headings: { curTotal: cs.headings.length, prevTotal: 0, added: [], removed: [],
          reworded: [], kept: 0, capped: false, asymmetric: false },
        paragraphs: null,
      })
      continue
    }
    const headings = diffHeadings(cs.headings, ps.headings, cs.headingsZh)
    if (headings.capped) {
      notes.push(`小標數量達到 ${HEADING_CAP} 條上限（今年 ${headings.curTotal}、去年 ${headings.prevTotal}），`
        + '尾端可能被切掉 —— 這種情況下「新增」與「刪除」分不出是真的變動還是超過上限，'
        + '所以只給數量與改寫清單，不列新增／刪除。')
    }
    if (headings.asymmetric && !headings.capped) {
      notes.push(`兩年抽出來的小標數量差很多（今年 ${headings.curTotal} 條、去年 ${headings.prevTotal} 條）。`
        + '年報本身當然可能改組，但這個形狀也常常是**抽取結果不一樣**造成的'
        + '（某一年的粗體標記沒被辨識出來）。下面的清單照列，但請當成線索而不是結論，'
        + '對得上不對得上以 EDGAR 原文為準。')
    }
    const paragraphs = cs.id === 'mdna' ? diffParagraphs(cs, ps, cs.paragraphsZh) : null
    if (paragraphs?.sameFocus && !paragraphs.curFocus) {
      notes.push('兩年都沒有找到「總覽」之類的小節，所以段落比對的是**章節開頭的前幾段**'
        + '（同一條取樣規則、兩年各取一次）。這仍然可比，但它不是整個 MD&A 的比對。')
    }
    if (paragraphs && !paragraphs.sameFocus) {
      notes.push('兩年的節錄不是同一個小節'
        + `（今年「${paragraphs.curFocus ?? '章節開頭'}」、去年「${paragraphs.prevFocus ?? '章節開頭'}」），`
        + '逐段比對會把「換了一段來節錄」誤報成「公司改了年報」，所以這一段不做段落比對。')
    }
    out.push({ id: cs.id, zh: cs.zh, anchor: cs.anchor, headings, paragraphs, notes })
  }
  return out
}
