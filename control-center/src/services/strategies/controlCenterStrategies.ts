import type {
  AccountInstance, StrategyAssignmentResult, StrategyDraft, VolumeStrategy,
} from '../../types'
import { calculateFundingPreflight, estimateRounds } from '../../utils/strategy'
import { apiRequest, controlPlaneEnabled } from '../core/controlCenterCore'

function strategyPayload(draft: StrategyDraft) {
  return {
    name: draft.name,
    direction: draft.direction,
    targetMode: draft.targetMode,
    targetVolumeQuote: draft.targetVolumeQuoteMax,
    targetVolumeQuoteMin: draft.targetVolumeQuoteMin,
    targetVolumeQuoteMax: draft.targetVolumeQuoteMax,
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
    return { id: `strategy-${crypto.randomUUID().slice(0, 8)}`, version: 1, ...strategyPayload(draft) }
  }
  return apiRequest<VolumeStrategy>('/strategies', {
    method: 'POST',
    body: JSON.stringify(strategyPayload(draft)),
  })
}

export async function duplicateVolumeStrategy(strategy: VolumeStrategy): Promise<VolumeStrategy> {
  if (!controlPlaneEnabled) {
    return {
      ...strategy,
      id: `strategy-${crypto.randomUUID().slice(0, 8)}`,
      name: `${strategy.name} 复制`.slice(0, 64),
      version: 1,
    }
  }
  return apiRequest<VolumeStrategy>(`/strategies/${strategy.id}/duplicate`, { method: 'POST' })
}

export async function updateVolumeStrategy(
  strategy: VolumeStrategy,
  draft: StrategyDraft,
): Promise<VolumeStrategy> {
  if (!controlPlaneEnabled) return { id: strategy.id, version: strategy.version + 1, ...strategyPayload(draft) }
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
