/**
 * 圖表與摘要頁共用的數字格式化。
 *
 * 單位一律「按整張圖的最大值決定一次」，不逐點決定 —— 同一張圖上
 * 一根是「12 億」另一根是「340 百萬」的話，讀者要心算才能比高低。
 */
export type Scale = { div: number; unit: string }

export function pickScale(values: (number | null | undefined)[]): Scale {
  const max = Math.max(0, ...values.filter((v): v is number => v != null).map((v) => Math.abs(v)))
  if (max >= 1e11) return { div: 1e8, unit: '億' }      // ≥ 1000 億
  if (max >= 1e8) return { div: 1e8, unit: '億' }
  if (max >= 1e5) return { div: 1e6, unit: '百萬' }
  return { div: 1, unit: '' }
}

const NF = (d: number) => new Intl.NumberFormat('en-US', {
  minimumFractionDigits: d, maximumFractionDigits: d,
})

export function fmtScaled(v: number | null | undefined, s: Scale): string {
  if (v == null || !Number.isFinite(v)) return 'n/a'
  const x = v / s.div
  const d = Math.abs(x) >= 100 ? 0 : Math.abs(x) >= 10 ? 1 : 2
  return NF(d).format(x)
}

/** 座標軸刻度用：不帶小數尾巴 */
export function fmtTick(v: number, s: Scale): string {
  const x = v / s.div
  if (x === 0) return '0'
  const d = Math.abs(x) >= 100 ? 0 : Math.abs(x) >= 10 ? 0 : 1
  return NF(d).format(x)
}

export function fmtPct(v: number | null | undefined, digits = 1): string {
  if (v == null || !Number.isFinite(v)) return 'n/a'
  return `${(v * 100).toFixed(digits)}%`
}

export function fmtUsd(v: number | null | undefined, digits = 2): string {
  if (v == null || !Number.isFinite(v)) return 'n/a'
  return `$${NF(digits).format(v)}`
}

/** 「好看的」刻度間距：1 / 2 / 2.5 / 5 × 10^n */
export function niceStep(rough: number): number {
  if (!(rough > 0)) return 1
  const p = Math.pow(10, Math.floor(Math.log10(rough)))
  const r = rough / p
  const m = r <= 1 ? 1 : r <= 2 ? 2 : r <= 2.5 ? 2.5 : r <= 5 ? 5 : 10
  return m * p
}

/** 給定資料範圍算出含 0 的軸（長條圖必須含 0，否則高度比例會說謊） */
export function axisFor(values: (number | null | undefined)[], ticks = 4, includeZero = true) {
  const xs = values.filter((v): v is number => v != null && Number.isFinite(v))
  if (!xs.length) return { min: 0, max: 1, step: 1, list: [0, 1] }
  let lo = Math.min(...xs)
  let hi = Math.max(...xs)
  if (includeZero) { lo = Math.min(lo, 0); hi = Math.max(hi, 0) }
  if (lo === hi) { hi = lo + Math.abs(lo || 1) * 0.5; lo = Math.min(lo, 0) }
  const step = niceStep((hi - lo) / ticks)
  const min = Math.floor(lo / step) * step
  const max = Math.ceil(hi / step) * step
  const list: number[] = []
  for (let v = min; v <= max + step * 1e-9; v += step) list.push(Math.abs(v) < step * 1e-9 ? 0 : v)
  return { min, max, step, list }
}

/**
 * 圖表數列色（深淺主題各一組，值在 main.css 的 --series-1…6）。
 * 索引對到色盤槽位的方式是刻意的：各圖實際並排的組合要落在驗證過的順序上
 * （validate_palette.js）——損益四層長條 [0,3,2,1] ＝ 槽 1–4 相鄰；資產負債 [0,3,2,1,5] ＝ 槽 1–5；
 * 會交叉的折線（利潤率、成長率、EPS）只用 0、2、3 ＝ 槽 1–3，任兩條都分得開。
 * 新增數列前先跑一次驗證，不要用肉眼挑。
 */
export const CHART_COLORS = [
  'var(--series-1)',
  'var(--series-4)',
  'var(--series-3)',
  'var(--series-2)',
  'var(--series-6)',
  'var(--series-5)',
]
