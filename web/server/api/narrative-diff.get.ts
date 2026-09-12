import { defineEventHandler, getQuery, createError, setHeader } from 'h3'
import { resolveCompany } from '../utils/cik'
import { secFetchJson } from '../utils/secFetch'
import { filingUrl, getFilings } from '../utils/filings'
import { getNarrative, type FilingMeta } from '../utils/narrative'
import { diffNarrative } from '../utils/narrativeDiff'

/**
 * GET /api/narrative-diff?ticker=AAPL
 *
 * 今年的年報跟去年那份，哪裡不一樣：新增／刪除／改寫了哪幾條風險與哪幾個小節。
 *
 * **為什麼要獨立一支端點，不掛在 /api/profile 上**
 *
 * 硬規則是「敘述性段落只抓最新一份年報的主文件，單檔 24 MB 上限」——上限的理由是
 * serverless 的記憶體。對比需要兩份，所以拆成兩支端點、兩個請求：
 * 公司簡介那一頁照舊只解析一份，要看對比的人才多付一份的成本。
 * 同一個請求裡連續解析兩份 24 MB 的 HTML 是這條規則當初要避免的事。
 * 這裡也只在解析完一份、拿到（很小的）結果之後才去拿第二份。
 *
 * SEC 請求：submissions 1 次（與其他端點共用快取）＋ 年報 HTML 最多 2 次。
 * 兩份都以申報書號為 key 永久快取（已申報文件不可變），且今年那份多半已經被
 * 公司簡介分頁抓過了 → 實際新增的是「去年那一份」一次，每家一次性成本。
 *
 * **取的是原始年報，不是修正案**（與 profile.get.ts 同一條規則）：10-K/A 多半只重申
 * 被修正的那幾項，裡面沒有 Item 1／1A／7，照申報日取最新會挑到它。
 */

interface RecentBlock {
  accessionNumber: string[]
  filingDate: string[]
  reportDate: string[]
  form: string[]
  primaryDocument: string[]
}
interface Submissions {
  name: string
  filings: {
    recent: RecentBlock
    /** 更早的申報分頁（recent 只有最近約 1000 筆） */
    files?: { name: string; filingCount: number; filingFrom: string; filingTo: string }[]
  }
}

const ANNUAL = ['10-K', '20-F']

export default defineEventHandler(async (event) => {
  setHeader(event, 'Cache-Control', 'public, s-maxage=3600, stale-while-revalidate=86400')
  const q = getQuery(event)
  const t = String(q.ticker || '').trim().toUpperCase()
  if (!t) throw createError({ statusCode: 400, message: '缺少 ticker' })

  const ref = await resolveCompany(t)
  if (!ref) {
    throw createError({
      statusCode: 404,
      message: `找不到「${t}」。請確認 ticker 拼寫；已下市公司與多數 ETF 不在 SEC 申報名單內。`,
    })
  }

  const sub = await secFetchJson<Submissions>(
    `https://data.sec.gov/submissions/CIK${ref.cik10}.json`,
  )
  const r = sub.filings.recent

  // 依申報日新到舊；同一份年報不會出現兩次，但改組過的公司可能混著 10-K 與 20-F，
  // 所以第二份要跟第一份**同一種表單**，否則等於拿兩套章節錨點的東西在比
  const annuals: FilingMeta[] = []
  for (let i = 0; i < r.form.length; i++) {
    if (!ANNUAL.includes(r.form[i]!)) continue
    annuals.push({
      form: r.form[i]!,
      accession: r.accessionNumber[i]!,
      reportDate: r.reportDate[i]!,
      filingDate: r.filingDate[i]!,
      url: filingUrl(ref.cik, r.accessionNumber[i]!, r.primaryDocument[i]!),
    })
  }
  const current = annuals[0] ?? null
  let previous = current ? annuals.find((f, i) => i > 0 && f.form === current.form) ?? null : null

  // `filings.recent` 只有最近約 1000 筆。JPM 這種一年發幾百份 424B2 的公司，
  // 那 1000 筆連一整年都蓋不滿 —— 實測 JPM 的 recent 裡只有一份 10-K，
  // 不翻頁的話這一頁會對所有大型銀行說「只找到一份年報」。
  // 視窗只開到上一份年報的前後（約 500 天），避免多翻幾頁就多幾次 SEC 請求。
  if (current && !previous && sub.filings.files?.length) {
    const from = new Date(new Date(current.reportDate).getTime() - 500 * 86400_000)
      .toISOString().slice(0, 10)
    const older = await getFilings(ref, from, current.reportDate, [current.form])
    const hit = older.filings.find((f) => f.accessionNumber !== current.accession)
    if (hit) {
      previous = {
        form: hit.form,
        accession: hit.accessionNumber,
        reportDate: hit.reportDate,
        filingDate: hit.filingDate,
        url: hit.url,
      }
    }
  }

  const notes: string[] = []
  const base = {
    ticker: ref.ticker,
    cik: ref.cik10,
    company: sub.name || ref.name,
    current,
    previous,
  }
  if (!current) {
    notes.push('這家公司的最近 1000 筆申報裡沒有年報（10-K／20-F），沒有可對比的章節。')
    return { ...base, sections: [], notes }
  }
  if (!previous) {
    notes.push(`只找到一份 ${current.form}（${current.reportDate}）。`
      + '逐年對比需要兩份年報，新上市或剛改組過的公司會落在這裡。')
    return { ...base, sections: [], notes }
  }

  // 先今年、再去年。兩次之間沒有共存的大字串：getNarrative 回來的是解析後的小物件
  const cur = await getNarrative(ref.cik10, current)
  const prev = await getNarrative(ref.cik10, previous)

  if (!cur.sections.length || !prev.sections.length) {
    const which = !cur.sections.length ? `今年那份 ${current.form}` : `去年那份 ${previous.form}`
    notes.push(`${which}抽不到任何章節，無法對比。`
      + [...cur.notes, ...prev.notes].map((n) => `（${n}）`).join(''))
    return { ...base, sections: [], notes }
  }

  notes.push('**比的是小標，不是全文。**風險因子的每一條小標本身就是一句完整的風險敘述，'
    + '新增一條風險就是多一個小標。散文的逐字比對會被改寫與數字更新淹沒，'
    + '那等於什麼都沒說。完整原文一律看 EDGAR。')
  notes.push('**不判斷變動是好是壞。**新增一條風險不代表公司變差（可能只是把既有的風險拆成兩條），'
    + '刪掉一條也不代表那個風險消失了。這一頁只說「這裡不一樣」。')

  return {
    ...base,
    currentUrl: cur.url,
    previousUrl: prev.url,
    sections: diffNarrative(cur, prev),
    notes,
  }
})
