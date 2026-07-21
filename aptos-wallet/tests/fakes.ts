import { createHash } from 'node:crypto'
import type { AssetBalance, AssetId } from '../shared/types.js'
import { formatAmount } from '../shared/amounts.js'
import type { ChainGateway, PreparedTransfer, TransferRequest } from '../server/aptos-gateway.js'

export class FakeGateway implements ChainGateway {
  balances = new Map<string, bigint>()
  existingAccounts = new Set<string>()
  accountErrors = new Set<string>()
  submissions = 0
  submitError = false
  commitBeforeError = false
  validationError = false
  estimateError: string | null = null
  private outcomes = new Map<string, { success: boolean; vmStatus: string }>()

  setBalance(address: string, asset: AssetId, value: bigint): void {
    this.balances.set(`${address}:${asset}`, value)
    if (value > 0n) this.existingAccounts.add(address)
  }

  async getBalances(address: string): Promise<AssetBalance[]> {
    return (['APT', 'USDT'] as const).map((asset) => {
      const value = this.balances.get(`${address}:${asset}`) ?? 0n
      return { asset, baseUnits: value.toString(), display: formatAmount(value, asset) }
    })
  }

  async getBalance(address: string, asset: AssetId): Promise<bigint> {
    return this.balances.get(`${address}:${asset}`) ?? 0n
  }

  async accountExists(address: string): Promise<boolean> {
    if (this.accountErrors.has(address)) throw new Error('account lookup failed')
    return this.existingAccounts.has(address)
  }

  async validateUsdt(): Promise<void> {
    if (this.validationError) throw new Error('USDt metadata invalid')
  }

  async estimateGas(request: TransferRequest): Promise<{ gasUnitPrice: bigint; maxGasAmount: bigint }> {
    request.sender.privateKey.clear()
    request.feePayer?.privateKey.clear()
    if (this.estimateError) throw new Error(this.estimateError)
    return { gasUnitPrice: 1n, maxGasAmount: 10n }
  }

  async prepareTransfer(request: TransferRequest): Promise<PreparedTransfer> {
    const sender = request.sender.accountAddress.toStringLong()
    const hash = `0x${createHash('sha256').update(`${sender}:${request.recipient}:${request.asset}:${request.amount}:${this.submissions}`).digest('hex')}`
    const feePayer = request.feePayer?.accountAddress.toStringLong() ?? null
    request.sender.privateKey.clear()
    request.feePayer?.privateKey.clear()
    const commit = () => {
      const sourceKey = `${sender}:${request.asset}`
      this.balances.set(sourceKey, (this.balances.get(sourceKey) ?? 0n) - request.amount)
      const targetKey = `${request.recipient}:${request.asset}`
      this.balances.set(targetKey, (this.balances.get(targetKey) ?? 0n) + request.amount)
      const gasKey = `${feePayer ?? sender}:APT`
      this.balances.set(gasKey, (this.balances.get(gasKey) ?? 0n) - 10n)
      this.outcomes.set(hash, { success: true, vmStatus: 'Executed successfully' })
    }
    return {
      txHash: hash,
      sequenceNumber: '0',
      gasUnitPrice: 1n,
      maxGasAmount: 10n,
      submit: async () => {
        this.submissions += 1
        if (this.submitError) {
          if (this.commitBeforeError) commit()
          throw new Error('network timeout')
        }
        commit()
        return hash
      },
      wait: async () => this.outcomes.get(hash) ?? { success: false, vmStatus: 'missing' },
    }
  }

  async findTransaction(hash: string): Promise<{ found: boolean; success?: boolean; vmStatus?: string }> {
    const result = this.outcomes.get(hash)
    return result ? { found: true, ...result } : { found: false }
  }
}
