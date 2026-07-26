import { useRef, useState } from 'react'
import { mockAccounts, mockStrategies } from '../data/mockAccounts'
import { controlPlaneEnabled } from '../services'
import type {
  AccountInstance,
  BetaMarketSnapshot,
  ExecutionCapacity,
  SchedulerMetrics,
  StatusFilter,
  VolumeStrategy,
} from '../types'

export const fleetFilters: { value: StatusFilter; label: string }[] = [
  { value: 'all', label: '全部' },
  { value: 'running', label: '运行中' },
  { value: 'paused', label: '已暂停' },
  { value: 'warning', label: '需处理' },
  { value: 'error', label: '错误' },
  { value: 'stopped', label: '已停止' },
]

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

export function snapshotTimeText(now = new Date()): string {
  return now.toLocaleTimeString('zh-CN', {
    hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
  })
}

export function useFleetState() {
  const [accounts, setAccounts] = useState<AccountInstance[]>(() => controlPlaneEnabled ? [] : mockAccounts)
  const [strategies, setStrategies] = useState<VolumeStrategy[]>(() => controlPlaneEnabled ? [] : mockStrategies)
  const [search, setSearch] = useState(() => sessionStorage.getItem('weex-fleet.search') ?? '')
  const [filter, setFilter] = useState<StatusFilter>(() => {
    const saved = sessionStorage.getItem('weex-fleet.filter')
    return fleetFilters.some((item) => item.value === saved) ? saved as StatusFilter : 'all'
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
  const [lastGlobalSync, setLastGlobalSync] = useState(controlPlaneEnabled ? '等待加载' : snapshotTimeText())
  const [controlPlaneConnected, setControlPlaneConnected] = useState(!controlPlaneEnabled)
  const [controlPlaneAdapter, setControlPlaneAdapter] = useState(controlPlaneEnabled ? 'connecting' : 'browser-mock')
  const [serviceReleaseId, setServiceReleaseId] = useState<string | null>(null)
  const [controlPlaneExecutionEnabled, setControlPlaneExecutionEnabled] = useState(!controlPlaneEnabled)
  const [boundStrategyExecutionEnabled, setBoundStrategyExecutionEnabled] = useState(!controlPlaneEnabled)
  const [executionCapacity, setExecutionCapacity] = useState<ExecutionCapacity | null>(null)
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

  return {
    accounts, setAccounts, strategies, setStrategies, search, setSearch, filter, setFilter,
    selectedIds, setSelectedIds, refreshingIds, setRefreshingIds, actioningIds, setActioningIds,
    logAccount, setLogAccount, logSessionId, setLogSessionId, executionAccount, setExecutionAccount,
    accountDialogOpen, setAccountDialogOpen, editingAccount, setEditingAccount,
    strategyDialogOpen, setStrategyDialogOpen, betaSourceDialogOpen, setBetaSourceDialogOpen,
    strategyDialogInitialId, setStrategyDialogInitialId, assignmentAccounts, setAssignmentAccounts,
    closePositionsAccount, setClosePositionsAccount, stopDialogOpen, setStopDialogOpen,
    stopPhrase, setStopPhrase, toast, setToast, lastGlobalSync, setLastGlobalSync,
    controlPlaneConnected, setControlPlaneConnected, controlPlaneAdapter, setControlPlaneAdapter,
    serviceReleaseId, setServiceReleaseId,
    controlPlaneExecutionEnabled, setControlPlaneExecutionEnabled,
    boundStrategyExecutionEnabled, setBoundStrategyExecutionEnabled,
    executionCapacity, setExecutionCapacity,
    boundExecutionQueue, setBoundExecutionQueue,
    initialControlPlaneSnapshotLoaded, setInitialControlPlaneSnapshotLoaded,
    initialControlPlaneError, setInitialControlPlaneError,
    schedulerMetrics, setSchedulerMetrics, betaSnapshot, setBetaSnapshot,
    betaAvailable, setBetaAvailable, betaLoading, setBetaLoading,
    betaReceivedAtMs, setBetaReceivedAtMs, pendingWebReleaseId, setPendingWebReleaseId,
    localUser, setLocalUser, localUserLoading, setLocalUserLoading, localUserError, setLocalUserError,
    searchInputRef, betaSnapshotRef, betaReceivedAtRef, actioningIdsRef, webReleaseIdRef,
  }
}

export type FleetState = ReturnType<typeof useFleetState>
