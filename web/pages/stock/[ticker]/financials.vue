<script setup lang="ts">
/**
 * 財務報表分頁：大方向的四張圖 + 利潤瀑布。
 *
 * 資料全部來自 `/api/financials`（companyfacts），**不另外打 SEC**。
 * 缺資料的圖一律整張不畫，並在頁尾列出「沒畫的圖與原因」——
 * 畫一張半空的圖比不畫更糟，讀者會以為那是真的下滑。
 */
const route = useRoute()
const ticker = String(route.params.ticker || '').toUpperCase()

const nQuarters = ref(12)
const thisYear = new Date().getFullYear()

// 一次抓滿 7 年，季數選擇只是前端切片 —— 改期間不必重打 API
const { data, pending, error } = await useAsyncData(
  `fin-${ticker}`,
  () => $fetch<any>(`/api/financials?ticker=${ticker}&from=${thisYear - 6}Q1&to=${thisYear + 1}Q4&valuation=0`),
  { server: false },
)

/** 外國發行人（TSM、ASML）只有 20-F，期別是年度 —— 「季增率」在那裡是年增率，
 *  照季度的文案寫會直接誤導，所以整頁的期間用語隨這個旗標切換 */
const isAnnual = computed(() => data.value?.periodicity === 'annual')
const unitZh = computed(() => (isAnnual.value ? '年' : '季'))

const li = computed<Map<string, any>>(
  () => new Map((data.value?.lineItems ?? []).map((x: any) => [x.id, x])),
)
const allPeriods = computed<string[]>(() => data.value?.periods ?? [])
const periods = computed(() => allPeriods.value.slice(-nQuarters.value))
const labels = computed(() => periods.value.map(shortLabel))
const marks = computed(() =>
  periods.value.map((p) => (li.value.get('revenue')?.values?.[p]?.isEstimated ? '推算' : null)))

function shortLabel(p: string) {
  const m = p.match(/^FY(\d{4})(?: Q([1-4]))?$/)
  if (!m) return p
  return m[2] ? `${m[1].slice(2)}Q${m[2]}` : `FY${m[1].slice(2)}`
}
/** 逐期取值；`ids` 依序回退（總權益在多數公司是 equity，少數是 equity_total） */
function series(...ids: string[]): (number | null)[] {
  return periods.value.map((p) => {
    for (const id of ids) {
      const v = li.value.get(id)?.values?.[p]?.value
      if (typeof v === 'number') return v
    }
    return null
  })
}
function at(p: string, ...ids: string[]): number | null {
  for (const id of ids) {
    const v = li.value.get(id)?.values?.[p]?.value
    if (typeof v === 'number') return v
  }
  return null
}
const count = (xs: (number | null)[]) => xs.filter((v) => v != null).length
const ratio = (a: (number | null)[], b: (number | null)[]) =>
  a.map((v, i) => {
    const d = b[i]
    return v != null && d != null && d !== 0 ? v / d : null
  })

// ── 圖 1：損益表大方向 ────────────────────────────────────
const revenue = computed(() => series('revenue'))
const netIncome = computed(() => series('net_income'))
const grossProfit = computed(() => series('gross_profit'))
const operatingIncome = computed(() => series('operating_income'))

/** 長條走損益表的層層遞減：營收 → 毛利 → 營業利益 → 淨利。
 *  一眼就看得出每一層被吃掉多少，比放「營業費用」直觀
 *  （費用是被扣掉的那一塊，跟營收並排反而要心算）。
 *  沒申報的那一層自動消失 —— 銀行只會剩營收與淨利兩根。 */
const isBars = computed(() => {
  const defs: [string, (number | null)[], string][] = [
    ['營業收入', revenue.value, CHART_COLORS[0]],
    ['毛利', grossProfit.value, CHART_COLORS[3]],
    ['營業利益', operatingIncome.value, CHART_COLORS[2]],
    ['本期淨利', netIncome.value, CHART_COLORS[1]],
  ]
  return defs.filter(([, v]) => count(v)).map(([name, values, color]) => ({ name, color, values }))
})
const isLines = computed(() => {
  const out: any[] = []
  const gm = ratio(grossProfit.value, revenue.value)
  const om = ratio(operatingIncome.value, revenue.value)
  const nm = ratio(netIncome.value, revenue.value)
  if (count(gm)) out.push({ name: '毛利率', color: CHART_COLORS[2], values: gm })
  if (count(om)) out.push({ name: '營業利益率', color: CHART_COLORS[3], values: om })
  if (count(nm)) out.push({ name: '淨利率', color: CHART_COLORS[5], values: nm })
  return out
})
const isScale = computed(() => pickScale(isBars.value.flatMap((b) => b.values)))
const hasIS = computed(() => count(revenue.value) >= 2 && count(netIncome.value) >= 2)

