<script setup lang="ts">
/**
 * 內部人買賣（Form 3/4/5）。
 *
 * 正確性的核心在**分流**：多數 Form 4 是股權獎勵入帳（A）與行權後依 10b5-1
 * 預定計畫賣出（M→S），那些不是看空訊號。把它們加總成「內部人賣出 $X」
 * 是這類頁面最常見的錯誤，本頁分成三欄呈現、預設不合併。
 */
const route = useRoute()
const ticker = String(route.params.ticker || '').toUpperCase()

/** 預設 30 份：每份 Form 4 是一次 SEC 請求、限速 100ms，60 份的冷啟動實測 6.7 秒。
 *  30 份約覆蓋大公司三個月、中小型公司一兩年，要更久按上面的按鈕。
 *  第二次查同一家會走永久快取，幾乎不花時間。 */
const limit = ref(30)
const { data, pending, error } = await useAsyncData(
  `insider-${ticker}`,
  () => $fetch<any>(`/api/insider?ticker=${ticker}&limit=${limit.value}`),
  { server: false, watch: [limit] },
)

type Bucket = 'open' | 'plan' | 'comp' | 'other'
const BUCKET: Record<Bucket, { zh: string; desc: string }> = {
  open: { zh: '自主買賣', desc: '公開市場買入（P）與非預定計畫的賣出（S）—— 真正帶訊號的一類' },
  plan: { zh: '10b5-1 計畫賣出', desc: '依事先訂立的交易計畫自動執行，時點與看法無關' },
  comp: { zh: '薪酬相關', desc: '股權獎勵入帳（A）、選擇權行權（M）、扣繳稅款交回（F）' },
  other: { zh: '其他', desc: '贈與、權益交換等' },
}
const CODE_ZH: Record<string, string> = {
  P: '公開市場買入', S: '公開市場賣出', A: '股權獎勵取得', D: '對發行人處分',
  F: '扣繳稅款交回', M: '選擇權行權', C: '轉換', G: '贈與', V: '自願提前申報',
  X: '選擇權行使', J: '其他取得或處分', K: '權益交換',
}

const txns = computed<any[]>(() => data.value?.transactions ?? [])
const filter = ref<Bucket | 'all'>('all')
const shown = computed(() =>
  filter.value === 'all' ? txns.value : txns.value.filter((t) => t.bucket === filter.value))

function agg(bucket: Bucket) {
  const rows = txns.value.filter((t) => t.bucket === bucket && !t.derivative)
  const buy = rows.filter((t) => t.ad === 'A')
  const sell = rows.filter((t) => t.ad === 'D')
  const sum = (xs: any[]) => xs.reduce((s, t) => s + (t.value ?? 0), 0)
  return {
    n: rows.length,
    buyN: buy.length, sellN: sell.length,
    buyV: sum(buy), sellV: sum(sell),
    /** 有股數但沒單價的筆數：獎勵入帳沒有價格，金額欄不能當 0 */
    noPrice: rows.filter((t) => t.shares != null && t.price == null).length,
  }
}
const cards = computed(() => (['open', 'plan', 'comp', 'other'] as Bucket[])
  .map((b) => ({ id: b, ...BUCKET[b], ...agg(b) })).filter((c) => c.n > 0))

const nf = new Intl.NumberFormat('en-US')
const money = (v: number) => (v ? `$${nf.format(Math.round(v))}` : '—')

const zhNames: Record<string, string> = {
  NVDA: '輝達', AAPL: '蘋果', TSLA: '特斯拉', MSFT: '微軟', AMZN: '亞馬遜',
  GOOGL: 'Alphabet', GOOG: 'Alphabet', META: 'Meta', AMD: '超微', TSM: '台積電',
}
useHead({ title: `${ticker} 內部人買賣｜Form 4 交易明細與 10b5-1 分流` })
</script>

