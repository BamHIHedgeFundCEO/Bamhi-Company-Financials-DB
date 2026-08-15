import { defineEventHandler, readBody, createError } from 'h3'

/**
 * POST /api/report —— 使用者回報某科目數字有誤 → 開一個 GitHub Issue。
 * body: { ticker, concept, period, note }
 *
 * 需環境變數：
 *   GITHUB_TOKEN  能對 repo 開 issue 的 token（建議 fine-grained：僅 Issues:write）
 *   GITHUB_REPO   預設 BamHIHedgeFundCEO/Bamhi-Company-Financials-DB
 *
 * 這是整個專案唯一會隨時間複利的東西：使用者踩過的坑長成 xbrl_zh_map.json。
 */
export default defineEventHandler(async (event) => {
  const body = await readBody<{ ticker?: string; concept?: string; period?: string; note?: string }>(event)
  const ticker = String(body?.ticker ?? '').trim().toUpperCase().slice(0, 12)
  const concept = String(body?.concept ?? '').trim().slice(0, 80)
  const period = String(body?.period ?? '').trim().slice(0, 40)
  const note = String(body?.note ?? '').trim().slice(0, 1000)

  if (!ticker || !concept) {
    throw createError({ statusCode: 400, statusMessage: 'Bad Request', message: '請填 ticker 與科目' })
  }

  const token = process.env.GITHUB_TOKEN
  const repo = process.env.GITHUB_REPO || 'BamHIHedgeFundCEO/Bamhi-Company-Financials-DB'
  if (!token) {
    // 未設 token：至少記到伺服器 log，不讓使用者的回報消失
    console.log('[report]', JSON.stringify({ ticker, concept, period, note }))
    return { ok: true, stored: 'log' }
  }

  const title = `[數據回報] ${ticker} ${concept}${period ? ` (${period})` : ''}`
  const dupe = await fetch(
    `https://api.github.com/search/issues?q=${encodeURIComponent(`repo:${repo} in:title "${ticker} ${concept}"`)}`,
    { headers: { Authorization: `Bearer ${token}`, Accept: 'application/vnd.github+json' } },
  )
    .then((r) => (r.ok ? r.json() : { total_count: 0 }))
    .catch(() => ({ total_count: 0 }))
  if ((dupe as { total_count: number }).total_count > 0) {
    return { ok: true, deduped: true } // 已有相同回報，不重複開
  }

  const bodyMd = [
    `**Ticker**: ${ticker}`,
    `**科目**: ${concept}`,
    period ? `**期間**: ${period}` : '',
    note ? `\n**使用者說明**:\n${note}` : '',
    `\n---\n來源：網站數據回報按鈕。處理流程見 docs/ITERATING_THE_MAP.md`,
    `建議：\`python tools/coverage.py ${ticker} --suggest\` 定位標籤。`,
  ]
    .filter(Boolean)
    .join('\n')

  const res = await fetch(`https://api.github.com/repos/${repo}/issues`, {
    method: 'POST',
    headers: {
      Authorization: `Bearer ${token}`,
      Accept: 'application/vnd.github+json',
      'Content-Type': 'application/json',
    },
    body: JSON.stringify({ title, body: bodyMd, labels: ['data-report'] }),
  })
  if (!res.ok) {
    console.log('[report:fallback]', JSON.stringify({ ticker, concept, period, note }))
    throw createError({ statusCode: 502, statusMessage: 'Bad Gateway', message: '回報暫時無法送出，已記錄' })
  }
  const issue = (await res.json()) as { html_url: string }
  return { ok: true, url: issue.html_url }
})
