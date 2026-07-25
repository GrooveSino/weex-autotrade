import { useCallback, useEffect, useRef } from 'react'
import { fetchStrategyMonitor } from '../../services'
import type { AccountInstance, StrategyMonitorSnapshot } from '../../types'
import { unreconciledExpiredWaitKeys } from './monitorRuntimeStage'

interface Options {
  account: AccountInstance | null
  sessionId: string | null
  monitor: StrategyMonitorSnapshot | null
  serverNowMs: number
  onSnapshot: (snapshot: StrategyMonitorSnapshot) => void
  onError: (message: string) => void
}

const ACTIVE_STATUSES = new Set(['planned', 'executing', 'stopping', 'recovering', 'uncertain'])
const TERMINAL_ACTOR_STATES = new Set(['completed', 'stopped', 'failed'])

export function useMonitorReconciliation(options: Options): void {
  const { account, monitor, serverNowMs, sessionId } = options
  const optionsRef = useRef(options)
  const inFlightRef = useRef(false)
  const reconciledDeadlinesRef = useRef(new Set<string>())
  optionsRef.current = options

  const reconcile = useCallback(async () => {
    const current = optionsRef.current
    if (!current.account || !isActive(current.monitor) || inFlightRef.current) return
    const requestKey = `${current.account.id}:${current.sessionId ?? ''}`
    inFlightRef.current = true
    try {
      const latest = await fetchStrategyMonitor(current.account, null, current.sessionId)
      const selected = optionsRef.current
      if (`${selected.account?.id ?? ''}:${selected.sessionId ?? ''}` === requestKey) {
        selected.onSnapshot(latest)
      }
    } catch (reason: unknown) {
      const selected = optionsRef.current
      if (`${selected.account?.id ?? ''}:${selected.sessionId ?? ''}` === requestKey) {
        selected.onError(reason instanceof Error ? reason.message : '监控快照核对失败')
      }
    } finally {
      inFlightRef.current = false
    }
  }, [])

  useEffect(() => {
    const timer = window.setInterval(() => void reconcile(), 2_000)
    return () => window.clearInterval(timer)
  }, [reconcile, account?.id, sessionId])

  useEffect(() => {
    reconciledDeadlinesRef.current.clear()
  }, [account?.id, monitor?.executorGeneration, monitor?.executionId, sessionId])

  useEffect(() => {
    if (!monitor || !isActive(monitor)) return
    const executionKey = `${monitor.executorGeneration}:${monitor.executionId ?? 'idle'}`
    const keys = unreconciledExpiredWaitKeys(
      monitor.activeWaits,
      serverNowMs,
      executionKey,
      reconciledDeadlinesRef.current,
    )
    for (const key of keys) {
      reconciledDeadlinesRef.current.add(key)
    }
    if (keys.length) void reconcile()
  }, [monitor, serverNowMs, reconcile])
}

function isActive(monitor: StrategyMonitorSnapshot | null): boolean {
  if (!monitor) return false
  if (monitor.executionState && !TERMINAL_ACTOR_STATES.has(monitor.executionState)) return true
  return ACTIVE_STATUSES.has(monitor.status)
}
