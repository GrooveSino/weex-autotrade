import type { AccountInstance, VolumeStrategy } from '../types'

const standardStrategy: VolumeStrategy = {
    id: 'strategy-standard-20k',
    version: 1,
    name: '标准 20K 双币策略',
    direction: 'btc_long_eth_short',
    targetMode: 'incremental',
    targetVolumeQuote: '20000',
    targetVolumeQuoteMin: '18000',
    targetVolumeQuoteMax: '20000',
    roundTurnoverQuoteMin: '500',
    roundTurnoverQuoteMax: '750',
    positionHoldMinSeconds: 300,
    positionHoldMaxSeconds: 900,
    roundIntervalMinSeconds: 600,
    roundIntervalMaxSeconds: 1800,
}

const compactStrategy: VolumeStrategy = {
  ...standardStrategy,
  id: 'strategy-compact-10k',
  name: '小额 10K 双币策略',
  targetVolumeQuote: '10000',
  targetVolumeQuoteMin: '8000',
  targetVolumeQuoteMax: '10000',
  roundTurnoverQuoteMin: '360',
  roundTurnoverQuoteMax: '600',
}

const overnightStrategy: VolumeStrategy = {
  ...standardStrategy,
  id: 'strategy-overnight-18k',
  name: '夜间 18K 双币策略',
  targetVolumeQuote: '18000',
  targetVolumeQuoteMin: '15000',
  targetVolumeQuoteMax: '18000',
  roundTurnoverQuoteMin: '540',
  roundTurnoverQuoteMax: '900',
  positionHoldMinSeconds: 600,
  positionHoldMaxSeconds: 1200,
}

export const mockStrategies: VolumeStrategy[] = [standardStrategy, compactStrategy, overnightStrategy]

function strategyProgress(
  generated: string,
  stage: AccountInstance['strategyProgress']['stage'] = 'idle',
  nextSeconds: number | null = null,
): AccountInstance['strategyProgress'] {
  return {
    generatedVolumeQuote: generated,
    startedAtMs: Date.now() - 3_600_000,
    stage,
    nextActionAtMs: nextSeconds === null ? null : Date.now() + nextSeconds * 1000,
    activeCycleId: stage === 'holding' ? `mock-active-${generated}` : null,
    lastEthRatio: '0.4426045637',
    lastAllocationVersion: 'beta-v1:browser-fixture',
    systemPauseReason: null,
  }
}

function runtimeHealth(
  successAgoMs: number | null,
  durationMs: number | null,
  consecutiveFailures = 0,
  lastErrorType: string | null = null,
): AccountInstance['runtime'] {
  const now = Date.now()
  const failedAt = consecutiveFailures ? now - 12_000 : null
  const succeededAt = successAgoMs === null ? null : now - successAgoMs
  const completedAt = failedAt ?? succeededAt
  return {
    lastPollStartedAtMs: completedAt === null ? null : completedAt - (durationMs ?? 0),
    lastPollSucceededAtMs: succeededAt,
    lastPollFailedAtMs: failedAt,
    lastPollDurationMs: durationMs,
    consecutiveFailures,
    lastErrorType,
    lastStopVerifiedAtMs: null,
  }
}

function idleExecutionLifecycle(): AccountInstance['executionLifecycle'] {
  return {
    state: 'idle', primaryAction: 'start', executionId: null, sessionId: null,
    reasonCode: null, positionCount: 0, regularOrderCount: 0, triggerOrderCount: 0,
    blockingPositions: [], boundaryCheckedAtMs: null,
  }
}

