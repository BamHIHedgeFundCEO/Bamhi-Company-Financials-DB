<script setup lang="ts">
/**
 * 13F 機構持股。資料來自離線批次（`tools/f13.py` → `config/f13/`），執行期零 SEC 請求。
 *
 * 呈現上把兩件事分開，因為它們的訊息量完全不同：
 * - **增持／減持**：前幾名永遠是 Vanguard、BlackRock、State Street，
 *   它們按指數權重機械式加減碼，訊號接近零
 * - **建倉／清倉**：0 → 有、有 → 0，那是主動決策，才是值得看的
 */
const route = useRoute()
const ticker = String(route.params.ticker || '').toUpperCase()

const { data, pending, error } = await useAsyncData(
  `funds-${ticker}`,
  () => $fetch<any>(`/api/funds?ticker=${ticker}`),
  { server: false },
)

const nf = new Intl.NumberFormat('en-US')
const sh = (v: number | null | undefined) => (v == null ? '—' : nf.format(Math.round(v)))
const money = (v: number | null | undefined) => {
  if (v == null) return '—'
  if (Math.abs(v) >= 1e8) return `$${nf.format(Math.round(v / 1e8))} 億`
  if (Math.abs(v) >= 1e6) return `$${nf.format(Math.round(v / 1e6))} 百萬`
  return `$${nf.format(Math.round(v))}`
}
const signed = (v: number) => `${v >= 0 ? '+' : ''}${nf.format(Math.round(v))}`
/** 13F 的期別是 `31-MAR-2026` 這種格式 */
function periodZh(p?: string) {
  if (!p) return ''
  const m = p.match(/^(\d{2})-([A-Z]{3})-(\d{4})$/i)
  if (!m) return p
  const q: Record<string, string> = { MAR: 'Q1', JUN: 'Q2', SEP: 'Q3', DEC: 'Q4' }
  return `${m[3]} ${q[m[2].toUpperCase()] ?? m[2]}`
}

/** 換過號的公司會對到不只一個 CUSIP（Carnival 2026Q2 重新註冊就是） */
const cusips = computed(() => {
  const c = data.value?.cusip
  return Array.isArray(c) ? c.join(' / ') : c
})
const holderDelta = computed(() => {
  const d = data.value
  return d?.available ? (d.holders ?? 0) - (d.holdersPrev ?? 0) : 0
})
const TABLES = [
  { key: 'topOpened', zh: '建倉', desc: '上一季完全沒有、這一季開始持有', hot: true, col: '持股', delta: false },
  { key: 'topClosed', zh: '清倉', desc: '上一季持有、這一季完全出清', hot: true, col: '原持股', delta: false },
  { key: 'topIncreased', zh: '增持', desc: '依增加股數排序；市值是整個部位的，不是加碼的那一段', hot: false, col: '增加', delta: true },
  { key: 'topDecreased', zh: '減持', desc: '依減少股數排序；市值是整個部位的，不是減碼的那一段', hot: false, col: '減少', delta: true },
] as const

const zhNames: Record<string, string> = {
  NVDA: '輝達', AAPL: '蘋果', TSLA: '特斯拉', MSFT: '微軟', AMZN: '亞馬遜',
  GOOGL: 'Alphabet', GOOG: 'Alphabet', META: 'Meta', AMD: '超微', TSM: '台積電',
}
useHead({ title: `${ticker} 13F 機構持股｜本季建倉、清倉、增減持` })
</script>

