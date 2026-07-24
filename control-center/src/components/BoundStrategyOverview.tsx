import type { AccountInstance, BetaCampaign, StrategyDirection } from '../types'

interface BoundStrategyOverviewProps {
  account: AccountInstance
  execution: BetaCampaign | null
  direction: StrategyDirection
  directionDisabled: boolean
  onDirectionChange: (direction: StrategyDirection) => void
}

const quote = new Intl.NumberFormat('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })

export function BoundStrategyOverview({
  account,
  execution,
  direction,
  directionDisabled,
  onDirectionChange,
}: BoundStrategyOverviewProps) {
  return <>
    <div className="bound-direction-control" role="radiogroup" aria-label="BTC 与 ETH 交易方向">
      <span>交易方向</span>
      <div>
        <button type="button" role="radio" aria-checked={direction === 'btc_long_eth_short'} className={direction === 'btc_long_eth_short' ? 'active' : ''} disabled={directionDisabled} onClick={() => onDirectionChange('btc_long_eth_short')}>BTC 多 · ETH 空</button>
        <button type="button" role="radio" aria-checked={direction === 'btc_short_eth_long'} className={direction === 'btc_short_eth_long' ? 'active' : ''} disabled={directionDisabled} onClick={() => onDirectionChange('btc_short_eth_long')}>BTC 空 · ETH 多</button>
      </div>
    </div>
    <div className="beta-campaign-summary">
      <div className="beta-campaign-summary-primary">
        <span>已绑定共享策略</span>
        <strong>{execution?.strategyName ?? account.strategy.name}<small>v{execution?.strategyVersion ?? account.strategy.version}</small></strong>
      </div>
      <div>
        <span>{execution?.targetMode === 'lifetime' || account.strategy.targetMode === 'lifetime' ? '本次执行差额' : '本次新增目标'}</span>
        <strong>{quote.format(Number(execution?.executionTargetQuoteVolume ?? execution?.targetQuote ?? account.strategy.targetVolumeQuote))}<small>USDT</small></strong>
      </div>
      <div>
        <span>本次抽取目标</span>
        <strong>{quote.format(Number(execution?.selectedTargetQuoteVolume ?? execution?.strategyTargetQuoteVolume ?? account.strategy.targetVolumeQuoteMax))}<small>USDT</small></strong>
      </div>
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
