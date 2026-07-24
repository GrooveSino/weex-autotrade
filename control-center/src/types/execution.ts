import type { AccountInstance } from './account'
import type { StrategyDirection, StrategyTargetMode, VolumeStrategy } from './strategy'

export interface ExecutionCycle {
  cycleId: string
  sequence: number
  status: 'planned' | 'opened' | 'completed' | 'rejected' | 'uncertain'
  reason: string
  totalQuote: string
  turnoverQuote: string
  btcLongQuote: string
  ethShortQuote: string
  allocationVersion: string
  positionHoldSeconds: number
  roundIntervalSeconds: number
  sizingMode: 'range_random' | 'residual_finish' | 'legacy_fixed'
  strategyId: string
  createdAtMs: number
  updatedAtMs: number
  reconciliationRequired: boolean
  retryAllowed: false
}

export interface StrategyAssignmentResult { strategy: VolumeStrategy; instances: AccountInstance[] }
export type BetaCampaignStatus = 'planned' | 'executing' | 'stopping' | 'completed' | 'stopped' | 'recovering' | 'uncertain'

export interface BetaCampaignEvent {
  sequence: number
  name: string
  atMs: number
  phase?: string | null
  run?: number | null
  childPlanId?: string | null
  status?: string | null
  message?: string | null
  fields: Record<string, unknown>
}

export interface BetaCampaign {
  campaignId: string
  instanceId: string
  status: BetaCampaignStatus
  schemaVersion: number
  strategyId: string | null
  strategyName: string | null
  strategyVersion: number | null
  strategySnapshot: Record<string, unknown> | null
  sessionId?: string | null
  targetMode?: StrategyTargetMode | null
  runDisposition?: 'new_incremental' | 'lifetime_residual' | null
  strategyTargetQuoteVolume?: string | null
  executionTargetQuoteVolume?: string | null
  baselineLifetimeQuoteVolume?: string | null
  direction: StrategyDirection
  selectedTargetQuoteVolume: string | null
  leverage: number | 'auto'
  marginMode: 'isolated' | 'cross'
  targetQuote: string
  roundTurnoverQuoteMin: string | null
  cycleVolume: string
  authorizedMaxQuote: string
  holdMinSeconds: number
  holdMaxSeconds: number
  roundGapMinSeconds: number
  roundGapMaxSeconds: number
  maxRuns: number
  beta: string
  betaVersion: string
  betaSource: string
  betaAsOfMs: number
  betaAgeMs: string
  betaMaxAgeMs: string
  btcLongWeight: string
  ethShortWeight: string
  availableQuote: string | null
  requiredLeverage: number | null
  plannedLeverage: number | null
  maxSupportedTurnoverQuote: string | null
  confirmation: string
  stopConfirmation: string
  reconciliationConfirmation: string | null
  reconciliationRequired: boolean
  retryAllowed: false
  riskAcknowledged: boolean
  currentRun: number
  generatedQuote: string
  remainingQuote: string
  excessQuote: string
  makerQuote: string
  takerQuote: string
  unknownQuote: string
  btcQuote: string
  ethQuote: string
  fillCount: number
  makerCount: number
  takerCount: number
  unknownCount: number
  orderCount: number
  cancelCount: number
  requoteCount: number
  phase: string
  reason: string | null
  startedAtMs: number | null
  finishedAtMs: number | null
  elapsedMs: number | null
  lastEvent: BetaCampaignEvent | null
  events: BetaCampaignEvent[]
}

export interface BetaCampaignPreviewRequest {
  targetQuote: string
  cycleVolume: string
  holdMinSeconds: number
  holdMaxSeconds: number
  roundGapMinSeconds: number
  roundGapMaxSeconds: number
}

export interface BetaCampaignPreview extends BetaCampaign { warnings: string[]; blockers: string[] }

export interface StrategyRunPrepareResponse {
  disposition: 'ready' | 'running' | 'stopping' | 'recovering' | 'cleanup_required' | 'unavailable'
  preview: BetaCampaignPreview | null
  current: BetaCampaign | null
  reasonCode: string | null
  message: string | null
  positionCount: number
  regularOrderCount: number
  triggerOrderCount: number
  cleanupConfirmation: string | null
}

export interface StrategyRunCapacity {
  activeExecutions: number
  maxActiveExecutions: number
  activeNormalPhases: number
  maxNormalPhases: number
  queuedNormalPhases: number
  revision: number
}

export interface StrategyRunPhaseQueue {
  position: number | null
  estimatedStartAtMs: number | null
  proxyLimited: boolean
}

export interface StrategyRunConfirmResponse {
  admissionState: 'admitted' | 'capacity_full'
  executionId: string
  execution: BetaCampaign
  capacity: StrategyRunCapacity
  phaseQueue: StrategyRunPhaseQueue | null
}