<template>
  <div>
    <TickerTabs :ticker="ticker" :company="data?.company" :zh="zhNames[ticker]"
                :meta="data?.available ? [`CUSIP ${cusips}`, `${periodZh(data.period)} 季末`,
                                          `${nf.format(data.filers || 0)} 家機構申報`] : []" />

    <main class="wrap funds">
      <p v-if="pending" class="state">讀取 13F 索引<span class="dots" /></p>
      <p v-else-if="error" class="state err">
        讀不到：{{ (error as any)?.data?.message || '請確認代號' }}
      </p>

      <!-- 沒有資料 -->
      <section v-else-if="!data?.available" class="cardblock">
        <div class="blockhead">
          <span class="num">§—</span><h2>13F 機構持股</h2><span class="badge">無紀錄</span>
        </div>
        <p class="lead">{{ data?.reason }}</p>
        <p class="tinynote" v-if="data?.generated">
          索引建立於 {{ data.generated }}，涵蓋 {{ periodZh(data.period) }} 季末。
        </p>
      </section>

      <template v-else>
        <p class="caution">
          13F 有兩個先天限制，看數字前要先知道：
          <b>①</b> 季末後 45 天內申報，你看到的永遠是<b>上一季末</b>的部位，不是現在；
          <b>②</b> 只揭露<b>多頭部位</b> —— 放空、選擇權空方、非美股都不在裡面。
          另外「家數」是一票一家，一家小型基金建倉和 Vanguard 加碼在家數上等重。
        </p>
        <p class="caution vintage">
          本頁比較的是 <b>{{ periodZh(data.period) }} 季末</b> 對 <b>{{ periodZh(data.periodPrev) }} 季末</b>。
          <template v-if="data.live">
            資料直接來自 EDGAR 申報索引，涵蓋到索引建立日 {{ data.generated }} 為止已送出的申報。
          </template>
          <template v-else>
            資料來自 SEC 的 13F 批次資料集，而那是<b>滾動三個月的申報視窗</b>、發布有時差
            —— 季末後 45 天的申報截止日剛過的那一個月，這裡會還停在<b>再前一季</b>。
            要拿到最新一季得用 <span class="mono">python tools/f13.py --live</span> 重跑。
          </template>
        </p>

        <!-- 這一季有分割 -->
        <p v-if="data.splitFactor" class="caution">
          這一季有一次<b>沒有人買賣、但每個人股數都變了</b>的公司行為 —— 分割、反向分割、
          分拆或股票股利，倍數 <b>{{ data.splitFactor }}</b>。直接比較的話全體會變成同一個方向
          （實測 KLAC 是 1,919 家增持對 9 家減持、HON 是 1,889 家減持對 95 家增持）。
          下面的增減持已把<b>上一季的股數乘上倍數</b>再比，市值不動 —— 這類事件不改變市值。
        </p>

        <!-- 概況 -->
        <section class="cards">
          <div class="card wide">
            <h3>持有機構<span class="n">{{ periodZh(data.period) }} 季末</span></h3>
            <p class="big">{{ nf.format(data.holders) }} <em>家</em></p>
            <p class="sub">
              上季 {{ nf.format(data.holdersPrev) }} 家
              <b :class="holderDelta >= 0 ? 'up' : 'down'">{{ signed(holderDelta) }}</b>
            </p>
          </div>
          <div class="card">
            <h3>合計持股</h3>
            <p class="big">{{ sh(data.totalShares) }} <em>股</em></p>
            <p class="sub">申報市值 {{ money(data.totalValue) }}</p>
          </div>
          <div class="card grid4">
            <div><dt>建倉</dt><dd class="up">{{ data.opened }}</dd></div>
            <div><dt>清倉</dt><dd class="down">{{ data.closed }}</dd></div>
            <div><dt>增持</dt><dd>{{ data.increased }}</dd></div>
            <div><dt>減持</dt><dd>{{ data.decreased }}</dd></div>
            <p class="foot">
              持股不變 {{ data.unchanged }} 家<template v-if="data.pendingIn || data.pendingOut">
              ／本期尚未申報 {{ (data.pendingIn || 0) + (data.pendingOut || 0) }} 家</template>
            </p>
          </div>
        </section>

        <!-- 申報主體重組 -->
        <section v-if="data.reorgs" class="cardblock warn">
          <div class="blockhead">
            <span class="num">⚠</span><h2>本季有申報主體重組，{{ data.reorgs }} 筆</h2>
          </div>
          <p class="lead">
            同一個集團換了申報主體（換 CIK），在原始資料裡會同時長出一筆巨額清倉與一筆巨額建倉，
            但<b>沒有人真的買賣過那些股票</b>。下面這幾筆已經從建倉／清倉排行榜移除
            —— 但上面的<b>家數統計沒有調整</b>，因為配對是靠名稱推測的，
            不該讓推測值去改真正的數字。
          </p>
          <table class="tab">
            <thead><tr><th>原申報主體</th><th /><th>新申報主體</th><th class="r">原持股</th><th class="r">新持股</th></tr></thead>
            <tbody>
              <tr v-for="(r, i) in data.topReorgs" :key="i">
                <td>{{ r.outof.name }}</td><td class="arrow">→</td><td>{{ r.into.name }}</td>
                <td class="r mono">{{ sh(r.outof.shares) }}</td>
                <td class="r mono">{{ sh(r.into.shares) }}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <!-- 本期尚未申報 -->
        <section v-if="data.pendingOut || data.pendingIn" class="cardblock warn">
          <div class="blockhead">
            <span class="num">◔</span>
            <h2>{{ (data.pendingIn || 0) + (data.pendingOut || 0) }} 家的申報還沒進來</h2>
          </div>
          <p class="lead">
            13F 的截止日是季末後 45 天。遲交的、改交 13F-NT（持股由母公司代為申報）的、
            以及<b>申請保密延後揭露</b>的（挪威主權基金 Norges Bank 每年 Q1／Q3 都只交一份
            一列的殘缺申報，滿一年後才補完整版），在原始資料上長得跟真的賣光一模一樣 ——
            <b>{{ data.pendingOut }} 家</b>只是這一季還沒交，照算就會變成 {{ data.pendingOut }} 筆假清倉，
            但沒有人賣過任何一股。
            <template v-if="data.pendingIn">
              反過來另有 <b>{{ data.pendingIn }} 家</b>這一季有、但上一季查無申報
              （遲交、或第一次達到 1 億美元門檻），無從判斷是新建倉還是本來就持有。
            </template>
            判準是硬事實（那家那一期到底有沒有交過任何一份 13F），不是名稱推測，
            所以<b>已經從上面的建倉／清倉家數扣掉</b>；上面的重組配對則相反，
            只移出排行榜、不動家數。
          </p>
          <table v-if="(data.topPendingOut || []).length" class="tab">
            <thead><tr><th>機構</th><th class="r">上一季持股</th><th class="r">上一季市值</th><th class="r">CIK</th></tr></thead>
            <tbody>
              <tr v-for="x in data.topPendingOut" :key="x.cik">
                <td>{{ x.name }}</td>
                <td class="r mono">{{ sh(x.shares) }}</td>
                <td class="r mono">{{ money(x.value) }}</td>
                <td class="r mono cik">{{ x.cik }}</td>
              </tr>
            </tbody>
          </table>
        </section>

        <!-- 四張榜 -->
        <section v-for="(t, i) in TABLES" :key="t.key" class="cardblock">
          <div class="blockhead">
            <span class="num">§{{ i + 1 }}</span>
            <h2>{{ periodZh(data.period) }} {{ t.zh }}前 {{ (data[t.key] || []).length }} 大</h2>
            <span class="hint">{{ t.desc }}</span>
            <span v-if="t.hot" class="flag">主動決策</span>
          </div>
          <table v-if="(data[t.key] || []).length" class="tab">
            <thead>
              <tr><th class="idx">#</th><th>機構</th><th class="r">{{ t.col }}（股）</th>
                <th v-if="t.delta" class="r">期末持股</th>
                <th class="r">{{ t.key === 'topClosed' ? '原申報市值' : '期末申報市值' }}</th>
                <th class="r">CIK</th></tr>
            </thead>
            <tbody>
              <tr v-for="(x, k) in data[t.key]" :key="x.cik">
                <td class="idx">{{ String(k + 1).padStart(2, '0') }}</td>
                <td>{{ x.name }}</td>
                <td class="r mono" :class="{ up: t.key === 'topIncreased', down: t.key === 'topDecreased' }">
                  {{ x.delta != null ? signed(x.delta) : sh(x.shares) }}
                </td>
                <td v-if="t.delta" class="r mono">{{ sh(x.shares) }}</td>
                <td class="r mono">{{ money(x.value) }}</td>
                <td class="r mono cik">{{ x.cik }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="tinynote">{{ periodZh(data.period) }} 沒有{{ t.zh }}紀錄。</p>
        </section>
      </template>

      <p class="disclaim">
        資料來源：SEC EDGAR 申報索引與 Form 13F Data Sets（機構持股申報）、SEC 交割失敗檔（CUSIP 對照），
        由離線批次 <span class="mono">tools/f13.py</span> 建立索引，本頁查詢不向 SEC 發出任何請求。
        僅計普通股部位：選擇權（PUTCALL）與債券本金（PRN）已排除。
        修正申報已依 SEC 規則解析（RESTATEMENT 整份取代、NEW HOLDINGS 與原申報相加），
        且一律在所有來源收齊之後才解析 —— 原申報與它的補充申報常常分屬不同批次。
        索引建立日 {{ data?.generated || '—' }}。
      </p>
    </main>
  </div>
</template>

<style scoped>
.funds { padding-top: 22px; }
.state { font-family: var(--mono); font-size: 13px; color: var(--ink-2); padding: 40px 0; }
.state.err { color: var(--sig); }
.dots::after { content: '…'; animation: d 1.2s steps(4) infinite; }
@keyframes d { 0% { content: '' } 25% { content: '.' } 50% { content: '..' } 75% { content: '...' } }
.caution { font-size: 12.5px; color: var(--ink-2); border-left: 2px solid var(--sig);
  padding-left: 10px; margin-bottom: 18px; line-height: 1.8; }
.caution.vintage { border-left-color: var(--rule); margin-top: -8px; }
.caution .mono { font-family: var(--mono); font-size: 11.5px; }
.cards { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 14px;
  margin-bottom: 20px; }
.card { background: var(--surface); border: 1px solid var(--rule); padding: 14px 16px 15px; }
.card h3 { font-size: 12.5px; font-weight: 600; display: flex; align-items: baseline; gap: 8px; }
.card h3 .n { margin-left: auto; font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); }
.card .big { font-family: var(--mono); font-size: 26px; font-weight: 700; line-height: 1.3;
  letter-spacing: -.01em; }
