# 分階段執行計畫

> **這份檔案是階段之間的唯一交接文件。**
> 每做完一個階段就可以清掉對話（`/clear`），下一次從這份檔案接手，不需要回溯歷史訊息。
> 策略層面的判斷原則見 `ROADMAP.md`；這份只講「做什麼、資料從哪來、怎麼驗收」。

---

## 產品終局

使用者輸入代碼 →

1. **先落地在一個摘要頁**（網頁直接看，快速判斷業務）
2. 想深入 → **下載 Excel**（現有的 8 分頁活頁簿）

摘要頁的原則：**只放好抓又重要的資料，不會出現 n/a。**
達不到這個標準的欄位不進摘要頁，留在 Excel 裡誠實寫 n/a。

參考對象：`sec-insider.up.railway.app`（概覽 / 損益表及現金流 / 營收細分 / 對沖基金動向）。

---

## 已驗證的事實（2026-08-20 實測，不要再重驗）

### yfinance 可用，且參考站的股價區塊就是它做的

```
period=1y, interval=1d, auto_adjust=True, 0.5 秒抓完三檔
AAPL   225.18 -> 316.83   +40.7%
^GSPC 6395.78 -> 7707.98  +20.5%   → 差 +20.2pp（參考站寫 vs 標普 +19pp）
^NDX  23249.6 -> 29426.0  +26.6%   → 差 +14.1pp（參考站寫 vs 納指100 +14pp）✓ 完全命中
```

本機 yfinance 1.2.0 正常。**風險在 Vercel：Yahoo 常擋機房 IP。**
→ 價格來源必須寫成可換源介面，備案 `stooq.com` CSV（免金鑰、機房 IP 可用）。

### 公司檔案：`submissions.json` 免費附送，零額外請求

有值：`name` / `tickers` / `exchanges` / `sic` / `sicDescription` / `entityType` /
`category`(申報人規模) / `stateOfIncorporation` / `fiscalYearEnd` / `ein` / `phone` /
`addresses` / `formerNames`（含改名歷史與生效日）

**永遠是空字串，別指望：`website`、`description`。**
→ 業務散文只能來自 Item 1 Business 的 HTML（屬 D 階段）。

### Form 4 是純 XML，品質最好的一塊，且一份資料兩用

`https://www.sec.gov/Archives/edgar/data/{cik}/{accession_nodash}/form4.xml`
（`primaryDocument` 欄位會寫成 `xslF345X06/form4.xml`，**去掉 `xslF345X06/` 前綴**才是原始 XML；
帶前綴的是 SEC 的 XSL 轉譯 HTML 版）

實測 AAPL `0001140361-26-032884` 的欄位：

```
rptOwnerName    = Newstead Jennifer
isOfficer       = true
officerTitle    = SVP, GC and Secretary
transactionCode = S      transactionAcquiredDisposedCode = D
transactionShares = 1439   transactionPricePerShare = 307.75
sharesOwnedFollowingTransaction = 40107
aff10b5One      = true     ← 10b5-1 預設交易計畫，「結構化欄位」不是註腳
```

AAPL 光是 `filings.recent` 裡就有 587 筆 Form 4。

**判讀規則（必須內建，否則做出來會誤導）：**
多數 Form 4 是 `A`（股權獎勵）與 `M`→`S`（行權後依 10b5-1 自動賣出）——**那不是看空信號**。
真訊號是 `P`（公開市場買入）與 `aff10b5One=false` 的 `S`。
UI 必須把兩類分開，不可混算成「本季內部人賣出 $X」。

**副產品：高管名單不必另做。** `officerTitle` 就是高管名冊，還自帶持股與變動。
（原本想抓的「歷史創始人」不在 SEC，已改成高管名單。）

### companyfacts 不含任何自訂命名空間

