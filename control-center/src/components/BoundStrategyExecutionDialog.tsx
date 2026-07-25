import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Copy, LoaderCircle, Play, RefreshCw, ShieldCheck, Square, X } from 'lucide-react'
import type {
  AccountInstance, BetaCampaign, BetaCampaignPreview, StrategyDirection, StrategyRunPrepareResponse,
} from '../types'
import {
  ControlPlaneRequestError,
  cleanupBoundStrategyRun,
  confirmBoundStrategyRun,
  listBoundStrategyExecutions,
  prepareBoundStrategyRun,
  stopBoundStrategyExecution,
} from '../services/controlCenter'
import { BoundStrategyPreparation, type PreparationStage } from './BoundStrategyPreparation'
import { BoundStrategyOverview } from './BoundStrategyOverview'
import { BoundStrategyBoundary } from './BoundStrategyBoundary'

interface BoundStrategyExecutionDialogProps {
  account: AccountInstance
  queuePosition: number
  queueLength: number
  enabled: boolean
  onClose: () => void
  onChanged: (execution: BetaCampaign) => void
  onStarted: (execution: BetaCampaign) => void
  onToast: (message: string) => void
}

const quote = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const statusLabel: Record<BetaCampaign['status'], string> = {
  planned: '待精确确认',
  executing: '执行中',
  stopping: '安全停止中',
  completed: '已完成',
  stopped: '已安全停止',
  recovering: '后台核验中',
  uncertain: '待核验',
}

function copy(value: string, onToast: (message: string) => void) {
  void navigator.clipboard?.writeText(value).then(
    () => onToast('已复制精确确认短语'),
    () => onToast('浏览器未允许复制，请手动选择短语'),
  )
}

function launchErrorMessage(reason: unknown, fallback: string): string {
  const message = reason instanceof Error ? reason.message : fallback
  if (message.includes('complete lifetime trade history synchronization')) {
    return '成交历史基线尚未完成，系统正在后台继续只读核验。请稍后重新获取确认。'
  }
  if (message.startsWith('final beta source unavailable:')) {
    return 'Final Beta 来源当前不可用，无法安全生成启动确认。请在顶部 Final Beta 旁检查来源设置，待数据恢复后重新获取确认；本次不会提交订单。'
  }
  return message
}

