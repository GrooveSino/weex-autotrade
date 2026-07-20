import { Activity, FileTerminal, Pause, Play, RefreshCw, RotateCcw, ShieldAlert } from 'lucide-react'
import type { AccountInstance } from '../types'
import type { BetaCampaign } from '../types'
import { calculateFundingPreflight, countdown, estimateRounds, targetModeLabel, targetProgress } from '../utils/strategy'
import { AccountActionsMenu } from './AccountActionsMenu'

interface AccountTableProps {
  accounts: AccountInstance[]
  selectedIds: Set<string>
  refreshingIds: Set<string>
  executionDisabled: boolean
  onSelect: (id: string, selected: boolean) => void
  onSelectAll: (selected: boolean) => void
  onToggleRunning: (account: AccountInstance) => void
  onOpenLogs: (account: AccountInstance) => void
  onOpenExecutions: (account: AccountInstance) => void
  onRefresh: (account: AccountInstance) => void
  onClosePositions: (account: AccountInstance) => void
  onEdit: (account: AccountInstance) => void
  onAssignStrategy: (account: AccountInstance) => void
  onOpenBetaCampaign: (account: AccountInstance) => void
  betaCampaigns: BetaCampaign[]
  betaCampaignAvailable: boolean
}

const currency = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

const statusLabel: Record<AccountInstance['status'], string> = {
  running: '运行中', paused: '已暂停', stopped: '已停止', warning: '需处理', error: '错误',
}

const modeLabel: Record<AccountInstance['mode'], string> = {
  demo: '演示',
  live: '实盘',
}

const emptyRuntime: AccountInstance['runtime'] = {
  lastPollStartedAtMs: null,
  lastPollSucceededAtMs: null,
  lastPollFailedAtMs: null,
  lastPollDurationMs: null,
  consecutiveFailures: 0,
  lastErrorType: null,
}

function relativeTime(timestamp: number | null): string {
  if (timestamp === null) return '未轮询'
  const seconds = Math.max(0, Math.round((Date.now() - timestamp) / 1_000))
  if (seconds < 5) return '刚刚'
  if (seconds < 60) return `${seconds} 秒前`
  const minutes = Math.round(seconds / 60)
  if (minutes < 60) return `${minutes} 分钟前`
  return `${Math.round(minutes / 60)} 小时前`
}

