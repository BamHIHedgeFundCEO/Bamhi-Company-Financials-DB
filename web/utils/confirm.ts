/**
 * 確認的轉折：互相印證的訊號（`config/signals.json` 的 `pairs_with`，視為無向）同時亮同一個方向，
 * 兩格以上連成一組才算一個轉折。單一格常常只是期末時點造成的，不算。
 *
 * 用連通分量而不是兩兩配對：存貨成長差、存貨天數、現金含量三格互相連著，
 * 三格同時變差是「一個」存貨警訊，不是三個。
 * 只看 warn／good；持平、背景（info）、無資料不參與 —— 背景型訊號本來就不判好壞。
 */
export interface ConfirmInput { id: string; state: string; pairsWith: string[] }
export interface ConfirmGroup { state: 'warn' | 'good'; ids: string[] }

export function confirmGroups(cards: ConfirmInput[]): ConfirmGroup[] {
  const byId = new Map(cards.map((c) => [c.id, c]))
  const adj = new Map<string, Set<string>>()
  const link = (a: string, b: string) => {
    if (!adj.has(a)) adj.set(a, new Set())
    adj.get(a)!.add(b)
  }
  for (const c of cards) {
    for (const p of c.pairsWith) {
      if (!byId.has(p)) continue
      link(c.id, p)
      link(p, c.id)
    }
  }
  const out: ConfirmGroup[] = []
  for (const state of ['warn', 'good'] as const) {
    const seen = new Set<string>()
    // 依卡片原本的順序走，組內順序與頁面上的順序一致
    for (const c of cards) {
      if (c.state !== state || seen.has(c.id)) continue
      const ids: string[] = []
      const stack = [c.id]
      seen.add(c.id)
      while (stack.length) {
        const id = stack.pop()!
        ids.push(id)
        for (const n of adj.get(id) ?? []) {
          if (seen.has(n) || byId.get(n)?.state !== state) continue
          seen.add(n)
          stack.push(n)
        }
      }
      if (ids.length >= 2) {
        const order = new Map(cards.map((x, i) => [x.id, i]))
        out.push({ state, ids: ids.sort((a, b) => order.get(a)! - order.get(b)!) })
      }
    }
  }
  return out
}