export function BoundStrategyExecutionDialog({ account, queuePosition, queueLength, enabled, onClose, onChanged, onStarted, onToast }: BoundStrategyExecutionDialogProps) {
  const [execution, setExecution] = useState<BetaCampaign | null>(null)
  const [preparation, setPreparation] = useState<StrategyRunPrepareResponse | null>(null)
  const [preparationStage, setPreparationStage] = useState<PreparationStage>('checking')
  const [busy, setBusy] = useState(false)
  const [riskAcknowledged, setRiskAcknowledged] = useState(false)
  const [direction, setDirection] = useState<StrategyDirection>('btc_long_eth_short')
  const [confirmation, setConfirmation] = useState('')
  const [error, setError] = useState<string | null>(null)
  const accountRef = useRef(account)
  const onChangedRef = useRef(onChanged)
  const onStartedRef = useRef(onStarted)
  const onToastRef = useRef(onToast)
  const preparationRequestRef = useRef(0)
  const dialogKey = `${account.id}:${account.strategy.id}:${account.strategy.version}`

  useEffect(() => { accountRef.current = account }, [account])
  useEffect(() => { onChangedRef.current = onChanged }, [onChanged])
  useEffect(() => { onStartedRef.current = onStarted }, [onStarted])
  useEffect(() => { onToastRef.current = onToast }, [onToast])

  const update = useCallback((next: BetaCampaign) => {
    setExecution(next)
    setConfirmation('')
    onChangedRef.current(next)
  }, [])

  const applyPreparation = useCallback((next: StrategyRunPrepareResponse) => {
    setPreparation(next)
    const current = next.preview ?? next.current
    setExecution(current)
    if (current?.direction) setDirection(current.direction)
    setConfirmation('')
    setRiskAcknowledged(false)
    if (current) onChangedRef.current(current)
    if (next.disposition === 'unavailable') {
      setError(next.message ?? '当前启动条件不可用')
    }
  }, [])

  useEffect(() => {
    let cancelled = false
    const requestId = ++preparationRequestRef.current
    const targetAccount = accountRef.current
    const current = () => !cancelled && preparationRequestRef.current === requestId

    const load = async () => {
      setPreparationStage('preflight')
      setExecution(null)
      setRiskAcknowledged(false)
      setConfirmation('')
      setError(null)
      try {
        if (!enabled) return
        // The server owns active-task detection and historical recovery, so a
        // normal launch needs one read-only preparation request rather than a
        // slow client-side list-then-preview race.
        const prepared = await prepareBoundStrategyRun(targetAccount, direction)
        if (!current()) return
        applyPreparation(prepared)
      } catch (reason) {
        if (current()) setError(launchErrorMessage(reason, '无法读取启动确认'))
      } finally {
        if (current()) setPreparationStage(null)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [applyPreparation, dialogKey, direction, enabled])

  const preview = async () => {
    const requestId = ++preparationRequestRef.current
    const targetAccount = accountRef.current
    setPreparationStage('preflight')
    setExecution(null)
    setRiskAcknowledged(false)
    setConfirmation('')
    setError(null)
    try {
      const next = await prepareBoundStrategyRun(targetAccount, direction)
      if (preparationRequestRef.current === requestId) applyPreparation(next)
    } catch (reason) {
      if (preparationRequestRef.current === requestId) setError(launchErrorMessage(reason, '预览失败'))
    } finally {
      if (preparationRequestRef.current === requestId) setPreparationStage(null)
    }
  }

  const cleanup = async () => {
    if (preparation?.disposition !== 'orders_cleanup_required') return
    setBusy(true)
    setError(null)
    try {
      const next = await cleanupBoundStrategyRun(accountRef.current, confirmation, direction)
      applyPreparation(next)
      onToastRef.current(`${accountRef.current.name} 的普通挂单与条件单已撤销并核验`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '撤单结果无法确认；命令不会自动重试')
    } finally {
      setBusy(false)
    }
  }

  const execute = async () => {
    if (!execution) return
    const commandId = crypto.randomUUID()
    setBusy(true)
    setError(null)
    try {
      const confirmed = await confirmBoundStrategyRun(
        accountRef.current,
        execution.campaignId,
        confirmation,
        riskAcknowledged,
        commandId,
      )
      if (confirmed.admissionState === 'capacity_full') {
        update(confirmed.execution)
        setError(`执行容量已满（${confirmed.capacity.activeExecutions}/${confirmed.capacity.maxActiveExecutions}）；当前预览仍有效，稍后直接再次确认即可。`)
        return
      }
      const started = confirmed.execution
      update(started)
      onToastRef.current(`${accountRef.current.name} 的已绑定策略已提交执行；不会自动重试任何订单命令`)
      if (started.status === 'executing') onStartedRef.current(started)
    } catch (reason) {
      // A transport acknowledgement can fail after the executor has accepted
      // the exact command.  Never submit it again: inspect this immutable
      // execution with a read-only query and surface the resulting state.
      setError('正在只读核验启动命令结果…')
      try {
        const executions = await listBoundStrategyExecutions(accountRef.current)
        const resolved = executions.find((item) => item.campaignId === execution.campaignId)
        if (resolved && resolved.status !== 'planned') {
          update(resolved)
          if (resolved.status === 'executing') {
            onStartedRef.current(resolved)
            onToastRef.current(`${accountRef.current.name} 的启动命令已确认，策略正在执行`)
          } else if (resolved.status === 'stopped') {
            const prepared = await prepareBoundStrategyRun(accountRef.current, direction)
            applyPreparation(prepared)
            setError('启动条件已变化，请重新确认；首笔订单前已安全终止，本次没有自动重试。')
          } else if (resolved.status === 'uncertain') {
            setError('启动命令已被执行器接收，但结果待核验；不会自动重发该命令。下次启动准备会自动执行只读账户边界检查。')
          }
          return
        }
      } catch {
        // Keep the original transport error below.  A failed read must not
        // trigger an order/execute retry either.
      }
      if (reason instanceof ControlPlaneRequestError && reason.commandId) {
        setError(`${reason.message}（命令 ${reason.commandId} 未重发）`)
      } else {
        setError(reason instanceof Error ? reason.message : '启动失败')
      }
    } finally {
      setBusy(false)
    }
  }

  const stop = async () => {
    if (!execution) return
    setBusy(true)
    setError(null)
    try {
      update(await stopBoundStrategyExecution(accountRef.current, execution, confirmation))
      onToastRef.current(`${accountRef.current.name} 正在安全停止；撤单、仓位和成交将继续核验`)
    } catch (reason) {
      setError(reason instanceof Error ? reason.message : '安全停止请求失败')
    } finally {
      setBusy(false)
    }
  }

  const plan = execution as (BetaCampaignPreview | null)
  const canExecute = execution?.status === 'planned' && riskAcknowledged && confirmation === execution.confirmation && enabled
  const canStop = Boolean(
    execution
    && ['executing', 'recovering', 'uncertain'].includes(execution.status)
    && confirmation === execution.stopConfirmation
    && (execution.status === 'executing' || preparation?.disposition === 'recovery_cleanup_required'),
  )
  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="dialog beta-campaign-dialog bound-strategy-execution-dialog" role="dialog" aria-modal="true" aria-labelledby="bound-execution-title" onMouseDown={(event) => event.stopPropagation()}>
        <header className="dialog-header">
          <div>
            <h2 id="bound-execution-title">启动已绑定策略</h2>
            <span>实盘账号 · {account.name}{queueLength > 1 ? ` · 队列 ${queuePosition + 1}/${queueLength}` : ''}</span>
          </div>
          <button className="icon-button" type="button" onClick={onClose} data-tooltip="关闭" aria-label="关闭"><X size={16} /></button>
        </header>

        <div className="beta-campaign-body">
          <BoundStrategyOverview
            account={account}
            execution={execution}
            direction={direction}
            directionDisabled={busy || Boolean(preparationStage) || Boolean(execution && execution.status !== 'planned')}
            onDirectionChange={setDirection}
          />

          {!enabled && <div className="execution-warning"><AlertTriangle size={16} /><p>执行器不可用或实盘门禁未启用。不会把普通策略启动当作实盘交易。</p></div>}
          {error && <div className="execution-warning"><AlertTriangle size={16} /><p>{error}</p></div>}

          {preparationStage ? <BoundStrategyPreparation stage={preparationStage} /> : preparation && ['orders_cleanup_required', 'position_blocked'].includes(preparation.disposition) ? (
            <BoundStrategyBoundary
              preparation={preparation}
              confirmation={confirmation}
              busy={busy}
              onConfirmationChange={setConfirmation}
              onCancelOrders={() => void cleanup()}
              onRecheck={() => void preview()}
              onClose={onClose}
              onCopy={(value) => copy(value, onToast)}
            />
          ) : !execution ? (
            <div className="bound-strategy-empty-actions">
              <p>{error ? '启动确认未能生成。重新读取只会执行只读预检，不会提交订单。' : '当前没有可用的启动确认。重新读取时不会提交订单。'}</p>
              <button className="button primary" type="button" disabled={busy || !enabled} onClick={() => void preview()}><Play size={15} />重新获取确认</button>
            </div>
          ) : (
            <>
              <div className="bound-execution-stage">
                <div>
                  <span>实时预检</span>
                  <strong>{execution.status === 'planned' ? '启动条件已生成，等待最后确认' : statusLabel[execution.status]}</strong>
                </div>
                <div className={`execution-status ${execution.status}`}><ShieldCheck size={13} />{statusLabel[execution.status]}</div>
              </div>
              <div className="beta-campaign-summary">
                <div><span>Final Beta</span><strong>{Number(execution.beta).toFixed(6)}</strong></div>
                <div><span>可用余额</span><strong>{execution.availableQuote === null ? '--' : quote.format(Number(execution.availableQuote))}<small>{execution.availableQuote === null ? '' : 'USDT'}</small></strong></div>
                <div><span>目标口径</span><strong>{execution.targetMode === 'lifetime' ? '累计达到' : '每次新增'}</strong></div>
                <div><span>执行参数</span><strong>{execution.leverage}x · {execution.marginMode === 'cross' ? '全仓' : '逐仓'}</strong></div>
                <div><span>已核验成交</span><strong>{quote.format(Number(account.volume.activeSession?.verifiedQuoteVolume ?? 0))}<small>USDT</small></strong></div>
                <div><span>数据状态</span><strong>{account.volume.activeSession?.stale || account.volume.activeSession?.reconciliationRequired ? '待核验' : '已核验'}</strong></div>
              </div>
              {execution.status === 'planned' && <section className="bound-start-confirmation">
                <div className="bound-confirmation-heading">
                  <div>
                    <span>最后确认</span>
                    <strong>粘贴确认短语后即可提交启动</strong>
                  </div>
                </div>
                <label className="bound-risk-check">
                  <input type="checkbox" checked={riskAcknowledged} onChange={(event) => setRiskAcknowledged(event.target.checked)} />
                  <span>我理解这会在实盘提交 {direction === 'btc_long_eth_short' ? 'BTC 多、ETH 空' : 'BTC 空、ETH 多'}的 POST_ONLY 订单。<small>固定 400x 全仓；正常订单保持 Maker。仅当前任务产生且不超过 {execution.dustClosePolicy.maxQuote} USDT 的小额尾仓，可能按仓位 ID 市价收尾一次。</small></span>
                </label>
                <div className="bound-phrase-panel">
                  <div className="bound-phrase-heading">
                    <div><span>精确确认短语</span><small>复制后完整粘贴</small></div>
                    <button className="icon-button compact" type="button" onClick={() => copy(execution.confirmation, onToast)} data-tooltip="复制确认短语" aria-label="复制确认短语"><Copy size={13} /></button>
                  </div>
                  <code className="confirmation-phrase">{execution.confirmation}</code>
                  <input className="confirm-input" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder="粘贴完整确认短语" autoComplete="off" />
                </div>
              </section>}
              {execution.status === 'executing' && <>
                <label className="confirm-label">安全停止确认<button className="icon-button compact" type="button" onClick={() => copy(execution.stopConfirmation, onToast)} data-tooltip="复制停止短语" aria-label="复制停止短语"><Copy size={13} /></button></label>
                <code className="confirmation-phrase">{execution.stopConfirmation}</code>
                <input className="confirm-input" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder="粘贴完整停止短语" autoComplete="off" />
              </>}
              {(execution.status === 'recovering' || execution.status === 'uncertain') && <div className="execution-warning"><AlertTriangle size={16} /><p>{preparation?.disposition === 'recovery_cleanup_required' ? '已确认当前仓位属于本次任务。粘贴原停止确认短语后，可撤单并执行 Maker 优先的安全收尾。' : '系统正在按退避计划只读核验订单、仓位和成交；确认空仓后会自动结束旧任务。'}</p></div>}
              {preparation?.disposition === 'recovery_cleanup_required' && <>
                <label className="confirm-label">安全收尾确认<button className="icon-button compact" type="button" onClick={() => copy(execution.stopConfirmation, onToast)} data-tooltip="复制停止短语" aria-label="复制停止短语"><Copy size={13} /></button></label>
                <code className="confirmation-phrase">{execution.stopConfirmation}</code>
                <input className="confirm-input" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder="粘贴完整停止短语" autoComplete="off" />
              </>}
              {(plan?.warnings?.length ?? 0) > 0 && <p className="reason-code">{plan?.warnings.join(' · ')}</p>}
              <footer className="dialog-actions">
                <button className="button secondary" type="button" onClick={onClose}>关闭</button>
                {execution.status === 'planned' && <button className="button primary" type="button" disabled={busy || !canExecute} onClick={() => void execute()}>{busy ? <LoaderCircle className="spin" size={15} /> : <Play size={15} />}确认并启动</button>}
                {execution.status === 'executing' && <button className="button danger" type="button" disabled={busy || !canStop} onClick={() => void stop()}>{busy ? <LoaderCircle className="spin" size={15} /> : <Square size={14} />}安全停止</button>}
                {preparation?.disposition === 'recovery_cleanup_required' && <button className="button danger" type="button" disabled={busy || !canStop} onClick={() => void stop()}>{busy ? <LoaderCircle className="spin" size={15} /> : <Square size={14} />}安全收尾</button>}
                {(execution.status === 'recovering' || execution.status === 'uncertain') && <button className="button secondary" type="button" disabled={busy || !enabled} onClick={() => void preview()}>{busy ? <LoaderCircle className="spin" size={15} /> : <RefreshCw size={15} />}刷新核验状态</button>}
              </footer>
            </>
          )}
        </div>
      </section>
    </div>
  )
}
