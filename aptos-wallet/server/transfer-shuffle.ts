import { randomInt } from 'node:crypto'

const MAX_CANDIDATES = 96

/**
 * Turns a human account label into the family used for spacing transfers.
 * For example, .Ls1, []Ls2 and []Ls3 are all part of the "ls" family.
 */
export function accountFamily(label: string | null | undefined): string | null {
  if (!label) return null
  const normalized = label.trim()
    .replace(/^\[\]/, '')
    .replace(/^\./, '')
    .toLocaleLowerCase()
  if (!normalized || /^(账户|account)\s*#?\d+$/.test(normalized)) return null
  const family = normalized.replace(/\d+$/, '').trim()
  return family || null
}

export function spreadShuffle<T>(values: readonly T[], affinityKeys: (value: T) => readonly string[]): T[] {
  if (values.length < 2) return [...values]
  const attempts = Math.min(MAX_CANDIDATES, Math.max(24, Math.ceil(values.length * 4)))
  let best = fisherYates(values)
  let bestScore = spacingPenalty(best, affinityKeys)

  for (let attempt = 1; attempt < attempts; attempt += 1) {
    const candidate = fisherYates(values)
    const score = spacingPenalty(candidate, affinityKeys)
    if (score < bestScore || (score === bestScore && randomInt(2) === 0)) {
      best = candidate
      bestScore = score
    }
  }
  return best
}

function fisherYates<T>(values: readonly T[]): T[] {
  const copy = [...values]
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const selected = randomInt(index + 1)
    ;[copy[index], copy[selected]] = [copy[selected], copy[index]]
  }
  return copy
}

function spacingPenalty<T>(values: readonly T[], affinityKeys: (value: T) => readonly string[]): number {
  const lastPosition = new Map<string, number>()
  let penalty = 0
  for (let position = 0; position < values.length; position += 1) {
    for (const family of new Set(affinityKeys(values[position]))) {
      const previous = lastPosition.get(family)
      if (previous !== undefined) {
        const gap = position - previous
        // Adjacent transfers from the same account family are especially undesirable.
        penalty += gap === 1 ? 1_000_000_000 : Math.floor(10_000_000 / (gap * gap))
      }
      lastPosition.set(family, position)
    }
  }
  return penalty
}