.card .big em { font-style: normal; font-size: 13px; font-weight: 400; color: var(--ink-3); }
.card .sub { font-size: 11.5px; color: var(--ink-3); font-family: var(--mono); }
.card .sub b { margin-left: 8px; }
.card.grid4 { display: grid; grid-template-columns: 1fr 1fr; gap: 0 14px; }
.card.grid4 > div { display: flex; align-items: baseline; border-top: 1px solid var(--rule-2);
  padding: 5px 0; }
.card.grid4 dt { font-size: 12px; color: var(--ink-2); }
.card.grid4 dd { margin-left: auto; font-family: var(--mono); font-size: 15px; font-weight: 700; }
.card.grid4 .foot { grid-column: 1 / -1; font-size: 10.5px; color: var(--ink-3); margin-top: 4px; }
.up { color: var(--green); }
.down { color: var(--sig); }
.cardblock { background: var(--surface); border: 1px solid var(--rule); padding: 18px 20px 20px;
  margin-bottom: 20px; }
.cardblock.warn { border-left: 3px solid var(--sig); }
.blockhead { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 12px; }
.blockhead h2 { font-size: 14px; font-weight: 600; }
.blockhead .num { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); }
.blockhead .hint { font-size: 12.5px; color: var(--ink-3); }
.blockhead .flag { margin-left: auto; font-family: var(--mono); font-size: 9.5px; letter-spacing: .1em;
  padding: 2px 6px; border: 1px solid var(--green); color: var(--green); }
.badge { font-family: var(--mono); font-size: 10px; letter-spacing: .1em; padding: 2px 7px;
  border: 1px solid var(--sig); color: var(--sig); }
.lead { font-size: 13px; line-height: 1.8; margin-bottom: 12px; }
.tab { width: 100%; border-collapse: collapse; font-size: 13px; }
.tab th { text-align: left; font-size: 10.5px; color: var(--ink-3); font-weight: 500;
  border-bottom: 1px solid var(--ink); padding: 4px 10px 4px 0; }
.tab td { border-top: 1px solid var(--rule-2); padding: 5px 10px 5px 0; }
.tab .r { text-align: right; padding-right: 0; }
.tab .idx { font-family: var(--mono); font-size: 11px; color: var(--ink-3); width: 2.5em; }
.tab .mono { font-family: var(--mono); font-size: 12px; }
.tab .cik { color: var(--ink-3); font-size: 11px; }
.tab .arrow { color: var(--ink-3); padding: 0 6px; }
.tinynote { font-size: 11.5px; color: var(--ink-3); }
.disclaim { font-size: 11.5px; color: var(--ink-3); line-height: 1.7; }
.mono { font-family: var(--mono); font-size: 11px; }
</style>
