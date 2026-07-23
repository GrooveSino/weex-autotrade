export type InstanceStatus = 'running' | 'paused' | 'stopped' | 'warning' | 'error'
export type TradingMode = 'demo' | 'live'
export type ProxyType = 'none' | 'http' | 'https' | 'socks5'
export type ProxyStatus = 'healthy' | 'degraded' | 'unchecked'
export type StrategyStage = 'idle' | 'holding' | 'cooldown' | 'complete'
export type StrategyTargetMode = 'incremental' | 'lifetime'
export type StrategyRunStatus = 'active' | 'stopping' | 'completed' | 'stopped' | 'verification_pending' | 'uncertain'

export interface StrategyRunSummary {
  sessionId: string
  strategyId: string | null
  strategyName: string | null
  strategyVersion: number | null
  targetMode: StrategyTargetMode
  startedAtMs: number
  finishedAtMs: number | null
  status: StrategyRunStatus
  result: 'completed' | 'stopped' | 'uncertain' | null
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

export interface GlobalStopResult {
  stopped: number
  cancelVerified: number
  cancelFailed: number
}

export interface AccountInstance {
  id: string
  name: string
  accountTag: string
  apiKeyTail: string
  mode: TradingMode
  status: InstanceStatus
  phase: string
  proxy: {
    type: ProxyType
    host: string
    location: string
    latencyMs: number | null
    status: ProxyStatus
  }
  wallet: {
    equity: number
    available: number
    unrealizedPnl: number
  }
  fundingPreflight?: {
    status: 'pending' | 'ready' | 'insufficient'
    availableQuote: string | null
    openingNotionalQuote: string
    requiredLeverage: number | null
    plannedLeverage: number | null
    maxLeverage: number
    safetyBuffer: string
    maxSupportedTurnoverQuote: string | null
    reason: string
  }
  volume: {
    lifetime: number
    today: number
    complete: boolean
    lifetimeSourceComplete?: boolean
    strategyTargetQuoteVolume?: string
    strategyVerifiedQuoteVolume?: string
    strategyRemainingQuoteVolume?: string
    strategyTargetReached?: boolean
    session?: {
      sessionId: string
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
      fillCount: number
      openingQuoteVolume: string
      closingQuoteVolume: string
      makerQuoteVolume: string
      takerQuoteVolume: string
      unknownLiquidityQuoteVolume: string
      lastSyncAtMs: number | null
      sourceComplete: boolean
      stale: boolean
      reconciliationRequired: boolean
      discrepancyQuoteVolume: string
      retryAllowed: false
    } | null
    activeSession?: VolumeSessionProjection | null
    lastRun?: VolumeSessionProjection | null
  }
  exposure: {
    btcLong: number
    ethShort: number
  }
  cycle: {
    completed: number
    target: number
    nextActionAt: string | null
  }
  strategyId: string
  strategy: VolumeStrategy
  strategyProgress: StrategyProgress
  mockCycleTotalQuote?: string | null
  historyStartAtMs?: number | null
  runtime: {
    lastPollStartedAtMs: number | null
    lastPollSucceededAtMs: number | null
    lastPollFailedAtMs: number | null
    lastPollDurationMs: number | null
    consecutiveFailures: number
    lastErrorType: string | null
    lastStopVerifiedAtMs: number | null
  }
  updatedAt: string
  unreadLogs: number
}

export interface LogLine {
  id: string
  timestamp: string
  level: 'info' | 'success' | 'warn' | 'error'
  message: string
}

export interface LogBatch {
  lines: LogLine[]
  cursor: string | null
  reset: boolean
}

export interface ActiveExecutionWait {
  key: string
  label: string
  updatedAtMs: number
  elapsedMs: number
  remainingMs: number | null
  detail: string
  symbol: string | null
  action: string | null
  startedAtMs: number | null
  deadlineAtMs: number | null
}

export interface ExecutionTimelineEntry {
  id: string
  sequence: number
  atMs: number
  level: LogLine['level']
  eventName: string
  title: string
  detail: string
}

export interface StrategyMonitorSnapshot {
  schemaVersion: number
  instanceId: string
  sessionId: string | null
  executionId: string | null
  executorGeneration: string
  status: string
  phase: string
  currentRun: number
  currentRound: number
  targetQuoteVolume: string
  verifiedQuoteVolume: string
  ledgerVerifiedQuoteVolume: string
  remainingQuoteVolume: string
  volumeSource: 'ledger' | 'execution_journal' | 'pending'
  sourceComplete: boolean
  stale: boolean
  reconciliationRequired: boolean
  btcQuoteVolume: string
  ethQuoteVolume: string
  makerFillCount: number
  takerFillCount: number
  unknownFillCount: number
  submissions: number
  cancels: number
  requotes: number
  activeWaits: ActiveExecutionWait[]
  timeline: ExecutionTimelineEntry[]
  projectionSequence: number
  projectionVersion: number
  ledgerRevision: number
  serverTimeMs: number
  updatedAtMs: number
  freshness: 'current' | 'stale' | 'rebuilding'
  streamState: 'ready' | 'catching_up' | 'reset_required'
  cursor: string | null
  hasMore: boolean
}

export interface StrategyMonitorEvent {
  type: 'snapshot' | 'delta' | 'reset' | 'heartbeat'
  snapshot?: StrategyMonitorSnapshot
  fromSequence?: number
  toSequence?: number
  journalSequence?: number
  projectionSequence?: number
  serverTimeMs?: number
}

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

export interface AccountDraft {
  name: string
  accountTag: string
  mode: TradingMode
  apiKey: string
  apiSecret: string
  passphrase: string
  proxyType: ProxyType
  proxyUrl: string
  strategyId: string
  historyStartAt: string
}

export interface StrategyAssignmentResult {
  strategy: VolumeStrategy
  instances: AccountInstance[]
}

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

export interface BetaMarketSnapshot {
  schemaVersion: string
  strategy: string
  status: string
  upstreamUsable: boolean
  reasonCodes: string[]
  finalBeta: string
  btcLongRatio: string
  ethShortRatio: string
  btcLongWeight: string
  ethShortWeight: string
  confidence: string
  confidenceThreshold: string
  source: string
  asOfMs: number
  generatedAtMs: number
  ageMs: string
  maxAgeMs: string
}

export interface BetaSourceSettings {
  url: string
  timeoutSeconds: number
  refreshIntervalSeconds: number
  backgroundRefreshEnabled: boolean
  updatedAtMs: number
}

export interface InstanceSnapshotEvent {
  type: 'instances'
  instances: AccountInstance[]
  runtime?: SchedulerMetrics
  campaigns?: BetaCampaign[]
  sequence?: number
  executorGeneration?: string
}

export interface SchedulerMetrics {
  maxParallelPolls: number
  activePolls: number
  maxObservedParallelism: number
  pollRounds: number
  accountsPolled: number
  successfulPolls: number
  failedPolls: number
  lastRoundAccountCount: number
  lastRoundSucceeded: number
  lastRoundFailed: number
  lastRoundStartedAtMs: number | null
  lastRoundCompletedAtMs: number | null
  lastRoundDurationMs: number | null
}

export interface ControlPlaneHealth {
  status: string
  adapter: string
  storage: string
  liveTradingEnabled: boolean
  executionEnabled: boolean
  liveCampaignsEnabled: boolean
  boundStrategyExecutionEnabled: boolean
  liveCampaignActiveWorkerCount: number
  liveCampaignWorkerCount: number
  apiReleaseId?: string | null
  executorConnected?: boolean
  executorGeneration?: string | null
}

export type BetaCampaignStatus = 'planned' | 'executing' | 'stopping' | 'completed' | 'stopped' | 'uncertain'

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

export interface BetaCampaignPreview extends BetaCampaign {
  warnings: string[]
  blockers: string[]
}

export type StatusFilter = 'all' | InstanceStatus
