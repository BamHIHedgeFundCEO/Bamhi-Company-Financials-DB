<script setup lang="ts">
/**
 * 原型 prototype_v2.html 的版面：季度矩陣（點兩格選區間）+ 側欄摘要 + 財報清單。
 * 首頁與個股頁共用；差別只在 ticker 是否預填（首頁側欄顯示熱門標的）。
 */
interface Filing {
  form: string
  fiscalPeriod: string
  reportDate: string
  filingDate: string
  accessionNumber: string
  url: string
  isAmendment: boolean
}
interface FilingsResult {
  company: string
  cik: string
  ticker: string
  sic?: string
  fiscalYearEnd?: string
  isForeignIssuer: boolean
  filings: Filing[]
}

const props = defineProps<{ initialTicker?: string }>()

const ZH: Record<string, string> = {
  NVDA: '輝達', AAPL: '蘋果', TSLA: '特斯拉', MSFT: '微軟', AMZN: '亞馬遜',
  GOOGL: 'Alphabet', GOOG: 'Alphabet', META: 'Meta', AMD: '超微', TSM: '台積電',
  PLTR: 'Palantir', RKLB: 'Rocket Lab',
}
const POPULAR: [string, string][] = [
  ['NVDA', '輝達'], ['AAPL', '蘋果'], ['MSFT', '微軟'], ['TSLA', '特斯拉'],
  ['AMZN', '亞馬遜'], ['GOOGL', 'Alphabet'], ['META', 'Meta'], ['AMD', '超微'],
  ['TSM', '台積電'], ['PLTR', 'Palantir'],
]

const ticker = ref(props.initialTicker ?? '')
type Status = 'idle' | 'loading' | 'ok' | 'notfound' | 'badformat' | 'foreign'
const status = ref<Status>('idle')
const errMsg = ref('')
const result = ref<FilingsResult | null>(null)

interface GridCell { fy: number; q: number; filing: Filing | null }
const years = ref<number[]>([])
const cells = ref<Map<string, Filing>>(new Map())
const sel = ref<Set<string>>(new Set())
const anchor = ref<number | null>(null)
const hoverIdx = ref<number | null>(null)

const key = (fy: number, q: number) => `${fy}-${q}`
const idxOf = (fy: number, q: number) => years.value.indexOf(fy) * 4 + (q - 1)

function parseFp(fp: string): { fy: number; q: number } | null {
  const m = fp.match(/^FY(\d{4})(?: Q([1-4]))?$/)
  if (!m) return null
  return { fy: Number(m[1]), q: m[2] ? Number(m[2]) : 4 }
}

async function search() {
  const t = ticker.value.trim().toUpperCase()
  if (!t) return
  if (!/^[A-Z]{1,5}(\.[A-Z])?$/.test(t)) {
    status.value = 'badformat'
    return
  }
  status.value = 'loading'
  result.value = null
  const thisYear = new Date().getFullYear()
  try {
    // 40 季上限：FY(今年-8) Q1 ～ FY(今年+1) Q4 = 10 個會計年度
    const r = await $fetch<FilingsResult>(
      `/api/filings?ticker=${encodeURIComponent(t)}&from=${thisYear - 8}Q1&to=${thisYear + 1}Q4`,
    )
    result.value = r
    if (r.isForeignIssuer) {
      status.value = 'foreign'
      if (r.ticker && r.ticker !== props.initialTicker) await navigateTo(`/stock/${r.ticker}`)
      return
    }
    // 由申報紀錄動態生成格子（有申報才可點選）
    const map = new Map<string, Filing>()
    const ys = new Set<number>()
    for (const f of r.filings) {
      if (f.isAmendment) continue
      const p = parseFp(f.fiscalPeriod)
      if (!p) continue
      ys.add(p.fy)
      if (!map.has(key(p.fy, p.q))) map.set(key(p.fy, p.q), f)
    }
    years.value = [...ys].sort()
    cells.value = map
    // 預設選最近 10 季
    const avail = allKeys()
    sel.value = new Set(avail.slice(-10))
    anchor.value = null
    hoverIdx.value = null
    status.value = 'ok'
    // 查成功就導到個股頁。首頁原本是就地載入、網址停在 `/`，
    // 於是「公司簡介／財務報表／13F／內部人買賣」四個分頁完全走不到 ——
    // 那幾頁只掛在 /stock/{ticker} 底下。順便讓網址可以直接分享。
    if (r.ticker && r.ticker !== props.initialTicker) {
      await navigateTo(`/stock/${r.ticker}`)
    }
  } catch (e: any) {
    errMsg.value = e?.data?.message || '查詢失敗，請稍後再試'
    status.value = 'notfound'
  }
}

