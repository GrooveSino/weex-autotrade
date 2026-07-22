import {
  Aptos,
  AptosApiType,
  AptosConfig,
  Network,
  generateUserTransactionHash,
  type Ed25519Account,
  type AccountAuthenticator,
  type InputGenerateTransactionOptions,
  type SimpleTransaction,
  type UserTransactionResponse,
} from '@aptos-labs/ts-sdk'
import { ASSETS, type AssetBalance, type AssetId } from '../shared/types.js'
import { formatAmount } from '../shared/amounts.js'
import type { AppConfig } from './config.js'

const MAINNET_READ_MAX_CONCURRENCY = 2
// Keep anonymous Fullnode traffic well below its public quota while allowing
// a normal multi-step preview to complete without a long serial queue.
const MAINNET_READ_MIN_INTERVAL_MS = 350
const MAINNET_READ_WINDOW_MS = 60_000
const MAINNET_READ_WINDOW_LIMIT = 180
const MAINNET_RATE_LIMIT_COOLDOWN_MS = 2_000
const ACCOUNT_EXISTS_CACHE_TTL_MS = 5 * 60_000
const USDT_METADATA_CACHE_TTL_MS = 6 * 60 * 60_000
// Building and simulating a preview are read-only operations. Give a transient
// Fullnode connection drop one extra bounded retry, but never apply this policy
// to transaction submission.
const PREVIEW_NETWORK_ATTEMPTS = 3
const APTOS_COIN_TYPE = '0x1::aptos_coin::AptosCoin'
export type ReadPriority = 'normal' | 'high'

interface FungibleMetadata {
  name: string
  symbol: string
  decimals: number
}

interface QueuedRead<T> {
  action: () => Promise<T>
  resolve: (value: T) => void
  reject: (error: unknown) => void
  priority: ReadPriority
}

/** Keeps all fullnode/indexer calls under one conservative per-process budget. */
class MainnetReadLimiter {
  private readonly queue: Array<QueuedRead<unknown>> = []
  private readonly starts: number[] = []
  private readonly rateLimitEvents: number[] = []
  private active = 0
  private lastStartAt = 0
  private cooldownUntil = 0
  private pumping = false

  run<T>(action: () => Promise<T>, priority: ReadPriority = 'normal'): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      const queued = { action, resolve: resolve as (value: unknown) => void, reject, priority }
      if (priority === 'high') {
        const firstNormal = this.queue.findIndex((item) => item.priority === 'normal')
        this.queue.splice(firstNormal < 0 ? this.queue.length : firstNormal, 0, queued)
      } else {
        this.queue.push(queued)
      }
      void this.pump()
    })
  }

  private async pump(): Promise<void> {
    if (this.pumping) return
    this.pumping = true
    try {
      while (this.queue.length && this.active < MAINNET_READ_MAX_CONCURRENCY) {
        const delay = this.nextDelay()
        if (delay > 0) {
          await sleep(delay)
          continue
        }
        const task = this.queue.shift()!
        const now = Date.now()
        this.starts.push(now)
        this.lastStartAt = now
        this.active += 1
        void task.action()
          .then((value) => task.resolve(value))
          .catch((error) => {
            if (isRateLimited(error)) this.noteRateLimit(error)
            task.reject(error)
          })
          .finally(() => {
            this.active -= 1
            void this.pump()
          })
      }
    } finally {
      this.pumping = false
    }
  }

  private noteRateLimit(error: unknown): void {
    const now = Date.now()
    while (this.rateLimitEvents[0] !== undefined && this.rateLimitEvents[0] <= now - MAINNET_READ_WINDOW_MS) this.rateLimitEvents.shift()
    this.rateLimitEvents.push(now)
    const localBackoff = Math.min(60_000, MAINNET_RATE_LIMIT_COOLDOWN_MS * (2 ** Math.min(5, this.rateLimitEvents.length - 1)))
    const serverBackoff = getRetryAfterMs(error, 0)
    this.cooldownUntil = Math.max(this.cooldownUntil, now + Math.max(localBackoff, serverBackoff))
  }

  private nextDelay(): number {
    const now = Date.now()
    while (this.starts[0] !== undefined && this.starts[0] <= now - MAINNET_READ_WINDOW_MS) this.starts.shift()
    if (this.cooldownUntil > now) return this.cooldownUntil - now
    if (this.starts.length >= MAINNET_READ_WINDOW_LIMIT) return this.starts[0] + MAINNET_READ_WINDOW_MS - now
    return Math.max(0, this.lastStartAt + MAINNET_READ_MIN_INTERVAL_MS - now)
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms))
}

