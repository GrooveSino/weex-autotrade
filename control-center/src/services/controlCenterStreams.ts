import type {
  AccountInstance, InstanceSnapshotEvent, LogBatch, LogLine,
  StrategyMonitorEvent, StrategyMonitorSnapshot,
} from '../types'
import { apiRequest, configuredBaseUrl, controlPlaneEnabled, sleep } from './controlCenterCore'

export function subscribeToInstanceEvents(
  onSnapshot: (snapshot: InstanceSnapshotEvent) => void,
  onConnectionChange: (connected: boolean) => void,
): () => void {
  if (!configuredBaseUrl) return () => undefined
  let executorGeneration: string | null = null
  let lastSequence = -1
  let retryTimer: number | undefined
  let closed = false
  const source = new EventSource(`${configuredBaseUrl}/events`, { withCredentials: true })
  source.onopen = () => {
    if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    retryTimer = undefined
    onConnectionChange(true)
  }
  source.onerror = () => {
    if (closed || retryTimer !== undefined) return
    // EventSource normally reconnects on its own.  Avoid flashing the whole
    // console into a disconnected state for a transient transport reset.
    retryTimer = window.setTimeout(() => {
      retryTimer = undefined
      if (!closed && source.readyState !== EventSource.OPEN) onConnectionChange(false)
    }, 3_000)
  }
  source.addEventListener('instances', (event) => {
    try {
      const snapshot = JSON.parse((event as MessageEvent<string>).data) as InstanceSnapshotEvent
      const sequence = snapshot.sequence
      if (
        snapshot.type !== 'instances'
        || !snapshot.executorGeneration
        || typeof sequence !== 'number'
        || !Number.isSafeInteger(sequence)
      ) return
      if (executorGeneration !== snapshot.executorGeneration) {
        executorGeneration = snapshot.executorGeneration
        lastSequence = -1
      }
      if (sequence <= lastSequence) return
      lastSequence = sequence
      onSnapshot(snapshot)
    } catch {
      onConnectionChange(false)
    }
  })
  return () => {
    closed = true
    if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    source.close()
  }
}

const mockLogStreams = new Map<string, LogLine[]>()

