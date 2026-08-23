<script setup lang="ts">
import type { Scale } from '~/utils/fmt'
/**
 * 直條 + 折線的雙軸圖（純 SVG，無外部圖表函式庫）。
 *
 * 兩條硬規則：
 * 1. 長條的左軸**一定含 0**。截軸的長條圖會讓 3% 的差看起來像三倍，那是說謊。
 * 2. `null` 一律斷線、不內插。缺的那一季就是缺，連過去等於編造。
 */
export interface BarSeries { name: string; color: string; values: (number | null)[] }
export interface LineSeries {
  name: string
  color: string
  values: (number | null)[]
  /** 'pct' 走右軸並以 % 呈現；'left' 與長條共用左軸 */
  axis?: 'pct' | 'left'
  dash?: boolean
}

const props = withDefaults(defineProps<{
  labels: string[]
  bars?: BarSeries[]
  lines?: LineSeries[]
  height?: number
  /** 左軸單位文字，例如「億美元」 */
  unit?: string
  /** 左軸的縮放倍數（由呼叫端以 pickScale 決定，整張圖共用一個） */
  div?: number
  /** 每個 x 標籤下方的次標籤（例如推算值標記） */
  marks?: (string | null)[]
}>(), { bars: () => [], lines: () => [], height: 300, unit: '', div: 1, marks: () => [] })

const wrapEl = ref<HTMLElement | null>(null)
const width = ref(880)
let ro: ResizeObserver | null = null
onMounted(() => {
  if (!wrapEl.value) return
  ro = new ResizeObserver((e) => { width.value = Math.max(320, e[0].contentRect.width) })
  ro.observe(wrapEl.value)
})
onUnmounted(() => ro?.disconnect())

const PAD = computed(() => ({
  l: 52,
  r: props.lines?.some((l) => (l.axis ?? 'pct') === 'pct') ? 46 : 14,
  t: 12,
  b: props.marks?.some(Boolean) ? 40 : 28,
}))
const iw = computed(() => Math.max(80, width.value - PAD.value.l - PAD.value.r))
const ih = computed(() => Math.max(80, props.height - PAD.value.t - PAD.value.b))

const leftVals = computed(() => [
  ...props.bars.flatMap((b) => b.values),
  ...props.lines.filter((l) => l.axis === 'left').flatMap((l) => l.values),
])
const leftAxis = computed(() => axisFor(leftVals.value.map((v) => (v == null ? null : v / props.div))))
const pctLines = computed(() => props.lines.filter((l) => (l.axis ?? 'pct') === 'pct'))
const rightAxis = computed(() => axisFor(pctLines.value.flatMap((l) => l.values), 4, true))

const n = computed(() => props.labels.length || 1)
const bandW = computed(() => iw.value / n.value)
const cx = (i: number) => PAD.value.l + bandW.value * (i + 0.5)
const yL = (v: number) => {
  const a = leftAxis.value
  return PAD.value.t + ih.value * (1 - (v / props.div - a.min) / (a.max - a.min || 1))
}
const yR = (v: number) => {
  const a = rightAxis.value
  return PAD.value.t + ih.value * (1 - (v - a.min) / (a.max - a.min || 1))
}
const zeroY = computed(() => yL(0))

const groupW = computed(() => bandW.value * 0.68)
const barW = computed(() => Math.max(2, groupW.value / Math.max(1, props.bars.length) - 2))
function barX(si: number, i: number) {
  const start = cx(i) - groupW.value / 2
  return start + si * (groupW.value / Math.max(1, props.bars.length)) + 1
}

/** 折線分段：遇 null 斷開，避免把缺值內插成一條假的趨勢 */
function segments(vals: (number | null)[], scaleFn: (v: number) => number) {
  const out: string[] = []
  let cur: string[] = []
  vals.forEach((v, i) => {
    if (v == null || !Number.isFinite(v)) {
      if (cur.length > 1) out.push(cur.join(' '))
      cur = []
      return
    }
    cur.push(`${cur.length ? 'L' : 'M'}${cx(i).toFixed(1)},${scaleFn(v).toFixed(1)}`)
  })
  if (cur.length > 1) out.push(cur.join(' '))
  return out
}

const hover = ref<number | null>(null)
function onMove(e: MouseEvent) {
  const r = (e.currentTarget as SVGElement).getBoundingClientRect()
  const x = e.clientX - r.left - PAD.value.l
  const i = Math.floor(x / bandW.value)
  hover.value = i >= 0 && i < n.value ? i : null
}
const scale = computed<Scale>(() => ({ div: props.div, unit: props.unit }))
const tipRight = computed(() => hover.value != null && cx(hover.value) > PAD.value.l + iw.value * 0.6)

// x 軸標籤過密就跳著標
const labelEvery = computed(() => Math.ceil(n.value / Math.max(4, Math.floor(iw.value / 62))))
</script>