interface AptBalanceRead {
  balance: bigint
  accountExists: boolean
}

class RateLimitedHttpError extends Error {
  readonly status = 429
  readonly retryAfterMs: number | undefined

  constructor(retryAfterMs: number | undefined) {
    super('Aptos 主网公共节点当前限流（HTTP 429）')
    this.name = 'RateLimitedHttpError'
    this.retryAfterMs = retryAfterMs
  }
}

export interface TransferRequest {
  sender: Ed25519Account
  feePayer: Ed25519Account | null
  recipient: string
  asset: AssetId
  amount: bigint
  availableGasBalance?: bigint
}

export interface PreparedTransfer {
  txHash: string
  sequenceNumber: string
  gasUnitPrice: bigint
  maxGasAmount: bigint
  submit(): Promise<string>
  wait(): Promise<{ success: boolean; vmStatus: string; gasFeeBaseUnits: string }>
}

export interface ChainTransferCandidate {
  transactionVersion: string
  eventIndex: number
  direction: 'in' | 'out'
  counterpartyAddress: string
  asset: AssetId
  amountBaseUnits: string
  chainTimestamp: string
  gasFeeBaseUnits: string | null
}

export interface ChainTransferPage {
  records: ChainTransferCandidate[]
  hasMore: boolean
  nextBeforeVersion: string | null
}

export interface ChainGateway {
  getBalances(address: string, priority?: ReadPriority): Promise<AssetBalance[]>
  getBalance(address: string, asset: AssetId, priority?: ReadPriority): Promise<bigint>
  accountExists(address: string, priority?: ReadPriority): Promise<boolean>
  validateUsdt(): Promise<void>
  estimateGas(request: TransferRequest): Promise<{ gasUnitPrice: bigint; maxGasAmount: bigint }>
  prepareTransfer(request: TransferRequest): Promise<PreparedTransfer>
  findTransaction(hash: string): Promise<{ found: boolean; success?: boolean; vmStatus?: string; gasFeeBaseUnits?: string }>
  getAccountTransferHistory(address: string, beforeVersion?: string | null, limit?: number): Promise<ChainTransferPage>
  getTransactionHashByVersion(version: string): Promise<string>
}

interface RawFungibleActivity {
  amount?: string | number | null
  asset_type?: string | null
  type?: string | null
  owner_address?: string | null
  is_gas_fee?: boolean | null
  is_transaction_success?: boolean | null
  transaction_timestamp?: string | null
  event_index?: string | number | null
}

interface RawAccountTransaction {
  transaction_version: string | number
  user_transaction?: { sender?: string | null; timestamp?: string | null } | null
  fungible_asset_activities?: RawFungibleActivity[]
}

function sameAddress(left: string | null | undefined, right: string): boolean {
  return Boolean(left && left.toLowerCase() === right.toLowerCase())
}

