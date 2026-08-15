# SEC 財報批量工具 — v1 建置指令

貼進 Claude Code。把 `xbrl_zh_map.json` 一起放進專案根目錄。

---

## 專案目標

一個給繁體中文使用者的美股財報工具。使用者輸入 ticker 與期間，得到兩種產出：

- **文件路徑**：該期間所有 10-Q / 10-K 的 SEC 官方直連網址清單
- **數據路徑**：三大財務報表的季度時間序列，輸出成可直接建模的 Excel（中英對照科目）

這兩條路徑在後端**完全獨立**，不共用資料來源，不互相依賴。

---

## 絕對不要做的事（先讀這段）

1. **不要在後端下載財報檔案**。v1 只回傳 SEC 官方 URL，由使用者瀏覽器自行下載。零儲存、零流量成本。
2. **不要做 PDF 轉檔**。不要引入 playwright / pdfkit / weasyprint / headless chromium。
3. **不要為了取得財務數字而下載或解析 10-K/10-Q 的 HTML**。數字一律走 `companyfacts` API。
4. **不要做登入、帳號、訂閱、付費牆**。
5. **不要做 zip 打包、job queue、Redis、worker**。這些是 v2 在有真實流量後才加。
6. **不要用 `filings.recent` 就當作全部資料**（見 Edge case 3）。
7. **不要接 Google Sheets API**。那需要 OAuth + Drive scope 安全審查 + 登入系統。使用者要 Google Sheet，自行把 .xlsx 或 .csv 拖進 Drive 即可。
8. **不要在瀏覽器端生成 Excel**（SheetJS）。排版能力不足，做不出凍結窗格與數字格式。一律伺服器端 exceljs。

---

## 技術棧

- 前端：**Nuxt 3**（Vue 3 底層）+ TypeScript，部署 Vercel。**必須用 SSG，不可做成純 SPA**（原因見「SEO 與靜態頁生成」）
- 後端 API：Vercel Serverless Functions（Node/TypeScript）— SEC 抓取、JSON 回傳、CSV
- Excel 生成：**Python + openpyxl，獨立部署於 Cloud Run**（原因見「Excel 產出規格」）
- 產出快取：Cloudflare R2
- 無資料庫。ticker→CIK 對照表在 build 時抓下來存成靜態 JSON，或用記憶體快取 + 24h TTL

---

## SEC 合規（不可妥協）

```
User-Agent: BamHI <你的email>
```

- **每一個**對 `sec.gov` / `data.sec.gov` 的請求都必須帶這個 header，否則回 403 並可能封 IP。
- User-Agent 固定填營運者的聯絡方式，**不是**使用者輸入的 email。
- 限速：全站對 SEC 合計 10 req/s。v1 每個查詢只打 1–2 次，正常使用碰不到；但仍要在 fetch wrapper 內建 100ms 最小間隔與 429 指數退避。
- 所有回應快取。已提交的財報不可變，永久有效。

**限速必須抽成獨立模組**，為 v2 留擴充點：

```ts
secFetch(url)  →  內部呼叫  rateLimiter.acquire()
```

v1 的 `rateLimiter` 實作就是行程內的最小間隔（約 20 行）。v2 要改成 Redis token bucket 時，只換掉這個模組的實作，上層程式碼一行都不動。**v1 不要實作 Redis、不要實作 queue。**

---

## 資料流 A：文件清單

1. `https://www.sec.gov/files/company_tickers.json` → 建立 ticker → CIK 對照（CIK 補零至 10 位）
2. `https://data.sec.gov/submissions/CIK{cik10}.json`
3. 從 `filings.recent` 取平行陣列 `form` / `filingDate` / `accessionNumber` / `primaryDocument` / `reportDate`，用 index 對齊組成物件陣列
4. 依 form type 與日期區間過濾
5. 組出直連網址：

```
https://www.sec.gov/Archives/edgar/data/{cik_no_leading_zeros}/{accession_no_dashes}/{primaryDocument}
```

注意：路徑中的 CIK **不補零**，accession number **移除橫線**。

## 資料流 B：財務數據

