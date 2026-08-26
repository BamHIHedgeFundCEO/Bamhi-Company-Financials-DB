<script setup lang="ts">
/**
 * 首頁的「本季機構在做什麼」。
 *
 * 為什麼每一種行為都要兩張榜：**只用家數排會全部變成大公司**。
 * 持有機構本來就多的標的，建倉、清倉的絕對家數自然也多，前 20 名永遠是
 * 那幾檔權值股，看不到「一群機構默默在小型股上建倉」這種事。
 * 所以絕對榜（多少家一起動）與相對榜（佔上一季持有家數的比例）並排，
 * 再加上按規模分三段各出一張。
 *
 * 相對榜一律要求上一季至少 20 家持有 —— 3 家變 6 家就是 +100%，那不是訊號。
 */
const { data, pending } = await useAsyncData(
  'f13-leaders',
  () => $fetch<any>('/api/f13leaders'),
  { server: false },
)

const GROUPS = [
  {
    id: 'open', zh: '建倉', hint: '上一季完全沒有、這一季開始持有',
    tabs: [
      { key: 'openedAbs', zh: '依家數', desc: '多少家機構一起建倉' },
      { key: 'openedRel', zh: '依比例', desc: '建倉家數 ÷ 上季持有家數' },
      { key: 'openedBig', zh: '大型股', desc: '機構持股市值 ≥ $100 億，依比例' },
      { key: 'openedMid', zh: '中型股', desc: '$10–100 億，依比例' },
      { key: 'openedSmall', zh: '小型股', desc: '< $10 億，依比例' },
    ],
  },
  {
    id: 'close', zh: '清倉', hint: '上一季持有、這一季完全出清',
    tabs: [
      { key: 'closedAbs', zh: '依家數', desc: '多少家機構一起出清' },
      { key: 'closedRel', zh: '依比例', desc: '清倉家數 ÷ 上季持有家數' },
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
    hint: '所有機構申報持股加總的季增率（分割與股票股利已正規化）。'
      + '限上季 ≥100 家持有、機構持股市值 ≥ $3 億 —— 微型股的股數比率會爆掉',
    tabs: [
      { key: 'netSharesUp', zh: '增幅最大', desc: '也可能來自公司增資或換股併購，不只是買進' },
      { key: 'netSharesDown', zh: '減幅最大', desc: '機構合計持股減少最多的' },
    ],
  },
  {
    id: 'fresh', zh: '新上市／分拆', hint: '上一季還不存在（或幾乎沒人持有）的標的，依本季持有家數排',
    tabs: [
      { key: 'fresh', zh: '本季新出現', desc: '股東是「收到」股票，不是有人買進 —— 所以不放進建倉榜' },
    ],
  },
] as const

const group = ref(0)
const tab = ref(0)
watch(group, () => (tab.value = 0))
const tabs = computed(() => GROUPS[group.value].tabs)
const active = computed(() => tabs.value[tab.value])
const rows = computed<any[]>(() => data.value?.boards?.[active.value.key] || [])

const nf = new Intl.NumberFormat('en-US')
const pct = (v: number) => `${v >= 0 ? '+' : ''}${(v * 100).toFixed(1)}%`
const money = (v: number) => {
  if (v >= 1e9) return `$${(v / 1e9).toFixed(0)} 億`.replace('億', '0 億')
  return `$${nf.format(Math.round(v / 1e6))} 百萬`
}
const bnUSD = (v: number) => (v >= 1e10 ? `$${(v / 1e8).toFixed(0)} 億`
  : v >= 1e8 ? `$${(v / 1e8).toFixed(1)} 億` : `$${(v / 1e6).toFixed(0)} 百萬`)
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
  <section v-if="pending || data?.available" class="flow">
    <div class="blockhead">
      <span class="num">§F</span>
      <h2>本季機構在做什麼</h2>
      <span class="hint">
        {{ periodZh(data?.period) }} 對 {{ periodZh(data?.periodPrev) }}，全市場 13F
      </span>
    </div>

    <p v-if="pending" class="state">讀取 13F 榜單<span class="dots" /></p>

    <template v-else>
      <p class="caution">
        排行榜有一個先天陷阱：<b>只用家數排，前 20 名永遠是那幾檔權值股</b>
        —— 持有機構本來就多的標的，建倉與清倉的絕對家數自然也多。
        所以每一種行為都同時給「依家數」與「依比例」兩張榜，建倉再多給三張分規模的。
        所有榜都要求上一季至少 20 家持有 —— 一來 3 家變 6 家就是 +100%，
        二來分拆出來的新公司會霸佔建倉榜（HONA 上季 0 家、本季 2,055 家，
        那是股東收到股票不是有人買進）。那一類另立一張榜。
      </p>

      <nav class="groups">
        <button
          v-for="(g, i) in GROUPS" :key="g.id"
          :class="['gbtn', { on: group === i }]" @click="group = i"
        >{{ g.zh }}</button>
      </nav>
      <p class="ghint">{{ GROUPS[group].hint }}</p>

      <nav class="tabs">
        <button
          v-for="(t, i) in tabs" :key="t.key"
          :class="['tbtn', { on: tab === i }]" @click="tab = i"
        >{{ t.zh }}</button>
        <span class="tdesc">{{ active.desc }}</span>
      </nav>

      <table class="tab">
        <thead>
          <tr>
            <th class="idx">#</th><th>代號</th><th class="co">公司</th>
            <th class="r">持有家數</th><th class="r">建倉</th><th class="r">清倉</th>
            <th class="r">合計持股</th><th class="r">機構持股市值</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(r, i) in rows" :key="r.ticker">
            <td class="idx">{{ String(i + 1).padStart(2, '0') }}</td>
            <td class="tk"><NuxtLink :to="`/stock/${r.ticker}/funds`">{{ r.ticker }}</NuxtLink></td>
            <td class="co">{{ r.company }}</td>
            <td class="r mono">
              {{ nf.format(r.holders) }}
              <em :class="r.holders >= r.holdersPrev ? 'up' : 'down'">
                {{ r.holders - r.holdersPrev >= 0 ? '+' : '' }}{{ r.holders - r.holdersPrev }}
              </em>
            </td>
            <td class="r mono up">{{ r.opened }}</td>
            <td class="r mono down">{{ r.closed }}</td>
            <td class="r mono" :class="r.netShares >= 0 ? 'up' : 'down'">{{ pct(r.netShares) }}</td>
            <td class="r mono">{{ bnUSD(r.totalValue) }}</td>
          </tr>
        </tbody>
      </table>
      <p v-if="!rows.length" class="tinynote">這張榜沒有資料。</p>

      <p v-if="splitList.length" class="tinynote">
        這一季偵測到並已正規化的分割／反向分割／股票股利（{{ splitList.length }} 檔）：
        <b v-for="([t, f], i) in splitList" :key="t">
          <template v-if="i">、</template>{{ t }} ×{{ f }}
        </b>。
        不修正的話，那些標的的持有人會全體被算成同一個方向 —— 但沒有人買賣過。
      </p>
      <p class="tinynote">
        13F 只揭露多頭部位，且是季末後 45 天內申報的<b>上一季末</b>快照，不是現在。
        遲交、改交 13F-NT、申請保密延後揭露的機構已排除在建倉／清倉之外
        （否則會變成假訊號），個股頁另有一區列出。
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
.groups { display: flex; gap: 0; border-bottom: 1px solid var(--rule); }
.gbtn { appearance: none; background: none; border: 0; border-bottom: 2px solid transparent;
  padding: 7px 14px; font-size: 13px; color: var(--ink-2); cursor: pointer; font-weight: 500; }
.gbtn.on { color: var(--ink); border-bottom-color: var(--ink); }
.ghint { font-size: 11.5px; color: var(--ink-3); margin: 7px 0 0; }
.tabs { display: flex; align-items: center; gap: 6px; margin: 9px 0 12px; flex-wrap: wrap; }
.tbtn { appearance: none; background: var(--surface); border: 1px solid var(--rule);
  padding: 4px 11px; font-size: 12px; color: var(--ink-2); cursor: pointer; }
.tbtn.on { background: var(--ink); color: var(--surface); border-color: var(--ink); }
.tdesc { margin-left: auto; font-size: 11.5px; color: var(--ink-3); }
.tab { width: 100%; border-collapse: collapse; font-size: 12.5px; }
.tab th { text-align: left; font-weight: 500; font-size: 11px; color: var(--ink-3);
  border-bottom: 1px solid var(--rule); padding: 5px 8px; white-space: nowrap; }
.tab td { padding: 5px 8px; border-bottom: 1px solid var(--rule); }
.tab .r, .tab th.r { text-align: right; }
.tab .idx { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); width: 30px; }
.tab .tk a { font-family: var(--mono); font-weight: 600; color: var(--ink); text-decoration: none; }
.tab .tk a:hover { text-decoration: underline; }
.tab .co { color: var(--ink-2); max-width: 300px; overflow: hidden;
  text-overflow: ellipsis; white-space: nowrap; }
.mono { font-family: var(--mono); }
.tab em { font-style: normal; font-size: 10.5px; margin-left: 5px; }
.up { color: var(--pos, #0a7); }
.down { color: var(--sig, #c33); }
.tinynote { font-size: 11.5px; color: var(--ink-3); margin-top: 9px; line-height: 1.75; }
@media (max-width: 720px) {
  .tab .co, .tab th.co { display: none; }
  .tdesc { display: none; }
}
</style>
