<script setup lang="ts">
/**
 * 轉折點分頁：把既有的 companyfacts 數字轉成「在變好還是變壞」。
 *
 * 資料全部來自 `/api/signals`，而那支走的是 `getFinancials` 同一條路
 * ——**零額外 SEC 請求**，與財務報表分頁共用同一份 companyfacts 與快取。
 *
 * 這一頁刻意不做的三件事：
 *   1. 不做綜合評分。把十幾個訊號壓成一個分數要靠權重，那是我們憑空造的數字。
 *   2. 不預測、不建議買賣。每個訊號只回答一個當場可以核對的問題。
 *   3. 不把三種留白混成一種：n/a（該申報卻抓不到）／—（不適用、或比較基期
 *      落在所選期間之外）各自寫清楚是哪一種。
 */
const route = useRoute()
const ticker = String(route.params.ticker || '').toUpperCase()

const nPeriods = ref(12)

const { data, pending, error } = await useAsyncData(
  `sig-${ticker}`,
  () => $fetch<any>(`/api/signals?ticker=${ticker}&years=6`),
  { server: false },
)

const isAnnual = computed(() => data.value?.periodicity === 'annual')
const unitZh = computed(() => (isAnnual.value ? '年' : '季'))
/** 外國發行人一欄＝一整年，「年變化」在那裡是「與前一年度相比」，文案要跟著換 */
const yoyZh = computed(() => (isAnnual.value ? '與前一年度相比' : '與去年同期相比'))

const allPeriods = computed<string[]>(() => data.value?.periods ?? [])
const shown = computed(() => Math.min(nPeriods.value, allPeriods.value.length))
const periods = computed(() => allPeriods.value.slice(-shown.value))
const labels = computed(() => periods.value.map(shortLabel))

function shortLabel(p: string) {
  const m = p.match(/^FY(\d{4})(?: Q([1-4]))?$/)
  if (!m) return p
  return m[2] ? `${m[1].slice(2)}Q${m[2]}` : `FY${m[1].slice(2)}`
}

interface Cell { value: number | null; reason: string; isEstimated: boolean; state: string; stateZh?: string; invalid?: boolean }
interface Sig {
  id: string; metric: string; layer: string; unit: string
  question: string | null; read: string; pairsWith: string[]
  zh: string; en: string; formula: string; desc: string
  inapplicable: boolean; guards: string[]
  series: Cell[]; latest: (Cell & { period: string }) | null
}

const signals = computed<Sig[]>(() => data.value?.signals ?? [])

/**
 * 每張卡片算一次就好：切片（期間鈕只切顯示，不重打 API）、最新一格、狀態。
 * 樣板裡到處寫 `sliceOf(s).at(-1)` 的話，同一次 render 會重切十幾遍，
 * 而且「最新一格是哪一格」這個判斷會散在四個地方，改一個忘一個就自相矛盾。
 */
interface Card extends Sig { cells: Cell[]; latestCell: Cell | null; state: string }
const cards = computed<Card[]>(() => signals.value.map((s) => {
  const cells = s.series.slice(-shown.value)
  const latestCell = cells.at(-1) ?? null
  return {
    ...s,
    cells,
    latestCell,
    state: latestCell && latestCell.reason === 'ok' ? latestCell.state : 'na',
  }
}))
const byId = computed(() => new Map(cards.value.map((c) => [c.id, c])))

// ── 數值呈現 ────────────────────────────────────────────
/** 百分點（pp）與百分比（pct）都是小數存的，差別只在說法：
 *  pp 是「變化了幾個百分點」，pct 是「本身是幾 %」。混寫會讓 +2pp 被讀成 +2% */
function fmtVal(s: Sig, c: Cell | null | undefined): string {
  if (!c) return 'n/a'
  if (c.reason !== 'ok' || c.value == null) return c.reason === 'ok' ? 'n/a' : reasonMark(c.reason)
  const v = c.value
  if (s.unit === 'pp') return `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}pp`
  if (s.unit === 'pct') return `${(v * 100).toFixed(1)}%`
  if (s.unit === 'days') {
    // 先四捨五入再決定正負號，否則 −0.4 天會寫成「−0 天」
    const r = Math.round(v)
    return `${r > 0 ? '+' : ''}${r} 天`
  }
  if (s.unit === 'x') return `${v.toFixed(2)}x`
  return String(v)
}
const reasonMark = (r: string) => (r === 'missing' ? 'n/a' : '—')