1. 同上取得 CIK
2. `https://data.sec.gov/api/xbrl/companyfacts/CIK{cik10}.json` — **一次請求拿到該公司歷來全部 XBRL 數值**
3. 讀取 `xbrl_zh_map.json`，對每個 concept 依 `tags` 陣列順序尋找第一個存在的標籤
4. 從 `units.USD`（或 `units.shares` / `units["USD/shares"]`）取值，依 `fp` 與 `start`/`end` 篩出季度
5. 輸出 Excel：列為科目（中英對照兩欄），欄為季度，另附 `derived` 衍生指標分頁

---

## Excel 產出規格

### ⚠️ 技術決定：Excel 生成必須用 Python + openpyxl，不是 exceljs

**exceljs 無法寫入原生 Excel 圖表**（GitHub issue #141，2016 年開至今未實作）。既然需求包含每個分頁都要有圖表，Node 這條路直接排除。

架構因此調整為：

```
Vue (Vercel)  →  /api/*  (Vercel Function，負責 SEC 抓取與 JSON 回傳)
                    ↓  需要 Excel 時轉呼叫
              Cloud Run (Python + openpyxl，生成 .xlsx)
                    ↓
              Cloudflare R2 (快取產出) → 回傳 signed URL
```

**不要試圖用「讀取含圖表的範本檔再填資料」的做法。** openpyxl 與 exceljs 在載入既有檔案後重新儲存時都會遺失圖表與圖片。正確做法是**每次從零生成整本活頁簿**，圖表也程式化建立。

### 分頁結構（6 個）

| # | 分頁 | 內容 |
|---|---|---|
| 1 | 說明 | 公司名、CIK、期間、資料來源、生成時間、對照表版本、免責聲明、指標定義總表 |
| 2 | 損益表 | IS 科目 + 圖表 |
| 3 | 資產負債表 | BS 科目 + 圖表 |
| 4 | 現金流量表 | CF 科目 + 圖表 |
| 5 | 關鍵指標 | 31 個衍生指標，依 group 分區 + 圖表 |
| 6 | 原始資料 | 每個數字對應的 XBRL 標籤、accession number、申報日、單位 |

### 版面規則

- **A 欄中文科目、B 欄英文科目、C 欄起為季度**（季度橫排、科目直排）。凍結窗格設在 `C2`
- **缺值一律寫 `n/a`，絕對不能寫 0**。SEC 無此標籤 ≠ 數值為零
- **Q4 推算值（`FY − Q1 − Q2 − Q3`）儲存格底色標為淺橘**，並在「說明」分頁註明
- 數值存**原始美元**，用千分位格式顯示（`#,##0;[Red](#,##0)`），不要在存檔時除以百萬
- 比率用 `0.0%`，倍數用 `0.00"x"`，天數用 `0"天"`
- **「關鍵指標」分頁必須寫 Excel 公式，不能寫算好的數值**：
  ```
  =IF(損益表!C2=0,"n/a",損益表!C4/損益表!C2)     ← 對
  0.4612                                          ← 錯
  ```
  使用者改一個假設整張表要能重算，這才叫「可建模」。所有除法都要包 `IFERROR` 或分母零值判斷。
- 每個指標列的 D 欄放**滑鼠移入顯示的註解**（openpyxl `Comment`），內容取自 `xbrl_zh_map.json` 的 `desc` 欄位

### 圖表規格

**圖表資料範圍必須由季度數動態計算，不可寫死。** 因為每次都是從零生成，直接依 `n_quarters` 算出範圍即可：

```python
data = Reference(ws, min_col=3, max_col=2 + n_quarters, min_row=row, max_row=row)
cats = Reference(ws, min_col=3, max_col=2 + n_quarters, min_row=1, max_row=1)
```

使用者選 5 季，圖表就只有 5 個點；選 20 季就有 20 個點。

**圖表清單另外抽成 `chart_spec.json`**（與 `xbrl_zh_map.json` 同層），不要寫死在程式碼裡。這是可客製化的關鍵——之後要增減圖表只改 JSON，不動程式：

