import { useCallback, useEffect, useRef, useState } from 'react'
import { AlertTriangle, Copy, LoaderCircle, Play, RefreshCw, ShieldCheck, Square, X } from 'lucide-react'
import type { AccountInstance, BetaCampaign, BetaCampaignPreview, StrategyRunPrepareResponse } from '../../types'
import {
  cleanupBoundStrategyRun,
  confirmBoundStrategyRun,
  listBoundStrategyExecutions,
  prepareBoundStrategyRun,
  stopBoundStrategyExecution,
} from '../../services'
import { BoundStrategyPreparation, type PreparationStage } from './BoundStrategyPreparation'
import { BoundStrategyOverview } from './BoundStrategyOverview'
import { BoundStrategyBoundary } from './BoundStrategyBoundary'
import { BoundStrategyPanelIssueView } from './BoundStrategyPanelIssue'
import { useBoundStrategyRecovery } from '../../hooks/useBoundStrategyRecovery'
import {
  strategyPanelError,
  strategyPanelNotice,
  type StrategyPanelIssue,
} from './boundStrategyPanelError'

interface BoundStrategyExecutionDialogProps {
  account: AccountInstance
  queuePosition: number
  queueLength: number
  enabled: boolean
  onClose: () => void
  onChanged: (execution: BetaCampaign) => void
  onStarted: (execution: BetaCampaign) => void
  onToast: (message: string) => void
  onEditAccount: () => void
  onOpenBetaSource: () => void
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

export function BoundStrategyExecutionDialog({ account, queuePosition, queueLength, enabled, onClose, onChanged, onStarted, onToast, onEditAccount, onOpenBetaSource }: BoundStrategyExecutionDialogProps) {
  const [execution, setExecution] = useState<BetaCampaign | null>(null)
  const [preparation, setPreparation] = useState<StrategyRunPrepareResponse | null>(null)
  const [preparationStage, setPreparationStage] = useState<PreparationStage>('checking')
  const [busy, setBusy] = useState(false)
  const [riskAcknowledged, setRiskAcknowledged] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const [panelIssue, setPanelIssue] = useState<StrategyPanelIssue | null>(null)
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
    setConfirmation('')
    setRiskAcknowledged(false)
    if (current) onChangedRef.current(current)
    if (next.disposition === 'unavailable') {
      setPanelIssue(strategyPanelError(next.message, 'prepare', next))
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
      setPanelIssue(null)
      try {
        if (!enabled) return
        // The server owns active-task detection and historical recovery, so a
        // normal launch needs one read-only preparation request rather than a
        // slow client-side list-then-preview race.
        const prepared = await prepareBoundStrategyRun(targetAccount)
        if (!current()) return
        applyPreparation(prepared)
      } catch (reason) {
        if (current()) setPanelIssue(strategyPanelError(reason, 'prepare'))
      } finally {
        if (current()) setPreparationStage(null)
      }
    }
    void load()
    return () => { cancelled = true }
  }, [applyPreparation, dialogKey, enabled])

  const preview = useCallback(async () => {
    const requestId = ++preparationRequestRef.current
    const targetAccount = accountRef.current
    setPreparationStage('preflight')
    setExecution(null)
    setRiskAcknowledged(false)
    setConfirmation('')
    setPanelIssue(null)
    try {
      const next = await prepareBoundStrategyRun(targetAccount)
      if (preparationRequestRef.current === requestId) applyPreparation(next)
    } catch (reason) {
      if (preparationRequestRef.current === requestId) setPanelIssue(strategyPanelError(reason, 'prepare'))
    } finally {
      if (preparationRequestRef.current === requestId) setPreparationStage(null)
    }
  }, [applyPreparation])

  useBoundStrategyRecovery(account.id, enabled, execution, preview)

  const cleanup = async () => {
    if (preparation?.disposition !== 'orders_cleanup_required') return
    setBusy(true)
    setPanelIssue(null)
    try {
      const next = await cleanupBoundStrategyRun(accountRef.current, confirmation)
      applyPreparation(next)
      onToastRef.current(`${accountRef.current.name} 的普通挂单与条件单已撤销并核验`)
    } catch (reason) {
      setPanelIssue(strategyPanelError(reason, 'cleanup'))
    } finally {
      setBusy(false)
    }
  }

  const execute = async () => {
    if (!execution) return
    const commandId = crypto.randomUUID()
    setBusy(true)
    setPanelIssue(null)
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
        setPanelIssue(strategyPanelNotice(
          '执行容量暂时已满',
          `当前已有 ${confirmed.capacity.activeExecutions}/${confirmed.capacity.maxActiveExecutions} 个活动任务。`,
          '等待其他任务退出后，重新粘贴当前确认短语并再次点击确认；若预览过期，先重新获取确认。',
          'none',
        ))
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
      setPanelIssue(strategyPanelNotice('正在核验启动结果', '启动响应中断，系统不会自动重发命令。', '请等待只读核验完成；不要再次点击启动。', 'none'))
      try {
        const executions = await listBoundStrategyExecutions(accountRef.current)
        const resolved = executions.find((item) => item.campaignId === execution.campaignId)
        if (resolved && resolved.status !== 'planned') {
          update(resolved)
          if (resolved.status === 'executing') {
            onStartedRef.current(resolved)
            onToastRef.current(`${accountRef.current.name} 的启动命令已确认，策略正在执行`)
          } else if (resolved.status === 'stopped') {
            const prepared = await prepareBoundStrategyRun(accountRef.current)
            applyPreparation(prepared)
            setPanelIssue(strategyPanelError('启动条件已变化，请重新确认', 'confirm'))
          } else if (resolved.status === 'uncertain') {
            setPanelIssue(strategyPanelNotice('启动结果待核验', '执行器已经接收命令，但任务结果尚未收敛。', '点击刷新核验状态；系统只读取订单、仓位和成交，不会重发启动命令。', 'verify_execution', '刷新核验状态'))
          }
          return
        }
      } catch {
        // Keep the original transport error below.  A failed read must not
        // trigger an order/execute retry either.
      }
      setPanelIssue(strategyPanelError(reason, 'confirm'))
    } finally {
      setBusy(false)
    }
  }

  const stop = async () => {
    if (!execution) return
    setBusy(true)
    setPanelIssue(null)
    try {
      update(await stopBoundStrategyExecution(accountRef.current, execution, confirmation))
      onToastRef.current(`${accountRef.current.name} 正在安全停止；撤单、仓位和成交将继续核验`)
    } catch (reason) {
      setPanelIssue(strategyPanelError(reason, 'stop'))
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
          />

          {!enabled && <BoundStrategyPanelIssueView
            issue={strategyPanelNotice('实盘执行器不可用', '执行服务尚未启用或正在恢复，本次不会提交订单。', '刷新页面；顶部恢复“实盘执行可用”后重新打开启动面板。', 'reload_page', '刷新页面')}
            busy={busy}
            onRetryPrepare={() => void preview()} onVerifyExecution={() => void preview()}
            onEditAccount={onEditAccount}
            onOpenBetaSettings={onOpenBetaSource}
          />}
          {panelIssue && <BoundStrategyPanelIssueView
            issue={panelIssue}
            busy={busy || Boolean(preparationStage)}
            onRetryPrepare={() => void preview()} onVerifyExecution={() => void preview()}
            onEditAccount={onEditAccount}
            onOpenBetaSettings={onOpenBetaSource}
          />}

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
              <p>{panelIssue ? '启动确认未能生成。重新读取只会执行只读预检，不会提交订单。' : '当前没有可用的启动确认。重新读取时不会提交订单。'}</p>
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
                  <span>我理解这会在实盘提交 {execution.direction === 'btc_long_eth_short' ? 'BTC 多、ETH 空' : 'BTC 空、ETH 多'}的 POST_ONLY 订单。<small>方向由绑定策略固定；400x 全仓，正常订单保持 Maker。仅当前任务产生且不超过 {execution.dustClosePolicy.maxQuote} USDT 的小额尾仓，可能按仓位 ID 市价收尾一次。</small></span>
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
