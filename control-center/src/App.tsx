import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ChevronsUp,
  CircleDollarSign,
  CloudCog,
  Gauge,
  LibraryBig,
  LoaderCircle,
  Pause,
  Play,
  Plus,
  RefreshCw,
  Search,
  ShieldCheck,
  SlidersHorizontal,
  Square,
  Target,
  TimerReset,
  Wifi,
  X,
} from 'lucide-react'
import { AccountDialog } from './components/AccountDialog'
import { AccountTable } from './components/AccountTable'
import { ClosePositionsDialog } from './components/ClosePositionsDialog'
import { BoundStrategyExecutionDialog } from './components/BoundStrategyExecutionDialog'
import { BetaSourceDialog } from './components/BetaSourceDialog'
import { ExecutionDrawer } from './components/ExecutionDrawer'
import { LogDrawer } from './components/LogDrawer'
import { LocalLogin } from './components/LocalLogin'
import { StrategyAssignmentDialog } from './components/StrategyAssignmentDialog'
import { StrategyDialog } from './components/StrategyDialog'
import { mockAccounts, mockStrategies } from './data/mockAccounts'
import {
  applyInstanceAction,
  assignVolumeStrategy,
  closeAccountPositions,
  controlPlaneEnabled,
  createAccountInstance,
  createVolumeStrategy,
  deleteAccountInstance,
  dataSourceLabel,
  fetchControlPlaneHealth,
  fetchBetaMarketSnapshot,
  listVolumeStrategies,
  refreshAccountSnapshot,
  stopAllInstances,
  subscribeToInstanceEvents,
  updateAccountInstance,
  updateVolumeStrategy,
  deleteVolumeStrategy,
  listAccountInstances,
  fetchLocalUserSession,
  loginLocalUser,
  logoutLocalUser,
} from './services/controlCenter'
import type {
  AccountDraft,
  AccountInstance,
  BetaMarketSnapshot,
  InstanceStatus,
  SchedulerMetrics,
  StatusFilter,
  StrategyDraft,
  VolumeStrategy,
} from './types'
import { calculateFundingPreflight, estimateRounds, targetProgress } from './utils/strategy'

const compactNumber = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 })
const currency = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const betaPollIntervalMs = 250
const betaRetryIntervalMs = 1_000
const betaCountdownStepMs = 100
const betaRefreshIndicatorThresholdMs = 1_000

const initialSchedulerMetrics: SchedulerMetrics = {
  maxParallelPolls: 12,
  activePolls: 0,
  maxObservedParallelism: 4,
  pollRounds: 0,
  accountsPolled: 0,
  successfulPolls: 0,
  failedPolls: 0,
  lastRoundAccountCount: 4,
  lastRoundSucceeded: 4,
  lastRoundFailed: 0,
  lastRoundStartedAtMs: null,
  lastRoundCompletedAtMs: null,
  lastRoundDurationMs: 221,
}

const filters: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'running', label: '运行中' },
  { value: 'paused', label: '已暂停' },
  { value: 'warning', label: '需处理' },
  { value: 'error', label: '错误' },
  { value: 'stopped', label: '已停止' },
]

type BetaStatusProps = {
  snapshot: BetaMarketSnapshot | null
  available: boolean
  loading: boolean
  receivedAtMs: number | null
}

