<script setup lang="ts">
/**
 * 首頁的「本季機構在做什麼」。
 *
 * 兩件事決定了這個介面長什麼樣：
 *
 * ① **排序依據一定要是看得見的一欄。** 之前「建倉·大型」是按
 *   「建倉家數 ÷ 上季持有家數」排的，畫面上卻只有建倉家數（250 → 696 → 387），
 *   看起來就像沒排序。現在第一個數字欄一律是那張榜的排序鍵，欄名跟著換。
 *
 * ② **規模是篩選器，不是更多張榜。** 只用家數排的話前 20 名永遠是持有機構
 *   本來就多的那些。規模切換套在任何一張榜上，比開 12×4 張榜好維護。
 *   規模用**機構申報持股市值**分段（不是公司市值）—— 在外流通股數那筆事實不可靠，
 *   companyfacts 裡 Visa 最新的一筆是 2010 年的。
 */
const size = ref<'all' | 'big' | 'mid' | 'small'>('all')
const { data, pending } = await useAsyncData(
  'f13-leaders',
  () => $fetch<any>(`/api/f13leaders?size=${size.value}`),
  { server: false, watch: [size] },
)

const SIZES = [
  { k: 'all', zh: '全部', hint: '不分規模' },
  { k: 'big', zh: '大型', hint: '機構持股市值 ≥ $100 億' },
  { k: 'mid', zh: '中型', hint: '$10–100 億' },
  { k: 'small', zh: '小型', hint: '< $10 億' },
] as const

const GROUPS = [
  {
    id: 'open', zh: '建倉', hint: '上一季完全沒有、這一季開始持有',
    tabs: [
      { key: 'openedAbs', zh: '依家數', desc: '多少家機構一起建倉' },
      { key: 'openedRel', zh: '依比例', desc: '同樣是 50 家建倉，本來只有 100 家持有的那一檔意義大得多' },
    ],
  },
  {
    id: 'close', zh: '清倉', hint: '上一季持有、這一季完全出清',
    tabs: [
      { key: 'closedAbs', zh: '依家數', desc: '多少家機構一起出清' },
      { key: 'closedRel', zh: '依比例', desc: '已排除機構持股市值 < $5,000 萬的破產殼股' },
    ],
  },
  {
    id: 'holders', zh: '持有家數', hint: '建倉、清倉、遲交全部算進去之後的淨變化',
    tabs: [
      { key: 'netHolders', zh: '淨增最多', desc: '本季持有家數 − 上季' },
      { key: 'netHoldersDown', zh: '淨減最多', desc: '上季持有家數 − 本季' },
    ],
  },
  {
    id: 'shares',
    zh: '合計持股',
    hint: '所有機構申報持股加總的季增率。分割與股票股利已正規化；'
      + '限上季 ≥100 家持有、機構持股市值 ≥ $3 億 —— 微型股的股數比率會爆掉',
    tabs: [
      { key: 'netSharesUp', zh: '增幅最大', desc: '也可能來自公司增資或換股併購，不只是買進' },
      { key: 'netSharesDown', zh: '減幅最大', desc: '機構合計持股減少最多的' },
    ],
  },
  {
    id: 'fresh', zh: '新上市／分拆', hint: '上一季還不存在（或不到 20 家持有）的標的',
    tabs: [
      { key: 'fresh', zh: '本季新出現', desc: '股東是「收到」股票不是買進，所以不放進建倉榜' },
    ],
  },
] as const

const group = ref(0)
const tab = ref(0)
watch(group, () => (tab.value = 0))
const tabs = computed(() => GROUPS[group.value].tabs)
const active = computed(() => tabs.value[tab.value])
const board = computed<any>(() => data.value?.boards?.[active.value.key] || null)
const rows = computed<any[]>(() => board.value?.rows || [])

const nf = new Intl.NumberFormat('en-US')
const signed = (v: number) => `${v >= 0 ? '+' : ''}${nf.format(Math.round(v))}`
const pct = (v: number) => `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%`
/** 排序鍵：家數就寫家數，比例寫百分比 */
const metric = (r: any) => (board.value?.metricKind === 'pct'
  ? `${(r.metric * 100).toFixed(1)}%`
  : nf.format(Math.round(r.metric)))
