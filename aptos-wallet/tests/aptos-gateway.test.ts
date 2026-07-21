import { afterEach, describe, expect, it, vi } from 'vitest'
import { Account } from '@aptos-labs/ts-sdk'
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
})
