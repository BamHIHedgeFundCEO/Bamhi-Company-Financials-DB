# 如何自己迭代對照表（xbrl_zh_map.json）

**核心觀念：每家公司的 XBRL 標籤用法不同，對照表必然有缺漏。這不是 bug，是這門生意的護城河——對照表越用越準，抄不走。**

你不需要一家一家人工比對。用診斷工具找出缺口，補一個標籤，重新部署即可。整個循環約 5 分鐘。

---

## 迭代循環（4 步）

### 1. 找缺口

單一公司（附候選標籤建議）：
```bash
python tools/coverage.py NVDA --suggest
```

批次掃描找「系統性缺口」（最該優先補的）：
```bash
python tools/coverage.py NVDA AAPL MSFT TSLA AMZN META GOOGL AMD
```
最後會列出「N 家中缺最多的科目」——缺最多的優先補，一次補完惠及很多公司。

大規模掃描（每行一個 ticker 的檔案）：
```bash
python tools/coverage.py --scan sp500.txt
```

### 2. 判斷是「真沒有」還是「標籤沒對到」

工具會替缺的科目列候選標籤：

- **候選清單裡有明顯相符的標籤**（名稱對、有近年數值）→ 標籤沒對到，去第 3 步補。
  範例：Amazon 的「研發費用」缺 → 候選出現 `TechnologyAndContentExpense`（Amazon 把研發叫這名字）。
- **候選全是無關雜訊，或空的** → 這家公司**真的沒有這個項目**（如未配息、無庫藏股、軟體公司無存貨）。n/a 正確，不用補。

### 3. 補標籤

打開 `config/xbrl_zh_map.json`，找到該 `concept`，把新標籤**加進 `tags` 陣列**。

順序 = 優先序，取值時由前往後找到第一個有資料的即停止。所以：
- 最標準、最多公司用的標籤放前面
- 冷門、特定公司的放後面（不會影響其他公司）

範例（替研發費用加 Amazon 的標籤）：
```json
{
  "id": "rnd",
  "zh": "研發費用",
  "tags": [
    "ResearchAndDevelopmentExpense",
    "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost",
    "TechnologyAndContentExpense"        ← 新增這行
  ]
}
```

外國發行人（IFRS）的標籤加在 `tags_ifrs`，規則相同。

改完把 `version` 往上加（如 `0.4` → `0.5`）。版本號變更會讓 R2 舊快取自動失效。

### 4. 重新部署

```bash
# excel-service（Python，讀 config/）
vercel deploy --prod --yes                         # 在 repo 根層

# 主站
vercel deploy --prod --yes --local-config web/vercel.json
```

驗證：
```bash
python tools/coverage.py AMZN --suggest             # 確認該科目不再列在缺少清單
```

---

## 判斷要不要花時間補（優先序）

1. **系統性缺口優先**：批次掃描中缺最多家的科目，補一次惠及最多公司。
2. **大公司優先**：流量大的標的（前 500 大）缺口影響最多使用者。
3. **真沒有的不用補**：候選全雜訊 = 公司本來就沒這項目，補了也沒用。

## 這條護城河怎麼複利

功能可以被抄，`xbrl_zh_map.json` 抄不走——它是幾百個真實使用者踩過坑後長出來的。
搭配 ROADMAP 的「錯誤回報按鈕」：使用者發現某科目取錯 → 一鍵回報 ticker + 科目 →
你用 `coverage.py {ticker} --suggest` 五分鐘定位 → 補標籤 → 重新部署。
愈多人用愈準，愈準愈多人用。

---

## 常見情況速查

| 現象 | 意義 | 動作 |
|---|---|---|
| 候選有相符標籤 | 標籤沒對到 | 加進 tags，重新部署 |
| 候選全雜訊/空 | 公司真的沒此項目 | 不用動，n/a 正確 |
| 同一科目很多家都缺 | 系統性缺口 | 最優先補 |
| 數值差 10/40 倍 | 股票分割（已自動處理）| 若仍異常回報，多半是分割偵測沒抓到，查 `computeSplits` |
| 現金流量表某季 n/a | 該公司該期只申報累計、缺 Q1 | 已自動差分還原；仍缺代表原始資料就沒有 |
