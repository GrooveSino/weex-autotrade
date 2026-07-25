import type { AccountInstance, BetaCampaign } from '../../types'

interface BoundStrategyOverviewProps {
  account: AccountInstance
  execution: BetaCampaign | null
}

const quote = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export function BoundStrategyOverview({
  account,
  execution,
}: BoundStrategyOverviewProps) {
  const targetMinimum = Number(account.strategy.targetVolumeQuoteMin)
  const targetMaximum = Number(account.strategy.targetVolumeQuoteMax)
  const hasTargetRange = targetMinimum !== targetMaximum
  const selectedTarget = execution?.selectedTargetQuoteVolume ?? execution?.strategyTargetQuoteVolume
  const executionTarget = execution?.executionTargetQuoteVolume ?? execution?.targetQuote
  const direction = execution?.direction ?? account.strategy.direction
  return <>
    <div className="beta-campaign-summary">
      <div className="beta-campaign-summary-primary">
        <span>已绑定共享策略</span>
        <strong>{execution?.strategyName ?? account.strategy.name}<small>v{execution?.strategyVersion ?? account.strategy.version}</small></strong>
      </div>
      <div>
        <span>策略方向</span>
        <strong>{direction === 'btc_long_eth_short' ? 'BTC 多 · ETH 空' : 'BTC 空 · ETH 多'}</strong>
      </div>
      <div>
        <span>{hasTargetRange ? '策略目标范围' : '策略目标'}</span>
        <strong>{hasTargetRange ? `${quote.format(targetMinimum)} - ${quote.format(targetMaximum)}` : quote.format(targetMaximum)}<small>USDT</small></strong>
      </div>
      {hasTargetRange && selectedTarget !== null && selectedTarget !== undefined && <div>
        <span>本次随机目标</span>
        <strong>{quote.format(Number(selectedTarget))}<small>USDT</small></strong>
      </div>}
      {executionTarget !== null && executionTarget !== undefined && <div>
        <span>{execution?.targetMode === 'lifetime' ? '本次待完成' : '本次新增目标'}</span>
        <strong>{quote.format(Number(executionTarget))}<small>USDT</small></strong>
      </div>}
      <div>
        <span>每轮总交易量</span>
        <strong>{quote.format(Number(execution?.roundTurnoverQuoteMin ?? account.strategy.roundTurnoverQuoteMin))} - {quote.format(Number(execution?.cycleVolume ?? account.strategy.roundTurnoverQuoteMax))}<small>USDT</small></strong>
      </div>
      <div>
        <span>持仓时间</span>
        <strong>{execution ? `${execution.holdMinSeconds}-${execution.holdMaxSeconds}s` : `${account.strategy.positionHoldMinSeconds}-${account.strategy.positionHoldMaxSeconds}s`}</strong>
      </div>
      <div>
        <span>轮次间隔</span>
        <strong>{execution ? `${execution.roundGapMinSeconds}-${execution.roundGapMaxSeconds}s` : `${account.strategy.roundIntervalMinSeconds}-${account.strategy.roundIntervalMaxSeconds}s`}</strong>
      </div>
    </div>
  </>
}
