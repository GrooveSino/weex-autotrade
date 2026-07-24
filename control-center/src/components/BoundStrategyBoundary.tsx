import { AlertTriangle, Copy, ListX, LoaderCircle, RefreshCw } from 'lucide-react'
import type { StrategyRunPrepareResponse } from '../types'

interface BoundStrategyBoundaryProps {
  preparation: StrategyRunPrepareResponse
  confirmation: string
  busy: boolean
  onConfirmationChange: (value: string) => void
  onCancelOrders: () => void
  onRecheck: () => void
  onClose: () => void
  onCopy: (value: string) => void
}

const quote = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const sideLabel = { long: '多仓', short: '空仓', unknown: '方向未知' }

export function BoundStrategyBoundary({
  preparation,
  confirmation,
  busy,
  onConfirmationChange,
  onCancelOrders,
  onRecheck,
  onClose,
  onCopy,
}: BoundStrategyBoundaryProps) {
  const hasOrders = preparation.disposition === 'orders_cleanup_required'
  const canCancel = Boolean(preparation.cleanupConfirmation)
    && confirmation === preparation.cleanupConfirmation
  return (
    <section className="bound-start-confirmation boundary-blocker">
      <div className="execution-warning">
        <AlertTriangle size={16} />
        <p>{hasOrders
          ? `启动前检测到 ${preparation.regularOrderCount} 个普通挂单和 ${preparation.triggerOrderCount} 个条件单。系统只会撤单，不会平掉已有仓位。`
          : '账户已有 BTC/ETH 仓位。为避免误平手工仓位，系统不会自动处理；请在交易所关闭后重新检查。'}</p>
      </div>
      {preparation.blockingPositions.length > 0 && (
        <div className="boundary-positions" aria-label="阻止启动的已有仓位">
          {preparation.blockingPositions.map((position, index) => (
            <div key={`${position.symbol}-${position.side}-${index}`}>
              <span>{position.symbol} · {sideLabel[position.side]}</span>
              <strong>{position.quantity}</strong>
              <small>约 {quote.format(Number(position.approximateQuote))} USDT</small>
            </div>
          ))}
        </div>
      )}
      {hasOrders && (
        <div className="bound-phrase-panel">
          <div className="bound-phrase-heading">
            <div><span>启动前撤单确认短语</span><small>仅撤普通单与条件单，不会提交平仓订单</small></div>
            <button className="icon-button compact" type="button" onClick={() => onCopy(preparation.cleanupConfirmation ?? '')} data-tooltip="复制撤单短语" aria-label="复制撤单短语"><Copy size={13} /></button>
          </div>
          <code className="confirmation-phrase">{preparation.cleanupConfirmation}</code>
          <input className="confirm-input" value={confirmation} onChange={(event) => onConfirmationChange(event.target.value)} placeholder="粘贴完整撤单短语" autoComplete="off" />
        </div>
      )}
      <footer className="dialog-actions">
        <button className="button secondary" type="button" onClick={onClose}>关闭</button>
        <button className="button secondary" type="button" disabled={busy} onClick={onRecheck}>{busy ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}重新检查</button>
        {hasOrders && <button className="button danger" type="button" disabled={busy || !canCancel} onClick={onCancelOrders}>{busy ? <LoaderCircle className="spin" size={15} /> : <ListX size={15} />}确认撤销挂单</button>}
      </footer>
    </section>
  )
}
