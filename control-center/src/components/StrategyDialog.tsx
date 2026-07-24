import { useMemo, useState, type FormEvent } from 'react'
import { ChartNoAxesCombined, Clock3, History, Plus, ShieldCheck, TimerReset, Trash2, X } from 'lucide-react'
import type { AccountInstance, StrategyDraft, VolumeStrategy } from '../types'
import { draftStrategy, durationParts, estimateRounds, secondsFromParts, targetTolerance } from '../utils/strategy'

interface StrategyDialogProps {
  strategies: VolumeStrategy[]
  accounts: AccountInstance[]
  initialStrategyId?: string | null
  onClose: () => void
  onCreate: (draft: StrategyDraft) => Promise<VolumeStrategy | null>
  onUpdate: (strategy: VolumeStrategy, draft: StrategyDraft) => Promise<VolumeStrategy | null>
  onDelete: (strategy: VolumeStrategy) => Promise<boolean>
}

const initialDraft: StrategyDraft = {
  name: '新成交量策略',
  targetMode: 'incremental',
  targetVolumeQuoteMin: '10000',
  targetVolumeQuoteMax: '15000',
  roundTurnoverQuoteMin: '500',
  roundTurnoverQuoteMax: '750',
  positionHoldMinSeconds: 300,
  positionHoldMaxSeconds: 900,
  roundIntervalMinSeconds: 600,
  roundIntervalMaxSeconds: 1800,
}

const quote = new Intl.NumberFormat('en-US', { maximumFractionDigits: 2 })

function draftFor(strategy: VolumeStrategy | null): StrategyDraft {
  if (!strategy) return initialDraft
  return {
    name: strategy.name,
    targetMode: strategy.targetMode,
    targetVolumeQuoteMin: strategy.targetVolumeQuoteMin ?? strategy.targetVolumeQuote,
    targetVolumeQuoteMax: strategy.targetVolumeQuoteMax ?? strategy.targetVolumeQuote,
    roundTurnoverQuoteMin: strategy.roundTurnoverQuoteMin,
    roundTurnoverQuoteMax: strategy.roundTurnoverQuoteMax,
    positionHoldMinSeconds: strategy.positionHoldMinSeconds,
    positionHoldMaxSeconds: strategy.positionHoldMaxSeconds,
    roundIntervalMinSeconds: strategy.roundIntervalMinSeconds,
    roundIntervalMaxSeconds: strategy.roundIntervalMaxSeconds,
  }
}

function DurationPoint({ value, label, disabled, onChange }: {
  value: number
  label: string
  disabled: boolean
  onChange: (value: number) => void
}) {
  const parts = durationParts(value)
  const update = (key: keyof typeof parts, next: number) => onChange(secondsFromParts(
    key === 'hours' ? next : parts.hours,
    key === 'minutes' ? next : parts.minutes,
    key === 'seconds' ? next : parts.seconds,
  ))
  return (
    <div className="duration-point">
      <span>{label}</span>
      <div className="duration-inputs">
        <label><input disabled={disabled} type="number" min="0" max="720" value={parts.hours} onChange={(event) => update('hours', event.target.valueAsNumber)} /><span>时</span></label>
        <label><input disabled={disabled} type="number" min="0" max="59" value={parts.minutes} onChange={(event) => update('minutes', event.target.valueAsNumber)} /><span>分</span></label>
        <label><input disabled={disabled} type="number" min="0" max="59" value={parts.seconds} onChange={(event) => update('seconds', event.target.valueAsNumber)} /><span>秒</span></label>
      </div>
    </div>
  )
}

function DurationRange({ title, subtitle, minimum, maximum, disabled, onMinimumChange, onMaximumChange }: {
  title: string
  subtitle: string
  minimum: number
  maximum: number
  disabled: boolean
  onMinimumChange: (value: number) => void
  onMaximumChange: (value: number) => void
}) {
  return (
    <div className="duration-range">
      <div className="duration-range-heading"><Clock3 size={14} /><div><strong>{title}</strong><span>{subtitle}</span></div></div>
      <div className="duration-range-grid">
        <DurationPoint value={minimum} label="最短" disabled={disabled} onChange={onMinimumChange} />
        <DurationPoint value={maximum} label="最长" disabled={disabled} onChange={onMaximumChange} />
      </div>
    </div>
  )
}