function parseChainTransferRow(row: RawAccountTransaction, address: string): ChainTransferCandidate[] {
  const activities = row.fungible_asset_activities ?? []
  const transferActivities = activities.filter((activity) => {
    const asset = activity.asset_type
    const type = activity.type ?? ''
    return activity.is_gas_fee !== true
      && activity.is_transaction_success !== false
      && (asset === APTOS_COIN_TYPE || asset === ASSETS.USDT.metadataAddress)
      && (type.includes('Withdraw') || type.includes('Deposit'))
  })
  const gasFee = activities
    .filter((activity) => activity.is_gas_fee === true && sameAddress(activity.owner_address, address))
    .reduce((sum, activity) => sum + BigInt(String(activity.amount ?? '0')), 0n)
  const sender = row.user_transaction?.sender ?? address
  const timestamp = row.user_transaction?.timestamp ?? transferActivities[0]?.transaction_timestamp ?? new Date().toISOString()
  const records: ChainTransferCandidate[] = []
  for (const activity of transferActivities.filter((candidate) => sameAddress(candidate.owner_address, address))) {
    const amount = BigInt(String(activity.amount ?? '0'))
    const isWithdraw = (activity.type ?? '').includes('Withdraw')
    const direction = isWithdraw ? 'out' : 'in'
    const counterpart = transferActivities
      .filter((candidate) => !sameAddress(candidate.owner_address, address)
        && candidate.asset_type === activity.asset_type
        && BigInt(String(candidate.amount ?? '0')) === amount
        && ((candidate.type ?? '').includes('Deposit') === isWithdraw))
      .sort((left, right) => Math.abs(Number(left.event_index ?? 0) - Number(activity.event_index ?? 0)) - Math.abs(Number(right.event_index ?? 0) - Number(activity.event_index ?? 0)))[0]
    if (!counterpart?.owner_address) continue
    records.push({
      transactionVersion: String(row.transaction_version),
      eventIndex: Number(activity.event_index ?? 0),
      direction,
      counterpartyAddress: counterpart.owner_address || sender,
      asset: activity.asset_type === ASSETS.USDT.metadataAddress ? 'USDT' : 'APT',
      amountBaseUnits: amount.toString(),
      chainTimestamp: timestamp,
      gasFeeBaseUnits: gasFee > 0n && direction === 'out' ? gasFee.toString() : null,
    })
  }
  return records
}

export class AptosMainnetGateway implements ChainGateway {
  readonly aptos: Aptos
  private readonly readLimiter = new MainnetReadLimiter()
  private readonly accountExistsCache = new Map<string, { value: boolean; expiresAt: number }>()
  private usdtValidatedUntil = 0
  private usdtValidationInFlight: Promise<void> | null = null

  constructor(config: AppConfig) {
    this.aptos = new Aptos(new AptosConfig({
      network: Network.MAINNET,
      fullnode: config.fullnodeUrl,
      indexer: config.indexerUrl,
    }))
  }

  async getBalances(address: string, priority: ReadPriority = 'normal'): Promise<AssetBalance[]> {
    try {
      const aptRead = await retryRead(() => this.readLimiter.run(() => this.getUnifiedAptBalance(address), priority))
      if (aptRead.accountExists) this.accountExistsCache.set(address, { value: true, expiresAt: Date.now() + ACCOUNT_EXISTS_CACHE_TTL_MS })
      const usdt = await this.getBalance(address, 'USDT', priority)
      return [
        { asset: 'APT', baseUnits: aptRead.balance.toString(), display: formatAmount(aptRead.balance, 'APT') },
        { asset: 'USDT', baseUnits: usdt.toString(), display: formatAmount(usdt, 'USDT') },
      ]
    } catch (error) {
      throw redactChainError(error)
    }
  }

  async getBalance(address: string, asset: AssetId, priority: ReadPriority = 'normal'): Promise<bigint> {
    try {
      if (asset === 'APT') {
        const result = await retryRead(() => this.readLimiter.run(() => this.getUnifiedAptBalance(address), priority))
        if (result.accountExists) this.accountExistsCache.set(address, { value: true, expiresAt: Date.now() + ACCOUNT_EXISTS_CACHE_TTL_MS })
        return result.balance
      }
      const [balance] = await retryRead(() => this.readLimiter.run(() => this.aptos.view<[string]>({
        payload: {
          function: '0x1::primary_fungible_store::balance',
          typeArguments: ['0x1::object::ObjectCore'],
          functionArguments: [address, ASSETS.USDT.metadataAddress],
        },
      }), priority))
      return BigInt(balance ?? '0')
    } catch (error) {
      if (isNotFound(error)) return 0n
      throw redactChainError(error)
    }
  }

