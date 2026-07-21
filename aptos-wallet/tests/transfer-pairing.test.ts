import { describe, expect, it } from 'vitest'
import { pairTransferEndpoints } from '../src/transfer-pairing.js'

const target = (walletId: string | null, suffix = walletId ?? 'external') => ({ walletId, address: suffix })

describe('transfer endpoint pairing', () => {
  it('preserves one-to-many and many-to-one transfers', () => {
    expect(pairTransferEndpoints(['a'], [target('b'), target('c')]).pairs.map((pair) => [pair.sourceWalletId, pair.target.walletId])).toEqual([['a', 'b'], ['a', 'c']])
    expect(pairTransferEndpoints(['a', 'b'], [target('c')]).pairs.map((pair) => [pair.sourceWalletId, pair.target.walletId])).toEqual([['a', 'c'], ['b', 'c']])
  })

  it('pairs equal multi-source and multi-target lists by position', () => {
    const result = pairTransferEndpoints(['a', 'b', 'c'], [target('x'), target('y'), target('z')])
    expect(result.mode).toBe('one_to_one')
    expect(result.issue).toBeNull()
    expect(result.pairs.map((pair) => [pair.sourceWalletId, pair.target.walletId])).toEqual([['a', 'x'], ['b', 'y'], ['c', 'z']])
  })

  it('rejects unequal multi-source and multi-target counts', () => {
    const result = pairTransferEndpoints(['a', 'b', 'c'], [target('x'), target('y'), target('z'), target(null, 'external-1'), target(null, 'external-2')])
    expect(result.pairs).toEqual([])
    expect(result.issue).toEqual({ kind: 'count_mismatch' })
  })

  it('rejects self transfers without silently dropping a pair', () => {
    const result = pairTransferEndpoints(['a', 'b'], [target('x'), target('b')])
    expect(result.pairs).toHaveLength(2)
    expect(result.issue).toEqual({ kind: 'self_transfer', position: 1 })
  })
})
