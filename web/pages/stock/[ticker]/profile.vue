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

/**
 * 逐年對比走自己的端點，而且**點了才載**。
 *
 * 它要多解析一份去年的年報（一次 SEC 請求，之後永久快取）。硬規則是
 * 「敘述性段落只抓最新一份年報」，所以這份額外成本不該加在每個打開公司簡介的人身上
 * —— 想看對比的人才付。兩份年報也刻意不在同一個請求裡解析（見 narrative-diff.get.ts）。
 */
const diff = ref<any>(null)
const diffPending = ref(false)
const diffError = ref<string | null>(null)
async function loadDiff() {
  if (diff.value || diffPending.value) return
  diffPending.value = true
  diffError.value = null
  try {
    diff.value = await $fetch<any>(`/api/narrative-diff?ticker=${ticker}`)
  } catch (e: any) {
    diffError.value = e?.data?.message || e?.message || '讀取失敗'
  } finally {
    diffPending.value = false
  }
}

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
/** 後端的說明文字只用一種記號：**粗體**。除此之外一律當純文字（先跳脫再換記號） */
const mdBold = (s: string) => s
  .replace(/[&<>]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;' }[c] as string))
  .replace(/\*\*(.+?)\*\*/g, '<b>$1</b>')

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
        <p v-if="(data.narrative?.sections || []).some((s: any) => s.viaTitle)" class="caution">
          這份年報採用「交叉索引式」排版：正文沒有 <span class="mono">Item 1A</span> 這種編號錨點，
          最前面只放一張「項次 → 頁碼」對照表。標示<b>「以標題定位」</b>的章節是以標題比對找到、
          並通過內容檢查後才採用的，請以 EDGAR 原文為準。
        </p>

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
            <span v-if="business.viaTitle" class="tagsrc alt" title="這份年報正文沒有 Item 編號錨點">
              以標題定位
            </span>
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
            <span v-if="mdna.viaTitle" class="tagsrc alt" title="這份年報正文沒有 Item 編號錨點">
              以標題定位
            </span>
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
            <span v-if="risk.viaTitle" class="tagsrc alt" title="這份年報正文沒有 Item 編號錨點">
              以標題定位
            </span>
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

        <!-- 與去年年報的差異 -->
        <section v-if="data.narrative" class="cardblock">
          <div class="blockhead">
            <span class="num">§5</span><h2>與去年年報的差異</h2>
            <span class="hint">比的是小標，不是全文</span>
          </div>

          <p v-if="!diff && !diffPending" class="excerpt">
            把今年這份年報的小標，跟去年那份逐條配對：哪幾條是新的、哪幾條不見了、
            哪幾條改了字。需要多讀一份去年的年報（一次 SEC 請求，之後永久快取），
            所以點了才載。
            <button class="more" @click="loadDiff">載入逐年對比</button>
          </p>
          <p v-if="diffPending" class="state small">讀取去年的年報<span class="dots" /></p>
          <p v-if="diffError" class="caution">{{ diffError }}</p>

          <template v-if="diff">
            <p v-if="diff.previous" class="diffhead">
              <a :href="diff.currentUrl" target="_blank" rel="noopener">
                今年 {{ diff.current.form }} · {{ diff.current.reportDate }} ↗</a>
              <span class="vs">vs</span>
              <a :href="diff.previousUrl" target="_blank" rel="noopener">
                去年 {{ diff.previous.form }} · {{ diff.previous.reportDate }} ↗</a>
            </p>
            <p v-for="(n, i) in diff.notes" :key="i" class="excerpt" v-html="mdBold(n)" />

            <div v-for="s in diff.sections" :key="s.id" class="dsec">
              <h3>{{ s.zh }} <span class="tagsrc">{{ s.anchor }}</span></h3>
              <p class="dcount mono">
                今年 {{ s.headings.curTotal }} 條 · 去年 {{ s.headings.prevTotal }} 條 ·
                沿用 {{ s.headings.kept }} · 改寫 {{ s.headings.reworded.length }} ·
                新增 {{ s.headings.added.length }} · 刪除 {{ s.headings.removed.length }}
              </p>
              <p v-for="(n, i) in s.notes" :key="i" class="caution" v-html="mdBold(n)" />

              <ul v-if="s.headings.added.length" class="dlist">
                <li class="dlabel">今年新增</li>
                <li v-for="a in s.headings.added" :key="a.text" class="add">
                  {{ lang === 'zh' && a.zh ? a.zh : a.text }}
                </li>
              </ul>
              <ul v-if="s.headings.removed.length" class="dlist">
                <li class="dlabel">去年有、今年沒有</li>
                <li v-for="a in s.headings.removed" :key="a.text" class="del">{{ a.text }}</li>
              </ul>
              <ul v-if="s.headings.reworded.length" class="dlist">
                <li class="dlabel">改寫（相似度由低到高）</li>
                <li v-for="a in s.headings.reworded" :key="a.to" class="mod">
                  <b>今</b> {{ a.to }}
                  <br><b>去</b> <span class="old">{{ a.from }}</span>
                  <i class="mono sim">{{ Math.round(a.sim * 100) }}%</i>
                </li>
              </ul>

              <template v-if="s.paragraphs?.sameFocus">
                <p class="dcount mono">
                  節錄段落：新增 {{ s.paragraphs.added.length }} ·
                  改寫 {{ s.paragraphs.reworded.length }} ·
                  幾乎照抄 {{ s.paragraphs.kept }}
                </p>
                <ul v-if="s.paragraphs.added.length" class="dlist">
                  <li class="dlabel">今年這一節新寫的段落</li>
                  <li v-for="a in s.paragraphs.added.slice(0, 6)" :key="a.text" class="add para">
                    {{ lang === 'zh' && a.zh ? a.zh : a.text }}
                  </li>
                </ul>
              </template>
            </div>
          </template>
        </section>

        <!-- 公司主管 -->
        <section class="cardblock">
          <div class="blockhead">
            <span class="num">§6</span><h2>公司主管與董事</h2>
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
/* ── 與去年年報的差異 ──────────────────────────────
   刻意不用 --pos／--neg：那兩個顏色在本站是「轉好／警訊」的語意色，
   而新增一條風險不代表公司變差（可能只是把既有的風險拆成兩條）。
   這一區只說「這裡不一樣」，所以一律用中性的灰階＋左側標記。 */
.diffhead { display: flex; gap: 10px; align-items: center; flex-wrap: wrap;
  font-size: 12.5px; margin: 0 0 10px; }
.diffhead .vs { color: var(--ink-3); font-family: var(--mono); font-size: 11px; }
.dsec { border-top: 1px solid var(--rule-2); padding-top: 12px; margin-top: 14px; }
.dsec h3 { font-size: 14px; margin: 0 0 6px; display: flex; gap: 8px; align-items: center; }
.dcount { font-size: 11.5px; color: var(--ink-3); margin: 0 0 8px; }
.dlist { list-style: none; padding: 0; margin: 0 0 10px; }
.dlist .dlabel { font-size: 11px; letter-spacing: .12em; text-transform: uppercase;
  color: var(--ink-3); font-family: var(--mono); margin-bottom: 4px; }
.dlist li + li { margin-top: 5px; }
.dlist .add, .dlist .del, .dlist .mod { font-size: 12.5px; line-height: 1.7;
  padding: 4px 0 4px 10px; border-left: 2px solid var(--rule); }
.dlist .add { border-left-color: var(--ink); }
.dlist .del { color: var(--ink-3); }
.dlist .mod b { font-family: var(--mono); font-size: 10.5px; color: var(--ink-3);
  margin-right: 4px; }
.dlist .mod .old { color: var(--ink-3); }
.dlist .mod .sim { float: right; font-size: 10.5px; color: var(--ink-3); }
.dlist .para { line-height: 1.85; }
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
.tagsrc.alt { border-color: var(--sig); color: var(--sig); }
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
