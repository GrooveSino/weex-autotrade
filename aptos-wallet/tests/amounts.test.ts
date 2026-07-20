import { describe, expect, it } from 'vitest'
import { formatAmount, parseAmount, randomBigIntInclusive } from '../shared/amounts.js'

describe('amount helpers', () => {
  it('round trips exact APT and USDt base units without floats', () => {
    expect(parseAmount('1.00000001', 'APT')).toBe(100000001n)
    expect(formatAmount(100000001n, 'APT')).toBe('1.00000001')
    expect(parseAmount('123456789.123456', 'USDT')).toBe(123456789123456n)
    expect(formatAmount(123456789123456n, 'USDT')).toBe('123456789.123456')
  })

  it('rejects excess precision and invalid ranges', () => {
    expect(() => parseAmount('0.0000001', 'USDT')).toThrow('最多支持 6 位小数')
    expect(() => randomBigIntInclusive(2n, 1n)).toThrow()
  })

  it('always samples within inclusive bigint bounds', () => {
    for (let index = 0; index < 100; index += 1) {
      const value = randomBigIntInclusive(10n, 20n)
      expect(value).toBeGreaterThanOrEqual(10n)
      expect(value).toBeLessThanOrEqual(20n)
    }
  })
})
