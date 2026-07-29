import { describe, expect, it } from 'vitest'
import { accountFamily, spreadShuffle } from '../server/transfer-shuffle.js'

describe('transfer shuffle', () => {
  it('recognizes numbered aliases but ignores the default account labels', () => {
    expect(accountFamily('.Ls1')).toBe('ls')
    expect(accountFamily('[]Ls3')).toBe('ls')
    expect(accountFamily('账户 #12')).toBeNull()
    expect(accountFamily('账户 12')).toBeNull()
  })

  it('keeps entire entries while spreading matching account families apart', () => {
    const entries = [
      { id: 'ls1', family: 'ls' }, { id: 'ls2', family: 'ls' }, { id: 'ls3', family: 'ls' },
      { id: 'ds1', family: 'ds' }, { id: 'ds2', family: 'ds' }, { id: 'ds3', family: 'ds' },
      { id: 'x1', family: 'x1' }, { id: 'x2', family: 'x2' }, { id: 'x3', family: 'x3' },
      { id: 'x4', family: 'x4' }, { id: 'x5', family: 'x5' }, { id: 'x6', family: 'x6' },
    ]
    const shuffled = spreadShuffle(entries, (entry) => [entry.family])

    expect(shuffled.map((entry) => entry.id).sort()).toEqual(entries.map((entry) => entry.id).sort())
    for (const family of ['ls', 'ds']) {
      const positions = shuffled.map((entry, index) => entry.family === family ? index : -1).filter((index) => index >= 0)
      expect(Math.min(...positions.slice(1).map((position, index) => position - positions[index]))).toBeGreaterThan(1)
    }
  })
})
