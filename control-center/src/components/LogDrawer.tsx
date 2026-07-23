import { useEffect, useMemo, useRef, useState } from 'react'
import {
  ArrowDownToLine,
  CircleDot,
  Copy,
  History,
  LoaderCircle,
  RefreshCw,
  Terminal,
  Trash2,
  X,
} from 'lucide-react'
import {
  clearInstanceLogs,
  fetchInstanceLogs,
  fetchStrategyMonitor,
  subscribeToStrategyMonitor,
} from '../services/controlCenter'
import type { AccountInstance, LogLine, StrategyMonitorSnapshot } from '../types'

interface LogDrawerProps {
  account: AccountInstance | null
  sessionId?: string | null
  onClose: () => void
}

type ConnectionState = 'connecting' | 'connected' | 'retrying'

const levelLabel: Record<LogLine['level'], string> = {
  info: '信息',
  success: '成功',
  warn: '警告',
  error: '错误',
}

function displayTime(value: string | number): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? String(value) : parsed.toLocaleTimeString('zh-CN', { hour12: false })
}

function quote(value: string): string {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? parsed.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 }) : value
}

function countdown(valueMs: number | null): string {
  if (valueMs === null) return '--:--.-'
  const remaining = Math.max(0, valueMs)
  const minutes = Math.floor(remaining / 60_000)
  const seconds = Math.floor((remaining % 60_000) / 1_000)
  const tenths = Math.floor((remaining % 1_000) / 100)
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${tenths}`
}

function mergeMonitor(
  current: StrategyMonitorSnapshot | null,
  incoming: StrategyMonitorSnapshot,
  replace: boolean,
): StrategyMonitorSnapshot {
  if (!current) return incoming
  const currentKey = `${current.executorGeneration}:${current.executionId ?? 'idle'}`
  const incomingKey = `${incoming.executorGeneration}:${incoming.executionId ?? 'idle'}`
  if (currentKey !== incomingKey) return incoming
  const entries = new Map(current.timeline.map((entry) => [entry.id, entry]))
  incoming.timeline.forEach((entry) => entries.set(entry.id, entry))
  const incomingIsNewer = incoming.projectionSequence > current.projectionSequence
    || (incoming.projectionSequence === current.projectionSequence && incoming.ledgerRevision > current.ledgerRevision)
    || (
      incoming.projectionSequence === current.projectionSequence
      && incoming.ledgerRevision === current.ledgerRevision
      && incoming.serverTimeMs >= current.serverTimeMs
    )
  const summary = incomingIsNewer ? incoming : current
  return {
    ...summary,
    timeline: [...entries.values()].sort((left, right) => left.sequence - right.sequence).slice(-500),
    hasMore: replace ? incoming.hasMore : current.hasMore || incoming.hasMore,
  }
}

export function LogDrawer({ account, sessionId = null, onClose }: LogDrawerProps) {
  const accountId = account?.id ?? ''
  const [tab, setTab] = useState<'monitor' | 'system'>('monitor')
  const [clockMs, setClockMs] = useState(() => Date.now())
  const [serverClockOffsetMs, setServerClockOffsetMs] = useState(0)
  const [monitor, setMonitor] = useState<StrategyMonitorSnapshot | null>(null)
  const [monitorLoading, setMonitorLoading] = useState(false)
  const [monitorError, setMonitorError] = useState<string | null>(null)
  const [monitorConnection, setMonitorConnection] = useState<ConnectionState>('connecting')
  const [systemLines, setSystemLines] = useState<LogLine[]>([])
  const [systemLoading, setSystemLoading] = useState(false)
  const [systemError, setSystemError] = useState<string | null>(null)
  const [systemConnection, setSystemConnection] = useState<ConnectionState>('connecting')
  const [clearArmed, setClearArmed] = useState(false)
  const [clearBusy, setClearBusy] = useState(false)
  const [loadOlderBusy, setLoadOlderBusy] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const followTailRef = useRef(true)
  const accountRef = useRef(account)
  const monitorRef = useRef<StrategyMonitorSnapshot | null>(null)

  useEffect(() => {
    accountRef.current = account
  }, [account])

  useEffect(() => {
    monitorRef.current = monitor
  }, [monitor])

  useEffect(() => {
    const selectedAccount = accountRef.current
    if (!selectedAccount) return
    let active = true
    let unsubscribe: () => void = () => undefined
    let retryTimer: number | undefined
    setMonitorLoading(true)
    setMonitorConnection('connecting')
    const bootstrap = async () => {
      try {
        const initial = await fetchStrategyMonitor(selectedAccount, null, sessionId)
        if (!active) return
        setServerClockOffsetMs(initial.serverTimeMs - Date.now())
        setMonitor((current) => mergeMonitor(current, initial, true))
        setMonitorError(null)
        setMonitorLoading(false)
        unsubscribe()
        unsubscribe = subscribeToStrategyMonitor(
          selectedAccount,
          initial.cursor,
          sessionId,
          (event) => {
            if (!active) return
            if (event.serverTimeMs) setServerClockOffsetMs(event.serverTimeMs - Date.now())
            if (event.snapshot) {
              setServerClockOffsetMs(event.snapshot.serverTimeMs - Date.now())
              setMonitor((current) => mergeMonitor(current, event.snapshot!, event.type !== 'delta'))
            }
            setMonitorError(null)
          },
          setMonitorConnection,
        )
      } catch (reason: unknown) {
        if (!active) return
        setMonitorLoading(false)
        setMonitorConnection('retrying')
        setMonitorError(reason instanceof Error ? reason.message : '执行监控加载失败')
        retryTimer = window.setTimeout(() => void bootstrap(), 2_000)
      }
    }
    void bootstrap()
    return () => {
      active = false
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
      unsubscribe()
    }
  }, [accountId, sessionId])

  useEffect(() => {
    const selectedAccount = accountRef.current
    if (!selectedAccount) return
    let active = true
    let inFlight = false
    const reconcileSnapshot = async () => {
      const current = monitorRef.current
      if (!current || !['planned', 'executing', 'stopping'].includes(current.status) || inFlight) return
      inFlight = true
      try {
        const latest = await fetchStrategyMonitor(selectedAccount, null, sessionId)
        if (!active) return
        setServerClockOffsetMs(latest.serverTimeMs - Date.now())
        setMonitor((existing) => mergeMonitor(existing, latest, true))
        setMonitorError(null)
      } catch (reason: unknown) {
        if (active) setMonitorError(reason instanceof Error ? reason.message : '监控快照核对失败')
      } finally {
        inFlight = false
      }
    }
    const timer = window.setInterval(() => void reconcileSnapshot(), 5_000)
    return () => {
      active = false
      window.clearInterval(timer)
    }
  }, [accountId, sessionId])

  useEffect(() => {
    const selectedAccount = accountRef.current
    if (!selectedAccount || tab !== 'system') return
    let active = true
    let cursor: string | null = null
    let timer: number | undefined
    setSystemLoading(true)
    setSystemConnection('connecting')
    const poll = async () => {
      try {
        const batch = await fetchInstanceLogs(selectedAccount, cursor)
        if (!active) return
        const replace = cursor === null || batch.reset
        cursor = batch.cursor
        setSystemLines((current) => {
          const byId = new Map((replace ? [] : current).map((line) => [line.id, line]))
          batch.lines.forEach((line) => byId.set(line.id, line))
          return [...byId.values()].slice(-500)
        })
        setSystemError(null)
        setSystemConnection('connected')
      } catch (reason: unknown) {
        if (!active) return
        setSystemError(reason instanceof Error ? reason.message : '系统日志加载失败')
        setSystemConnection('retrying')
      } finally {
        if (active) {
          setSystemLoading(false)
          timer = window.setTimeout(poll, 1_000)
        }
      }
    }
    void poll()
    return () => {
      active = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [accountId, tab])

  useEffect(() => {
    if (!monitor?.activeWaits.length) return
    const timer = window.setInterval(() => setClockMs(Date.now()), 125)
    return () => window.clearInterval(timer)
  }, [monitor?.activeWaits.length])

  useEffect(() => {
    if (!clearArmed) return
    const timer = window.setTimeout(() => setClearArmed(false), 5_000)
    return () => window.clearTimeout(timer)
  }, [clearArmed])

  useEffect(() => {
    const body = bodyRef.current
    if (body && followTailRef.current) body.scrollTop = body.scrollHeight
  }, [monitor?.timeline.length, systemLines.length, tab])

  const monitorText = useMemo(() => monitor?.timeline.map((entry) => (
    `[${new Date(entry.atMs).toISOString()}] ${levelLabel[entry.level]} ${entry.title}${entry.detail ? `；${entry.detail}` : ''}`
  )).join('\n') ?? '', [monitor])
  const systemText = useMemo(() => systemLines.map((line) => (
    `[${line.timestamp}] ${levelLabel[line.level]} ${line.message}`
  )).join('\n'), [systemLines])
  const plainText = tab === 'monitor' ? monitorText : systemText

  if (!account) return null

  const download = () => {
    const url = URL.createObjectURL(new Blob([plainText], { type: 'text/plain;charset=utf-8' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${account.id}-${tab === 'monitor' ? 'execution' : 'system'}.log`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  const clearSystemLogs = async () => {
    if (!clearArmed) {
      setClearArmed(true)
      return
    }
    setClearBusy(true)
    try {
      await clearInstanceLogs(account)
      setSystemLines([])
      setClearArmed(false)
    } catch (reason: unknown) {
      setSystemError(reason instanceof Error ? reason.message : '系统日志清除失败')
      setClearArmed(false)
    } finally {
      setClearBusy(false)
    }
  }

  const loadOlder = async () => {
    const first = monitor?.timeline[0]
    if (!first) return
    setLoadOlderBusy(true)
    try {
      const older = await fetchStrategyMonitor(account, first.sequence, sessionId)
      setMonitor((current) => {
        if (!current) return older
        const entries = new Map(older.timeline.map((entry) => [entry.id, entry]))
        current.timeline.forEach((entry) => entries.set(entry.id, entry))
        return { ...current, timeline: [...entries.values()].sort((a, b) => a.sequence - b.sequence), hasMore: older.hasMore }
      })
    } catch (reason: unknown) {
      setMonitorError(reason instanceof Error ? reason.message : '更早记录加载失败')
    } finally {
      setLoadOlderBusy(false)
    }
  }

  const connection = tab === 'monitor' ? monitorConnection : systemConnection
  const error = tab === 'monitor' ? monitorError : systemError
  const primaryWait = monitor?.activeWaits.find((wait) => wait.key === 'hold' || wait.key === 'round-gap')
    ?? monitor?.activeWaits[0]
    ?? null
  const serverNowMs = clockMs + serverClockOffsetMs
  const primaryWaitDelta = primaryWait ? Math.max(0, serverNowMs - primaryWait.updatedAtMs) : 0
  const primaryWaitRemaining = primaryWait?.remainingMs === null || primaryWait?.remainingMs === undefined
    ? null
    : Math.max(
      0,
      primaryWait.deadlineAtMs !== null && primaryWait.deadlineAtMs !== undefined
        ? primaryWait.deadlineAtMs - serverNowMs
        : primaryWait.remainingMs - primaryWaitDelta,
    )
  const volumeState = monitor?.volumeSource === 'ledger'
    ? '权威成交账本已同步'
    : monitor?.volumeSource === 'execution_journal'
      ? '执行器已核验 · 成交账本同步中'
      : '等待首笔权威成交'

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={onClose}>
      <aside className="log-drawer execution-monitor" role="dialog" aria-modal="true" aria-labelledby="log-title" onMouseDown={(event) => event.stopPropagation()}>
        <header className="drawer-header">
          <div className="drawer-title">
            <span className="terminal-mark"><Terminal size={16} /></span>
            <div><h2 id="log-title">{account.name}</h2><span>{account.id} / 执行监控</span></div>
          </div>
          <div className="drawer-actions">
            <button className="icon-button dark" type="button" onClick={() => navigator.clipboard.writeText(plainText)} data-tooltip="复制当前视图" aria-label="复制当前视图"><Copy size={15} /></button>
            <button className="icon-button dark" type="button" onClick={download} data-tooltip="下载当前视图" aria-label="下载当前视图"><ArrowDownToLine size={15} /></button>
            {tab === 'system' && <button className={`icon-button dark log-clear-button ${clearArmed ? 'armed' : ''}`} type="button" onClick={() => void clearSystemLogs()} disabled={clearBusy || (!clearArmed && systemLines.length === 0)} data-tooltip={clearArmed ? '再次点击确认清除' : '清除系统日志'} aria-label={clearArmed ? '确认清除系统日志' : '清除系统日志'}>{clearBusy ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}<span>{clearArmed ? '确认清除' : '清除'}</span></button>}
            <button className="icon-button dark" type="button" onClick={onClose} data-tooltip="关闭" aria-label="关闭执行监控"><X size={16} /></button>
          </div>
        </header>

        <div className="monitor-tabs" role="tablist">
          <button type="button" className={tab === 'monitor' ? 'active' : ''} onClick={() => setTab('monitor')}><Terminal size={13} />执行监控</button>
          <button type="button" className={tab === 'system' ? 'active' : ''} onClick={() => setTab('system')}><History size={13} />系统日志</button>
        </div>

        <div className={`terminal-statusbar ${connection}`}>
          <span><CircleDot size={12} />{connection === 'connecting' ? '正在建立连接' : connection === 'retrying' ? '监控同步恢复中' : '本地执行器实时连接'}</span>
          <span>{tab === 'monitor' ? '事件驱动 / 倒计时 8 fps' : '仅当前实例 / 1s 增量读取'}</span>
        </div>

        <section className={`runtime-stage-strip ${primaryWait ? 'waiting' : ''}`} aria-label="当前运行阶段">
          <div className="runtime-stage-main">
            {primaryWait ? <LoaderCircle className="spin" size={15} /> : <CircleDot size={13} />}
            <span><small>{primaryWait?.label ?? '当前阶段'}</small><strong>{monitor?.phase ?? '等待执行状态'}</strong></span>
          </div>
          <div className="runtime-stage-context">
            <span>运行 <strong>{monitor?.currentRun || '-'}</strong></span>
            <span>轮次 <strong>{monitor?.currentRound || '-'}</strong></span>
          </div>
          <div className="runtime-stage-countdown">
            <small>{primaryWait ? (primaryWait.key === 'hold' ? '持仓剩余' : primaryWait.key === 'round-gap' ? '下轮开始' : '等待剩余') : '倒计时'}</small>
            <strong>{countdown(primaryWaitRemaining)}</strong>
          </div>
        </section>

        <div className="terminal-body monitor-body" ref={bodyRef} onScroll={(event) => {
          const node = event.currentTarget
          followTailRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 48
        }}>
          {tab === 'monitor' ? (
            monitorLoading && !monitor ? <div className="terminal-loading"><span className="terminal-cursor" />正在读取本次任务...</div> : <>
              {monitor && <>
                <section className="monitor-summary" aria-label="任务摘要">
                  <div><span>状态 / 阶段</span><strong>{monitor.status} / {monitor.phase}</strong></div>
                  <div><span>运行 / 轮次</span><strong>{monitor.currentRun || '-'} / {monitor.currentRound || '-'}</strong></div>
                  <div><span>本次已核验 / 目标</span><strong>{quote(monitor.verifiedQuoteVolume)} / {quote(monitor.targetQuoteVolume)} USDT</strong></div>
                  <div><span>剩余目标</span><strong>{quote(monitor.remainingQuoteVolume)} USDT</strong></div>
                  <div><span>BTC / ETH 成交量</span><strong>{quote(monitor.btcQuoteVolume)} / {quote(monitor.ethQuoteVolume)} USDT</strong></div>
                  <div><span>Maker / Taker / Unknown</span><strong>{monitor.makerFillCount} / {monitor.takerFillCount} / {monitor.unknownFillCount}</strong></div>
                  <div><span>挂单 / 撤单 / requote</span><strong>{monitor.submissions} / {monitor.cancels} / {monitor.requotes}</strong></div>
                  <div><span>数据来源</span><strong className={monitor.volumeSource !== 'ledger' || monitor.stale || monitor.reconciliationRequired ? 'monitor-unverified' : 'monitor-verified'}>{volumeState}</strong>{monitor.volumeSource === 'execution_journal' && <small className="monitor-ledger-progress">账本已同步 {quote(monitor.ledgerVerifiedQuoteVolume)} USDT · 最终完成仍待对账</small>}</div>
                </section>

                <section className="active-waits" aria-label="当前等待">
                  <header><span>当前活动</span><small>{monitor.activeWaits.length ? `${monitor.activeWaits.length} 项并行等待` : '无活动等待'}</small></header>
                  {monitor.activeWaits.map((wait) => {
                    const delta = Math.max(0, serverNowMs - wait.updatedAtMs)
                    const elapsed = wait.startedAtMs !== null && wait.startedAtMs !== undefined
                      ? Math.max(0, serverNowMs - wait.startedAtMs)
                      : wait.elapsedMs + delta
                    const remaining = wait.remainingMs === null
                      ? null
                      : Math.max(
                        0,
                        wait.deadlineAtMs !== null && wait.deadlineAtMs !== undefined
                          ? wait.deadlineAtMs - serverNowMs
                          : wait.remainingMs - delta,
                      )
                    const total = remaining === null ? 0 : Math.max(1, elapsed + remaining)
                    return <div className="active-wait-row" key={wait.key}>
                      <LoaderCircle className="spin" size={14} />
                      <div><strong>{wait.label}</strong>{wait.detail && <span>{wait.detail}</span>}</div>
                      <time>已等待 {(elapsed / 1000).toFixed(1)}s{remaining !== null && <> / 剩余 {(remaining / 1000).toFixed(1)}s</>}</time>
                      {remaining !== null && <span className="wait-progress"><i style={{ width: `${Math.min(100, elapsed / total * 100)}%` }} /></span>}
                    </div>
                  })}
                </section>

                <section className="monitor-timeline" aria-label="执行时间线">
                  <header><span>执行时间线</span><small>只记录状态变化，等待心跳原地更新</small></header>
                  {monitor.hasMore && <button className="timeline-load-older" type="button" disabled={loadOlderBusy} onClick={() => void loadOlder()}>{loadOlderBusy ? <LoaderCircle className="spin" size={12} /> : <RefreshCw size={12} />}加载更早记录</button>}
                  {monitor.timeline.map((entry) => <div className={`log-line monitor-entry ${entry.level}`} key={entry.id}>
                    <time>{displayTime(entry.atMs)}</time><span className="log-level">{levelLabel[entry.level]}</span><span className="log-message"><strong>{entry.title}</strong>{entry.detail && <small>{entry.detail}</small>}</span>
                  </div>)}
                  {!monitor.timeline.length && <div className="terminal-empty">等待第一个执行事件...</div>}
                </section>
              </>}
              {error && <div className="terminal-error terminal-retry">执行监控：{error}</div>}
            </>
          ) : (
            systemLoading && !systemLines.length ? <div className="terminal-loading"><span className="terminal-cursor" />正在请求系统日志...</div> : <>
              {systemLines.map((line) => <div className={`log-line ${line.level}`} key={line.id}><time>{displayTime(line.timestamp)}</time><span className="log-level">{levelLabel[line.level]}</span><span className="log-message">{line.message}</span></div>)}
              {!systemLines.length && !systemError && <div className="terminal-empty">暂无系统日志</div>}
              {error && <div className="terminal-error terminal-retry">系统日志：{error}</div>}
            </>
          )}
        </div>
      </aside>
    </div>
  )
}