/** 三種留白各自的解釋。混成一種的話，讀者會把「這家公司沒有這一行」當成我們漏抓 */
function blankWhy(s: Sig, c: Cell | null | undefined): string | null {
  if (!c || c.reason === 'ok') return null
  if (c.invalid) {
    return `本期${s.guards.map((g) => guardZh[g] ?? g).join('、')}為負或零，這個比值會算出反過來的數字，整格作廢`
  }
  if (c.reason === 'inapplicable') {
    return s.inapplicable
      ? '這個科目對這家公司本來就不存在（例如軟體業沒有存貨、無有息負債的公司沒有利息費用），不是漏抓'
      : `${isAnnual.value ? '年度' : '季度'}資料算不出這個指標`
  }
  if (c.reason === 'window') return `比較基期（${yoyZh.value}）落在所選期間之外，往前拉長期間就會出現`
  return 'SEC 申報裡沒有對應的標籤，我們不以 0 代替'
}
const guardZh: Record<string, string> = { net_income: '淨利', ebitda: 'EBITDA' }

const stateZh: Record<string, string> = {
  good: '轉好', warn: '警訊', flat: '持平', info: '背景', na: '無資料',
}

// ── 摘要 ────────────────────────────────────────────────
const tally = computed(() => {
  const t = { good: 0, warn: 0, flat: 0, info: 0, na: 0 }
  for (const c of cards.value) {
    const k = c.state as keyof typeof t
    t[k] = (t[k] ?? 0) + 1
  }
  return t
})
const latestPeriod = computed(() => periods.value.at(-1) ?? '')

/** 印證關係：兩個互相對照的訊號同時亮警訊才算數（單一格常常只是期末時點造成的） */
function confirmed(c: Card): string | null {
  if (!c.pairsWith.length || c.state !== 'warn') return null
  const both = c.pairsWith
    .map((id) => byId.value.get(id))
    .filter((o): o is Card => !!o && o.state === 'warn')
  return both.length ? `與「${both.map((o) => o.zh).join('、')}」同時亮警訊 —— 互相印證` : null
}

// ── 量化評分 ────────────────────────────────────────────
const score = computed<any>(() => data.value?.score ?? null)
const dims = computed<any[]>(() => score.value?.dimensions ?? [])
/** 展開哪一個構面的逐項明細 */
const openDim = ref<string | null>(null)

/** 沒算到分的三種理由，各自寫清楚 —— 混成一種讀者會以為是我們漏抓 */
const itemWhy: Record<string, string> = {
  missing: 'SEC 申報裡沒有對應的標籤',
  inapplicable: '這家公司沒有這一行（產業結構）',
  window: '比較基期落在所選期間之外',
  invalid: '分母為負，比值會反過來，整項作廢',
  not_meaningful: '這個指標對這門生意沒有定義',
}

/** 得分條的長度用分數，顏色只分三段——顏色是輔助，數字才是主體 */
function scoreClass(v: number | null | undefined) {
  if (v == null) return 'na'
  return v >= 65 ? 'good' : v >= 40 ? 'mid' : 'weak'
}
const fmtScore = (v: number | null | undefined) => (v == null ? '—' : String(Math.round(v)))
/** 逐項的原始值：不知道單位就照指標的數量級決定小數位 */
function itemValue(it: any): string {
  if (it.value == null) return '—'
  const v = it.value as number
  if (Math.abs(v) >= 1000) return v.toLocaleString('en-US', { maximumFractionDigits: 0 })
  if (Math.abs(v) >= 10) return v.toFixed(1)
  if (Math.abs(v) >= 1) return v.toFixed(2)
  return `${(v * 100).toFixed(1)}%`
}
const arrowGlyph: Record<string, string> = {
  up2: '↑', up1: '↗', flat: '→', down1: '↘', down2: '↓', na: '·',
}

const layers = computed(() => (data.value?.layers ?? []).map((l: any) => ({
  ...l,
  items: cards.value.filter((c) => c.layer === l.id),
})))

/** 展開哪一張卡的公式與說明 */
const open = ref<string | null>(null)

