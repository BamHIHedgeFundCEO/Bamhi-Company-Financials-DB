import { secFetchTextLimited } from './secFetch'
import { cacheGet, cacheSet } from './blobCache'

/**
 * 10-K 的敘述性章節（Item 1 業務、Item 1A 風險、Item 7 MD&A）。
 *
 * ⚠️ 這是唯一會解析財報 HTML 的模組，且**只取散文、絕不取數字**。
 * 三大報表的每一個數字仍然一律來自 companyfacts；本模組抽出來的文字不會
 * 進入任何計算、不會被轉成數值、也不會出現在 Excel。
 *
 * 為什麼不用 LLM：這四段本來就是人寫的散文，原文直出 + 章節錨點 + EDGAR
 * 連結就已經是正確答案。翻譯與濃縮是加值層，而那正好是唯一會說謊的一層。
 *
 * 成本：每份 10-K 一次請求，解析結果永久快取（已申報文件不可變）。
 */

const MAX_HTML = 24 * 1024 * 1024
/**
 * 粗體標記。剝標籤會把粗體資訊一起剝掉，但風險因子的小標**就是**粗體，
 * 所以在剝的過程中先留下這個記號，段落化時再拿掉。
 *
 * ⚠️ 這個字元**必須是 `\s` 匹配得到的空白**（垂直定位字元 U+000B），不能用
 * `` 之類的控制字元。Alphabet 的 10-K 標題長成 `ITEM 1.<粗體>BUSINESS`，
 * 記號正好卡在「Item 1.」與「Business」中間 —— 記號不算空白的話
 * `item\s*1\s*\.?\s*business` 這個式子永遠比對不到，整份年報三個章節全部抓不到。
 * 申報文件本身不會出現 U+000B，所以拿它當記號不會誤判。
 */
const B = ''

export interface Section {
  id: 'business' | 'risk' | 'mdna'
  /** 例如 'Item 1A. Risk Factors' */
  anchor: string
  zh: string
  /** 段落（已去標籤、已合併換行） */
  paragraphs: string[]
  /** 粗體小標（風險章節就是各項風險的標題） */
  headings: string[]
  /** 繁中譯文，與上面兩個陣列**逐項對齊**（同索引同一段）。
   *  沒翻到的位置是空字串 —— 前端那一項就顯示英文原文，不是留白 */
  paragraphsZh?: string[]
  headingsZh?: string[]
  /** MD&A 專用：節錄的是哪一個小節（例如 "Executive Overview"）。null = 退回開頭幾段 */
  focus?: string | null
  /** true = 這份 10-K 沒有 `Item N.` 錨點，是靠標題比對 + 內容驗證救回來的 */
  viaTitle?: boolean
  /** 原章節的字元數（節錄前）—— 讓讀者知道自己看的是多大一份的節錄 */
  chars: number
  truncated: boolean
}

export interface Narrative {
  form: string
  accession: string
  reportDate: string
  filedDate: string
  url: string
  sections: Section[]
  notes: string[]
  /** 繁中譯文的來源標記（`config/narrative_zh/`，離線批次產物）。null = 這份沒有譯文 */
  translator?: string | null
  translatedAt?: string | null
}

/* ── HTML → 帶粗體標記的純文字 ──────────────────────────── */
const BOLD_OPEN = /<(b|strong)\b[^>]*>/gi
const BOLD_CLOSE = /<\/(b|strong)\s*>/gi
const BLOCK = /<\/(p|div|tr|li|h[1-6]|table|section)\s*>/gi
/** 行內標籤要**刪掉**、不能換成空白。iXBRL 常把單字切成兩半包在不同 span 裡
 *  （波克夏的 `Busines<span…>s Description`），換成空白就再也比對不到章節標題。 */
const INLINE = /<\/?(span|b|strong|i|em|u|font|a|sup|sub|nobr|ix:[a-z]+)\b[^>]*>/gi

