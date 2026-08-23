import { defineEventHandler, getQuery, createError } from 'h3'
import { resolveCompany } from '../utils/cik'
import { secFetchJson } from '../utils/secFetch'
import { filingUrl } from '../utils/filings'
import { getNarrative, type Narrative } from '../utils/narrative'

/**
 * GET /api/profile?ticker=AAPL[&narrative=0]
 *
 * 公司檔案（submissions.json，零額外請求）＋ 最新年報的敘述性章節。
 * SEC 請求：submissions 1 次（與其他端點共用快取）＋ 年報 HTML 1 次（永久快取）。
 *
 * `website` 與 `description` 這兩個欄位 SEC **永遠回空字串**，不要指望。
 * 業務散文一律來自 Item 1。
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
  cik: string
  sic: string
  sicDescription: string
  entityType: string
  category: string
  stateOfIncorporation: string
  stateOfIncorporationDescription?: string
  fiscalYearEnd: string
  ein: string
  phone: string
  exchanges: string[]
  tickers: string[]
  addresses?: Record<string, Record<string, string>>
  formerNames?: { name: string; from: string; to: string }[]
  filings: { recent: RecentBlock }
}

/** 年報表單：外國發行人是 20-F（章節結構與 10-K 不同，抽不出來會誠實說） */
const ANNUAL = ['10-K', '20-F', '40-F', '10-K/A', '20-F/A']

export default defineEventHandler(async (event) => {
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
  const biz = sub.addresses?.business ?? {}

  let latest: { form: string; accession: string; reportDate: string; filingDate: string; url: string } | null = null
  for (let i = 0; i < r.form.length; i++) {
    if (!ANNUAL.includes(r.form[i])) continue
    latest = {
      form: r.form[i],
      accession: r.accessionNumber[i],
      reportDate: r.reportDate[i],
      filingDate: r.filingDate[i],
      url: filingUrl(ref.cik, r.accessionNumber[i], r.primaryDocument[i]),
    }
    break // filings.recent 已依申報日新到舊排序
  }

  let narrative: Narrative | null = null
  const notes: string[] = []
  if (!latest) {
    notes.push('這家公司的最近 1000 筆申報裡沒有年報（10-K / 20-F），沒有可抽取的敘述性章節。')
  } else if (q.narrative === '0') {
    // 前端可分兩段載入：先出公司檔案，再補章節
  } else if (latest.form.startsWith('20-F') || latest.form.startsWith('40-F')) {
    notes.push(`最近的年報是 ${latest.form}（外國發行人）。20-F/40-F 的章節編號與 10-K 不同，目前只支援 10-K 的 Item 1 / 1A / 7。`)
  } else {
    narrative = await getNarrative(ref.cik10, latest)
  }

  return {
    ticker: ref.ticker,
    cik: ref.cik10,
    company: sub.name || ref.name,
    tickers: sub.tickers ?? [],
    exchanges: sub.exchanges ?? [],
    sic: sub.sic,
    sicDescription: sub.sicDescription,
    entityType: sub.entityType,
    category: sub.category,
    stateOfIncorporation: sub.stateOfIncorporationDescription || sub.stateOfIncorporation,
    fiscalYearEnd: sub.fiscalYearEnd,
    ein: sub.ein,
    phone: sub.phone,
    address: [biz.street1, biz.street2, biz.city, biz.stateOrCountry, biz.zipCode]
      .filter(Boolean).join(' '),
    formerNames: sub.formerNames ?? [],
    latestAnnual: latest,
    narrative,
    notes,
  }
})