const blanks = computed(() => cards.value
  .filter((c) => !c.latestCell || c.latestCell.reason !== 'ok')
  .map((c) => ({
    zh: c.zh,
    why: blankWhy(c, c.latestCell) ?? '無資料',
    mark: reasonMark(c.latestCell?.reason ?? 'missing'),
  })))

const zhNames: Record<string, string> = {
  NVDA: '輝達', AAPL: '蘋果', TSLA: '特斯拉', MSFT: '微軟', AMZN: '亞馬遜',
  GOOGL: 'Alphabet', GOOG: 'Alphabet', META: 'Meta', AMD: '超微', TSM: '台積電',
}
useHead({ title: `${ticker} 轉折點訊號｜營業槓桿、應收與存貨品質、現金含量` })
</script>

<template>
  <div>
    <TickerTabs
      :ticker="ticker" :company="data?.company" :zh="zhNames[ticker]"
      :meta="data ? [`CIK ${data.cik}`, `對照表 v${data.mapVersion}`, `訊號 v${data.signalsVersion}`] : []"
    />

    <main class="wrap sig">
      <div class="toolbar">
        <div class="seg">
          <span class="lb">期間</span>
          <button v-for="n in [8, 12, 16, 20]" :key="n" :class="{ on: nPeriods === n }"
                  @click="nPeriods = n">{{ n }} {{ unitZh }}</button>
        </div>
        <NuxtLink class="xls" :to="`/stock/${ticker}/financials`">看原始報表與圖表 ↗</NuxtLink>
      </div>

      <p v-if="pending" class="state">計算轉折訊號<span class="dots" /></p>
      <p v-else-if="error" class="state err">
        讀不到 {{ ticker }} 的財務資料：{{ (error as any)?.data?.message || '請確認代號' }}
      </p>

      <template v-else-if="data">
        <!-- 體質評分 -->
        <section v-if="score" class="cardblock scoreblock">
          <div class="blockhead">
            <span class="num">§1</span><h2>體質評分</h2>
            <span class="hint">{{ latestPeriod }}　評分模型 v{{ score.version }}　對照表 v{{ data.mapVersion }}</span>
          </div>

          <div class="scorehead">
            <div class="big">
              <b v-if="score.total != null" class="tot mono" :class="scoreClass(score.total)">{{ score.total }}</b>
              <b v-else class="tot mono na">—</b>
              <span class="grade">{{ score.grade?.zh ?? '無法評分' }}</span>
            </div>
            <div class="arrow" :class="`a-${score.arrow.id}`">
              <b>{{ arrowGlyph[score.arrow.id] }} {{ score.arrow.zh }}</b>
              <small v-if="score.arrow.delta != null">
                {{ score.arrow.delta > 0 ? '+' : '' }}{{ score.arrow.delta }} 分
                （vs {{ isAnnual ? '前一年度' : '去年同期' }} {{ score.arrow.from }} 分，
                取兩期都算得出來的 {{ score.arrow.comparableItems }} 項重算）
              </small>
              <small v-else>{{ score.arrow.zh }}</small>
            </div>
            <div class="cov">
              <b class="mono">{{ score.counted }}/{{ score.itemsTotal }} 項</b>
              <small>算得出來的計分項</small>
              <small class="covw">
                權重覆蓋 <b class="mono">{{ Math.round(score.coverage * 100) }}%</b>
                <i>（低於 {{ Math.round(score.coverageFloor * 100) }}% 不給總分）</i>
              </small>
            </div>
          </div>

          <p class="covnote">
            兩個數字算的不是同一件事：<b>{{ score.counted }}/{{ score.itemsTotal }}</b> 是幾個計分項算得出來，
            <b>{{ Math.round(score.coverage * 100) }}%</b> 是這些項目佔多少<b>權重</b>——
            每項權重不同，所以兩者不會相等。總分的門檻擋的是權重那個。
            算不出來的項目一律<b>移出分母</b>，不是給 0 分。
          </p>

          <p v-if="score.total == null" class="nototal">
            覆蓋率低於 {{ Math.round(score.coverageFloor * 100) }}%，<b>不給總分</b>。
            拿少數幾項湊出來的總分是雜訊不是結論——下面算得出來的構面分仍然是真的。
            銀行、保險、REITs 常常落在這裡：它們的財務結構與現金含量要用另一套指標
            （資本適足率、淨利差、提存覆蓋率）才有意義，那是另一個模型。
          </p>

          <div class="dims">
            <div v-for="d in dims" :key="d.id" class="dim">
              <button class="dimhead" @click="openDim = openDim === d.id ? null : d.id">
                <span class="dname">{{ d.zh }}</span>
                <span class="dw mono">權重 {{ Math.round(d.weight * 100) }}%</span>
                <span class="bar"><i :class="scoreClass(d.score)"
                                     :style="{ width: `${d.score ?? 0}%` }" /></span>
                <span class="dscore mono" :class="scoreClass(d.score)">{{ fmtScore(d.score) }}</span>
                <span class="dcov mono" title="此構面算得出來的項目佔權重的比例">
                  覆蓋 {{ Math.round(d.coverage * 100) }}%
                </span>
                <span class="caret">{{ openDim === d.id ? '−' : '+' }}</span>
              </button>
              <p v-if="d.score != null && !d.counted" class="dimout">
                此構面覆蓋率不足，<b>分數照給但不進總分</b>——只靠一兩項撐起
                {{ Math.round(d.weight * 100) }}% 權重的，不是這一層的分數。
              </p>
              <div v-if="openDim === d.id" class="dimbody">
                <p class="ddesc">{{ d.desc }}</p>
                <table class="items">
                  <thead>
                    <tr><th>指標</th><th class="r">值</th><th class="r">錨點</th>
                      <th class="r">得分</th><th class="r">權重</th></tr>
                  </thead>
                  <tbody>
                    <tr v-for="it in d.items" :key="it.metric" :class="{ off: it.score == null }">
                      <td>
                        {{ it.zh }}
                        <i v-if="it.isEstimated" class="est">推算</i>
                        <em v-if="it.score == null">{{ it.note || itemWhy[it.reason] || '無資料' }}</em>
                      </td>
                      <td class="r mono">{{ itemValue(it) }}</td>
                      <td class="r mono anch">{{ it.bad }} → {{ it.good }}</td>
                      <td class="r mono" :class="scoreClass(it.score)">{{ fmtScore(it.score) }}</td>
                      <td class="r mono w">{{ it.weight }}</td>
                    </tr>
                  </tbody>
                </table>
                <p class="dnote">
                  得分＝100 × clamp((值 − 左錨點) ÷ (右錨點 − 左錨點), 0, 1)，兩點之間線性、兩端飽和。
                  右錨點小於左錨點的是「越低越好」的指標（負債倍數、稀釋率），公式相同。
                  構面分＝各項得分的加權平均，缺項移出分母。
                </p>
              </div>
            </div>
          </div>

          <div class="scoretrend" v-if="(score.totalSeries || []).some((x: any) => x != null)">
            <span class="tlabel mono">總分走勢</span>
            <Sparkline :values="score.totalSeries.slice(-shown)" :state="scoreClass(score.total)" :height="34" />
            <span class="taxis mono">{{ labels[0] }} – {{ labels.at(-1) }}</span>
          </div>

          <p class="disclaimer">
            <b>評分是量化整理，不是投資建議。</b>權重與錨點都是我們設定的（攤在上面每一列，
            也在 <code>config/scoring.json</code> 裡），不同的假設會得到不同的分數。
            分數只用 SEC 申報數字計算，不含產業前景、競爭態勢、管理階層、法規與股價。
            現階段錨點是<b>跨產業絕對值</b>，重資產產業（半導體、電信、能源）會被系統性低估；
            下一版改用同業百分位。做決定前請自己看原始申報。
          </p>
        </section>

        <section class="cardblock lede">
          <div class="blockhead">
            <span class="num">§2</span><h2>轉折訊號（逐項判讀）</h2>
            <span class="hint">最新期別 {{ latestPeriod }}；每個訊號都是{{ yoyZh }}</span>
          </div>
          <div class="tally">
            <span class="chip s-warn">警訊 {{ tally.warn }}</span>
            <span class="chip s-good">轉好 {{ tally.good }}</span>
            <span class="chip s-flat">持平 {{ tally.flat }}</span>
            <span class="chip s-info">背景 {{ tally.info }}</span>
            <span class="chip s-na">無資料 {{ tally.na }}</span>
          </div>
          <p class="ledetext">
            上面的分數回答「現在體質如何」，下面這些卡片回答「為什麼」。
            財報的轉折幾乎不會只出現在一個地方 ——
            <b>要兩個以上互相印證的訊號同時轉向才算數</b>，單一格惡化常常只是期末出貨時點造成的。
            數字全部由 <NuxtLink :to="`/stock/${ticker}/financials`">財務報表</NuxtLink>
            的同一份 companyfacts 算出來，公式攤在每張卡片裡，可以自己核。
          </p>
        </section>

        <section v-for="l in layers" :key="l.id" class="cardblock">
          <div class="blockhead">
            <span class="num">{{ l.source }}</span><h2>{{ l.zh }}</h2>
            <span class="hint">{{ l.desc }}</span>
          </div>

          <div class="grid">
            <article v-for="s in l.items" :key="s.id" class="card" :class="`b-${s.state}`">
              <header>
                <h3>{{ s.zh }}</h3>
                <span class="badge" :class="`s-${s.state}`">{{ stateZh[s.state] }}</span>
              </header>
              <p v-if="s.question" class="q">{{ s.question }}</p>

              <div class="valrow">
                <b class="val mono">{{ fmtVal(s, s.latestCell) }}</b>
                <span v-if="s.latestCell?.isEstimated" class="estflag">推算</span>
              </div>

              <Sparkline
                :values="s.cells.map((c) => (c.reason === 'ok' ? c.value : null))"
                :estimated="s.cells.map((c) => c.isEstimated)"
                :state="s.state"
              />
              <div class="axis mono"><span>{{ labels[0] }}</span><span>{{ labels.at(-1) }}</span></div>

              <p v-if="s.latestCell?.stateZh" class="verdict">{{ s.latestCell.stateZh }}</p>
              <p v-else-if="blankWhy(s, s.latestCell)" class="blank">
                {{ reasonMark(s.latestCell?.reason ?? 'missing') }} —— {{ blankWhy(s, s.latestCell) }}
              </p>

              <p v-if="confirmed(s)" class="confirm">{{ confirmed(s) }}</p>

              <p class="read">{{ s.read }}</p>

              <button class="more" @click="open = open === s.id ? null : s.id">
                {{ open === s.id ? '收合' : '這格怎麼算的' }}
              </button>
              <div v-if="open === s.id" class="detail">
                <p class="mono formula">{{ s.formula }}</p>
                <p>{{ s.desc }}</p>
                <table class="hist">
                  <tbody>
                    <tr>
                      <th>期別</th>
                      <td v-for="p in labels" :key="p" class="mono">{{ p }}</td>
                    </tr>
                    <tr>
                      <th>數值</th>
                      <td v-for="(c, i) in s.cells" :key="i" class="mono"
                          :class="{ est: c.isEstimated }">{{ fmtVal(s, c) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </article>
          </div>
        </section>

        <section v-if="blanks.length" class="cardblock skip">
          <div class="blockhead"><span class="num">§—</span><h2>沒有算出來的訊號</h2>
            <span class="hint">兩種留白分開寫：n/a ＝該申報卻抓不到；— ＝不適用或基期不在區間內</span></div>
          <ul>
            <li v-for="b in blanks" :key="b.zh">
              <b>{{ b.zh }}</b><i class="mono">{{ b.mark }}</i><span>{{ b.why }}</span>
            </li>
          </ul>
        </section>
      </template>

      <p class="disclaim">
        數字全部來自 SEC EDGAR 的 XBRL companyfacts，與財務報表分頁同一份資料、同一層快取，
        本頁不另外下載或解析任何財報檔案。門檻（幾個百分點算警訊）是判讀輔助，不是買賣訊號 ——
        跨產業必然有例外，所以頁面一律把數值本身放在最前面、分級放在後面，並且不做綜合評分。
        Q4 單季為推算值（年報 − 前三季），以橘色標記。缺值不以 0 代替（SEC 無該標籤 ≠ 數值為零）。
      </p>
    </main>
  </div>
</template>

<style scoped>
.sig { padding-top: 22px; }
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
.blockhead .hint { font-size: 12.5px; color: var(--ink-3); flex: 1; min-width: 12em; }

.tally { display: flex; gap: 8px; flex-wrap: wrap; margin-bottom: 12px; }
.chip { font-family: var(--mono); font-size: 11.5px; padding: 3px 9px; border: 1px solid var(--rule); }
.chip.s-warn { border-color: var(--neg); color: var(--neg); background: var(--neg-wash); }
.chip.s-good { border-color: var(--pos); color: var(--pos); background: var(--pos-wash); }
.chip.s-flat, .chip.s-info, .chip.s-na { color: var(--ink-2); }
.ledetext { font-size: 13px; line-height: 1.85; color: var(--ink-2); }
.ledetext b { color: var(--ink); }
.ledetext a { color: var(--green); }

.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(260px, 1fr)); gap: 14px; }
.card { border: 1px solid var(--rule-2); border-left: 3px solid var(--rule); padding: 12px 14px 14px; }
.card.b-warn { border-left-color: var(--neg); }
.card.b-good { border-left-color: var(--pos); }
.card header { display: flex; align-items: baseline; gap: 8px; }
.card h3 { font-size: 13px; font-weight: 600; flex: 1; }
.badge { font-family: var(--mono); font-size: 10.5px; padding: 1px 6px; border: 1px solid var(--rule); color: var(--ink-2); }
.badge.s-warn { border-color: var(--neg); color: var(--neg); }
.badge.s-good { border-color: var(--pos); color: var(--pos); }
.q { font-size: 12px; color: var(--ink-3); margin-top: 4px; }
.valrow { display: flex; align-items: baseline; gap: 8px; margin: 8px 0 2px; }
.val { font-size: 21px; font-weight: 700; letter-spacing: -.01em; }
.estflag { font-size: 10.5px; color: var(--sig); }
.axis { display: flex; justify-content: space-between; font-size: 10px; color: var(--ink-3); margin-top: 2px; }
.verdict { font-size: 12.5px; color: var(--ink); margin-top: 8px; }
.blank { font-size: 12px; color: var(--ink-3); margin-top: 8px; line-height: 1.7; }
.confirm { font-size: 12px; color: var(--neg); margin-top: 6px; }
.read { font-size: 12px; color: var(--ink-2); line-height: 1.75; margin-top: 8px;
  border-left: 2px solid var(--rule-2); padding-left: 9px; }