// ── 圖 2：營收與季增率 ───────────────────────────────────
const qoq = computed(() =>
  periods.value.map((p, i) => {
    if (i === 0) return null
    const a = revenue.value[i], b = revenue.value[i - 1]
    return a != null && b != null && b !== 0 ? a / b - 1 : null
  }))
const yoy = computed(() =>
  periods.value.map((_, i) => {
    if (i < 4) return null
    const a = revenue.value[i], b = revenue.value[i - 4]
    return a != null && b != null && b !== 0 ? a / b - 1 : null
  }))
const hasQoQ = computed(() => count(revenue.value) >= 2)
const growthLines = computed(() => {
  if (isAnnual.value) {
    return [{ name: '營收年增率 YoY', color: CHART_COLORS[2], values: qoq.value }]
  }
  return [
    { name: '營收季增率 QoQ', color: CHART_COLORS[2], values: qoq.value },
    { name: '營收年增率 YoY', color: CHART_COLORS[3], values: yoy.value, dash: true },
  ]
})

// ── 圖 3：EPS ───────────────────────────────────────────
const epsD = computed(() => series('eps_diluted'))
const epsB = computed(() => series('eps_basic'))
const hasEps = computed(() => count(epsD.value) >= 2 || count(epsB.value) >= 2)
const epsLines = computed(() => {
  const out: any[] = []
  if (count(epsD.value)) out.push({ name: '稀釋每股盈餘', color: CHART_COLORS[0], values: epsD.value, axis: 'left' })
  if (count(epsB.value)) out.push({ name: '基本每股盈餘', color: CHART_COLORS[2], values: epsB.value, axis: 'left', dash: true })
  return out
})

// ── 圖 4：資產負債表 ─────────────────────────────────────
const bsSeries = computed(() => {
  const defs: [string, string[], string][] = [
    ['流動資產合計', ['current_assets'], CHART_COLORS[3]],
    ['資產總計', ['total_assets'], CHART_COLORS[0]],
    ['流動負債合計', ['current_liabilities'], CHART_COLORS[2]],
    ['負債總計', ['total_liabilities'], CHART_COLORS[4]],
    ['權益總計', ['equity', 'equity_total'], CHART_COLORS[1]],
  ]
  return defs
    .map(([name, ids, color]) => ({ name, color, values: series(...ids) }))
    .filter((s) => count(s.values) > 0)
})
const bsScale = computed(() => pickScale(bsSeries.value.flatMap((s) => s.values)))
const hasBS = computed(() => bsSeries.value.length >= 2)

