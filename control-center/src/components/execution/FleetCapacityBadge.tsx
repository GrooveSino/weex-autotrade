import { Network } from 'lucide-react'
import type { ExecutionCapacity } from '../../types/controlPlane'

interface FleetCapacityBadgeProps {
  capacity: ExecutionCapacity | null
  loading: boolean
}

export function FleetCapacityBadge({ capacity, loading }: FleetCapacityBadgeProps) {
  if (loading || !capacity) {
    return <span className="scheduler-state"><Network size={13} />执行容量读取中</span>
  }

  const title = [
    `生命周期 ${capacity.activeExecutions}/${capacity.maxActiveExecutions}`,
    `正常开平仓 ${capacity.activeNormalPhases}/${capacity.maxNormalPhases}`,
    `阶段队列 ${capacity.queuedNormalPhases}（代理限速 ${capacity.queuedProxyLimitedPhases}）`,
    `启动预算 ${capacity.phaseStartRatePerSecond}/秒 · 每代理间隔 ${capacity.perProxyGapSeconds}秒`,
    `阶段等待 p50/p95 ${capacity.phaseQueueP50Ms}/${capacity.phaseQueueP95Ms}ms`,
    `普通 I/O ${capacity.activeNormalIo}/${capacity.maxNormalIo}`,
    `安全 I/O ${capacity.activeEmergencyIo}/${capacity.maxEmergencyIo}`,
    `活动代理分片 ${capacity.activeProxyPhasePartitions} · Actor ${capacity.actorCount}`,
    `SQLite 队列 ${capacity.sqliteWriteQueueCritical}/${capacity.sqliteWriteQueueLowPriority} · p95 ${capacity.sqliteWriteP95Ms}ms`,
    `行情/私有流租赁 ${capacity.marketDataActiveLeases}/${capacity.privateOrderStreamActiveLeases}`,
    `成交同步 排队 ${capacity.historySyncQueued} · 运行 ${capacity.historySyncRunning}`,
  ].join(' · ')

  return (
    <span className={`scheduler-state ${capacity.queuedNormalPhases ? 'degraded' : ''}`} title={title}>
      <Network size={13} />生命周期 {capacity.activeExecutions}/{capacity.maxActiveExecutions}
      <small>阶段 {capacity.activeNormalPhases}/{capacity.maxNormalPhases} · 排队 {capacity.queuedNormalPhases} · 代理限速 {capacity.queuedProxyLimitedPhases}</small>
    </span>
  )
}