export function AccountTable({ accounts, selectedIds, refreshingIds, executionDisabled, onSelect, onSelectAll, onToggleRunning, onOpenLogs, onOpenExecutions, onRefresh, onClosePositions, onEdit, onAssignStrategy, onOpenBetaCampaign, betaCampaigns, betaCampaignAvailable }: AccountTableProps) {
  const allSelected = accounts.length > 0 && accounts.every((account) => selectedIds.has(account.id))

  return (
    <div className="table-shell">
      <table className="account-table">
        <thead>
          <tr>
            <th className="checkbox-column"><input type="checkbox" checked={allSelected} onChange={(event) => onSelectAll(event.target.checked)} aria-label="选择全部账号" /></th>
            <th>账号实例</th>
            <th>运行状态</th>
            <th>代理</th>
            <th className="numeric">合约钱包</th>
            <th className="numeric">累计交易量</th>
            <th>BTC 多 / ETH 空</th>
            <th>策略进度</th>
            <th>同步</th>
            <th className="actions-column">操作</th>
          </tr>
        </thead>
        <tbody>
          {accounts.map((account) => {
            const session = account.volume.session
            const targetVolume = session ? Number(session.targetQuoteVolume) : Number(account.strategy.targetVolumeQuote)
            const achievedVolume = session ? Number(session.verifiedQuoteVolume) : targetProgress(account)
            const remainingVolume = session ? Number(session.remainingQuoteVolume) : Math.max(targetVolume - achievedVolume, 0)
            const progress = targetVolume ? Math.min(100, achievedVolume / targetVolume * 100) : 0
            const estimate = estimateRounds(account.strategy, achievedVolume)
            const runtime = account.runtime ?? emptyRuntime
            const funding = account.fundingPreflight ?? calculateFundingPreflight(
              account.strategy,
              account.wallet.available,
              account.wallet.equity > 0 || account.wallet.available > 0,
            )
            const fundingLabel = funding.status === 'ready'
              ? `自动 ${funding.plannedLeverage}x`
              : funding.status === 'insufficient' ? `资金不足 · 需 ${funding.requiredLeverage ?? '>99'}x` : '资金待同步'
            const openingNewPair = account.strategyProgress.stage !== 'holding'
            const startBlocked = openingNewPair && (
              funding.status !== 'ready'
              || (account.strategy.targetMode === 'lifetime' && !account.volume.complete)
            )
            return (
              <tr key={account.id} className={selectedIds.has(account.id) ? 'selected-row' : ''}>
                <td className="checkbox-column"><input type="checkbox" checked={selectedIds.has(account.id)} onChange={(event) => onSelect(account.id, event.target.checked)} aria-label={`选择 ${account.name}`} /></td>
                <td>
                  <div className="account-cell">
                    <span className="account-avatar">{account.name.slice(0, 1)}</span>
                    <div>
                      <strong>{account.name}</strong>
                      <span>{account.accountTag} · ••••{account.apiKeyTail}</span>
                    </div>
                    <span className={`mode-badge ${account.mode}`}>{modeLabel[account.mode]}</span>
                  </div>
                </td>
                <td>
                  <div className="status-cell">
                    <span className={`status-dot ${account.status}`} />
                    <div><strong>{statusLabel[account.status]}</strong><span>{account.phase}</span></div>
                  </div>
                </td>
                <td>
                  <div className="proxy-cell">
                    <div><span className={`proxy-health ${account.proxy.status}`} /> <strong className="proxy-mark" title="代理">P</strong> {account.proxy.host}</div>
                    <span>{account.proxy.location} · {account.proxy.latencyMs ? `${account.proxy.latencyMs} ms` : '未检测'}</span>
                  </div>
                </td>
                <td className="numeric">
                  <strong className="money">${currency.format(account.wallet.equity)}</strong>
                  <span className={account.wallet.unrealizedPnl < 0 ? 'negative' : account.wallet.unrealizedPnl > 0 ? 'positive' : ''}>
                    可用 ${currency.format(account.wallet.available)} · <span className={`funding-state ${funding.status}`}>{fundingLabel}</span>
                  </span>
                </td>
                <td className="numeric">
                  <strong className="money">${currency.format(account.volume.lifetime)}</strong>
                  <span>
                    {!account.volume.complete && (
                      <span className="volume-incomplete" title="历史交易量尚未完整同步" aria-label="历史交易量尚未完整同步">
                        <ShieldAlert size={12} aria-hidden="true" />
                        历史未完
                      </span>
                    )} 今日 ${currency.format(account.volume.today)}
                  </span>
                </td>
                <td>
                  <div className="exposure-cell">
                    <span><i className="btc" />BTC <strong>${currency.format(account.exposure.btcLong)}</strong></span>
                    <span><i className="eth" />ETH <strong>${currency.format(account.exposure.ethShort)}</strong></span>
                  </div>
                </td>
                <td>
                  <div className="progress-label"><span>{session ? '本次测试' : '策略'} ${currency.format(achievedVolume)} / ${currency.format(targetVolume)}</span><span>{progress.toFixed(0)}%</span></div>
                  <div className="progress-track"><span style={{ width: `${progress}%` }} /></div>
                  <span className={`next-action ${session && (session.stale || session.reconciliationRequired) ? 'warning' : ''}`}>
                    {session && (session.stale || session.reconciliationRequired) ? '数据待核验' : session ? `还差 ${currency.format(remainingVolume)}` : null}
                    {session ? ' · ' : ''}
                    {account.strategyProgress.stage === 'holding' ? '待平仓' : account.strategyProgress.stage === 'cooldown' ? '待下轮' : account.strategyProgress.stage === 'complete' ? '已完成' : '待启动'} · {countdown(account.strategyProgress.nextActionAtMs)}
                  </span>
                  <span className="strategy-scope">{account.strategy.name} · {targetModeLabel(account.strategy.targetMode)} · 每轮 {account.strategy.roundTurnoverQuoteMin}-{account.strategy.roundTurnoverQuoteMax} · 约 {estimate ? `${estimate.minimum}-${estimate.maximum}` : '-'} 轮</span>
                </td>
                <td>
                  <div className={`runtime-cell ${runtime.consecutiveFailures ? 'failed' : ''}`} title={runtime.lastErrorType ?? '最近轮询正常'}>
                    <span><i />{relativeTime(runtime.consecutiveFailures ? runtime.lastPollFailedAtMs : runtime.lastPollSucceededAtMs)}</span>
                    <small>{runtime.lastPollDurationMs === null ? '无耗时' : `${runtime.lastPollDurationMs} ms`}{runtime.consecutiveFailures ? ` · 连败 ${runtime.consecutiveFailures}` : ''}</small>
                  </div>
                </td>
                <td className="actions-column">
                  <div className="row-actions">
                    <button className="icon-button" type="button" onClick={() => onToggleRunning(account)} disabled={executionDisabled || (account.status !== 'running' && startBlocked)} data-tooltip={executionDisabled ? '只读模式不可操作' : startBlocked ? funding.status === 'insufficient' ? '资金不足，无法在 99x 内完成最大单轮' : account.strategy.targetMode === 'lifetime' && !account.volume.complete ? '请先完成历史成交同步' : '等待钱包余额同步' : account.status === 'running' ? '暂停实例' : account.status === 'paused' ? '继续实例' : '启动实例'} aria-label={executionDisabled ? '只读模式不可操作' : account.status === 'running' ? '暂停实例' : account.status === 'paused' ? '继续实例' : '启动实例'}>
                      {account.status === 'running' ? <Pause size={15} /> : account.status === 'paused' ? <Play size={15} /> : <Activity size={15} />}
                    </button>
                    <button className="icon-button" type="button" onClick={() => onRefresh(account)} data-tooltip="刷新账户快照" aria-label="刷新账户快照" disabled={refreshingIds.has(account.id)}>
                      <RotateCcw size={15} className={refreshingIds.has(account.id) ? 'spin' : ''} />
                    </button>
                    <button className="icon-button log-button" type="button" onClick={() => onOpenLogs(account)} data-tooltip="查看实例日志" aria-label="查看实例日志">
                      <FileTerminal size={15} />{account.unreadLogs > 0 && <span className="unread-dot">{account.unreadLogs}</span>}
                    </button>
                    <AccountActionsMenu
                      account={account}
                      executionDisabled={executionDisabled}
                      onClosePositions={onClosePositions}
                      onOpenExecutions={onOpenExecutions}
                      onAssignStrategy={onAssignStrategy}
                      onEdit={onEdit}
                      onOpenBetaCampaign={onOpenBetaCampaign}
                      betaCampaignAvailable={betaCampaignAvailable && account.mode === 'live' && account.status !== 'running'}
                      betaCampaignActive={betaCampaigns.some((campaign) => campaign.instanceId === account.id && ['planned', 'executing', 'stopping'].includes(campaign.status))}
                    />
                  </div>
                </td>
              </tr>
            )
          })}
        </tbody>
      </table>
      {accounts.length === 0 && <div className="empty-state"><RefreshCw size={20} /><span>当前筛选没有账号实例</span></div>}
    </div>
  )
}
