import type { StrategyProgress, VolumeStrategy } from './strategy'
import type { VolumeSessionProjection } from './volume'
import type { TradingMode } from './shared'

export type InstanceStatus = 'running' | 'paused' | 'stopped' | 'warning' | 'error'
export type ProxyType = 'none' | 'http' | 'https' | 'socks5'
export type ProxyStatus = 'healthy' | 'degraded' | 'unchecked'
export type ExecutionLifecycleState = 'idle' | 'preparing' | 'running' | 'stopping' | 'recovering' | 'recovery_cleanup_required' | 'orders_cleanup_required' | 'position_blocked'

export interface AccountInstance {
  id: string
  name: string
  accountTag: string
  apiKeyTail: string
  mode: TradingMode
  status: InstanceStatus
  phase: string
  proxy: { type: ProxyType; host: string; location: string; latencyMs: number | null; status: ProxyStatus }
  wallet: { equity: number; available: number; unrealizedPnl: number }
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
    strategyProgressSource?: 'ledger' | 'execution_journal' | 'pending'
    strategyProgressUpdatedAtMs?: number | null
    historySync?: {
      state: 'not_requested' | 'initial_baseline_queued' | 'initial_baseline_running' | 'initial_baseline_pending' | 'incremental_queued' | 'syncing' | 'fresh' | 'stale'
      reason: string | null
      initialBaselineState: 'not_requested' | 'queued' | 'running' | 'complete' | 'pending'
      pending: boolean
      sourceComplete: boolean
      stale: boolean
      lastSuccessAtMs: number | null
      nextSyncAtMs: number | null
      highWatermarkMs: number | null
    } | null
    session?: VolumeSessionProjection | null
    activeSession?: VolumeSessionProjection | null
    lastRun?: VolumeSessionProjection | null
  }
  exposure: { btcLong: number; ethShort: number }
  cycle: { completed: number; target: number; nextActionAt: string | null }
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
  executionLifecycle: {
    state: ExecutionLifecycleState
    primaryAction: 'start' | 'stop' | 'safe_stop' | 'wait' | 'cancel_orders' | 'recheck'
    executionId: string | null
    sessionId: string | null
    reasonCode: string | null
    positionCount: number
    regularOrderCount: number
    triggerOrderCount: number
    blockingPositions: Array<{ symbol: string; side: string; quantity: string; approximateQuote: string }>
    boundaryCheckedAtMs: number | null
  }
  updatedAt: string
  unreadLogs: number
}

export interface AccountTradeVolumePeriod {
  lookbackDays: 1 | 7 | 30
  startAtMs: number
  endAtMs: number
  totalQuoteVolume: string
  makerQuoteVolume: string
  takerQuoteVolume: string
  unknownLiquidityQuoteVolume: string
  tradeCount: number
  complete: boolean
  warnings: string[]
}

export interface AccountTradeVolumeReport {
  periods: AccountTradeVolumePeriod[]
  generatedAtMs: number
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

export interface GlobalStopResult { stopped: number; cancelVerified: number; cancelFailed: number }
export type StatusFilter = 'all' | InstanceStatus