function BetaStatus({ snapshot, available, loading, receivedAtMs }: BetaStatusProps) {
  const [clockMs, setClockMs] = useState(() => Date.now())

  useEffect(() => {
    const timer = window.setInterval(() => setClockMs(Date.now()), betaCountdownStepMs)
    return () => window.clearInterval(timer)
  }, [])

  const snapshotAgeMs = snapshot && receivedAtMs !== null
    ? Math.max(0, Number(snapshot.ageMs) + (clockMs - receivedAtMs))
    : 0
  const maxAgeMs = snapshot ? Number(snapshot.maxAgeMs) : 0
  const refreshRemainingMs = Math.max(0, maxAgeMs - snapshotAgeMs)
  const refreshPending = available && snapshot !== null && (
    loading || refreshRemainingMs <= betaRefreshIndicatorThresholdMs
  )
  const betaValue = available && snapshot ? Number(snapshot.finalBeta).toFixed(6) : '--'
  const betaConfidence = available && snapshot ? `${(Number(snapshot.confidence) * 100).toFixed(1)}%` : '--'
  const betaAge = available && snapshot ? `${(snapshotAgeMs / 1000).toFixed(1)}s` : '等待数据'
  const refreshCountdown = `${(refreshRemainingMs / 1000).toFixed(1)}s`
  const title = snapshot
    ? `来源 ${snapshot.source} · 数据年龄 ${betaAge} · 下次刷新 ${refreshCountdown} · 置信度仅展示，不参与执行门禁`
    : 'Beta 数据加载中'

  return (
    <span className={`beta-state ${available ? '' : 'offline'}`} title={title}>
      <span className="beta-symbol">β</span>
      <span><small>实时 Final Beta</small><strong>{betaValue}</strong></span>
      <em>
        <span className="beta-reference">参考 {betaConfidence} ·</span>
        <span className="beta-refresh-slot">
          {refreshPending ? <LoaderCircle className="spin beta-refreshing" size={12} aria-label="正在刷新 Beta" /> : `刷新 ${refreshCountdown}`}
        </span>
      </em>
    </span>
  )
}

