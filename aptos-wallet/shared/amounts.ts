import { ASSETS, type AssetId } from './types.js'

const DECIMAL_PATTERN = /^(0|[1-9]\d*)(\.\d+)?$/
export const RANDOM_AMOUNT_DECIMALS = 2

export function parseAmount(value: string, asset: AssetId): bigint {
  const normalized = value.trim()
  if (!DECIMAL_PATTERN.test(normalized)) throw new Error('金额格式无效')
  const decimals = ASSETS[asset].decimals
  const [whole, fraction = ''] = normalized.split('.')
  if (fraction.length > decimals) throw new Error(`${ASSETS[asset].symbol} 最多支持 ${decimals} 位小数`)
  return BigInt(whole) * 10n ** BigInt(decimals) + BigInt(fraction.padEnd(decimals, '0') || '0')
}

export function hasAtMostDecimals(value: string, maxDecimals: number): boolean {
  const normalized = value.trim()
  if (!DECIMAL_PATTERN.test(normalized)) return false
  return (normalized.split('.')[1]?.length ?? 0) <= maxDecimals
}

export function formatAmount(value: bigint | string, asset: AssetId): string {
  const amount = typeof value === 'bigint' ? value : BigInt(value)
  const decimals = ASSETS[asset].decimals
  const scale = 10n ** BigInt(decimals)
  const whole = amount / scale
  const fraction = (amount % scale).toString().padStart(decimals, '0').replace(/0+$/, '')
  return fraction ? `${whole}.${fraction}` : whole.toString()
}

export function randomBigIntInclusive(min: bigint, max: bigint): bigint {
  if (min > max) throw new Error('随机金额最小值不能大于最大值')
  const range = max - min + 1n
  if (range <= BigInt(Number.MAX_SAFE_INTEGER)) return min + BigInt(randomSafeInt(Number(range)))
  const bits = range.toString(2).length
  const bytes = Math.ceil(bits / 8)
  while (true) {
    let candidate = 0n
    for (let offset = 0; offset < bytes; offset += 6) {
      const width = Math.min(6, bytes - offset)
      candidate = (candidate << BigInt(width * 8)) + BigInt(randomSafeInt(2 ** (width * 8)))
    }
    if (candidate < range) return min + candidate
  }
}

export function randomAmountInclusive(min: bigint, max: bigint, asset: AssetId): bigint {
  const precision = ASSETS[asset].decimals
  const quantum = 10n ** BigInt(precision - RANDOM_AMOUNT_DECIMALS)
  if (min % quantum !== 0n || max % quantum !== 0n) throw new Error('随机金额必须按 0.01 的步长设置')
  return randomBigIntInclusive(min / quantum, max / quantum) * quantum
}

function randomSafeInt(maxExclusive: number): number {
  if (!Number.isSafeInteger(maxExclusive) || maxExclusive < 1) throw new Error('随机数范围无效')
  const ceiling = 2 ** 48
  const acceptable = ceiling - (ceiling % maxExclusive)
  const bytes = new Uint8Array(6)
  while (true) {
    globalThis.crypto.getRandomValues(bytes)
    let value = 0
    for (const byte of bytes) value = value * 256 + byte
    if (value < acceptable) return value % maxExclusive
  }
}

export function addAmountStrings(left: string, right: string): string {
  return (BigInt(left) + BigInt(right)).toString()
}