function allKeys(): string[] {
  const out: string[] = []
  for (const fy of years.value) for (const q of [1, 2, 3, 4]) {
    if (cells.value.has(key(fy, q))) out.push(key(fy, q))
  }
  return out
}

function rangeKeys(a: number, b: number): string[] {
  const out: string[] = []
  for (let i = Math.min(a, b); i <= Math.max(a, b); i++) {
    const fy = years.value[Math.floor(i / 4)]
    const q = (i % 4) + 1
    if (cells.value.has(key(fy, q))) out.push(key(fy, q))
  }
  return out
}

function pick(fy: number, q: number) {
  if (!cells.value.has(key(fy, q))) return
  if (anchor.value === null) {
    sel.value = new Set([key(fy, q)])
    anchor.value = idxOf(fy, q)
  } else {
    sel.value = new Set(rangeKeys(anchor.value, idxOf(fy, q)))
    anchor.value = null
    hoverIdx.value = null
  }
}
function hover(fy: number, q: number) {
  if (anchor.value === null) return
  if (cells.value.has(key(fy, q))) hoverIdx.value = idxOf(fy, q)
}
const previewKeys = computed(() =>
  anchor.value !== null && hoverIdx.value !== null
    ? new Set(rangeKeys(anchor.value, hoverIdx.value))
    : new Set<string>(),
)

const selList = computed(() => {
  const list: { k: string; fy: number; q: number; filing: Filing }[] = []
  for (const fy of years.value) for (const q of [1, 2, 3, 4]) {
    const kk = key(fy, q)
    if (sel.value.has(kk) && cells.value.has(kk)) {
      list.push({ k: kk, fy, q, filing: cells.value.get(kk)! })
    }
  }
  return list
})
const kCount = computed(() => selList.value.filter((r) => r.filing.form.startsWith('10-K')).length)
const rangeText = computed(() => {
  const l = selList.value
  return l.length ? `FY${l[0].fy} Q${l[0].q} → FY${l.at(-1)!.fy} Q${l.at(-1)!.q}` : '—'
})
const fromQS = computed(() => selList.value.length ? `${selList.value[0].fy}Q${selList.value[0].q}` : '')
const toQS = computed(() => selList.value.length ? `${selList.value.at(-1)!.fy}Q${selList.value.at(-1)!.q}` : '')
const excelUrl = computed(() =>
  `/api/financials/excel?ticker=${result.value?.ticker}&from=${fromQS.value}&to=${toQS.value}`)

function downloadCsv() {
  for (const [i, st] of (['IS', 'BS', 'CF'] as const).entries()) {
    setTimeout(() => {
      location.href = `/api/financials/csv?ticker=${result.value?.ticker}&from=${fromQS.value}&to=${toQS.value}&statement=${st}`
    }, i * 400)
  }
}
function openAll() {
  selList.value.forEach((r, i) => setTimeout(() => window.open(r.filing.url, '_blank'), i * 300))
}

// 數據回報：使用者發現某科目數字有誤 → 開 GitHub Issue（對照表複利的來源）
const reportOpen = ref(false)
const reportForm = ref({ concept: '', period: '', note: '' })
const reportState = ref<'idle' | 'sending' | 'done' | 'error'>('idle')
async function submitReport() {
  if (!reportForm.value.concept.trim()) return
  reportState.value = 'sending'
  try {
    await $fetch('/api/report', {
      method: 'POST',
      body: { ticker: result.value?.ticker, ...reportForm.value },
    })
    reportState.value = 'done'
    reportForm.value = { concept: '', period: '', note: '' }
    setTimeout(() => { reportOpen.value = false; reportState.value = 'idle' }, 2000)
  } catch {
    reportState.value = 'error'
  }
}

const zhName = computed(() => (result.value ? ZH[result.value.ticker] ?? '' : ''))
const fyeText = computed(() => {
  const fye = result.value?.fiscalYearEnd
  return fye ? `會計年度 ${Number(fye.slice(0, 2))} 月結算` : ''
})
const foreignFilings = computed(() => result.value?.filings ?? [])

function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && anchor.value !== null) {
    anchor.value = null
    hoverIdx.value = null
  }
}
onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  if (props.initialTicker) search()
})
onUnmounted(() => document.removeEventListener('keydown', onKeydown))