  private async getUnifiedAptBalance(address: string): Promise<AptBalanceRead> {
    const fullnode = this.aptos.config.getRequestUrl(AptosApiType.FULLNODE).replace(/\/$/, '')
    const asset = encodeURIComponent('0x1::aptos_coin::AptosCoin')
    const response = await fetch(`${fullnode}/accounts/${encodeURIComponent(address)}/balance/${asset}`, {
      headers: { accept: 'application/json' },
    })
    if (response.status === 404) return { balance: 0n, accountExists: false }
    if (response.status === 429) throw new RateLimitedHttpError(parseRetryAfter(response.headers.get('retry-after')))
    if (!response.ok) throw new Error(`APT 余额查询失败 (HTTP ${response.status})`)
    const raw = (await response.text()).trim()
    const match = /^"?(0|[1-9]\d*)"?$/.exec(raw)
    if (!match) throw new Error('APT 余额响应格式无效')
    return { balance: BigInt(match[1]), accountExists: true }
  }

  async accountExists(address: string, priority: ReadPriority = 'normal'): Promise<boolean> {
    const cached = this.accountExistsCache.get(address)
    if (cached && cached.expiresAt > Date.now()) return cached.value
    try {
      await retryRead(() => this.readLimiter.run(() => this.aptos.getAccountInfo({ accountAddress: address }), priority))
      this.accountExistsCache.set(address, { value: true, expiresAt: Date.now() + ACCOUNT_EXISTS_CACHE_TTL_MS })
      return true
    } catch (error) {
      if (isNotFound(error)) {
        this.accountExistsCache.set(address, { value: false, expiresAt: Date.now() + ACCOUNT_EXISTS_CACHE_TTL_MS })
        return false
      }
      throw redactChainError(error)
    }
  }

  async validateUsdt(): Promise<void> {
    if (this.usdtValidatedUntil > Date.now()) return
    if (this.usdtValidationInFlight) return this.usdtValidationInFlight
    const validation = (async () => {
      const metadata = await retryRead(() => this.readLimiter.run(() => this.aptos.getAccountResource<FungibleMetadata>({
        accountAddress: ASSETS.USDT.metadataAddress,
        resourceType: '0x1::fungible_asset::Metadata',
      }), 'high'), PREVIEW_NETWORK_ATTEMPTS)
      if (metadata.name !== 'Tether USD' || metadata.symbol !== 'USDt' || Number(metadata.decimals) !== 6) {
        throw new Error('原生 USDt 元数据校验失败，已停止执行')
      }
      this.usdtValidatedUntil = Date.now() + USDT_METADATA_CACHE_TTL_MS
    })()
    this.usdtValidationInFlight = validation
    try {
      await validation
    } catch (error) {
      throw redactChainError(error)
    } finally {
      this.usdtValidationInFlight = null
    }
  }

  async estimateGas(request: TransferRequest): Promise<{ gasUnitPrice: bigint; maxGasAmount: bigint }> {
    try {
      const { simulation } = await this.buildAndSimulate(request)
      if (!simulation.success) throw new Error(`交易模拟失败：${simulation.vm_status}`)
      const gasUsed = BigInt(simulation.gas_used)
      const maxGasAmount = (gasUsed * 125n + 99n) / 100n
      return {
        gasUnitPrice: BigInt(simulation.gas_unit_price),
        maxGasAmount,
      }
    } finally {
      request.sender.privateKey.clear()
      request.feePayer?.privateKey.clear()
    }
  }

