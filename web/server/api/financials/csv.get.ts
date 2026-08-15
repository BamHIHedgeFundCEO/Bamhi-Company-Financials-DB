import { defineEventHandler, getQuery, setHeader, createError } from 'h3'
import { resolveTicker } from '../../utils/cik'
import { getFinancials } from '../../utils/financials'
import { parseTickers, parseRange, clampPeriods } from '../../utils/params'

/**
 * GET /api/financials/csv?ticker=AAPL&from=2021Q1&to=2026Q2&statement=IS
 * → text/csv（單一報表；statement = IS | BS | CF）
 * 缺值輸出 n/a（絕不是 0）。UTF-8 BOM 讓 Excel 正確讀中文。
 */
export default defineEventHandler(async (event) => {
  const query = getQuery(event)
  const ticker = parseTickers(query.ticker)[0]
  const range = parseRange(query as Record<string, unknown>)
  const statement = String(query.statement ?? 'IS').toUpperCase()
  if (!['IS', 'BS', 'CF'].includes(statement)) {
    throw createError({ statusCode: 400, statusMessage: 'Bad Request', message: 'statement 須為 IS、BS 或 CF' })
  }

  const ref = await resolveTicker(ticker)
  if (!ref) {
    throw createError({ statusCode: 404, statusMessage: 'Not Found', message: `找不到「${ticker}」` })
  }
  const fin = clampPeriods(await getFinancials(ref, range.fromFy, range.toFy), range)

  const esc = (s: string) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s)
  const header = ['科目', 'Line Item', ...fin.periods].map(esc).join(',')
  const rows = fin.lineItems
    .filter((li) => li.statement === statement)
    .map((li) =>
      [
        esc(li.zh),
        esc(li.en),
        ...fin.periods.map((p) => {
          const c = li.values[p]
          return c?.value == null ? 'n/a' : String(c.value)
        }),
      ].join(','),
    )

  setHeader(event, 'Content-Type', 'text/csv; charset=utf-8')
  setHeader(
    event,
    'Content-Disposition',
    `attachment; filename="${ref.ticker}_${statement}_${fin.periods[0] ?? ''}-${fin.periods.at(-1) ?? ''}.csv"`.replace(/\s/g, ''),
  )
  return '﻿' + [header, ...rows].join('\r\n')
})
