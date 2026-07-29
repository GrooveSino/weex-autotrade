import { describe, expect, it } from 'vitest'
import type { WalletGroup, WalletRecord } from '../shared/types.js'
import { walletDisplayRefreshOrder } from '../src/wallet-display-order.js'

const wallet = (id: string, groupId: string | null, archivedAt: string | null = null): WalletRecord => ({
  id,
  label: id,
  address: `0x${id}`,
  source: groupId ? 'mnemonic' : 'private_key',
  groupId,
  accountIndex: groupId ? 0 : null,
  accountStatus: groupId ? 'unused' : 'standalone',
  derivationPath: null,
  createdAt: '2026-07-23T00:00:00.000Z',
  balances: [],
  balanceError: null,
  balanceUpdatedAt: null,
  archivedAt,
})

const group = (id: string): WalletGroup => ({
  id,
  label: id,
  source: 'mnemonic',
  derivationProfile: 'aptos_hd',
  nextAccountIndex: 1,
  activeAccountCount: 0,
  totalAccountCount: 0,
  archivedAt: null,
  accounts: [],
  balances: [],
  createdAt: '2026-07-23T00:00:00.000Z',
  updatedAt: '2026-07-23T00:00:00.000Z',
})

describe('wallet display refresh order', () => {
  it('follows the wallet page order instead of the database wallet order', () => {
    const wallets = [
      wallet('standalone', null),
      wallet('group-one-first', 'group-one'),
      wallet('group-one-archived', 'group-one', '2026-07-23T00:00:00.000Z'),
      wallet('group-two-first', 'group-two'),
      wallet('group-two-second', 'group-two'),
    ]

    expect(walletDisplayRefreshOrder(wallets, [group('group-two'), group('group-one')]).map((item) => item.id))
      .toEqual(['group-two-first', 'group-two-second', 'group-one-first', 'standalone'])
  })
})
