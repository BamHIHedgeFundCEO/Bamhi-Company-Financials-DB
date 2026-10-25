<script setup lang="ts">
import type { Scale } from '~/utils/fmt'
/**
 * 利潤瀑布：營收怎麼一層一層被吃掉，剩下淨利。
 *
 * 小計（毛利／營業利益／稅前淨利／淨利）一律用**公司自己申報的數字**，
 * 不用前面幾根加總 —— 加總對不上的時候，差額要以「其他」明確畫出來，
 * 而不是把小計改成算出來的值把差額藏掉。
 */
export interface Step {
  label: string
  value: number
  /** base = 從 0 起算的第一根；subtotal = 從 0 起算的小計；delta = 增減 */
  kind: 'base' | 'delta' | 'subtotal'
  /** 殘差推算出來的（例如「其他營業費用」），要標註 */
  plug?: boolean
}

const props = withDefaults(defineProps<{
  steps: Step[]
  height?: number
  unit?: string
  div?: number
}>(), { height: 320, unit: '', div: 1 })

const wrapEl = ref<HTMLElement | null>(null)
const width = ref(880)
let ro: ResizeObserver | null = null
onMounted(() => {
  if (!wrapEl.value) return
  ro = new ResizeObserver((e) => { width.value = Math.max(320, e[0].contentRect.width) })
  ro.observe(wrapEl.value)
})
onUnmounted(() => ro?.disconnect())

const PAD = { l: 52, r: 14, t: 18, b: 42 }
const iw = computed(() => Math.max(80, width.value - PAD.l - PAD.r))
const ih = computed(() => Math.max(80, props.height - PAD.t - PAD.b))

/** 每根的 [起, 迄]（累計值） */
const spans = computed(() => {
  let run = 0
  return props.steps.map((s) => {
    if (s.kind === 'base' || s.kind === 'subtotal') {
      run = s.value
      return { from: 0, to: s.value, s }
    }
    const from = run
    run += s.value
    return { from, to: run, s }
  })
})

const axis = computed(() =>
  axisFor(spans.value.flatMap((x) => [x.from / props.div, x.to / props.div])))
const n = computed(() => props.steps.length || 1)
const bandW = computed(() => iw.value / n.value)
const barW = computed(() => Math.min(64, bandW.value * 0.62))
const cx = (i: number) => PAD.l + bandW.value * (i + 0.5)
const y = (v: number) =>
  PAD.t + ih.value * (1 - (v / props.div - axis.value.min) / (axis.value.max - axis.value.min || 1))

const scale = computed<Scale>(() => ({ div: props.div, unit: props.unit }))
function colorOf(s: Step) {
  if (s.kind === 'base') return 'var(--ink)'
  if (s.kind === 'subtotal') return 'var(--series-3)'
  return s.value >= 0 ? 'var(--series-1)' : 'var(--series-2)'
}
const hover = ref<number | null>(null)

/**
 * 窄螢幕改成橫向瀑布：十根直條擠在 390px 裡，每根 30px，名稱與數字一定互相壓到。
 * 橫向時每一步一列，名稱在左、長條往右、數字接在長條尾端，列與列之間用虛線接著。
 */
const compact = computed(() => width.value < 560)
const ROW = 32
const HP = { l: 84, r: 48, t: 6, b: 22 }
const hw = computed(() => Math.max(80, width.value - HP.l - HP.r))
const hx = (v: number) =>
  HP.l + hw.value * ((v / props.div - axis.value.min) / (axis.value.max - axis.value.min || 1))
const hHeight = computed(() => HP.t + n.value * ROW + HP.b)
const rowY = (i: number) => HP.t + i * ROW
function onMove(e: MouseEvent) {
  const r = (e.currentTarget as SVGElement).getBoundingClientRect()
  const i = Math.floor((e.clientX - r.left - PAD.l) / bandW.value)
  hover.value = i >= 0 && i < n.value ? i : null
}
</script>