```json
{
  "損益表": [
    { "type": "bar+line", "title": "營收與毛利率",
      "bars": ["revenue"], "line": ["gross_margin"], "secondary_axis": true },
    { "type": "stacked_bar", "title": "成本費用結構",
      "series": ["cogs", "rnd", "sgna"] },
    { "type": "line", "title": "三率趨勢",
      "series": ["gross_margin", "operating_margin", "net_margin"] }
  ],
  "資產負債表": [
    { "type": "stacked_bar", "title": "資產結構",
      "series": ["cash", "accounts_receivable", "inventory", "ppe_net", "goodwill"] },
    { "type": "bar+line", "title": "淨負債與利息保障倍數",
      "bars": ["net_debt"], "line": ["interest_coverage"], "secondary_axis": true }
  ],
  "現金流量表": [
    { "type": "bar", "title": "三大現金流", "series": ["cfo", "cfi", "cff"] },
    { "type": "bar+line", "title": "自由現金流與 FCF Margin",
      "bars": ["fcf"], "line": ["fcf_margin"], "secondary_axis": true }
  ],
  "關鍵指標": [
    { "type": "line", "title": "營收成長動能", "series": ["revenue_yoy", "revenue_qoq"] },
    { "type": "line", "title": "報酬率", "series": ["roe", "roa", "roic"] },
    { "type": "bar", "title": "現金轉換循環", "series": ["dso", "dio", "dpo", "ccc"] },
    { "type": "line", "title": "盈餘品質", "series": ["ocf_to_net_income"] }
  ]
}
```

**圖表樣式（統一套用，不要用 openpyxl 預設）：**

- 配色固定一組 5–6 色的專業色盤（深藍 / 灰藍 / 橘 / 灰 / 淺藍），寫成常數，不要每張圖各自指定
- 移除圖表格線的粗重外框，橫向格線用淺灰虛線
- 圖例置於下方，不要放右側（會壓縮繪圖區）
- 座標軸標題只在必要時出現；金額軸統一以百萬為顯示單位並在軸標題註明
- 雙軸圖（bar+line）：長條走主軸（金額），折線走次軸（比率）
- 每張圖寬約 12 個欄寬、高約 18 個列高，統一放在資料區右側或下方固定位置

### 可客製化架構（規模化的核心）

三個檔案構成整個產品的「設定層」，程式碼只負責執行：

| 檔案 | 決定什麼 | 客製化情境 |
|---|---|---|
| `xbrl_zh_map.json` | 抓哪些科目、中文怎麼翻、指標怎麼算 | 加新科目、修正標籤、調整指標定義 |
| `chart_spec.json` | 每個分頁畫哪些圖 | 產業別專屬圖表（如銀行股、生技股） |
| `theme.json` | 配色、字體、數字格式 | 白標（white-label）給不同客戶 |

**任何「加一個指標」或「加一張圖」的需求，都必須是改 JSON 就能完成，不能需要改程式碼。** 這是這個專案能否規模化的唯一判準。



## 使用者旅程（驗收時照這個走一遍）

1. 從 Google 搜尋「NVDA 財報下載」進站，直接落在 `/stock/NVDA`，ticker 已預填
2. 選期間 2023 Q1 ～ 2026 Q2（12 季），按查詢，1–2 秒回結果
3. 表格列出 12 份財報：中文季別、表單類型、期末日、申報日、下載鈕。NVDA 為 1 月結算，季別標示須與曆年錯開且正確
4. 三個主要動作：
   - **全部原始財報** → 瀏覽器依序開啟 12 個 SEC 官方連結，直連 sec.gov，伺服器不參與
   - **下載 CSV** → 三大報表各一檔
   - **下載 Excel** → 3 秒內取得 6 分頁 .xlsx
5. 打開 Excel：關鍵指標分頁全是公式，改一個假設整張表重算；滑鼠移入指標名稱顯示該指標的判讀說明
6. 第二位使用者查同樣的 ticker + 期間 → 命中 R2 快取，0.5 秒回檔，Cloud Run 不被喚醒

**產品成立的判準：** 使用者打開 Excel 後拿到的不是一份報表，而是一個可以直接接續建模的檔案。若他還需要重新整理格式才能用，這個產品就失敗了。

---

## 頁面結構（共兩種模板，不是三種）

### 1. 首頁 `/`

**不要做成行銷型 landing page。** 沒有 hero 標語、沒有功能卡片、沒有「為什麼選擇我們」、沒有見證。

首頁就是**同一個工具，ticker 欄位留空**。上方加：