function App() {
  const [accounts, setAccounts] = useState<AccountInstance[]>(() => controlPlaneEnabled ? [] : mockAccounts)
  const [strategies, setStrategies] = useState<VolumeStrategy[]>(() => controlPlaneEnabled ? [] : mockStrategies)
  const [search, setSearch] = useState(() => sessionStorage.getItem('weex-fleet.search') ?? '')
  const [filter, setFilter] = useState<StatusFilter>(() => {
    const saved = sessionStorage.getItem('weex-fleet.filter')
    return filters.some((item) => item.value === saved) ? saved as StatusFilter : 'all'
  })
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [refreshingIds, setRefreshingIds] = useState<Set<string>>(new Set())
  const [actioningIds, setActioningIds] = useState<Set<string>>(new Set())
  const [logAccount, setLogAccount] = useState<AccountInstance | null>(null)
  const [logSessionId, setLogSessionId] = useState<string | null>(null)
  const [executionAccount, setExecutionAccount] = useState<AccountInstance | null>(null)
  const [accountDialogOpen, setAccountDialogOpen] = useState(false)
  const [editingAccount, setEditingAccount] = useState<AccountInstance | null>(null)
  const [strategyDialogOpen, setStrategyDialogOpen] = useState(false)
  const [betaSourceDialogOpen, setBetaSourceDialogOpen] = useState(false)
  const [strategyDialogInitialId, setStrategyDialogInitialId] = useState<string | null>(null)
  const [assignmentAccounts, setAssignmentAccounts] = useState<AccountInstance[] | null>(null)
  const [closePositionsAccount, setClosePositionsAccount] = useState<AccountInstance | null>(null)
  const [stopDialogOpen, setStopDialogOpen] = useState(false)
  const [stopPhrase, setStopPhrase] = useState('')
  const [toast, setToast] = useState<string | null>(null)
  const [lastGlobalSync, setLastGlobalSync] = useState(controlPlaneEnabled ? '等待首个快照' : '刚刚')
  const [controlPlaneConnected, setControlPlaneConnected] = useState(!controlPlaneEnabled)
  const [controlPlaneAdapter, setControlPlaneAdapter] = useState(controlPlaneEnabled ? 'connecting' : 'browser-mock')
  const [controlPlaneExecutionEnabled, setControlPlaneExecutionEnabled] = useState(!controlPlaneEnabled)
  const [boundStrategyExecutionEnabled, setBoundStrategyExecutionEnabled] = useState(!controlPlaneEnabled)
  const [boundExecutionQueue, setBoundExecutionQueue] = useState<AccountInstance[] | null>(null)
  const [initialControlPlaneSnapshotLoaded, setInitialControlPlaneSnapshotLoaded] = useState(!controlPlaneEnabled)
  const [initialControlPlaneError, setInitialControlPlaneError] = useState<string | null>(null)
  const [schedulerMetrics, setSchedulerMetrics] = useState<SchedulerMetrics>(initialSchedulerMetrics)
  const [betaSnapshot, setBetaSnapshot] = useState<BetaMarketSnapshot | null>(null)
  const [betaAvailable, setBetaAvailable] = useState(true)
  const [betaLoading, setBetaLoading] = useState(false)
  const [betaReceivedAtMs, setBetaReceivedAtMs] = useState<number | null>(null)
  const [pendingWebReleaseId, setPendingWebReleaseId] = useState<string | null>(null)
  const [localUser, setLocalUser] = useState<string | null>(controlPlaneEnabled ? null : 'local')
  const [localUserLoading, setLocalUserLoading] = useState(controlPlaneEnabled)
  const [localUserError, setLocalUserError] = useState<string | null>(null)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const betaSnapshotRef = useRef<BetaMarketSnapshot | null>(null)
  const betaReceivedAtRef = useRef<number | null>(null)
  const actioningIdsRef = useRef<Set<string>>(new Set())
  const webReleaseIdRef = useRef<string | null>(null)
  const controlPlaneHydrating = controlPlaneEnabled && Boolean(localUser) && !initialControlPlaneSnapshotLoaded
  const executionDisabled = controlPlaneEnabled && !controlPlaneExecutionEnabled
  const refreshBlockedByWorkflow = Boolean(
    accountDialogOpen || strategyDialogOpen || assignmentAccounts || closePositionsAccount || stopDialogOpen || boundExecutionQueue,
  )

  useEffect(() => {
    // Bootstrap must run while no user is known yet. Otherwise a fresh browser
    // session stays in its initial loading state forever and cannot submit the
    // login form.
    if (!controlPlaneEnabled) return
    let active = true
    void fetchLocalUserSession()
      .then((session) => {
        if (!active) return
        setLocalUser(session.userId)
        setLocalUserError(null)
      })
      .catch(() => {
        if (!active) return
        setLocalUser(null)
      })
      .finally(() => {
        if (active) setLocalUserLoading(false)
      })
    return () => { active = false }
  }, [localUser])

  useEffect(() => {
    const focusSearch = (event: KeyboardEvent) => {
      if (!(event.metaKey || event.ctrlKey) || event.key.toLowerCase() !== 'k') return
      event.preventDefault()
      searchInputRef.current?.focus()
      searchInputRef.current?.select()
    }
    window.addEventListener('keydown', focusSearch)
    return () => window.removeEventListener('keydown', focusSearch)
  }, [])

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
          : typeof payload.releaseId === 'string'
            ? payload.releaseId
            : null
        if (releaseId && active) {
          if (webReleaseIdRef.current && webReleaseIdRef.current !== releaseId) setPendingWebReleaseId(releaseId)
          webReleaseIdRef.current ??= releaseId
        }
      } catch {
        // A static-version check must never affect the connected trading console.
      } finally {
        if (active) timer = window.setTimeout(() => void pollWebRelease(), 60_000)
      }
    }
    void pollWebRelease()
    return () => {
      active = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [localUser])

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
  }, [accounts.length])

  useEffect(() => {
    if (!controlPlaneEnabled || !localUser) return
    let active = true
    let retryTimer: number | undefined
    const loadControlPlane = async () => {
      try {
        const [instances, health, availableStrategies] = await Promise.all([
          listAccountInstances(),
          fetchControlPlaneHealth(),
          listVolumeStrategies(),
        ])
        if (!active) return
        setAccounts(instances)
        setStrategies(availableStrategies)
        setControlPlaneConnected(true)
        setControlPlaneAdapter(health.adapter)
        setControlPlaneExecutionEnabled(health.executionEnabled)
        setBoundStrategyExecutionEnabled(health.boundStrategyExecutionEnabled)
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
  }, [localUser])

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
  }, [localUser])

  useEffect(() => {
    if (controlPlaneEnabled && !localUser) return
    let active = true
    let timer: number | undefined
    const loadBeta = async () => {
      let nextDelay = betaPollIntervalMs
      const previousSnapshot = betaSnapshotRef.current
      const previousReceivedAtMs = betaReceivedAtRef.current
      const previousAgeMs = previousSnapshot && previousReceivedAtMs !== null
        ? Number(previousSnapshot.ageMs) + (Date.now() - previousReceivedAtMs)
        : 0
      const previousRemainingMs = previousSnapshot
        ? Number(previousSnapshot.maxAgeMs) - previousAgeMs
        : 0
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
  }, [localUser])

  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), 2600)
    return () => window.clearTimeout(timer)
  }, [toast])

  const filteredAccounts = useMemo(() => {
    const query = search.trim().toLowerCase()
    return accounts.filter((account) => {
      const matchesFilter = filter === 'all' || account.status === filter
      const matchesSearch = !query || [account.name, account.accountTag, account.id, account.proxy.host, account.proxy.location, account.strategy.name]
        .some((value) => value.toLowerCase().includes(query))
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
    !actioningIds.has(account.id) && (account.status !== 'stopped' || account.runtime.lastStopVerifiedAtMs === null)
  ))

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
    const blockedLive = candidates.some((account) => status === 'running' && account.mode === 'live')
    setAccounts((current) => current.map((account) => {
      if (!candidateIds.has(account.id)) return account
      if (status === 'running' && account.mode === 'live') return account
      if (status === 'running' && ['error', 'warning'].includes(account.status)) return account
      const funding = account.fundingPreflight ?? calculateFundingPreflight(
        account.strategy,
        account.wallet.available,
        account.wallet.equity > 0 || account.wallet.available > 0,
      )
      const openingNewPair = account.strategyProgress.stage !== 'holding'
      if (status === 'running' && openingNewPair && funding.status !== 'ready') return account
      if (
        status === 'running'
        && openingNewPair
        && account.strategy.targetMode === 'lifetime'
        && !account.volume.complete
      ) return account
      const progress = status === 'running'
        && account.strategy.targetMode === 'incremental'
        && account.strategyProgress.startedAtMs === null
        ? { ...account.strategyProgress, startedAtMs: Date.now() }
        : account.strategyProgress
      return {
        ...account,
        status,
        phase: status === 'running' ? '正在初始化运行周期' : status === 'paused' ? '已人工暂停' : '等待启动',
        strategyProgress: status === 'running'
          ? progress
          : { ...progress, systemPauseReason: null },
        cycle: { ...account.cycle, nextActionAt: status === 'running' ? '10s' : null },
        fundingPreflight: funding,
      }
    }))
    if (blockedLive) setToast('Live 实例尚未连接执行服务')
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
    const ids = new Set(accounts.map((account) => account.id))
    setRefreshingIds(ids)
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
      if (editingAccount) {
        const updated = await updateAccountInstance(editingAccount, draft, strategy)
        setAccounts((current) => current.map((account) => account.id === updated.id ? updated : account))
        setToast('账号配置已更新')
      } else {
        const created = await createAccountInstance(draft, strategy)
        setAccounts((current) => current.some((account) => account.id === created.id)
          ? current.map((account) => account.id === created.id ? created : account)
          : [created, ...current])
        setToast('账号实例已加入待验证队列')
      }
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
            stage: complete ? 'complete' : 'idle',
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
          ? `已停止 ${result.stopped} 个实例；${result.cancelFailed} 个账号撤单未核验，已转为错误状态`
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

  if (controlPlaneEnabled && (localUserLoading || !localUser)) {
    return <LocalLogin loading={localUserLoading} error={localUserError} onSubmit={login} />
  }

  return (
    <div className="app-shell">
      {pendingWebReleaseId && <aside className="release-banner" role="status">
        <span>新界面已就绪。</span>
        <button className="button secondary" type="button" disabled={refreshBlockedByWorkflow} onClick={() => window.location.reload()}>
          {refreshBlockedByWorkflow ? '完成当前操作后刷新' : '刷新界面'}
        </button>
      </aside>}
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark"><ChevronsUp size={19} /></span>
          <div><strong>WEEX Fleet</strong><span>多账号交易控制台</span></div>
          <span className="prototype-badge">{controlPlaneHydrating ? '正在连接控制面' : controlPlaneAdapter === 'weex-live' ? 'WEEX 实盘控制面' : controlPlaneAdapter === 'weex-readonly' ? 'WEEX 只读' : controlPlaneEnabled ? 'API 模拟' : '浏览器模拟'}</span>
        </div>
        <div className="topbar-right">
          <span className={`connection-state ${controlPlaneConnected ? '' : 'offline'}`}>
            <Wifi size={13} />{controlPlaneEnabled ? controlPlaneHydrating ? '控制面连接中' : controlPlaneConnected ? '控制面在线' : '控制面重连中' : '浏览器演示'}
          </span>
          <BetaStatus
            snapshot={betaSnapshot}
            available={betaAvailable}
            loading={betaLoading}
            receivedAtMs={betaReceivedAtMs}
          />
          {controlPlaneEnabled && <button className="icon-button topbar-tool" type="button" onClick={() => setBetaSourceDialogOpen(true)} data-tooltip="Beta 来源设置" aria-label="Beta 来源设置"><CloudCog size={14} /></button>}
          <span className={`scheduler-state ${schedulerMetrics.lastRoundFailed ? 'degraded' : ''}`} title="最近一轮账号调度状态">
            <TimerReset size={13} />{controlPlaneHydrating ? '等待调度快照' : `${schedulerMetrics.lastRoundDurationMs ?? '-'} ms · 峰值 ${schedulerMetrics.maxObservedParallelism}/${schedulerMetrics.maxParallelPolls}`}
          </span>
          <span className="clock-state">同步 {lastGlobalSync}</span>
          {controlPlaneEnabled && <button className="local-user" type="button" title="退出本机用户" onClick={() => void logout()}>{localUser}</button>}
          <button className="button emergency" type="button" onClick={() => setStopDialogOpen(true)}><Square size={13} fill="currentColor" />全部停止</button>
        </div>
      </header>

      <main>
        <section className={`summary-band ${controlPlaneHydrating ? 'summary-loading' : ''}`} aria-label="账户概况">
          {controlPlaneHydrating ? <>
            <div className="summary-heading"><span>全局概况</span><strong>正在读取控制面快照</strong></div>
            {Array.from({ length: 6 }, (_, index) => <div className="metric metric-placeholder" key={index}><span className="summary-skeleton" /></div>)}
          </> : <>
            <div className="summary-heading"><span>全局概况</span><strong>{accounts.length} 个实例</strong></div>
            <div className="metric"><span className="metric-icon green"><Activity size={16} /></span><div><span>运行中</span><strong>{stats.running}<small> / {accounts.length}</small></strong></div></div>
            <div className="metric"><span className="metric-icon blue"><CircleDollarSign size={16} /></span><div><span>合约总权益</span><strong>${compactNumber.format(stats.equity)}</strong></div></div>
            <div className="metric"><span className="metric-icon dark"><Gauge size={16} /></span><div><span>累计交易量</span><strong>${compactNumber.format(stats.lifetimeVolume)}</strong><small>今日 ${compactNumber.format(stats.todayVolume)}</small></div></div>
            <div className="metric"><span className="metric-icon amber"><Target size={16} /></span><div><span>策略目标</span><strong>${compactNumber.format(stats.strategyGenerated)}<small> / ${compactNumber.format(stats.strategyTarget)}</small></strong></div></div>
            <div className="metric"><span className="metric-icon cyan"><ShieldCheck size={16} /></span><div><span>代理正常</span><strong>{stats.healthyProxies}<small> / {accounts.length}</small></strong></div></div>
            <div className="metric"><span className={`metric-icon ${stats.alerts ? 'red' : 'green'}`}><AlertTriangle size={16} /></span><div><span>需要处理</span><strong>{stats.alerts}</strong></div></div>
          </>}
        </section>

        <section className="workspace">
          <div className="workspace-title-row">
            <div><h1>账号实例</h1><span>每个实例使用独立凭据与网络出口</span></div>
            <div className="workspace-actions">
              <button className="button secondary" type="button" onClick={refreshAll} disabled={controlPlaneHydrating || refreshingIds.size > 0}><RefreshCw size={14} className={refreshingIds.size > 0 ? 'spin' : ''} />刷新全部</button>
              <button className="button secondary" type="button" onClick={() => {
                setStrategyDialogInitialId(null)
                setStrategyDialogOpen(true)
              }} disabled={controlPlaneHydrating}><LibraryBig size={14} />策略库</button>
              <button className="button primary" type="button" onClick={() => {
                setEditingAccount(null)
                setAccountDialogOpen(true)
              }} disabled={controlPlaneHydrating}><Plus size={15} />添加账号</button>
            </div>
          </div>

          {!controlPlaneHydrating && <div className="toolbar">
            <label className="search-box"><Search size={14} /><input ref={searchInputRef} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索账号、标签、实例 ID 或代理" /><kbd>⌘ K</kbd></label>
            <div className="filter-tabs" aria-label="状态筛选">
              {filters.map((item) => <button key={item.value} className={filter === item.value ? 'active' : ''} type="button" onClick={() => setFilter(item.value)}>{item.label}<span>{item.value === 'all' ? accounts.length : accounts.filter((account) => account.status === item.value).length}</span></button>)}
            </div>
            <div className="bulk-actions">
              <span>已选 {selectedIds.size}</span>
              <button className="button compact-button" type="button" disabled={!selectedIds.size || !strategies.length} onClick={() => setAssignmentAccounts(accounts.filter((account) => selectedIds.has(account.id)))}><SlidersHorizontal size={14} />应用策略</button>
              <button className="icon-button" type="button" disabled={!canStartSelected || (!boundStrategyExecutionEnabled && executionDisabled)} onClick={() => void updateStatuses(selectedIds, 'running')} data-tooltip="启动所选" aria-label="启动所选"><Play size={14} /></button>
              <button className="icon-button" type="button" disabled={!canPauseSelected || (!boundStrategyExecutionEnabled && executionDisabled)} onClick={() => void updateStatuses(selectedIds, 'paused')} data-tooltip="安全停止所选" aria-label="安全停止所选"><Pause size={14} /></button>
              <button className="icon-button" type="button" disabled={!canStopSelected} onClick={() => void updateStatuses(selectedIds, 'stopped')} data-tooltip="停止所选" aria-label="停止所选"><Square size={13} /></button>
            </div>
          </div>}

          {controlPlaneHydrating ? (
            <div className="control-plane-loading" role="status">
              <LoaderCircle className="spin" size={18} />
              <strong>正在读取账号、策略与执行状态</strong>
              <span>首个真实快照到达前不会显示本地模拟数据。</span>
            </div>
          ) : (
          <AccountTable
            accounts={filteredAccounts}
            selectedIds={selectedIds}
            refreshingIds={refreshingIds}
            actioningIds={actioningIds}
            executionDisabled={executionDisabled}
            boundStrategyExecutionEnabled={boundStrategyExecutionEnabled}
            onSelect={selectOne}
            onSelectAll={selectAllVisible}
            onToggleRunning={toggleRunning}
            onOpenLogs={(account) => {
              setExecutionAccount(null)
              setLogSessionId(null)
              setLogAccount(account)
              setAccounts((current) => current.map((item) => item.id === account.id ? { ...item, unreadLogs: 0 } : item))
            }}
            onOpenExecutions={(account) => {
              setLogAccount(null)
              setExecutionAccount(account)
            }}
            onRefresh={refreshOne}
            onClosePositions={setClosePositionsAccount}
            onEdit={(account) => {
              setEditingAccount(account)
              setAccountDialogOpen(true)
            }}
            onAssignStrategy={(account) => setAssignmentAccounts([account])}
          />
          )}

          {!controlPlaneHydrating && <footer className="table-footer">
            <span>显示 {filteredAccounts.length} / {accounts.length} 个实例</span>
            <span><CloudCog size={13} />数据适配器：{dataSourceLabel}</span>
            <span className={schedulerMetrics.lastRoundFailed ? 'scheduler-error' : ''}>上轮 {schedulerMetrics.lastRoundSucceeded}/{schedulerMetrics.lastRoundAccountCount} 成功 · 失败 {schedulerMetrics.lastRoundFailed}</span>
            <span>总权益 ${currency.format(stats.equity)}</span>
          </footer>}
          {initialControlPlaneError && !controlPlaneConnected && !controlPlaneHydrating && accounts.length === 0 && (
            <div className="control-plane-unavailable" role="status"><AlertTriangle size={16} /><span>控制面不可用：{initialControlPlaneError}。正在自动重连；不会回退到本地模拟数据。</span></div>
          )}
        </section>
      </main>

      {accountDialogOpen && (
        <AccountDialog
          key={editingAccount?.id ?? 'new'}
          account={editingAccount}
          strategies={strategies}
          onClose={closeAccountDialog}
          onSubmit={saveAccount}
          onDelete={editingAccount ? deleteEditingAccount : undefined}
        />
      )}
      {strategyDialogOpen && (
        <StrategyDialog
          strategies={strategies}
          accounts={accounts}
          initialStrategyId={strategyDialogInitialId}
          onClose={() => setStrategyDialogOpen(false)}
          onCreate={createStrategy}
          onUpdate={updateStrategy}
          onDelete={deleteStrategy}
        />
      )}
      {betaSourceDialogOpen && (
        <BetaSourceDialog
          onClose={() => setBetaSourceDialogOpen(false)}
          onChanged={() => {
            setBetaAvailable(false)
            setBetaLoading(true)
          }}
          onToast={setToast}
        />
      )}
      {assignmentAccounts && (
        <StrategyAssignmentDialog
          accounts={assignmentAccounts}
          strategies={strategies}
          onClose={() => setAssignmentAccounts(null)}
          onAssign={assignStrategy}
        />
      )}
      {boundExecutionQueue && boundExecutionQueue[0] && !logAccount && (
        <BoundStrategyExecutionDialog
          key={boundExecutionQueue[0].id}
          account={boundExecutionQueue[0]}
          queuePosition={0}
          queueLength={boundExecutionQueue.length}
          enabled={boundStrategyExecutionEnabled}
          onClose={() => setBoundExecutionQueue((queue) => queue && queue.length > 1 ? queue.slice(1) : null)}
          onChanged={(execution) => {
            setAccounts((current) => current.map((account) => account.id === execution.instanceId
              ? {
                  ...account,
                  status: execution.status === 'executing' ? 'running' : execution.status === 'uncertain' ? 'warning' : execution.status === 'stopping' ? 'paused' : account.status,
                  phase: execution.phase,
                }
              : account))
          }}
          onStarted={(execution) => {
            const currentAccount = accounts.find((account) => account.id === execution.instanceId)
              ?? boundExecutionQueue[0]
            const runningAccount: AccountInstance = {
              ...currentAccount,
              status: 'running',
              phase: execution.phase,
              unreadLogs: 0,
            }
            setAccounts((current) => current.map((account) => account.id === execution.instanceId
              ? runningAccount
              : account))
            setExecutionAccount(null)
            setLogSessionId(null)
            setLogAccount(runningAccount)
            setBoundExecutionQueue((queue) => queue && queue.length > 1 ? queue.slice(1) : null)
          }}
          onToast={setToast}
        />
      )}
      <LogDrawer key={`${logAccount?.id ?? 'closed'}:${logSessionId ?? 'current'}`} account={logAccount} sessionId={logSessionId} onClose={() => {
        setLogAccount(null)
        setLogSessionId(null)
      }} />
      <ExecutionDrawer
        account={executionAccount}
        onClose={() => setExecutionAccount(null)}
        onOpenMonitor={(account, sessionId) => {
          setExecutionAccount(null)
          setLogSessionId(sessionId)
          setLogAccount(account)
        }}
      />
      {closePositionsAccount && (
        <ClosePositionsDialog
          key={closePositionsAccount.id}
          account={closePositionsAccount}
          onClose={() => setClosePositionsAccount(null)}
          onConfirm={confirmClosePositions}
        />
      )}

      {stopDialogOpen && (
        <div className="modal-backdrop" role="presentation" onMouseDown={() => setStopDialogOpen(false)}>
          <section className="dialog stop-dialog" role="alertdialog" aria-modal="true" aria-labelledby="stop-dialog-title" onMouseDown={(event) => event.stopPropagation()}>
            <header className="dialog-header">
              <div><h2 id="stop-dialog-title">停止全部实例</h2><span>全局操作</span></div>
              <button className="icon-button" type="button" onClick={() => setStopDialogOpen(false)} data-tooltip="关闭" aria-label="关闭"><X size={16} /></button>
            </header>
            <div className="stop-warning"><AlertTriangle size={18} /><p>这会停止所有运行周期。请输入 <strong>STOP ALL</strong> 确认。</p></div>
            <input className="confirm-input" value={stopPhrase} onChange={(event) => setStopPhrase(event.target.value)} placeholder="STOP ALL" autoFocus />
            <footer className="dialog-actions">
              <button className="button secondary" type="button" onClick={() => setStopDialogOpen(false)}>取消</button>
              <button className="button danger" type="button" disabled={stopPhrase !== 'STOP ALL'} onClick={() => void emergencyStop()}>确认停止</button>
            </footer>
          </section>
        </div>
      )}

      {toast && <div className="toast"><ShieldCheck size={15} />{toast}</div>}
    </div>
  )
}

export default App
