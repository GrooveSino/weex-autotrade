import { useEffect } from 'react'
import {
  controlPlaneEnabled,
  fetchBetaMarketSnapshot,
  fetchControlPlaneHealth,
  fetchExecutionCapacity,
  fetchLocalUserSession,
  listAccountInstances,
  listVolumeStrategies,
  subscribeToInstanceEvents,
} from '../services/controlCenter'
import type { FleetState } from './fleetAppState'

const betaPollIntervalMs = 250
const betaRetryIntervalMs = 1_000
const betaRefreshIndicatorThresholdMs = 1_000

export function useFleetSynchronization(state: FleetState) {
  const {
    accounts, setAccounts, search, filter, setStrategies, toast, setToast,
    localUser, setLocalUser, setLocalUserError, setLocalUserLoading,
    setControlPlaneConnected, setControlPlaneAdapter, setControlPlaneExecutionEnabled,
    setBoundStrategyExecutionEnabled, setInitialControlPlaneError,
    setExecutionCapacity,
    setInitialControlPlaneSnapshotLoaded, setSchedulerMetrics, setLastGlobalSync,
    setBetaLoading, setBetaAvailable, setBetaSnapshot, setBetaReceivedAtMs,
    setPendingWebReleaseId, searchInputRef, betaSnapshotRef, betaReceivedAtRef,
    webReleaseIdRef,
  } = state

  useEffect(() => {
    if (!controlPlaneEnabled) return
    let active = true
    void fetchLocalUserSession()
      .then((session) => {
        if (!active) return
        setLocalUser(session.userId)
        setLocalUserError(null)
      })
      .catch(() => { if (active) setLocalUser(null) })
      .finally(() => { if (active) setLocalUserLoading(false) })
    return () => { active = false }
  }, [localUser, setLocalUser, setLocalUserError, setLocalUserLoading])

  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'k') return
      event.preventDefault()
      searchInputRef.current?.focus()
      searchInputRef.current?.select()
    }
    window.addEventListener('keydown', focusSearch)
    return () => window.removeEventListener('keydown', focusSearch)
  }, [searchInputRef])

  useEffect(() => {
    sessionStorage.setItem('weex-fleet.search', search)
    sessionStorage.setItem('weex-fleet.filter', filter)
  }, [filter, search])

  useEffect(() => {
    if (!controlPlaneEnabled || !localUser) return
    let active = true
    let timer: number | undefined
    const pollWebRelease = async () => {
      try {
        const response = await fetch(`${import.meta.env.BASE_URL}__fleet/version.json`, { cache: 'no-store' })
        if (!response.ok) return
        const payload = await response.json() as { release_id?: unknown; releaseId?: unknown }
        const releaseId = typeof payload.release_id === 'string'
          ? payload.release_id
          : typeof payload.releaseId === 'string' ? payload.releaseId : null
        if (releaseId && active) {
          if (webReleaseIdRef.current && webReleaseIdRef.current !== releaseId) setPendingWebReleaseId(releaseId)
          webReleaseIdRef.current ??= releaseId
        }
      } catch {
        // Static release checks must not affect the trading console connection.
      } finally {
        if (active) timer = window.setTimeout(() => void pollWebRelease(), 60_000)
      }
    }
    void pollWebRelease()
    return () => {
      active = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [localUser, setPendingWebReleaseId, webReleaseIdRef])

  useEffect(() => {
    if (controlPlaneEnabled) return
    const timer = window.setInterval(() => {
      setAccounts((current) => current.map((account) => account.status === 'running' ? {
        ...account,
        volume: {
          ...account.volume,
          lifetime: account.volume.lifetime + 18.6,
          today: account.volume.today + 18.6,
        },
        strategyProgress: account.strategy.targetMode === 'incremental' ? {
          ...account.strategyProgress,
          generatedVolumeQuote: (Number(account.strategyProgress.generatedVolumeQuote) + 18.6).toFixed(2),
        } : account.strategyProgress,
        updatedAt: '刚刚',
        runtime: {
          ...account.runtime,
          lastPollStartedAtMs: Date.now() - 120,
          lastPollSucceededAtMs: Date.now(),
          lastPollDurationMs: 120,
          consecutiveFailures: 0,
          lastErrorType: null,
        },
      } : account))
      setSchedulerMetrics((current) => ({
        ...current,
        pollRounds: current.pollRounds + 1,
        maxObservedParallelism: Math.min(current.maxParallelPolls, accounts.length),
        accountsPolled: current.accountsPolled + accounts.length,
        successfulPolls: current.successfulPolls + accounts.length,
        lastRoundAccountCount: accounts.length,
        lastRoundSucceeded: accounts.length,
        lastRoundFailed: 0,
        lastRoundStartedAtMs: Date.now() - 221,
        lastRoundCompletedAtMs: Date.now(),
      }))
    }, 2400)
    return () => window.clearInterval(timer)
  }, [accounts.length, setAccounts, setSchedulerMetrics])

  useEffect(() => {
    if (!controlPlaneEnabled || !localUser) return
    let active = true
    let retryTimer: number | undefined
    const loadControlPlane = async () => {
      try {
        const [instances, health, availableStrategies] = await Promise.all([
          listAccountInstances(), fetchControlPlaneHealth(), listVolumeStrategies(),
        ])
        if (!active) return
        setAccounts(instances)
        setStrategies(availableStrategies)
        setControlPlaneConnected(true)
        setControlPlaneAdapter(health.adapter)
        setControlPlaneExecutionEnabled(health.executionEnabled)
        setBoundStrategyExecutionEnabled(health.boundStrategyExecutionEnabled)
        setExecutionCapacity({
          activeExecutions: health.activeExecutionCapacity,
          maxActiveExecutions: health.maxExecutionCapacity,
          activeNormalPhases: health.activeNormalPhaseCapacity,
          maxNormalPhases: health.maxNormalPhaseCapacity,
          queuedNormalPhases: health.queuedNormalPhaseCount,
          phaseStartRatePerSecond: 0,
          perProxyGapSeconds: 0,
          revision: health.capacityRevision,
          activeNormalIo: health.activeNormalIo,
          maxNormalIo: health.maxNormalIo,
          activeEmergencyIo: health.activeEmergencyIo,
          maxEmergencyIo: health.maxEmergencyIo,
          activeProxyPhasePartitions: health.activeProxyPhasePartitions,
          queuedProxyLimitedPhases: health.queuedProxyLimitedPhaseCount,
          phaseQueueP50Ms: health.normalPhaseQueueP50Ms,
          phaseQueueP95Ms: health.normalPhaseQueueP95Ms,
          sqliteWriteQueueCritical: health.sqliteWriteQueueCritical,
          sqliteWriteQueueLowPriority: health.sqliteWriteQueueLowPriority,
          sqliteWriteP95Ms: health.sqliteWriteP95Ms,
          actorCount: health.actorCount,
          eventLoopDelayP99Ms: health.eventLoopDelayP99Ms,
          openFileDescriptors: health.openFileDescriptors,
          rssBytes: health.rssBytes,
          marketDataActiveLeases: health.marketDataActiveLeases,
          marketDataSharedConnections: health.marketDataSharedConnections,
          marketDataIdleConnections: health.marketDataIdleConnections,
          privateOrderStreamActiveLeases: health.privateOrderStreamActiveLeases,
          privateOrderStreams: health.privateOrderStreams,
          historySyncQueued: health.historySyncQueued,
          historySyncRunning: health.historySyncRunning,
        })
        setInitialControlPlaneError(null)
        setInitialControlPlaneSnapshotLoaded(true)
      } catch (error: unknown) {
        if (!active) return
        const message = error instanceof Error ? error.message : '控制平面连接失败'
        setControlPlaneConnected(false)
        setInitialControlPlaneError(message)
        setInitialControlPlaneSnapshotLoaded(true)
        setToast(message)
        retryTimer = window.setTimeout(() => void loadControlPlane(), 3_000)
      }
    }
    void loadControlPlane()
    return () => {
      active = false
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    }
  }, [localUser, setAccounts, setBoundStrategyExecutionEnabled, setControlPlaneAdapter,
    setControlPlaneConnected, setControlPlaneExecutionEnabled, setInitialControlPlaneError,
    setExecutionCapacity, setInitialControlPlaneSnapshotLoaded, setStrategies, setToast])

  useEffect(() => {
    if (!controlPlaneEnabled || !localUser) return
    let active = true
    const loadCapacity = () => void fetchExecutionCapacity().then((capacity) => {
      if (active) setExecutionCapacity(capacity)
    }).catch(() => undefined)
    loadCapacity()
    const timer = window.setInterval(loadCapacity, 1_000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [localUser, setExecutionCapacity])

  useEffect(() => {
    if (!controlPlaneEnabled || !localUser) return
    return subscribeToInstanceEvents(
      (snapshot) => {
        setAccounts(snapshot.instances)
        if (snapshot.runtime) setSchedulerMetrics(snapshot.runtime)
        setLastGlobalSync('刚刚')
      },
      setControlPlaneConnected,
    )
  }, [localUser, setAccounts, setControlPlaneConnected, setLastGlobalSync, setSchedulerMetrics])

  useEffect(() => {
    if (controlPlaneEnabled && !localUser) return
    let active = true
    let timer: number | undefined
    const loadBeta = async () => {
      let nextDelay = betaPollIntervalMs
      const previousSnapshot = betaSnapshotRef.current
      const previousReceivedAtMs = betaReceivedAtRef.current
      const previousAgeMs = previousSnapshot && previousReceivedAtMs !== null
        ? Number(previousSnapshot.ageMs) + (Date.now() - previousReceivedAtMs) : 0
      const previousRemainingMs = previousSnapshot
        ? Number(previousSnapshot.maxAgeMs) - previousAgeMs : 0
      if (!previousSnapshot || previousRemainingMs <= betaRefreshIndicatorThresholdMs) setBetaLoading(true)
      try {
        const snapshot = await fetchBetaMarketSnapshot()
        if (!active) return
        const receivedAtMs = Date.now()
        const changed = previousSnapshot === null
          || previousSnapshot.asOfMs !== snapshot.asOfMs
          || previousSnapshot.generatedAtMs !== snapshot.generatedAtMs
        if (changed) {
          betaSnapshotRef.current = snapshot
          betaReceivedAtRef.current = receivedAtMs
          setBetaSnapshot(snapshot)
          setBetaReceivedAtMs(receivedAtMs)
        }
        setBetaAvailable(true)
      } catch {
        if (!active) return
        setBetaAvailable(false)
        nextDelay = betaRetryIntervalMs
      } finally {
        if (active) {
          setBetaLoading(false)
          timer = window.setTimeout(() => void loadBeta(), nextDelay)
        }
      }
    }
    void loadBeta()
    return () => {
      active = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [betaReceivedAtRef, betaSnapshotRef, localUser, setBetaAvailable, setBetaLoading,
    setBetaReceivedAtMs, setBetaSnapshot])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), 2600)
    return () => window.clearTimeout(timer)
  }, [setToast, toast])
}
