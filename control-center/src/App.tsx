import { useEffect, useState } from 'react'
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
import { AccountDialog } from './components/accounts/AccountDialog'
import { AccountTable } from './components/accounts/AccountTable'
import { ClosePositionsDialog } from './components/dialogs/ClosePositionsDialog'
import { BoundStrategyExecutionDialog } from './components/execution/BoundStrategyExecutionDialog'
import { BetaSourceDialog } from './components/strategy/BetaSourceDialog'
import { ExecutionDrawer } from './components/execution/ExecutionDrawer'
import { LogDrawer } from './components/monitoring/LogDrawer'
import { LocalLogin } from './components/shell/LocalLogin'
import { FleetCapacityBadge } from './components/execution/FleetCapacityBadge'
import { StrategyAssignmentDialog } from './components/strategy/StrategyAssignmentDialog'
import { StrategyDialog } from './components/strategy/StrategyDialog'
import { useFleetApp } from './hooks/useFleetApp'
import { controlPlaneEnabled, dataSourceLabel } from './services'
import type { BetaMarketSnapshot, StatusFilter } from './types'

const compactNumber = new Intl.NumberFormat('en-US', { notation: 'compact', maximumFractionDigits: 2 })

const currency = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

const betaCountdownStepMs = 100

const betaRefreshIndicatorThresholdMs = 1_000

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
  const { accounts, setAccounts, strategies, search, setSearch, filter, setFilter, selectedIds, refreshingIds, actioningIds, logAccount, setLogAccount, logSessionId, setLogSessionId, executionAccount, setExecutionAccount, accountDialogOpen, setAccountDialogOpen, editingAccount, setEditingAccount, strategyDialogOpen, setStrategyDialogOpen, betaSourceDialogOpen, setBetaSourceDialogOpen, strategyDialogInitialId, setStrategyDialogInitialId, assignmentAccounts, setAssignmentAccounts, closePositionsAccount, setClosePositionsAccount, stopDialogOpen, setStopDialogOpen, stopPhrase, setStopPhrase, toast, setToast, lastGlobalSync, controlPlaneConnected, controlPlaneAdapter, boundStrategyExecutionEnabled, executionCapacity, boundExecutionQueue, setBoundExecutionQueue, initialControlPlaneError, schedulerMetrics, betaSnapshot, betaAvailable, setBetaAvailable, betaLoading, setBetaLoading, betaReceivedAtMs, pendingWebReleaseId, localUser, localUserLoading, localUserError, loginRequired, login, searchInputRef, controlPlaneHydrating, executionDisabled, refreshBlockedByWorkflow, filteredAccounts, stats, canStartSelected, canPauseSelected, canStopSelected, selectOne, selectAllVisible, updateStatuses, toggleRunning, refreshOne, refreshAll, confirmClosePositions, saveAccount, deleteEditingAccount, closeAccountDialog, createStrategy, updateStrategy, duplicateStrategy, deleteStrategy, assignStrategy, emergencyStop, logout } = useFleetApp()

  if (loginRequired) return <LocalLogin loading={localUserLoading} error={localUserError} onSubmit={login} />

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
          <FleetCapacityBadge capacity={executionCapacity} loading={controlPlaneHydrating} />
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
            <div className={`bulk-actions ${selectedIds.size ? 'has-selection' : ''}`}>
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
          onDuplicate={duplicateStrategy}
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
          onChanged={() => { /* Account state comes from the executor lifecycle snapshot. */ }}
          onEditAccount={() => { setEditingAccount(boundExecutionQueue[0]); setAccountDialogOpen(true); setBoundExecutionQueue(null) }}
          onOpenBetaSource={() => { setBetaSourceDialogOpen(true); setBoundExecutionQueue(null) }}
          onStarted={(execution) => {
            const currentAccount = accounts.find((account) => account.id === execution.instanceId)
              ?? boundExecutionQueue[0]
            setExecutionAccount(null)
            setLogSessionId(null)
            setLogAccount(currentAccount)
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