實測 AAPL `['dei','us-gaap']`、TSLA/BRK.A `['dei','ffd','us-gaap']`、NVDA、JPM 同。
→ 「用 companyfacts 建自訂標籤對照表」不是難，是**不可能**。
自訂標籤只能從 XBRL instance 或 DERA Financial Statement Data Sets 取得。

### 全公司標籤盤點只能靠 DERA Financial Statement Data Sets

`https://www.sec.gov/files/dera/data/financial-statement-data-sets/2025q3.zip` = 127,842,298 B
內含 `sub.txt`(6,541 申報 / 5,909 CIK)、`num.txt`(542MB)、`pre.txt`、`tag.txt`(83,782 標籤：標準 8,277 / 自訂 75,505)

**陷阱：`tag.txt` 的 `version` 欄位對自訂標籤而言，是「定義該標籤的申報書號」，
前綴是申報代理商的號碼、不是公司 CIK。**用前綴比對算每家公司的自訂標籤會全錯
（實測 NVDA 會算成 1、TSLA 會算成 0）。
正確做法是三表 join：`num.adsh` → `sub.cik`，再用 `tag.custom='1'` 過濾。
正確結果：JPM 20 / TSLA 18 / AAPL 3 / NVDA 1，**且沒有一個是營收標籤**
（SEC DQC 規則會逼申報人在主要報表上用標準標籤）。

---

## LLM 決策：不用 LLM，也不需要

原本以為 C/D 階段（CAMs / MD&A / 業務概況 / 指引展望）非 LLM 不可。不是。

**這四項的原文本來就是人寫的散文，直接原文直出 + 錨點 + EDGAR 連結即可。**
LLM 唯一真正的用途是「翻成中文並濃縮」，那是加值層不是必要層，而且它正好是
唯一會違反「資料一定是正確」的一層。

**定案：**

- 主線一律 **零 LLM、零 API 成本**：抽出原文段落，附 `Item 1A` 之類的來源標籤與 EDGAR 連結
  （參考站那個小標籤做法是對的，抄它）
- 若日後真的要中文摘要 → **離線批次跑在自己機器上**（本機模型或手動），產物存成 JSON
  提交進 repo / Blob，執行期零成本、零延遲、可人工覆核
- **數字層永遠不經過 LLM。** 這條不可協商

---

## 階段表

| # | 項目 | 狀態 | 破規則? | LLM? |
|---|---|---|---|---|
| 0 | FSDS 覆蓋率盤點工具 | **完成** `tools/fsds_coverage.py` | 否 | 否 |
| A | 摘要頁：損益 / 現金流 / 營收細分 | 待做 | 否 | 否 |
| A+ | 公司檔案 + 股價 vs 指數 | 待做 | 否 | 否 |
| B | 13F 對沖基金動向 | 待做 | 否 | 否 |
| B+ | Form 4 內部人買賣 + 高管名單 | 待做 | 否 | 否 |
| C | CAMs | 待做 | **是** | 否 |
| D | MD&A + 指引展望 + 業務概況散文 | 待做 | **是** | 否 |
| E | 台股 | 待做 | — | — |

0 / A / A+ / B / B+ **不用改規則、不用 LLM、數字可驗**，五項蓋掉參考站除了
「未來發展」「主要風險」以外的每一塊。

---

### 階段 0 — FSDS 覆蓋率盤點工具

**為什麼排第一：** 它的產出直接決定階段 A 摘要頁能放哪些欄位。
「不會有 n/a」不能用猜的，要有全市場密度數據支撐。

**做什麼：** `tools/fsds_coverage.py`
吃一包 FSDS 季度 zip，離線盤點 `config/xbrl_zh_map.json` 在全部 5,909 家上的覆蓋率。
**per-company 零 SEC 請求。**

**要回答的問題：**
1. 每個科目的全市場命中率（有多少家真的抓得到）
2. 依 SIC 產業分組的命中率 → 哪個科目在哪個產業整片抓不到
3. 抓不到的那些公司實際用了什麼標籤（候選補進 alias 清單）
4. 哪些科目密度夠高，可以進摘要頁而保證不出現 n/a

