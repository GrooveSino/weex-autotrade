import { describe, expect, it } from 'vitest'
import { validateMnemonic } from '@scure/bip39'
import { wordlist } from '@scure/bip39/wordlists/english.js'
import { createMnemonic, parseAccountIndexes, pickConfirmationIndexes } from '../src/mnemonic.js'

describe('browser mnemonic helpers', () => {
  it('creates valid 24-word BIP39 mnemonics', () => {
    const first = createMnemonic()
    const second = createMnemonic()
    expect(first.split(' ')).toHaveLength(24)
    expect(validateMnemonic(first, wordlist)).toBe(true)
    expect(second).not.toBe(first)
  })

  it('selects distinct confirmation positions and parses explicit indexes', () => {
    const indexes = pickConfirmationIndexes()
    expect(indexes).toHaveLength(4)
    expect(new Set(indexes).size).toBe(4)
    expect(indexes.every((index) => index >= 0 && index < 24)).toBe(true)
    expect(parseAccountIndexes('0, 5 37, 100-102')).toEqual([0, 5, 37, 100, 101, 102])
    expect(() => parseAccountIndexes('10-1')).toThrow('必须递增')
  })
})
