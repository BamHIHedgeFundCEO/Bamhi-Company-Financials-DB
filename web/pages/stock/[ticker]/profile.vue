<script setup lang="ts">
/**
 * 公司簡介：公司檔案（submissions.json）＋ 最新 10-K 的 Item 1 / 7 / 1A ＋ 高管名單。
 *
 * 敘述性內容一律**原文直出**，不翻譯也不摘要 —— 翻譯與濃縮是唯一會說謊的一層。
 * 每一段都標明來自哪一個 Item、哪一份申報，並附 EDGAR 原始連結供核對。
 */
const route = useRoute()
const ticker = String(route.params.ticker || '').toUpperCase()

const { data, pending, error } = await useAsyncData(
  `profile-${ticker}`,
  () => $fetch<any>(`/api/profile?ticker=${ticker}`),
  { server: false },
)
// 高管名單走 Form 4（結構化 XML），與內部人買賣分頁同一支 API
const { data: ins, pending: insPending } = await useAsyncData(
  `officers-${ticker}`,
  () => $fetch<any>(`/api/insider?ticker=${ticker}&limit=30`).catch(() => null),
  { server: false },
)

const sec = (id: string) => (data.value?.narrative?.sections ?? []).find((s: any) => s.id === id) ?? null
const business = computed(() => sec('business'))
const mdna = computed(() => sec('mdna'))
const risk = computed(() => sec('risk'))

const open = ref<Record<string, boolean>>({ business: false, mdna: false, risk: false, riskAll: false })

/** 繁中／原文切換。譯文是離線批次的產物（tools/translate_narrative.py），
 *  逐項對齊；**沒翻到的那一條直接顯示英文原文**，不留白。 */
const lang = ref<'zh' | 'en'>('zh')
const hasZh = computed(() => (data.value?.narrative?.sections ?? []).some((s: any) =>
  (s.headingsZh ?? []).some(Boolean) || (s.paragraphsZh ?? []).some(Boolean)))
const zhCount = computed(() => (data.value?.narrative?.sections ?? []).reduce((n: number, s: any) =>
  n + (s.headingsZh ?? []).filter(Boolean).length + (s.paragraphsZh ?? []).filter(Boolean).length, 0))
/** 第 i 項要顯示的文字 */
const pick = (en: string[], zh: string[] | undefined, i: number) =>
  (lang.value === 'zh' ? (zh?.[i] || en[i]) : en[i])
/** 這一項是不是還沒翻譯（在繁中模式下要標出來，讀者才知道自己在看原文） */
const raw = (zh: string[] | undefined, i: number) => lang.value === 'zh' && !zh?.[i]
const PREVIEW = 4

const officers = computed(() => {
  const list = (ins.value?.officers ?? []) as any[]
  return list.filter((o) => o.isOfficer || o.isDirector || o.isTenPercent)
})
const roleOf = (o: any) => o.isOfficer ? '經理人' : o.isDirector ? '董事' : o.isTenPercent ? '10% 股東' : '—'

const fyeText = computed(() => {
  const f = data.value?.fiscalYearEnd
  return f ? `${Number(f.slice(0, 2))} 月 ${Number(f.slice(2))} 日` : '—'
})
const nf = new Intl.NumberFormat('en-US')

const zhNames: Record<string, string> = {
  NVDA: '輝達', AAPL: '蘋果', TSLA: '特斯拉', MSFT: '微軟', AMZN: '亞馬遜',
  GOOGL: 'Alphabet', GOOG: 'Alphabet', META: 'Meta', AMD: '超微', TSM: '台積電',
}
useHead({ title: `${ticker} 公司簡介｜業務概況、經營層討論、主要風險與高管名單` })
</script>