export function StrategyDialog({ strategies, accounts, initialStrategyId, onClose, onCreate, onUpdate, onDelete }: StrategyDialogProps) {
  const initial = strategies.find((strategy) => strategy.id === initialStrategyId) ?? strategies[0] ?? null
  const [selectedId, setSelectedId] = useState<string | null>(initial?.id ?? null)
  const [creating, setCreating] = useState(!initial)
  const selected = strategies.find((strategy) => strategy.id === selectedId) ?? null
  const [draft, setDraft] = useState<StrategyDraft>(() => draftFor(initial))
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [confirmDelete, setConfirmDelete] = useState(false)
  const assigned = useMemo(
    () => selected ? accounts.filter((account) => account.strategyId === selected.id) : [],
    [accounts, selected],
  )
  const blocked = assigned.some((account) => (
    account.status !== 'stopped'
    || account.strategyProgress.stage === 'holding'
    || account.exposure.btcLong !== 0
    || account.exposure.ethShort !== 0
  ))
  const estimate = useMemo(() => estimateRounds(draftStrategy(draft)), [draft])

  const choose = (strategy: VolumeStrategy) => {
    setCreating(false)
    setSelectedId(strategy.id)
    setDraft(draftFor(strategy))
    setError(null)
    setConfirmDelete(false)
  }
  const startCreate = () => {
    setCreating(true)
    setSelectedId(null)
    setDraft(initialDraft)
    setError(null)
    setConfirmDelete(false)
  }
  const update = <K extends keyof StrategyDraft>(key: K, value: StrategyDraft[K]) => {
    setDraft((current) => ({ ...current, [key]: value }))
  }

  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (busy || (!creating && (!selected || blocked))) return
    setError(null)
    if (!draft.name.trim() || !estimate) {
      setError('请填写有效的策略名称、目标交易量和每轮总交易量范围')
      return
    }
    if (Number(draft.targetVolumeQuoteMin) > Number(draft.targetVolumeQuoteMax)) {
      setError('任务目标最小值不能大于最大值')
      return
    }
    if (draft.positionHoldMinSeconds > draft.positionHoldMaxSeconds) {
      setError('持仓最短时间不能大于最长时间')
      return
    }
    if (draft.roundIntervalMinSeconds > draft.roundIntervalMaxSeconds) {
      setError('轮次最短间隔不能大于最长间隔')
      return
    }
    setBusy(true)
    const saved = creating ? await onCreate(draft) : await onUpdate(selected as VolumeStrategy, draft)
    setBusy(false)
    if (!saved) return
    setCreating(false)
    setSelectedId(saved.id)
  }

  const remove = async () => {
    if (!selected || assigned.length || busy) return
    if (!confirmDelete) {
      setConfirmDelete(true)
      return
    }
    setBusy(true)
    const deleted = await onDelete(selected)
    setBusy(false)
    if (!deleted) return
    const fallback = strategies.find((strategy) => strategy.id !== selected.id) ?? null
    setSelectedId(fallback?.id ?? null)
    setCreating(!fallback)
    setDraft(draftFor(fallback))
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="dialog strategy-library-dialog" role="dialog" aria-modal="true" aria-labelledby="strategy-dialog-title" onMouseDown={(event) => event.stopPropagation()}>
        <header className="dialog-header">
          <div><h2 id="strategy-dialog-title">策略库</h2><span>{strategies.length} 套共享策略</span></div>
          <button className="icon-button" type="button" onClick={onClose} data-tooltip="关闭" aria-label="关闭"><X size={16} /></button>
        </header>
        <div className="strategy-library-layout">
          <aside className="strategy-list">
            <button className="button secondary strategy-create-button" type="button" onClick={startCreate}><Plus size={14} />新建策略</button>
            <div className="strategy-list-items">
              {strategies.map((strategy) => {
                const usage = accounts.filter((account) => account.strategyId === strategy.id).length
                return (
                  <button key={strategy.id} type="button" className={!creating && selectedId === strategy.id ? 'active' : ''} onClick={() => choose(strategy)}>
                    <strong>{strategy.name}</strong>
                    <span>{strategy.targetMode === 'lifetime' ? '历史累计' : '启动后新增'} · 目标 {quote.format(Number(strategy.targetVolumeQuoteMin))}-{quote.format(Number(strategy.targetVolumeQuoteMax))}</span>
                    <small>{usage} 个账号</small>
                  </button>
                )
              })}
            </div>
          </aside>

          <form className="strategy-editor" onSubmit={submit}>
            <div className="strategy-target-row">
              <div className="strategy-target-mark"><ChartNoAxesCombined size={18} /></div>
              <label><span>策略名称</span><input required disabled={blocked || busy} value={draft.name} onChange={(event) => update('name', event.target.value)} autoFocus /></label>
            </div>

            <div className="target-mode-control" role="radiogroup" aria-label="目标交易量统计口径">
              <button type="button" role="radio" aria-checked={draft.targetMode === 'incremental'} className={draft.targetMode === 'incremental' ? 'active' : ''} disabled={blocked || busy} onClick={() => update('targetMode', 'incremental')}>
                <TimerReset size={14} /><span><strong>启动后新增</strong><small>首次启动时从 0 计量</small></span>
              </button>
              <button type="button" role="radio" aria-checked={draft.targetMode === 'lifetime'} className={draft.targetMode === 'lifetime' ? 'active' : ''} disabled={blocked || busy} onClick={() => update('targetMode', 'lifetime')}>
                <History size={14} /><span><strong>历史累计目标</strong><small>以账号累计成交量为进度</small></span>
              </button>
            </div>

            <div className="dialog-section-title"><ChartNoAxesCombined size={14} />每次任务目标交易量</div>
            <div className="amount-range-row">
              <label><span>最小</span><div className="input-suffix"><input required disabled={blocked || busy} type="number" min="0.01" max="1000000000000" step="0.01" value={draft.targetVolumeQuoteMin} onChange={(event) => update('targetVolumeQuoteMin', event.target.value)} /><span>USDT</span></div></label>
              <span className="range-separator">至</span>
              <label><span>最大</span><div className="input-suffix"><input required disabled={blocked || busy} type="number" min="0.01" max="1000000000000" step="0.01" value={draft.targetVolumeQuoteMax} onChange={(event) => update('targetVolumeQuoteMax', event.target.value)} /><span>USDT</span></div></label>
            </div>

            <div className="strategy-progress-summary">
              <span><strong>{assigned.length}</strong> 已绑定账号</span>
              <span><strong>{estimate ? `${estimate.minimum}-${estimate.maximum}` : '-'}</strong> 预计轮数</span>
              <span><strong>{draft.targetMode === 'lifetime' ? '累计' : '新增'}</strong> 目标口径</span>
              <span><strong>Final Beta</strong> 双腿分配</span>
            </div>

            <div className="dialog-section-title"><ChartNoAxesCombined size={14} />每轮总交易量</div>
            <div className="amount-range-row">
              <label><span>最小</span><div className="input-suffix"><input required disabled={blocked || busy} type="number" min="0.01" max="1000000000" step="0.01" value={draft.roundTurnoverQuoteMin} onChange={(event) => update('roundTurnoverQuoteMin', event.target.value)} /><span>USDT</span></div></label>
              <span className="range-separator">至</span>
              <label><span>最大</span><div className="input-suffix"><input required disabled={blocked || busy} type="number" min="0.01" max="1000000000" step="0.01" value={draft.roundTurnoverQuoteMax} onChange={(event) => update('roundTurnoverQuoteMax', event.target.value)} /><span>USDT</span></div></label>
            </div>
            <div className="turnover-preview">
              <span>单轮统计口径</span>
              <strong>BTC 开 + ETH 开 + BTC 平 + ETH 平</strong>
              <small>开仓名义金额合计为本轮总量的 50%</small>
            </div>

            <div className="strategy-duration-stack">
              <DurationRange title="持仓时间" subtitle="双腿开仓完成 → 开始平仓" minimum={draft.positionHoldMinSeconds} maximum={draft.positionHoldMaxSeconds} disabled={blocked || busy} onMinimumChange={(value) => update('positionHoldMinSeconds', value)} onMaximumChange={(value) => update('positionHoldMaxSeconds', value)} />
              <DurationRange title="轮次间隔" subtitle="双腿平仓完成 → 开始下一轮" minimum={draft.roundIntervalMinSeconds} maximum={draft.roundIntervalMaxSeconds} disabled={blocked || busy} onMinimumChange={(value) => update('roundIntervalMinSeconds', value)} onMaximumChange={(value) => update('roundIntervalMaxSeconds', value)} />
            </div>

            <div className="residual-policy"><ShieldCheck size={15} /><span>任务目标</span><strong>启动预览时在范围内抽取并固定 · 容差 {targetTolerance(Number(draft.targetVolumeQuoteMax)).toFixed(2)} USDT</strong></div>
            {blocked && <div className="edit-lock-note">已绑定账号中存在运行实例或未平双腿，当前策略已锁定。</div>}
            {confirmDelete && <div className="form-error">再次点击删除确认。</div>}
            {error && <div className="form-error">{error}</div>}

            <footer className="dialog-actions">
              {!creating && selected && <button className="button danger dialog-delete-button" type="button" disabled={Boolean(assigned.length) || busy} onClick={() => void remove()}><Trash2 size={14} />{confirmDelete ? '确认删除' : assigned.length ? '使用中' : '删除策略'}</button>}
              <button className="button secondary" type="button" onClick={onClose}>关闭</button>
              <button className="button primary" type="submit" disabled={blocked || busy}>{busy ? '处理中...' : creating ? '创建策略' : '保存策略'}</button>
            </footer>
          </form>
        </div>
      </section>
    </div>
  )
}
