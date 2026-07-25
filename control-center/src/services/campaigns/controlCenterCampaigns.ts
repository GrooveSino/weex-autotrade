import type {
  AccountInstance, BetaCampaign, BetaCampaignEvent, BetaCampaignPreview,
  BetaCampaignPreviewRequest, BetaMarketSnapshot, BetaSourceSettings,
  StrategyRunConfirmResponse, StrategyRunPrepareResponse, VolumeStrategy,
} from '../../types'
import { mockStrategies } from '../../data/mockAccounts'
import { apiRequest, controlPlaneEnabled } from '../core/controlCenterCore'

export async function previewBetaCampaign(
  account: AccountInstance,
  request: BetaCampaignPreviewRequest,
): Promise<BetaCampaignPreview> {
  if (!controlPlaneEnabled) throw new Error('Beta Campaign requires the control-plane API')
  return apiRequest<BetaCampaignPreview>(`/instances/${account.id}/beta-campaigns/preview`, {
    method: 'POST',
    body: JSON.stringify(request),
  })
}

export async function previewBoundStrategyExecution(
  account: AccountInstance,
): Promise<BetaCampaignPreview> {
  if (!controlPlaneEnabled) throw new Error('已绑定策略实盘执行需要控制平面 API')
  return apiRequest<BetaCampaignPreview>(`/instances/${account.id}/strategy-executions/preview`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export async function prepareBoundStrategyRun(
  account: AccountInstance,
): Promise<StrategyRunPrepareResponse> {
  if (!controlPlaneEnabled) throw new Error('已绑定策略实盘执行需要控制平面 API')
  return apiRequest<StrategyRunPrepareResponse>(`/instances/${account.id}/strategy-run/prepare`, {
    method: 'POST',
    body: JSON.stringify({}),
  })
}

export async function cleanupBoundStrategyRun(
  account: AccountInstance,
  confirmation: string,
): Promise<StrategyRunPrepareResponse> {
  const commandId = crypto.randomUUID()
  return apiRequest<StrategyRunPrepareResponse>(`/instances/${account.id}/strategy-run/cleanup`, {
    method: 'POST',
    body: JSON.stringify({ confirmation, commandId }),
    headers: { 'X-Fleet-Command-Id': commandId },
  })
}

export async function listBoundStrategyExecutions(account: Pick<AccountInstance, 'id'>): Promise<BetaCampaign[]> {
  return apiRequest<BetaCampaign[]>(`/instances/${account.id}/strategy-executions`)
}

export async function executeBoundStrategyExecution(
  account: AccountInstance,
  executionId: string,
  confirmation: string,
  riskAcknowledged: boolean,
  commandId?: string,
): Promise<BetaCampaign> {
  return apiRequest<BetaCampaign>(`/instances/${account.id}/strategy-executions/${executionId}/execute`, {
    method: 'POST',
    body: JSON.stringify({ confirmation, riskAcknowledged }),
    headers: commandId ? { 'X-Fleet-Command-Id': commandId } : undefined,
  })
}

export async function confirmBoundStrategyRun(
  account: AccountInstance,
  executionId: string,
  confirmation: string,
  riskAcknowledged: boolean,
  commandId?: string,
): Promise<StrategyRunConfirmResponse> {
  return apiRequest<StrategyRunConfirmResponse>(`/instances/${account.id}/strategy-run/confirm`, {
    method: 'POST',
    body: JSON.stringify({ executionId, confirmation, riskAcknowledged }),
    headers: commandId ? { 'X-Fleet-Command-Id': commandId } : undefined,
  })
}

export async function stopBoundStrategyExecution(
  account: AccountInstance,
  execution: BetaCampaign,
  confirmation: string,
): Promise<BetaCampaign> {
  return apiRequest<BetaCampaign>(`/instances/${account.id}/strategy-executions/${execution.campaignId}/stop`, {
    method: 'POST',
    body: JSON.stringify({ confirmation }),
  })
}

export async function executeBetaCampaign(
  account: AccountInstance,
  campaignId: string,
  confirmation: string,
  riskAcknowledged: boolean,
): Promise<BetaCampaign> {
  return apiRequest<BetaCampaign>(`/instances/${account.id}/beta-campaigns/${campaignId}/execute`, {
    method: 'POST',
    body: JSON.stringify({ confirmation, riskAcknowledged }),
  })
}

export async function stopBetaCampaign(
  account: AccountInstance,
  campaign: BetaCampaign,
  confirmation: string,
): Promise<BetaCampaign> {
  return apiRequest<BetaCampaign>(`/instances/${account.id}/beta-campaigns/${campaign.campaignId}/stop`, {
    method: 'POST',
    body: JSON.stringify({ confirmation }),
  })
}

export async function fetchBetaCampaignEvents(account: AccountInstance, campaignId: string): Promise<BetaCampaignEvent[]> {
  return apiRequest<BetaCampaignEvent[]>(`/instances/${account.id}/beta-campaigns/${campaignId}/events`)
}

export async function listVolumeStrategies(): Promise<VolumeStrategy[]> {
  if (!controlPlaneEnabled) return mockStrategies.map((strategy) => ({ ...strategy }))
  return apiRequest<VolumeStrategy[]>('/strategies')
}

export async function fetchBetaMarketSnapshot(): Promise<BetaMarketSnapshot> {
  if (controlPlaneEnabled) return apiRequest<BetaMarketSnapshot>('/beta')
  const now = Date.now()
  return {
    schemaVersion: '1.0',
    strategy: 'btc_long_eth_short',
    status: 'ok',
    upstreamUsable: true,
    reasonCodes: [],
    finalBeta: '0.44260456370165036',
    btcLongRatio: '1.0',
    ethShortRatio: '0.44260456370165036',
    btcLongWeight: '0.6931906533236318',
    ethShortWeight: '0.30680934667636806',
    confidence: '0.6793400100124344',
    confidenceThreshold: '0.65',
    source: 'beta_v2',
    asOfMs: now - 324,
    generatedAtMs: now,
    ageMs: '324',
    maxAgeMs: '10000',
  }
}

export async function fetchBetaSourceSettings(): Promise<BetaSourceSettings> {
  if (!controlPlaneEnabled) throw new Error('Beta 来源设置需要控制平面 API')
  return apiRequest<BetaSourceSettings>('/beta/source')
}

export async function updateBetaSourceSettings(settings: Omit<BetaSourceSettings, 'updatedAtMs'>): Promise<BetaSourceSettings> {
  if (!controlPlaneEnabled) throw new Error('Beta 来源设置需要控制平面 API')
  return apiRequest<BetaSourceSettings>('/beta/source', {
    method: 'PATCH',
    body: JSON.stringify(settings),
  })
}