.more { background: none; border: 0; padding: 0; margin-top: 9px; font-family: inherit;
  font-size: 11.5px; color: var(--green); cursor: pointer; border-bottom: 1px solid var(--green); }
.detail { margin-top: 10px; font-size: 12px; color: var(--ink-2); line-height: 1.7; }
.detail .formula { font-size: 11px; color: var(--ink); background: var(--paper); padding: 5px 7px;
  overflow-x: auto; white-space: nowrap; }
.hist { width: 100%; border-collapse: collapse; margin-top: 8px; display: block; overflow-x: auto; }
.hist th { text-align: left; font-size: 10.5px; color: var(--ink-3); font-weight: 500;
  padding: 2px 6px 2px 0; white-space: nowrap; }
.hist td { font-size: 10.5px; padding: 2px 6px 2px 0; white-space: nowrap; color: var(--ink-2); }
.hist td.est { color: var(--sig); }

.skip ul { list-style: none; display: grid; gap: 6px; }
.skip li { display: flex; gap: 10px; font-size: 12.5px; flex-wrap: wrap; align-items: baseline; }
.skip li b { font-weight: 600; min-width: 9em; }
.skip li i { font-style: normal; color: var(--ink-3); font-size: 11px; }
.skip li span { color: var(--ink-2); flex: 1; min-width: 14em; }