<template>
  <div>
    <TickerTabs
      :ticker="ticker" :company="data?.company" :zh="zhNames[ticker]"
      :meta="data ? [`CIK ${data.cik}`, data.sicDescription, data.exchanges?.join('／')].filter(Boolean) : []"
    />

    <main class="wrap prof">
      <p v-if="pending" class="state">讀取公司檔案與最新年報<span class="dots" /></p>
      <p v-else-if="error" class="state err">
        讀不到 {{ ticker }}：{{ (error as any)?.data?.message || '請確認代號' }}
      </p>

      <template v-else-if="data">
        <div v-if="data.narrative" class="langbar">
          <template v-if="hasZh">
            <span class="lb">內文語言</span>
            <button :class="{ on: lang === 'zh' }" @click="lang = 'zh'">繁體中文</button>
            <button :class="{ on: lang === 'en' }" @click="lang = 'en'">英文原文</button>
            <span class="note">
              譯文 {{ zhCount }} 條，由 {{ data.narrative.translator }} 於
              {{ data.narrative.translatedAt }} 離線批次產生並存檔；
              <b>數字不經翻譯</b>，一律來自 XBRL。有疑義以 EDGAR 原文為準
            </span>
          </template>
          <span v-else class="note">
            這家公司的年報尚未翻譯（繁中譯文是離線批次產物，逐家累積中），以下為英文原文。
          </span>
        </div>

        <!-- 公司檔案 -->
        <section class="cardblock">
          <div class="blockhead"><span class="num">§1</span><h2>公司檔案</h2>
            <span class="hint">SEC submissions．公司自行申報</span></div>
          <dl class="facts">
            <div><dt>法定名稱</dt><dd>{{ data.company }}</dd></div>
            <div><dt>CIK</dt><dd class="mono">{{ data.cik }}</dd></div>
            <div><dt>產業（SIC）</dt><dd>{{ data.sicDescription }}<span class="sub mono">{{ data.sic }}</span></dd></div>
            <div><dt>掛牌</dt><dd>{{ data.exchanges?.join('／') || '—' }}
              <span class="sub mono">{{ data.tickers?.join(' ') }}</span></dd></div>
            <div><dt>註冊地</dt><dd>{{ data.stateOfIncorporation || '—' }}</dd></div>
            <div><dt>申報人規模</dt><dd>{{ data.category || '—' }}</dd></div>
            <div><dt>會計年度結束</dt><dd>{{ fyeText }}</dd></div>
            <div><dt>EIN</dt><dd class="mono">{{ data.ein || '—' }}</dd></div>
            <div class="wide"><dt>營業地址</dt><dd>{{ data.address || '—' }}
              <span class="sub mono">{{ data.phone }}</span></dd></div>
            <div v-if="data.formerNames?.length" class="wide">
              <dt>曾用名</dt>
              <dd>
                <span v-for="f in data.formerNames" :key="f.name" class="former">
                  {{ f.name }}<i class="mono">{{ (f.from || '').slice(0, 10) }} → {{ (f.to || '').slice(0, 10) || '至今' }}</i>
                </span>
              </dd>
            </div>
          </dl>
          <p class="tinynote">
            SEC 的 <span class="mono">website</span> 與 <span class="mono">description</span>
            兩個欄位永遠是空的（不是漏抓），所以業務描述一律取自 10-K 的 Item 1。
          </p>
        </section>

        <!-- 業務概況 -->
        <section v-if="business" class="cardblock">
          <div class="blockhead">
            <span class="num">§2</span><h2>業務概況</h2>
            <span class="tagsrc">{{ business.anchor }}</span>
            <a class="edgar" :href="data.narrative.url" target="_blank" rel="noopener">
              {{ data.narrative.form }} · {{ data.narrative.reportDate }} ↗
            </a>
          </div>
          <ul v-if="business.headings.length" class="chips">
            <li v-for="(h, i) in business.headings.slice(0, 14)" :key="h">
              {{ pick(business.headings, business.headingsZh, i) }}
            </li>
          </ul>
          <div class="prose">
            <p v-for="(p, i) in (open.business ? business.paragraphs : business.paragraphs.slice(0, PREVIEW))"
               :key="i" :class="{ en: raw(business.paragraphsZh, i) }">
              {{ pick(business.paragraphs, business.paragraphsZh, i) }}
            </p>
          </div>
          <button v-if="business.paragraphs.length > PREVIEW" class="more"
                  @click="open.business = !open.business">
            {{ open.business ? '收合' : `再讀 ${business.paragraphs.length - PREVIEW} 段` }}
          </button>
          <p class="excerpt">
            節錄自 {{ business.anchor }}（原文約 {{ business.chars.toLocaleString() }} 字元）。
            <a :href="data.narrative.url" target="_blank" rel="noopener">看 EDGAR 完整原文 ↗</a>
          </p>
        </section>

        <!-- 未來發展 = MD&A -->
        <section v-if="mdna" class="cardblock">
          <div class="blockhead">
            <span class="num">§3</span><h2>未來發展</h2>
            <span class="tagsrc">{{ mdna.anchor }}</span>
            <a class="edgar" :href="data.narrative.url" target="_blank" rel="noopener">
              {{ data.narrative.form }} · {{ data.narrative.reportDate }} ↗
            </a>
          </div>
          <p class="caution">
            這一段是經營層自己寫的討論與分析（MD&A），不是財測。
            <b>公司對下一季／下一年的正式財測指引通常發在 8-K，本站目前尚未收錄</b>，
            所以這裡不會出現「預估營收 XX 億」之類的數字。
          </p>
          <ul v-if="mdna.headings.length" class="chips">
            <li v-for="(h, i) in mdna.headings.slice(0, 14)" :key="h">
              {{ pick(mdna.headings, mdna.headingsZh, i) }}
            </li>
          </ul>
          <div class="prose">
            <p v-for="(p, i) in (open.mdna ? mdna.paragraphs : mdna.paragraphs.slice(0, PREVIEW))"
               :key="i" :class="{ en: raw(mdna.paragraphsZh, i) }">
              {{ pick(mdna.paragraphs, mdna.paragraphsZh, i) }}
            </p>
          </div>
          <button v-if="mdna.paragraphs.length > PREVIEW" class="more" @click="open.mdna = !open.mdna">
            {{ open.mdna ? '收合' : `再讀 ${mdna.paragraphs.length - PREVIEW} 段` }}
          </button>
          <p class="excerpt">
            {{ mdna.focus
              ? `節錄自 MD&A 的「${mdna.focus}」小節`
              : '這份 MD&A 沒有可辨識的總覽小節，節錄的是章節開頭幾段' }}（原文約
            {{ mdna.chars.toLocaleString() }} 字元）。
            <a :href="data.narrative.url" target="_blank" rel="noopener">看 EDGAR 完整原文 ↗</a>
          </p>
        </section>

        <!-- 主要風險 -->
        <section v-if="risk" class="cardblock">
          <div class="blockhead">
            <span class="num">§4</span><h2>主要風險</h2>
            <span class="tagsrc">{{ risk.anchor }}</span>
            <a class="edgar" :href="data.narrative.url" target="_blank" rel="noopener">
              {{ data.narrative.form }} · {{ data.narrative.reportDate }} ↗
            </a>
          </div>
          <ol v-if="risk.headings.length" class="risklist">
            <li v-for="(h, i) in (open.riskAll ? risk.headings : risk.headings.slice(0, 12))" :key="h"
                :class="{ en: raw(risk.headingsZh, i) }">
              {{ pick(risk.headings, risk.headingsZh, i) }}
            </li>
          </ol>
          <button v-if="risk.headings.length > 12" class="more" @click="open.riskAll = !open.riskAll">
            {{ open.riskAll ? '收合' : `其餘 ${risk.headings.length - 12} 條風險` }}
          </button>
          <p v-else class="caution">這份 10-K 的風險因子沒有可辨識的粗體小標，只能看全文。</p>
          <div v-if="open.risk" class="prose">
            <p v-for="(p, i) in risk.paragraphs" :key="i" :class="{ en: raw(risk.paragraphsZh, i) }">
              {{ pick(risk.paragraphs, risk.paragraphsZh, i) }}
            </p>
          </div>
          <button v-if="risk.paragraphs.length" class="more" @click="open.risk = !open.risk">
            {{ open.risk ? '收合導言' : '讀這一章的導言' }}
          </button>
          <p class="excerpt">
            上面每一條就是公司自己下的風險標題（原文約 {{ risk.chars.toLocaleString() }} 字元，
            逐條展開通常有幾十頁）。
            <a :href="data.narrative.url" target="_blank" rel="noopener">看 EDGAR 完整原文 ↗</a>
          </p>
        </section>

        <!-- 公司主管 -->
        <section class="cardblock">
          <div class="blockhead">
            <span class="num">§5</span><h2>公司主管與董事</h2>
            <span class="hint">來自 Form 3/4/5 的 <span class="mono">officerTitle</span> 欄位（結構化 XML）</span>
          </div>
          <p v-if="insPending" class="state small">讀取 Form 4<span class="dots" /></p>
          <table v-else-if="officers.length" class="tab">
            <thead><tr><th>姓名</th><th>職稱</th><th>身分</th><th class="r">最近申報後持股</th><th class="r">最近申報日</th></tr></thead>
            <tbody>
              <tr v-for="o in officers" :key="o.ownerCik + o.owner">
                <td>{{ o.owner }}</td>
                <td>{{ o.title || '—' }}</td>
                <td class="role">{{ roleOf(o) }}</td>
                <td class="r mono">{{ o.sharesAfter != null ? nf.format(o.sharesAfter) : 'n/a' }}</td>
                <td class="r mono">{{ o.lastDate || '—' }}</td>
              </tr>
            </tbody>
          </table>
          <p v-else class="caution">最近的 Form 3/4/5 裡沒有可辨識的主管申報。</p>
          <p class="tinynote">
            這份名單來自「最近有申報持股異動的人」，不是完整的經營團隊名冊
            —— 任期內沒有任何股權異動的人不會出現。持股僅計直接持有部位。
          </p>
        </section>

        <!-- 沒抓到的部分 -->
        <section v-if="(data.narrative?.notes?.length || data.notes?.length)" class="cardblock skip">
          <div class="blockhead"><span class="num">§—</span><h2>沒有抓到的章節</h2></div>
          <ul>
            <li v-for="n in [...(data.notes || []), ...(data.narrative?.notes || [])]" :key="n">{{ n }}</li>
          </ul>
        </section>
      </template>

      <p class="disclaim">
        敘述性內容為 SEC 申報文件原文節錄，未翻譯、未摘要、未經任何模型改寫；
        數字一律不從這些文字取得（三大報表數字全部來自 XBRL companyfacts）。
        風險因子與業務描述請以 EDGAR 原始文件為準。
      </p>
    </main>
  </div>
