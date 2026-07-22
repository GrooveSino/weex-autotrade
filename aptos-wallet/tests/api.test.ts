import { generateKeyPairSync } from 'node:crypto'
import { mkdirSync, mkdtempSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { Account } from '@aptos-labs/ts-sdk'
import { createApp } from '../server/app.js'
import type { AppConfig } from '../server/config.js'
import { openDatabase } from '../server/database.js'
import { EncryptedVault } from '../server/vault.js'
import { WalletService } from '../server/wallets.js'
import { JobService } from '../server/jobs.js'
import { BackupService } from '../server/backup.js'
import { FakeGateway } from './fakes.js'

const PASSWORD = 'correct horse battery staple'
// Public BIP39 test vector. Never use it for a funded wallet.
const MNEMONIC = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art' // gitleaks:allow
const apps: Array<Awaited<ReturnType<typeof createApp>>> = []

afterEach(async () => { while (apps.length) await apps.pop()!.close() })

async function setup(executionEnabled = false) {
  const config: AppConfig = { host: '127.0.0.1', port: 4311, webOrigin: 'http://127.0.0.1:4310', databasePath: join(mkdtempSync(join(tmpdir(), 'aptos-api-')), 'wallet.sqlite'), executionEnabled }
  const db = openDatabase(config.databasePath)
  const vault = new EncryptedVault(db)
  const gateway = new FakeGateway()
  const wallets = new WalletService(db, vault, gateway)
  const jobs = new JobService(db, wallets, gateway, executionEnabled)
  const app = await createApp(config, { db, vault, gateway, wallets, jobs, backup: new BackupService(db) })
  apps.push(app)
  const status = await app.inject({ method: 'GET', url: '/api/v1/status', headers: { origin: config.webOrigin } })
  const csrf = status.json().csrfToken as string
  const initialized = await app.inject({ method: 'POST', url: '/api/v1/vault/initialize', payload: { password: PASSWORD }, headers: { origin: config.webOrigin, 'x-csrf-token': csrf } })
  const headers = { origin: config.webOrigin, 'x-csrf-token': csrf, cookie: `aptos_wallet_session=${initialized.cookies[0].value}` }
  return { app, config, headers, wallets, jobs, gateway }
}

describe('local API safety', () => {
  it('returns a clear mainnet connectivity error instead of a raw fetch failure', async () => {
    const { app, headers, gateway, wallets } = await setup()
    const source = wallets.importPrivateKey('转出账户', Account.generate().privateKey.toString())
    const target = wallets.importPrivateKey('收款账户', Account.generate().privateKey.toString())
    gateway.setBalance(source.address, 'APT', 100_000_000n)
    gateway.estimateError = 'fetch failed'
    const response = await app.inject({
      method: 'POST',
      url: '/api/v1/jobs',
      headers,
      payload: {
        name: '连接检查', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
        steps: [{ sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'APT', amountMode: 'fixed', amountMin: '0.01', amountMax: null }],
      },
    })
    const jobId = response.json().id as string
    const preview = await app.inject({ method: 'POST', url: `/api/v1/jobs/${jobId}/check`, headers })

    expect(preview.statusCode).toBe(200)
    expect(preview.json().checks[0]).toMatchObject({ valid: false, error: 'Aptos 主网连接暂时中断，已自动重试仍未恢复；请稍后重新预览。' })
  })

  it('requires a fresh exact confirmation before retrying a definitely failed step', async () => {
    const { app, config, headers, gateway, wallets, jobs } = await setup(true)
    const source = wallets.importPrivateKey('转出账户', Account.generate().privateKey.toString())
    const target = wallets.importPrivateKey('收款账户', Account.generate().privateKey.toString())
    gateway.setBalance(source.address, 'APT', 100_000_000n)
    gateway.setBalance(source.address, 'USDT', 5_000_000n)
    const draft = jobs.createDraft({ name: '失败后继续', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null }] })
    const preview = await jobs.preview(draft.id)
    gateway.failNextTransaction = true
    jobs.confirm(draft.id, preview.confirmationPhrase!)
    await waitFor(() => jobs.get(draft.id).status === 'failed')

    expect((await app.inject({ method: 'POST', url: `/api/v1/jobs/${draft.id}/retry-failed`, headers: { origin: config.webOrigin }, payload: { confirmation: preview.confirmationPhrase } })).statusCode).toBe(403)
    expect((await app.inject({ method: 'POST', url: `/api/v1/jobs/${draft.id}/retry-failed`, headers, payload: { confirmation: 'wrong' } })).statusCode).toBe(400)
    const retried = await app.inject({ method: 'POST', url: `/api/v1/jobs/${draft.id}/retry-failed`, headers, payload: { confirmation: preview.confirmationPhrase } })
    expect(retried.statusCode).toBe(200)
    await waitFor(() => jobs.get(draft.id).status === 'completed')
    expect(jobs.attempts(draft.id)).toHaveLength(2)
  })

  it('serves frontend assets created after startup without returning the SPA HTML fallback', async () => {
    const root = mkdtempSync(join(tmpdir(), 'aptos-web-'))
    mkdirSync(join(root, 'assets'))
    writeFileSync(join(root, 'index.html'), '<!doctype html><div id="root"></div>')
    const config: AppConfig = {
      host: '127.0.0.1', port: 4311, webOrigin: 'http://127.0.0.1:4310',
      databasePath: join(root, 'wallet.sqlite'), webDistPath: root, executionEnabled: false,
    }
    const app = await createApp(config)
    apps.push(app)

    writeFileSync(join(root, 'assets', 'index-new-hash.js'), 'globalThis.walletBooted = true')
    const asset = await app.inject({ method: 'GET', url: '/assets/index-new-hash.js' })
    expect(asset.statusCode).toBe(200)
    expect(asset.headers['content-type']).toContain('javascript')
    expect(asset.body).toBe('globalThis.walletBooted = true')

    const missingAsset = await app.inject({ method: 'GET', url: '/assets/missing.js' })
    expect(missingAsset.statusCode).toBe(404)
    expect(missingAsset.body).not.toContain('<div id="root">')
    const clientRoute = await app.inject({ method: 'GET', url: '/execution-history' })
    expect(clientRoute.statusCode).toBe(200)
    expect(clientRoute.body).toContain('<div id="root">')
    expect(clientRoute.headers['cache-control']).toBe('no-store')
  })

  it('requires CSRF, same-origin, and an unlocked HttpOnly session', async () => {
    const config: AppConfig = { host: '127.0.0.1', port: 4311, webOrigin: 'http://127.0.0.1:4310', databasePath: join(mkdtempSync(join(tmpdir(), 'aptos-api-auth-')), 'wallet.sqlite'), executionEnabled: false }
    const app = await createApp(config)
    apps.push(app)
    const status = await app.inject({ method: 'GET', url: '/api/v1/status', headers: { origin: config.webOrigin } })
    const csrf = status.json().csrfToken as string
    expect(status.headers['content-security-policy']).toContain("frame-ancestors 'none'")
    expect(status.headers['x-frame-options']).toBe('DENY')
    expect(status.headers['cache-control']).toBe('no-store')
    expect((await app.inject({ method: 'GET', url: '/api/v1/status', headers: { host: 'wallet.example.com', origin: config.webOrigin } })).statusCode).toBe(403)
    expect((await app.inject({ method: 'GET', url: '/api/v1/status', headers: { origin: config.webOrigin, 'sec-fetch-site': 'cross-site' } })).statusCode).toBe(403)
    expect((await app.inject({ method: 'POST', url: '/api/v1/vault/initialize', payload: { password: PASSWORD }, headers: { origin: config.webOrigin } })).statusCode).toBe(403)
    expect((await app.inject({ method: 'POST', url: '/api/v1/vault/initialize', payload: { password: PASSWORD }, headers: { 'x-csrf-token': csrf } })).statusCode).toBe(403)
    const initialized = await app.inject({ method: 'POST', url: '/api/v1/vault/initialize', payload: { password: PASSWORD }, headers: { origin: config.webOrigin, 'x-csrf-token': csrf } })
    expect(initialized.statusCode).toBe(200)
    expect(initialized.headers['set-cookie']).toContain('HttpOnly')
    const firstCookie = `aptos_wallet_session=${initialized.cookies[0].value}`
    expect((await app.inject({ method: 'GET', url: '/api/v1/status', headers: { origin: config.webOrigin } })).json().unlocked).toBe(false)
    expect((await app.inject({ method: 'GET', url: '/api/v1/status', headers: { origin: config.webOrigin, cookie: firstCookie } })).json().unlocked).toBe(true)
    expect((await app.inject({ method: 'GET', url: '/api/v1/status', headers: { origin: 'https://evil.example' } })).statusCode).toBe(403)
    expect((await app.inject({ method: 'GET', url: '/api/v1/wallets', headers: { origin: config.webOrigin } })).statusCode).toBe(423)

    const unlockedAgain = await app.inject({ method: 'POST', url: '/api/v1/vault/unlock', payload: { password: PASSWORD }, headers: { origin: config.webOrigin, 'x-csrf-token': csrf } })
    const secondCookie = `aptos_wallet_session=${unlockedAgain.cookies[0].value}`
    expect((await app.inject({ method: 'GET', url: '/api/v1/wallets', headers: { origin: config.webOrigin, cookie: firstCookie } })).statusCode).toBe(423)
    expect((await app.inject({ method: 'GET', url: '/api/v1/wallets', headers: { origin: config.webOrigin, cookie: secondCookie } })).statusCode).toBe(200)
  })

  it('rate-limits repeated master-password failures', async () => {
    const { app, config, headers } = await setup()
    for (let attempt = 0; attempt < 5; attempt += 1) {
      const response = await app.inject({ method: 'POST', url: '/api/v1/vault/unlock', headers, payload: { password: 'incorrect password value' } })
      expect(response.statusCode).toBe(400)
    }
    const blocked = await app.inject({ method: 'POST', url: '/api/v1/vault/unlock', headers, payload: { password: PASSWORD } })
    expect(blocked.statusCode).toBe(429)
    expect(Number(blocked.headers['retry-after'])).toBeGreaterThan(0)
  })

  it('creates, restores, and extends HD wallets without discovery scan routes', async () => {
    const { app, headers } = await setup()
    const words = MNEMONIC.split(' ')
    const created = await app.inject({ method: 'POST', url: '/api/v1/wallets/groups/create', headers, payload: {
      label: '主钱包', mnemonic: MNEMONIC, confirmationIndexes: [0, 5, 10, 23], confirmationWords: [words[0], words[5], words[10], words[23]],
    } })
    expect(created.statusCode).toBe(200)
    expect(created.json().accounts[0].accountIndex).toBe(0)
    expect(created.body).not.toContain(MNEMONIC)
    expect(created.body).not.toContain('privateKey')

    const groupId = created.json().id as string
    const added = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/accounts`, headers, payload: { count: 2 } })
    expect(added.json().accounts.map((account: { accountIndex: number }) => account.accountIndex)).toEqual([0, 1, 2])
    const restored = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/accounts/restore`, headers, payload: { accountIndexes: [37] } })
    expect(restored.json().nextAccountIndex).toBe(38)

    const duplicate = await app.inject({ method: 'POST', url: '/api/v1/wallets/groups/restore', headers, payload: { label: '重复', mnemonic: MNEMONIC, accountCount: 1, accountIndexes: [] } })
    expect(duplicate.statusCode).toBe(409)
    expect((await app.inject({ method: 'POST', url: '/api/v1/wallets/import/mnemonic/scan', headers, payload: {} })).statusCode).toBe(404)
    expect((await app.inject({ method: 'POST', url: '/api/v1/wallets/generate', headers, payload: {} })).statusCode).toBe(404)
  })

  it('returns only RSA encrypted secret material and protects group archive', async () => {
    const { app, headers } = await setup()
    const restored = await app.inject({ method: 'POST', url: '/api/v1/wallets/groups/restore', headers, payload: { label: '秘密钱包', mnemonic: MNEMONIC, accountCount: 1, accountIndexes: [] } })
    const groupId = restored.json().id as string
    const { publicKey } = generateKeyPairSync('rsa', { modulusLength: 2048 })
    const publicKeyPem = publicKey.export({ type: 'spki', format: 'pem' }).toString()

    const wrong = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/reveal-mnemonic`, headers, payload: { password: 'wrong password value', confirmationName: '秘密钱包', publicKey: publicKeyPem } })
    expect(wrong.statusCode).toBe(400)
    const revealed = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/reveal-mnemonic`, headers, payload: { password: PASSWORD, confirmationName: '秘密钱包', publicKey: publicKeyPem } })
    expect(revealed.statusCode).toBe(200)
    expect(revealed.json().algorithm).toBe('RSA-OAEP-256')
    expect(revealed.body).not.toContain(MNEMONIC)

    const missingName = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/archive`, headers, payload: { confirmationName: '' } })
    expect(missingName.statusCode).toBe(400)
    expect(missingName.json().error).toBe('请手动输入完整钱包名称或账户名称')
    const badName = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/archive`, headers, payload: { confirmationName: '错误名称' } })
    expect(badName.statusCode).toBe(400)
    const archived = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/archive`, headers, payload: { confirmationName: '秘密钱包' } })
    expect(archived.json().archivedAt).not.toBeNull()
    expect((await app.inject({ method: 'GET', url: '/api/v1/wallets/groups', headers })).json()).toEqual([])
    expect((await app.inject({ method: 'GET', url: '/api/v1/wallets/groups?includeArchived=true', headers })).json()).toHaveLength(1)
    const missingPassword = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/unarchive`, headers, payload: {} })
    expect(missingPassword.statusCode).toBe(400)
    const wrongPassword = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/unarchive`, headers, payload: { password: 'wrong password value' } })
    expect(wrongPassword.statusCode).toBe(400)
    const restoredArchive = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/unarchive`, headers, payload: { password: PASSWORD } })
    expect(restoredArchive.statusCode).toBe(200)
    expect(restoredArchive.json().archivedAt).toBeNull()
  })

  it('returns paginated per-account transfer logs and backfills legacy gas fees', async () => {
    const { app, headers, wallets, jobs, gateway } = await setup(true)
    const [source, target] = wallets.restoreGroup('日志钱包', MNEMONIC, 2).accounts
    gateway.setBalance(source.address, 'APT', 10_000_000n)
    gateway.setBalance(source.address, 'USDT', 5_000_000n)
    const draft = jobs.createDraft({ name: '日志任务', gasPayerWalletId: null, intervalMinSeconds: 0, intervalMaxSeconds: 0, shuffle: false,
      steps: [{ id: crypto.randomUUID(), sourceWalletId: source.id, targetAddress: target.address, targetWalletId: target.id, asset: 'USDT', amountMode: 'fixed', amountMin: '2', amountMax: null }] })
    const preview = await jobs.preview(draft.id)
    jobs.confirm(draft.id, preview.confirmationPhrase!)
    await waitFor(() => jobs.get(draft.id).status === 'completed')
    app.services.db.prepare('UPDATE transaction_attempts SET gas_fee_base_units = NULL WHERE job_id = ?').run(draft.id)

    const outgoing = await app.inject({ method: 'GET', url: `/api/v1/wallets/${source.id}/transfers?direction=out&limit=50&offset=0`, headers })
    expect(outgoing.statusCode).toBe(200)
    expect(outgoing.json()).toMatchObject({ total: 1, counts: { all: 1, in: 0, out: 1 }, items: [{ direction: 'out', counterpartyWalletId: target.id, jobName: '日志任务', gasFeeBaseUnits: '10' }] })
    expect(gateway.findTransactionCalls).toBe(1)
    expect(outgoing.body).not.toContain(MNEMONIC)
    expect(outgoing.body).not.toContain('privateKey')

    const incoming = await app.inject({ method: 'GET', url: `/api/v1/wallets/${target.id}/transfers?direction=in`, headers })
    expect(incoming.json().items[0]).toMatchObject({ direction: 'in', counterpartyWalletId: source.id })
    const inspected = await app.inject({ method: 'GET', url: `/api/v1/jobs/${draft.id}`, headers })
    expect(inspected.json().steps[0].gasFeeBaseUnits).toBe('10')
    expect((await app.inject({ method: 'GET', url: `/api/v1/wallets/${crypto.randomUUID()}/transfers`, headers })).statusCode).toBe(404)
  })

  it('updates aliases for mnemonic and imported accounts through the protected API', async () => {
    const { app, headers, wallets } = await setup()
    const derived = wallets.restoreGroup('别名钱包', MNEMONIC, 1).accounts[0]
    const standalone = wallets.importPrivateKey('独立账户', Account.generate().privateKey.toString())

    for (const [id, label] of [[derived.id, '日常付款'], [standalone.id, '归集账户']] as const) {
      const response = await app.inject({ method: 'PATCH', url: `/api/v1/wallets/${id}`, headers, payload: { label } })
      expect(response.statusCode).toBe(200)
      expect(response.json().label).toBe(label)
    }
    expect((await app.inject({ method: 'PATCH', url: `/api/v1/wallets/${derived.id}`, headers, payload: { label: '   ' } })).statusCode).toBe(400)
  })

  it('protects and manages normalized external address book entries', async () => {
    const { app, config, headers } = await setup()
    const firstAddress = `0x${'a'.repeat(64)}`
    const secondAddress = `0x${'b'.repeat(64)}`
    const unauthorized = await app.inject({ method: 'POST', url: '/api/v1/address-book', headers: { origin: config.webOrigin }, payload: { label: '交易所', address: firstAddress } })
    expect(unauthorized.statusCode).toBe(403)

    const created = await app.inject({ method: 'POST', url: '/api/v1/address-book', headers, payload: { label: '交易所充值', address: firstAddress } })
    expect(created.statusCode).toBe(200)
    expect(created.json()).toMatchObject({ label: '交易所充值' })
    expect(created.json().address).toBe(firstAddress)

    const id = created.json().id as string
    const updated = await app.inject({ method: 'PATCH', url: `/api/v1/address-book/${id}`, headers, payload: { label: '交易所主账户', address: secondAddress } })
    expect(updated.json()).toMatchObject({ id, label: '交易所主账户' })
    const listed = await app.inject({ method: 'GET', url: '/api/v1/address-book', headers })
    expect(listed.json()).toEqual([expect.objectContaining({ id, label: '交易所主账户' })])

    const duplicate = await app.inject({ method: 'POST', url: '/api/v1/address-book', headers, payload: { label: '重复', address: secondAddress } })
    expect(duplicate.statusCode).toBe(409)
    expect((await app.inject({ method: 'DELETE', url: `/api/v1/address-book/${id}`, headers })).statusCode).toBe(200)
    expect((await app.inject({ method: 'GET', url: '/api/v1/address-book', headers })).json()).toEqual([])
  })

  it('refreshes one wallet group behind session and CSRF protection', async () => {
    const { app, config, headers, wallets, gateway } = await setup()
    const group = wallets.restoreGroup('刷新钱包', MNEMONIC, 2)
    gateway.setBalance(group.accounts[1].address, 'APT', 30_000_000n)
    const url = `/api/v1/wallets/groups/${group.id}/refresh`

    expect((await app.inject({ method: 'POST', url, headers: { origin: config.webOrigin } })).statusCode).toBe(403)
    expect((await app.inject({ method: 'POST', url, headers: { origin: config.webOrigin, 'x-csrf-token': headers['x-csrf-token'] } })).statusCode).toBe(423)
    const refreshed = await app.inject({ method: 'POST', url, headers })
    expect(refreshed.statusCode).toBe(200)
    expect(refreshed.json().accounts).toHaveLength(2)
    expect(refreshed.json().accounts[1].balances[0]).toMatchObject({ asset: 'APT', baseUnits: '30000000', display: '0.3' })
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
