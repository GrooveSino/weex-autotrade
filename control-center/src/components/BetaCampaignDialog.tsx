import { useMemo, useState } from 'react'
import { AlertTriangle, CheckCircle2, Copy, ExternalLink, LoaderCircle, OctagonAlert, Play, Square, X } from 'lucide-react'
import type { AccountInstance, BetaCampaign, BetaCampaignPreview } from '../types'
import { executeBetaCampaign, previewBetaCampaign, stopBetaCampaign } from '../services/controlCenter'

interface BetaCampaignDialogProps {
  account: AccountInstance
  campaign: BetaCampaign | null
  liveEnabled: boolean
  onClose: () => void
  onChanged: (campaign: BetaCampaign) => void
  onToast: (message: string) => void
}

const quote = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })
const statusLabels: Record<BetaCampaign['status'], string> = {
  planned: '待确认', executing: '运行中', stopping: '等待安全停止', completed: '已完成', stopped: '已停止', uncertain: '待人工核对',
}

function copyText(value: string, onToast: (message: string) => void) {
  void navigator.clipboard?.writeText(value).then(() => onToast('已复制精确短语')).catch(() => onToast('浏览器未允许复制，请手动选择短语'))
}

export function BetaCampaignDialog({ account, campaign, liveEnabled, onClose, onChanged, onToast }: BetaCampaignDialogProps) {
  const [targetQuote, setTargetQuote] = useState('6000')
  const [cycleVolume, setCycleVolume] = useState('500')
  const [holdMin, setHoldMin] = useState('5')
  const [holdMax, setHoldMax] = useState('7')
  const [gapMin, setGapMin] = useState('5')
  const [gapMax, setGapMax] = useState('7')
  const [preview, setPreview] = useState<BetaCampaignPreview | null>(campaign && campaign.status === 'planned' ? campaign as BetaCampaignPreview : null)
  const [riskAcknowledged, setRiskAcknowledged] = useState(false)
  const [confirmation, setConfirmation] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const active = campaign && ['executing', 'stopping'].includes(campaign.status)
  const terminal = campaign && ['completed', 'stopped', 'uncertain'].includes(campaign.status)
  const exactMatch = preview ? confirmation === preview.confirmation : false
  const canExecute = Boolean(preview && riskAcknowledged && exactMatch && !busy)
  const progress = useMemo(() => {
    const current = Number(campaign?.generatedQuote ?? preview?.generatedQuote ?? 0)
    const target = Number(campaign?.targetQuote ?? preview?.targetQuote ?? targetQuote)
    return target > 0 ? Math.min(100, current / target * 100) : 0
  }, [campaign, preview, targetQuote])

  const runPreview = async () => {
    setBusy(true)
    setError(null)
    try {
      const next = await previewBetaCampaign(account, {
        targetQuote,
        cycleVolume,
        holdMinSeconds: Math.round(Number(holdMin) * 60),
        holdMaxSeconds: Math.round(Number(holdMax) * 60),
        roundGapMinSeconds: Math.round(Number(gapMin) * 60),
        roundGapMaxSeconds: Math.round(Number(gapMax) * 60),
      })
      setPreview(next)
      setConfirmation('')
      setRiskAcknowledged(false)
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '预览失败')
    } finally {
      setBusy(false)
    }
  }

  const execute = async () => {
    if (!preview) return
    setBusy(true)
    setError(null)
    try {
      const next = await executeBetaCampaign(account, preview.campaignId, confirmation, riskAcknowledged)
      onChanged(next)
      onToast('Beta Campaign 已启动')
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '启动失败')
    } finally {
      setBusy(false)
    }
  }

  const requestStop = async () => {
    if (!campaign) return
    setBusy(true)
    setError(null)
    try {
      onChanged(await stopBetaCampaign(account, campaign))
      onToast('已发出安全停止请求；当前子周期会先完成核对')
    } catch (nextError) {
      setError(nextError instanceof Error ? nextError.message : '停止失败')
    } finally {
      setBusy(false)
    }
  }

  return (
    <section className="dialog beta-campaign-dialog" role="dialog" aria-modal="true" aria-labelledby="beta-campaign-title" onMouseDown={(event) => event.stopPropagation()}>
      <header className="dialog-header">
        <div><span className="dialog-kicker">WEEX LIVE</span><h2 id="beta-campaign-title">Beta Campaign · {account.name}</h2><p>BTC 多 / ETH 空 · 全部订单 POST_ONLY · 杠杆 AUTO</p></div>
        <button className="icon-button" type="button" onClick={onClose} aria-label="关闭"><X size={16} /></button>
      </header>
      {!liveEnabled && <div className="campaign-banner danger"><OctagonAlert size={16} />控制平面尚未开启网页实盘 campaign 能力</div>}
      {error && <div className="campaign-banner danger"><AlertTriangle size={16} />{error}</div>}

      {active && campaign ? (
        <div className="campaign-monitor">
          <div className="campaign-status-line"><span className={`campaign-status ${campaign.status}`}>{statusLabels[campaign.status]}</span><strong>{campaign.phase}</strong><span>Campaign {campaign.campaignId}</span></div>
          <div className="campaign-progress"><span style={{ width: `${progress}%` }} /></div>
          <div className="campaign-metrics"><div><small>已完成</small><strong>{quote.format(Number(campaign.generatedQuote))} USDT</strong></div><div><small>剩余</small><strong>{quote.format(Number(campaign.remainingQuote))} USDT</strong></div><div><small>当前轮次</small><strong>{campaign.currentRun} / {campaign.maxRuns}</strong></div><div><small>BTC / ETH</small><strong>{quote.format(Number(campaign.btcQuote))} / {quote.format(Number(campaign.ethQuote))}</strong></div><div><small>Maker 成交</small><strong>{campaign.makerCount} / {campaign.fillCount}</strong></div><div><small>Taker / Unknown</small><strong>{campaign.takerCount} / {campaign.unknownCount}</strong></div></div>
          <div className="campaign-event-list">{campaign.events.slice(-12).reverse().map((event) => <div key={`${event.sequence}-${event.name}`}><time>{new Date(event.atMs).toLocaleTimeString()}</time><span>{event.message ?? event.name}</span></div>)}</div>
          <footer className="dialog-actions"><button className="button secondary" type="button" onClick={onClose}>关闭</button><button className="button danger-button" type="button" disabled={busy || campaign.status === 'stopping'} onClick={() => void requestStop()}><Square size={14} />安全停止</button></footer>
        </div>
      ) : terminal && campaign ? (
        <div className="campaign-monitor">
          <div className="campaign-status-line"><span className={`campaign-status ${campaign.status}`}>{statusLabels[campaign.status]}</span><strong>{campaign.reason ?? campaign.phase}</strong></div>
          <div className="campaign-metrics"><div><small>已完成</small><strong>{quote.format(Number(campaign.generatedQuote))} USDT</strong></div><div><small>超额</small><strong>{quote.format(Number(campaign.excessQuote))} USDT</strong></div><div><small>BTC / ETH</small><strong>{quote.format(Number(campaign.btcQuote))} / {quote.format(Number(campaign.ethQuote))}</strong></div><div><small>Maker / Taker / Unknown</small><strong>{campaign.makerCount} / {campaign.takerCount} / {campaign.unknownCount}</strong></div><div><small>耗时</small><strong>{campaign.elapsedMs ? `${Math.round(campaign.elapsedMs / 1000)}s` : '-'}</strong></div></div>
          {campaign.status === 'uncertain' && <div className="campaign-banner danger"><OctagonAlert size={16} />状态不确定：请先在交易所核对仓位、挂单和成交，不提供自动重试。</div>}
          <footer className="dialog-actions"><button className="button secondary" type="button" onClick={onClose}>关闭</button><button className="button secondary" type="button" onClick={() => copyText(campaign.campaignId, onToast)}><Copy size={14} />复制 Campaign ID</button></footer>
        </div>
      ) : (
        <>
          <div className="campaign-form-grid">
            <label><span>目标交易量</span><div className="input-suffix"><input inputMode="decimal" value={targetQuote} onChange={(event) => setTargetQuote(event.target.value)} /><em>USDT</em></div></label>
            <label><span>每轮交易量</span><div className="input-suffix"><input inputMode="decimal" value={cycleVolume} onChange={(event) => setCycleVolume(event.target.value)} /><em>USDT</em></div></label>
            <label><span>持仓时间</span><div className="range-inputs"><input inputMode="numeric" value={holdMin} onChange={(event) => setHoldMin(event.target.value)} /><b>至</b><input inputMode="numeric" value={holdMax} onChange={(event) => setHoldMax(event.target.value)} /><em>分钟</em></div></label>
            <label><span>轮次间隔</span><div className="range-inputs"><input inputMode="numeric" value={gapMin} onChange={(event) => setGapMin(event.target.value)} /><b>至</b><input inputMode="numeric" value={gapMax} onChange={(event) => setGapMax(event.target.value)} /><em>分钟</em></div></label>
          </div>
          <div className="campaign-policy"><span><CheckCircle2 size={15} />固定 Beta：BTC 多 / ETH 空</span><span><CheckCircle2 size={15} />纯 Maker：POST_ONLY</span><span><CheckCircle2 size={15} />余额自动计算杠杆</span></div>
          {preview && <div className="campaign-preview"><div className="campaign-preview-head"><strong>预览已锁定</strong><span>{preview.campaignId}</span></div><div className="campaign-preview-grid"><div><small>Final Beta</small><strong>{preview.beta}</strong></div><div><small>数据年龄</small><strong>{Math.round(Number(preview.betaAgeMs) / 1000)}s</strong></div><div><small>自动杠杆</small><strong>{preview.plannedLeverage ?? 'AUTO'}x</strong></div><div><small>预计轮数</small><strong>{Math.ceil(Number(preview.targetQuote) / Number(preview.cycleVolume))}</strong></div></div><p>授权上限 {quote.format(Number(preview.authorizedMaxQuote))} USDT · 可用余额 {quote.format(Number(preview.availableQuote ?? 0))} USDT</p><label className="risk-check"><input type="checkbox" checked={riskAcknowledged} onChange={(event) => setRiskAcknowledged(event.target.checked)} /><span>我确认这是 Live 账户，接受纯 Maker 订单可能长时间不成交，且不确定状态必须人工核对。</span></label><div className="confirmation-box"><div><span>精确执行短语</span><button className="icon-button" type="button" onClick={() => copyText(preview.confirmation, onToast)} aria-label="复制精确执行短语"><Copy size={14} /></button></div><code>{preview.confirmation}</code><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} placeholder="粘贴或手动输入完整短语" spellCheck={false} /></div></div>}
          <footer className="dialog-actions"><button className="button secondary" type="button" onClick={onClose}>取消</button>{!preview ? <button className="button primary" type="button" disabled={!liveEnabled || busy} onClick={() => void runPreview()}>{busy ? <LoaderCircle size={14} className="spin" /> : <ExternalLink size={14} />}生成预览</button> : <button className="button primary" type="button" disabled={!canExecute} onClick={() => void execute()}>{busy ? <LoaderCircle size={14} className="spin" /> : <Play size={14} />}确认并启动</button>}</footer>
        </>
      )}
    </section>
  )
}
