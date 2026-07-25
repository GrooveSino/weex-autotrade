import { useEffect, useState } from 'react'
import { AlertTriangle, CircleX, X } from 'lucide-react'
import type { AccountInstance } from '../../types'

interface ClosePositionsDialogProps {
  account: AccountInstance
  onClose: () => void
  onConfirm: (account: AccountInstance) => Promise<void>
}

const currency = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export function ClosePositionsDialog({ account, onClose, onConfirm }: ClosePositionsDialogProps) {
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const btcOpen = account.exposure.btcLong > 0
  const ethOpen = account.exposure.ethShort > 0
  const singleLeg = btcOpen !== ethOpen
  const totalExposure = Math.max(0, account.exposure.btcLong) + Math.max(0, account.exposure.ethShort)

  useEffect(() => {
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape' && !busy) onClose()
    }
    document.addEventListener('keydown', closeOnEscape)
    return () => document.removeEventListener('keydown', closeOnEscape)
  }, [busy, onClose])

  const confirm = async () => {
    if (busy) return
    setBusy(true)
    setError(null)
    try {
      await onConfirm(account)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '平仓请求失败')
      setBusy(false)
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={() => !busy && onClose()}>
      <section
        className="dialog close-positions-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-labelledby="close-positions-title"
        onMouseDown={(event) => event.stopPropagation()}
      >
        <header className="dialog-header">
          <div><h2 id="close-positions-title">一键平仓</h2><span>{account.name} · 策略保持非运行</span></div>
          <button className="icon-button" type="button" onClick={onClose} disabled={busy} data-tooltip="关闭" aria-label="关闭">
            <X size={16} />
          </button>
        </header>
        <div className="close-positions-body">
          <div className="close-position-warning">
            <AlertTriangle size={17} />
            <p>系统会先撤销并核验活动挂单，再平掉当前仓位。任一步结果无法确认时都会停止，不会自动重试。</p>
          </div>
          <div className="close-position-summary" aria-label="当前待平仓敞口">
            <div><span><i className="btc" />BTC 多单</span><strong>${currency.format(account.exposure.btcLong)}</strong></div>
            <div><span><i className="eth" />ETH 空单</span><strong>${currency.format(account.exposure.ethShort)}</strong></div>
            <div className="close-position-total"><span>合计当前敞口</span><strong>${currency.format(totalExposure)}</strong></div>
          </div>
          {singleLeg && (
            <div className="single-leg-warning">
              <AlertTriangle size={14} />当前仅检测到 {btcOpen ? 'BTC 多单' : 'ETH 空单'}，将只平掉仍存在的仓位。
            </div>
          )}
          {error && <div className="form-error" role="alert">{error}</div>}
        </div>
        <footer className="dialog-actions close-position-actions">
          <button className="button secondary" type="button" onClick={onClose} disabled={busy}>取消</button>
          <button className="button danger" type="button" onClick={() => void confirm()} disabled={busy || totalExposure <= 0} autoFocus>
            <CircleX size={15} />{busy ? '正在核验并平仓' : '确认一键平仓'}
          </button>
        </footer>
      </section>
    </div>
  )
}
