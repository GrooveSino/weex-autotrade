import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { openDatabase, type SqliteDatabase } from '../server/database.js'
import { JobService } from '../server/jobs.js'
import { EncryptedVault } from '../server/vault.js'
import { WalletService } from '../server/wallets.js'
import { FakeGateway } from './fakes.js'

let db: SqliteDatabase | null = null
afterEach(() => { db?.close(); db = null })

async function setup(executionEnabled = true) {
  db = openDatabase(join(mkdtempSync(join(tmpdir(), 'aptos-jobs-')), 'wallet.sqlite'))
  const vault = new EncryptedVault(db)
  await vault.initialize('correct horse battery staple')
  const gateway = new FakeGateway()
  const wallets = new WalletService(db, vault, gateway)
  // Public BIP39 test vector. Never use it for a funded wallet.
  const mnemonic = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art' // gitleaks:allow
  const [source, target, payer] = wallets.restoreGroup('测试钱包', mnemonic, 3).accounts
  gateway.setBalance(source.address, 'APT', 10_000_000n)
  gateway.setBalance(source.address, 'USDT', 5_000_000n)
  gateway.setBalance(payer.address, 'APT', 10_000_000n)
  const jobs = new JobService(db, wallets, gateway, executionEnabled)
  return { gateway, wallets, jobs, source, target, payer }
}

describe('transfer jobs', () => {
  it('freezes random values and enforces exact mainnet confirmation', async () => {
    const { jobs, source, target } = await setup()
    const draft = jobs.createDraft({
      name: 'random', gasPayerWalletId: null, intervalMinSeconds: 2, intervalMaxSeconds: 5, shuffle: true,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'APT', amountMode: 'random', amountMin: '0.01', amountMax: '0.02' }],
    })
    const preview = await jobs.preview(draft.id)
    expect(preview.steps[0].frozenAmountBaseUnits).not.toBeNull()
    expect(() => jobs.confirm(draft.id, 'wrong')).toThrow('确认短语不匹配')
    jobs.confirm(draft.id, preview.confirmationPhrase!)
    await waitFor(() => jobs.get(draft.id).status === 'completed')
    expect(jobs.get(draft.id).steps[0].status).toBe('confirmed')
  })

  it('uses a fee payer and permits a full USDt transfer', async () => {
    const { jobs, gateway, source, target, payer } = await setup()
    const draft = jobs.createDraft({ name: 'max', gasPayerWalletId: payer.id, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'max', amountMin: null, amountMax: null }] })
    const preview = await jobs.preview(draft.id)
    jobs.confirm(draft.id, preview.confirmationPhrase!)
    await waitFor(() => jobs.get(draft.id).status === 'completed')
    expect(await gateway.getBalance(source.address, 'USDT')).toBe(0n)
    expect(await gateway.getBalance(target.address, 'USDT')).toBe(5_000_000n)
  })

  it('returns a row-level error and blocks preview when the sender cannot pay APT gas', async () => {
    const { jobs, gateway, source, target } = await setup()
    gateway.setBalance(source.address, 'APT', 0n)
    const draft = jobs.createDraft({ name: 'no gas', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null }] })
    const result = await jobs.checkAndPreview(draft.id)
    expect(result.valid).toBe(false)
    expect(result.checks[0].error).toContain('APT 手续费不足')
    expect(result.checks[0].estimatedGasBaseUnits).toBe('10')
    expect(jobs.get(draft.id).status).toBe('draft')
  })

  it('shows simulated gas and accepts a separate fee payer for a USDt sender without APT', async () => {
    const { jobs, gateway, source, target, payer } = await setup()
    gateway.setBalance(source.address, 'APT', 0n)
    const draft = jobs.createDraft({ name: 'sponsored gas', gasPayerWalletId: payer.id, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null }] })
    const result = await jobs.checkAndPreview(draft.id)
    expect(result.valid).toBe(true)
    expect(result.checks[0]).toMatchObject({ valid: true, gasWalletId: payer.id, estimatedGasBaseUnits: '10' })
    expect(result.summary?.estimatedGasBaseUnits).toBe('10')
  })

  it('keeps the configured APT gas reserve for a self-paid full transfer', async () => {
    const { jobs, gateway, source, target } = await setup()
    const draft = jobs.createDraft({ name: 'apt max', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'APT', amountMode: 'max', amountMin: null, amountMax: null }] })
    const preview = await jobs.preview(draft.id)
    jobs.confirm(draft.id, preview.confirmationPhrase!)
    await waitFor(() => jobs.get(draft.id).status === 'completed')
    expect(await gateway.getBalance(target.address, 'APT')).toBe(9_900_000n)
    expect(await gateway.getBalance(source.address, 'APT')).toBe(99_990n)
  })

  it('marks an unobservable submission uncertain and never retries', async () => {
    const { jobs, gateway, source, target } = await setup()
    gateway.submitError = true
    const draft = jobs.createDraft({ name: 'uncertain', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'APT', amountMode: 'fixed', amountMin: '0.01', amountMax: null }] })
    const preview = await jobs.preview(draft.id)
    jobs.confirm(draft.id, preview.confirmationPhrase!)
    await waitFor(() => jobs.get(draft.id).status === 'uncertain', 6_000)
    expect(gateway.submissions).toBe(1)
    expect(() => jobs.resume(draft.id)).toThrow()
  })

  it('keeps execution disabled by default', async () => {
    const { jobs, source, target } = await setup(false)
    const draft = jobs.createDraft({ name: 'blocked', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'APT', amountMode: 'fixed', amountMin: '0.01', amountMax: null }] })
    const preview = await jobs.preview(draft.id)
    expect(() => jobs.confirm(draft.id, preview.confirmationPhrase!)).toThrow('门禁未开启')
  })
})

async function waitFor(predicate: () => boolean, timeout = 2_000): Promise<void> {
  const end = Date.now() + timeout
  while (Date.now() < end) {
    if (predicate()) return
    await new Promise((resolve) => setTimeout(resolve, 20))
  }
  throw new Error('timed out')
}
