import {
  Aptos,
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

interface AptCoinStore {
  coin: { value: string }
}

interface FungibleMetadata {
  name: string
  symbol: string
  decimals: number
}

export interface TransferRequest {
  sender: Ed25519Account
  feePayer: Ed25519Account | null
  recipient: string
  asset: AssetId
  amount: bigint
}

export interface PreparedTransfer {
  txHash: string
  sequenceNumber: string
  gasUnitPrice: bigint
  maxGasAmount: bigint
  submit(): Promise<string>
  wait(): Promise<{ success: boolean; vmStatus: string }>
}

export interface ChainGateway {
  getBalances(address: string): Promise<AssetBalance[]>
  getBalance(address: string, asset: AssetId): Promise<bigint>
  accountExists(address: string): Promise<boolean>
  validateUsdt(): Promise<void>
  estimateGas(request: TransferRequest): Promise<{ gasUnitPrice: bigint; maxGasAmount: bigint }>
  prepareTransfer(request: TransferRequest): Promise<PreparedTransfer>
  findTransaction(hash: string): Promise<{ found: boolean; success?: boolean; vmStatus?: string }>
}

export class AptosMainnetGateway implements ChainGateway {
  readonly aptos: Aptos

  constructor(config: AppConfig) {
    this.aptos = new Aptos(new AptosConfig({
      network: Network.MAINNET,
      fullnode: config.fullnodeUrl,
      indexer: config.indexerUrl,
    }))
  }

  async getBalances(address: string): Promise<AssetBalance[]> {
    const [apt, usdt] = await Promise.all([this.getBalance(address, 'APT'), this.getBalance(address, 'USDT')])
    return [
      { asset: 'APT', baseUnits: apt.toString(), display: formatAmount(apt, 'APT') },
      { asset: 'USDT', baseUnits: usdt.toString(), display: formatAmount(usdt, 'USDT') },
    ]
  }

  async getBalance(address: string, asset: AssetId): Promise<bigint> {
    try {
      if (asset === 'APT') {
        const resource = await this.aptos.getAccountResource<AptCoinStore>({
          accountAddress: address,
          resourceType: '0x1::coin::CoinStore<0x1::aptos_coin::AptosCoin>',
        })
        return BigInt(resource.coin.value)
      }
      const [balance] = await this.aptos.view<[string]>({
        payload: {
          function: '0x1::primary_fungible_store::balance',
          typeArguments: ['0x1::object::ObjectCore'],
          functionArguments: [address, ASSETS.USDT.metadataAddress],
        },
      })
      return BigInt(balance ?? '0')
    } catch (error) {
      if (isNotFound(error)) return 0n
      throw redactChainError(error)
    }
  }

  async accountExists(address: string): Promise<boolean> {
    try {
      await this.aptos.getAccountInfo({ accountAddress: address })
      return true
    } catch (error) {
      if (isNotFound(error)) return false
      throw redactChainError(error)
    }
  }

  async validateUsdt(): Promise<void> {
    const metadata = await this.aptos.getAccountResource<FungibleMetadata>({
      accountAddress: ASSETS.USDT.metadataAddress,
      resourceType: '0x1::fungible_asset::Metadata',
    })
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
    const first = await this.buildAndSimulate(request)
    if (!first.simulation.success) {
      request.sender.privateKey.clear()
      request.feePayer?.privateKey.clear()
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
    request.sender.privateKey.clear()
    request.feePayer?.privateKey.clear()
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
        return { success: response.success, vmStatus: response.vm_status }
      },
    }
  }

  async findTransaction(hash: string): Promise<{ found: boolean; success?: boolean; vmStatus?: string }> {
    try {
      const response = await this.aptos.getTransactionByHash({ transactionHash: hash })
      if (response.type === 'pending_transaction') return { found: true }
      return { found: true, success: response.success, vmStatus: response.vm_status }
    } catch (error) {
      if (isNotFound(error)) return { found: false }
      throw redactChainError(error)
    }
  }

  private async buildAndSimulate(request: TransferRequest): Promise<{ transaction: SimpleTransaction; simulation: UserTransactionResponse }> {
    const transaction = await this.build(request)
    const [simulation] = await this.aptos.transaction.simulate.simple({
      signerPublicKey: request.sender.publicKey,
      feePayerPublicKey: request.feePayer?.publicKey,
      transaction,
      options: { estimateGasUnitPrice: true, estimateMaxGasAmount: true },
    })
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
    if (request.asset === 'APT') {
      return this.aptos.transaction.build.simple({
        ...common,
        data: {
          function: '0x1::aptos_account::transfer',
          functionArguments: [request.recipient, request.amount.toString()],
        },
      })
    }
    return this.aptos.transaction.build.simple({
      ...common,
      data: {
        function: '0x1::primary_fungible_store::transfer',
        typeArguments: ['0x1::object::ObjectCore'],
        functionArguments: [ASSETS.USDT.metadataAddress, request.recipient, request.amount.toString()],
      },
    })
  }
}

function isNotFound(error: unknown): boolean {
  const candidate = error as { status?: number; response?: { status?: number }; message?: string }
  return candidate?.status === 404 || candidate?.response?.status === 404 || candidate?.message?.includes('404') === true
}

function redactChainError(error: unknown): Error {
  const message = error instanceof Error ? error.message : String(error)
  return new Error(message.replace(/ed25519-priv-[^\s"']+/gi, '[REDACTED]').slice(0, 500))
}

export type { AccountAuthenticator }
