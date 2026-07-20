import { generateMnemonic } from '@scure/bip39'
import { wordlist } from '@scure/bip39/wordlists/english.js'

export function createMnemonic(): string {
  return generateMnemonic(wordlist, 256)
}

export function pickConfirmationIndexes(count = 4, wordCount = 24): number[] {
  const selected = new Set<number>()
  while (selected.size < count) {
    const value = new Uint32Array(1)
    crypto.getRandomValues(value)
    const limit = Math.floor(0x1_0000_0000 / wordCount) * wordCount
    if (value[0] < limit) selected.add(value[0] % wordCount)
  }
  return [...selected].sort((left, right) => left - right)
}

export function parseAccountIndexes(input: string): number[] {
  if (!input.trim()) return []
  const values: number[] = []
  for (const part of input.split(/[\s,]+/).filter(Boolean)) {
    const range = /^(\d+)-(\d+)$/.exec(part)
    if (range) {
      const start = Number(range[1])
      const end = Number(range[2])
      if (end < start || end - start > 199) throw new Error('单个索引区间必须递增且不能超过 200 个账户')
      for (let index = start; index <= end; index += 1) values.push(index)
    } else if (/^\d+$/.test(part)) values.push(Number(part))
    else throw new Error(`无法识别账户索引：${part}`)
  }
  return values
}
