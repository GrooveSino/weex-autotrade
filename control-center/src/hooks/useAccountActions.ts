import {
  applyInstanceAction,
  closeAccountPositions,
  controlPlaneEnabled,
  createAccountInstance,
  deleteAccountInstance,
  refreshAccountSnapshot,
  updateAccountInstance,
} from '../services'
import type { AccountDraft, AccountInstance, InstanceStatus } from '../types'
import { calculateFundingPreflight } from '../utils/strategy'
import type { FleetState } from './fleetAppState'

export function useAccountActions(
  state: FleetState,
  filteredAccounts: AccountInstance[],
  executionDisabled: boolean,
) {
  const {
    accounts, setAccounts, strategies, selectedIds, setSelectedIds,
    setRefreshingIds, setActioningIds, setBoundExecutionQueue,
    boundStrategyExecutionEnabled, setToast, actioningIdsRef,
    setLastGlobalSync, setClosePositionsAccount, editingAccount,
    setLogAccount, setExecutionAccount, setAccountDialogOpen, setEditingAccount,
  } = state

  const selectOne = (id: string, selected: boolean) => {
    setSelectedIds((current) => {
      const next = new Set(current)
      if (selected) next.add(id)
      else next.delete(id)
      return next
    })
  }

  const selectAllVisible = (selected: boolean) => {
    setSelectedIds((current) => {
      const next = new Set(current)
      filteredAccounts.forEach((account) => {
        if (selected) next.add(account.id)
        else next.delete(account.id)
      })
      return next
    })
  }

  const updateStatuses = async (ids: Set<string>, status: InstanceStatus) => {
    const allCandidates = accounts.filter((account) => {
      if (!ids.has(account.id) || actioningIdsRef.current.has(account.id)) return false
      if (status === 'running') return account.status === 'paused' || account.status === 'stopped'
      if (status === 'paused') return account.status === 'running'
      return account.status !== 'stopped' || account.runtime.lastStopVerifiedAtMs === null
    })
    const liveCandidates = allCandidates.filter((account) => account.mode === 'live')
    if (liveCandidates.length) {
      if (!boundStrategyExecutionEnabled) {
        setToast('实盘执行器未连接或实盘门禁未启用；未把普通策略启动降级为实盘下单')
      } else {
        setBoundExecutionQueue(liveCandidates)
      }
    }
    const candidates = allCandidates.filter((account) => account.mode !== 'live')
    if (!candidates.length) return
    if (executionDisabled && status !== 'stopped') {
      setToast('当前控制面不允许启动或暂停模拟策略实例')
      return
    }

    const claimedIds = new Set(candidates.map((account) => account.id))
    claimedIds.forEach((id) => actioningIdsRef.current.add(id))
    setActioningIds(new Set(actioningIdsRef.current))
    try {
      if (controlPlaneEnabled) {
        const action = status === 'running' ? 'start' : status === 'paused' ? 'pause' : 'stop'
        const results = await Promise.all(candidates.map(async (account) => {
          try {
            return { id: account.id, instance: await applyInstanceAction(account.id, action), error: null }
          } catch (error) {
            return { id: account.id, instance: null, error }
          }
        }))
        const replacements = new Map(
          results.filter((result) => result.instance).map((result) => [result.id, result.instance as AccountInstance]),
        )
        setAccounts((current) => current.map((account) => replacements.get(account.id) ?? account))
        const failure = results.find((result) => result.error)?.error
        if (failure) setToast(failure instanceof Error ? failure.message : '实例操作失败')
        return
      }

      const candidateIds = new Set(candidates.map((account) => account.id))
      setAccounts((current) => current.map((account) => {
        if (!candidateIds.has(account.id)) return account
        if (status === 'running' && ['error', 'warning'].includes(account.status)) return account
        const funding = account.fundingPreflight ?? calculateFundingPreflight(
          account.strategy,
          account.wallet.available,
          account.wallet.equity > 0 || account.wallet.available > 0,
        )
        const openingNewPair = account.strategyProgress.stage !== 'holding'
        if (status === 'running' && openingNewPair && funding.status !== 'ready') return account
        if (status === 'running' && openingNewPair
          && account.strategy.targetMode === 'lifetime' && !account.volume.complete) return account
        const progress = status === 'running'
          && account.strategy.targetMode === 'incremental'
          && account.strategyProgress.startedAtMs === null
          ? { ...account.strategyProgress, startedAtMs: Date.now() }
          : account.strategyProgress
        return {
          ...account,
          status,
          phase: status === 'running' ? '正在初始化运行周期' : status === 'paused' ? '已人工暂停' : '等待启动',
          strategyProgress: status === 'running' ? progress : { ...progress, systemPauseReason: null },
          cycle: { ...account.cycle, nextActionAt: status === 'running' ? '10s' : null },
          fundingPreflight: funding,
        }
      }))
    } finally {
      claimedIds.forEach((id) => actioningIdsRef.current.delete(id))
      setActioningIds(new Set(actioningIdsRef.current))
    }
  }

  const toggleRunning = (account: AccountInstance) => {
    if (account.mode === 'live') {
      if (!boundStrategyExecutionEnabled) {
        setToast('实盘执行器未连接或实盘门禁未启用')
        return
      }
      setBoundExecutionQueue([account])
      return
    }
    if (executionDisabled) {
      setToast('当前控制面不允许启动或暂停模拟策略实例')
      return
    }
    if (account.status === 'error' || account.status === 'warning') {
      setToast(account.status === 'warning' ? '数据待核验，请修复连接后刷新' : '请先处理实例错误')
      return
    }
    void updateStatuses(new Set([account.id]), account.status === 'running' ? 'paused' : 'running')
  }

  const refreshOne = async (account: AccountInstance) => {
    setRefreshingIds((current) => new Set(current).add(account.id))
    try {
      const refreshed = await refreshAccountSnapshot(account)
      setAccounts((current) => current.map((item) => item.id === account.id ? refreshed : item))
    } catch (error) {
      setToast(error instanceof Error ? error.message : '刷新实例失败')
    } finally {
      setRefreshingIds((current) => {
        const next = new Set(current)
        next.delete(account.id)
        return next
      })
    }
  }

  const refreshAll = async () => {
    setRefreshingIds(new Set(accounts.map((account) => account.id)))
    try {
      const refreshed = await Promise.all(accounts.map(refreshAccountSnapshot))
      setAccounts(refreshed)
      setLastGlobalSync('刚刚')
      setToast(`已刷新 ${refreshed.length} 个实例`)
    } catch (error) {
      setToast(error instanceof Error ? error.message : '批量刷新失败')
    } finally {
      setRefreshingIds(new Set())
    }
  }

  const confirmClosePositions = async (account: AccountInstance) => {
    const current = accounts.find((item) => item.id === account.id)
    if (!current) throw new Error('账号实例已不存在')
    if (current.status === 'running') throw new Error('策略已恢复运行，请先暂停或停止后再平仓')
    if (current.exposure.btcLong <= 0 && current.exposure.ethShort <= 0) {
      throw new Error('当前账号已经没有可平仓仓位')
    }
    const updated = await closeAccountPositions(current)
    setAccounts((items) => items.map((item) => item.id === updated.id ? updated : item))
    setClosePositionsAccount(null)
    setToast(`${updated.name} 已完成一键平仓，策略保持非运行`)
  }

  const saveAccount = async (draft: AccountDraft): Promise<boolean> => {
    try {
      const strategy = strategies.find((item) => item.id === draft.strategyId)
      if (!strategy) throw new Error('所选策略不存在，请重新选择')
      const saved = editingAccount
        ? await updateAccountInstance(editingAccount, draft, strategy)
        : await createAccountInstance(draft, strategy)
      setAccounts((current) => current.some((account) => account.id === saved.id)
        ? current.map((account) => account.id === saved.id ? saved : account)
        : [saved, ...current])
      setToast(editingAccount ? '账号配置已更新' : '账号实例已加入待验证队列')
      return true
    } catch (error) {
      setToast(error instanceof Error ? error.message : '保存账号失败')
      return false
    }
  }

  const deleteEditingAccount = async (): Promise<boolean> => {
    if (!editingAccount) return false
    try {
      await deleteAccountInstance(editingAccount.id)
      setAccounts((current) => current.filter((account) => account.id !== editingAccount.id))
      setSelectedIds((current) => {
        const next = new Set(current)
        next.delete(editingAccount.id)
        return next
      })
      setLogAccount((current) => current?.id === editingAccount.id ? null : current)
      setExecutionAccount((current) => current?.id === editingAccount.id ? null : current)
      setToast('账号实例已删除')
      return true
    } catch (error) {
      setToast(error instanceof Error ? error.message : '删除账号失败')
      return false
    }
  }

  const closeAccountDialog = () => {
    setAccountDialogOpen(false)
    setEditingAccount(null)
  }

  return {
    selectOne, selectAllVisible, updateStatuses, toggleRunning, refreshOne, refreshAll,
    confirmClosePositions, saveAccount, deleteEditingAccount, closeAccountDialog, selectedIds,
  }
}