/* ── 體質評分 ─────────────────────────────────────── */
.scorehead { display: flex; align-items: stretch; gap: 22px; flex-wrap: wrap; margin-bottom: 16px; }
.scorehead .big { display: flex; align-items: baseline; gap: 10px; }
.tot { font-size: 46px; font-weight: 700; letter-spacing: -.03em; line-height: 1; }
.tot.good { color: var(--pos); } .tot.mid { color: var(--ink); }
.tot.weak { color: var(--neg); } .tot.na { color: var(--ink-3); }
.grade { font-size: 15px; font-weight: 600; }
.arrow, .cov { display: flex; flex-direction: column; gap: 2px; justify-content: center;
  border-left: 1px solid var(--rule-2); padding-left: 18px; }
.arrow b { font-size: 14px; }
.arrow.a-up1 b, .arrow.a-up2 b { color: var(--pos); }
.arrow.a-down1 b, .arrow.a-down2 b { color: var(--neg); }
.arrow small, .cov small { font-size: 11.5px; color: var(--ink-3); line-height: 1.55; }
.cov b { font-size: 14px; }
.covw { margin-top: 3px; }
.covw b { font-size: 11.5px; }
.covw i { font-style: normal; color: var(--ink-3); }
.covnote { font-size: 11.5px; color: var(--ink-3); line-height: 1.75; margin-bottom: 12px; }
.covnote b { color: var(--ink-2); }
.nototal { font-size: 12.5px; color: var(--ink-2); line-height: 1.8; border-left: 2px solid var(--sig);
  padding-left: 10px; margin-bottom: 14px; }