<template>
  <div>
    <TickerTabs :ticker="ticker" :company="data?.company" :zh="zhNames[ticker]"
                :meta="data ? [`CIK ${data.cik}`, `已讀 ${data.filingsRead}／${data.filingsTotal} 份`] : []" />

    <main class="wrap ins">
      <div class="toolbar">
        <div class="seg">
          <span class="lb">讀取份數</span>
          <button v-for="n in [30, 60, 120]" :key="n" :class="{ on: limit === n }" @click="limit = n">
            {{ n }}
          </button>
        </div>
        <span class="hint">份數愈多回溯愈久；每份申報只會向 SEC 抓一次，之後永久快取</span>
      </div>

      <p v-if="pending" class="state">讀取 Form 3/4/5<span class="dots" /></p>
      <p v-else-if="error" class="state err">
        讀不到 {{ ticker }}：{{ (error as any)?.data?.message || '請確認代號' }}
      </p>

      <template v-else-if="data">
        <p class="caution">
          <b>不要把這四類加總。</b>薪酬相關（A／M／F）是例行入帳與扣稅，
          10b5-1 是幾個月前就排好的自動賣出 —— 兩者都與經營層當下的看法無關。
          真正帶訊號的只有「自主買賣」那一欄，尤其是公開市場<b>買入</b>。
        </p>

        <section class="cards">
          <div v-for="c in cards" :key="c.id" class="card" :class="{ hot: c.id === 'open' }">
            <h3>{{ c.zh }}<span class="n">{{ c.n }} 筆</span></h3>
            <p class="d">{{ c.desc }}</p>
            <dl>
              <div><dt>取得</dt><dd class="mono">{{ c.buyN }} 筆<i>{{ money(c.buyV) }}</i></dd></div>
              <div><dt>處分</dt><dd class="mono">{{ c.sellN }} 筆<i>{{ money(c.sellV) }}</i></dd></div>
            </dl>
            <p v-if="c.noPrice" class="np">其中 {{ c.noPrice }} 筆沒有申報單價（獎勵入帳本來就沒有價格），金額未計入</p>
          </div>
        </section>

        <section class="cardblock">
          <div class="blockhead">
            <span class="num">§1</span><h2>交易明細</h2>
            <div class="seg small">
              <button :class="{ on: filter === 'all' }" @click="filter = 'all'">全部</button>
              <button v-for="c in cards" :key="c.id" :class="{ on: filter === c.id }" @click="filter = c.id">
                {{ c.zh }}
              </button>
            </div>
            <span class="count">{{ shown.length }} 筆</span>
          </div>
          <div class="scroll">
            <table class="tab">
              <thead>
                <tr>
                  <th>交易日</th><th>申報人</th><th>職稱</th><th>證券</th>
                  <th>代碼</th><th class="r">股數</th><th class="r">單價</th><th class="r">金額</th>
                  <th class="r">交易後持股</th><th>類別</th><th />
                </tr>
              </thead>
              <tbody>
                <tr v-for="(t, i) in shown.slice(0, 400)" :key="i" :class="t.bucket">
                  <td class="mono">{{ t.date }}</td>
                  <td>{{ t.owner }}</td>
                  <td class="ti">{{ t.title || '—' }}</td>
                  <td class="ti">{{ t.security }}<i v-if="t.derivative" class="dv">衍生性</i></td>
                  <td class="code"><b>{{ t.code }}{{ t.ad }}</b><i>{{ CODE_ZH[t.code] || '' }}</i></td>
                  <td class="r mono">{{ t.shares != null ? nf.format(t.shares) : 'n/a' }}</td>
                  <td class="r mono">{{ t.price != null ? `$${t.price}` : '—' }}</td>
                  <td class="r mono">{{ t.value != null ? money(t.value) : '—' }}</td>
                  <td class="r mono">{{ t.sharesAfter != null ? nf.format(t.sharesAfter) : '—' }}</td>
                  <td class="bk">{{ BUCKET[t.bucket as Bucket].zh }}<i v-if="t.planned">10b5-1</i></td>
                  <td><a class="src" :href="t.url" target="_blank" rel="noopener">XML ↗</a></td>
                </tr>
              </tbody>
            </table>
          </div>
          <p v-if="shown.length > 400" class="tinynote">只顯示前 400 筆。</p>
        </section>

        <section v-if="data.notes?.length" class="cardblock skip">
          <div class="blockhead"><span class="num">§—</span><h2>讀取範圍</h2></div>
          <ul><li v-for="n in data.notes" :key="n">{{ n }}</li></ul>
        </section>
      </template>

      <p class="disclaim">
        資料來自 SEC EDGAR 的 Form 3/4/5 原始 XML（非 SEC 的 HTML 轉譯版）。
        單價為申報人填報的加權平均價，部分交易以註腳說明價格區間。
        「交易後持股」僅為該筆申報所屬的持有型態（直接／間接分開申報），不是總持股。
        內部人申報有時間差（法定 2 個營業日內申報）。
      </p>
    </main>
  </div>
