import type { AccountInstance, ExecutionCycle, StrategyRunPage } from '../../types'
import { apiRequest, controlPlaneEnabled, sleep } from '../core/controlCenterCore'
import { appendMockLog, mockLogStream } from '../monitoring/controlCenterStreams'

export async function fetchExecutionHistory(account: AccountInstance): Promise<ExecutionCycle[]> {
  if (controlPlaneEnabled) return apiRequest<ExecutionCycle[]>(`/instances/${account.id}/executions?limit=50`)
  await sleep(360)
  const now = Date.now()
  const turnoverQuote = String(
    (Number(account.strategy.roundTurnoverQuoteMin) + Number(account.strategy.roundTurnoverQuoteMax)) / 2,
  )
  const totalQuote = String(Number(turnoverQuote) / 2)
  const beta = Number(account.strategyProgress.lastEthRatio ?? '0.44260456370165036')
  const btcQuote = String(Number(totalQuote) / (1 + beta))
  const ethQuote = String(Number(totalQuote) - Number(btcQuote))
  const completed = Array.from({ length: Math.min(account.cycle.completed, 8) }, (_, index) => {
    const sequence = account.cycle.completed - index
    return {
      cycleId: `mock-${account.id}-${sequence}`,
      sequence,
      status: 'completed' as const,
      reason: 'mock_pair_filled',
      totalQuote,
      turnoverQuote,
      btcLongQuote: btcQuote,
      ethShortQuote: ethQuote,
      allocationVersion: 'browser-demo:historical-fixture',
      positionHoldSeconds: account.strategy.positionHoldMinSeconds,
      roundIntervalSeconds: account.strategy.roundIntervalMinSeconds,
      sizingMode: 'range_random' as const,
      strategyId: account.strategy.id,
      createdAtMs: now - (index + 1) * 18_000,
      updatedAtMs: now - (index + 1) * 18_000 + 420,
      reconciliationRequired: false,
      retryAllowed: false as const,
    }
  })
  if (account.status !== 'error') return completed
  return [{
    cycleId: `mock-${account.id}-uncertain`,
    sequence: account.cycle.completed + 1,
    status: 'uncertain',
    reason: 'mock_transport_outcome_unknown',
    totalQuote,
    turnoverQuote,
    btcLongQuote: btcQuote,
    ethShortQuote: ethQuote,
    allocationVersion: 'browser-demo:historical-fixture',
    positionHoldSeconds: account.strategy.positionHoldMinSeconds,
    roundIntervalSeconds: account.strategy.roundIntervalMinSeconds,
    sizingMode: 'range_random',
    strategyId: account.strategy.id,
    createdAtMs: now - 5_000,
    updatedAtMs: now - 4_200,
    reconciliationRequired: true,
    retryAllowed: false,
  }, ...completed]
}

export async function fetchStrategyRuns(
  account: AccountInstance,
  cursor: string | null = null,
): Promise<StrategyRunPage> {
  if (controlPlaneEnabled) {
    const query = new URLSearchParams({ limit: '50' })
    if (cursor) query.set('cursor', cursor)
    return apiRequest<StrategyRunPage>(`/instances/${account.id}/strategy-runs?${query}`)
  }
  await sleep(240)
  return { items: [], nextCursor: null }
}

export async function refreshAccountSnapshot(account: AccountInstance): Promise<AccountInstance> {
  if (controlPlaneEnabled) return apiRequest<AccountInstance>(`/instances/${account.id}/refresh`, { method: 'POST' })
  await sleep(420)
  const completedAt = Date.now()
  mockLogStream(account)
  appendMockLog(account.id, 'success', '刷新成功：价格、钱包与仓位已同步')
  return {
    ...account,
    wallet: {
      ...account.wallet,
      equity: account.wallet.equity + (Math.random() - 0.5) * 3,
      available: account.wallet.available + (Math.random() - 0.5) * 2,
    },
    proxy: {
      ...account.proxy,
      latencyMs: account.proxy.latencyMs ? Math.max(38, account.proxy.latencyMs + Math.round((Math.random() - 0.5) * 12)) : null,
    },
    runtime: {
      ...account.runtime,
      lastPollStartedAtMs: completedAt - 380,
      lastPollSucceededAtMs: completedAt,
      lastPollDurationMs: 380,
      consecutiveFailures: 0,
      lastErrorType: null,
      lastStopVerifiedAtMs: null,
    },
    updatedAt: '刚刚',
  }
}