const cellCls = (fy: number, q: number) => {
  const kk = key(fy, q)
  const isK = cells.value.get(kk)?.form.startsWith('10-K')
  const on = sel.value.has(kk) && anchor.value === null
  const isAnchor = anchor.value !== null && anchor.value === idxOf(fy, q)
  return ['cell', isK ? 'k' : '', on ? 'on' : '', isAnchor ? 'anchor' : '',
    !isAnchor && previewKeys.value.has(kk) ? 'prev' : ''].filter(Boolean).join(' ')
}
const edgeOf = (kk: string) => {
  const l = selList.value
  if (anchor.value !== null || l.length < 2) return ''
  if (kk === l[0].k) return '起'
  if (kk === l.at(-1)!.k) return '迄'
  return ''
}
</script>

<template>
  <main class="wrap">
    <section class="query">
      <p class="eyebrow">美股財報批量下載</p>
      <div class="tickerline">
        <div class="tickerfield">
          <label class="fieldtag" for="tk">Ticker　股票代號</label>
          <input
            id="tk" v-model="ticker" maxlength="6" spellcheck="false"
            autocomplete="off" autocapitalize="characters" placeholder="NVDA"
            @input="ticker = ticker.toUpperCase()" @keydown.enter="search" @change="search"
          />
        </div>
        <div v-if="result && (status === 'ok' || status === 'foreign')" class="resolved">
          <div class="name">{{ result.company }}<span v-if="zhName" class="tc">{{ zhName }}</span></div>
          <div class="meta">
            <span>CIK {{ result.cik }}</span>
            <span v-if="result.sic">{{ result.sic }}</span>
            <span v-if="fyeText">{{ fyeText }}</span>
            <span class="flag">{{ result.isForeignIssuer ? '外國發行人' : '美國申報人' }}</span>
          </div>
        </div>
      </div>
    </section>

    <!-- 載入中 -->
    <div v-if="status === 'loading'" class="workspace solo">
      <section class="gridblock">
        <div class="skel"><div /><div /><div /><div /><div /></div>
        <p class="loadline">
          <span>讀取 EDGAR 申報索引<span class="dots" /></span>
          <span style="color: var(--ink-3)">submissions.json</span>
        </p>
      </section>
    </div>

    <!-- 查無代號 -->
    <div v-else-if="status === 'notfound'" class="workspace solo">
      <section class="gridblock">
        <div class="panel">
          <span class="code">查無代號</span>
          <h3>EDGAR 沒有 <span class="t">{{ ticker }}</span> 的申報紀錄</h3>
          <p>{{ errMsg }}</p>
          <div class="chips">
            <NuxtLink v-for="[t, n] in POPULAR" :key="t" class="chip" :to="`/stock/${t}`">
              <span class="t">{{ t }}</span><span class="n">{{ n }}</span>
            </NuxtLink>
          </div>
          <p class="also">或直接以公司名稱查詢
            <a class="out" href="https://efts.sec.gov/LATEST/search-index?q=" target="_blank" rel="noopener">EDGAR 全文檢索 ↗</a>
          </p>
        </div>
      </section>
    </div>

    <!-- 格式錯誤 -->
    <div v-else-if="status === 'badformat'" class="workspace solo">
      <section class="gridblock">
        <div class="panel">
          <span class="code">格式錯誤</span>
          <h3>代號僅能包含英文字母與點號</h3>
          <p>長度 1–5 個字元。多層股權的代號以點號分隔，例如 <span class="mono">BRK.A</span>、
            <span class="mono">BF.B</span>。此處不接受空白、數字與連字號。</p>
          <p class="also">目前輸入：<span class="mono" style="color: var(--ink)">{{ ticker }}</span></p>
        </div>
      </section>
    </div>

    <!-- 外國發行人：無季度矩陣，列 20-F / 6-K -->
    <div v-else-if="status === 'foreign' && result">
      <div class="workspace solo">
        <section class="gridblock">
          <div class="panel">
            <span class="code">無季報</span>
            <h3><span class="t">{{ result.ticker }}</span>{{ zhName ? ` ${zhName}` : '' }} 是外國發行人</h3>
            <p>外國私人發行人（Foreign Private Issuer）向 SEC 申報 20-F 年報，不申報 10-K 與 10-Q，
              因此沒有季度矩陣可選。財務數據以年度為單位（Excel 亦為年度欄）。</p>
            <p class="also">
              <a class="out" :href="`/api/financials/excel?ticker=${result.ticker}&from=${new Date().getFullYear() - 5}Q1&to=${new Date().getFullYear()}Q4`">下載年度 Excel ↗</a>
            </p>
          </div>
        </section>
      </div>
      <section class="results">
        <div class="blockhead">
          <span class="num">§2</span><h2>申報清單</h2><span class="hint">20-F 年報與 6-K 不定期申報</span>
        </div>
        <table class="filings">
          <thead><tr><th class="idx">#</th><th>會計期別</th><th>表單</th><th>期末日</th><th>申報日</th><th>Accession No.</th><th /></tr></thead>
          <tbody>
            <tr v-for="(f, i) in foreignFilings.slice(0, 40)" :key="f.url">
              <td class="idx">{{ String(i + 1).padStart(2, '0') }}</td>
              <td class="per">{{ f.fiscalPeriod || '—' }}</td>
              <td class="form"><span :class="{ k: f.form.startsWith('20-F') }">{{ f.form }}</span></td>
              <td class="date end">{{ f.reportDate || '—' }}</td>
              <td class="date filed">{{ f.filingDate }}</td>
              <td class="acc">{{ f.accessionNumber }}</td>
              <td class="dl"><a :href="f.url" target="_blank" rel="noopener">原始檔 ↗</a></td>
            </tr>
          </tbody>
        </table>
      </section>
    </div>

    <!-- 正常 / 首頁待輸入 -->
    <template v-else>
      <div class="workspace">
        <section class="gridblock">
          <div class="blockhead">
            <span class="num">§1</span>
            <h2>選擇季度</h2>
            <span class="hint">{{ anchor !== null ? '再點一格，選到該季為止' : '點一格開始、點第二格選到該季' }}</span>
            <span class="count" :class="{ armed: anchor !== null }">
              {{ status === 'idle' ? '未載入' : anchor !== null ? '選取起點已定' : `已選 ${selList.length} 季` }}
            </span>
          </div>

          <!-- 待輸入：空格子 -->
          <div v-if="status === 'idle'" class="qgrid">
            <div class="corner">會計年度</div>
            <div v-for="q in 4" :key="q" class="colhead" :class="{ q4: q === 4 }">Q{{ q }}</div>
            <template v-for="r in 4" :key="r">
              <div class="rowhead">FY────</div>
              <div v-for="q in 4" :key="q" class="cell void">
                <span class="form">—</span><span class="end">—/—</span>
              </div>
            </template>
          </div>

          <!-- 已載入：實際申報格子 -->
          <div v-else class="qgrid" @mouseleave="hoverIdx = null">
            <div class="corner">會計年度</div>
            <div v-for="q in 4" :key="q" class="colhead" :class="{ q4: q === 4 }">Q{{ q }}</div>
            <template v-for="fy in years" :key="fy">
              <div class="rowhead">FY{{ fy }}</div>
              <template v-for="q in 4" :key="q">
                <button
                  v-if="cells.has(key(fy, q))"
                  :class="cellCls(fy, q)"
                  :aria-pressed="sel.has(key(fy, q))"
                  :aria-label="`FY${fy} 第${q}季 ${cells.get(key(fy, q))!.form} 期末 ${cells.get(key(fy, q))!.reportDate}`"
                  @click="pick(fy, q)" @mouseover="hover(fy, q)"
                >
                  <span v-if="edgeOf(key(fy, q))" class="edge">{{ edgeOf(key(fy, q)) }}</span>
                  <span class="form">{{ cells.get(key(fy, q))!.form }}</span>
                  <span class="end">{{ cells.get(key(fy, q))!.reportDate.slice(5).replace('-', '/') }}</span>
                </button>
                <div v-else class="cell void" aria-disabled="true">
                  <span class="form">—</span><span class="end">未申報</span>
                </div>
              </template>
            </template>
          </div>

          <div class="legend">
            <span><i class="q" />10-Q 季報</span>
            <span><i class="k" />10-K 年報（Q4 不申報季報）</span>
            <span><i class="v" />尚未申報</span>
            <span><i class="s" />已選取</span>
          </div>

          <p class="note">
            <span class="tag">推算值</span>
            <span>美股第四季不申報 10-Q，該季數字由年報減去前三季推算。Excel 中此類儲存格以同一組橘色標記註明。</span>
          </p>
        </section>

        <aside class="sidecol">
          <template v-if="status === 'ok'">
            <h2><span class="num">§2</span>選取摘要</h2>
            <dl class="sumlist">
              <div class="sumrow"><dt>期間</dt><dd>{{ rangeText }}</dd></div>
              <div class="sumrow"><dt>季數</dt><dd>{{ selList.length }} 季</dd></div>
              <div class="sumrow"><dt>10-Q</dt><dd>{{ selList.length - kCount }} 份</dd></div>
              <div class="sumrow"><dt>10-K</dt><dd class="sig">{{ kCount }} 份</dd></div>
              <div class="sumrow total"><dt>檔案總數</dt><dd>{{ selList.length }}</dd></div>
            </dl>
            <div class="actions">
              <a class="btn" :href="excelUrl">下載 Excel<small>6 分頁 · 含圖表與公式</small></a>
              <button class="btn ghost" @click="downloadCsv">下載 CSV<small>三大報表各一檔</small></button>
              <button class="btn ghost" @click="openAll">開啟全部原始財報<small>直連 sec.gov</small></button>
            </div>
            <button class="reportlink" @click="reportOpen = !reportOpen">
              數據有誤？一鍵回報 →
            </button>
            <div v-if="reportOpen" class="reportbox">
              <template v-if="reportState === 'done'">
                <p class="reportok">已收到回報，謝謝！我們會盡快修正對照表。</p>
              </template>
              <template v-else>
                <label>科目（如 EPS、營收、庫藏股）
                  <input v-model="reportForm.concept" placeholder="哪個科目數字怪" />
                </label>
                <label>期間（選填，如 2023 Q1）
                  <input v-model="reportForm.period" placeholder="哪一季" />
                </label>
                <label>說明（選填）
                  <textarea v-model="reportForm.note" rows="2" placeholder="正確值大概多少 / 怎麼怪" />
                </label>
                <button class="btn" :disabled="reportState === 'sending' || !reportForm.concept.trim()" @click="submitReport">
                  {{ reportState === 'sending' ? '送出中…' : '送出回報' }}
                </button>
                <p v-if="reportState === 'error'" class="reporterr">送出失敗，請稍後再試。</p>
              </template>
            </div>
          </template>
          <template v-else>
            <h2><span class="num">§2</span>熱門標的</h2>
            <div class="hotcol">
              <NuxtLink v-for="[t, n] in POPULAR" :key="t" :to="`/stock/${t}`">
                <span class="t">{{ t }}</span><span class="n">{{ n }}</span>
              </NuxtLink>
            </div>
          </template>
        </aside>
      </div>

      <section v-if="status === 'ok'" class="results">
        <div class="blockhead">
          <span class="num">§3</span><h2>財報清單</h2><span class="hint">依會計期別排序</span>
        </div>
        <table class="filings">
          <thead>
            <tr>
              <th class="idx">#</th><th>會計期別</th><th>表單</th>
              <th>期末日</th><th>申報日</th><th>Accession No.</th><th />
            </tr>
          </thead>
          <tbody>
            <tr v-for="(r, i) in selList" :key="r.k">
              <td class="idx">{{ String(i + 1).padStart(2, '0') }}</td>
              <td class="per">FY{{ r.fy }} Q{{ r.q }}</td>
              <td class="form"><span :class="{ k: r.filing.form.startsWith('10-K') }">{{ r.filing.form }}</span></td>
              <td class="date end">{{ r.filing.reportDate }}</td>
              <td class="date filed">{{ r.filing.filingDate }}</td>
              <td class="acc">{{ r.filing.accessionNumber }}</td>
              <td class="dl"><a :href="r.filing.url" target="_blank" rel="noopener">原始檔 ↗</a></td>
            </tr>
          </tbody>
        </table>
        <p class="tablefoot">
          <span>共 {{ selList.length }} 份</span>
          <span>10-Q {{ selList.length - kCount }}</span>
          <span>10-K {{ kCount }}</span>
          <span>單次上限 40 季</span>
        </p>
      </section>
    </template>

    <p class="disclaim">
      資料來源為美國證券交易委員會（SEC）EDGAR 系統之公開申報文件。BAMHI 與 SEC 無任何隸屬或合作關係。
      財務數據由 XBRL 標籤自動擷取，各公司標籤用法不一，建議對照「原始資料」分頁核對後再用於投資決策。
      缺值以 n/a 表示（SEC 無該標籤不代表數值為零）；Q4 單季數字為推算值（FY − Q1 − Q2 − Q3）。
    </p>

    <slot />
  </main>
</template>
