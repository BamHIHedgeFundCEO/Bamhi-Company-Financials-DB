<script setup lang="ts">
interface Filing {
  form: string
  fiscalPeriod: string
  reportDate: string
  filingDate: string
  url: string
  isAmendment: boolean
}
interface FilingsResult {
  company: string
  cik: string
  ticker: string
  isForeignIssuer: boolean
  filings: Filing[]
}

const props = defineProps<{ initialTicker?: string }>()

const ticker = ref(props.initialTicker ?? '')
const thisYear = new Date().getFullYear()
const years = Array.from({ length: 12 }, (_, i) => thisYear + 1 - i)
const fromFy = ref(thisYear - 3)
const fromQ = ref(1)
const toFy = ref(thisYear + 1)
const toQ = ref(4)

const loading = ref(false)
const error = ref('')
const result = ref<FilingsResult | null>(null)

const rangeQS = computed(() => `from=${fromFy.value}Q${fromQ.value}&to=${toFy.value}Q${toQ.value}`)

async function search() {
  const t = ticker.value.trim()
  if (!t) return
  loading.value = true
  error.value = ''
  result.value = null
  try {
    result.value = await $fetch<FilingsResult>(`/api/filings?ticker=${encodeURIComponent(t)}&${rangeQS.value}`)
  } catch (e: any) {
    error.value = e?.data?.message || e?.message || '查詢失敗，請稍後再試'
  } finally {
    loading.value = false
  }
}

/** 依序開啟全部 SEC 官方連結，間隔 300ms（伺服器不參與下載） */
function openAll() {
  const filings = result.value?.filings ?? []
  filings.forEach((f, i) => setTimeout(() => window.open(f.url, '_blank'), i * 300))
}

function csvUrl(statement: string) {
  return `/api/financials/csv?ticker=${encodeURIComponent(ticker.value.trim())}&${rangeQS.value}&statement=${statement}`
}
const excelUrl = computed(
  () => `/api/financials/excel?ticker=${encodeURIComponent(ticker.value.trim())}&${rangeQS.value}`,
)

// 個股頁預填時自動查詢
onMounted(() => {
  if (props.initialTicker) search()
})
</script>

<template>
  <section class="query wrap">
    <div class="eyebrow">輸入美股代號，取得 SEC 官方財報直連與可建模 Excel</div>
    <div class="tickerline">
      <div class="tickerfield">
        <span class="fieldtag">Ticker</span>
        <input
          v-model="ticker"
          type="text"
          placeholder="NVDA"
          spellcheck="false"
          autocomplete="off"
          @keydown.enter="search"
        />
      </div>
      <div v-if="result" class="resolved">
        <div class="name">{{ result.company }}</div>
        <div class="meta">
          <span>CIK {{ result.cik }}</span>
          <span>{{ result.filings.length }} 份申報</span>
          <span v-if="result.isForeignIssuer" class="flag">外國發行人</span>
        </div>
      </div>
    </div>

    <div class="rangeline">
      <span>期間</span>
      <select v-model.number="fromFy"><option v-for="y in years" :key="y" :value="y">{{ y }}</option></select>
      <select v-model.number="fromQ"><option v-for="q in 4" :key="q" :value="q">Q{{ q }}</option></select>
      <span>～</span>
      <select v-model.number="toFy"><option v-for="y in years" :key="y" :value="y">{{ y }}</option></select>
      <select v-model.number="toQ"><option v-for="q in 4" :key="q" :value="q">Q{{ q }}</option></select>
      <button class="go" :disabled="loading || !ticker.trim()" @click="search">查詢</button>
    </div>

    <slot name="below-query" />

    <div v-if="loading" class="loadline">查詢 SEC EDGAR 中…</div>

    <div v-if="error" class="panel" style="margin-top: 24px">
      <h3>查無結果</h3>
      <p>{{ error }}</p>
    </div>

    <div v-if="result" class="results">
      <div v-if="result.isForeignIssuer" class="note">
        <span class="tag">外國發行人</span>
        <span>此公司為外國發行人，無 10-Q 季報。以下列出 20-F 年報與 6-K 不定期申報。</span>
      </div>

      <div class="blockhead" style="margin-top: 16px">
        <h2>財報清單</h2>
        <span class="hint">直連 sec.gov，本站不經手檔案</span>
      </div>

      <table class="filings">
        <thead>
          <tr>
            <th class="idx">#</th>
            <th>季別</th>
            <th>表單</th>
            <th>期末日</th>
            <th>申報日</th>
            <th style="text-align: right">下載</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(f, i) in result.filings" :key="f.url">
            <td class="idx">{{ String(i + 1).padStart(2, '0') }}</td>
            <td class="per">{{ f.fiscalPeriod }}</td>
            <td class="form">
              <span :class="{ k: f.form.startsWith('10-K') || f.form.startsWith('20-F') }">
                {{ f.form }}{{ f.isAmendment ? '（修正版）' : '' }}
              </span>
            </td>
            <td class="date">{{ f.reportDate }}</td>
            <td class="date">{{ f.filingDate }}</td>
            <td class="dl"><a :href="f.url" target="_blank" rel="noopener">SEC 原文 ↗</a></td>
          </tr>
        </tbody>
      </table>

      <div class="actions">
        <button class="btn" @click="openAll">
          全部原始財報
          <small>瀏覽器依序開啟 {{ result.filings.length }} 個 SEC 官方連結</small>
        </button>
        <a class="btn ghost" :href="excelUrl">
          下載 Excel
          <small>6 分頁 · 中英對照 · 指標全公式</small>
        </a>
        <a class="btn ghost" :href="csvUrl('IS')">CSV 損益表</a>
        <a class="btn ghost" :href="csvUrl('BS')">CSV 資產負債表</a>
        <a class="btn ghost" :href="csvUrl('CF')">CSV 現金流量表</a>
      </div>
    </div>

    <p class="disclaim">
      資料來源為美國證券交易委員會（SEC）EDGAR 公開資料，本站與 SEC 無任何隸屬關係。
      財報檔案由 sec.gov 直接提供，本站僅整理連結與 XBRL 數據，不構成投資建議。
      缺值以 n/a 表示（SEC 無該標籤不代表數值為零）；Q4 單季數字為推算值（FY − Q1 − Q2 − Q3）。
    </p>
  </section>
</template>
