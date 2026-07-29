import type { WalletGroup, WalletRecord } from '../shared/types'

/**
 * Mirrors the account rows on the wallet page. Keeping refreshes in this
 * order makes the single loading indicator travel through the same list the
 * user is looking at, instead of following SQLite's storage order.
 */
export function walletDisplayRefreshOrder(wallets: WalletRecord[], groups: WalletGroup[]): WalletRecord[] {
  const activeById = new Map(wallets.filter((wallet) => !wallet.archivedAt).map((wallet) => [wallet.id, wallet]))
  const ordered: WalletRecord[] = []
  const included = new Set<string>()
  const append = (wallet: WalletRecord | undefined) => {
    if (!wallet || included.has(wallet.id)) return
    included.add(wallet.id)
    ordered.push(wallet)
  }

  // Wallet groups are rendered first, in `groups` order. Their account rows
  // use the same `wallets.filter(...)` ordering as WalletView below.
  for (const group of groups) {
    for (const wallet of wallets) {
      if (wallet.groupId === group.id) append(activeById.get(wallet.id))
    }
  }
  // The wallet page renders standalone imported accounts after all groups.
  for (const wallet of wallets) {
    if (!wallet.groupId) append(activeById.get(wallet.id))
  }
  // Keep a just-created or stale group account refreshable even before the
  // group snapshot reaches the client, without disturbing visible ordering.
  for (const wallet of wallets) append(activeById.get(wallet.id))

  return ordered
}
