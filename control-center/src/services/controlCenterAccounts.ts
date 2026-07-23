import type { AccountDraft, AccountInstance, GlobalStopResult, VolumeStrategy } from '../types'
import { calculateFundingPreflight, estimateRounds } from '../utils/strategy'
import { apiRequest, controlPlaneEnabled, sleep } from './controlCenterCore'
import { mockLogStream } from './controlCenterStreams'

function proxyHost(value: string): string {
  if (!value.trim()) return '不使用代理'
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
      host: draft.proxyType === 'none' ? '不使用代理' : proxyHost(draft.proxyUrl),
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
    executionLifecycle: {
      state: 'idle', primaryAction: 'start', executionId: null, sessionId: null,
      reasonCode: null, positionCount: 0, regularOrderCount: 0, triggerOrderCount: 0,
    },
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
      lastStopVerifiedAtMs: null,
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
        url: draft.proxyType === 'none' ? undefined : draft.proxyUrl,
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
      proxy: draft.proxyType === 'none' ? {
        type: 'none',
        host: '不使用代理',
        location: '直连',
        latencyMs: null,
        status: 'unchecked',
      } : draft.proxyUrl ? {
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
  }
  if (instance.status !== 'error') {
    payload.strategyId = draft.strategyId
    payload.historyStartAtMs = historyStartAtMs(draft.historyStartAt)
  }
  if (draft.apiKey && draft.apiSecret && draft.passphrase) {
    payload.credentials = {
      apiKey: draft.apiKey,
      apiSecret: draft.apiSecret,
      passphrase: draft.passphrase,
    }
  }
  if (draft.proxyType === 'none' || draft.proxyUrl) {
    payload.proxy = {
      type: draft.proxyType,
      url: draft.proxyType === 'none' ? undefined : draft.proxyUrl,
    }
  }
  return apiRequest<AccountInstance>(`/instances/${instance.id}`, {
    method: 'PATCH',
    body: JSON.stringify(payload),
  })
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