**必須避開的坑：**
- `version` 前綴比對 → 錯，要三表 join（見上）
- 只看 `num.txt` 有沒有該 tag 是不夠的：管線還有維度過濾與期間長度過濾，
  掃描要模擬管線的 fallback 層級，否則假陰性滿天飛（`coverage-sweep-false-negatives`）
- `num.txt` 542MB → 串流讀，不要整包進記憶體

**驗收：** 產出一份分產業的缺口報告；抽三個報告說「抓不到」的公司，
用 `tools/coverage.py` 打真 SEC 驗證確實抓不到。

#### 已完成（2026-08-20，commit `ea74e97`）

`python tools/fsds_coverage.py 2025q3.zip`（可餵多包合併）

**產出的第一個修法：**現金淨變動補上 `…PeriodIncreaseDecreaseExcludingExchangeRateEffect`
→ 全市場命中率 **76.5% → 98.7%**，map v1.5 → v1.6。

**驗證缺口的正確問法（踩過才知道）：**
- 問「這家歷史上用過我們收的標籤嗎」→ 假警報率 **87%**
- 問「這家在**那幾期**用了嗎」→ 假警報率 **3%**

網站呈現的是特定期間，所以缺口的單位是「**期**」不是「家」。
比對時要用 FSDS 的 `ddate` 對 companyfacts 的 `end`。

**通用摘要頁（所有產業都 ≥90%）目前 6 個：**
營業活動現金流量、本期淨利、資產總計、負債總計、現金淨變動、籌資活動現金流量

**分產業可上摘要頁的欄位數：**其他製造業 20 / 生技製藥 19 / 其他服務業 17 /
銀行與信貸 15 / 控股與投資機構 8

→ **摘要頁必須分產業樣板**，這點已由數據證實，不是猜的。

**低分 ≠ map 壞掉。**生技製藥「非利息收入」0%、軟體業「存貨」低，都是正確的
——那種格子該顯示「—」（不適用）不是 n/a。工具的第二部分才是要動手修的清單。

**尚未處理的候選（第二部分還有）：** `AccountsPayableAndAccruedLiabilitiesCurrentAndNoncurrent`
(309 家)、`DeferredRevenue` / `DeferredRevenueNoncurrent`(96 家)。
`CommonStockSharesIssued` 雖然分數最高**但絕對不能加**——SharesIssued 含庫藏股，
會讓市值高估。

#### 衍生成果：三大報表的「—」（commit `40ca8b1`）

覆蓋率盤點的第一個直接應用。`config/concept_applicability.json`（21 個 SIC 產業組）
由 `fsds_coverage.py --applicability` 產生，執行期 `web/server/utils/applicability.ts`
判斷缺值該寫 `n/a` 還是 `—`。SIC 由 `resolveCompany` 從 submissions 帶出，零額外 SEC 請求。

| | 舊 n/a 率 | 新 n/a 率 |
|---|---|---|
| AAPL | 6.4% | **4.7%** |
| REGN | 8.9% | **7.0%** |
| JPM | 19.0% | **8.0%** |
| BRK.A | 23.8% | **18.1%** |

四家的「有值」格數逐字不變 —— 沒有任何真數字被蓋掉。

**已知限制：**BRK.A 降幅小是 SIC 粒度的問題。它是 6331（保險）但實際像控股公司、
用未分類資產負債表；同組多數小型保險公司有分流動／非流動，所以「流動資產合計」
在保險組沒低於門檻。要再進一步得做**逐家判斷**（用 IDF 計分器看該公司報表上
有沒有語意相當的標籤），成本高很多，暫不做。

---

### 階段 A — 摘要頁

資料**今天全都在** `/api/financials` + `/api/segments`，這是純前端 + 一個 route。