export const mockAccounts: AccountInstance[] = [
  {
    id: 'ins-01', name: 'Alpha 01', accountTag: '主策略-A', apiKeyTail: '8F2A', mode: 'demo', status: 'running', phase: '等待 BTC Maker 成交',
    proxy: { type: 'https', host: 'proxy.example.com:9341', location: 'US / New York', latencyMs: 86, status: 'healthy' },
    wallet: { equity: 12582.41, available: 10428.18, unrealizedPnl: 18.42 },
    volume: { lifetime: 2847193.28, today: 184206.14, complete: true },
    exposure: { btcLong: 1184.2, ethShort: 1129.7 }, cycle: { completed: 42, target: 100, nextActionAt: '8s' }, runtime: runtimeHealth(900, 86), updatedAt: '刚刚', unreadLogs: 3,
    executionLifecycle: idleExecutionLifecycle(), strategyId: standardStrategy.id, strategy: standardStrategy, strategyProgress: strategyProgress('12780', 'holding', 480),
  },
  {
    id: 'ins-02', name: 'Alpha 02', accountTag: '主策略-B', apiKeyTail: '19C7', mode: 'demo', status: 'running', phase: 'ETH 空单已挂出',
    proxy: { type: 'socks5', host: 'proxy.example.com:1080', location: 'DE / Frankfurt', latencyMs: 113, status: 'healthy' },
    wallet: { equity: 11942.06, available: 9817.55, unrealizedPnl: -7.81 },
    volume: { lifetime: 2394810.92, today: 176882.31, complete: true },
    exposure: { btcLong: 1102.4, ethShort: 1078.1 }, cycle: { completed: 38, target: 100, nextActionAt: '14s' }, runtime: runtimeHealth(2_000, 113), updatedAt: '2 秒前', unreadLogs: 0,
    executionLifecycle: idleExecutionLifecycle(), strategyId: standardStrategy.id, strategy: standardStrategy, strategyProgress: strategyProgress('9400', 'cooldown', 840),
  },
  {
    id: 'ins-03', name: 'Beta 01', accountTag: '低余额组', apiKeyTail: 'E423', mode: 'demo', status: 'warning', phase: '余额低于预警线',
    proxy: { type: 'https', host: 'proxy.example.com:9428', location: 'SG / Singapore', latencyMs: 221, status: 'degraded' },
    wallet: { equity: 1864.72, available: 824.1, unrealizedPnl: -16.24 },
    volume: { lifetime: 894231.44, today: 38218.04, complete: true },
    exposure: { btcLong: 486.3, ethShort: 451.8 }, cycle: { completed: 11, target: 80, nextActionAt: null }, runtime: runtimeHealth(9_000, 221), updatedAt: '9 秒前', unreadLogs: 5,
    executionLifecycle: idleExecutionLifecycle(), strategyId: compactStrategy.id, strategy: compactStrategy, strategyProgress: strategyProgress('2800'),
  },
  {
    id: 'ins-04', name: 'Beta 02', accountTag: '低余额组', apiKeyTail: '7B0D', mode: 'demo', status: 'paused', phase: '已人工暂停',
    proxy: { type: 'socks5', host: 'proxy.example.com:1080', location: 'GB / London', latencyMs: 96, status: 'healthy' },
    wallet: { equity: 2381.19, available: 2381.19, unrealizedPnl: 0 },
    volume: { lifetime: 1028366.18, today: 0, complete: true },
    exposure: { btcLong: 0, ethShort: 0 }, cycle: { completed: 0, target: 80, nextActionAt: null }, runtime: runtimeHealth(180_000, 96), updatedAt: '3 分钟前', unreadLogs: 1,
    executionLifecycle: idleExecutionLifecycle(), strategyId: compactStrategy.id, strategy: compactStrategy, strategyProgress: strategyProgress('0'),
  },
  {
    id: 'ins-05', name: 'Gamma 01', accountTag: '验证组', apiKeyTail: 'DC81', mode: 'demo', status: 'error', phase: '代理认证失败',
    proxy: { type: 'https', host: 'proxy.example.com:9140', location: 'NL / Amsterdam', latencyMs: null, status: 'unchecked' },
    wallet: { equity: 0, available: 0, unrealizedPnl: 0 },
    volume: { lifetime: 0, today: 0, complete: false },
    exposure: { btcLong: 0, ethShort: 0 }, cycle: { completed: 0, target: 60, nextActionAt: null }, runtime: runtimeHealth(720_000, 408, 3, 'ProxyAuthenticationError'), updatedAt: '12 分钟前', unreadLogs: 8,
    executionLifecycle: idleExecutionLifecycle(), strategyId: compactStrategy.id, strategy: compactStrategy, strategyProgress: strategyProgress('0'),
  },
  {
    id: 'ins-06', name: 'Gamma 02', accountTag: '验证组', apiKeyTail: '5A11', mode: 'demo', status: 'stopped', phase: '等待启动',
    proxy: { type: 'https', host: 'proxy.example.com:9217', location: 'JP / Tokyo', latencyMs: 74, status: 'healthy' },
    wallet: { equity: 6240.3, available: 6240.3, unrealizedPnl: 0 },
    volume: { lifetime: 734110.62, today: 0, complete: true },
    exposure: { btcLong: 0, ethShort: 0 }, cycle: { completed: 0, target: 60, nextActionAt: null }, runtime: runtimeHealth(1_080_000, 74), updatedAt: '18 分钟前', unreadLogs: 0,
    executionLifecycle: idleExecutionLifecycle(), strategyId: standardStrategy.id, strategy: standardStrategy, strategyProgress: strategyProgress('0'),
  },
  {
    id: 'ins-07', name: 'Delta 01', accountTag: '夜间组', apiKeyTail: 'A66E', mode: 'demo', status: 'running', phase: '校验双边仓位',
    proxy: { type: 'socks5', host: 'proxy.example.com:1080', location: 'CA / Toronto', latencyMs: 128, status: 'healthy' },
    wallet: { equity: 8932.77, available: 7229.48, unrealizedPnl: 4.16 },
    volume: { lifetime: 1489238.72, today: 120482.77, complete: true },
    exposure: { btcLong: 920.2, ethShort: 893.4 }, cycle: { completed: 28, target: 90, nextActionAt: '3s' }, runtime: runtimeHealth(600, 128), updatedAt: '刚刚', unreadLogs: 2,
    executionLifecycle: idleExecutionLifecycle(), strategyId: overnightStrategy.id, strategy: overnightStrategy, strategyProgress: strategyProgress('8300', 'holding', 180),
  },
  {
    id: 'ins-08', name: 'Delta 02', accountTag: '夜间组', apiKeyTail: '42BB', mode: 'demo', status: 'running', phase: 'BTC 多单已挂出',
    proxy: { type: 'https', host: 'proxy.example.com:9032', location: 'FR / Paris', latencyMs: 104, status: 'healthy' },
    wallet: { equity: 9101.52, available: 7410.13, unrealizedPnl: 9.08 },
    volume: { lifetime: 1538188.35, today: 127940.11, complete: true },
    exposure: { btcLong: 948.8, ethShort: 907.6 }, cycle: { completed: 31, target: 90, nextActionAt: '11s' }, runtime: runtimeHealth(1_000, 104), updatedAt: '1 秒前', unreadLogs: 0,
    executionLifecycle: idleExecutionLifecycle(), strategyId: overnightStrategy.id, strategy: overnightStrategy, strategyProgress: strategyProgress('9100', 'cooldown', 660),
  },
]
