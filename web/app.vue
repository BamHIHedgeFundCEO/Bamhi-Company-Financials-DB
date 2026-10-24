<script setup lang="ts">
const route = useRoute()
const me = await useMe()
</script>

<template>
  <div>
    <header class="masthead">
      <div class="wrap">
        <NuxtLink to="/" class="logo" aria-label="BAMHI 美股財報庫">
          <img :src="withBase('/bamhi-wordmark-small.png')" alt="BAMHI" class="wordmark" />
          <span class="mark" aria-hidden="true"><i /><i /><i /><i /></span>
          <span class="name">美股財報庫</span>
        </NuxtLink>
        <span class="src">SEC EDGAR · 官方原始資料</span>
        <!-- 本站是股譜的一部分（www.tickifu.com/financials）：回股譜、登入都在股譜那邊 -->
        <nav class="tk">
          <a href="/">股譜</a>
          <a v-if="!me.loggedIn" :href="loginHref(route.path)" class="login">免費註冊</a>
        </nav>
      </div>
    </header>
    <NuxtPage />
  </div>
</template>
