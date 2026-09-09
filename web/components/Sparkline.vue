<script setup lang="ts">
/**
 * 轉折訊號用的迷你折線。純 SVG，不用外部函式庫（與 ComboChart／WaterfallChart 同一條規則）。
 *
 * 三件和大圖一樣的規矩：
 *   1. **y 軸一定含 0**。這一頁畫的多半是「年變化（百分點）」，0 就是「沒有變化」那條線
 *      —— 截掉 0 的話 +0.1pp 和 +5pp 看起來一樣高。
 *   2. **null 斷線，不內插**。缺一期就把線斷開，不要拿兩端連起來假裝有資料。
 *   3. **推算值（Q4 = 年報 − 前三季）標成空心點**，與整站的橘色「推算」標記同義。
 */
const props = withDefaults(defineProps<{
  values: (number | null)[]
  estimated?: boolean[]
  /** 最新一點的狀態，決定收尾那顆點的顏色 */
  state?: string
  height?: number
}>(), { height: 40, state: 'flat' })

const W = 200
const H = 40
const PAD = 3

const pts = computed(() => {
  const xs = props.values.filter((v): v is number => v != null && Number.isFinite(v))
  if (!xs.length) return null
  let lo = Math.min(0, ...xs)
  let hi = Math.max(0, ...xs)
  if (lo === hi) { hi = lo + 1 }
  const n = props.values.length
  const x = (i: number) => (n <= 1 ? W / 2 : PAD + (i * (W - PAD * 2)) / (n - 1))
  const y = (v: number) => H - PAD - ((v - lo) / (hi - lo)) * (H - PAD * 2)
  return {
    zeroY: y(0),
    dots: props.values.map((v, i) => (v == null ? null : { x: x(i), y: y(v), est: !!props.estimated?.[i] })),
  }
})

/** null 斷線：把連續有值的段落各自畫成一條 polyline */
const segments = computed(() => {
  if (!pts.value) return []
  const out: string[] = []
  let cur: string[] = []
  for (const d of pts.value.dots) {
    if (d) cur.push(`${d.x.toFixed(1)},${d.y.toFixed(1)}`)
    else { if (cur.length > 1) out.push(cur.join(' ')); cur = [] }
  }
  if (cur.length > 1) out.push(cur.join(' '))
  return out
})
const lastDot = computed(() => [...(pts.value?.dots ?? [])].reverse().find(Boolean) ?? null)
</script>

<template>
  <svg v-if="pts" class="spark" :viewBox="`0 0 ${W} ${H}`" preserveAspectRatio="none"
       :style="{ height: `${height}px` }" role="img" aria-hidden="true">
    <line :x1="0" :x2="W" :y1="pts.zeroY" :y2="pts.zeroY" class="zero" vector-effect="non-scaling-stroke" />
    <polyline v-for="(s, i) in segments" :key="i" :points="s" class="ln"
              vector-effect="non-scaling-stroke" />
    <template v-for="(d, i) in pts.dots" :key="i">
      <circle v-if="d?.est" :cx="d.x" :cy="d.y" r="2.4" class="est" vector-effect="non-scaling-stroke" />
    </template>
    <circle v-if="lastDot" :cx="lastDot.x" :cy="lastDot.y" r="2.8" :class="['end', `s-${state}`]"
            vector-effect="non-scaling-stroke" />
  </svg>
  <div v-else class="spark empty">無資料</div>
</template>

<style scoped>
.spark { width: 100%; display: block; overflow: visible; }
.zero { stroke: var(--rule); stroke-width: 1; stroke-dasharray: 3 3; }
.ln { fill: none; stroke: var(--ink-2); stroke-width: 1.4; }
.est { fill: var(--surface); stroke: var(--sig); stroke-width: 1.2; }
.end { stroke: var(--surface); stroke-width: 1.5; fill: var(--ink); }
.end.s-good { fill: var(--pos); }
.end.s-warn { fill: var(--neg); }
.end.s-info { fill: var(--ink-3); }
.empty { display: flex; align-items: center; font-family: var(--mono); font-size: 10.5px;
  color: var(--ink-3); height: 40px; }
</style>