</template>

<style scoped>
.prof { padding-top: 22px; }
.state { font-family: var(--mono); font-size: 13px; color: var(--ink-2); padding: 40px 0; }
.state.small { padding: 10px 0; font-size: 12px; }
.state.err { color: var(--sig); }
.dots::after { content: '…'; animation: d 1.2s steps(4) infinite; }
@keyframes d { 0% { content: '' } 25% { content: '.' } 50% { content: '..' } 75% { content: '...' } }
.cardblock { background: var(--surface); border: 1px solid var(--rule); padding: 18px 20px 20px;
  margin-bottom: 20px; }
.blockhead { display: flex; align-items: baseline; gap: 12px; flex-wrap: wrap; margin-bottom: 14px; }
.blockhead h2 { font-size: 14px; font-weight: 600; }
.blockhead .num { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3); }
.blockhead .hint { font-size: 12.5px; color: var(--ink-3); }
.tagsrc { font-family: var(--mono); font-size: 10px; letter-spacing: .04em; padding: 2px 6px;
  border: 1px solid var(--rule); color: var(--ink-2); }
.edgar { margin-left: auto; font-family: var(--mono); font-size: 11px; color: var(--green);
  text-decoration: none; border-bottom: 1px solid var(--green); }
.facts { display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 0; }
.facts > div { border-top: 1px solid var(--rule-2); padding: 7px 12px 7px 0; }
.facts > div.wide { grid-column: 1 / -1; }
.facts dt { font-size: 11px; color: var(--ink-3); letter-spacing: .04em; }
.facts dd { font-size: 13.5px; }
.facts .sub { color: var(--ink-3); font-size: 11px; margin-left: 8px; }
.former { display: inline-flex; gap: 8px; align-items: baseline; margin-right: 16px; font-size: 13px; }
.former i { font-style: normal; font-size: 10.5px; color: var(--ink-3); }
.chips { list-style: none; display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 12px; }
.chips li { font-size: 11.5px; padding: 2px 8px; background: var(--green-wash); color: var(--ink-2); }
.prose p { font-size: 13.5px; line-height: 1.8; margin-bottom: 10px; color: var(--ink); }
.risklist { padding-left: 1.4em; display: grid; gap: 5px; }
.risklist li { font-size: 13px; line-height: 1.6; }
.more { margin-top: 10px; background: none; border: 0; padding: 0; font-family: inherit;
  font-size: 12.5px; color: var(--green); cursor: pointer; border-bottom: 1px solid var(--green); }
