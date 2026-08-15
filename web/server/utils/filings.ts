import { secFetchJson } from './secFetch'
import type { CompanyRef } from './cik'

/**
 * 資料流 A：submissions.json → 財報清單。
 * - filings.recent 只含最近約 1000 筆；目標區間更早時追加抓 filings.files 分頁
 * - 外國發行人（TSM/ASML）：無 10-K/10-Q → 自動改抓 20-F/6-K，isForeignIssuer=true
 * - 10-K/A、10-Q/A 一併列出，isAmendment=true
 * - 季別一律以 reportDate（期末日）為準（NVDA/AAPL 會計年度錯開曆年）
 */

interface RecentBlock {
  form: string[]
  filingDate: string[]
  accessionNumber: string[]
  primaryDocument: string[]
  reportDate: string[]
}

interface Submissions {
  name: string
  entityType?: string
  sicDescription?: string
  fiscalYearEnd?: string // MMDD
  filings: {
    recent: RecentBlock
    files?: { name: string; filingCount: number; filingFrom: string; filingTo: string }[]
  }
}

export interface Filing {
  form: string
  fiscalPeriod: string // 例：FY2026 Q2
  reportDate: string
  filingDate: string
  accessionNumber: string
  url: string
  isAmendment: boolean
}

export interface FilingsResult {
  company: string
  cik: string
  ticker: string
  sic?: string
  fiscalYearEnd?: string
  isForeignIssuer: boolean
  filings: Filing[]
}

const DOMESTIC_FORMS = ['10-K', '10-Q', '10-K/A', '10-Q/A']
const FOREIGN_FORMS = ['20-F', '6-K', '20-F/A', '40-F']

function rowsOf(b: RecentBlock) {
  // SEC 給平行陣列，用 index 對齊組物件
  return b.form.map((form, i) => ({
    form,
    filingDate: b.filingDate[i],
    accessionNumber: b.accessionNumber[i],
    primaryDocument: b.primaryDocument[i],
    reportDate: b.reportDate[i],
  }))
}

/** 文件直連：CIK 不補零、accession 去橫線 */
export function filingUrl(cik: number, accession: string, primaryDocument: string): string {
  return `https://www.sec.gov/Archives/edgar/data/${cik}/${accession.replace(/-/g, '')}/${primaryDocument}`
}

/**
 * 以 reportDate 推季別。fiscalYearEnd 為 MMDD（如 NVDA "0131"）。
 * FY 年 = 期末日落在該會計年度者；Q = 距 FY 年末的月數差推回。
 */
export function fiscalPeriodOf(reportDate: string, fiscalYearEnd: string | undefined, form: string): string {
  if (!reportDate) return ''
  const [y, m] = reportDate.split('-').map(Number)
  const fyeMonth = fiscalYearEnd ? Number(fiscalYearEnd.slice(0, 2)) : 12
  // 期末月距會計年度末的月數（0=年度末季，3=差一季...）
  const diff = (fyeMonth - m + 12) % 12
  // FY 年 = reportDate 往後推 diff 個月落到的年份
  const fy = new Date(y, m - 1 + diff).getFullYear()
  const isAnnual = form.startsWith('10-K') || form.startsWith('20-F') || form.startsWith('40-F')
  if (isAnnual || diff === 0) return `FY${fy}`
  const q = 4 - Math.round(diff / 3)
  return `FY${fy} Q${q}`
}

export async function getFilings(
  ref: CompanyRef,
  fromDate: string,
  toDate: string,
  forms?: string[],
): Promise<FilingsResult> {
  const sub = await secFetchJson<Submissions>(
    `https://data.sec.gov/submissions/CIK${ref.cik10}.json`,
  )

  let rows = rowsOf(sub.filings.recent)

  // Edge case 3：目標區間早於 recent 最舊一筆 → 抓 filings.files 分頁
  const oldestRecent = rows.length ? rows[rows.length - 1].filingDate : ''
  if (sub.filings.files?.length && oldestRecent && fromDate < oldestRecent) {
    for (const f of sub.filings.files) {
      if (f.filingTo >= fromDate) {
        const page = await secFetchJson<RecentBlock>(
          `https://data.sec.gov/submissions/${f.name}`,
        )
        rows = rows.concat(rowsOf(page))
      }
    }
  }

  // Edge case 4：偵測外國發行人（歷史 form type 無 10-K/10-Q 但有 20-F/6-K）
  const allForms = new Set(rows.map((r) => r.form))
  const hasDomestic = DOMESTIC_FORMS.some((f) => allForms.has(f))
  const isForeignIssuer = !hasDomestic && FOREIGN_FORMS.some((f) => allForms.has(f))
  const wanted = forms?.length ? forms : isForeignIssuer ? FOREIGN_FORMS : DOMESTIC_FORMS

  const filings: Filing[] = rows
    .filter((r) => wanted.includes(r.form))
    .filter((r) => {
      const d = r.reportDate || r.filingDate
      return d >= fromDate && d <= toDate
    })
    .map((r) => ({
      form: r.form,
      fiscalPeriod: fiscalPeriodOf(r.reportDate, sub.fiscalYearEnd, r.form),
      reportDate: r.reportDate,
      filingDate: r.filingDate,
      accessionNumber: r.accessionNumber,
      url: filingUrl(ref.cik, r.accessionNumber, r.primaryDocument),
      isAmendment: r.form.endsWith('/A'),
    }))
    .sort((a, b) => (a.reportDate < b.reportDate ? 1 : -1))

  return {
    company: sub.name || ref.name,
    cik: ref.cik10,
    ticker: ref.ticker,
    sic: sub.sicDescription,
    fiscalYearEnd: sub.fiscalYearEnd,
    isForeignIssuer,
    filings,
  }
}
