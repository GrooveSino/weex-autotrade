import { generateKeyPairSync } from 'node:crypto'
import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
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

async function setup() {
  const config: AppConfig = { host: '127.0.0.1', port: 4311, webOrigin: 'http://127.0.0.1:4310', databasePath: join(mkdtempSync(join(tmpdir(), 'aptos-api-')), 'wallet.sqlite'), executionEnabled: false }
  const db = openDatabase(config.databasePath)
  const vault = new EncryptedVault(db)
  const gateway = new FakeGateway()
  const wallets = new WalletService(db, vault, gateway)
  const jobs = new JobService(db, wallets, gateway, false)
  const app = await createApp(config, { db, vault, gateway, wallets, jobs, backup: new BackupService(db) })
  apps.push(app)
  const status = await app.inject({ method: 'GET', url: '/api/v1/status', headers: { origin: config.webOrigin } })
  const csrf = status.json().csrfToken as string
  const initialized = await app.inject({ method: 'POST', url: '/api/v1/vault/initialize', payload: { password: PASSWORD }, headers: { origin: config.webOrigin, 'x-csrf-token': csrf } })
  const headers = { origin: config.webOrigin, 'x-csrf-token': csrf, cookie: `aptos_wallet_session=${initialized.cookies[0].value}` }
  return { app, config, headers }
}

describe('local API safety', () => {
  it('requires CSRF, same-origin, and an unlocked HttpOnly session', async () => {
    const config: AppConfig = { host: '127.0.0.1', port: 4311, webOrigin: 'http://127.0.0.1:4310', databasePath: join(mkdtempSync(join(tmpdir(), 'aptos-api-auth-')), 'wallet.sqlite'), executionEnabled: false }
    const app = await createApp(config)
    apps.push(app)
    const status = await app.inject({ method: 'GET', url: '/api/v1/status', headers: { origin: config.webOrigin } })
    const csrf = status.json().csrfToken as string
    expect((await app.inject({ method: 'POST', url: '/api/v1/vault/initialize', payload: { password: PASSWORD }, headers: { origin: config.webOrigin } })).statusCode).toBe(403)
    const initialized = await app.inject({ method: 'POST', url: '/api/v1/vault/initialize', payload: { password: PASSWORD }, headers: { origin: config.webOrigin, 'x-csrf-token': csrf } })
    expect(initialized.statusCode).toBe(200)
    expect(initialized.headers['set-cookie']).toContain('HttpOnly')
    expect((await app.inject({ method: 'GET', url: '/api/v1/status', headers: { origin: 'https://evil.example' } })).statusCode).toBe(403)
    expect((await app.inject({ method: 'GET', url: '/api/v1/wallets', headers: { origin: config.webOrigin } })).statusCode).toBe(423)
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

    const badName = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/archive`, headers, payload: { password: PASSWORD, confirmationName: '错误名称' } })
    expect(badName.statusCode).toBe(400)
    const archived = await app.inject({ method: 'POST', url: `/api/v1/wallets/groups/${groupId}/archive`, headers, payload: { password: PASSWORD, confirmationName: '秘密钱包' } })
    expect(archived.json().archivedAt).not.toBeNull()
    expect((await app.inject({ method: 'GET', url: '/api/v1/wallets/groups', headers })).json()).toEqual([])
    expect((await app.inject({ method: 'GET', url: '/api/v1/wallets/groups?includeArchived=true', headers })).json()).toHaveLength(1)
  })
})
