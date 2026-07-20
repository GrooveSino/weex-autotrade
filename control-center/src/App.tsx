import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ChevronsUp,
  CircleDollarSign,
  CloudCog,
  Gauge,
  LibraryBig,
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
import { BetaCampaignDialog } from './components/BetaCampaignDialog'
import { AccountTable } from './components/AccountTable'
import { ClosePositionsDialog } from './components/ClosePositionsDialog'
import { ExecutionDrawer } from './components/ExecutionDrawer'
import { LogDrawer } from './components/LogDrawer'
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
} from './services/controlCenter'
import type {
  AccountDraft,
  AccountInstance,
  BetaMarketSnapshot,
  BetaCampaign,
  InstanceStatus,
  SchedulerMetrics,
  StatusFilter,
  StrategyDraft,
  VolumeStrategy,
} from './types'
import { calculateFundingPreflight, estimateRounds, targetProgress } from './utils/strategy'

const compactNumber = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 })
const currency = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

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

function App() {
  const [accounts, setAccounts] = useState<AccountInstance[]>(mockAccounts)
  const [strategies, setStrategies] = useState<VolumeStrategy[]>(mockStrategies)
  const [search, setSearch] = useState('')
  const [filter, setFilter] = useState<StatusFilter>('all')
  const [selectedIds, setSelectedIds] = useState<Set<string>>(new Set())
  const [refreshingIds, setRefreshingIds] = useState<Set<string>>(new Set())
  const [logAccount, setLogAccount] = useState<AccountInstance | null>(null)
  const [executionAccount, setExecutionAccount] = useState<AccountInstance | null>(null)
  const [accountDialogOpen, setAccountDialogOpen] = useState(false)
  const [editingAccount, setEditingAccount] = useState<AccountInstance | null>(null)
  const [strategyDialogOpen, setStrategyDialogOpen] = useState(false)
  const [strategyDialogInitialId, setStrategyDialogInitialId] = useState<string | null>(null)
  const [assignmentAccounts, setAssignmentAccounts] = useState<AccountInstance[] | null>(null)
  const [closePositionsAccount, setClosePositionsAccount] = useState<AccountInstance | null>(null)
  const [betaCampaignAccount, setBetaCampaignAccount] = useState<AccountInstance | null>(null)
  const [campaigns, setCampaigns] = useState<BetaCampaign[]>([])
  const [stopDialogOpen, setStopDialogOpen] = useState(false)
  const [stopPhrase, setStopPhrase] = useState('')
  const [toast, setToast] = useState<string | null>(null)
  const [lastGlobalSync, setLastGlobalSync] = useState('刚刚')
  const [controlPlaneConnected, setControlPlaneConnected] = useState(!controlPlaneEnabled)
  const [controlPlaneAdapter, setControlPlaneAdapter] = useState(controlPlaneEnabled ? 'mock' : 'browser-mock')
  const [controlPlaneExecutionEnabled, setControlPlaneExecutionEnabled] = useState(!controlPlaneEnabled)
  const [liveCampaignsEnabled, setLiveCampaignsEnabled] = useState(false)
  const [schedulerMetrics, setSchedulerMetrics] = useState<SchedulerMetrics>(initialSchedulerMetrics)
  const [betaSnapshot, setBetaSnapshot] = useState<BetaMarketSnapshot | null>(null)
  const [betaAvailable, setBetaAvailable] = useState(true)
  const searchInputRef = useRef<HTMLInputElement>(null)
  const executionDisabled = controlPlaneEnabled && !controlPlaneExecutionEnabled

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
    if (!controlPlaneEnabled) return
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
        setControlPlaneAdapter(health.adapter)
        setControlPlaneExecutionEnabled(health.executionEnabled)
        setLiveCampaignsEnabled(health.liveCampaignsEnabled)
      } catch (error: unknown) {
        if (!active) return
        setControlPlaneConnected(false)
        setToast(error instanceof Error ? error.message : '控制平面连接失败')
        retryTimer = window.setTimeout(() => void loadControlPlane(), 3_000)
      }
    }
    void loadControlPlane()
    return () => {
      active = false
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    }
  }, [])

  useEffect(() => {
    if (!controlPlaneEnabled) return
    return subscribeToInstanceEvents(
      (instances, runtime) => {
        setAccounts(instances)
        if (runtime) setSchedulerMetrics(runtime)
        setLastGlobalSync('刚刚')
      },
      setControlPlaneConnected,
      setCampaigns,
    )
  }, [])

  useEffect(() => {
    let active = true
    let timer: number | undefined
    const loadBeta = async () => {
      try {
        const snapshot = await fetchBetaMarketSnapshot()
        if (!active) return
        setBetaSnapshot(snapshot)
        setBetaAvailable(true)
      } catch {
        if (!active) return
        setBetaAvailable(false)
      } finally {
        if (active) timer = window.setTimeout(() => void loadBeta(), 10_000)
      }
    }
    void loadBeta()
    return () => {
      active = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [])

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
      account.volume.session?.targetQuoteVolume ?? account.strategy.targetVolumeQuote,
    ), 0),
    strategyGenerated: accounts.reduce((sum, account) => sum + Number(
      account.volume.session?.verifiedQuoteVolume ?? targetProgress(account),
    ), 0),
  }), [accounts])

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
    if (executionDisabled && status !== 'stopped') {
      setToast('WEEX 只读模式不允许启动或暂停实例')
      return
    }
    if (controlPlaneEnabled) {
      const action = status === 'running' ? 'start' : status === 'paused' ? 'pause' : 'stop'
      const targets = accounts.filter((account) => ids.has(account.id))
      const results = await Promise.all(targets.map(async (account) => {
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

    const blockedLive = accounts.some((account) => ids.has(account.id) && status === 'running' && account.mode === 'live')
    setAccounts((current) => current.map((account) => {
      if (!ids.has(account.id)) return account
      if (status === 'running' && account.mode === 'live') return account
      if (status === 'running' && account.status === 'error') return account
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
  }

  const toggleRunning = (account: AccountInstance) => {
    if (executionDisabled) {
      setToast('WEEX 只读模式不允许启动或暂停实例')
      return
    }
    if (account.status === 'error') {
      setToast('请先处理实例错误')
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

  const campaignForAccount = (account: AccountInstance) => campaigns
    .filter((campaign) => campaign.instanceId === account.id)
    .sort((left, right) => (right.startedAtMs ?? 0) - (left.startedAtMs ?? 0))[0] ?? null

  const updateCampaign = (next: BetaCampaign) => {
    setCampaigns((current) => current.some((campaign) => campaign.campaignId === next.campaignId)
      ? current.map((campaign) => campaign.campaignId === next.campaignId ? next : campaign)
      : [next, ...current])
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
    setToast(executionDisabled ? '只读实例已停止' : '所有模拟实例已停止')
  }

  const betaValue = betaAvailable && betaSnapshot
    ? Number(betaSnapshot.finalBeta).toFixed(6)
    : '--'
  const betaConfidence = betaAvailable && betaSnapshot
    ? `${(Number(betaSnapshot.confidence) * 100).toFixed(1)}%`
    : '--'
  const betaAge = betaAvailable && betaSnapshot
    ? `${(Number(betaSnapshot.ageMs) / 1000).toFixed(1)}s`
    : '等待数据'

  return (
    <div className="app-shell">
      <header className="topbar">
        <div className="brand">
          <span className="brand-mark"><ChevronsUp size={19} /></span>
          <div><strong>WEEX Fleet</strong><span>多账号交易控制台</span></div>
          <span className="prototype-badge">{controlPlaneAdapter === 'weex-readonly' ? 'WEEX 只读' : controlPlaneEnabled ? 'API 模拟' : '浏览器模拟'}</span>
        </div>
        <div className="topbar-right">
          <span className={`connection-state ${controlPlaneConnected ? '' : 'offline'}`}>
            <Wifi size={13} />{controlPlaneEnabled ? controlPlaneConnected ? '控制面在线' : '控制面重连中' : '浏览器演示'}
          </span>
          <span className={`beta-state ${betaAvailable ? '' : 'offline'}`} title={betaSnapshot ? `来源 ${betaSnapshot.source} · 数据年龄 ${betaAge} · 置信度仅展示，不参与执行门禁` : 'Beta 数据加载中'}>
            <span className="beta-symbol">β</span>
            <span><small>实时 Final Beta</small><strong>{betaValue}</strong></span>
            <em>参考 {betaConfidence} · {betaAge}</em>
          </span>
          <span className={`scheduler-state ${schedulerMetrics.lastRoundFailed ? 'degraded' : ''}`} title="最近一轮账号调度状态">
            <TimerReset size={13} />{schedulerMetrics.lastRoundDurationMs ?? '-'} ms · 峰值 {schedulerMetrics.maxObservedParallelism}/{schedulerMetrics.maxParallelPolls}
          </span>
          <span className="clock-state">同步 {lastGlobalSync}</span>
          <button className="button emergency" type="button" onClick={() => setStopDialogOpen(true)}><Square size={13} fill="currentColor" />全部停止</button>
        </div>
      </header>

      <main>
        <section className="summary-band" aria-label="账户概况">
          <div className="summary-heading"><span>全局概况</span><strong>{accounts.length} 个实例</strong></div>
          <div className="metric"><span className="metric-icon green"><Activity size={16} /></span><div><span>运行中</span><strong>{stats.running}<small> / {accounts.length}</small></strong></div></div>
          <div className="metric"><span className="metric-icon blue"><CircleDollarSign size={16} /></span><div><span>合约总权益</span><strong>${compactNumber.format(stats.equity)}</strong></div></div>
          <div className="metric"><span className="metric-icon dark"><Gauge size={16} /></span><div><span>累计交易量</span><strong>${compactNumber.format(stats.lifetimeVolume)}</strong><small>今日 ${compactNumber.format(stats.todayVolume)}</small></div></div>
          <div className="metric"><span className="metric-icon amber"><Target size={16} /></span><div><span>策略目标</span><strong>${compactNumber.format(stats.strategyGenerated)}<small> / ${compactNumber.format(stats.strategyTarget)}</small></strong></div></div>
          <div className="metric"><span className="metric-icon cyan"><ShieldCheck size={16} /></span><div><span>代理正常</span><strong>{stats.healthyProxies}<small> / {accounts.length}</small></strong></div></div>
          <div className="metric"><span className={`metric-icon ${stats.alerts ? 'red' : 'green'}`}><AlertTriangle size={16} /></span><div><span>需要处理</span><strong>{stats.alerts}</strong></div></div>
        </section>

        <section className="workspace">
          <div className="workspace-title-row">
            <div><h1>账号实例</h1><span>每个实例使用独立凭据与网络出口</span></div>
            <div className="workspace-actions">
              <button className="button secondary" type="button" onClick={refreshAll} disabled={refreshingIds.size > 0}><RefreshCw size={14} className={refreshingIds.size > 0 ? 'spin' : ''} />刷新全部</button>
              <button className="button secondary" type="button" onClick={() => {
                setStrategyDialogInitialId(null)
                setStrategyDialogOpen(true)
              }}><LibraryBig size={14} />策略库</button>
              <button className="button primary" type="button" onClick={() => {
                setEditingAccount(null)
                setAccountDialogOpen(true)
              }}><Plus size={15} />添加账号</button>
            </div>
          </div>

          <div className="toolbar">
            <label className="search-box"><Search size={14} /><input ref={searchInputRef} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索账号、标签、实例 ID 或代理" /><kbd>⌘ K</kbd></label>
            <div className="filter-tabs" aria-label="状态筛选">
              {filters.map((item) => <button key={item.value} className={filter === item.value ? 'active' : ''} type="button" onClick={() => setFilter(item.value)}>{item.label}<span>{item.value === 'all' ? accounts.length : accounts.filter((account) => account.status === item.value).length}</span></button>)}
            </div>
            <div className="bulk-actions">
              <span>已选 {selectedIds.size}</span>
              <button className="button compact-button" type="button" disabled={!selectedIds.size || !strategies.length} onClick={() => setAssignmentAccounts(accounts.filter((account) => selectedIds.has(account.id)))}><SlidersHorizontal size={14} />应用策略</button>
              <button className="icon-button" type="button" disabled={!selectedIds.size || executionDisabled} onClick={() => void updateStatuses(selectedIds, 'running')} data-tooltip="启动所选" aria-label="启动所选"><Play size={14} /></button>
              <button className="icon-button" type="button" disabled={!selectedIds.size || executionDisabled} onClick={() => void updateStatuses(selectedIds, 'paused')} data-tooltip="暂停所选" aria-label="暂停所选"><Pause size={14} /></button>
              <button className="icon-button" type="button" disabled={!selectedIds.size} onClick={() => void updateStatuses(selectedIds, 'stopped')} data-tooltip="停止所选" aria-label="停止所选"><Square size={13} /></button>
            </div>
          </div>

          <AccountTable
            accounts={filteredAccounts}
            selectedIds={selectedIds}
            refreshingIds={refreshingIds}
            executionDisabled={executionDisabled}
            onSelect={selectOne}
            onSelectAll={selectAllVisible}
            onToggleRunning={toggleRunning}
            onOpenLogs={(account) => {
              setExecutionAccount(null)
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
            onOpenBetaCampaign={setBetaCampaignAccount}
            betaCampaigns={campaigns}
            betaCampaignAvailable={liveCampaignsEnabled}
          />

          <footer className="table-footer">
            <span>显示 {filteredAccounts.length} / {accounts.length} 个实例</span>
            <span><CloudCog size={13} />数据适配器：{dataSourceLabel}</span>
            <span className={schedulerMetrics.lastRoundFailed ? 'scheduler-error' : ''}>上轮 {schedulerMetrics.lastRoundSucceeded}/{schedulerMetrics.lastRoundAccountCount} 成功 · 失败 {schedulerMetrics.lastRoundFailed}</span>
            <span>总权益 ${currency.format(stats.equity)}</span>
          </footer>
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
      {assignmentAccounts && (
        <StrategyAssignmentDialog
          accounts={assignmentAccounts}
          strategies={strategies}
          onClose={() => setAssignmentAccounts(null)}
          onAssign={assignStrategy}
        />
      )}
      {betaCampaignAccount && (
        <BetaCampaignDialog
          account={betaCampaignAccount}
          campaign={campaignForAccount(betaCampaignAccount)}
          liveEnabled={liveCampaignsEnabled && controlPlaneConnected}
          onClose={() => setBetaCampaignAccount(null)}
          onChanged={updateCampaign}
          onToast={setToast}
        />
      )}
      <LogDrawer account={logAccount} onClose={() => setLogAccount(null)} />
      <ExecutionDrawer account={executionAccount} onClose={() => setExecutionAccount(null)} />
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
