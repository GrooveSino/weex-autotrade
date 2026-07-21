export type TransferPairingMode = 'none' | 'one_to_one' | 'one_to_many' | 'many_to_one'

export type TransferPairingIssue =
  | { kind: 'count_mismatch' }
  | { kind: 'self_transfer'; position: number }

export interface PairingTarget {
  walletId: string | null
}

export interface TransferPair<T extends PairingTarget> {
  sourceWalletId: string
  target: T
}

export function pairTransferEndpoints<T extends PairingTarget>(sourceWalletIds: string[], targets: T[]): {
  mode: TransferPairingMode
  pairs: Array<TransferPair<T>>
  issue: TransferPairingIssue | null
} {
  if (!sourceWalletIds.length || !targets.length) return { mode: 'none', pairs: [], issue: null }

  let mode: TransferPairingMode
  let pairs: Array<TransferPair<T>>
  if (sourceWalletIds.length === 1 && targets.length === 1) {
    mode = 'one_to_one'
    pairs = [{ sourceWalletId: sourceWalletIds[0], target: targets[0] }]
  } else if (sourceWalletIds.length === 1) {
    mode = 'one_to_many'
    pairs = targets.map((target) => ({ sourceWalletId: sourceWalletIds[0], target }))
  } else if (targets.length === 1) {
    mode = 'many_to_one'
    pairs = sourceWalletIds.map((sourceWalletId) => ({ sourceWalletId, target: targets[0] }))
  } else if (sourceWalletIds.length === targets.length) {
    mode = 'one_to_one'
    pairs = sourceWalletIds.map((sourceWalletId, position) => ({ sourceWalletId, target: targets[position] }))
  } else {
    return { mode: 'none', pairs: [], issue: { kind: 'count_mismatch' } }
  }

  const selfTransferPosition = pairs.findIndex(({ sourceWalletId, target }) => target.walletId === sourceWalletId)
  return {
    mode,
    pairs,
    issue: selfTransferPosition === -1 ? null : { kind: 'self_transfer', position: selfTransferPosition },
  }
}
