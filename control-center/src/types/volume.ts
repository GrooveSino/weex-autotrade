import type { StrategyTargetMode } from './strategy'
import type { TradingMode } from './account'

export interface VolumeSessionProjection {
  sessionId: string
  accountId: string
  mode: TradingMode
  startedAtMs: number
  finishedAtMs: number | null
  strategyId: string | null
  strategyName: string | null
  strategyVersion: number | null
  targetMode: StrategyTargetMode
  strategyTargetQuoteVolume: string
  baselineLifetimeQuoteVolume: string
  finalLifetimeQuoteVolume: string | null
  result: string | null
  resultReason: string | null
  targetQuoteVolume: string
  verifiedQuoteVolume: string
  remainingQuoteVolume: string
  status: string
  auditStatus: 'verified' | 'pending' | 'discrepant'
  fillCount: number
  openingQuoteVolume: string
  closingQuoteVolume: string
  makerQuoteVolume: string
  takerQuoteVolume: string
  unknownLiquidityQuoteVolume: string
  lastSyncAtMs: number | null
  lastReconciliationAtMs: number | null
  sourceComplete: boolean
  stale: boolean
  reconciliationRequired: boolean
  discrepancyQuoteVolume: string
  retryAllowed: false
}
