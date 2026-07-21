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

  it('shows the same internal transfer as outgoing and incoming account history', async () => {
    const { jobs, source, target, payer } = await setup()
    const draft = jobs.createDraft({ name: 'account history', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null }] })
    const preview = await jobs.preview(draft.id)
    jobs.confirm(draft.id, preview.confirmationPhrase!)
    await waitFor(() => jobs.get(draft.id).status === 'completed')

    const outgoing = jobs.accountTransfers(source.id, 50, 0, 'out')
    expect(outgoing.counts).toEqual({ all: 1, in: 0, out: 1 })
    expect(outgoing.items[0]).toMatchObject({ direction: 'out', counterpartyWalletId: target.id, counterpartyAddress: target.address, frozenAmountDisplay: '1', status: 'confirmed' })
    expect(outgoing.items[0].txHash).toMatch(/^0x/)

    const incoming = jobs.accountTransfers(target.id, 50, 0, 'in')
    expect(incoming.counts).toEqual({ all: 1, in: 1, out: 0 })
    expect(incoming.items[0]).toMatchObject({ direction: 'in', counterpartyWalletId: source.id, counterpartyAddress: source.address, status: 'confirmed' })
    expect(jobs.accountTransfers(payer.id).items).toEqual([])
  })

  it('does not expose draft or preview-only steps as account transfer records', async () => {
    const { jobs, source, target } = await setup()
    const draft = jobs.createDraft({ name: 'not executed', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null }] })

    expect(jobs.accountTransfers(source.id).items).toEqual([])
    await jobs.preview(draft.id)
    expect(jobs.accountTransfers(source.id).items).toEqual([])
    expect(jobs.accountTransfers(target.id).items).toEqual([])
    expect(jobs.attempts(draft.id)).toEqual([])
  })

  it('discards a cancelled plan when no transaction was attempted', async () => {
    const { jobs, source, target } = await setup()
    const draft = jobs.createDraft({ name: 'discard me', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null }] })
    await jobs.preview(draft.id)

    expect(jobs.cancel(draft.id)).toBeNull()
    expect(() => jobs.get(draft.id)).toThrow('任务不存在')
    expect(db!.prepare('SELECT COUNT(*) AS count FROM job_steps WHERE job_id = ?').get(draft.id)).toEqual({ count: 0 })
    expect(db!.prepare('SELECT COUNT(*) AS count FROM transaction_attempts WHERE job_id = ?').get(draft.id)).toEqual({ count: 0 })
    expect(db!.prepare("SELECT COUNT(*) AS count FROM audit_events WHERE entity_id = ? AND kind LIKE 'job.%'").get(draft.id)).toEqual({ count: 0 })
    expect(jobs.accountTransfers(source.id).items).toEqual([])
  })

  it('keeps attempted rows but discards untouched rows when cancelling a partially executed plan', async () => {
    const { jobs, source, target, payer } = await setup()
    const draft = jobs.createDraft({ name: 'partial', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [
        { id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null },
        { id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: payer.address, targetWalletId: payer.id, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null },
      ] })
    const preview = await jobs.preview(draft.id)
    const attempted = preview.steps[0]
    const now = new Date().toISOString()
    db!.prepare(`
      INSERT INTO transaction_attempts(id, job_id, step_id, sender_address, sequence_number, tx_hash, state, created_at, updated_at)
      VALUES (?, ?, ?, ?, '0', ?, 'uncertain', ?, ?)
    `).run(crypto.randomUUID(), draft.id, attempted.id, source.address, `0x${'1'.repeat(64)}`, now, now)

    expect(jobs.cancel(draft.id)?.status).toBe('cancelled')
    expect(jobs.get(draft.id).steps.map((step) => step.id)).toEqual([attempted.id])
    expect(jobs.attempts(draft.id)).toHaveLength(1)
    expect(jobs.accountTransfers(source.id).items).toHaveLength(1)
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

  it('rejects random ranges with more than two decimal places', async () => {
    const { jobs, source, target } = await setup()
    expect(() => jobs.createDraft({ name: 'precision', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'random', amountMin: '1.001', amountMax: '2' }] })).toThrow('随机金额最多支持 2 位小数')
  })

  it('turns Aptos fee simulation errors into an actionable message', async () => {
    const { jobs, gateway, source, target } = await setup()
    gateway.estimateError = '交易模拟失败：INSUFFICIENT_BALANCE_FOR_TRANSACTION_FEE'
    const draft = jobs.createDraft({ name: 'clear fee error', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null }] })
    const result = await jobs.checkAndPreview(draft.id)
    expect(result.valid).toBe(false)
    expect(result.checks[0].error).toBe('交易无法支付 APT 网络手续费：请给转出账户充值 APT，或选择一个有 APT 余额的手续费账户。')
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

  it('reorders frozen preview steps as whole records and invalidates the old phrase', async () => {
    const { jobs, source, target, payer } = await setup()
    const draft = jobs.createDraft({ name: 'reorder', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [
        { id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null },
        { id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: payer.address, targetWalletId: payer.id, asset: 'USDT', amountMode: 'fixed', amountMin: '2', amountMax: null },
      ] })
    const first = await jobs.preview(draft.id)
    const oldPhrase = first.confirmationPhrase
    const original = first.steps.map((step) => ({ id: step.id, amount: step.frozenAmountDisplay }))
    const reordered = await jobs.reorderPreview(draft.id, [...first.steps].reverse().map((step) => step.id))
    expect(reordered.valid).toBe(true)
    expect(reordered.job.status).toBe('previewed')
    expect(reordered.job.confirmationPhrase).not.toBe(oldPhrase)
    expect(reordered.job.steps.map((step) => ({ id: step.id, amount: step.frozenAmountDisplay }))).toEqual(original.reverse())
    expect(() => jobs.confirm(draft.id, oldPhrase!)).toThrow('确认短语不匹配')
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
    db!.prepare("UPDATE jobs SET status = 'paused' WHERE id = ?").run(draft.id)
    expect(() => jobs.resume(draft.id)).toThrow('门禁未开启')
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
