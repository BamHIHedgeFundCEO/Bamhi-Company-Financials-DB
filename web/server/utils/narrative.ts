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
/** 粗體標記。剝標籤會把粗體資訊一起剝掉，但風險因子的小標**就是**粗體，
 *  所以在剝的過程中先留下這個控制字元當記號，段落化時再拿掉。 */
const B = ''

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
}

const ITEM = (n: string, title: string) =>
  new RegExp(`item\\s*${n}\\s*[.．:：\\-–—]?\\s*${title}`, 'gi')

const SPECS: ItemSpec[] = [
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
function headingStarts(re: RegExp, text: string): number[] {
  const out: number[] = []
  re.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = re.exec(text))) {
    const i = m.index
    if (re.lastIndex === m.index) re.lastIndex++
    const ls = text.lastIndexOf('\n', i) + 1
    if (text.slice(ls, i).split(B).join('').trim()) continue
    const after = text.slice(m.index + m[0].length, m.index + m[0].length + 3).split(B).join('')
    if (['”', '"', ',', ';'].includes(after.slice(0, 1))) continue
    let le = text.indexOf('\n', m.index + m[0].length)
    if (le === -1) le = text.length
    if (le - ls > 160) continue
    out.push(i)
  }
  return out
}

export function locateSection(text: string, spec: ItemSpec): { from: number; to: number } | null {
  const starts = headingStarts(spec.start, text)
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
  const key = `narr/v4/${cik10}/${f.accession}.json`
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
    for (const spec of SPECS) {
      const loc = locateSection(text, spec)
      if (!loc) {
        notes.push(`${spec.anchor}：在這份 ${f.form} 裡找不到可辨識的章節標題。${spec.missingHint ?? ''}`)
        continue
      }
      const raw = text.slice(loc.from, Math.min(loc.to, loc.from + MAX_CHARS))
      const { paragraphs, headings, focus } = paragraphize(raw, spec.id)
      sections.push({
        id: spec.id, anchor: spec.anchor, zh: spec.zh,
        paragraphs, headings, focus,
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