  async prepareTransfer(request: TransferRequest): Promise<PreparedTransfer> {
    try {
      const first = await this.buildAndSimulate(request)
      if (!first.simulation.success) {
        throw new Error(`交易模拟失败：${first.simulation.vm_status}`)
      }
      const gasUsed = BigInt(first.simulation.gas_used)
      const maxGasAmount = (gasUsed * 125n + 99n) / 100n
      const gasUnitPrice = BigInt(first.simulation.gas_unit_price)
      const sequenceNumber = first.transaction.rawTransaction.sequence_number.toString()
      const transaction = await this.buildForPreview(request, {
        accountSequenceNumber: BigInt(sequenceNumber),
        gasUnitPrice: Number(gasUnitPrice),
        maxGasAmount: Number(maxGasAmount),
      })
      const senderAuthenticator = this.aptos.transaction.sign({ signer: request.sender, transaction })
      const feePayerAuthenticator = request.feePayer
        ? this.aptos.transaction.signAsFeePayer({ signer: request.feePayer, transaction })
        : undefined
      const txHash = generateUserTransactionHash({ transaction, senderAuthenticator, feePayerAuthenticator })
      let submittedHash: string | null = null
      return {
        txHash,
        sequenceNumber,
        gasUnitPrice,
        maxGasAmount,
        submit: async () => {
          // Submissions are intentionally queued but never retried. This keeps
          // them from colliding with balance refreshes while preserving the
          // one-submit-only safety rule.
          const pending = await this.readLimiter.run(() => this.aptos.transaction.submit.simple({ transaction, senderAuthenticator, feePayerAuthenticator }), 'high')
          submittedHash = pending.hash
          if (pending.hash !== txHash) throw new Error('节点返回的交易哈希与本地签名哈希不一致')
          return pending.hash
        },
        wait: async () => {
          const response = await this.waitForFinalizedTransaction(submittedHash ?? txHash)
          return {
            success: response.success,
            vmStatus: response.vm_status,
            gasFeeBaseUnits: (BigInt(response.gas_used) * BigInt(response.gas_unit_price)).toString(),
          }
        },
      }
    } finally {
      request.sender.privateKey.clear()
      request.feePayer?.privateKey.clear()
    }
  }

  async findTransaction(hash: string): Promise<{ found: boolean; success?: boolean; vmStatus?: string; gasFeeBaseUnits?: string }> {
    try {
      const response = await retryRead(() => this.readLimiter.run(() => this.aptos.getTransactionByHash({ transactionHash: hash }), 'high'))
      if (response.type === 'pending_transaction') return { found: true }
      const finalized = response as UserTransactionResponse
      return {
        found: true,
        success: finalized.success,
        vmStatus: finalized.vm_status,
        gasFeeBaseUnits: (BigInt(finalized.gas_used) * BigInt(finalized.gas_unit_price)).toString(),
      }
    } catch (error) {
      if (isNotFound(error)) return { found: false }
      throw redactChainError(error)
    }
  }

  private async waitForFinalizedTransaction(transactionHash: string): Promise<UserTransactionResponse> {
    for (let attempt = 0; attempt < 45; attempt += 1) {
      try {
        const response = await retryRead(() => this.readLimiter.run(
          () => this.aptos.getTransactionByHash({ transactionHash }),
          'high',
        ))
        if (response.type !== 'pending_transaction') return response as UserTransactionResponse
      } catch (error) {
        // The fullnode can briefly return 404 before a newly submitted
        // transaction reaches its mempool. Other errors remain actionable.
        if (!isNotFound(error)) throw error
      }
      await sleep(1_000)
    }
    throw new Error('等待链上确认超时')
  }

  async getAccountTransferHistory(address: string, beforeVersion: string | null = null, limit = 5): Promise<ChainTransferPage> {
    const boundedLimit = Math.max(1, Math.min(5, Math.trunc(limit)))
    const versionFilter = beforeVersion ? ', transaction_version: {_lt: $before}' : ''
    const query = `query($address: String!${beforeVersion ? ', $before: bigint' : ''}) {
      account_transactions(
        where: {
          account_address: {_eq: $address}
          fungible_asset_activities: {
            owner_address: {_eq: $address}
            is_gas_fee: {_eq: false}
            is_transaction_success: {_eq: true}
            asset_type: {_in: ["${APTOS_COIN_TYPE}", "${ASSETS.USDT.metadataAddress}"]}
          }${versionFilter}
        }
        order_by: {transaction_version: desc}
        limit: ${boundedLimit + 1}
      ) {
        transaction_version
        user_transaction { sender timestamp }
        fungible_asset_activities {
          amount asset_type type owner_address is_gas_fee is_transaction_success transaction_timestamp event_index
        }
      }
    }`
    try {
      const indexer = this.aptos.config.getRequestUrl(AptosApiType.INDEXER).replace(/\/$/, '')
      const response = await this.readLimiter.run(() => fetch(indexer, {
        method: 'POST',
        headers: { accept: 'application/json', 'content-type': 'application/json' },
        body: JSON.stringify({ query, variables: beforeVersion ? { address, before: beforeVersion } : { address } }),
      }))
      if (response.status === 429) throw new RateLimitedHttpError(parseRetryAfter(response.headers.get('retry-after')))
      if (!response.ok) throw new Error(`Aptos 链上日志查询失败 (HTTP ${response.status})`)
      const body = await response.json() as { errors?: Array<{ message?: string }>; data?: { account_transactions?: RawAccountTransaction[] } }
      if (body.errors?.length) throw new Error(body.errors[0]?.message || 'Aptos 链上日志查询失败')
      const rows = body.data?.account_transactions ?? []
      const records = rows.flatMap((row) => parseChainTransferRow(row, address))
      const selectedVersions = rows.slice(0, boundedLimit).map((row) => String(row.transaction_version))
      return {
        records: records.filter((record) => selectedVersions.includes(record.transactionVersion)),
        hasMore: rows.length > boundedLimit,
        nextBeforeVersion: selectedVersions.at(-1) ?? null,
      }
    } catch (error) {
      throw redactChainError(error)
    }
  }

