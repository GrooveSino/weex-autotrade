import {
  controlPlaneEnabled,
  fetchLocalUserSession,
  listAccountInstances,
  loginLocalUser,
  logoutLocalUser,
  stopAllInstances,
} from '../services'
import type { FleetState } from './fleetAppState'

export function useFleetSessionActions(state: FleetState, executionDisabled: boolean) {
  const {
    setAccounts, setStrategies, setSelectedIds, stopPhrase,
    setStopDialogOpen, setStopPhrase, setToast, setLocalUserError,
    setLocalUser, setInitialControlPlaneSnapshotLoaded, setInitialControlPlaneError,
  } = state

  const emergencyStop = async () => {
    if (stopPhrase !== 'STOP ALL') return
    if (controlPlaneEnabled) {
      try {
        const result = await stopAllInstances()
        const snapshot = await listAccountInstances().catch(() => null)
        if (snapshot) setAccounts(snapshot)
        setStopDialogOpen(false)
        setStopPhrase('')
        setToast(result.cancelFailed > 0
          ? `已停止 ${result.stopped} 个实例；${result.cancelFailed} 个账号撤单未核验`
          : `全部停止完成；${result.cancelVerified} 个账号撤单核验通过`)
        return
      } catch (error) {
        setToast(error instanceof Error ? error.message : '全局停止失败')
        return
      }
    }
    setAccounts((current) => current.map((account) => ({
      ...account,
      status: 'stopped',
      phase: '全局停止已触发',
      cycle: { ...account.cycle, nextActionAt: null },
      strategyProgress: { ...account.strategyProgress, systemPauseReason: null },
    })))
    setStopDialogOpen(false)
    setStopPhrase('')
    setToast(executionDisabled ? '普通策略实例已停止' : '所有模拟实例已停止')
  }

  const login = async (username: string, password: string) => {
    setLocalUserError(null)
    try {
      const session = await loginLocalUser(username, password)
      setAccounts([])
      setStrategies([])
      setInitialControlPlaneSnapshotLoaded(false)
      setInitialControlPlaneError(null)
      setLocalUser(session.userId)
    } catch (error) {
      setLocalUserError(error instanceof Error ? error.message : '本机登录失败')
    }
  }

  const logout = async () => {
    try {
      await logoutLocalUser()
    } finally {
      setLocalUser(null)
      setAccounts([])
      setStrategies([])
      setSelectedIds(new Set())
      setInitialControlPlaneSnapshotLoaded(false)
    }
  }

  return { emergencyStop, login, logout, fetchLocalUserSession }
}
