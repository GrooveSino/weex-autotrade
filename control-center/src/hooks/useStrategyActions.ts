import {
  assignVolumeStrategy,
  createVolumeStrategy,
  deleteVolumeStrategy,
  updateVolumeStrategy,
} from '../services'
import type { StrategyDraft, VolumeStrategy } from '../types'
import { calculateFundingPreflight, estimateRounds, targetProgress } from '../utils/strategy'
import type { FleetState } from './fleetAppState'

export function useStrategyActions(state: FleetState) {
  const {
    setStrategies, setAccounts, setToast, assignmentAccounts,
  } = state

  const createStrategy = async (draft: StrategyDraft): Promise<VolumeStrategy | null> => {
    try {
      const created = await createVolumeStrategy(draft)
      setStrategies((current) => [...current, created])
      setToast('共享策略已创建')
      return created
    } catch (error) {
      setToast(error instanceof Error ? error.message : '创建策略失败')
      return null
    }
  }

  const updateStrategy = async (
    strategy: VolumeStrategy,
    draft: StrategyDraft,
  ): Promise<VolumeStrategy | null> => {
    try {
      const updated = await updateVolumeStrategy(strategy, draft)
      setStrategies((current) => current.map((item) => item.id === updated.id ? updated : item))
      setAccounts((current) => current.map((account) => {
        if (account.strategyId !== updated.id) return account
        const modeChanged = account.strategy.targetMode !== updated.targetMode
        const strategyProgress = modeChanged ? {
          ...account.strategyProgress,
          generatedVolumeQuote: '0',
          startedAtMs: null,
        } : account.strategyProgress
        const projected = { ...account, strategy: updated, strategyProgress }
        const achieved = targetProgress(projected)
        const estimate = estimateRounds(updated, achieved)
        const complete = achieved >= Number(updated.targetVolumeQuote)
        return {
          ...account,
          strategy: updated,
          strategyProgress: {
            ...strategyProgress,
            stage: complete ? 'complete' as const : 'idle' as const,
            nextActionAtMs: null,
            activeCycleId: null,
          },
          fundingPreflight: calculateFundingPreflight(updated, account.wallet.available),
          cycle: {
            ...account.cycle,
            target: Math.max(1, account.cycle.completed + (estimate?.maximum ?? 0)),
            nextActionAt: null,
          },
          phase: complete ? '目标交易量已完成' : '策略已更新，等待启动',
          updatedAt: '刚刚',
        }
      }))
      setToast('共享策略已更新')
      return updated
    } catch (error) {
      setToast(error instanceof Error ? error.message : '更新策略失败')
      return null
    }
  }

  const deleteStrategy = async (strategy: VolumeStrategy): Promise<boolean> => {
    try {
      await deleteVolumeStrategy(strategy.id)
      setStrategies((current) => current.filter((item) => item.id !== strategy.id))
      setToast('共享策略已删除')
      return true
    } catch (error) {
      setToast(error instanceof Error ? error.message : '删除策略失败')
      return false
    }
  }

  const assignStrategy = async (strategy: VolumeStrategy): Promise<boolean> => {
    if (!assignmentAccounts?.length) return false
    try {
      const result = await assignVolumeStrategy(strategy, assignmentAccounts)
      const replacements = new Map(result.instances.map((instance) => [instance.id, instance]))
      setAccounts((current) => current.map((account) => replacements.get(account.id) ?? account))
      setToast(`已为 ${result.instances.length} 个账号应用策略`)
      return true
    } catch (error) {
      setToast(error instanceof Error ? error.message : '应用策略失败')
      return false
    }
  }

  return { createStrategy, updateStrategy, deleteStrategy, assignStrategy }
}
