import { useState } from 'react'
import { BarChart3, Clock3, RefreshCw, ShieldCheck, X } from 'lucide-react'
import type { AccountInstance, AccountTradeVolumePeriod } from '../../types'
import { fetchAccountTradeVolumeReport } from '../../services'

interface AccountTradeVolumeDialogProps {
  account: AccountInstance
  onClose: () => void
}

const quote = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })

function rangeText(period: AccountTradeVolumePeriod): string {
  const format = new Intl.DateTimeFormat('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' })
  return `${format.format(period.startAtMs)} 至 ${format.format(period.endAtMs)}`
}

export function AccountTradeVolumeDialog({ account, onClose }: AccountTradeVolumeDialogProps) {
  const [periods, setPeriods] = useState<AccountTradeVolumePeriod[]>([])
  const [loading, setLoading] = useState<'short' | 'month' | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [generatedAtMs, setGeneratedAtMs] = useState<number | null>(null)

  const load = async (scope: 'short' | 'month') => {
    if (loading) return
    setLoading(scope)
    setError(null)
    try {
      const report = await fetchAccountTradeVolumeReport(account, scope === 'short' ? [1, 7] : [30])
      setPeriods((current) => {
        const next = new Map(current.map((period) => [period.lookbackDays, period]))
        report.periods.forEach((period) => next.set(period.lookbackDays, period))
        return [...next.values()].sort((left, right) => left.lookbackDays - right.lookbackDays)
      })
      setGeneratedAtMs(report.generatedAtMs)
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : '近期交易量暂时无法统计。请检查账号代理和 API 只读权限后重试。')
    } finally {
      setLoading(null)
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="dialog trade-volume-dialog" role="dialog" aria-modal="true" aria-labelledby="trade-volume-title" onMouseDown={(event) => event.stopPropagation()}>
        <header className="dialog-header">
          <div><h2 id="trade-volume-title">近期交易量</h2><span>{account.name} · 只读统计</span></div>
          <button className="icon-button" type="button" onClick={onClose} data-tooltip="关闭" aria-label="关闭"><X size={16} /></button>
        </header>
        <div className="trade-volume-report-body">
          <div className="trade-volume-intro"><ShieldCheck size={16} /><span>按 WEEX 实际成交 `quoteQty` 汇总；不会修改策略、仓位、挂单或成交账本。</span></div>
          <div className="trade-volume-actions">
            <button className="button" type="button" disabled={loading !== null} onClick={() => void load('short')}>
              {loading === 'short' ? <RefreshCw className="spin" size={15} /> : <BarChart3 size={15} />}统计近 1 天与 7 天
            </button>
            <button className="button secondary" type="button" disabled={loading !== null} onClick={() => void load('month')}>
              {loading === 'month' ? <RefreshCw className="spin" size={15} /> : <Clock3 size={15} />}统计近 30 天
            </button>
          </div>
          {error && <div className="execution-warning" role="alert">{error}</div>}
          {periods.length === 0 && !error && <div className="trade-volume-empty">选择一个统计周期后，系统会通过该账号自身的代理与 API 凭据读取已成交历史。</div>}
          {periods.map((period) => (
            <article className="trade-volume-period" key={period.lookbackDays}>
              <div><strong>近 {period.lookbackDays} 天</strong><span>{rangeText(period)}</span></div>
              <strong>{quote.format(Number(period.totalQuoteVolume))} <small>USDT</small></strong>
              <span>{period.tradeCount} 笔成交 · Maker {quote.format(Number(period.makerQuoteVolume))} · Taker {quote.format(Number(period.takerQuoteVolume))}</span>
              <em className={period.complete ? 'complete' : 'pending'}>{period.complete ? '数据完整' : '数据待核验'}</em>
              {period.warnings.map((warning) => <p key={warning}>{warning}</p>)}
            </article>
          ))}
          {generatedAtMs && <small className="trade-volume-generated">统计完成于 {new Date(generatedAtMs).toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' })}</small>}
        </div>
      </section>
    </div>
  )
}
