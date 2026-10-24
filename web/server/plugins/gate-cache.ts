import { defineNitroPlugin } from 'nitropack/runtime'

/**
 * 要登入才看得到的回應改成只給瀏覽器自己快取（見 `middleware/gate.ts`）。
 * 在送出前改，因為各端點的 handler 會在 middleware 之後把 Cache-Control 設成 public。
 */
export default defineNitroPlugin((nitro) => {
  nitro.hooks.hook('beforeResponse', (event) => {
    if (event.context.gated) event.node.res.setHeader('Cache-Control', 'private, max-age=600')
  })
})