// ── 圖 5：利潤瀑布 ───────────────────────────────────────
const wfPeriod = ref<string>('')
watchEffect(() => {
  if (!wfPeriod.value && periods.value.length) wfPeriod.value = periods.value.at(-1)!
})
const wfSteps = computed<any[]>(() => {
  const p = wfPeriod.value
  if (!p) return []
  const rev = at(p, 'revenue')
  const ni = at(p, 'net_income')
  if (rev == null || ni == null) return []
  const steps: any[] = [{ label: '營業收入', value: rev, kind: 'base' }]
  let running = rev

  const gp = at(p, 'gross_profit')
  const cogs = at(p, 'cogs')
  if (gp != null && cogs != null) {
    steps.push({ label: '營業成本', value: -cogs, kind: 'delta' })
    steps.push({ label: '毛利', value: gp, kind: 'subtotal' })
    running = gp
  } else if (gp != null) {
    steps.push({ label: '營業成本', value: gp - rev, kind: 'delta', plug: true })
    steps.push({ label: '毛利', value: gp, kind: 'subtotal' })
    running = gp
  }

  const oi = at(p, 'operating_income')
  if (oi != null) {
    const rnd = at(p, 'rnd')
    const sgna = at(p, 'sgna')
    const named = (rnd ?? 0) + (sgna ?? 0)
    if (rnd != null) steps.push({ label: '研發費用', value: -rnd, kind: 'delta' })
    if (sgna != null) steps.push({ label: '銷管費用', value: -sgna, kind: 'delta' })
    const rest = running - named - oi
    if (Math.abs(rest) > Math.abs(running) * 0.005) {
      steps.push({ label: '其他營業費用', value: -rest, kind: 'delta', plug: true })
    }
    steps.push({ label: '營業利益', value: oi, kind: 'subtotal' })
    running = oi
  }

  const pre = at(p, 'pretax_income')
  const tax = at(p, 'income_tax')
  if (pre != null) {
    const nonop = pre - running
    if (Math.abs(nonop) > Math.abs(rev) * 0.001) {
      steps.push({ label: '業外損益', value: nonop, kind: 'delta', plug: true })
    }
    steps.push({ label: '稅前淨利', value: pre, kind: 'subtotal' })
    running = pre
  }
  if (tax != null) steps.push({ label: '所得稅費用', value: -tax, kind: 'delta' })
  const gapToNi = ni - running - (tax != null ? -tax : 0)
  if (Math.abs(gapToNi) > Math.abs(rev) * 0.001) {
    steps.push({ label: '其他（含非控制權益）', value: gapToNi, kind: 'delta', plug: true })
  }
  steps.push({ label: '本期淨利', value: ni, kind: 'subtotal' })
  return steps
})
const wfScale = computed(() => pickScale(wfSteps.value.map((s) => s.value)))
const wfEstimated = computed(() =>
  !!wfPeriod.value && !!li.value.get('revenue')?.values?.[wfPeriod.value]?.isEstimated)

// ── 沒畫出來的圖 ────────────────────────────────────────
const skipped = computed(() => {
  const out: { name: string; why: string }[] = []
  if (!hasIS.value) out.push({ name: '損益表大方向', why: '營業收入或本期淨利在所選期間不足兩期' })
  if (!hasQoQ.value) out.push({ name: '營收與季增率', why: '營業收入不足兩期，算不出季增率' })
  if (!hasEps.value) out.push({ name: '每股盈餘', why: 'SEC 申報中沒有可用的每股盈餘標籤' })
  if (!hasBS.value) out.push({ name: '資產負債表', why: '資產負債表五個科目抓到的不足兩項' })
  if (!wfSteps.value.length) out.push({ name: '利潤瀑布', why: '所選期間缺營業收入或本期淨利' })
  const missing = ['gross_profit', 'operating_income']
    .filter((id) => !count(series(id)))
  if (missing.length && hasIS.value) {
    const zh = missing.map((id) => li.value.get(id)?.zh ?? id).join('、')
    out.push({
      name: '部分長條與折線',
      why: `${zh} 未申報（銀行、保險、控股公司常見）—— 該層的長條與對應的利潤率整條不畫`,
    })
  }
  return out
})

// 科目來源（信任層：讓讀者知道每條線是哪個 XBRL 標籤來的）
const srcOpen = ref(false)
const sources = computed(() => {
  const ids = ['revenue', 'cogs', 'gross_profit', 'opex_total', 'rnd', 'sgna', 'operating_income',
    'pretax_income', 'income_tax', 'net_income', 'eps_diluted', 'eps_basic',
    'current_assets', 'total_assets', 'current_liabilities', 'total_liabilities', 'equity']
  return ids.map((id) => {
    const x = li.value.get(id)
    const tag = periods.value.map((p) => x?.values?.[p]?.sourceTag).find(Boolean)
    return x && tag ? { id, zh: x.zh, tag } : null
  }).filter(Boolean) as { id: string; zh: string; tag: string }[]
})

const zhNames: Record<string, string> = {
  NVDA: '輝達', AAPL: '蘋果', TSLA: '特斯拉', MSFT: '微軟', AMZN: '亞馬遜',
  GOOGL: 'Alphabet', GOOG: 'Alphabet', META: 'Meta', AMD: '超微', TSM: '台積電',
}
useHead({ title: `${ticker} 財務報表圖表｜營收、毛利率、EPS、資產負債與利潤瀑布` })
</script>