- 一行說明工具做什麼（一句話，不是段落）
- 熱門標的快捷列（NVDA / AAPL / TSLA / MSFT / AMZN / GOOGL / META / AMD / TSM / PLTR），點了直接跳 `/stock/{ticker}`
- 底部：資料來源說明、免責聲明

理由：SEO 策略決定了**多數流量會直接落在 `/stock/{ticker}`，不會經過首頁**。首頁只服務三種人——從 BamHI 導流過來的、直接輸網址的、以及不知道要查哪一檔的。這三種人都要的是「趕快讓我開始查」，不是產品介紹。

### 2. 個股頁 `/stock/{ticker}`

即原型檔的版面。ticker 已預填、公司資訊已解析、季度格子已載入。

**兩個模板共用同一個元件**，差別只在 ticker 是否預填、以及個股頁多出 SEO 用的靜態內容區塊。

---

## 季度格子的資料來源與更新

**格子必須由 `submissions.json` 動態生成，不可寫死年份或季度數。**

- 有申報紀錄 → 可點選，顯示表單類型與期末日
- 無申報紀錄 → 斜線填滿、不可點選
- 新財報申報後，格子**自動多一格**，不需要改任何程式碼

注意判斷依據是**「SEC 有無該筆申報」，不是「日曆走到哪一季」**。公司季末後約 40 天才申報 10-Q、年末後約 60 天才申報 10-K，中間那段空窗期格子應維持不可點選狀態。

靜態頁在 build 時寫入的財報清單，用 GitHub Actions **每週重新生成一次**；使用者實際操作時仍以 client-side 打 API 取得最新狀態，兩者不衝突。

---

**不要做成純 SPA。** SPA 只有一個網址，只能排一個關鍵字，且原始 HTML 是空的。

**改用支援 SSG（靜態生成）的架構：Nuxt 3，或 Vite + 預渲染外掛。**

build 時預先生成美股市值前 500 大公司的靜態頁：

```
/stock/NVDA
/stock/AAPL
/stock/TSLA
...
```

每一頁的 **HTML 原始碼**（不是 JS 渲染後）必須包含：

- `<title>`：`NVDA 輝達 財報下載｜10-K、10-Q 季度財務報表與 Excel 下載`
- `<meta name="description">`：含公司中英文名、可下載的期間範圍
- `<h1>` 與可見文字中出現公司中文名、英文名、ticker
- 最近 8 季財報的清單與連結（build 時抓一次寫死，每週用 GitHub Actions 重新生成）
- 該公司的簡短中文業務描述（可從 submissions JSON 的 SIC 分類對應）
- 結構化資料 `JSON-LD`（`FinancialProduct` 或 `Dataset`）
- 指向 `/stock/{同產業其他 ticker}` 的內部連結

另外需要：`sitemap.xml`（含全部 500 頁）、`robots.txt`、每頁 canonical URL。

**ticker 頁的互動部分（期間選擇、下載）在靜態 HTML 之上以 client-side 掛載**，不影響已渲染的內容。

---

## Edge cases（這些會決定成品能不能用）

1. **Q4 沒有 10-Q**。美股全年只申報 3 份 10-Q + 1 份 10-K。「5 年每季」= 15 份 10-Q + 5 份 10-K，共 20 份。前端不要顯示成 20 份 10-Q。
2. **Q4 單季數字要自行計算**。companyfacts 中 Q4 通常只有全年累計值，`Q4 = FY − Q1 − Q2 − Q3`。Excel 中要標註此欄為推算值。
3. **`filings.recent` 只含最近約 1000 筆**。更早的在 `filings.files` 陣列指向的分頁 JSON。若目標區間的最早日期早於 `filings.recent` 的最舊一筆，必須額外抓分頁。
4. **外國發行人不申報 10-Q**。ASML、TSM 等申報 20-F（年報）與 6-K（不定期）。偵測方式：submissions JSON 的 `entityType` 與歷史 form type。若查無 10-K/10-Q，自動改抓 20-F/6-K 並在 UI 明確告知使用者「此公司為外國發行人，無季報」。
5. **修正申報（10-K/A、10-Q/A）**。預設一併列出並標記為「修正版」，讓使用者自行選擇，不要靜默丟棄。
6. **會計年度非曆年**。NVDA、AAPL 等的 FY 與曆年錯開。一律以 `reportDate`（期末日）為準顯示，不要用 `filingDate` 推算季別。
7. **XBRL 同一數值有多筆 frame**。同一期間可能因後續重編而有多筆記錄，取 `filed` 最新的那筆。
8. **ticker 不存在 / 已下市 / ETF**。給明確錯誤訊息，不要回空陣列了事。
9. **同一 CIK 多 ticker**（如 GOOG/GOOGL）。對照表要能雙向解析。

