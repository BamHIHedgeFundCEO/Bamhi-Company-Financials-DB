<script setup lang="ts">
/**
 * 總分走勢。純 SVG，不用外部函式庫（與 ComboChart／WaterfallChart／Sparkline 同一條規則）。
 *
 * **不能用 Sparkline 畫這個**：Sparkline 的 y 軸一定含 0，那條規則是給「年變化
 * （百分點）」用的 —— 0 在那裡是「沒有變化」，有意義。總分不一樣：它本來就是
 * 0–100 的有界指數，多數公司整條線落在 40–70 之間，硬把 0 塞進來之後整段走勢
 * 被壓成頂端一條幾乎水平的線，實測看不出 52 → 64 和 52 → 53 的差別。
 *
 * 但也**不能只把 y 軸縮到資料範圍**：那會把 2 分的抖動畫成一整格的暴跌。
 * 折衷是三件事一起做：
 *   1. y 軸固定 0–100（不截軸，比例是誠實的）
 *   2. 背景畫**評級級距的帶狀區**（底 10%／10–30%／30–70%／70–90%／頂 10% 的分位數）
 *      —— 走勢的意義本來就是「跨過了哪一條線」，不是「上升了幾像素」
 *   3. 每一點**直接標數字**。眼睛分不出 4 個像素，但讀得出 52 → 64
 *
 * `null` 一律斷線不內插（那一期覆蓋率不足、根本沒有總分，連起來等於編一個分數）。
 *
 * 尺寸走 ResizeObserver 量出來的**實際像素**、viewBox 與它 1:1（與 ComboChart 同一招）。
 * 固定 viewBox 再用 CSS 撐滿的話，字會跟著縮放 —— 桌機上變成 17px 的巨大數字，
 * 手機上又縮成 5px 看不見。
 */
const props = withDefaults(defineProps<{
  values: (number | null)[]
  labels: string[]
  /** 評級級距（`lt` 是上界，最後一段沒有 lt）。沒給就不畫帶狀區 */
  grades?: { lt?: number; id: string; zh: string }[]
}>(), { grades: () => [] })

const wrap = ref<HTMLElement | null>(null)
const width = ref(760)
let ro: ResizeObserver | null = null
onMounted(() => {
  if (!wrap.value) return
  ro = new ResizeObserver((e) => { width.value = Math.max(300, e[0]!.contentRect.width) })
  ro.observe(wrap.value)
})
onBeforeUnmount(() => ro?.disconnect())

const L = 6
const GUT = 58          // 右側留給級距名稱
// 0–100 全畫出來，所以繪圖區要夠高才看得出走勢：112px 時 1 分只有 1.1 個像素，
// 9 分的變化畫成 10px ——「不截軸」就得用高度換回解析度
const TOP = 18          // 100 分的 y
const BOT = 198         // 0 分的 y
const H = BOT + 26      // 底下留給期間標籤

const R = computed(() => Math.max(L + 60, width.value - GUT))
const y = (v: number) => BOT - (Math.max(0, Math.min(100, v)) / 100) * (BOT - TOP)
const n = computed(() => props.values.length)
const x = (i: number) => (n.value <= 1 ? (L + R.value) / 2 : L + (i * (R.value - L)) / (n.value - 1))
const gap = computed(() => (n.value > 1 ? (R.value - L) / (n.value - 1) : R.value - L))

const bands = computed(() => {
  const g = props.grades
  if (!g.length) return []
  const out: { y: number; h: number; zh: string; mid: number; i: number }[] = []
  let lo = 0
  g.forEach((b, i) => {
    const hi = b.lt ?? 100
    out.push({ y: y(hi), h: y(lo) - y(hi), zh: b.zh, mid: (y(hi) + y(lo)) / 2, i })
    lo = hi
  })
  return out
})

const dots = computed(() => props.values.map((v, i) => (
  v == null ? null : { x: x(i), y: y(v), v, i }
)))

/** null 斷線：連續有值的段落各自畫成一條 polyline */
const segments = computed(() => {
  const out: string[] = []
  let cur: string[] = []
  for (const d of dots.value) {
    if (d) cur.push(`${d.x.toFixed(1)},${d.y.toFixed(1)}`)
    else { if (cur.length > 1) out.push(cur.join(' ')); cur = [] }
  }
  if (cur.length > 1) out.push(cur.join(' '))
  return out
})

/** 標籤密度：間距不夠就隔幾個標一次，但**最新一期一定標**（那是讀者在找的數字） */
const vStep = computed(() => Math.max(1, Math.ceil(22 / gap.value)))
const xStep = computed(() => Math.max(1, Math.ceil(38 / gap.value)))
const shows = (i: number, s: number) => (n.value - 1 - i) % s === 0