<template>
  <div>
    <TickerTabs
      :ticker="ticker" :company="data?.company" :zh="zhNames[ticker]"
      :meta="data ? [`CIK ${data.cik}`, `對照表 v${data.mapVersion}`, data.currency] : []"
    />

    <main class="wrap fin">
      <div class="toolbar">
        <div class="seg">
          <span class="lb">期間</span>
          <button v-for="n in [8, 12, 16, 20]" :key="n" :class="{ on: nQuarters === n }"
                  @click="nQuarters = n">{{ n }} {{ unitZh }}</button>
        </div>
        <a class="xls" :href="`/api/financials/excel?ticker=${ticker}&from=${thisYear - 4}Q1&to=${thisYear + 1}Q4`">
          下載完整 Excel ↗
        </a>
      </div>

      <p v-if="pending" class="state">讀取 companyfacts<span class="dots" /></p>
      <p v-else-if="error" class="state err">
        讀不到 {{ ticker }} 的財務資料：{{ (error as any)?.data?.message || '請確認代號' }}
      </p>

      <template v-else-if="data">
        <!-- 圖 1 -->
        <section v-if="hasIS" class="cardblock">
          <div class="blockhead">
            <span class="num">§1</span><h2>損益表大方向</h2>
            <span class="hint">長條由左到右是損益表的四層（營收 → 毛利 → 營業利益 → 淨利，左軸金額）；
              折線為對應的利潤率（右軸）</span>
          </div>
          <ComboChart :labels="labels" :bars="isBars" :lines="isLines" :marks="marks"
                      :div="isScale.div" :unit="`${isScale.unit}美元`" :height="330" />
        </section>

        <!-- 圖 2 -->
        <section v-if="hasQoQ" class="cardblock">
          <div class="blockhead">
            <span class="num">§2</span><h2>營收與{{ unitZh }}增率</h2>
            <span class="hint">{{ isAnnual
              ? '年增率＝本年 ÷ 上年 − 1（外國發行人只申報年度數字，沒有季度）'
              : '季增率＝本季 ÷ 上季 − 1；年增率同季比較，看得出季節性' }}</span>
          </div>
          <ComboChart
            :labels="labels" :marks="marks"
            :bars="[{ name: '營業收入', color: CHART_COLORS[0], values: revenue }]"
            :lines="growthLines"
            :div="pickScale(revenue).div" :unit="`${pickScale(revenue).unit}美元`" :height="300"
          />
        </section>

        <!-- 圖 3 -->
        <section v-if="hasEps" class="cardblock">
          <div class="blockhead">
            <span class="num">§3</span><h2>每股盈餘</h2>
            <span class="hint">{{ isAnnual ? '年度值' : '單季值，非 TTM；Q4 為推算' }}</span>
          </div>
          <ComboChart :labels="labels" :lines="epsLines" :marks="marks" :div="1" unit="美元" :height="250" />
        </section>

        <!-- 圖 4 -->
        <section v-if="hasBS" class="cardblock">
          <div class="blockhead">
            <span class="num">§4</span><h2>資產負債表</h2>
            <span class="hint">期末餘額（時點值，不受期間長短影響）</span>
          </div>
          <ComboChart :labels="labels" :bars="bsSeries" :div="bsScale.div"
                      :unit="`${bsScale.unit}美元`" :height="330" />
        </section>

        <!-- 圖 5 -->
        <section v-if="wfSteps.length" class="cardblock">
          <div class="blockhead">
            <span class="num">§5</span><h2>利潤瀑布</h2>
            <select v-model="wfPeriod" class="psel">
              <option v-for="p in [...periods].reverse()" :key="p" :value="p">{{ p }}</option>
            </select>
            <span v-if="wfEstimated" class="estflag">此季為推算值（年報減前三季）</span>
          </div>
          <WaterfallChart :steps="wfSteps" :div="wfScale.div" :unit="`${wfScale.unit}美元`" :height="330" />
          <p class="wfnote">
            小計（毛利／營業利益／稅前淨利／淨利）取公司申報值。虛線框的是殘差反推的差額
            —— 例如公司只報營業費用合計、沒拆研發與銷管時，差額就掛在「其他營業費用」，
            不會把小計改成加總值把差額藏起來。
          </p>
        </section>

        <!-- 沒畫的圖 -->
        <section v-if="skipped.length" class="cardblock skip">
          <div class="blockhead"><span class="num">§—</span><h2>沒有呈現的圖</h2>
            <span class="hint">缺資料時整張不畫，不畫半張</span></div>
          <ul>
            <li v-for="s in skipped" :key="s.name"><b>{{ s.name }}</b><span>{{ s.why }}</span></li>
          </ul>
        </section>

        <section class="cardblock src">
          <button class="srctoggle" @click="srcOpen = !srcOpen">
            {{ srcOpen ? '收合' : '展開' }}本頁每個科目的 XBRL 來源標籤（{{ sources.length }} 項）
          </button>
          <table v-if="srcOpen" class="srctab">
            <tbody>
              <tr v-for="s in sources" :key="s.id">
                <td>{{ s.zh }}</td><td class="mono id">{{ s.id }}</td><td class="mono">{{ s.tag }}</td>
              </tr>
            </tbody>
          </table>
        </section>
      </template>

      <p class="disclaim">
        數字全部來自 SEC EDGAR 的 XBRL companyfacts，本頁不另外下載或解析財報 HTML。
        Q4 單季為推算值（年報 − 前三季），圖上以橘色「推算」標記。
        缺值一律不畫，不以 0 代替（SEC 無該標籤 ≠ 數值為零）。
      </p>
    </main>
  </div>
