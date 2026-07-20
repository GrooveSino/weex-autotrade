import type {
  AccountDraft,
  AccountInstance,
  BetaCampaign,
  BetaCampaignEvent,
  BetaCampaignPreview,
  BetaCampaignPreviewRequest,
  BetaMarketSnapshot,
  ControlPlaneHealth,
  ExecutionCycle,
  GlobalStopResult,
  InstanceSnapshotEvent,
  LogBatch,
  LogLine,
  SchedulerMetrics,
  StrategyAssignmentResult,
  StrategyDraft,
  VolumeStrategy,
  VolumeSessionProjection,
} from '../types'
import { mockStrategies } from '../data/mockAccounts'
import { calculateFundingPreflight, estimateRounds } from '../utils/strategy'

const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))
const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, '')

export const controlPlaneEnabled = Boolean(configuredBaseUrl)
export const dataSourceLabel = controlPlaneEnabled ? '控制平面 API' : '内置 Mock 服务'

async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  if (!configuredBaseUrl) throw new Error('control-plane API is not configured')
  const headers = new Headers(init?.headers)
  if (init?.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const response = await fetch(`${configuredBaseUrl}${path}`, {
    ...init,
    headers,
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null
    const detail = payload?.detail
    const message = typeof detail === 'string'
      ? detail
      : Array.isArray(detail)
        ? detail
            .map((item) => {
              if (!item || typeof item !== 'object') return null
              const error = item as { loc?: unknown; msg?: unknown }
              const location = Array.isArray(error.loc) ? error.loc.slice(1).join('.') : ''
              const description = typeof error.msg === 'string' ? error.msg : ''
              return [location, description].filter(Boolean).join(': ')
            })
            .filter(Boolean)
            .join('; ')
        : ''
    throw new Error(message || `control-plane request failed (${response.status})`)
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}

export async function listAccountInstances(): Promise<AccountInstance[]> {
  return apiRequest<AccountInstance[]>('/instances')
}

export async function getVolumeSession(sessionId: string): Promise<VolumeSessionProjection> {
  return apiRequest<VolumeSessionProjection>(`/volume-sessions/${sessionId}`)
}

export async function syncVolumeSession(sessionId: string): Promise<VolumeSessionProjection> {
  return apiRequest<VolumeSessionProjection>(`/volume-sessions/${sessionId}/sync`, { method: 'POST' })
}

export async function reconcileVolumeSession(sessionId: string): Promise<VolumeSessionProjection> {
  return apiRequest<VolumeSessionProjection>(`/volume-sessions/${sessionId}/reconcile`, { method: 'POST' })
}

export async function fetchControlPlaneHealth(): Promise<ControlPlaneHealth> {
  return apiRequest<ControlPlaneHealth>('/health')
}

export async function previewBetaCampaign(
  account: AccountInstance,
  request: BetaCampaignPreviewRequest,
): Promise<BetaCampaignPreview> {
  if (!controlPlaneEnabled) throw new Error('Beta Campaign requires the control-plane API')
  return apiRequest<BetaCampaignPreview>(`/instances/${account.id}/beta-campaigns/preview`, {
    method: 'POST',
    body: JSON.stringify(request),
  })
}

export async function executeBetaCampaign(
  account: AccountInstance,
  campaignId: string,
  confirmation: string,
  riskAcknowledged: boolean,
): Promise<BetaCampaign> {
  return apiRequest<BetaCampaign>(`/instances/${account.id}/beta-campaigns/${campaignId}/execute`, {
    method: 'POST',
    body: JSON.stringify({ confirmation, riskAcknowledged }),
  })
}

export async function stopBetaCampaign(account: AccountInstance, campaign: BetaCampaign): Promise<BetaCampaign> {
  return apiRequest<BetaCampaign>(`/instances/${account.id}/beta-campaigns/${campaign.campaignId}/stop`, {
    method: 'POST',
    body: JSON.stringify({ confirmation: campaign.stopConfirmation }),
  })
}

export async function fetchBetaCampaignEvents(account: AccountInstance, campaignId: string): Promise<BetaCampaignEvent[]> {
  return apiRequest<BetaCampaignEvent[]>(`/instances/${account.id}/beta-campaigns/${campaignId}/events`)
}

export async function listVolumeStrategies(): Promise<VolumeStrategy[]> {
  if (!controlPlaneEnabled) return mockStrategies.map((strategy) => ({ ...strategy }))
  return apiRequest<VolumeStrategy[]>('/strategies')
}

export async function fetchBetaMarketSnapshot(): Promise<BetaMarketSnapshot> {
  if (controlPlaneEnabled) return apiRequest<BetaMarketSnapshot>('/beta')
  const now = Date.now()
  return {
    schemaVersion: '1.0',
    strategy: 'btc_long_eth_short',
    status: 'ok',
    upstreamUsable: true,
    reasonCodes: [],
    finalBeta: '0.44260456370165036',
    btcLongRatio: '1.0',
    ethShortRatio: '0.44260456370165036',
    btcLongWeight: '0.6931906533236318',
    ethShortWeight: '0.30680934667636806',
    confidence: '0.6793400100124344',
    confidenceThreshold: '0.65',
    source: 'beta_v2',
    asOfMs: now - 324,
    generatedAtMs: now,
    ageMs: '324',
    maxAgeMs: '10000',
  }
}

export function subscribeToInstanceEvents(
  onInstances: (instances: AccountInstance[], runtime?: SchedulerMetrics) => void,
  onConnectionChange: (connected: boolean) => void,
  onCampaigns?: (campaigns: BetaCampaign[]) => void,
): () => void {
  if (!configuredBaseUrl) return () => undefined
  const source = new EventSource(`${configuredBaseUrl}/events`)
  source.onopen = () => onConnectionChange(true)
  source.onerror = () => onConnectionChange(false)
  source.addEventListener('instances', (event) => {
    try {
      const snapshot = JSON.parse((event as MessageEvent<string>).data) as InstanceSnapshotEvent
      if (snapshot.type === 'instances') {
        onInstances(snapshot.instances, snapshot.runtime)
        if (snapshot.campaigns && onCampaigns) onCampaigns(snapshot.campaigns)
      }
    } catch {
      onConnectionChange(false)
    }
  })
  return () => source.close()
}

const mockLogStreams = new Map<string, LogLine[]>()

function mockLogStream(account: AccountInstance): LogLine[] {
  const existing = mockLogStreams.get(account.id)
  if (existing) return existing

  const now = Date.now()
  const line = (offset: number, level: LogLine['level'], message: string): LogLine => ({
    id: `${account.id}-initial-${offset}-${level}`,
    timestamp: new Date(now - offset * 1000).toISOString(),
    level,
    message,
  })
  const lines = account.status === 'error'
    ? [
        line(73, 'info', `实例 ${account.id} 已请求启动工作进程`),
        line(70, 'info', `正在检查代理连通性：${account.proxy.host}`),
        line(68, 'error', '代理认证失败，工作进程保持停止'),
        line(66, 'warn', '已禁用自动重试，需要人工处理'),
      ]
    : [
        line(58, 'info', `已收到实例 ${account.id} 的账号快照`),
        line(47, 'success', `合约钱包权益：${account.wallet.equity.toFixed(2)} USDT`),
        line(33, 'info', `累计交易量：${account.volume.lifetime.toFixed(2)} USDT，历史完整：${account.volume.complete ? '是' : '否'}`),
        line(21, 'info', `当前阶段：${account.phase}`),
        line(8, 'success', `代理正常，延迟：${account.proxy.latencyMs ?? '-'} ms`),
      ]
  mockLogStreams.set(account.id, lines)
  return lines
}

export async function fetchInstanceLogs(account: AccountInstance, after: string | null = null): Promise<LogBatch> {
  if (controlPlaneEnabled) {
    const query = new URLSearchParams({ limit: '200' })
    if (after) query.set('after', after)
    return apiRequest<LogBatch>(`/instances/${account.id}/log-updates?${query}`)
  }
  await sleep(520)
  const stream = mockLogStream(account)
  const sequence = stream.length + 1
  stream.push({
    id: `${account.id}-heartbeat-${sequence}`,
    timestamp: new Date().toISOString(),
    level: 'info',
    message: `状态心跳正常：${account.status}`,
  })
  if (stream.length > 500) stream.splice(0, stream.length - 500)

  if (!after) {
    const lines = stream.slice(-200)
    return { lines, cursor: lines.at(-1)?.id ?? null, reset: false }
  }
  const cursorIndex = stream.findIndex((line) => line.id === after)
  if (cursorIndex < 0) {
    const lines = stream.slice(-200)
    return { lines, cursor: lines.at(-1)?.id ?? null, reset: true }
  }
  const lines = stream.slice(cursorIndex + 1, cursorIndex + 201)
  return { lines, cursor: lines.at(-1)?.id ?? after, reset: false }
}

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

export async function refreshAccountSnapshot(account: AccountInstance): Promise<AccountInstance> {
  if (controlPlaneEnabled) return apiRequest<AccountInstance>(`/instances/${account.id}/refresh`, { method: 'POST' })
  await sleep(420)
  const completedAt = Date.now()
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
    },
    updatedAt: '刚刚',
  }
}

