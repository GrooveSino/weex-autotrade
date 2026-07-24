import type { LogLine, StrategyMonitorSnapshot } from '../types'

export const levelLabel: Record<LogLine['level'], string> = {
  info: '信息',
  success: '成功',
  warn: '警告',
  error: '错误',
}

export function displayTime(value: string | number): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime())
    ? String(value)
    : parsed.toLocaleTimeString('zh-CN', { hour12: false })
}

export function quote(value: string): string {
  const parsed = Number(value)
  return Number.isFinite(parsed)
    ? parsed.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 4 })
    : value
}

export function formatExecutionDurations(message: string): string {
  return message.replace(/(\d+\.\d{2,})s\b/g, (raw, seconds: string) => {
    const parsed = Number(seconds)
    return Number.isFinite(parsed) ? `${parsed.toFixed(1)}s` : raw
  })
}

export function countdown(valueMs: number | null): string {
  if (valueMs === null) return '--:--.-'
  const remaining = Math.max(0, valueMs)
  const minutes = Math.floor(remaining / 60_000)
  const seconds = Math.floor((remaining % 60_000) / 1_000)
  const tenths = Math.floor((remaining % 1_000) / 100)
  return `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}.${tenths}`
}

export function mergeMonitor(
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
