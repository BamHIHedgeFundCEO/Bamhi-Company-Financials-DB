/**
 * SEC 限速模組 — v1：行程內最小間隔。
 * v2 要換 Redis token bucket 時只改這個檔案的實作，上層 secFetch 不動。
 */
const MIN_INTERVAL_MS = 100

let lastAcquired = 0
let queue: Promise<void> = Promise.resolve()

export const rateLimiter = {
  acquire(): Promise<void> {
    queue = queue.then(async () => {
      const now = Date.now()
      const wait = lastAcquired + MIN_INTERVAL_MS - now
      if (wait > 0) await new Promise((r) => setTimeout(r, wait))
      lastAcquired = Date.now()
    })
    return queue
  },
}