const gaps = computed(() => props.values.filter((v) => v == null).length)
const lastD = computed(() => [...dots.value].reverse().find(Boolean) ?? null)
const firstD = computed(() => dots.value.find(Boolean) ?? null)
const span = computed(() => {
  const a = firstD.value; const b = lastD.value
  if (!a || !b || a.i === b.i) return null
  return { from: a.v, to: b.v, d: b.v - a.v, fl: props.labels[a.i] ?? '', tl: props.labels[b.i] ?? '' }
})
</script>

<template>
  <figure ref="wrap" class="strend">
    <svg :viewBox="`0 0 ${width} ${H}`" :width="width" :height="H" role="img"
         :aria-label="`總分走勢，最新 ${lastD?.v ?? '無'} 分`">
      <!-- 評級級距：走勢的意義是「跨過了哪一條線」 -->
      <g v-for="b in bands" :key="b.zh">
        <rect :x="L" :y="b.y" :width="R - L" :height="b.h" class="band" :class="`b${b.i}`" />
        <line :x1="L" :x2="R" :y1="b.y" :y2="b.y" class="bline" />
        <text :x="R + 6" :y="b.mid + 3.5" class="blabel">{{ b.zh }}</text>
      </g>
      <text :x="R + 6" :y="BOT + 3.5" class="blabel dim">0</text>
      <text :x="R + 6" :y="TOP + 3.5" class="blabel dim">100</text>

      <polyline v-for="(s, i) in segments" :key="i" :points="s" class="ln" />

      <template v-for="d in dots" :key="d?.i ?? -1">
        <template v-if="d">
          <circle :cx="d.x" :cy="d.y" r="2.6" class="dot" :class="{ end: d.i === lastD?.i }" />
          <!-- 最新一期的數字靠左掛：它落在繪圖區右緣，置中會壓到右邊的級距名稱 -->
          <text v-if="shows(d.i, vStep) || d.i === lastD?.i"
                :x="d.i === lastD?.i ? d.x - 6 : d.x" :y="d.y - 8" class="val"
                :class="{ end: d.i === lastD?.i }">{{ d.v }}</text>
        </template>
      </template>

      <template v-for="(lb, i) in labels" :key="lb + i">
        <text v-if="shows(i, xStep)" :x="x(i)" :y="BOT + 16" class="xl">{{ lb }}</text>
      </template>
    </svg>
    <figcaption>
      <template v-if="span">
        {{ span.fl }} 的 <b>{{ span.from }}</b> 分到 {{ span.tl }} 的 <b>{{ span.to }}</b> 分，
        <b :class="span.d > 0 ? 'up' : span.d < 0 ? 'dn' : ''">{{ span.d > 0 ? '+' : '' }}{{ span.d }} 分</b>。
      </template>
      每一期都用<b>當期</b>的資料重算一次全部計分項，不是把今天的分數往回套。
      <template v-if="gaps">
        其中 {{ gaps }} 期覆蓋率不足、算不出總分，線在那裡<b>斷開</b>——不內插，
        連起來等於替那幾期編一個分數。
      </template>
      背景是評級級距（全市場總分的分位數），<b>只在同一套模型之內可比</b>。
    </figcaption>
  </figure>
</template>

<style scoped>
.strend { margin: 0; width: 100%; }
svg { display: block; max-width: 100%; }
.band { fill: var(--ink); }
.band.b0 { opacity: .085; }
.band.b1 { opacity: .055; }
.band.b2 { opacity: .028; }
.band.b3 { opacity: .055; }
.band.b4 { opacity: .085; }
.bline { stroke: var(--rule); stroke-width: 1; stroke-dasharray: 2 3; }
.blabel { font-family: var(--mono); font-size: 10px; fill: var(--ink-3); }
.blabel.dim { opacity: .55; font-size: 9px; }
.ln { fill: none; stroke: var(--ink-2); stroke-width: 1.6; }
.dot { fill: var(--surface); stroke: var(--ink-2); stroke-width: 1.4; }
.dot.end { fill: var(--ink); stroke: var(--surface); stroke-width: 1.6; }
.val { font-family: var(--mono); font-size: 10px; fill: var(--ink-3); text-anchor: middle; }
.val.end { fill: var(--ink); font-size: 12px; font-weight: 600; text-anchor: end; }
.xl { font-family: var(--mono); font-size: 9.5px; fill: var(--ink-3); text-anchor: middle; }
figcaption { font-size: 11px; color: var(--ink-3); line-height: 1.8; margin-top: 6px; }
figcaption b { color: var(--ink-2); }
figcaption b.up { color: var(--pos); }
figcaption b.dn { color: var(--neg); }
</style>
