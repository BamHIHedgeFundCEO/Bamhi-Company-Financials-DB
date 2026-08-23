<script setup lang="ts">
/** 個股頁的分頁導覽。Excel 下載仍在「財報下載」那一頁，不受影響。 */
const props = defineProps<{ ticker: string; company?: string | null; zh?: string; meta?: string[] }>()

const TABS = [
  { to: '', label: '財報下載', hint: 'SEC 原始檔 + Excel' },
  { to: '/profile', label: '公司簡介', hint: '業務・高管・風險' },
  { to: '/financials', label: '財務報表', hint: '損益・資產負債・瀑布' },
  { to: '/funds', label: '13F', hint: '機構持股' },
  { to: '/insider', label: '內部人買賣', hint: 'Form 4' },
]
const base = computed(() => `/stock/${props.ticker}`)
const route = useRoute()
const cur = computed(() => {
  const p = route.path.replace(/\/$/, '')
  const rest = p.slice(base.value.length)
  return TABS.find((t) => t.to === rest)?.to ?? ''
})
</script>

<template>
  <div class="tickerhead">
    <div class="wrap">
      <div class="idline">
        <NuxtLink :to="base" class="tk">{{ ticker }}</NuxtLink>
        <span v-if="company" class="co">{{ company }}<i v-if="zh">{{ zh }}</i></span>
        <span v-if="meta?.length" class="mt">
          <span v-for="m in meta" :key="m">{{ m }}</span>
        </span>
      </div>
      <nav class="tabs">
        <NuxtLink v-for="t in TABS" :key="t.to" :to="base + t.to" :class="{ on: cur === t.to }">
          <b>{{ t.label }}</b><small>{{ t.hint }}</small>
        </NuxtLink>
      </nav>
    </div>
  </div>
</template>

<style scoped>
.tickerhead { background: var(--surface); border-bottom: 1px solid var(--ink); }
.idline { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; padding: 14px 0 8px; }
.tk { font-family: var(--mono); font-weight: 700; font-size: 26px; letter-spacing: -.01em;
  color: var(--ink); text-decoration: none; }
.co { font-size: 14px; font-weight: 600; color: var(--ink); }
.co i { font-style: normal; font-weight: 500; color: var(--ink-2); margin-left: 8px; }
.mt { font-family: var(--mono); font-size: 11px; color: var(--ink-3); display: flex; flex-wrap: wrap; }
.mt span { padding-right: 10px; margin-right: 10px; border-right: 1px solid var(--rule-2); }
.mt span:last-child { border-right: 0; }
.tabs { display: flex; gap: 0; overflow-x: auto; }
.tabs a { display: grid; gap: 1px; padding: 8px 16px 9px; text-decoration: none; color: var(--ink-2);
  border-bottom: 2px solid transparent; white-space: nowrap; }
.tabs a b { font-size: 13.5px; font-weight: 600; }
.tabs a small { font-size: 10.5px; color: var(--ink-3); font-family: var(--mono); }
.tabs a:first-child { padding-left: 0; }
.tabs a:hover { color: var(--ink); background: var(--green-wash); }
.tabs a.on { color: var(--ink); border-bottom-color: var(--sig); }
.tabs a.on small { color: var(--ink-2); }
</style>
