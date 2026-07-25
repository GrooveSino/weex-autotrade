import type { MutableRefObject, RefObject } from 'react'
import {
  ArrowDownToLine, CircleDot, Copy, History, LoaderCircle,
  RefreshCw, Terminal, Trash2, X,
} from 'lucide-react'
import type { AccountInstance, ActiveExecutionWait, LogLine, StrategyMonitorSnapshot } from '../../types'
import { countdown, displayTime, formatExecutionDurations, levelLabel, quote } from './logDrawerFormat'
import { presentRuntimeWait, type RuntimeWaitPresentation } from './monitorRuntimeStage'

type Tab = 'monitor' | 'system'
type ConnectionState = 'connecting' | 'connected' | 'retrying'

interface Props {
  account: AccountInstance
  tab: Tab
  setTab: (tab: Tab) => void
  connection: ConnectionState
  monitor: StrategyMonitorSnapshot | null
  monitorLoading: boolean
  systemLines: LogLine[]
  systemLoading: boolean
  error: string | null
  clearArmed: boolean
  clearBusy: boolean
  loadOlderBusy: boolean
  primaryRuntimeWait: RuntimeWaitPresentation | null
  serverNowMs: number
  volumeState: string
  volumeDetail: string
  plainText: string
  bodyRef: RefObject<HTMLDivElement | null>
  followTailRef: MutableRefObject<boolean>
  onClose: () => void
  onDownload: () => void
  onClear: () => void
  onLoadOlder: () => void
}