<template>
  <div ref="wrapEl" class="chartwrap">
    <svg :viewBox="`0 0 ${width} ${height}`" :height="height" :width="width" role="img"
         @mousemove="onMove" @mouseleave="hover = null">
      <!-- 左軸格線 -->
      <g class="grid">
        <template v-for="t in leftAxis.list" :key="`g${t}`">
          <line :x1="PAD.l" :x2="PAD.l + iw" :y1="yL(t * div)" :y2="yL(t * div)"
                :class="{ zero: t === 0 }" />
          <text :x="PAD.l - 8" :y="yL(t * div) + 3.5" class="ax">{{ fmtTick(t * div, scale) }}</text>
        </template>
      </g>
      <!-- 右軸（%） -->
      <g v-if="pctLines.length" class="grid right">
        <text v-for="t in rightAxis.list" :key="`r${t}`" :x="PAD.l + iw + 8" :y="yR(t) + 3.5" class="ax">
          {{ (t * 100).toFixed(0) }}%
        </text>
      </g>

      <!-- hover 帶 -->
      <rect v-if="hover !== null" :x="PAD.l + bandW * hover" :y="PAD.t" :width="bandW" :height="ih"
            class="hoverband" />

      <!-- 長條 -->
      <g v-for="(b, si) in bars" :key="`b${b.name}`">
        <template v-for="(v, i) in b.values" :key="i">
          <rect v-if="v != null" :x="barX(si, i)" :width="barW"
                :y="Math.min(yL(v), zeroY)" :height="Math.max(1, Math.abs(yL(v) - zeroY))"
                :fill="b.color" :opacity="hover === null || hover === i ? 1 : 0.35" />
        </template>
      </g>

      <!-- 折線 -->
      <g v-for="l in lines" :key="`l${l.name}`" class="ln">
        <path v-for="(d, k) in segments(l.values, (l.axis ?? 'pct') === 'pct' ? yR : yL)" :key="k"
              :d="d" :stroke="l.color" :stroke-dasharray="l.dash ? '4 3' : undefined" fill="none" />
        <template v-for="(v, i) in l.values" :key="`p${i}`">
          <circle v-if="v != null" :cx="cx(i)"
                  :cy="((l.axis ?? 'pct') === 'pct' ? yR : yL)(v)"
                  :r="hover === i ? 3.4 : 2" :fill="l.color" />
        </template>
      </g>

      <!-- x 軸 -->
      <line :x1="PAD.l" :x2="PAD.l + iw" :y1="PAD.t + ih" :y2="PAD.t + ih" class="axisline" />
      <g class="xlab">
        <template v-for="(lb, i) in labels" :key="lb">
          <text v-if="i % labelEvery === 0 || hover === i" :x="cx(i)" :y="PAD.t + ih + 15"
                :class="{ on: hover === i }">{{ lb }}</text>
          <text v-if="marks[i]" :x="cx(i)" :y="PAD.t + ih + 28" class="mark">{{ marks[i] }}</text>
        </template>
      </g>
    </svg>

    <div v-if="hover !== null" class="tip" :class="{ right: tipRight }"
         :style="{ left: `${cx(hover)}px` }">
      <div class="tl">{{ labels[hover] }}<span v-if="marks[hover]" class="est">{{ marks[hover] }}</span></div>
      <div v-for="b in bars" :key="b.name" class="tr">
        <i :style="{ background: b.color }" /><span>{{ b.name }}</span>
        <b>{{ fmtScaled(b.values[hover], scale) }}<em v-if="b.values[hover] != null">{{ unit }}</em></b>
      </div>
      <div v-for="l in lines" :key="l.name" class="tr">
        <i class="line" :style="{ background: l.color }" /><span>{{ l.name }}</span>
        <b>{{ (l.axis ?? 'pct') === 'pct'
          ? fmtPct(l.values[hover]) : fmtScaled(l.values[hover], scale) }}</b>
      </div>
    </div>

    <div class="legend">
      <span v-for="b in bars" :key="b.name"><i :style="{ background: b.color }" />{{ b.name }}</span>
      <span v-for="l in lines" :key="l.name"><i class="line" :style="{ background: l.color }" />{{ l.name }}</span>
      <span v-if="unit" class="u">左軸單位：{{ unit }}</span>
    </div>
  </div>
</template>

<style scoped>
.chartwrap { position: relative; width: 100%; }
svg { display: block; max-width: 100%; }
.grid line { stroke: var(--rule-2); stroke-width: 1; }
.grid line.zero { stroke: var(--rule); }
.axisline { stroke: var(--ink); stroke-width: 1; }
text.ax { font-family: var(--mono); font-size: 10px; fill: var(--ink-3); text-anchor: end; }
.grid.right text.ax { text-anchor: start; }
.xlab text { font-family: var(--mono); font-size: 10px; fill: var(--ink-3); text-anchor: middle; }
.xlab text.on { fill: var(--ink); font-weight: 700; }
.xlab text.mark { font-size: 9px; fill: var(--sig); }
.ln path { stroke-width: 1.8; stroke-linejoin: round; stroke-linecap: round; }
.hoverband { fill: var(--green-wash); opacity: .55; }
.legend { display: flex; flex-wrap: wrap; gap: 4px 16px; margin-top: 10px;
  font-size: 11.5px; color: var(--ink-2); }
.legend span { display: inline-flex; align-items: center; gap: 6px; }
.legend i, .tip i { width: 10px; height: 10px; display: inline-block; flex: none; }
.legend i.line, .tip i.line { height: 3px; }
.legend .u { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); margin-left: auto; }
.tip { position: absolute; top: 4px; transform: translateX(10px); background: var(--surface);
  border: 1px solid var(--ink); padding: 7px 9px; font-size: 11.5px; pointer-events: none;
  min-width: 168px; z-index: 3; box-shadow: 3px 3px 0 rgba(21,23,26,.08); }
.tip.right { transform: translateX(calc(-100% - 10px)); }
.tip .tl { font-family: var(--mono); font-size: 10.5px; letter-spacing: .06em;
  color: var(--ink); border-bottom: 1px solid var(--rule-2); padding-bottom: 4px; margin-bottom: 4px; }
.tip .tl .est { color: var(--sig); margin-left: 6px; }
.tip .tr { display: flex; align-items: center; gap: 6px; line-height: 1.75; }
.tip .tr span { color: var(--ink-2); }
.tip .tr b { margin-left: auto; font-family: var(--mono); font-weight: 500; }
.tip .tr b em { font-style: normal; color: var(--ink-3); font-size: 10px; margin-left: 2px; }
</style>