  async getTransactionHashByVersion(version: string): Promise<string> {
    try {
      const response = await this.readLimiter.run(() => this.aptos.getTransactionByVersion({ ledgerVersion: BigInt(version) })) as { hash?: string }
      if (!response.hash) throw new Error('链上交易缺少哈希')
      return response.hash
    } catch (error) {
      throw redactChainError(error)
    }
  }

  private async buildAndSimulate(request: TransferRequest): Promise<{ transaction: SimpleTransaction; simulation: UserTransactionResponse }> {
    let transaction = await this.buildForPreview(request)
    const gasUnitPrice = transaction.rawTransaction.gas_unit_price
    if (gasUnitPrice <= 0n) throw new Error('交易 Gas 单价无效')
    const gasBalance = request.availableGasBalance ?? await this.getBalance(
      (request.feePayer ?? request.sender).accountAddress.toString(),
      'APT',
      'high',
    )
    const spendableForGas = !request.feePayer && request.asset === 'APT'
      ? gasBalance - request.amount
      : gasBalance
    const affordableGasUnits = spendableForGas > 0n ? spendableForGas / gasUnitPrice : 0n
    if (affordableGasUnits <= 0n) throw new Error('INSUFFICIENT_BALANCE_FOR_TRANSACTION_FEE')

    // Aptos SDK defaults to a 2,000,000-unit cap. Validation requires the fee
    // payer to cover that whole cap before simulation, even when actual gas is
    // tiny, so use the largest cap the selected payer can currently afford.
    if (affordableGasUnits < transaction.rawTransaction.max_gas_amount) {
      transaction = await this.buildForPreview(request, {
        accountSequenceNumber: transaction.rawTransaction.sequence_number,
        gasUnitPrice: Number(gasUnitPrice),
        maxGasAmount: Number(affordableGasUnits),
      })
    }
    const [simulation] = await retryRead(() => this.readLimiter.run(() => this.aptos.transaction.simulate.simple({
      signerPublicKey: request.sender.publicKey,
      feePayerPublicKey: request.feePayer?.publicKey,
      transaction,
      options: { estimateGasUnitPrice: true, estimateMaxGasAmount: false },
    }), 'high'), PREVIEW_NETWORK_ATTEMPTS)
    return { transaction, simulation }
  }

  private buildForPreview(
    request: TransferRequest,
    options?: { accountSequenceNumber: bigint; gasUnitPrice: number; maxGasAmount: number },
  ): Promise<SimpleTransaction> {
    return retryRead(() => this.readLimiter.run(() => this.build(request, options), 'high'), PREVIEW_NETWORK_ATTEMPTS)
  }