</template>

<style scoped>
.fin { padding-top: 22px; }
.toolbar { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 20px; }
.seg { display: flex; align-items: center; gap: 8px; }
.seg .lb { font-family: var(--mono); font-size: 10px; letter-spacing: .16em;
  text-transform: uppercase; color: var(--ink-3); }
.seg button { font-family: var(--mono); font-size: 12px; padding: 4px 10px; cursor: pointer;
  background: var(--surface); border: 1px solid var(--rule); color: var(--ink-2); margin-left: -1px; }
.seg button.on { background: var(--ink); color: var(--paper); border-color: var(--ink); }
.xls { margin-left: auto; font-size: 12.5px; color: var(--green); text-decoration: none;
  border-bottom: 1px solid var(--green); }
.state { font-family: var(--mono); font-size: 13px; color: var(--ink-2); padding: 40px 0; }
.state.err { color: var(--sig); }
.dots::after { content: '…'; animation: d 1.2s steps(4) infinite; }
@keyframes d { 0% { content: '' } 25% { content: '.' } 50% { content: '..' } 75% { content: '...' } }
.cardblock { background: var(--surface); border: 1px solid var(--rule); padding: 18px 20px 20px;
  margin-bottom: 20px; }
.blockhead { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
.blockhead h2 { font-size: 14px; font-weight: 600; }
.blockhead .num { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); }
.blockhead .hint { font-size: 12.5px; color: var(--ink-3); }
.psel { font-family: var(--mono); font-size: 12px; padding: 3px 6px; border: 1px solid var(--rule);
  background: var(--paper); color: var(--ink); }
.estflag { font-size: 11.5px; color: var(--sig); }
.wfnote { margin-top: 12px; font-size: 12px; color: var(--ink-2); border-left: 2px solid var(--rule);
  padding-left: 10px; }
.skip ul { list-style: none; display: grid; gap: 6px; }
.skip li { display: flex; gap: 12px; font-size: 12.5px; flex-wrap: wrap; }
.skip li b { font-weight: 600; min-width: 8em; }
.skip li span { color: var(--ink-2); }
.srctoggle { background: none; border: 0; padding: 0; font-size: 12.5px; color: var(--green);
  cursor: pointer; border-bottom: 1px solid var(--green); font-family: inherit; }
.srctab { width: 100%; border-collapse: collapse; margin-top: 12px; font-size: 12px; }
.srctab td { border-top: 1px solid var(--rule-2); padding: 4px 8px 4px 0; }
.srctab td.mono { font-family: var(--mono); font-size: 11px; color: var(--ink-2); }
.srctab td.id { color: var(--ink-3); width: 12em; }
.disclaim { font-size: 11.5px; color: var(--ink-3); line-height: 1.7; margin-top: 6px; }
</style>