export function appendMockLog(accountId: string, level: LogLine['level'], message: string): void {
  const stream = mockLogStreams.get(accountId)
  if (!stream) return
  stream.push({
    id: `${accountId}-manual-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
    timestamp: new Date().toISOString(),
    level,
    message,
  })
  if (stream.length > 500) stream.splice(0, stream.length - 500)
}

export function mockLogStream(account: AccountInstance): LogLine[] {
  const existing = mockLogStreams.get(account.id)
  if (existing) return existing

  const now = Date.now()
  const line = (offset: number, level: LogLine['level'], message: string): LogLine => ({
    id: `${account.id}-initial-${offset}-${level}`,
    timestamp: new Date(now - offset * 1000).toISOString(),
    level,
    message,
  })
  const lines = account.status === 'error'
    ? [
        line(73, 'info', `实例 ${account.id} 已请求启动工作进程`),
        line(70, 'info', `正在检查代理连通性：${account.proxy.host}`),
        line(68, 'error', '代理认证失败，工作进程保持停止'),
        line(66, 'warn', '已禁用自动重试；账户边界将在后台只读核验'),
      ]
    : [
        line(58, 'info', `已收到实例 ${account.id} 的账号快照`),
        line(47, 'success', `合约钱包权益：${account.wallet.equity.toFixed(2)} USDT`),
        line(33, 'info', `累计交易量：${account.volume.lifetime.toFixed(2)} USDT，历史完整：${account.volume.complete ? '是' : '否'}`),
        line(21, 'info', `当前阶段：${account.phase}`),
        line(8, 'success', `代理正常，延迟：${account.proxy.latencyMs ?? '-'} ms`),
      ]
  mockLogStreams.set(account.id, lines)
  return lines
}

export async function fetchInstanceLogs(account: AccountInstance, after: string | null = null): Promise<LogBatch> {
  if (controlPlaneEnabled) {
    const query = new URLSearchParams({ limit: '200' })
    if (after) query.set('after', after)
    return apiRequest<LogBatch>(`/instances/${account.id}/log-updates?${query}`)
  }
  await sleep(520)
  const stream = mockLogStream(account)
  const sequence = stream.length + 1
  stream.push({
    id: `${account.id}-heartbeat-${sequence}`,
    timestamp: new Date().toISOString(),
    level: 'info',
    message: `状态心跳正常：${account.status}`,
  })
  if (stream.length > 500) stream.splice(0, stream.length - 500)

  if (!after) {
    const lines = stream.slice(-200)
    return { lines, cursor: lines.at(-1)?.id ?? null, reset: false }
  }
  const cursorIndex = stream.findIndex((line) => line.id === after)
  if (cursorIndex < 0) {
    const lines = stream.slice(-200)
    return { lines, cursor: lines.at(-1)?.id ?? null, reset: true }
  }
  const lines = stream.slice(cursorIndex + 1, cursorIndex + 201)
  return { lines, cursor: lines.at(-1)?.id ?? after, reset: false }
}

export async function fetchStrategyMonitor(
  account: AccountInstance,
  beforeSequence: number | null = null,
  sessionId: string | null = null,
): Promise<StrategyMonitorSnapshot> {
  const query = new URLSearchParams({ limit: '200' })
  if (beforeSequence !== null) query.set('beforeSequence', String(beforeSequence))
  if (sessionId) query.set('sessionId', sessionId)
  return apiRequest<StrategyMonitorSnapshot>(`/instances/${account.id}/strategy-monitor?${query}`)
}

export function subscribeToStrategyMonitor(
  account: AccountInstance,
  cursor: string | null,
  sessionId: string | null,
  onEvent: (event: StrategyMonitorEvent) => void,
  onConnectionChange: (state: 'connected' | 'retrying') => void,
): () => void {
  if (!configuredBaseUrl) return () => undefined
  let closed = false
  let retryTimer: number | undefined
  let reconnectTimer: number | undefined
  let source: EventSource | null = null
  let resumeCursor = cursor
  let lastMessageAt = Date.now()
  const receive = (message: Event) => {
    try {
      const event = message as MessageEvent<string>
      if (event.lastEventId) resumeCursor = event.lastEventId
      lastMessageAt = Date.now()
      onEvent(JSON.parse(event.data) as StrategyMonitorEvent)
      onConnectionChange('connected')
    } catch {
      onConnectionChange('retrying')
    }
  }
  const connect = () => {
    if (closed) return
    const query = new URLSearchParams()
    if (resumeCursor) query.set('after', resumeCursor)
    if (sessionId) query.set('sessionId', sessionId)
    const suffix = query.size ? `?${query}` : ''
    source = new EventSource(`${configuredBaseUrl}/instances/${account.id}/strategy-monitor/events${suffix}`, {
      withCredentials: true,
    })
    source.onopen = () => {
      if (retryTimer !== undefined) window.clearTimeout(retryTimer)
      retryTimer = undefined
      if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer)
      reconnectTimer = undefined
      lastMessageAt = Date.now()
      onConnectionChange('connected')
    }
    source.onerror = () => {
      if (retryTimer !== undefined) return
      retryTimer = window.setTimeout(() => {
        retryTimer = undefined
        if (!closed && source?.readyState !== EventSource.OPEN && Date.now() - lastMessageAt > 15_000) {
          onConnectionChange('retrying')
        }
      }, 3_000)
    }
    source.addEventListener('snapshot', receive)
    source.addEventListener('delta', receive)
    source.addEventListener('reset', receive)
    source.addEventListener('heartbeat', receive)
  }
  connect()
  const watchdog = window.setInterval(() => {
    // The executor emits a heartbeat every five seconds.  Leave room for a
    // brief proxy/Caddy hiccup before deliberately recreating the stream.
    if (Date.now() - lastMessageAt <= 20_000) return
    source?.close()
    source = null
    onConnectionChange('retrying')
    lastMessageAt = Date.now()
    if (reconnectTimer === undefined) {
      reconnectTimer = window.setTimeout(() => {
        reconnectTimer = undefined
        connect()
      }, 750)
    }
  }, 1_000)
  return () => {
    closed = true
    if (retryTimer !== undefined) window.clearTimeout(retryTimer)
    if (reconnectTimer !== undefined) window.clearTimeout(reconnectTimer)
    window.clearInterval(watchdog)
    source?.close()
  }
}

export async function clearInstanceLogs(account: AccountInstance): Promise<void> {
  if (controlPlaneEnabled) {
    await apiRequest<void>(`/instances/${account.id}/log-updates`, { method: 'DELETE' })
    return
  }
  mockLogStreams.set(account.id, [])
}
