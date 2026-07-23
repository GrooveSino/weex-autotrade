import { useMemo, useState, type FormEvent } from 'react'
import { ChartNoAxesCombined, Eye, EyeOff, KeyRound, Network, ShieldCheck, Trash2, X } from 'lucide-react'
import type { AccountDraft, AccountInstance, VolumeStrategy } from '../types'
import { targetModeLabel } from '../utils/strategy'

interface AccountDialogProps {
  account?: AccountInstance | null
  strategies: VolumeStrategy[]
  onClose: () => void
  onSubmit: (draft: AccountDraft) => Promise<boolean>
  onDelete?: () => Promise<boolean>
}

const initialDraft: AccountDraft = {
  name: '',
  accountTag: '',
  mode: 'live',
  apiKey: '',
  apiSecret: '',
  passphrase: '',
  proxyType: 'http',
  proxyUrl: '',
  strategyId: '',
  historyStartAt: '',
}

function localDateTimeInput(timestamp: number | null | undefined): string {
  if (!timestamp) return ''
  const date = new Date(timestamp)
  if (Number.isNaN(date.getTime())) return ''
  const pad = (value: number) => String(value).padStart(2, '0')
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())}T${pad(date.getHours())}:${pad(date.getMinutes())}`
}

function draftFor(account: AccountInstance | null | undefined, strategies: VolumeStrategy[]): AccountDraft {
  return {
    ...initialDraft,
    name: account?.name ?? '',
    accountTag: account?.accountTag ?? '',
    mode: account?.mode ?? 'live',
    proxyType: account?.proxy.type ?? 'http',
    strategyId: account?.strategyId ?? strategies[0]?.id ?? '',
    historyStartAt: localDateTimeInput(account?.historyStartAtMs),
  }
}

const quote = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })

export function AccountDialog({ account, strategies, onClose, onSubmit, onDelete }: AccountDialogProps) {
  const editing = Boolean(account)
  const recoveryMode = account?.status === 'error'
  const editable = !account || (
    (account.status === 'stopped' || recoveryMode)
    && account.strategyProgress.stage !== 'holding'
    && account.exposure.btcLong === 0
    && account.exposure.ethShort === 0
  )
  const [draft, setDraft] = useState(() => draftFor(account, strategies))
  const [historyMax] = useState(() => localDateTimeInput(Date.now()))
  const [showSecrets, setShowSecrets] = useState(false)
  const [formError, setFormError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const credentialUpdateStarted = editing && [draft.apiKey, draft.apiSecret, draft.passphrase].some(Boolean)
  const selectedStrategy = useMemo(
    () => strategies.find((strategy) => strategy.id === draft.strategyId),
    [draft.strategyId, strategies],
  )

  const update = <K extends keyof AccountDraft>(key: K, value: AccountDraft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!editable || busy) return
    setFormError(null)
    const credentialFields = [draft.apiKey, draft.apiSecret, draft.passphrase]
    if (!editing && (!draft.name.trim() || !credentialFields.every(Boolean) || (draft.proxyType !== 'none' && !draft.proxyUrl.trim()))) {
      setFormError('请完整填写实例名称和 API 凭据；配置代理时还需要填写代理地址')
      return
    }
    if (editing && credentialFields.some(Boolean) && !credentialFields.every(Boolean)) {
      setFormError('更换 API 凭据时必须同时填写 Key、Secret 和 Passphrase')
      return
    }
    if (!selectedStrategy) {
      setFormError('请选择一个有效的共享策略')
      return
    }
    setBusy(true)
    const saved = await onSubmit(draft)
    setBusy(false)
    if (saved) onClose()
  }

  const remove = async () => {
    if (!editable || busy || !onDelete) return
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    setBusy(true)
    const deleted = await onDelete()
    setBusy(false)
    if (deleted) onClose()
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="dialog account-dialog account-only-dialog" role="dialog" aria-modal="true" aria-labelledby="account-dialog-title" onMouseDown={(event) => event.stopPropagation()}>
        <header className="dialog-header">
          <div>
            <h2 id="account-dialog-title">{editing ? account?.name : '添加账号实例'}</h2>
            <span>{account?.id ?? '凭据、网络与策略绑定'}</span>
          </div>
          <button className="icon-button" type="button" onClick={onClose} data-tooltip="关闭" aria-label="关闭"><X size={16} /></button>
        </header>

        <form onSubmit={submit}>
          <div className="dialog-tab-panel account-form-panel">
            <div className="dialog-section-title"><KeyRound size={14} />账号</div>
            <div className="form-grid two-columns">
              <label><span>实例名称</span><input required disabled={!editable} value={draft.name} onChange={(event) => update('name', event.target.value)} placeholder="例如 Alpha 09" autoFocus /></label>
              <label><span>分组标签</span><input disabled={!editable} value={draft.accountTag} onChange={(event) => update('accountTag', event.target.value)} placeholder="例如 主策略-A" /></label>
            </div>

            <div className="field-label">交易环境</div>
            <div className="segmented" role="radiogroup" aria-label="交易环境">
              <button type="button" disabled={editing || !editable} className={draft.mode === 'demo' ? 'active' : ''} onClick={() => update('mode', 'demo')}>Demo</button>
              <button type="button" disabled={editing || !editable} className={draft.mode === 'live' ? 'active danger-choice' : ''} onClick={() => update('mode', 'live')}>Live</button>
            </div>

            {editing && (
              <div className={`credential-preservation-note ${credentialUpdateStarted ? 'updating' : ''}`}>
                <ShieldCheck size={16} />
                <div>
                  <strong>{credentialUpdateStarted ? '正在更换 API 凭据' : '现有 API 凭据已安全保留'}</strong>
                  <span>{credentialUpdateStarted ? '请完整填写 Key、Secret 和 Passphrase 后保存。' : '凭据不会回显到网页；三个字段都留空即可保持当前值。'}</span>
                </div>
              </div>
            )}

            <div className="form-grid">
              <label><span>API Key{editing ? '（留空保持当前值）' : ''}</span><input required={!editing} disabled={!editable} value={draft.apiKey} onChange={(event) => update('apiKey', event.target.value)} placeholder={editing ? '已安全保存；留空保持当前值' : ''} autoComplete="off" spellCheck="false" /></label>
              <label>
                <span>API Secret{editing ? '（留空保持当前值）' : ''}</span>
                <div className="input-with-action">
                  <input required={!editing} disabled={!editable} type={showSecrets ? 'text' : 'password'} value={draft.apiSecret} onChange={(event) => update('apiSecret', event.target.value)} placeholder={editing ? '已安全保存；留空保持当前值' : ''} autoComplete="new-password" />
                  <button type="button" disabled={!editable || !draft.apiSecret} onClick={() => setShowSecrets((value) => !value)} data-tooltip={draft.apiSecret ? (showSecrets ? '隐藏本次输入' : '显示本次输入') : '已保存凭据不会回显'} aria-label={draft.apiSecret ? (showSecrets ? '隐藏本次输入' : '显示本次输入') : '已保存凭据不会回显'}>{showSecrets ? <EyeOff size={15} /> : <Eye size={15} />}</button>
                </div>
              </label>
              <label><span>Passphrase{editing ? '（留空保持当前值）' : ''}</span><input required={!editing} disabled={!editable} type={showSecrets ? 'text' : 'password'} value={draft.passphrase} onChange={(event) => update('passphrase', event.target.value)} placeholder={editing ? '已安全保存；留空保持当前值' : ''} autoComplete="new-password" /></label>
            </div>

            <div className="form-grid two-columns">
              <label><span>历史起点（可选）</span><input disabled={!editable || recoveryMode} type="datetime-local" value={draft.historyStartAt} max={historyMax} onChange={(event) => update('historyStartAt', event.target.value)} /></label>
              <label><span>绑定策略</span><select required disabled={!editable || recoveryMode || !strategies.length} value={draft.strategyId} onChange={(event) => update('strategyId', event.target.value)}>{strategies.map((strategy) => <option key={strategy.id} value={strategy.id}>{strategy.name}</option>)}</select></label>
            </div>

            {selectedStrategy && (
              <div className="bound-strategy-summary">
                <ChartNoAxesCombined size={16} />
                <div><strong>{selectedStrategy.name}</strong><span>{quote.format(Number(selectedStrategy.roundTurnoverQuoteMin))}-{quote.format(Number(selectedStrategy.roundTurnoverQuoteMax))} USDT / 轮</span></div>
                <div><span>{targetModeLabel(selectedStrategy.targetMode)}</span><strong>{quote.format(Number(selectedStrategy.targetVolumeQuote))} USDT</strong></div>
              </div>
            )}

            <div className="dialog-section-title"><Network size={14} />代理配置</div>
            <div className="proxy-input-row">
              <select
                disabled={!editable}
                value={draft.proxyType}
                onChange={(event) => {
                  const proxyType = event.target.value as AccountDraft['proxyType']
                  setDraft((current) => ({ ...current, proxyType, proxyUrl: proxyType === 'none' ? '' : current.proxyUrl }))
                }}
                aria-label="代理类型"
              >
                <option value="none">无代理</option>
                <option value="http">HTTP</option>
                <option value="https">HTTPS</option>
                <option value="socks5">SOCKS5</option>
              </select>
              <input required={!editing && draft.proxyType !== 'none'} disabled={!editable || draft.proxyType === 'none'} value={draft.proxyUrl} onChange={(event) => update('proxyUrl', event.target.value)} placeholder={draft.proxyType === 'none' ? '已选择不使用代理' : editing ? `当前 ${account?.proxy.host}，留空不更换` : 'IP:端口:用户名:密码 或 username:password@IP:端口'} autoComplete="off" spellCheck="false" />
            </div>
          </div>

          {recoveryMode && <div className="edit-lock-note">连接异常时仅允许修正 API 凭据或代理配置；保存后请使用行内刷新重新验证。</div>}
          {!editable && <div className="edit-lock-note">请先停止实例并完成双腿平仓。</div>}
          {confirmDelete && <div className="form-error">删除会清除该实例的本地凭据、日志和成交量记录。再次点击删除确认。</div>}
          {formError && <div className="form-error">{formError}</div>}

          <footer className="dialog-actions">
            {editing && onDelete && <button className="button danger dialog-delete-button" type="button" onClick={() => void remove()} disabled={!editable || busy}><Trash2 size={14} />{confirmDelete ? '确认删除' : '删除实例'}</button>}
            <button className="button secondary" type="button" onClick={onClose}>取消</button>
            <button className="button primary" type="submit" disabled={!editable || busy || !strategies.length}>{busy ? '处理中...' : editing ? '保存变更' : '添加实例'}</button>
          </footer>
        </form>
      </section>
    </div>
  )
}