.dims { display: grid; gap: 6px; }
.dimhead { width: 100%; display: grid; grid-template-columns: minmax(7em, 1.4fr) auto minmax(60px, 2fr) 2.4em 4.6em 1em;
  align-items: center; gap: 10px; background: none; border: 0; border-top: 1px solid var(--rule-2);
  padding: 9px 0; cursor: pointer; font-family: inherit; text-align: left; }
.dname { font-size: 13px; font-weight: 600; }
.dw { font-size: 10.5px; color: var(--ink-3); }
.bar { display: block; height: 7px; background: var(--paper); position: relative; }
.bar i { display: block; height: 100%; }
.bar i.good { background: var(--pos); } .bar i.mid { background: var(--ink-3); }
.bar i.weak { background: var(--neg); } .bar i.na { background: transparent; }
.dscore { font-size: 14px; font-weight: 700; text-align: right; }
.dscore.good { color: var(--pos); } .dscore.weak { color: var(--neg); }
.dcov { font-size: 10.5px; color: var(--ink-3); text-align: right; }
.caret { font-family: var(--mono); color: var(--ink-3); text-align: right; }
.dimout { font-size: 12px; color: var(--sig); line-height: 1.7; padding: 0 0 8px; }
.dimbody { padding: 4px 0 16px; }
.ddesc { font-size: 12.5px; color: var(--ink-2); line-height: 1.8; margin-bottom: 10px; }
.items { width: 100%; border-collapse: collapse; display: block; overflow-x: auto; }
.items th { font-size: 10.5px; color: var(--ink-3); font-weight: 500; text-align: left;
  padding: 3px 8px 3px 0; white-space: nowrap; border-bottom: 1px solid var(--rule-2); }