function proxyHost(value: string): string {
  try {
    return new URL(value.includes('://') ? value : `https://${value}`).host || '未配置'
  } catch {
    return value.replace(/^\w+:\/\//, '').split('@').at(-1) || '格式待验证'
  }
}

function historyStartAtMs(value: string): number | null {
  if (!value.trim()) return null
  const timestamp = new Date(value).getTime()
  return Number.isFinite(timestamp) ? timestamp : null
}

export function accountFromDraft(draft: AccountDraft, strategy: VolumeStrategy): AccountInstance {
  const id = `ins-${crypto.randomUUID().slice(0, 8)}`
  const initialWallet = { equity: 1000, available: 820, unrealizedPnl: 0 }
  const rounds = estimateRounds(strategy, 0)?.maximum ?? 1
  return {
    id,
    name: draft.name,
    accountTag: draft.accountTag || '未分组',
    apiKeyTail: draft.apiKey.slice(-4).toUpperCase() || '----',
    mode: draft.mode,
    status: 'stopped',
    phase: '等待连接验证',
    proxy: {
      type: draft.proxyType,
      host: proxyHost(draft.proxyUrl),
      location: '待检测',
      latencyMs: null,
      status: 'unchecked',
    },
    wallet: initialWallet,
    fundingPreflight: calculateFundingPreflight(strategy, initialWallet.available),
    volume: { lifetime: 0, today: 0, complete: false },
    exposure: { btcLong: 0, ethShort: 0 },
    cycle: { completed: 0, target: rounds, nextActionAt: null },
    strategyId: strategy.id,
    strategy,
    strategyProgress: {
      generatedVolumeQuote: '0',
      startedAtMs: null,
      stage: 'idle',
      nextActionAtMs: null,
      activeCycleId: null,
      lastEthRatio: null,
      lastAllocationVersion: null,
      systemPauseReason: null,
    },
    historyStartAtMs: historyStartAtMs(draft.historyStartAt),
    runtime: {
      lastPollStartedAtMs: null,
      lastPollSucceededAtMs: null,
      lastPollFailedAtMs: null,
      lastPollDurationMs: null,
      consecutiveFailures: 0,
      lastErrorType: null,
    },
    updatedAt: '尚未同步',
    unreadLogs: 0,
  }
}

export async function createAccountInstance(
  draft: AccountDraft,
  strategy: VolumeStrategy,
): Promise<AccountInstance> {
  if (!controlPlaneEnabled) return accountFromDraft(draft, strategy)
  return apiRequest<AccountInstance>('/instances', {
    method: 'POST',
    body: JSON.stringify({
      name: draft.name,
      accountTag: draft.accountTag,
      mode: draft.mode,
      strategyId: draft.strategyId,
      historyStartAtMs: historyStartAtMs(draft.historyStartAt),
      credentials: {
        apiKey: draft.apiKey,
        apiSecret: draft.apiSecret,
        passphrase: draft.passphrase,
      },
      proxy: {
        type: draft.proxyType,
        url: draft.proxyUrl,
      },
    }),
  })
}

export async function updateAccountInstance(
  instance: AccountInstance,
  draft: AccountDraft,
  strategy: VolumeStrategy,
): Promise<AccountInstance> {
  if (!controlPlaneEnabled) {
    const strategyChanged = instance.strategyId !== strategy.id
    const achieved = strategy.targetMode === 'lifetime'
      ? instance.volume.lifetime
      : strategyChanged ? 0 : Number(instance.strategyProgress.generatedVolumeQuote)
    const remainingRounds = estimateRounds(strategy, achieved)?.maximum ?? 1
    const complete = achieved >= Number(strategy.targetVolumeQuote)
    return {
      ...instance,
      name: draft.name,
      accountTag: draft.accountTag || '未分组',
      apiKeyTail: draft.apiKey ? draft.apiKey.slice(-4).toUpperCase() : instance.apiKeyTail,
      proxy: draft.proxyUrl ? {
        type: draft.proxyType,
        host: proxyHost(draft.proxyUrl),
        location: '待检测',
        latencyMs: null,
        status: 'unchecked',
      } : instance.proxy,
      strategyId: strategy.id,
      strategy,
      strategyProgress: strategyChanged ? {
        generatedVolumeQuote: '0',
        startedAtMs: null,
        stage: complete ? 'complete' : 'idle',
        nextActionAtMs: null,
        activeCycleId: null,
        lastEthRatio: null,
        lastAllocationVersion: null,
        systemPauseReason: null,
      } : instance.strategyProgress,
      cycle: strategyChanged ? {
        completed: instance.cycle.completed,
        target: instance.cycle.completed + remainingRounds,
        nextActionAt: null,
      } : instance.cycle,
      historyStartAtMs: historyStartAtMs(draft.historyStartAt),
      phase: complete ? '目标交易量已完成' : strategyChanged ? '策略已切换，等待启动' : '配置已更新，等待验证',
      fundingPreflight: calculateFundingPreflight(strategy, instance.wallet.available),
      updatedAt: '刚刚',
    }
  }
  const payload: Record<string, unknown> = {
    name: draft.name,
    accountTag: draft.accountTag,
    strategyId: draft.strategyId,
    historyStartAtMs: historyStartAtMs(draft.historyStartAt),
  }
  if (draft.apiKey && draft.apiSecret && draft.passphrase) {
    payload.credentials = {
      apiKey: draft.apiKey,
      apiSecret: draft.apiSecret,
      passphrase: draft.passphrase,
    }
  }
  if (draft.proxyUrl) {
    payload.proxy = {
      type: draft.proxyType,
      url: draft.proxyUrl,
    }
  }
  return apiRequest<AccountInstance>(`/instances/${instance.id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
}

function strategyPayload(draft: StrategyDraft) {
  return {
    name: draft.name,
    targetMode: draft.targetMode,
    targetVolumeQuote: draft.targetVolumeQuote,
    roundTurnoverQuoteMin: draft.roundTurnoverQuoteMin,
    roundTurnoverQuoteMax: draft.roundTurnoverQuoteMax,
    positionHoldMinSeconds: draft.positionHoldMinSeconds,
    positionHoldMaxSeconds: draft.positionHoldMaxSeconds,
    roundIntervalMinSeconds: draft.roundIntervalMinSeconds,
    roundIntervalMaxSeconds: draft.roundIntervalMaxSeconds,
  }
}

export async function createVolumeStrategy(draft: StrategyDraft): Promise<VolumeStrategy> {
  if (!controlPlaneEnabled) {
    return { id: `strategy-${crypto.randomUUID().slice(0, 8)}`, ...strategyPayload(draft) }
  }
  return apiRequest<VolumeStrategy>('/strategies', {
    method: 'POST',
    body: JSON.stringify(strategyPayload(draft)),
  })
}

export async function updateVolumeStrategy(
  strategy: VolumeStrategy,
  draft: StrategyDraft,
): Promise<VolumeStrategy> {
  if (!controlPlaneEnabled) return { id: strategy.id, ...strategyPayload(draft) }
  return apiRequest<VolumeStrategy>(`/strategies/${strategy.id}`, {
    method: 'PATCH',
    body: JSON.stringify(strategyPayload(draft)),
  })
}

export async function deleteVolumeStrategy(strategyId: string): Promise<void> {
  if (!controlPlaneEnabled) return
  await apiRequest<void>(`/strategies/${strategyId}`, { method: 'DELETE' })
}

export async function assignVolumeStrategy(
  strategy: VolumeStrategy,
  accounts: AccountInstance[],
): Promise<StrategyAssignmentResult> {
  if (controlPlaneEnabled) {
    return apiRequest<StrategyAssignmentResult>(`/strategies/${strategy.id}/assign`, {
      method: 'POST',
      body: JSON.stringify({ instanceIds: accounts.map((account) => account.id) }),
    })
  }
  const blocked = accounts.find((account) => (
    account.status !== 'stopped'
    || account.strategyProgress.stage === 'holding'
    || account.exposure.btcLong !== 0
    || account.exposure.ethShort !== 0
  ))
  if (blocked) throw new Error(`请先停止 ${blocked.name} 并完成双腿平仓`)
  const progress = (account: AccountInstance) => strategy.targetMode === 'lifetime' ? account.volume.lifetime : 0
  return {
    strategy,
    instances: accounts.map((account) => ({
      ...account,
      strategyId: strategy.id,
      strategy,
      strategyProgress: {
        generatedVolumeQuote: '0',
        startedAtMs: null,
        stage: progress(account) >= Number(strategy.targetVolumeQuote) ? 'complete' : 'idle',
        nextActionAtMs: null,
        activeCycleId: null,
        lastEthRatio: null,
        lastAllocationVersion: null,
        systemPauseReason: null,
      },
      cycle: {
        completed: account.cycle.completed,
        target: account.cycle.completed + (estimateRounds(strategy, progress(account))?.maximum ?? 1),
        nextActionAt: null,
      },
      fundingPreflight: calculateFundingPreflight(strategy, account.wallet.available),
      phase: progress(account) >= Number(strategy.targetVolumeQuote) ? '目标交易量已完成' : '策略已更新，等待启动',
      updatedAt: '刚刚',
    })),
  }
}

export async function deleteAccountInstance(instanceId: string): Promise<void> {
  if (!controlPlaneEnabled) return
  await apiRequest<void>(`/instances/${instanceId}`, { method: 'DELETE' })
}

export async function applyInstanceAction(
  instanceId: string,
  action: 'start' | 'pause' | 'stop',
): Promise<AccountInstance> {
  return apiRequest<AccountInstance>(`/instances/${instanceId}/actions/${action}`, { method: 'POST' })
}

export async function closeAccountPositions(account: AccountInstance): Promise<AccountInstance> {
  if (controlPlaneEnabled) {
    return apiRequest<AccountInstance>(`/instances/${account.id}/positions/close`, { method: 'POST' })
  }
  if (account.status === 'running') throw new Error('请先暂停或停止策略，再执行一键平仓')
  const btcQuote = Math.max(0, account.exposure.btcLong)
  const ethQuote = Math.max(0, account.exposure.ethShort)
  const closingVolume = btcQuote + ethQuote
  if (closingVolume <= 0) throw new Error('当前账号没有可平仓仓位')
  await sleep(560)

  const lifetime = account.volume.lifetime + closingVolume
  const today = account.volume.today + closingVolume
  const generated = account.strategy.targetMode === 'incremental'
    ? Number(account.strategyProgress.generatedVolumeQuote) + closingVolume
    : Number(account.strategyProgress.generatedVolumeQuote)
  const targetProgress = account.strategy.targetMode === 'lifetime' ? lifetime : generated
  const targetReached = targetProgress >= Number(account.strategy.targetVolumeQuote)
  const activeCycleClosed = account.strategyProgress.stage === 'holding'
    && account.strategyProgress.activeCycleId !== null
  const status = targetReached && ['paused', 'stopped'].includes(account.status) ? 'stopped' : account.status
  const phase = targetReached
    ? '一键平仓完成；目标交易量已完成'
    : status === 'paused'
      ? '一键平仓完成；策略保持暂停'
      : ['warning', 'error'].includes(status)
        ? '一键平仓完成；原状态保留待处理'
        : '一键平仓完成；策略保持停止'
  const updated: AccountInstance = {
    ...account,
    status,
    phase,
    volume: { ...account.volume, lifetime, today },
    exposure: { btcLong: 0, ethShort: 0 },
    cycle: {
      ...account.cycle,
      completed: activeCycleClosed ? account.cycle.completed + 1 : account.cycle.completed,
      nextActionAt: null,
    },
    strategyProgress: {
      ...account.strategyProgress,
      generatedVolumeQuote: String(generated),
      stage: targetReached ? 'complete' : activeCycleClosed ? 'cooldown' : 'idle',
      nextActionAtMs: null,
      activeCycleId: null,
    },
    fundingPreflight: calculateFundingPreflight(account.strategy, account.wallet.available, true),
    updatedAt: '刚刚',
    unreadLogs: account.unreadLogs + 1,
  }
  mockLogStream(account).push({
    id: `${account.id}-position-close-${Date.now()}`,
    timestamp: new Date().toISOString(),
    level: 'success',
    message: `一键平仓完成：BTC ${btcQuote.toFixed(2)} + ETH ${ethQuote.toFixed(2)} USDT；策略未恢复`,
  })
  return updated
}

export async function stopAllInstances(): Promise<GlobalStopResult> {
  return apiRequest<GlobalStopResult>('/actions/stop-all', {
    method: 'POST',
    body: JSON.stringify({ confirmation: 'STOP ALL' }),
  })
}
