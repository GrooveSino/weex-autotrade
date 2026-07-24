import type { AccountInstance, StrategyDraft, VolumeStrategy } from '../types'

type StrategySizing = Pick<
  VolumeStrategy,
  'targetVolumeQuoteMax' | 'roundTurnoverQuoteMin' | 'roundTurnoverQuoteMax'
>

export interface RoundEstimate {
  minimum: number
  maximum: number
  minimumTurnover: number
  maximumTurnover: number
}

export function estimateRounds(strategy: StrategySizing, generatedVolumeQuote = 0): RoundEstimate | null {
  const target = Number(strategy.targetVolumeQuoteMax)
  const minimumTurnover = Number(strategy.roundTurnoverQuoteMin)
  const maximumTurnover = Number(strategy.roundTurnoverQuoteMax)
  if (![target, minimumTurnover, maximumTurnover].every(Number.isFinite)) return null
  if (target <= 0 || minimumTurnover <= 0 || maximumTurnover < minimumTurnover) return null
  const remaining = Math.max(0, target - generatedVolumeQuote)
  return {
    minimum: remaining ? Math.ceil(remaining / maximumTurnover) : 0,
    maximum: remaining ? Math.ceil(remaining / minimumTurnover) : 0,
    minimumTurnover,
    maximumTurnover,
  }
}

export function targetProgress(account: AccountInstance): number {
  return account.strategy.targetMode === 'lifetime'
    ? account.volume.lifetime
    : Number(account.strategyProgress.generatedVolumeQuote)
}

export function targetModeLabel(mode: VolumeStrategy['targetMode']): string {
  return mode === 'lifetime' ? '历史累计' : '启动后新增'
}

export function targetTolerance(target: number): number {
  const proportional = target * 0.0025
  return target >= 10_000 ? Math.min(50, proportional) : Math.max(1, proportional)
}

export function calculateFundingPreflight(
  strategy: VolumeStrategy,
  availableQuote: number,
  walletKnown = true,
): NonNullable<AccountInstance['fundingPreflight']> {
  const openingNotional = Number(strategy.roundTurnoverQuoteMax) / 2
  const maxLeverage = 99
  const safetyBuffer = 1.2
  if (!walletKnown) {
    return {
      status: 'pending', availableQuote: null, openingNotionalQuote: String(openingNotional),
      requiredLeverage: null, plannedLeverage: null, maxLeverage, safetyBuffer: '1.20',
      maxSupportedTurnoverQuote: null, reason: 'wallet_not_synchronized',
    }
  }
  const maxSupported = availableQuote > 0 ? availableQuote * maxLeverage / safetyBuffer * 2 : 0
  const required = availableQuote > 0 ? Math.max(1, Math.ceil(openingNotional * safetyBuffer / availableQuote)) : null
  return {
    status: required !== null && required <= maxLeverage ? 'ready' : 'insufficient',
    availableQuote: String(Math.max(0, availableQuote)),
    openingNotionalQuote: String(openingNotional),
    requiredLeverage: required,
    plannedLeverage: required !== null && required <= maxLeverage ? required : null,
    maxLeverage,
    safetyBuffer: '1.20',
    maxSupportedTurnoverQuote: String(maxSupported),
    reason: required === null ? 'available_balance_zero' : required > maxLeverage ? 'required_leverage_exceeds_99x' : 'ready',
  }
}

export function draftStrategy(draft: StrategyDraft): StrategySizing {
  return {
    targetVolumeQuoteMax: draft.targetVolumeQuoteMax,
    roundTurnoverQuoteMin: draft.roundTurnoverQuoteMin,
    roundTurnoverQuoteMax: draft.roundTurnoverQuoteMax,
  }
}

export function durationParts(totalSeconds: number): { hours: number; minutes: number; seconds: number } {
  const normalized = Math.max(0, Math.floor(totalSeconds || 0))
  return {
    hours: Math.floor(normalized / 3600),
    minutes: Math.floor((normalized % 3600) / 60),
    seconds: normalized % 60,
  }
}

export function secondsFromParts(hours: number, minutes: number, seconds: number): number {
  return Math.max(0, Math.floor(hours || 0) * 3600 + Math.floor(minutes || 0) * 60 + Math.floor(seconds || 0))
}

export function compactDuration(totalSeconds: number): string {
  const { hours, minutes, seconds } = durationParts(totalSeconds)
  const parts: string[] = []
  if (hours) parts.push(`${hours}h`)
  if (minutes) parts.push(`${minutes}m`)
  if (seconds || parts.length === 0) parts.push(`${seconds}s`)
  return parts.join(' ')
}

export function countdown(timestamp: number | null): string {
  if (!timestamp) return '等待规划'
  return compactDuration(Math.max(0, Math.ceil((timestamp - Date.now()) / 1000)))
}
