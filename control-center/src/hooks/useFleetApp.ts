import { useMemo } from 'react'
import { controlPlaneEnabled } from '../services'
import { targetProgress } from '../utils/strategy'
import { useFleetState } from './fleetAppState'
import { useAccountActions } from './useAccountActions'
import { useFleetSessionActions } from './useFleetSessionActions'
import { useFleetSynchronization } from './useFleetSynchronization'
import { useStrategyActions } from './useStrategyActions'

export function useFleetApp() {
  const state = useFleetState()
  const {
    accounts, search, filter, selectedIds, actioningIds,
    localUser, localUserLoading, initialControlPlaneSnapshotLoaded,
    controlPlaneExecutionEnabled, accountDialogOpen, strategyDialogOpen,
    assignmentAccounts, closePositionsAccount, stopDialogOpen, boundExecutionQueue,
  } = state

  useFleetSynchronization(state)

  const controlPlaneHydrating = controlPlaneEnabled
    && Boolean(localUser)
    && !initialControlPlaneSnapshotLoaded
  const executionDisabled = controlPlaneEnabled && !controlPlaneExecutionEnabled
  const refreshBlockedByWorkflow = Boolean(
    accountDialogOpen || strategyDialogOpen || assignmentAccounts
    || closePositionsAccount || stopDialogOpen || boundExecutionQueue,
  )
  const filteredAccounts = useMemo(() => {
    const query = search.trim().toLowerCase()
    return accounts.filter((account) => {
      const matchesFilter = filter === 'all' || account.status === filter
      const matchesSearch = !query || [
        account.name, account.accountTag, account.id, account.proxy.host,
        account.proxy.location, account.strategy.name,
      ].some((value) => value.toLowerCase().includes(query))
      return matchesFilter && matchesSearch
    })
  }, [accounts, filter, search])
  const stats = useMemo(() => ({
    running: accounts.filter((account) => account.status === 'running').length,
    alerts: accounts.filter((account) => ['warning', 'error'].includes(account.status)).length,
    healthyProxies: accounts.filter((account) => account.proxy.status === 'healthy').length,
    equity: accounts.reduce((sum, account) => sum + account.wallet.equity, 0),
    lifetimeVolume: accounts.reduce((sum, account) => sum + account.volume.lifetime, 0),
    todayVolume: accounts.reduce((sum, account) => sum + account.volume.today, 0),
    strategyTarget: accounts.reduce((sum, account) => sum + Number(
      account.volume.strategyTargetQuoteVolume ?? account.strategy.targetVolumeQuote,
    ), 0),
    strategyGenerated: accounts.reduce((sum, account) => sum + Number(
      account.volume.strategyVerifiedQuoteVolume ?? targetProgress(account),
    ), 0),
  }), [accounts])
  const selectedAccounts = accounts.filter((account) => selectedIds.has(account.id))
  const canStartSelected = selectedAccounts.some((account) => (
    !actioningIds.has(account.id) && (account.status === 'paused' || account.status === 'stopped')
  ))
  const canPauseSelected = selectedAccounts.some((account) => (
    !actioningIds.has(account.id) && account.status === 'running'
  ))
  const canStopSelected = selectedAccounts.some((account) => (
    !actioningIds.has(account.id)
    && (account.status !== 'stopped' || account.runtime.lastStopVerifiedAtMs === null)
  ))

  const accountActions = useAccountActions(state, filteredAccounts, executionDisabled)
  const strategyActions = useStrategyActions(state)
  const sessionActions = useFleetSessionActions(state, executionDisabled)
  const loginRequired = controlPlaneEnabled && (localUserLoading || !localUser)

  return {
    ...state, ...accountActions, ...strategyActions, ...sessionActions,
    controlPlaneHydrating, executionDisabled, refreshBlockedByWorkflow,
    filteredAccounts, stats, canStartSelected, canPauseSelected, canStopSelected,
    loginRequired,
  }
}
