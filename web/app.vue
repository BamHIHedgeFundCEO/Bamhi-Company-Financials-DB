<script setup lang="ts">
/**
 * 本站是股譜的一部分（www.tickifu.com/financials）：頁首、標誌、深淺主題都跟股譜一樣。
 * 標誌幾何抄自股譜 apps/web/src/lib/brand.ts（MARK），改那邊要一起改。
 * 主題：股譜把淺色存在 cookie `tickbase-theme`（同網域讀得到），伺服器端直接輸出 data-theme，
 * 換頁不會閃回深色；cookie 還沒設定的舊使用者由 head 裡的小段程式從 localStorage 補上。
 */
const route = useRoute()
const me = await useMe()
const theme = useCookie<string | null>('tickbase-theme')
const THEME_INIT = `try{if(!document.documentElement.dataset.theme&&localStorage.getItem("tickbase-theme")==="light")document.documentElement.dataset.theme="light"}catch(e){}`

useHead({
  htmlAttrs: { 'data-theme': theme.value === 'light' ? 'light' : undefined },
  script: [{ innerHTML: THEME_INIT, tagPosition: 'head' }],
})

const MARK = {
  path: 'M3.5 25.5 9 19.5 13 23.5 17.5 14 M17.5 14 26.5 7 26 22 Z',
  nodes: [
    { cx: 17.5, cy: 14, r: 2.7 },
    { cx: 26, cy: 22, r: 2.7 },
  ],
  focus: { cx: 26.5, cy: 7, r: 3.3 },
  accent: '#F5A524',
}
</script>

<template>
  <div>
    <header class="masthead">
      <div class="wrap">
        <!-- 標誌回股譜首頁（站外連結，股譜是另一個應用程式） -->
        <a href="/" class="logo" aria-label="股譜 Tickifu 首頁">
          <svg viewBox="0 0 32 32" class="logomark" aria-hidden="true">
            <path :d="MARK.path" fill="none" stroke="currentColor" stroke-width="2.4"
              stroke-linecap="round" stroke-linejoin="round" />
            <circle v-for="n in MARK.nodes" :key="n.cx" v-bind="n" fill="currentColor" />
            <circle v-bind="MARK.focus" :fill="MARK.accent" />
          </svg>
          <span class="wordmark">
            <span class="zh">股譜</span>
            <span class="en">Tic<span :style="{ color: MARK.accent }">kifu</span></span>
          </span>
        </a>
        <NuxtLink to="/" class="section">美股財報</NuxtLink>
        <span class="src">SEC EDGAR · 官方原始資料</span>
        <nav class="tk">
          <a href="/explore">探索標的</a>
          <a v-if="!me.loggedIn" :href="loginHref(route.path)" class="login">免費註冊</a>
        </nav>
      </div>
    </header>
    <NuxtPage />
  </div>
</template>