**「簡單的不會有 n/a」要按產業修正：**對非金融業成立，對銀行 / REIT 不成立
（毛利率、營業成本這類概念它們根本不報）。
→ **分產業樣板**，不是一套欄位打天下。欄位清單由階段 0 的密度數據決定。

**UI 尚未討論定案 —— 開工前要先跟使用者對過版面。**

---

### 階段 A+ — 公司檔案 + 股價

- 公司檔案：`submissions.json`，零額外請求（見上方欄位清單）
- 股價：yfinance，`ticker` vs `^GSPC` vs `^NDX`，近一年報酬與超額報酬 pp
- **價格來源寫成可換源介面**，Vercel 上先驗證 Yahoo 有沒有被擋，被擋就切 stooq

---

### 階段 B — 13F 對沖基金動向

13F-HR information table 是 XML，無需解 HTML、無需 LLM、數字可驗。
需要準備追蹤基金的 CIK 名冊（參考站追 36 家）。

**呈現時必須標明兩件事，否則會誤導：**13F 有 45 天延遲；只揭露多頭部位。

---

### 階段 B+ — Form 4 內部人買賣 + 高管名單

見上方「已驗證的事實」。`P` vs `10b5-1 的 S` 分流是這階段的核心正確性要求。

---

### 階段 C / D — 需要先改 CLAUDE.md

使用者已同意改規則，但**改動草案要先給他確認，不可偷改**。涉及：

- 硬規則 #3「三大報表數字一律走 companyfacts，不解析 10-K/10-Q HTML」
  → 要開一個新例外：**敘述性段落**（CAMs / MD&A / Item 1 / Item 1A）可解 HTML，
  但**數字一律不得從 HTML 取**
- 硬規則 #5「不做 job queue / worker」
  → 8-K 指引要事件驅動輪詢。注意 **Vercel Hobby 的 cron 最短是一天一次，不是一分鐘**

**CAMs 抽取的已知坑（外部建議沒講到的）：**
1. `Critical Audit Matter` 這串字在報告裡至少出現三次：定義樣板段
   （"Critical audit matters are matters arising from the current period audit..."）、
   實際 CAM 標題、否定句（"We determined that there are no critical audit matters"）。
   全文搜第一個匹配會撈到定義段
2. 判 SRC/EGC 豁免**不能看匹配次數是否為 0** —— 豁免的公司也會印定義樣板。要看否定句
3. 一份 10-K 常有**兩份審計報告**（財報 + 內控 ICFR），CAMs 只在財報那份；
   還可能有前任會計師報告。「向後找簽名」會跨錯報告
4. 終點錨點 `We have served as the Company's auditor since` 是真的
   （PCAOB AS 3101 強制揭露審計任期），可用

**MD&A：**先切 `PART I` / `PART II`，再在 PART II 內找 Item 7，比單純錨點距離穩。

**8-K 指引：**`index.json` 真的存在，但 `Description` 欄位是申報人自填，
**大量為空或只寫 "EX-99.1"**，靠 Description 比對會失敗。開工前先抓 50 份統計空值率。

---

### 階段 E — 台股

**不是延伸，是另一個專案。** TWSE / 公開資訊觀測站有 XBRL（IFRS 分類）但沒有 SEC 那種
穩定免費 API，MOPS 是 form-post HTML，中文科目對照要另做一套。
唯一能共用的是**輸出層**（Excel / 摘要頁），資料層整個另建。

---

## 待討論

- [ ] 摘要頁 UI 版面（使用者指定要另外討論）
- [ ] 13F 追蹤基金名冊要收哪幾家
- [ ] `axis_dropped` 的 7 家（WMT 部分交叉表型態）：留白 vs `verified=false`
- [ ] `period_hole`（54 個訊號）：需要兩種報導結構在同一期共存才解得掉
- [ ] `config/segment_axes.json` 小修：BW 地區成員 `ID` / `PH` → 印尼 / 菲律賓
