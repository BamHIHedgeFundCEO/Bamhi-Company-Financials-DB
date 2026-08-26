<script setup lang="ts">
// 首頁 = 同一個工具、ticker 留空（原型 prototype_home_v2）。不做行銷型 landing page。
// 下面再掛一塊「本季機構在做什麼」（13F 全市場榜），走 StockTool 末端的 slot。
useHead({
  title: 'BAMHI 美股財報庫｜美股 SEC 財報批量下載與繁中 Excel 財務資料',
  meta: [
    {
      name: 'description',
      content:
        '輸入美股代號，一鍵取得 10-K、10-Q 財報 SEC 官方直連下載，以及三大報表季度數據的中英對照 Excel（指標全公式、可直接建模）。',
    },
  ],
})
</script>

<template>
  <StockTool>
    <!-- StockTool 末端的 slot：只有首頁掛，個股頁不掛 -->
    <FundFlowBoard />
  </StockTool>
</template>
