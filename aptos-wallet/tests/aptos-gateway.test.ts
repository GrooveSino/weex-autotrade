import { afterEach, describe, expect, it, vi } from 'vitest'
import { Account, AccountAddress, type SimpleTransaction } from '@aptos-labs/ts-sdk'
import { AptosMainnetGateway } from '../server/aptos-gateway.js'
import type { AppConfig } from '../server/config.js'

const config: AppConfig = {
  host: '127.0.0.1',
  port: 4311,
  webOrigin: 'http://127.0.0.1:4310',
  databasePath: ':memory:',
  executionEnabled: false,
  fullnodeUrl: 'https://fullnode.example/v1',
}

afterEach(() => vi.unstubAllGlobals())

describe('Aptos mainnet balance reader', () => {
  it('reads unified Coin and Fungible Asset APT balances without number precision loss', async () => {
    const amount = '900719925474099312345'
    const fetchMock = vi.fn().mockResolvedValue(new Response(amount, { status: 200, headers: { 'content-type': 'application/json' } }))
    vi.stubGlobal('fetch', fetchMock)

    const balance = await new AptosMainnetGateway(config).getBalance(`0x${'1'.repeat(64)}`, 'APT')
    expect(balance).toBe(900719925474099312345n)
    expect(fetchMock.mock.calls[0][0]).toContain('/balance/0x1%3A%3Aaptos_coin%3A%3AAptosCoin')
  })

  it('returns zero only for a missing account and surfaces fullnode failures', async () => {
    const gateway = new AptosMainnetGateway(config)
    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('', { status: 404 })))
    await expect(gateway.getBalance(`0x${'2'.repeat(64)}`, 'APT')).resolves.toBe(0n)

    vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(new Response('unavailable', { status: 503 })))
    await expect(gateway.getBalance(`0x${'2'.repeat(64)}`, 'APT')).rejects.toThrow('APT 余额查询失败 (HTTP 503)')
  })

  it('retries rate-limited read requests without retrying permanent failures', async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(new Response('rate limited', { status: 429 }))
      .mockResolvedValueOnce(new Response('30000000', { status: 200 }))
    vi.stubGlobal('fetch', fetchMock)

    await expect(new AptosMainnetGateway(config).getBalance(`0x${'3'.repeat(64)}`, 'APT')).resolves.toBe(30_000_000n)
    expect(fetchMock).toHaveBeenCalledTimes(2)
  })

  it('reuses account existence learned from a successful APT balance response', async () => {
    const gateway = new AptosMainnetGateway(config)
    const address = `0x${'5'.repeat(64)}`
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue(new Response('0', { status: 200 })))
    vi.spyOn(gateway.aptos, 'view').mockResolvedValue(['0'] as never)
    const accountInfo = vi.spyOn(gateway.aptos, 'getAccountInfo')

    await gateway.getBalances(address)
    await expect(gateway.accountExists(address)).resolves.toBe(true)
    expect(accountInfo).not.toHaveBeenCalled()
  })

  it('keeps concurrent fullnode requests at the process-wide limit', async () => {
    const gateway = new AptosMainnetGateway(config)
    let active = 0
    let peak = 0
    const delayedResponse = async () => {
      active += 1
      peak = Math.max(peak, active)
      await new Promise((resolve) => setTimeout(resolve, 5))
      active -= 1
      return new Response('0', { status: 200 })
    }
    vi.stubGlobal('fetch', vi.fn(delayedResponse))
    vi.spyOn(gateway.aptos, 'view').mockImplementation((async () => {
      const response = await delayedResponse()
      await response.text()
      return ['0']
    }) as never)

    await Promise.all(Array.from({ length: 4 }, (_, index) => gateway.getBalances(`0x${(index + 6).toString(16).padStart(64, '0')}`)))
    expect(peak).toBeLessThanOrEqual(2)
  })

  it('clears signing keys when transaction construction fails', async () => {
    const gateway = new AptosMainnetGateway(config)
    const sender = Account.generate()
    const feePayer = Account.generate()
    vi.spyOn(gateway.aptos.transaction.build, 'simple').mockRejectedValueOnce(new Error('build failed'))

    await expect(gateway.prepareTransfer({
      sender,
      feePayer,
      recipient: `0x${'4'.repeat(64)}`,
      asset: 'APT',
      amount: 1n,
    })).rejects.toThrow('build failed')
    expect(sender.privateKey.isCleared()).toBe(true)
    expect(feePayer.privateKey.isCleared()).toBe(true)
  })

  it('binds the real fee payer before simulating when the recipient pays the gas', async () => {
    const gateway = new AptosMainnetGateway(config)
    const sender = Account.generate()
    const feePayer = Account.generate()
    vi.spyOn(gateway, 'getBalance').mockResolvedValue(27_793_300n)
    const build = vi.spyOn(gateway.aptos.transaction.build, 'simple').mockImplementation(async (args) => ({
      rawTransaction: {
        sequence_number: args.options?.accountSequenceNumber ?? 0n,
        gas_unit_price: BigInt(args.options?.gasUnitPrice ?? 100),
        max_gas_amount: BigInt(args.options?.maxGasAmount ?? 2_000_000),
      },
      feePayerAddress: AccountAddress.ZERO,
    }) as unknown as SimpleTransaction)
    const simulate = vi.spyOn(gateway.aptos.transaction.simulate, 'simple').mockImplementation(async (args) => {
      expect(args.transaction.feePayerAddress?.toString()).toBe(feePayer.accountAddress.toString())
      expect(args.feePayerPublicKey).toBe(feePayer.publicKey)
      expect(args.transaction.rawTransaction.max_gas_amount).toBe(277_933n)
      expect(args.options?.estimateMaxGasAmount).toBe(false)
      return [{ success: true, gas_used: '5070', gas_unit_price: '100' }] as never
    })

    await expect(gateway.estimateGas({
      sender,
      feePayer,
      recipient: feePayer.accountAddress.toString(),
      asset: 'USDT',
      amount: 1_000_000n,
    })).resolves.toEqual({ gasUnitPrice: 100n, maxGasAmount: 6_338n })
    expect(build).toHaveBeenCalledTimes(2)
    expect(build.mock.calls[1][0].options).toMatchObject({ gasUnitPrice: 100, maxGasAmount: 277_933 })
    expect(simulate).toHaveBeenCalledOnce()
    expect(sender.privateKey.isCleared()).toBe(true)
    expect(feePayer.privateKey.isCleared()).toBe(true)
  })
})
