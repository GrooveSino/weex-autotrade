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
const MAINNET_READ_MIN_INTERVAL_MS = 200
const MAINNET_READ_WINDOW_MS = 60_000
const MAINNET_READ_WINDOW_LIMIT = 600
const MAINNET_RATE_LIMIT_COOLDOWN_MS = 2_000
const ACCOUNT_EXISTS_CACHE_TTL_MS = 5 * 60_000
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

export interface ChainGateway {
  getBalances(address: string, priority?: ReadPriority): Promise<AssetBalance[]>
  getBalance(address: string, asset: AssetId, priority?: ReadPriority): Promise<bigint>
  accountExists(address: string, priority?: ReadPriority): Promise<boolean>
  validateUsdt(): Promise<void>
  estimateGas(request: TransferRequest): Promise<{ gasUnitPrice: bigint; maxGasAmount: bigint }>
  prepareTransfer(request: TransferRequest): Promise<PreparedTransfer>
  findTransaction(hash: string): Promise<{ found: boolean; success?: boolean; vmStatus?: string; gasFeeBaseUnits?: string }>
}

export class AptosMainnetGateway implements ChainGateway {
  readonly aptos: Aptos
  private readonly readLimiter = new MainnetReadLimiter()
  private readonly accountExistsCache = new Map<string, { value: boolean; expiresAt: number }>()

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
    const metadata = await this.readLimiter.run(() => this.aptos.getAccountResource<FungibleMetadata>({
      accountAddress: ASSETS.USDT.metadataAddress,
      resourceType: '0x1::fungible_asset::Metadata',
    }))
    if (metadata.name !== 'Tether USD' || metadata.symbol !== 'USDt' || Number(metadata.decimals) !== 6) {
      throw new Error('原生 USDt 元数据校验失败，已停止执行')
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
      const transaction = await this.build(request, {
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
          const pending = await this.aptos.transaction.submit.simple({ transaction, senderAuthenticator, feePayerAuthenticator })
          submittedHash = pending.hash
          if (pending.hash !== txHash) throw new Error('节点返回的交易哈希与本地签名哈希不一致')
          return pending.hash
        },
        wait: async () => {
          const response = await this.aptos.waitForTransaction({ transactionHash: submittedHash ?? txHash }) as UserTransactionResponse
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
      const response = await this.readLimiter.run(() => this.aptos.getTransactionByHash({ transactionHash: hash }))
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

  private async buildAndSimulate(request: TransferRequest): Promise<{ transaction: SimpleTransaction; simulation: UserTransactionResponse }> {
    let transaction = await this.build(request)
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
      transaction = await this.build(request, {
        accountSequenceNumber: transaction.rawTransaction.sequence_number,
        gasUnitPrice: Number(gasUnitPrice),
        maxGasAmount: Number(affordableGasUnits),
      })
    }
    const [simulation] = await this.readLimiter.run(() => this.aptos.transaction.simulate.simple({
      signerPublicKey: request.sender.publicKey,
      feePayerPublicKey: request.feePayer?.publicKey,
      transaction,
      options: { estimateGasUnitPrice: true, estimateMaxGasAmount: false },
    }))
    return { transaction, simulation }
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
      if (!isRateLimited(error) || attempt === maxAttempts) throw error
      await sleep(Math.max(getRetryAfterMs(error, 1_000), Math.min(10_000, attempt * 1_000) + Math.floor(Math.random() * 250)))
    }
  }
  throw lastError
}

function isRateLimited(error: unknown): boolean {
  const candidate = error as { status?: number; statusCode?: number; response?: { status?: number }; message?: string }
  return candidate?.status === 429 || candidate?.statusCode === 429 || candidate?.response?.status === 429 || /(?:HTTP 429|Too Many Requests)/i.test(candidate?.message ?? '')
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
