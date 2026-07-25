import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, CircleStop, Clock3, History, LoaderCircle, RefreshCw, Terminal, X } from 'lucide-react'
import { fetchStrategyRuns } from '../../services'
import type { AccountInstance, StrategyRunSummary } from '../../types'

interface ExecutionDrawerProps {
  account: AccountInstance | null
  onClose: () => void
  onOpenMonitor: (account: AccountInstance, sessionId: string) => void
}

const quote = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 8 })

const resultMeta: Record<string, { label: string; icon: typeof Clock3 }> = {
  active: { label: '运行中', icon: Clock3 },
  recovering: { label: '后台核验', icon: Clock3 },
  stopping: { label: '停止中', icon: Clock3 },
  completed: { label: '已完成', icon: CheckCircle2 },
  stopped: { label: '已停止', icon: CircleStop },
}

function formatQuote(value: string): string {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? quote.format(parsed) : value
}

function formatSignedQuote(value: string): string {
  const parsed = Number(value)
  if (!Number.isFinite(parsed)) return value
  const formatted = quote.format(parsed)
  return parsed > 0 ? `+${formatted}` : formatted
}

function formatTime(value: number | null): string {
  if (value === null) return '进行中'
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

export function ExecutionDrawer({ account, onClose, onOpenMonitor }: ExecutionDrawerProps) {
  const accountRef = useRef(account)
  const [requestNonce, setRequestNonce] = useState(0)
  const [result, setResult] = useState<{ key: string; runs: StrategyRunSummary[]; cursor: string | null; error: string | null }>({
    key: '',
    runs: [],
    cursor: null,
    error: null,
  })
  const [loadingMore, setLoadingMore] = useState(false)
  const accountId = account?.id ?? ''
  const requestKey = accountId ? `${accountId}:${requestNonce}` : ''
  const loading = Boolean(account) && result.key !== requestKey
  const runs = result.key === requestKey ? result.runs : []
  const cursor = result.key === requestKey ? result.cursor : null
  const error = result.key === requestKey ? result.error : null

  useEffect(() => { accountRef.current = account }, [account])

  useEffect(() => {
    const selected = accountRef.current
    if (!selected || selected.id !== accountId) return
    let active = true
    const currentKey = requestKey
    fetchStrategyRuns(selected)
      .then((page) => {
        if (active) setResult({ key: currentKey, runs: page.items, cursor: page.nextCursor, error: null })
      })
      .catch((reason: unknown) => {
        if (active) setResult({
          key: currentKey,
          runs: [],
          cursor: null,
          error: reason instanceof Error ? reason.message : '策略运行记录加载失败',
        })
      })
    return () => { active = false }
  }, [accountId, requestKey])

  if (!account) return null

  const completed = runs.filter((run) => run.status === 'completed').length
  const needsVerification = runs.filter((run) => run.auditStatus !== 'verified').length

  const loadMore = async () => {
    if (!cursor || loadingMore) return
    setLoadingMore(true)
    try {
      const page = await fetchStrategyRuns(account, cursor)
      setResult((current) => ({ ...current, runs: [...current.runs, ...page.items], cursor: page.nextCursor }))
    } catch (reason) {
      setResult((current) => ({ ...current, error: reason instanceof Error ? reason.message : '更多记录加载失败' }))
    } finally {
      setLoadingMore(false)
    }
  }

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={onClose}>
      <aside className="execution-drawer" role="dialog" aria-modal="true" aria-labelledby="execution-title" onMouseDown={(event) => event.stopPropagation()}>
        <header className="execution-header">
          <div className="execution-title">
            <span className="execution-mark"><History size={16} /></span>
            <div><h2 id="execution-title">{account.name}</h2><span>策略运行记录</span></div>
          </div>
          <div className="execution-actions">
            <button className="icon-button" type="button" onClick={() => setRequestNonce((value) => value + 1)} data-tooltip="刷新运行记录" aria-label="刷新运行记录"><RefreshCw size={15} /></button>
            <button className="icon-button" type="button" onClick={onClose} data-tooltip="关闭" aria-label="关闭"><X size={16} /></button>
          </div>
        </header>

        <div className="execution-summary" aria-label="策略运行概况">
          <span>记录 <strong>{runs.length}</strong></span>
          <span className="completed">完成 <strong>{completed}</strong></span>
          <span className={needsVerification ? 'uncertain' : ''}>待核验 <strong>{needsVerification}</strong></span>
        </div>

        <div className="execution-body" aria-live="polite">
          {needsVerification > 0 && <div className="execution-warning"><AlertTriangle size={16} /><p>部分历史记录的成交审计尚未完成。审计状态不会自动重试订单，也不会阻止新的增量任务。</p></div>}
          {loading ? (
            <div className="execution-state"><LoaderCircle size={17} className="spin" />正在读取策略运行记录</div>
          ) : error && runs.length === 0 ? (
            <div className="execution-state error"><AlertTriangle size={17} />{error}</div>
          ) : runs.length === 0 ? (
            <div className="execution-state"><History size={18} />该账号尚无策略运行记录</div>
          ) : (
            <div className="execution-table-shell">
              <table className="execution-table strategy-run-table">
                <thead><tr><th>策略 / 口径</th><th>开始 / 结束</th><th>结果</th><th className="numeric">任务目标</th><th className="numeric">权威完成</th><th className="numeric">累计变化</th><th className="numeric">可用余额变化</th><th aria-label="查看监控" /></tr></thead>
                <tbody>
                  {runs.map((run) => {
                    const meta = resultMeta[run.status] ?? resultMeta.stopped
                    const MetaIcon = meta.icon
                    const lifetimeEnd = run.finalLifetimeQuoteVolume
                    const balanceStart = run.startingAvailableBalanceQuote
                    const balanceEnd = run.endingAvailableBalanceQuote
                    const balanceDelta = run.availableBalanceChangeQuote
                    const balanceTone = balanceDelta === null
                      ? ''
                      : Number(balanceDelta) > 0 ? 'positive' : Number(balanceDelta) < 0 ? 'negative' : ''
                    return (
                      <tr key={run.sessionId} className={run.stale || run.reconciliationRequired ? 'needs-reconciliation' : ''}>
                        <td className="run-strategy-cell" data-label="策略"><strong>{run.strategyName ?? '历史策略'}{run.strategyVersion ? ` v${run.strategyVersion}` : ''}</strong><span>{run.targetMode === 'incremental' ? '每次新增' : '累计达到'}</span></td>
                        <td className="run-time-cell" data-label="时间"><time>{formatTime(run.startedAtMs)}</time><span>{formatTime(run.finishedAtMs)}</span></td>
                        <td className="run-status-cell" data-label="结果"><span className={`execution-status ${run.status}`}><MetaIcon size={12} />{meta.label}</span></td>
                        <td className="numeric run-target-cell" data-label="任务目标"><strong>{formatQuote(run.executionTargetQuoteVolume)}</strong><span>USDT</span></td>
                        <td className="numeric run-verified-cell" data-label="权威完成"><strong>{formatQuote(run.verifiedQuoteVolume)}</strong><span>USDT</span></td>
                        <td className="numeric run-lifetime-cell" data-label="累计变化"><strong>{formatQuote(run.baselineLifetimeQuoteVolume)}</strong><span>{lifetimeEnd ? `→ ${formatQuote(lifetimeEnd)}` : '→ --'}</span></td>
                        <td className="numeric balance-change run-balance-cell" data-label="余额变化"><strong>{balanceStart === null ? '--' : formatQuote(balanceStart)} → {balanceEnd === null ? '--' : formatQuote(balanceEnd)}</strong><span className={balanceTone}>{balanceDelta === null ? (balanceStart === null ? '历史未记录' : '结束快照缺失') : `变化 ${formatSignedQuote(balanceDelta)} USDT`}</span></td>
                        <td className="run-monitor-cell"><button className="icon-button compact" type="button" onClick={() => onOpenMonitor(account, run.sessionId)} data-tooltip="查看执行监控" aria-label="查看执行监控"><Terminal size={13} /><span className="mobile-action-label">监控</span></button></td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
              {cursor && <div className="execution-load-more"><button className="button secondary compact-button" type="button" disabled={loadingMore} onClick={() => void loadMore()}>{loadingMore && <LoaderCircle size={13} className="spin" />}加载更早记录</button></div>}
              {error && <div className="execution-inline-error">{error}</div>}
            </div>
          )}
        </div>
      </aside>
    </div>
  )
}