function decodeEntities(s: string): string {
  return s
    .replace(/&nbsp;|&#160;|&#xa0;/gi, ' ')
    .replace(/&amp;|&#38;/gi, '&')
    .replace(/&lt;|&#60;/gi, '<')
    .replace(/&gt;|&#62;/gi, '>')
    .replace(/&quot;|&#34;/gi, '"')
    .replace(/&apos;|&#39;|&rsquo;|&#8217;/gi, "'")
    .replace(/&ldquo;|&rdquo;|&#8220;|&#8221;/gi, '"')
    .replace(/&mdash;|&#8212;/gi, '—')
    .replace(/&ndash;|&#8211;/gi, '–')
    .replace(/&#(\d+);/g, (_, d) => String.fromCharCode(Number(d)))
    .replace(/&[a-z]+;/gi, ' ')
}

export function htmlToText(html: string): string {
  let s = html
    .replace(/<\?xml[\s\S]*?\?>/gi, ' ')
    .replace(/<!--[\s\S]*?-->/g, ' ')
    .replace(/<(script|style)\b[\s\S]*?<\/\1\s*>/gi, ' ')
    // iXBRL 的隱藏事實區塊：內容不會顯示給人看，留著只會污染章節定位
    .replace(/<[^>]*style=["'][^"']*display\s*:\s*none[^"']*["'][\s\S]*?<\/(div|span)\s*>/gi, ' ')
  // 以 style 表示的粗體也要算（現代 iXBRL 幾乎不用 <b>）
  s = s.replace(/<(p|span|div|font|td|li)\b([^>]*)>/gi, (m, tag, attrs) =>
    /font-weight\s*:\s*(bold|[6-9]00)/i.test(attrs) ? `${B}${m}` : m)
  s = s.replace(BOLD_OPEN, B).replace(BOLD_CLOSE, B)
  s = s.replace(BLOCK, '\n')
  s = s.replace(/<br\s*\/?>/gi, '\n')
  s = s.replace(INLINE, '')
  s = s.replace(/<[^>]+>/g, ' ')
  s = decodeEntities(s)
  // 落在單字中間的粗體記號拿掉（同上，被 span 切成兩半的字要接回去）
  s = s.replace(new RegExp(`(\\w)${B}+(\\w)`, 'g'), '$1$2')
  s = s.replace(/[ \t ]+/g, ' ').replace(/ *\n */g, '\n').replace(/\n{3,}/g, '\n\n')
  return s.trim()
}

/* ── 章節定位 ───────────────────────────────────────────
   目錄與正文用同一串字（"Item 1A. Risk Factors" 至少出現兩次），
   所以不能取第一個匹配。做法：每個「起點候選 × 之後最近的終點」都算一遍，
   **取內容最長的那一段** —— 目錄裡的兩個標題彼此相鄰，長度必然很短。 */
interface ItemSpec {
  id: Section['id']; zh: string; anchor: string; start: RegExp; ends: RegExp[]
  /** 找不到時的補充說明（有些公司是合法地併入附件，不是解析失敗） */
  missingHint?: string
  /**
   * 起點必須是粗體行。**20-F 專用**，因為「取最長」那一招在 20-F 上會反過來咬人：
   * 目錄的 `D. Risk Factors` 之後隔幾十字元就是目錄的 `ITEM 4.`，`s + 30` 的門檻
   * 正好跳過它，於是終點落到正文的 Item 4 —— 這一段（目錄＋Item 1~3 全部）
   * 比正文的風險章節還長，最長者勝就選了目錄（GLOB／NU／ONON／PAGS／ASML 實測）。
   * 十份 20-F 裡正文標題**全部是粗體、目錄列全部不是**，用排版事實切開最乾淨。
   */
  boldStart?: boolean
}

/**
 * `Item 1A. Risk Factors` 這個錨點，各家中間塞的東西不一樣：
 *   AIG   `ITEM 1A | Risk Factors`      直線
 *   LVS   `ITEM 1A. — RISK FACTORS`     句點加破折號
 *   AMCR  `Item 1A. - Risk Factors`     句點加連字號
 * 原本只允許「零或一個分隔字元」，這三家全部比對不到、而且不會報錯。
 * 改成「零或多個」之後這幾家一次修好，且七份已知正確的年報逐字元不變。
 */
const SEP = '[\\s.．:：\\-–—|]*'
/**
 * 項次與標題之間偶爾夾一個**孤立的大寫 I**：Horace Mann 排版成
 * `ITEM 1. I Business`、`ITEM 1A. I Risk Factors` —— 那個 I 是拿字母當直線用
 * （不是 `|`，實測碼位 U+0049），SEP 認不得，於是整份 10-K 的 24 個項次錨點
 * 全部落空而且不會報錯。只收前後都是邊界的單一個 I，`Item 1 Insurance…` 不會誤中。
 */
const SEP_I = `${SEP}(?:I\\b${SEP})?`
const ITEM = (n: string, title: string) =>
  new RegExp(`item${SEP}${n}${SEP_I}${title}`, 'gi')

/** 有些公司把章節標題寫成 `Part I. Item 1A. Risk Factors`，前綴不算「行中引用」 */
const PART_PREFIX = /^part\s+[ivx]+\s*[.．:：\-–—|]*$/i

export const SPECS: ItemSpec[] = [
  {
    id: 'business', zh: '業務概況', anchor: 'Item 1. Business',
    start: ITEM('1', 'business'),
    ends: [ITEM('1a', 'risk\\s*factors'), ITEM('1b', 'unresolved'), ITEM('2', 'propert')],
  },
  {
    id: 'risk', zh: '主要風險', anchor: 'Item 1A. Risk Factors',
    start: ITEM('1a', 'risk\\s*factors'),
    ends: [ITEM('1b', 'unresolved'), ITEM('1c', 'cybersecurity'), ITEM('2', 'propert')],
  },
  {
    id: 'mdna', zh: '經營層討論與分析（MD&A）', anchor: "Item 7. Management's Discussion and Analysis",
    missingHint: '部分公司（如 JPM）的 Item 7 以「併入參照」方式指向年報附件 Exhibit 13，本文不在 10-K 主文件內。',
    start: ITEM('7', "management[’']?s?\\s*discussion"),
    ends: [ITEM('7a', 'quantitative'), ITEM('8', 'financial\\s*statements')],
  },
]

/* ── 20-F（外國發行人）的章節錨點 ──────────────────────────
   20-F 的項次編號與 10-K 完全不同，一一對應是：
     Item 3.D Risk Factors                     ≈ 10-K Item 1A
     Item 4.  Information on the Company       ≈ 10-K Item 1
     Item 5.  Operating and Financial Review   ≈ 10-K Item 7（MD&A）
   十份 20-F 實測出三種排版，三種都要收：
   ① 風險章節**多半不寫 "Item"**：`D. Risk Factors`（SPOT／NU／ONON／VIK／GLOB）、
      `3D. Risk Factors`（PAGS）、`ITEM 3. KEY INFORMATION Risk Factors`（DOX）
   ② 標點與單複數各家不同：TSM 是 `ITEM 4.INFORMATION`（句點後無空白，SEP 已涵蓋）
      與 `OPERATING AND FINANCIAL REVIEWS`（複數）
   ③ ASML／QGEN 整份不編項次，只有粗體標題 —— 那兩家走既有的標題式退路
      （`locateByTitle`），所以下面的 TITLE_START 也要認得 20-F 的標題字 */
const F20 = (body: string) => new RegExp(body, 'gi')
export const SPECS_20F: ItemSpec[] = [
  {
    id: 'business', zh: '業務概況', anchor: 'Item 4. Information on the Company',
    start: F20(`item${SEP}4${SEP_I}(?:[ab]${SEP})?information\\s+on\\s+the\\s+company`),
    ends: [F20(`item${SEP}4a${SEP_I}unresolved`),
           F20(`item${SEP}5${SEP_I}operating\\s+and\\s+financial\\s+reviews?`),
           F20(`item${SEP}6${SEP_I}directors`)],
    boldStart: true,
  },
  {
    id: 'risk', zh: '主要風險', anchor: 'Item 3.D Risk Factors',
    start: F20(`(?:item${SEP}3${SEP}(?:key${SEP}information${SEP})?|(?:3${SEP})?d${SEP})risk\\s*factors`),
    ends: [F20(`item${SEP}4${SEP_I}(?:[ab]${SEP})?information\\s+on\\s+the\\s+company`),
           F20(`item${SEP}4${SEP_I}business\\s+overview`)],
    boldStart: true,
  },
  {
    id: 'mdna', zh: '經營層討論與分析（MD&A）', anchor: 'Item 5. Operating and Financial Review and Prospects',
    start: F20(`item${SEP}5${SEP_I}operating\\s+and\\s+financial\\s+reviews?`),
    ends: [F20(`item${SEP}6${SEP_I}directors`),
           F20(`item${SEP}7${SEP_I}major\\s+shareholders`),
           F20(`item${SEP}8${SEP_I}financial\\s+information`)],
    boldStart: true,
  },
]

/** 這份年報該用哪一組錨點。40-F（加拿大 MJDS）是把本國年報整份當附件送，
 *  主文件裡沒有章節本文，所以不在這裡分流 —— 由 profile 端點擋掉並說明。 */
export function specsFor(form: string): ItemSpec[] {
  return form.startsWith('20-F') ? SPECS_20F : SPECS
}

/**
 * 只收「像章節標題」的匹配。
 *
 * 同一串字在一份 10-K 裡至少出現三種身分：目錄、正文標題、以及**句中的交叉引用**
 * （"see Item 1A. Risk Factors" in Part I of this report...）。交叉引用最危險：
 * 它出現在 MD&A 中段，抓到它會讓「風險」章節從 MD&A 一路吃到報表附註
 * （JPM 實測 972K 字元，整份 10-K 的 83%）。
 *
 * 判準三條，都是「排版事實」不是猜的：
 * 1. 匹配必須從**行首**開始（句中引用前面一定有字）
 * 2. 後面緊接引號或逗號的是引用，不是標題
 * 3. 標題行不會超過 160 字元
 */
function headingStarts(re: RegExp, text: string, boldOnly = false): number[] {
  const out: number[] = []
  re.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    const i = m.index
    if (re.lastIndex === m.index) re.lastIndex++
    const ls = text.lastIndexOf('\n', i) + 1
    const pre = text.slice(ls, i).split(B).join('').trim()
    if (pre && !PART_PREFIX.test(pre)) continue
    const after = text.slice(m.index + m[0].length, m.index + m[0].length + 3).split(B).join('')
    if (['”', '"', ',', ';'].includes(after.slice(0, 1))) continue
    let le = text.indexOf('\n', m.index + m[0].length)
    if (le === -1) le = text.length
    if (le - ls > 160) continue
    if (boldOnly && !text.slice(ls, le).includes(B)) continue
    out.push(i)
  }
  return out
}

export function locateSection(text: string, spec: ItemSpec): { from: number; to: number } | null {
  const starts = headingStarts(spec.start, text, spec.boldStart)
  if (!starts.length) return null
  const ends = spec.ends.flatMap((r) => headingStarts(r, text)).sort((a, b) => a - b)
  let best: { from: number; to: number } | null = null
  for (const s of starts) {
    // 目錄裡起點與終點只隔幾十字元 → 該段自然很短，被下面的「取最長」淘汰
    const e = ends.find((x) => x > s + 30) ?? text.length
    if (!best || e - s > best.to - best.from) best = { from: s, to: e }
  }
  // 短於 1200 字的一定是目錄或交叉引用，不是章節本文
  return best && best.to - best.from >= 1200 ? best : null
}

/* ── 交叉索引式 10-K 的退路（只做風險章節）──────────────────
   有一類 10-K 正文完全沒有 `Item N.`：最前面放一張「項次 → 頁碼」對照表，
   內文標題是公司自己的寫法（Intel、摩根士丹利、花旗、麥當勞、漢威都是這樣，
   Intel 自己在文件裡寫明「the order and presentation of content ... differ
   from the traditional SEC Form 10-K format」）。

   對這類文件只救**風險章節**，而且**要通過內容驗證才收**。原因是驗證器只有
   風險段落分得開：拿七份已知抽對的年報量「風險用語密度」，風險段 0.82–1.92、
   業務段 0.00–0.56、MD&A 段 0.10–0.41，門檻 0.75 可以完全分開；
   業務與 MD&A 的密度重疊，驗不了就不收 —— 半對的章節比沒有更糟。 */
const RISK_TITLE = /^risk\s*factors$/i
const RISK_END = [
  /^unresolved\s+staff\s+comments$/i,
  /^cybersecurity$/i,
  /^propert(y|ies)$/i,
  /^legal\s+proceedings$/i,
  /^(quantitative|management[’']?s?\s+discussion)/i,
]
/** 20-F 的風險章節終點：接下來就是 Item 4／Item 5 */
const RISK_END_20F = [
  // 項次前綴要收：TSM 的正文標題是 `ITEM 4.INFORMATION ON THE COMPANY`，
  // 只認裸標題的話這份找不到終點 → 吃到檔尾 → 被 MAX_SHARE 丟掉
  /^(item\s*4[\s.．:：\-–—|]*)?(a[.．]?\s*)?information\s+on\s+the\s+company$/i,
  /^(item\s*5[\s.．:：\-–—|]*)?(a[.．]?\s*)?operating\s+(and\s+financial\s+)?reviews?/i,
]
const RISK_WORDS = /(adversely affect|materially adverse|could harm|may be harmed|risks?\b|uncertaint)/gi
/** 章節不可能佔整份 10-K 的三成以上 —— 超過就是抓到跨章節的一大段 */
const MAX_SHARE = 0.30
const MIN_RISK_DENSITY = 0.75

/** 整行等於某個標題、且該行是粗體的位置 */
function titleLines(text: string, re: RegExp): number[] {
  const out: number[] = []
  let pos = 0
  for (const line of text.split('\n')) {
    const bare = line.split(B).join('').trim()
    if (bare && line.includes(B) && re.test(bare)) out.push(pos)
    pos += line.length + 1
  }
  return out
}

function riskDensity(seg: string): number {
  return (seg.match(RISK_WORDS)?.length ?? 0) / Math.max(1, seg.length / 1000)
}

/* ── 頁首式標題（Intel、Synchrony）────────────────────────
   還有一種更麻煩的：章節標題**不是粗體**，而是每一頁重複的頁首。
   Intel 的 10-K 裡「Our Business」「Risk Factors」各出現十幾次，
   第一次後面接的是內文，其餘後面接的都是頁碼。
   判準就用這個事實：**後面幾行之內要有內文**（≥40 字元且不是純數字）。

   三個章節都用這條路，但**每一個都要通過自己的驗證器**才收：
     風險   風險用語密度 ≥ 0.75（同上，七份已知正確的年報校準出來的）
     MD&A   逐年比較用語密度 ≥ 0.12（六份真 MD&A 的最低值）
     業務   反向驗證：不像風險段、不像 MD&A、且至少有 5 段長內文
   MD&A 的驗證器不是裝飾：JPM 的「Item 7 見第 46–160 頁」那段併入參照聲明
   長度有 3 萬字元，形狀跟章節一模一樣，只有密度驗證擋得住（實測被擋下）。 */
/**
 * 標題與內文**同一行**（run-in heading）：GE 的 10-K 每一節都長成
 * `RISK FACTORS. The following discussion of the material factors…`、
 * `LEGAL PROCEEDINGS. Refer to Legal Matters…`。標題後面接的是句點加整段內文，
 * 所以「整行等於標題」（`…$`）這個判準一條都比對不到。
 *
 * 允許標題後面接「句讀 + 空白 + 內文」，但**必須有那個句讀**：
 * 少了它，`BUSINESS OVERVIEW AND ENVIRONMENT.` 這種以標題字開頭的普通小標
 * 也會被當成章節起點。
 *
 * 句讀**只收句點與冒號，不收破折號**。GE 的風險小標長成
 * `Cybersecurity - Increased cybersecurity requirements…`、
 * `Product safety and quality - Our products…` —— 破折號也算的話，這一條
 * 會被 Item 1C 的終點式吃掉，風險章節在自己的第 12 條風險上被腰斬（實測 36K／62K）。
 */
const RUNIN = '(?:[.．:：]\\s+\\S.*)?'
const T = (body: string) => new RegExp(`^[\\W_]*${body}[\\W_]*${RUNIN}$`, 'i')

const TITLE_START: Record<Section['id'], RegExp> = {
  // 試過放寬成 `about …`（GE 的業務章節標題是 `ABOUT GE AEROSPACE.`，MCD 是
  // `ABOUT McDONALD'S`）—— **不能收**。業務段的驗證器是排除法（正面特徵測不到，
  // 蘋果的 Item 1 連 "we design" 都不出現），放寬起點就沒有東西擋得住誤中：
  // 實測換到 GE 與 MCD 各一格，代價是 INTC 從正確的 29249 漂到附註裡的
  // 「Business Combinations」、SYF 漂到目錄，另外 C／HON／MS 生出三段垃圾。
  // 半對的章節比沒有更糟，維持只認 Business／Our Business。
  business: T('(our\\s+)?business(\\s+overview)?'),
  risk: T('(risk\\s*factors|risks)'),
  mdna: /^[\W_]*management[’']?s?\s+discussion\s+and\s+analysis.*$/i,
}
const TITLE_END: Record<Section['id'], RegExp[]> = {
  business: [TITLE_START.risk, T('unresolved\\s+staff\\s+comments'), T('propert(y|ies)')],
  risk: [T('unresolved\\s+staff\\s+comments'), T('cybersecurity'),
         T('propert(y|ies)'), T('legal\\s+proceedings'),
         /^[\W_]*management[’']?s?\s+discussion.*$/i],
  // MD&A 的終點原本只認 Item 7A／Item 8。GE 這種交叉索引式 10-K 兩個都沒有
  // （MD&A 之後直接接 CYBERSECURITY），終點找不到就一路吃到檔尾、被 MAX_SHARE
  // 判成「抓到跨章節的一大段」而整段丟掉 —— 章節明明抓對了起點卻交白卷。
  // 加上 TITLE_START.risk 也順手修好三家：交叉索引式 10-K 把風險章節排在
  // MD&A 後面，終點認不得它就會把整個風險章節吞進 MD&A（C／HON／MCD 實測）。
  mdna: [/^[\W_]*quantitative\s+and\s+qualitative.*$/i,
         T('(consolidated\\s+)?financial\\s+statements(\\s+and\\s+supplementary\\s+data)?'),
         T('cybersecurity'), T('legal\\s+proceedings'), TITLE_START.risk],
}

/* 20-F 的標題式退路自成一組。**不能跟 10-K 那組混用**：10-K 的 business 起點是
   裸的 `Business`，套到 20-F 上會抓到永續章節的「Business travel」與公司章程的
   「business combinations with an interested shareholder…」（ASML／NU 實測，
   而且長度剛好過得了 MAX_SHARE、排除法驗證器也擋不住 —— 那兩段本來就
   不像風險也不像 MD&A）。20-F 的業務章節標題只有一個寫法：Information on the Company。 */
const TITLE_START_20F: Record<Section['id'], RegExp> = {
  business: T('(a[.．]?\\s*)?information\\s+on\\s+the\\s+company'),
  // **不收裸的 `risks`**（10-K 那組收）。它加上 run-in 之後會吃掉正文裡任何
  // 以 risks 開頭的句子 —— NU 抓到的是「risks. These operations involve a range
  // of derivatives…」，長度足夠、風險用語密度又高，驗證器擋不住
  risk: T('(d[.．]?\\s*)?risk\\s*factors'),
  mdna: T('(a[.．]?\\s*)?operating\\s+(and\\s+financial\\s+)?reviews?(\\s+and\\s+prospects)?'),
}
const TITLE_END_20F: Record<Section['id'], RegExp[]> = {
  business: [TITLE_START_20F.mdna, T('unresolved\\s+staff\\s+comments')],
  risk: [TITLE_START_20F.business, TITLE_START_20F.mdna],
  mdna: [T('directors,?\\s+senior\\s+management\\s+and\\s+employees'),
         T('major\\s+shareholders.*'), T('financial\\s+information')],
}
const titleSet = (form: string) => (form.startsWith('20-F')
  ? { start: TITLE_START_20F, end: TITLE_END_20F }
  : { start: TITLE_START, end: TITLE_END })
const MDNA_WORDS = /(compared (to|with) (the )?(prior|fiscal|year)|increased? \d|decreased? \d|year[- ]over[- ]year|results of operations|net revenues? (increased|decreased)|primarily (due|driven|attributable) to)/gi
const MDNA_MIN = 0.12

function density(re: RegExp, seg: string): number {
  return (seg.match(re)?.length ?? 0) / Math.max(1, seg.length / 1000)
}
function longParas(seg: string): number {
  return seg.split('\n').filter((l) => l.split(B).join('').trim().length >= 200).length
}
function validSection(id: Section['id'], seg: string): boolean {
  if (id === 'risk') return riskDensity(seg) >= MIN_RISK_DENSITY
  if (id === 'mdna') return density(MDNA_WORDS, seg) >= MDNA_MIN
  // 業務段沒有正面特徵可測（蘋果的 Item 1 連 "we design" 都不出現）→ 用排除法
  return riskDensity(seg) < MIN_RISK_DENSITY && density(MDNA_WORDS, seg) < MDNA_MIN
    && longParas(seg) >= 5
}

/** 整行等於標題，且「該行是粗體」或「後面幾行內有內文」——兩者皆非就是頁首或目錄行 */
function headingLines(text: string, re: RegExp): number[] {
  const lines = text.split('\n')
  const out: number[] = []
  let pos = 0
  for (let i = 0; i < lines.length; i++) {
    const bare = lines[i].split(B).join('').trim()
    if (bare && re.test(bare)) {
      const bold = lines[i].includes(B)
      let prose = false
      for (let k = i + 1; k < Math.min(i + 5, lines.length) && !prose; k++) {
        const b = lines[k].split(B).join('').trim()
        if (!b) continue
        prose = b.length >= 40 && !/^[\d\s.,%$()-]+$/.test(b)
      }
      if (bold || prose) out.push(pos)
    }
    pos += lines[i].length + 1
  }
  return out
}

export function locateByTitle(text: string, id: Section['id'], form = '10-K'):
{ from: number; to: number } | null {
  const set = titleSet(form)
  const starts = headingLines(text, set.start[id])
  if (!starts.length) return null
  const ends = set.end[id].flatMap((r) => headingLines(text, r)).sort((a, b) => a - b)
  const limit = text.length * MAX_SHARE
  const cands: { from: number; to: number }[] = []
  for (const from of starts) {
    const to = ends.find((x) => x > from + 30) ?? text.length
    if (to - from >= 1200 && to - from <= limit) cands.push({ from, to })
  }
  cands.sort((a, b) => (b.to - b.from) - (a.to - a.from))
  for (const c of cands) {
    if (validSection(id, text.slice(c.from, Math.min(c.to, c.from + MAX_CHARS)))) return c
  }
  return null
}

export function locateRiskByTitle(text: string, form = '10-K'):
{ from: number; to: number } | null {
  const starts = titleLines(text, RISK_TITLE)
  if (!starts.length) return null
  // 20-F 的風險章節後面接的是 Item 4，10-K 那組終點（Item 1B／1C／2）一個都不存在。
  // 終點找不到就吃到檔尾、再被 MAX_SHARE 丟掉 —— TSM 的粗體 `Risk Factors` 明明抓對了
  const ends = [...RISK_END, ...(form.startsWith('20-F') ? RISK_END_20F : [])]
    .flatMap((r) => titleLines(text, r)).sort((a, b) => a - b)
  const limit = text.length * MAX_SHARE
  const cands: { from: number; to: number }[] = []
  for (const from of starts) {
    const to = ends.find((x) => x > from + 30) ?? text.length
    if (to - from >= 1200 && to - from <= limit) cands.push({ from, to })
  }
  cands.sort((a, b) => (b.to - b.from) - (a.to - a.from))
  for (const c of cands) {
    if (riskDensity(text.slice(c.from, Math.min(c.to, c.from + MAX_CHARS))) >= MIN_RISK_DENSITY) {
      return c
    }
  }
  return null
}

const MAX_CHARS = 60000
/** 每個章節最多送幾段給前端。
 *
 *  原本是 400 段 —— MP 的 MD&A 有 189 段、JPM 的風險有 269 段，整包送出去
 *  API 回應會到 178 KB，而且**沒有人讀得完**。10-K 的完整原文本來就在 EDGAR，
 *  這裡的工作是「挑出值得先看的那幾段」，不是把整本搬過來。 */
const MAX_PARAS = 14

interface Item { t: string; h: boolean }

function itemize(raw: string): Item[] {
  const items: Item[] = []
  for (const lineRaw of raw.split('\n')) {
    const bold = lineRaw.includes(B)
    const line = lineRaw.split(B).join('').trim()
    if (line.length < 2) continue
    if (/^(page\s*)?\d{1,3}$/i.test(line)) continue      // 頁碼
    if (/^table of contents$/i.test(line)) continue
    // 條列符號開頭的是內文不是小標；章節自己的標題也不算小標
    const isHeading = bold && line.length >= 12 && line.length <= 220 && /[a-z]/.test(line)
      && !/^[•·▪-]/.test(line) && !/^item\s*\d/i.test(line)
    const prev = items[items.length - 1]
    if (!isHeading && prev && !prev.h && line.length < 90
        && !/[.!?:]$/.test(line) && prev.t.length < 90) {
      // 被硬換行切開的同一句（表格化排版常見）→ 接回去
      prev.t = `${prev.t} ${line}`
      continue
    }
    items.push({ t: line, h: isHeading })
  }
  // 第一段常常就是章節標題本身（"Item 7. Management's Discussion…"），不是內文
  while (items.length && /^item\s*\d/i.test(items[0].t) && items[0].t.length < 200) items.shift()
  return items
}

/**
 * MD&A 的「值得先看」那一段。
 *
 * Item 7 動輒十萬字元，其中絕大多數是逐項的數字比較（那些數字我們本來就有，
 * 而且是從 companyfacts 來的、比散文可靠）。真正只有文字才講得出來的是開頭
 * 那個總覽小節 —— 公司自己用一兩百字說「這一年發生什麼、接下來要做什麼」。
 * 找不到就退回開頭幾段，不硬湊。
 */
const OVERVIEW = /^(executive\s+)?(overview|summary|highlights|outlook|business\s+(update|overview|outlook)|fiscal\s+\d{4}\s+(overview|highlights)|recent\s+developments)\b/i

function pickOverview(items: Item[]): Item[] | null {
  const at = items.findIndex((x) => x.h && OVERVIEW.test(x.t))
  if (at < 0) return null
  const out: Item[] = []
  for (let i = at + 1; i < items.length; i++) {
    if (items[i].h && !OVERVIEW.test(items[i].t) && out.length) break
    out.push(items[i])
    if (out.length >= MAX_PARAS) break
  }
  const body = out.filter((x) => !x.h && x.t.length > 40)
  return body.length >= 2 ? out : null
}

function paragraphize(raw: string, id: Section['id']): {
  paragraphs: string[]; headings: string[]; focus: string | null
} {
  const items = itemize(raw)
  const seen = new Set<string>()
  const headings = items.filter((x) => x.h).map((x) => x.t)
    .filter((h) => (seen.has(h) ? false : (seen.add(h), true))).slice(0, 120)

  let body = items
  let focus: string | null = null
  if (id === 'mdna') {
    const ov = pickOverview(items)
    if (ov) {
      body = ov
      focus = items[items.findIndex((x) => x.h && OVERVIEW.test(x.t))].t
    }
  }
  // 風險章節的價值全在小標（每一條風險的標題本身就是完整的一句話），
  // 內文只留導言幾段；要逐條讀完整版的人請點 EDGAR 原文
  const cap = id === 'risk' ? 6 : MAX_PARAS
  return {
    paragraphs: body.filter((x) => !x.h && x.t.length > 40).map((x) => x.t).slice(0, cap),
    headings,
    focus,
  }
}

export interface FilingMeta {
  form: string
  accession: string
  reportDate: string
  filingDate: string
  url: string
}

interface ZhDoc {
  translator?: string
  date?: string
  sections: Record<string, { headings?: string[]; paragraphs?: string[]; focus?: string }>
}

/**
 * 掛上繁中譯文（`config/narrative_zh/{cik}-{accession}.json`，`tools/translate_narrative.py` 產生）。
 *
 * 兩件事刻意這樣設計：
 * 1. **譯文檔綁申報書號**。公司出新的 10-K 就是新書號 → 舊譯文自動失效、頁面退回英文，
 *    不會拿去年的翻譯套在今年的財報上
 * 2. **逐項對齊、缺的補空字串**。翻到一半的章節照樣可用，沒翻到的那幾條顯示英文原文
 */
async function attachZh(n: Narrative, cik10: string): Promise<void> {
  const doc = await useStorage('assets:config')
    .getItem(`narrative_zh/${cik10}-${n.accession}.json`) as ZhDoc | null
  if (!doc?.sections) return
  n.translator = doc.translator ?? null
  n.translatedAt = doc.date ?? null
  for (const s of n.sections) {
    const t = doc.sections[s.id]
    if (!t) continue
    const pad = (src: string[], zh?: string[]) =>
      src.map((_, i) => (zh?.[i] ?? '').trim())
    s.headingsZh = pad(s.headings, t.headings)
    s.paragraphsZh = pad(s.paragraphs, t.paragraphs)
  }
}

export async function getNarrative(cik10: string, f: FilingMeta): Promise<Narrative> {
  // v8：抽取器改了（HMN 的 I 分隔符、GE 的 run-in 標題、20-F 章節）。
  // 不換版號的話舊快取會一直回舊的「找不到章節」，改了等於沒改
  const key = `narr/v8/${cik10}/${f.accession}.json`
  const hit = await cacheGet<Narrative>(key)
  if (hit) {
    // 譯文不進 Blob 快取 —— 它會在快取之後才補上，每次都要重新掛
    await attachZh(hit, cik10)
    return hit
  }

  const notes: string[] = []
  let sections: Section[] = []
  try {
    const html = await secFetchTextLimited(f.url, MAX_HTML)
    const text = htmlToText(html)
    for (const spec of specsFor(f.form)) {
      let loc = locateSection(text, spec)
      let viaTitle = false
      if (!loc) {
        // 兩條退路都留著：粗體式（花旗那種）與頁首式（Intel 那種）各自抓得到
        // 對方抓不到的文件，取先命中的
        loc = (spec.id === 'risk' ? locateRiskByTitle(text, f.form) : null)
          ?? locateByTitle(text, spec.id, f.form)
        viaTitle = !!loc
      }
      if (!loc) {
        notes.push(`${spec.anchor}：在這份 ${f.form} 裡找不到可辨識的章節標題。${spec.missingHint ?? ''}`)
        continue
      }
      const raw = text.slice(loc.from, Math.min(loc.to, loc.from + MAX_CHARS))
      const { paragraphs, headings, focus } = paragraphize(raw, spec.id)
      sections.push({
        id: spec.id, anchor: spec.anchor, zh: spec.zh,
        paragraphs, headings, focus, viaTitle,
        chars: loc.to - loc.from,
        truncated: true,
      })
    }
  } catch (err) {
    const msg = (err as Error).message
    sections = []
    notes.push(msg.startsWith('too-large')
      ? `這份 ${f.form} 的 HTML 超過 24 MB，未解析（避免函式記憶體不足）。`
      : `讀取 ${f.form} 失敗：${msg}`)
  }

  const out: Narrative = {
    form: f.form, accession: f.accession, reportDate: f.reportDate,
    filedDate: f.filingDate, url: f.url, sections, notes,
  }
  if (sections.length) await cacheSet(key, out)   // 存的是英文版；譯文另外掛
  await attachZh(out, cik10)
  return out
}
