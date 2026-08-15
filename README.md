# Bamhi Company Financials DB

給繁體中文使用者的美股財報工具。輸入 ticker 與期間，得到兩種產出：

- **文件路徑**：該期間所有 10-Q / 10-K 的 SEC 官方直連網址清單（伺服器不下載、不儲存）
- **數據路徑**：三大財務報表的季度時間序列，輸出成可直接建模的 Excel（中英對照科目、指標全公式、原生圖表）

兩條路徑後端完全獨立，資料來源皆為 [SEC EDGAR](https://www.sec.gov/) 公開 API。

**線上站台**：https://bamhi-company-financials.vercel.app

## 架構

```
Nuxt 3 SSG (Vercel)  →  /api/*  (Nitro server routes：SEC 抓取、JSON、CSV)
                            ↓ 需要 Excel 時轉呼叫
                     Vercel Python Function (openpyxl，生成 .xlsx)
                            ↓
                     Cloudflare R2（產出快取）→ 回傳 signed URL
```

無資料庫。ticker→CIK 對照表由 SEC `company_tickers.json` 取得，記憶體快取 24h。

## 目錄

| 路徑 | 內容 |
|---|---|
| `config/xbrl_zh_map.json` | XBRL 標籤 → 繁中科目對照 + 31 個衍生指標定義（設定層 1） |
| `config/chart_spec.json` | Excel 各分頁圖表清單（設定層 2） |
| `config/theme.json` | 配色、字體、數字格式（設定層 3，白標用） |
| `web/` | Nuxt 3 + TypeScript 前端與 API（SSG，部署 Vercel） |
| `excel-service/` | Python + openpyxl Excel 生成服務（第二 Vercel project，入口 `api/index.py`） |
| `docs/CLAUDE_CODE_PROMPT.md` | 完整產品規格（唯一真相來源） |
| `docs/prototypes/` | HTML 原型與 BamHI 視覺資產 |

**任何「加一個指標」或「加一張圖」的需求，都必須是改 `config/*.json` 就能完成**，不能需要改程式碼。

## 本地開發

```bash
# 前端 + API
cd web
cp ../.env.example .env
npm install
npm run dev          # http://localhost:3000

# Excel 服務
cd excel-service
pip install -r requirements.txt
uvicorn main:app --reload --port 8080
```

## 部署（Vercel，兩個 project、同一 repo）

| Project | rootDirectory | 部署指令 |
|---|---|---|
| 主站（Nuxt） | `web` | `vercel deploy --prod --local-config web/vercel.json` |
| Excel 服務（Python） | 根層 | `vercel deploy --prod`（用根層 `vercel.json`） |

> ⚠️ **主站一定要帶 `--local-config web/vercel.json`**。根層 `vercel.json` 是給 Excel 服務用的（`@vercel/python` + `includeFiles`）；CLI 從 repo 根層部署時會誤套用它，導致主站整站 404。`web/vercel.json` 覆蓋回 Nuxt 設定。

環境變數：
- 主站需設 `EXCEL_SERVICE_URL`（= Excel 服務的網域），否則 Excel 鈕回 503
- `SEC_USER_AGENT` 可選（程式內有營運者預設值）
- R2 快取為選配（`R2_*`）；未設時 Excel 服務直接串流 .xlsx，無快取

## SEC 合規

- 每個請求帶 `User-Agent: BamHI <營運者email>`（`SEC_USER_AGENT` 環境變數）
- 內建 100ms 最小間隔限速與 429 指數退避（`web/server/utils/rateLimiter.ts`，v2 換 Redis 只改此模組）
- 單一 ticker 查詢對 SEC ≤ 2 次請求，回應皆快取

## 免責聲明

資料來源為美國 SEC EDGAR 公開資料。本站與 SEC 無任何隸屬關係。所有數據僅供參考，不構成投資建議。
