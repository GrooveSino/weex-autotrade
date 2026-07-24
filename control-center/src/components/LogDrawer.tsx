import { useEffect, useMemo, useRef, useState } from 'react'
import {
  clearInstanceLogs,
  fetchInstanceLogs,
  fetchStrategyMonitor,
  subscribeToStrategyMonitor,
} from '../services/controlCenter'
import type { AccountInstance, LogLine, StrategyMonitorSnapshot } from '../types'
import { LogDrawerView } from './LogDrawerView'
import { formatExecutionDurations, levelLabel, mergeMonitor, quote } from './logDrawerFormat'

interface LogDrawerProps {
  account: AccountInstance | null
  sessionId?: string | null
  onClose: () => void
}

type ConnectionState = 'connecting' | 'connected' | 'retrying'

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
  const [systemReloadToken, setSystemReloadToken] = useState(0)
  const [loadOlderBusy, setLoadOlderBusy] = useState(false)
  const bodyRef = useRef<HTMLDivElement>(null)
  const followTailRef = useRef(true)
  const systemRequestGenerationRef = useRef(0)
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
    const generation = ++systemRequestGenerationRef.current
    let active = true
    let cursor: string | null = null
    let timer: number | undefined
    setSystemLoading(true)
    setSystemConnection('connecting')
    const poll = async () => {
      try {
        const batch = await fetchInstanceLogs(selectedAccount, cursor)
        if (!active || generation !== systemRequestGenerationRef.current) return
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
        if (!active || generation !== systemRequestGenerationRef.current) return
        setSystemError(reason instanceof Error ? reason.message : '系统日志加载失败')
        setSystemConnection('retrying')
      } finally {
        if (active && generation === systemRequestGenerationRef.current) {
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
  }, [accountId, systemReloadToken, tab])

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
    formatExecutionDurations(
      `[${new Date(entry.atMs).toISOString()}] ${levelLabel[entry.level]} ${entry.title}${entry.detail ? `；${entry.detail}` : ''}`,
    )
  )).join('\n') ?? '', [monitor])
  const systemText = useMemo(() => systemLines.map((line) => (
    formatExecutionDurations(`[${line.timestamp}] ${levelLabel[line.level]} ${line.message}`)
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
    systemRequestGenerationRef.current += 1
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
      setSystemReloadToken((value) => value + 1)
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
  const { volumeState, volumeDetail } = monitorVolumePresentation(monitor)

  return <LogDrawerView
    account={account}
    tab={tab}
    setTab={setTab}
    connection={connection}
    monitor={monitor}
    monitorLoading={monitorLoading}
    systemLines={systemLines}
    systemLoading={systemLoading}
    error={error}
    clearArmed={clearArmed}
    clearBusy={clearBusy}
    loadOlderBusy={loadOlderBusy}
    primaryWait={primaryWait}
    primaryWaitRemaining={primaryWaitRemaining}
    serverNowMs={serverNowMs}
    volumeState={volumeState}
    volumeDetail={volumeDetail}
    plainText={plainText}
    bodyRef={bodyRef}
    followTailRef={followTailRef}
    onClose={onClose}
    onDownload={download}
    onClear={() => void clearSystemLogs()}
    onLoadOlder={() => void loadOlder()}
  />
}

function monitorVolumePresentation(monitor: StrategyMonitorSnapshot | null) {
  if (!monitor) return { volumeState: '等待执行数据', volumeDetail: '' }
  if (monitor.ledgerSyncState === 'queued' || monitor.ledgerSyncState === 'syncing') {
    return {
      volumeState: monitor.ledgerSyncState === 'syncing' ? '成交账本同步中' : '成交账本已排队同步',
      volumeDetail: `当前已核验 ${quote(monitor.verifiedQuoteVolume)} USDT`,
    }
  }
  if (monitor.boundaryState === 'owned_exposure') {
    return { volumeState: '开仓成交已同步，任务尚未完成平仓', volumeDetail: '' }
  }
  if (monitor.boundaryState === 'flat' && monitor.auditStatus !== 'verified') {
    return { volumeState: '账户已空仓，成交审计待完成', volumeDetail: '' }
  }
  if (monitor.ledgerSyncState === 'complete' && monitor.auditStatus !== 'verified') {
    return {
      volumeState: '成交账本已同步，任务收尾待完成',
      volumeDetail: `账本已核验 ${quote(monitor.ledgerVerifiedQuoteVolume)} USDT`,
    }
  }
  if (monitor.ledgerSyncState === 'complete' && monitor.auditStatus === 'verified') {
    return { volumeState: '权威成交账本已同步', volumeDetail: '' }
  }
  if (monitor.ledgerSyncState === 'stale') {
    return { volumeState: '成交账本数据待核验', volumeDetail: `账本已有 ${quote(monitor.ledgerVerifiedQuoteVolume)} USDT` }
  }
  if (monitor.volumeSource === 'execution_journal') {
    return { volumeState: '执行器成交已核验，账本待更新', volumeDetail: `账本已有 ${quote(monitor.ledgerVerifiedQuoteVolume)} USDT` }
  }
  return { volumeState: '等待首笔权威成交', volumeDetail: '' }
}
