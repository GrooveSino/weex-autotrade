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
}
