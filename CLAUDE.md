# CLAUDE.md

完整規格見 `docs/CLAUDE_CODE_PROMPT.md`（唯一真相來源）。以下為開發時的硬規則摘要。

## 絕對不要做

1. 不在後端下載財報檔案 — 只回傳 SEC 官方 URL
2. 不做 PDF 轉檔（無 playwright / pdfkit / headless chromium）
3. 三大報表數字一律走 `companyfacts` API，不解析 10-K/10-Q HTML
   　例外：**分部數據**。`companyfacts` 不含維度（dimension），分部數字只在申報的
   　XBRL instance（結構化 XML，不是 HTML）裡，走 `web/server/utils/segments.ts`
4. 不做登入 / 帳號 / 訂閱 / 付費牆
5. 不做 zip 打包 / job queue / Redis / worker（v2 才加）
6. `filings.recent` 只含最近約 1000 筆，更早的要抓 `filings.files` 分頁
7. 不接 Google Sheets API
8. Excel 一律伺服器端生成；**用 Python + openpyxl（excel-service/），不是 exceljs**（exceljs 無法寫原生圖表）

## SEC 合規

- 每個 sec.gov / data.sec.gov 請求帶 `User-Agent: BamHI <營運者email>`（環境變數 `SEC_USER_AGENT`），否則 403
- 限速走 `web/server/utils/rateLimiter.ts` 獨立模組（100ms 最小間隔 + 429 指數退避）；v2 換 Redis 只改此檔實作
- 三大報表：單 ticker 查詢 ≤ 2 次 SEC 請求
- 分部數據：每份申報 2 次（`index.json` 定位 instance + 抓 instance），預設只抓
  2 份年報 = 4 次；一份 10-K 自帶 3 年比較數，所以能蓋到約 4–6 年
- 回應快取：行程內 Map（`secFetch.ts`）+ 持久層 Vercel Blob（`blobCache.ts`）。
  已提交財報不可變 → 永久有效。**只快取解析後的小結果，不快取原始 instance**
  （單檔 0.7–14MB，存原始檔會把 Blob 額度吃光）

## 設定層（規模化判準）

改 `config/xbrl_zh_map.json`（科目/翻譯/指標）、`config/chart_spec.json`（圖表）、`config/theme.json`（配色/格式）、`config/segment_axes.json`（分部軸/成員正規化/中文名）就能完成的需求，禁止改程式碼實現。

## Excel 硬規則（excel-service/）

- 分頁：說明 / 損益表 / 資產負債表 / 現金流量表 / 關鍵指標 / 估值倍數 / 分部數據 / 原始資料
  （估值倍數、分部數據為條件式：無股價或該公司未揭露分部時不產生）
- 分部數據分頁的**列必須動態產生**：ASC 280 規定 CODM 看什麼才揭露什麼，各家
  科目不同（Apple 有分部成本、NVDA 有折舊、銀行給稅前損益），寫死列一定會錯
- 分部的上層匯總成員（如 Apple 的「產品」含 iPhone/Mac/iPad）照列但不進合計，
  否則重複計算；判定靠與合併總額對帳，不是硬編碼
- A 欄中文、B 欄英文、C 欄起季度；凍結窗格 `C2`
- 缺值寫 `n/a`，絕不寫 0（SEC 無標籤 ≠ 零）
- Q4 推算值（FY − Q1 − Q2 − Q3）底色淺橘，說明分頁註明
- 數值存原始美元，格式 `#,##0;[Red](#,##0)`；比率 `0.0%`；倍數 `0.00"x"`；天數 `0"天"`
- 關鍵指標分頁**必須寫 Excel 公式**（IFERROR 包除法），不能寫算好的數值
- 每次從零生成整本活頁簿，圖表程式化建立（不用範本檔——重存會遺失圖表）
- 圖表資料範圍依 `n_quarters` 動態計算，不寫死

## Edge cases（必須全數處理）

1. Q4 無 10-Q（全年 = 3×10-Q + 1×10-K）
2. Q4 單季數字 = FY − Q1 − Q2 − Q3，標註推算
3. `filings.recent` 之外的舊申報走 `filings.files` 分頁
4. 外國發行人（TSM、ASML）無 10-Q → 自動改抓 20-F/6-K 並在 UI 告知
5. 10-K/A、10-Q/A 一併列出並標記「修正版」
6. 會計年度非曆年（NVDA、AAPL）→ 一律以 `reportDate` 判斷季別
7. 同一期間多筆 frame → 取 `filed` 最新
8. ticker 不存在 / 下市 / ETF → 明確錯誤訊息
9. 同 CIK 多 ticker（GOOG/GOOGL）→ 雙向解析

## 上限與文案

- 單次 5 檔 ticker、40 季；超限訊息：「單次比較上限 5 檔，可分批查詢」，不用錯誤代碼

## 驗收

AAPL 5年 = 15×10-Q + 5×10-K；TSM 判外國發行人；NVDA 季別對齊 1 月結算；BRK.A 可解析；AAPL/MSFT/TSLA 營收 fallback 皆取值；單查詢 SEC 請求 ≤ 2。