export function LogDrawerView(props: Props) {
  const {
    account, tab, setTab, connection, monitor, monitorLoading, systemLines,
    systemLoading, error, clearArmed, clearBusy, loadOlderBusy, primaryRuntimeWait,
    serverNowMs, volumeState, volumeDetail, plainText, bodyRef,
    followTailRef, onClose, onDownload, onClear, onLoadOlder,
  } = props
  return <div className="drawer-backdrop" role="presentation" onMouseDown={onClose}>
    <aside className="log-drawer execution-monitor" role="dialog" aria-modal="true" aria-labelledby="log-title" onMouseDown={(event) => event.stopPropagation()}>
      <header className="drawer-header">
        <div className="drawer-title"><span className="terminal-mark"><Terminal size={16} /></span><div><h2 id="log-title">{account.name}</h2><span>{account.id} / 执行监控</span></div></div>
        <div className="drawer-actions">
          <button className="icon-button dark" type="button" onClick={() => navigator.clipboard.writeText(plainText)} data-tooltip="复制当前视图" aria-label="复制当前视图"><Copy size={15} /></button>
          <button className="icon-button dark" type="button" onClick={onDownload} data-tooltip="下载当前视图" aria-label="下载当前视图"><ArrowDownToLine size={15} /></button>
          {tab === 'system' && <button className={`icon-button dark log-clear-button ${clearArmed ? 'armed' : ''}`} type="button" onClick={onClear} disabled={clearBusy || (!clearArmed && systemLines.length === 0)} data-tooltip={clearArmed ? '再次点击确认清除' : '清除系统日志'} aria-label={clearArmed ? '确认清除系统日志' : '清除系统日志'}>{clearBusy ? <LoaderCircle className="spin" size={14} /> : <Trash2 size={14} />}<span>{clearArmed ? '确认清除' : '清除'}</span></button>}
          <button className="icon-button dark" type="button" onClick={onClose} data-tooltip="关闭" aria-label="关闭执行监控"><X size={16} /></button>
        </div>
      </header>
      <div className="monitor-tabs" role="tablist">
        <button type="button" className={tab === 'monitor' ? 'active' : ''} onClick={() => setTab('monitor')}><Terminal size={13} />执行监控</button>
        <button type="button" className={tab === 'system' ? 'active' : ''} onClick={() => setTab('system')}><History size={13} />系统日志</button>
      </div>
      <div className={`terminal-statusbar ${connection}`}><span><CircleDot size={12} />{connection === 'connecting' ? '正在建立连接' : connection === 'retrying' ? '监控同步恢复中' : '本地执行器实时连接'}</span><span>{tab === 'monitor' ? '事件驱动 / 倒计时 8 fps' : '仅当前实例 / 1s 增量读取'}</span></div>
      <section className={`runtime-stage-strip ${primaryRuntimeWait ? 'waiting' : ''} ${primaryRuntimeWait?.state === 'transitioning' ? 'transitioning' : ''}`} aria-label="当前运行阶段">
        <div className="runtime-stage-main">{primaryRuntimeWait ? <LoaderCircle className="spin" size={15} /> : <CircleDot size={13} />}<span><small>{primaryRuntimeWait?.label ?? '当前阶段'}</small><strong>{primaryRuntimeWait?.phase ?? monitor?.phase ?? '等待执行状态'}</strong></span></div>
        <div className="runtime-stage-context"><span>运行 <strong>{monitor?.currentRun || '-'}</strong></span><span>轮次 <strong>{monitor?.currentRound || '-'}</strong></span></div>
        <div className="runtime-stage-countdown"><small>{primaryRuntimeWait?.countdownLabel ?? '倒计时'}</small><strong>{runtimeTime(primaryRuntimeWait)}</strong></div>
      </section>
      <div className="terminal-body monitor-body" ref={bodyRef} onScroll={(event) => { const node = event.currentTarget; followTailRef.current = node.scrollHeight - node.scrollTop - node.clientHeight < 48 }}>
        {tab === 'monitor' ? monitorLoading && !monitor ? <div className="terminal-loading"><span className="terminal-cursor" />正在读取本次任务...</div> : <>
          {monitor && <>
            <section className="monitor-summary" aria-label="任务摘要">
              <div className="monitor-metric-primary"><span>本次已核验 / 目标</span><strong>{quote(monitor.verifiedQuoteVolume)} / {quote(monitor.targetQuoteVolume)} USDT</strong></div><div className="monitor-metric-primary"><span>剩余目标</span><strong>{quote(monitor.remainingQuoteVolume)} USDT</strong></div>
              <div><span>状态 / 阶段</span><strong>{monitor.status} / {monitor.phase}</strong></div><div><span>运行 / 轮次</span><strong>{monitor.currentRun || '-'} / {monitor.currentRound || '-'}</strong></div>
              <div><span>BTC / ETH 成交量</span><strong>{quote(monitor.btcQuoteVolume)} / {quote(monitor.ethQuoteVolume)} USDT</strong></div><div><span>Maker / Taker / Unknown</span><strong>{monitor.makerFillCount} / {monitor.takerFillCount} / {monitor.unknownFillCount}</strong></div>
              <div><span>挂单 / 撤单 / requote</span><strong>{monitor.submissions} / {monitor.cancels} / {monitor.requotes}</strong></div><div><span>数据来源</span><strong className={monitor.auditStatus !== 'verified' || monitor.ledgerSyncState !== 'complete' ? 'monitor-unverified' : 'monitor-verified'}>{volumeState}</strong>{volumeDetail && <small className="monitor-ledger-progress">{volumeDetail}</small>}</div>
            </section>
            <section className="active-waits" aria-label="当前等待"><header><span>当前活动</span><small>{monitor.activeWaits.length ? `${monitor.activeWaits.length} 项并行等待` : '无活动等待'}</small></header>
              {monitor.activeWaits.map((wait) => <ActiveWaitRow key={wait.key} wait={wait} serverNowMs={serverNowMs} />)}
            </section>
            <section className="monitor-timeline" aria-label="执行时间线"><header><span>执行时间线</span><small>只记录状态变化，等待心跳原地更新</small></header>{monitor.hasMore && <button className="timeline-load-older" type="button" disabled={loadOlderBusy} onClick={onLoadOlder}>{loadOlderBusy ? <LoaderCircle className="spin" size={12} /> : <RefreshCw size={12} />}加载更早记录</button>}{monitor.timeline.map((entry) => <div className={`log-line monitor-entry ${entry.level}`} key={entry.id}><time>{displayTime(entry.atMs)}</time><span className="log-level">{levelLabel[entry.level]}</span><span className="log-message"><strong>{formatExecutionDurations(entry.title)}</strong>{entry.detail && <small>{formatExecutionDurations(entry.detail)}</small>}</span></div>)}{!monitor.timeline.length && <div className="terminal-empty">等待第一个执行事件...</div>}</section>
          </>}{error && <div className="terminal-error terminal-retry">执行监控：{error}</div>}
        </> : systemLoading && !systemLines.length ? <div className="terminal-loading"><span className="terminal-cursor" />正在请求系统日志...</div> : <>{systemLines.map((line) => <div className={`log-line ${line.level}`} key={line.id}><time>{displayTime(line.timestamp)}</time><span className="log-level">{levelLabel[line.level]}</span><span className="log-message">{formatExecutionDurations(line.message)}</span></div>)}{!systemLines.length && !error && <div className="terminal-empty">暂无系统日志</div>}{error && <div className="terminal-error terminal-retry">系统日志：{error}</div>}</>}
      </div>
    </aside>
  </div>
}

function runtimeTime(runtime: RuntimeWaitPresentation | null): string {
  if (!runtime) return '--:--.-'
  if (runtime.state === 'transitioning') return '切换中'
  if (runtime.state === 'indefinite') return '进行中'
  return countdown(runtime.remainingMs)
}

function ActiveWaitRow({ wait, serverNowMs }: { wait: ActiveExecutionWait; serverNowMs: number }) {
  const runtime = presentRuntimeWait(wait, serverNowMs)
  const total = runtime.remainingMs === null ? 0 : Math.max(1, runtime.elapsedMs + runtime.remainingMs)
  const progress = runtime.state === 'transitioning'
    ? 100
    : total > 0 ? Math.min(100, runtime.elapsedMs / total * 100) : 0
  return <div className={`active-wait-row ${runtime.state}`}>
    <LoaderCircle className="spin" size={14} />
    <div><strong>{runtime.label}</strong>{wait.detail && <span>{wait.detail}</span>}</div>
    <time>已等待 {(runtime.elapsedMs / 1000).toFixed(1)}s{runtime.state === 'counting' && <> / 剩余 {((runtime.remainingMs ?? 0) / 1000).toFixed(1)}s</>}{runtime.state === 'transitioning' && <> / 正在确认下一阶段</>}</time>
    {runtime.state !== 'indefinite' && <span className="wait-progress"><i style={{ width: `${progress}%` }} /></span>}
  </div>
}