.caution { font-size: 12.5px; color: var(--ink-2); border-left: 2px solid var(--sig);
  padding-left: 10px; margin-bottom: 12px; }
.tab { width: 100%; border-collapse: collapse; font-size: 13px; }
.tab th { text-align: left; font-size: 11px; color: var(--ink-3); font-weight: 500;
  border-bottom: 1px solid var(--ink); padding: 4px 8px 4px 0; }
.tab td { border-top: 1px solid var(--rule-2); padding: 5px 8px 5px 0; }
.tab .r { text-align: right; padding-right: 0; }
.tab .mono { font-family: var(--mono); font-size: 12px; }
.tab .role { color: var(--ink-2); font-size: 12px; }
.langbar { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 16px; }
.langbar .lb { font-family: var(--mono); font-size: 10px; letter-spacing: .16em;
  text-transform: uppercase; color: var(--ink-3); }
.langbar button { font-size: 12px; padding: 3px 10px; cursor: pointer; background: var(--surface);
  border: 1px solid var(--rule); color: var(--ink-2); margin-left: -1px; font-family: inherit; }
.langbar button.on { background: var(--ink); color: var(--paper); border-color: var(--ink); }
.langbar .note { font-size: 11.5px; color: var(--ink-3); flex: 1 1 320px; line-height: 1.6; }
.prose p.en, .risklist li.en { color: var(--ink-2); }
.prose p.en::after, .risklist li.en::after { content: '原文'; font-size: 9.5px; color: var(--sig);
  border: 1px solid var(--sig); padding: 0 3px; margin-left: 6px; vertical-align: 2px; }
.excerpt { margin-top: 12px; font-size: 11.5px; color: var(--ink-3); line-height: 1.7; }
.excerpt a { color: var(--green); text-decoration: none; border-bottom: 1px solid var(--green); }
.tinynote { margin-top: 12px; font-size: 11.5px; color: var(--ink-3); line-height: 1.7; }
.skip ul { list-style: none; display: grid; gap: 6px; font-size: 12.5px; color: var(--ink-2); }
.disclaim { font-size: 11.5px; color: var(--ink-3); line-height: 1.7; }
</style>