<template>
  <div ref="wrapEl" class="chartwrap">
    <svg v-if="compact" :viewBox="`0 0 ${width} ${hHeight}`" :height="hHeight" :width="width" role="img">
      <g class="grid">
        <template v-for="t in axis.list" :key="`h${t}`">
          <line :x1="hx(t * div)" :x2="hx(t * div)" :y1="HP.t" :y2="HP.t + n * ROW" :class="{ zero: t === 0 }" />
          <text :x="hx(t * div)" :y="HP.t + n * ROW + 14" class="ax mid">{{ fmtTick(t * div, scale) }}</text>
        </template>
      </g>
      <g class="conn">
        <line v-for="(sp, i) in spans.slice(0, -1)" :key="`hc${i}`"
              :x1="hx(sp.to)" :x2="hx(sp.to)" :y1="rowY(i) + ROW - 7" :y2="rowY(i + 1) + 7" />
      </g>
      <g v-for="(sp, i) in spans" :key="`hb${i}`" @click="hover = hover === i ? null : i">
        <text :x="HP.l - 8" :y="rowY(i) + ROW / 2 + 4" class="hlab"
              :class="{ on: hover === i, sub: sp.s.kind !== 'delta' }">{{ sp.s.label }}</text>
        <rect :x="Math.min(hx(sp.from), hx(sp.to))" :y="rowY(i) + 7" :height="ROW - 14"
              :width="Math.max(1.5, Math.abs(hx(sp.to) - hx(sp.from)))"
              :fill="colorOf(sp.s)" :opacity="hover === null || hover === i ? 1 : 0.4"
              :stroke-dasharray="sp.s.plug ? '3 2' : undefined"
              :stroke="sp.s.plug ? 'var(--ink)' : undefined" />
        <text :x="Math.max(hx(sp.from), hx(sp.to)) + 5" :y="rowY(i) + ROW / 2 + 4" class="val start">
          {{ fmtScaled(sp.s.value, scale) }}
        </text>
      </g>
    </svg>

    <svg v-else :viewBox="`0 0 ${width} ${height}`" :height="height" :width="width" role="img"
         @mousemove="onMove" @mouseleave="hover = null">
      <g class="grid">
        <template v-for="t in axis.list" :key="`g${t}`">
          <line :x1="PAD.l" :x2="PAD.l + iw" :y1="y(t * div)" :y2="y(t * div)" :class="{ zero: t === 0 }" />
          <text :x="PAD.l - 8" :y="y(t * div) + 3.5" class="ax">{{ fmtTick(t * div, scale) }}</text>
        </template>
      </g>

      <!-- 連接線：讓讀者看得出每一根是接著上一根扣的 -->
      <g class="conn">
        <line v-for="(sp, i) in spans.slice(0, -1)" :key="`c${i}`"
              :x1="cx(i) - barW / 2" :x2="cx(i + 1) + barW / 2"
              :y1="y(sp.to)" :y2="y(sp.to)" />
      </g>

      <g v-for="(sp, i) in spans" :key="`b${i}`">
        <rect :x="cx(i) - barW / 2" :width="barW"
              :y="Math.min(y(sp.from), y(sp.to))"
              :height="Math.max(1.5, Math.abs(y(sp.to) - y(sp.from)))"
              :fill="colorOf(sp.s)" :opacity="hover === null || hover === i ? 1 : 0.4"
              :stroke-dasharray="sp.s.plug ? '3 2' : undefined"
              :stroke="sp.s.plug ? 'var(--ink)' : undefined" />
        <text :x="cx(i)" :y="Math.min(y(sp.from), y(sp.to)) - 5" class="val">
          {{ fmtScaled(sp.s.kind === 'delta' ? sp.s.value : sp.s.value, scale) }}
        </text>
      </g>

      <line :x1="PAD.l" :x2="PAD.l + iw" :y1="y(0)" :y2="y(0)" class="axisline" />
      <g class="xlab">
        <text v-for="(s, i) in steps" :key="s.label" :x="cx(i)" :y="PAD.t + ih + 16"
              :class="{ on: hover === i, sub: s.kind !== 'delta' }">{{ s.label }}</text>
      </g>
    </svg>

    <div class="legend">
      <span><i style="background:var(--ink)" />起點／終點</span>
      <span><i style="background:var(--series-3)" />小計（公司申報值）</span>
      <span><i style="background:var(--series-2)" />扣除</span>
      <span><i style="background:var(--series-1)" />增加</span>
      <span v-if="steps.some((s) => s.plug)"><i class="plug" />虛線＝差額（其他）</span>
      <span v-if="unit" class="u">單位：{{ unit }}</span>
    </div>
  </div>
</template>

<style scoped>
.chartwrap { position: relative; width: 100%; }
svg { display: block; max-width: 100%; }
.grid line { stroke: var(--rule-2); }
.grid line.zero { stroke: var(--rule); }
.axisline { stroke: var(--ink); }
.conn line { stroke: var(--ink-3); stroke-width: 1; stroke-dasharray: 2 2; }
text.ax { font-family: var(--mono); font-size: 10px; fill: var(--ink-3); text-anchor: end; }
text.ax.mid { text-anchor: middle; }
text.val.start { text-anchor: start; }
.hlab { font-size: 12px; fill: var(--ink-2); text-anchor: end; }
.hlab.sub { font-weight: 600; fill: var(--ink); }
.hlab.on { fill: var(--ink); font-weight: 700; }
text.val { font-family: var(--mono); font-size: 10px; fill: var(--ink-2); text-anchor: middle; }
.xlab text { font-size: 11px; fill: var(--ink-2); text-anchor: middle; }
.xlab text.sub { font-weight: 600; fill: var(--ink); }
.xlab text.on { fill: var(--ink); font-weight: 700; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin-top: 10px;
  font-size: 11.5px; color: var(--ink-2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i { width: 10px; height: 10px; display: inline-block; flex: none; }
.legend i.plug { border: 1px dashed var(--ink); }
.legend .u { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); margin-left: auto; }
</style>
