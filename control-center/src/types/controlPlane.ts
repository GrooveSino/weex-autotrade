import type { AccountInstance } from './account'
import type { BetaCampaign } from './execution'

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

export interface InstanceSnapshotEvent {
  type: 'instances'
  instances: AccountInstance[]
  runtime?: SchedulerMetrics
  campaigns?: BetaCampaign[]
  sequence?: number
  executorGeneration?: string
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
  activeExecutionCapacity: number
  maxExecutionCapacity: number
  activeNormalPhaseCapacity: number
  maxNormalPhaseCapacity: number
  queuedNormalPhaseCount: number
  capacityRevision: number
  activeNormalIo: number
  maxNormalIo: number
  activeEmergencyIo: number
  maxEmergencyIo: number
  activeProxyPhasePartitions: number
  queuedProxyLimitedPhaseCount: number
  normalPhaseQueueP50Ms: number
  normalPhaseQueueP95Ms: number
  sqliteWriteQueueCritical: number
  sqliteWriteQueueLowPriority: number
  sqliteWriteP95Ms: number
  actorCount: number
  eventLoopDelayP99Ms: number
  openFileDescriptors: number
  rssBytes: number
  marketDataActiveLeases: number
  marketDataSharedConnections: number
  marketDataIdleConnections: number
  sharedMarketEnabled: boolean
  sharedMarketConnected: boolean
  sharedMarketGeneration: number
  sharedMarketBtcSnapshotAgeMs: number | null
  sharedMarketEthSnapshotAgeMs: number | null
  sharedMarketRestFallbackCount: number
  sharedMarketReconnectCount: number
  sharedMarketWaitingPhaseCount: number
  sharedMarketSourceState: string
  privateOrderStreamActiveLeases: number
  privateOrderStreams: number
  historySyncQueued: number
  historySyncRunning: number
}

export interface ExecutionCapacity {
  activeExecutions: number
  maxActiveExecutions: number
  activeNormalPhases: number
  maxNormalPhases: number
  queuedNormalPhases: number
  phaseStartRatePerSecond: number
  perProxyGapSeconds: number
  revision: number
  activeNormalIo: number
  maxNormalIo: number
  activeEmergencyIo: number
  maxEmergencyIo: number
  activeProxyPhasePartitions: number
  queuedProxyLimitedPhases: number
  phaseQueueP50Ms: number
  phaseQueueP95Ms: number
  sqliteWriteQueueCritical: number
  sqliteWriteQueueLowPriority: number
  sqliteWriteP95Ms: number
  actorCount: number
  eventLoopDelayP99Ms: number
  openFileDescriptors: number
  rssBytes: number
  marketDataActiveLeases: number
  marketDataSharedConnections: number
  marketDataIdleConnections: number
  sharedMarketEnabled: boolean
  sharedMarketConnected: boolean
  sharedMarketGeneration: number
  sharedMarketBtcSnapshotAgeMs: number | null
  sharedMarketEthSnapshotAgeMs: number | null
  sharedMarketRestFallbackCount: number
  sharedMarketReconnectCount: number
  sharedMarketWaitingPhaseCount: number
  sharedMarketSourceState: string
  privateOrderStreamActiveLeases: number
  privateOrderStreams: number
  historySyncQueued: number
  historySyncRunning: number
}
