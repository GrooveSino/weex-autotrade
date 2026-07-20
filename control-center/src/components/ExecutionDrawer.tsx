import { useEffect, useRef, useState } from 'react'
import { AlertTriangle, CheckCircle2, CircleDot, Copy, RefreshCw, ScrollText, ShieldAlert, X, XCircle } from 'lucide-react'
import { fetchExecutionHistory } from '../services/controlCenter'
import type { AccountInstance, ExecutionCycle } from '../types'
import { compactDuration } from '../utils/strategy'

interface ExecutionDrawerProps {
  account: AccountInstance | null
  onClose: () => void
}

const statusMeta: Record<ExecutionCycle['status'], { label: string; icon: typeof CircleDot }> = {
  planned: { label: '已准备', icon: CircleDot },
  opened: { label: '持仓中', icon: CircleDot },
  completed: { label: '已完成', icon: CheckCircle2 },
  rejected: { label: '已拒绝', icon: XCircle },
  uncertain: { label: '待核对', icon: AlertTriangle },
}

const quote = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 8 })

function formatQuote(value: string): string {
  const parsed = Number(value)
  return Number.isFinite(parsed) ? quote.format(parsed) : value
}

function formatTime(value: number): string {
  return new Date(value).toLocaleString('zh-CN', { hour12: false })
}

export function ExecutionDrawer({ account, onClose }: ExecutionDrawerProps) {
  const accountRef = useRef(account)
  const [requestNonce, setRequestNonce] = useState(0)
  const [result, setResult] = useState<{ key: string; cycles: ExecutionCycle[]; error: string | null }>({
    key: '',
    cycles: [],
    error: null,
  })
  const accountId = account?.id ?? ''
  const requestKey = accountId ? `${accountId}:${requestNonce}` : ''
  const loading = Boolean(account) && result.key !== requestKey
  const cycles = result.key === requestKey ? result.cycles : []
  const error = result.key === requestKey ? result.error : null
  const counts = {
    completed: cycles.filter((cycle) => cycle.status === 'completed').length,
    rejected: cycles.filter((cycle) => cycle.status === 'rejected').length,
    uncertain: cycles.filter((cycle) => cycle.reconciliationRequired).length,
  }

  useEffect(() => {
    accountRef.current = account
  }, [account])

  useEffect(() => {
    const selected = accountRef.current
    if (!selected || selected.id !== accountId) return
    let active = true
    const currentKey = requestKey
    fetchExecutionHistory(selected)
      .then((history) => {
        if (active) setResult({ key: currentKey, cycles: history, error: null })
      })
      .catch((reason: unknown) => {
        if (active) setResult({
          key: currentKey,
          cycles: [],
          error: reason instanceof Error ? reason.message : '执行审计加载失败',
        })
    })
    return () => { active = false }
  }, [accountId, requestKey])

  if (!account) return null

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={onClose}>
      <aside className="execution-drawer" role="dialog" aria-modal="true" aria-labelledby="execution-title" onMouseDown={(event) => event.stopPropagation()}>
        <header className="execution-header">
          <div className="execution-title">
            <span className="execution-mark"><ScrollText size={16} /></span>
            <div>
              <h2 id="execution-title">{account.name}</h2>
              <span>{account.id} / 执行记录</span>
            </div>
          </div>
          <div className="execution-actions">
            <button className="icon-button" type="button" onClick={() => setRequestNonce((value) => value + 1)} data-tooltip="重新加载执行审计" aria-label="重新加载执行审计"><RefreshCw size={15} /></button>
            <button className="icon-button" type="button" onClick={onClose} data-tooltip="关闭执行审计" aria-label="关闭执行审计"><X size={16} /></button>
          </div>
        </header>

        <div className="execution-summary" aria-label="执行审计概况">
          <span>最近 <strong>{cycles.length}</strong></span>
          <span className="completed">完成 <strong>{counts.completed}</strong></span>
          <span className="rejected">拒绝 <strong>{counts.rejected}</strong></span>
          <span className={counts.uncertain ? 'uncertain' : ''}>待核对 <strong>{counts.uncertain}</strong></span>
          <span className="policy"><ShieldAlert size={12} />禁止自动重试</span>
        </div>

        <div className="execution-body" aria-live="polite">
          {counts.uncertain > 0 && (
            <div className="execution-warning">
              <AlertTriangle size={16} />
              <p>存在结果不确定的周期。必须使用周期 ID 和交易所订单记录人工对账；当前页面不会重试、补单或改写结果。</p>
            </div>
          )}
          {loading ? (
            <div className="execution-state"><RefreshCw size={17} className="spin" />正在按需读取执行日志</div>
          ) : error ? (
            <div className="execution-state error"><XCircle size={17} />{error}</div>
          ) : cycles.length === 0 ? (
            <div className="execution-state"><ScrollText size={18} />该实例尚无执行周期</div>
          ) : (
            <div className="execution-table-shell">
              <table className="execution-table">
                <thead><tr><th>周期 / 贡献</th><th>状态 / 时间</th><th className="numeric">BTC 多</th><th className="numeric">ETH 空</th><th>比例版本 / 原因</th><th>更新时间</th><th /></tr></thead>
                <tbody>
                  {cycles.map((cycle) => {
                    const MetaIcon = statusMeta[cycle.status].icon
                    return (
                      <tr key={cycle.cycleId} className={cycle.reconciliationRequired ? 'needs-reconciliation' : ''}>
                        <td><strong>#{cycle.sequence} · {formatQuote(cycle.turnoverQuote)} USDT</strong><span className="cycle-id">{cycle.cycleId}</span></td>
                        <td><span className={`execution-status ${cycle.status}`}><MetaIcon size={12} />{statusMeta[cycle.status].label}</span><span className="reason-code">持仓 {compactDuration(cycle.positionHoldSeconds)} · 间隔 {compactDuration(cycle.roundIntervalSeconds)}</span></td>
                        <td className="numeric"><strong>{formatQuote(cycle.btcLongQuote)}</strong><span>USDT</span></td>
                        <td className="numeric"><strong>{formatQuote(cycle.ethShortQuote)}</strong><span>USDT</span></td>
                        <td><strong className="allocation-version">{cycle.allocationVersion}</strong><span className="reason-code">{cycle.reason}</span></td>
                        <td><time>{formatTime(cycle.updatedAtMs)}</time></td>
                        <td><button className="icon-button compact" type="button" onClick={() => navigator.clipboard.writeText(cycle.cycleId)} data-tooltip="复制周期 ID" aria-label="复制周期 ID"><Copy size={13} /></button></td>
                      </tr>
                    )
                  })}
                </tbody>
              </table>
            </div>
          )}
        </div>
      </aside>
    </div>
  )
}