  private async build(
    request: TransferRequest,
    options?: { accountSequenceNumber: bigint; gasUnitPrice: number; maxGasAmount: number },
  ): Promise<SimpleTransaction> {
    const buildOptions: InputGenerateTransactionOptions | undefined = options
    const common = {
      sender: request.sender.accountAddress,
      withFeePayer: Boolean(request.feePayer),
      options: buildOptions,
    }
    const transaction = request.asset === 'APT'
      ? await this.aptos.transaction.build.simple({
        ...common,
        data: {
          function: '0x1::aptos_account::transfer',
          functionArguments: [request.recipient, request.amount.toString()],
        },
      })
      : await this.aptos.transaction.build.simple({
        ...common,
        data: {
          function: '0x1::primary_fungible_store::transfer',
          typeArguments: ['0x1::object::ObjectCore'],
          functionArguments: [ASSETS.USDT.metadataAddress, request.recipient, request.amount.toString()],
        },
      })

    // The SDK initially uses 0x0 as the fee-payer placeholder. Bind the real
    // sponsor before simulation and before either party signs the transaction.
    if (request.feePayer) transaction.feePayerAddress = request.feePayer.accountAddress
    return transaction
  }
}

function isNotFound(error: unknown): boolean {
  const candidate = error as { status?: number; statusCode?: number; response?: { status?: number }; message?: string }
  return candidate?.status === 404 || candidate?.statusCode === 404 || candidate?.response?.status === 404 || candidate?.message?.includes('404') === true
}

function redactChainError(error: unknown): Error {
  if (isRateLimited(error)) return new Error('主网公共节点暂时限流，程序已自动降速并退避，请稍后重试')
  if (isTransientReadFailure(error)) return new Error('Aptos 主网连接暂时中断，已自动重试仍未恢复；请稍后重新预览。')
  const message = error instanceof Error ? error.message : String(error)
  return new Error(message
    .replace(/ed25519-priv-[^\s"']+/gi, '[REDACTED]')
    .replace(/\b0x[0-9a-f]{64}\b/gi, '[ADDRESS]')
    .slice(0, 500))
}

async function retryRead<T>(action: () => Promise<T>, maxAttempts = 4): Promise<T> {
  let lastError: unknown
  for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
    try {
      return await action()
    } catch (error) {
      lastError = error
      if ((!isRateLimited(error) && !isTransientReadFailure(error)) || attempt === maxAttempts) throw error
      const retryDelay = isRateLimited(error)
        ? Math.max(getRetryAfterMs(error, 1_000), Math.min(10_000, attempt * 1_000) + Math.floor(Math.random() * 250))
        : Math.min(4_000, 500 * (2 ** (attempt - 1)) + Math.floor(Math.random() * 200))
      await sleep(retryDelay)
    }
  }
  throw lastError
}

function isRateLimited(error: unknown): boolean {
  const candidate = error as { status?: number; statusCode?: number; response?: { status?: number }; message?: string }
  return candidate?.status === 429 || candidate?.statusCode === 429 || candidate?.response?.status === 429 || /(?:HTTP 429|Too Many Requests)/i.test(candidate?.message ?? '')
}

function isTransientReadFailure(error: unknown): boolean {
  const candidate = error as { code?: string; cause?: { code?: string; message?: string }; message?: string }
  const code = `${candidate?.code ?? ''} ${candidate?.cause?.code ?? ''}`.toUpperCase()
  const message = `${candidate?.message ?? ''} ${candidate?.cause?.message ?? ''}`.toUpperCase()
  return /(?:ECONNRESET|ECONNREFUSED|ECONNABORTED|ETIMEDOUT|EAI_AGAIN|ENOTFOUND|UND_ERR)/.test(code)
    || /(?:FETCH FAILED|NETWORK ERROR|NETWORK REQUEST FAILED|SOCKET HANG UP|OTHER SIDE CLOSED)/.test(message)
}

function parseRetryAfter(value: string | null): number | undefined {
  if (!value) return undefined
  const seconds = Number(value)
  if (Number.isFinite(seconds) && seconds >= 0) return seconds * 1_000
  const date = Date.parse(value)
  return Number.isFinite(date) ? Math.max(0, date - Date.now()) : undefined
}

function getRetryAfterMs(error: unknown, fallbackMs: number): number {
  const candidate = error as { retryAfterMs?: number; response?: { headers?: Headers } }
  if (typeof candidate?.retryAfterMs === 'number' && Number.isFinite(candidate.retryAfterMs)) return Math.max(0, candidate.retryAfterMs)
  const header = candidate?.response?.headers?.get?.('retry-after')
  return parseRetryAfter(header ?? null) ?? fallbackMs
}

export type { AccountAuthenticator }
