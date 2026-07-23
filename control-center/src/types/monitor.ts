export interface LogLine {
  id: string
  timestamp: string
  level: 'info' | 'success' | 'warn' | 'error'
  message: string
}

export interface LogBatch { lines: LogLine[]; cursor: string | null; reset: boolean }

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