const usd = (v: number) => (v >= 1e10 ? `$${(v / 1e8).toFixed(0)} 億`
  : v >= 1e8 ? `$${(v / 1e8).toFixed(1)} 億`
    : v >= 1e6 ? `$${(v / 1e6).toFixed(0)} 百萬` : `$${nf.format(Math.round(v))}`)
function periodZh(p?: string) {
  if (!p) return ''
  const m = p.match(/^(\d{2})-([A-Z]{3})-(\d{4})$/i)
  if (!m) return p
  const q: Record<string, string> = { MAR: 'Q1', JUN: 'Q2', SEP: 'Q3', DEC: 'Q4' }
  return `${m[3]} ${q[m[2].toUpperCase()] ?? m[2]}`
}
const splitList = computed(() => Object.entries(data.value?.splits || {}) as [string, number][])
</script>

<template>
  <section class="flow">
    <div class="blockhead">
      <span class="num">§F</span>
      <h2>本季機構在做什麼</h2>
      <span class="hint">
        {{ periodZh(data?.period) }} 對 {{ periodZh(data?.periodPrev) }}，全市場 13F
      </span>
    </div>

    <p v-if="pending && !data" class="state">讀取 13F 榜單<span class="dots" /></p>

    <template v-else-if="data?.available">
      <p class="caution">
        排行榜有一個先天陷阱：<b>只用家數排，前 20 名永遠是持有機構本來就多的那些</b>。
        所以每一種行為都同時給「依家數」與「依比例」兩張榜，再加上規模篩選。
        表格<b>第一個數字欄就是那張榜的排序依據</b>，欄名會跟著換。
        所有榜都要求上一季至少 20 家持有 —— 3 家變 6 家就是 +100%，
        而分拆出來的新公司（HONA 上季 0 家、本季 2,055 家）會霸佔建倉榜，另立一張。
      </p>

      <nav class="groups">
        <button
          v-for="(g, i) in GROUPS" :key="g.id"
          :class="['gbtn', { on: group === i }]" @click="group = i"
        >{{ g.zh }}</button>
      </nav>
      <p class="ghint">{{ GROUPS[group].hint }}</p>

      <div class="controls">
        <nav class="tabs">
          <button
            v-for="(t, i) in tabs" :key="t.key"
            :class="['tbtn', { on: tab === i }]" @click="tab = i"
          >{{ t.zh }}</button>
        </nav>
        <nav class="sizes">
          <span class="slab">規模</span>
          <button
            v-for="s in SIZES" :key="s.k"
            :class="['sbtn', { on: size === s.k }]" :title="s.hint"
            @click="size = s.k as any"
          >{{ s.zh }}<em v-if="data.counts">{{ nf.format(data.counts[s.k]) }}</em></button>
        </nav>
      </div>
      <p class="tdesc">{{ active.desc }}</p>

      <table class="tab">
        <thead>
          <tr>
            <th class="idx">#</th><th>代號</th><th class="co">公司</th>
            <th class="r key">{{ board?.metricLabel }}</th>
            <th class="r">持有家數</th><th class="r">建倉</th><th class="r">清倉</th>
            <th class="r">合計持股</th><th class="r">機構持股市值</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in rows" :key="r.ticker">
            <td class="idx">{{ String(i + 1).padStart(2, '0') }}</td>
            <td class="tk"><NuxtLink :to="`/stock/${r.ticker}/funds`">{{ r.ticker }}</NuxtLink></td>
            <td class="co">{{ r.company }}</td>
            <td class="r mono key">{{ metric(r) }}</td>
            <td class="r mono">
              {{ nf.format(r.holders) }}
              <em :class="r.holders >= r.holdersPrev ? 'up' : 'down'">
                {{ signed(r.holders - r.holdersPrev) }}
              </em>
            </td>
            <td class="r mono up">{{ nf.format(r.opened) }}</td>
            <td class="r mono down">{{ nf.format(r.closed) }}</td>
            <td class="r mono" :class="r.netShares >= 0 ? 'up' : 'down'">{{ pct(r.netShares) }}</td>
            <td class="r mono">{{ usd(r.totalValue) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length" class="tinynote">這個規模級距下沒有符合條件的標的。</p>

      <p v-if="splitList.length" class="tinynote">
        這一季偵測到並已正規化的分割／反向分割／股票股利（{{ splitList.length }} 檔）：
        <b v-for="([t, f], i) in splitList" :key="t">
          <template v-if="i">、</template>{{ t }} ×{{ f }}
        </b>。
        不修正的話那些標的的持有人會全體被算成同一個方向 —— 但沒有人買賣過。
      </p>
      <p class="tinynote">
        規模用<b>機構申報持股市值</b>分段，不是公司市值 —— 在外流通股數那筆事實不可靠
        （companyfacts 裡 Visa 最新的一筆是 2010 年的），而 13F 自己就帶市值。
        13F 只揭露多頭部位，且是季末後 45 天內申報的<b>上一季末</b>快照，不是現在。
        遲交、改交 13F-NT、申請保密延後揭露的機構已排除在建倉／清倉之外，個股頁另有一區列出。
      </p>
    </template>
  </section>
</template>

<style scoped>
.flow { margin-top: 34px; }
.blockhead { display: flex; align-items: baseline; gap: 10px; border-bottom: 1px solid var(--ink);
  padding-bottom: 7px; margin-bottom: 12px; }
.blockhead .num { font-family: var(--mono); font-size: 11px; color: var(--ink-3); }
.blockhead h2 { font-size: 14px; font-weight: 600; letter-spacing: .02em; }
.blockhead .hint { margin-left: auto; font-family: var(--mono); font-size: 11px; color: var(--ink-3); }
.state { font-family: var(--mono); font-size: 13px; color: var(--ink-2); padding: 30px 0; }
.caution { font-size: 12.5px; color: var(--ink-2); border-left: 2px solid var(--sig);
  padding-left: 10px; margin-bottom: 14px; line-height: 1.8; }
.groups { display: flex; border-bottom: 1px solid var(--rule); flex-wrap: wrap; }
.gbtn { appearance: none; background: none; border: 0; border-bottom: 2px solid transparent;
  padding: 7px 14px; font-size: 13px; color: var(--ink-2); cursor: pointer; font-weight: 500; }
.gbtn.on { color: var(--ink); border-bottom-color: var(--ink); }
.ghint { font-size: 11.5px; color: var(--ink-3); margin: 7px 0 0; line-height: 1.7; }
.controls { display: flex; align-items: center; gap: 14px; margin: 9px 0 4px; flex-wrap: wrap; }
.tabs, .sizes { display: flex; align-items: center; gap: 6px; }
.sizes { margin-left: auto; }
.slab { font-size: 11px; color: var(--ink-3); }
.tbtn, .sbtn { appearance: none; background: var(--surface); border: 1px solid var(--rule);
  padding: 4px 11px; font-size: 12px; color: var(--ink-2); cursor: pointer; }
.tbtn.on, .sbtn.on { background: var(--ink); color: var(--surface); border-color: var(--ink); }
.sbtn em { font-style: normal; font-size: 9.5px; margin-left: 5px; opacity: .6;
  font-family: var(--mono); }
.tdesc { font-size: 11.5px; color: var(--ink-3); margin: 0 0 11px; }
.tab { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.tab th { text-align: left; font-weight: 500; font-size: 11px; color: var(--ink-3);
  border-bottom: 1px solid var(--rule); padding: 5px 8px; white-space: nowrap; }
.tab td { padding: 5px 8px; border-bottom: 1px solid var(--rule); }
.tab .r, .tab th.r { text-align: right; }
.tab .idx { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); width: 30px; }
.tab .tk a { font-family: var(--mono); font-weight: 600; color: var(--ink); text-decoration: none; }
.tab .tk a:hover { text-decoration: underline; }
.tab .co { color: var(--ink-2); max-width: 260px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
/* 排序依據那一欄：畫面上一定要看得出來是它在排 */
.tab th.key { color: var(--ink); font-weight: 600; }
.tab td.key { font-weight: 600; color: var(--ink); background: var(--surface); }
.mono { font-family: var(--mono); }
.tab em { font-style: normal; font-size: 10.5px; margin-left: 5px; }
.up { color: var(--pos, #0a7); }
.down { color: var(--sig, #c33); }
.tinynote { font-size: 11.5px; color: var(--ink-3); margin-top: 9px; line-height: 1.75; }
@media (max-width: 760px) {
  .tab .co, .tab th.co { display: none; }
  .sizes { margin-left: 0; }
}
</style>
