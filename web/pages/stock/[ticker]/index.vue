<script setup lang="ts">
/**
 * 個股頁。SSG：useAsyncData 在 prerender 時打 /api/filings，
 * 最近 8 季清單與公司資訊寫進靜態 HTML（SEO 用）；
 * 互動部分（StockTool）client-side 掛載後仍以 API 取最新狀態，兩者不衝突。
 */
const route = useRoute()
const ticker = String(route.params.ticker || '').toUpperCase()

// 熱門標的中文名（SEO title 用；不在表內的只用英文名）
const zhNames: Record<string, string> = {
  NVDA: '輝達', AAPL: '蘋果', TSLA: '特斯拉', MSFT: '微軟', AMZN: '亞馬遜',
  GOOGL: '字母', GOOG: '字母', META: 'Meta', AMD: '超微', TSM: '台積電', PLTR: 'Palantir',
}
const zh = zhNames[ticker] ?? ''

const { data: seo } = await useAsyncData(`filings-${ticker}`, async () => {
  try {
    const thisYear = new Date().getFullYear()
    return await $fetch<any>(
      `/api/filings?ticker=${ticker}&from=${thisYear - 2}Q1&to=${thisYear + 1}Q4`,
    )
  } catch {
    return null
  }
})

const recent = computed(() => (seo.value?.filings ?? []).slice(0, 8))
const sameIndustry = ['NVDA', 'AAPL', 'TSLA', 'MSFT', 'AMZN', 'GOOGL', 'META', 'AMD', 'TSM', 'PLTR'].filter(
  (t) => t !== ticker,
).slice(0, 6)

const title = `${ticker} ${zh} 財報下載｜10-K、10-Q 季度財務報表與 Excel 下載`
useHead({
  title,
  meta: [
    {
      name: 'description',
      content: `${seo.value?.company ?? ticker}${zh ? `（${zh}）` : ''} SEC 財報官方直連下載與三大報表季度數據 Excel（中英對照、指標全公式）。`,
    },
  ],
  link: [{ rel: 'canonical', href: `https://bamhi-company-financials.vercel.app/stock/${ticker}` }],
  script: [
    {
      type: 'application/ld+json',
      innerHTML: JSON.stringify({
        '@context': 'https://schema.org',
        '@type': 'Dataset',
        name: title,
        description: `${ticker} 歷年 10-K / 10-Q 財報與 XBRL 季度財務數據`,
        creator: { '@type': 'Organization', name: 'BamHI' },
        isBasedOn: 'https://www.sec.gov/',
      }),
    },
  ],
})
</script>

<template>
  <div>
    <TickerTabs
      :ticker="ticker" :company="seo?.company" :zh="zh"
      :meta="seo ? [`CIK ${seo.cik}`, seo.sic, seo.isForeignIssuer ? '外國發行人' : '美國申報人'].filter(Boolean) as string[] : []"
    />
    <StockTool :initial-ticker="ticker" />

    <section class="seoblock wrap" style="margin-left: auto; margin-right: auto">
      <h1>{{ ticker }}{{ zh ? ` ${zh}` : '' }} 財報下載</h1>
      <p v-if="seo">
        {{ seo.company }}（CIK {{ seo.cik }}）{{ seo.sic ? `，產業：${seo.sic}。` : '。' }}
        本頁提供 10-K 年報與 10-Q 季報的 SEC 官方直連下載，以及三大財務報表季度時間序列的繁中 Excel。
      </p>

      <template v-if="recent.length">
        <h2>最近申報</h2>
        <ul>
          <li v-for="f in recent" :key="f.url">
            <a :href="f.url" target="_blank" rel="noopener">
              {{ f.fiscalPeriod }} · {{ f.form }} · 期末 {{ f.reportDate }}
            </a>
          </li>
        </ul>
      </template>

      <h2>其他熱門標的</h2>
      <ul>
        <li v-for="t in sameIndustry" :key="t">
          <NuxtLink :to="`/stock/${t}`">{{ t }} 財報下載</NuxtLink>
        </li>
      </ul>
    </section>
  </div>
</template>
