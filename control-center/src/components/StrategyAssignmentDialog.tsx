import { useMemo, useState, type FormEvent } from 'react'
import { ChartNoAxesCombined, ShieldAlert, X } from 'lucide-react'
import type { AccountInstance, VolumeStrategy } from '../types'
import { targetModeLabel } from '../utils/strategy'

interface StrategyAssignmentDialogProps {
  accounts: AccountInstance[]
  strategies: VolumeStrategy[]
  onClose: () => void
  onAssign: (strategy: VolumeStrategy) => Promise<boolean>
}

const quote = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })

export function StrategyAssignmentDialog({ accounts, strategies, onClose, onAssign }: StrategyAssignmentDialogProps) {
  const initialId = accounts.length === 1 ? accounts[0]?.strategyId : strategies[0]?.id
  const [selectedId, setSelectedId] = useState(initialId ?? '')
  const [busy, setBusy] = useState(false)
  const selected = strategies.find((strategy) => strategy.id === selectedId)
  const blocked = useMemo(() => accounts.filter((account) => (
    account.status !== 'stopped'
    || account.strategyProgress.stage === 'holding'
    || account.exposure.btcLong !== 0
    || account.exposure.ethShort !== 0
  )), [accounts])

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!selected || blocked.length || busy) return
    setBusy(true)
    const assigned = await onAssign(selected)
    setBusy(false)
    if (assigned) onClose()
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="dialog strategy-assignment-dialog" role="dialog" aria-modal="true" aria-labelledby="assignment-dialog-title" onMouseDown={(event) => event.stopPropagation()}>
        <header className="dialog-header">
          <div><h2 id="assignment-dialog-title">应用共享策略</h2><span>{accounts.length} 个账号实例</span></div>
          <button className="icon-button" type="button" onClick={onClose} data-tooltip="关闭" aria-label="关闭"><X size={16} /></button>
        </header>
        <form onSubmit={submit}>
          <div className="assignment-account-strip">
            {accounts.slice(0, 5).map((account) => <span key={account.id}>{account.name}</span>)}
            {accounts.length > 5 && <span>+{accounts.length - 5}</span>}
          </div>
          <div className="assignment-strategy-list" role="radiogroup" aria-label="共享策略">
            {strategies.map((strategy) => (
              <label key={strategy.id} className={selectedId === strategy.id ? 'active' : ''}>
                <input type="radio" name="strategy" value={strategy.id} checked={selectedId === strategy.id} onChange={() => setSelectedId(strategy.id)} />
                <ChartNoAxesCombined size={15} />
                <div><strong>{strategy.name}</strong><span>{quote.format(Number(strategy.roundTurnoverQuoteMin))}-{quote.format(Number(strategy.roundTurnoverQuoteMax))} USDT / 轮</span></div>
                <small>{targetModeLabel(strategy.targetMode)} · 目标 {quote.format(Number(strategy.targetVolumeQuote))}</small>
              </label>
            ))}
          </div>
          <div className={`assignment-reset-note ${blocked.length ? 'blocked' : ''}`}>
            <ShieldAlert size={15} />
            <span>{blocked.length ? `${blocked.length} 个账号尚未停止或仍有双腿敞口` : selected?.targetMode === 'lifetime' ? '切换后直接按各账号历史累计成交量判断目标' : '切换后在账号首次启动时从 0 开始累计'}</span>
          </div>
          <footer className="dialog-actions">
            <button className="button secondary" type="button" onClick={onClose}>取消</button>
            <button className="button primary" type="submit" disabled={!selected || Boolean(blocked.length) || busy}>{busy ? '应用中...' : '确认应用'}</button>
          </footer>
        </form>
      </section>
    </div>
  )
}