</template>

<style scoped>
.ins { padding-top: 22px; }
.toolbar { display: flex; align-items: center; gap: 16px; flex-wrap: wrap; margin-bottom: 18px; }
.seg { display: flex; align-items: center; gap: 8px; }
.seg .lb { font-family: var(--mono); font-size: 10px; letter-spacing: .16em;
  text-transform: uppercase; color: var(--ink-3); }
.seg button { font-family: var(--mono); font-size: 12px; padding: 4px 10px; cursor: pointer;
  background: var(--surface); border: 1px solid var(--rule); color: var(--ink-2); margin-left: -1px; }
.seg button.on { background: var(--ink); color: var(--paper); border-color: var(--ink); }
.seg.small button { font-family: var(--sans); font-size: 11.5px; padding: 2px 8px; }
.hint { font-size: 12px; color: var(--ink-3); }
.state { font-family: var(--mono); font-size: 13px; color: var(--ink-2); padding: 40px 0; }
.state.err { color: var(--sig); }
.dots::after { content: '…'; animation: d 1.2s steps(4) infinite; }
@keyframes d { 0% { content: '' } 25% { content: '.' } 50% { content: '..' } 75% { content: '...' } }
.caution { font-size: 12.5px; color: var(--ink-2); border-left: 2px solid var(--sig);
  padding-left: 10px; margin-bottom: 18px; line-height: 1.75; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: 14px;
  margin-bottom: 20px; }
.card { background: var(--surface); border: 1px solid var(--rule); padding: 14px 16px 15px; }
.card.hot { border-color: var(--ink); border-left: 3px solid var(--sig); }
.card h3 { font-size: 13px; display: flex; align-items: baseline; gap: 8px; }
.card h3 .n { margin-left: auto; font-family: var(--mono); font-size: 11px; color: var(--ink-3); }
.card .d { font-size: 11.5px; color: var(--ink-3); line-height: 1.6; margin: 5px 0 9px; }
.card dl > div { display: flex; align-items: baseline; border-top: 1px solid var(--rule-2); padding: 4px 0; }
.card dt { font-size: 11.5px; color: var(--ink-2); width: 3.5em; }
.card dd { margin-left: auto; font-size: 12.5px; }
.card dd i { font-style: normal; color: var(--ink-3); margin-left: 8px; }
.card .np { font-size: 10.5px; color: var(--ink-3); margin-top: 6px; }
.cardblock { background: var(--surface); border: 1px solid var(--rule); padding: 18px 20px 20px;
  margin-bottom: 20px; }
.blockhead { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.blockhead h2 { font-size: 14px; font-weight: 600; }
.blockhead .num { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); }
.blockhead .count { margin-left: auto; font-family: var(--mono); font-size: 11.5px; color: var(--ink-3); }
.scroll { overflow-x: auto; }
.tab { width: 100%; border-collapse: collapse; font-size: 12.5px; white-space: nowrap; }
.tab th { text-align: left; font-size: 10.5px; color: var(--ink-3); font-weight: 500;
  border-bottom: 1px solid var(--ink); padding: 4px 10px 4px 0; }
.tab td { border-top: 1px solid var(--rule-2); padding: 4px 10px 4px 0; }
.tab .r { text-align: right; }
.tab .mono { font-family: var(--mono); font-size: 11.5px; }
.tab .ti { color: var(--ink-2); max-width: 16em; overflow: hidden; text-overflow: ellipsis; }
.tab .dv { font-style: normal; font-size: 10px; color: var(--ink-3); margin-left: 6px; }
.tab .code b { font-family: var(--mono); }
.tab .code i { font-style: normal; font-size: 10.5px; color: var(--ink-3); margin-left: 6px; }
.tab .bk { font-size: 11.5px; color: var(--ink-2); }
.tab .bk i { font-style: normal; font-size: 10px; color: var(--sig); margin-left: 6px; }
.tab tr.open td { background: var(--sig-wash); }
.tab .src { font-family: var(--mono); font-size: 10.5px; color: var(--green); text-decoration: none; }
.tinynote { margin-top: 10px; font-size: 11.5px; color: var(--ink-3); }
.skip ul { list-style: none; font-size: 12.5px; color: var(--ink-2); }
.disclaim { font-size: 11.5px; color: var(--ink-3); line-height: 1.7; }
</style>
