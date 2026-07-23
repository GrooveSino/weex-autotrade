export type StrategyStage = 'idle' | 'holding' | 'cooldown' | 'complete'
export type StrategyTargetMode = 'incremental' | 'lifetime'
export type StrategyRunStatus = 'active' | 'recovering' | 'stopping' | 'completed' | 'stopped'

export interface StrategyRunSummary {
  sessionId: string
  strategyId: string | null
  strategyName: string | null
  strategyVersion: number | null
  targetMode: StrategyTargetMode
  startedAtMs: number
  finishedAtMs: number | null
  status: StrategyRunStatus
  auditStatus: 'verified' | 'pending' | 'discrepant'
  result: 'completed' | 'stopped' | null
  resultReason: string | null
  strategyTargetQuoteVolume: string
  executionTargetQuoteVolume: string
  verifiedQuoteVolume: string
  remainingQuoteVolume: string
  baselineLifetimeQuoteVolume: string
  finalLifetimeQuoteVolume: string | null
  startingAvailableBalanceQuote: string | null
  endingAvailableBalanceQuote: string | null
  availableBalanceChangeQuote: string | null
  sourceComplete: boolean
  stale: boolean
  reconciliationRequired: boolean
}

export interface StrategyRunPage {
  items: StrategyRunSummary[]
  nextCursor: string | null
}

export interface VolumeStrategy {
  id: string
  version: number
  name: string
  targetMode: StrategyTargetMode
  targetVolumeQuote: string
  roundTurnoverQuoteMin: string
  roundTurnoverQuoteMax: string
  positionHoldMinSeconds: number
  positionHoldMaxSeconds: number
  roundIntervalMinSeconds: number
  roundIntervalMaxSeconds: number
}

export interface StrategyDraft {
  name: string
  targetMode: StrategyTargetMode
  targetVolumeQuote: string
  roundTurnoverQuoteMin: string
  roundTurnoverQuoteMax: string
  positionHoldMinSeconds: number
  positionHoldMaxSeconds: number
  roundIntervalMinSeconds: number
  roundIntervalMaxSeconds: number
}

export interface StrategyProgress {
  generatedVolumeQuote: string
  startedAtMs: number | null
  stage: StrategyStage
  nextActionAtMs: number | null
  activeCycleId: string | null
  lastEthRatio: string | null
  lastAllocationVersion: string | null
  systemPauseReason: string | null
}