---

## API 規格

```
GET /api/filings?ticker=AAPL&years=5&forms=10-K,10-Q
→ { company, cik, isForeignIssuer, filings: [{ form, fiscalPeriod, reportDate, filingDate, url, isAmendment }] }

GET /api/financials?ticker=AAPL&years=5
→ { company, cik, periods: [...], lineItems: [{ id, zh, en, statement, values: {...}, sourceTag, isDerived }] }

GET /api/financials/excel?ticker=AAPL&from=2021Q1&to=2026Q2
→ .xlsx 檔（用 exceljs 在記憶體組完直接串流回傳，不落地）

GET /api/financials/csv?ticker=AAPL&from=2021Q1&to=2026Q2&statement=IS
→ .csv 檔（三大報表各一，給要丟進 Google Sheet 或 pandas 的人）
```

**產出快取（v1 就要做，這是唯一的成本控制手段）：**

以 `{ticker}_{from}_{to}_{mapVersion}` 為 key，生成後存進 Cloudflare R2（**不是 S3**，R2 egress 為零）。每次請求先檢查是否已存在，存在就直接回傳 storage URL。這不是快取系統，就是一個檔案存在性判斷。

`mapVersion` 取自 `xbrl_zh_map.json` 的 `version` 欄位——對照表更新時自動失效舊檔。

---

## 前端要求

- 全繁體中文介面
- ticker 輸入支援逗號分隔多檔，**上限 5 檔**（ticker 是請求數的唯一乘數）
- 期間用「起訖季度」選擇器（如 2021 Q1 ～ 2026 Q2），不要只給「幾年」，**上限 40 季（≈10 年）**
- **40 季上限對「原始文件」與「Excel」一致套用**。文件路徑的技術成本其實是零（僅回傳網址），但統一上限讓介面只有一套規則、不需要在按下不同按鈕時跳出不同限制。簡單性優先於壓榨零成本優勢。
- 超過上限時的訊息寫成「單次比較上限 5 檔，可分批查詢」，不要寫成錯誤代碼
- 結果表格每列一份財報，含中文季別標示、表單類型、期末日、下載按鈕
- 提供「全部下載」按鈕：JS 依序觸發下載，每個間隔 300ms
- 明確的 disclaimer：資料來源為美國 SEC EDGAR 公開資料，本站與 SEC 無任何隸屬關係

---

## 驗收標準

- `AAPL` 5 年 → 正確回傳 15 份 10-Q + 5 份 10-K，季別標示符合其九月結算的會計年度
- `TSM` → 正確識別為外國發行人，回傳 20-F 並在 UI 提示無季報
- `NVDA` → 會計年度錯開曆年，季別標示正確
- `BRK.A` → ticker 含特殊字元能正確解析
- Excel 中營收欄位對 AAPL、MSFT、TSLA 三家皆能取到值（三家使用的 XBRL 標籤不同，驗證 fallback 機制有效）
- 全程對 SEC 的請求數：單一 ticker 查詢 ≤ 2 次

---

## 開發順序

1. ticker → CIK 對照 + fetch wrapper（含 User-Agent、rateLimiter 模組、429 退避）
2. 資料流 A 全部走通 + 前端表格
3. **500 檔 ticker 靜態頁生成 + sitemap**（這步不能延後，它決定前端架構）
4. 部署上線，先讓它能用
5. 資料流 B（companyfacts + 對照表 + Excel）→ Cloud Run Python 服務 + chart_spec
6. 之後才考慮：跨公司比較（v2 首選，資料結構已支援）、AI 中文摘要（財報不可變，每份只需生成一次並永久快取）、queue 與全域限流
