import { describe, expect, it } from 'vitest'
import { formatAmount, hasAtMostDecimals, parseAmount, randomAmountInclusive, randomBigIntInclusive } from '../shared/amounts.js'

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

  it('samples transfer amounts in 0.01 increments', () => {
    const min = parseAmount('1.01', 'APT')
    const max = parseAmount('1.09', 'APT')
    const quantum = 1_000_000n
    for (let index = 0; index < 100; index += 1) {
      const value = randomAmountInclusive(min, max, 'APT')
      expect(value % quantum).toBe(0n)
      expect(value).toBeGreaterThanOrEqual(min)
      expect(value).toBeLessThanOrEqual(max)
    }
    expect(hasAtMostDecimals('1.25', 2)).toBe(true)
    expect(hasAtMostDecimals('1.251', 2)).toBe(false)
  })
})