.items td { font-size: 12px; padding: 5px 8px 5px 0; border-bottom: 1px solid var(--rule-2);
  vertical-align: top; }
.items .r { text-align: right; }
.items tr.off td { color: var(--ink-3); }
.items td em { display: block; font-style: normal; font-size: 11px; color: var(--ink-3); margin-top: 2px; }
.items td i.est { font-style: normal; font-size: 10px; color: var(--sig); margin-left: 5px; }
.items td.anch { color: var(--ink-3); font-size: 10.5px; white-space: nowrap; }
.items td.w { color: var(--ink-3); }
.items td.good { color: var(--pos); font-weight: 600; }
.items td.weak { color: var(--neg); font-weight: 600; }
.dnote { font-size: 11.5px; color: var(--ink-3); line-height: 1.8; margin-top: 8px; }
.scoretrend { display: grid; grid-template-columns: auto 1fr auto; align-items: center; gap: 10px;
  margin-top: 16px; padding-top: 12px; border-top: 1px solid var(--rule-2); }
.tlabel, .taxis { font-size: 10.5px; color: var(--ink-3); white-space: nowrap; }
.disclaimer { margin-top: 14px; font-size: 11.5px; color: var(--ink-2); line-height: 1.85;
  background: var(--paper); padding: 10px 12px; }
.disclaimer b { color: var(--ink); }
.disclaimer code { font-family: var(--mono); font-size: 10.5px; }
@media (max-width: 560px) {
  .dimhead { grid-template-columns: 1fr auto 2.4em 1em; }
  .dimhead .dw { display: none; }
  .dcov { font-size: 9.5px; }
  .scorehead { gap: 12px; }
  .arrow, .cov { border-left: 0; padding-left: 0; }
}
.disclaim { font-size: 11.5px; color: var(--ink-3); line-height: 1.7; margin-top: 6px; }
</style>
