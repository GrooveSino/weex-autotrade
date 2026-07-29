import { generateKeyPairSync, privateDecrypt, constants } from 'node:crypto'
import { mkdtempSync, readFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { Account } from '@aptos-labs/ts-sdk'
import { openDatabase, type SqliteDatabase } from '../server/database.js'
import { BackupService, type VaultBackup } from '../server/backup.js'
import { EncryptedVault } from '../server/vault.js'
import { APTOS_HD_PATH, BALANCE_REFRESH_CONCURRENCY, BALANCE_REFRESH_MAX_ATTEMPTS, MAX_ACCOUNT_INDEX, WalletService } from '../server/wallets.js'
import { FakeGateway } from './fakes.js'

// Public BIP39 test vector. Never use it for a funded wallet.
const MNEMONIC = 'abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon abandon art' // gitleaks:allow
const EXPECTED = {
  0: '0x226b5b5c15d19946b1fd26c3e4ad36833fd8c9c11c612b1327e608f824a5f2d4',
  1: '0xf0f12cc090bb526434ecc758b5fd8547f806fe973f4e923f89e481a20b332828',
  37: '0xaf6a398fad01b047672b6e568dbdd751beeec89e158fafcf090de7be5c5b82e5',
  [MAX_ACCOUNT_INDEX]: '0x27466c3776422e75ce93f71995f0c8e97cd28ad4c6221ff1cb88971cc39cd796',
} as const

let db: SqliteDatabase
let vault: EncryptedVault
let gateway: FakeGateway
let wallets: WalletService
let databasePath: string

beforeEach(async () => {
  databasePath = join(mkdtempSync(join(tmpdir(), 'aptos-hd-')), 'wallet.sqlite')
  db = openDatabase(databasePath)
  vault = new EncryptedVault(db)
  await vault.initialize('correct horse battery staple')
  gateway = new FakeGateway()
  wallets = new WalletService(db, vault, gateway)
})

afterEach(() => db.close())

describe('Aptos HD wallet groups', () => {
  it('matches fixed derivation vectors including the maximum index', () => {
    const preview = wallets.previewRestore(MNEMONIC, 0, [0, 1, 37, MAX_ACCOUNT_INDEX])
    for (const account of preview) expect(account.address).toBe(EXPECTED[account.accountIndex as keyof typeof EXPECTED])
    expect(() => wallets.previewRestore(MNEMONIC, 0, [MAX_ACCOUNT_INDEX + 1])).toThrow('账户索引必须在')
    expect(() => wallets.previewRestore(MNEMONIC, 0, [1, 1])).toThrow('不能重复')
    expect(() => wallets.previewRestore(MNEMONIC, 200, [MAX_ACCOUNT_INDEX])).toThrow('单次最多处理 200')
  })

  it('requires a correct four-word backup confirmation and creates account zero atomically', () => {
    const words = MNEMONIC.split(' ')
    expect(() => wallets.createGroup('主钱包', MNEMONIC, [0, 5, 10, 23], ['wrong', words[5], words[10], words[23]])).toThrow('确认失败')
    expect(wallets.listGroups()).toHaveLength(0)

    const group = wallets.createGroup('主钱包', MNEMONIC, [0, 5, 10, 23], [words[0], words[5], words[10], words[23]])
    expect(group.derivationProfile).toBe('aptos_hd')
    expect(group.nextAccountIndex).toBe(1)
    expect(group.accounts.map((account) => account.accountIndex)).toEqual([0])
    expect(group.accounts[0].address).toBe(EXPECTED[0])
    expect(group.accounts[0].accountStatus).toBe('unused')
    expect(readFileSync(databasePath).toString('utf8')).not.toContain(MNEMONIC)
    expect(() => wallets.restoreGroup('重复', MNEMONIC, 1)).toThrow(/已存在.*钱包组 ID/)
  })

  it('adds sequential accounts, restores explicit indexes, and never reuses archived indexes', () => {
    const group = wallets.restoreGroup('账户钱包', MNEMONIC, 1)
    wallets.addAccounts(group.id, 2)
    wallets.restoreAccounts(group.id, [37])
    expect(wallets.getGroup(group.id).accounts.map((account) => account.accountIndex)).toEqual([0, 1, 2, 37])
    expect(wallets.getGroup(group.id).nextAccountIndex).toBe(38)

    const account1 = wallets.getGroup(group.id).accounts.find((account) => account.accountIndex === 1)!
    wallets.archive(account1.id)
    wallets.addAccounts(group.id, 1)
    const withArchived = wallets.getGroup(group.id, true)
    expect(withArchived.accounts.map((account) => account.accountIndex)).toEqual([0, 1, 2, 37, 38])
    expect(withArchived.accounts.find((account) => account.accountIndex === 1)?.archivedAt).not.toBeNull()
    expect(withArchived.nextAccountIndex).toBe(39)
  })

  it('archives an account with history, while discarding unexecuted plans', () => {
    const account = wallets.restoreGroup('可归档钱包', MNEMONIC, 1).accounts[0]
    const now = new Date().toISOString()
    const insertJob = (status: string, withAttempt = false) => {
      const jobId = crypto.randomUUID()
      const stepId = crypto.randomUUID()
      db.prepare(`INSERT INTO jobs(id,name,status,interval_min_seconds,interval_max_seconds,shuffle,created_at,updated_at)
        VALUES (?, ?, ?, 0, 0, 0, ?, ?)`).run(jobId, `${status} 计划`, status, now, now)
      db.prepare(`INSERT INTO job_steps(id,job_id,position,source_wallet_id,target_address,asset,amount_mode,status,updated_at)
        VALUES (?, ?, 0, ?, ?, 'USDT', 'fixed', 'pending', ?)`).run(stepId, jobId, account.id, account.address, now)
      if (withAttempt) {
        db.prepare(`INSERT INTO transaction_attempts(id,job_id,step_id,sender_address,state,created_at,updated_at)
          VALUES (?, ?, ?, ?, 'confirmed', ?, ?)`).run(crypto.randomUUID(), jobId, stepId, account.address, now, now)
      }
      return jobId
    }

    const draftId = insertJob('draft')
    const previewedId = insertJob('previewed')
    const completedId = insertJob('completed', true)

    expect(wallets.archive(account.id).archivedAt).not.toBeNull()
    expect(db.prepare('SELECT id FROM jobs WHERE id IN (?, ?)').all(draftId, previewedId)).toHaveLength(0)
    expect(db.prepare('SELECT status FROM jobs WHERE id = ?').get(completedId)).toEqual({ status: 'completed' })
  })

  it.each(['running', 'paused', 'uncertain'] as const)('keeps %s jobs blocking archive', (status) => {
    const account = wallets.restoreGroup(`${status} 保护`, MNEMONIC, 1).accounts[0]
    const now = new Date().toISOString()
    const jobId = crypto.randomUUID()
    const stepId = crypto.randomUUID()
    db.prepare(`INSERT INTO jobs(id,name,status,interval_min_seconds,interval_max_seconds,shuffle,created_at,updated_at)
      VALUES (?, ?, ?, 0, 0, 0, ?, ?)`).run(jobId, `${status} 计划`, status, now, now)
    db.prepare(`INSERT INTO job_steps(id,job_id,position,source_wallet_id,target_address,asset,amount_mode,status,updated_at)
      VALUES (?, ?, 0, ?, ?, 'USDT', 'fixed', 'pending', ?)`).run(stepId, jobId, account.id, account.address, now)

    expect(() => wallets.archive(account.id)).toThrow('账户仍被活动任务引用，不能归档')
    expect(wallets.get(account.id).archivedAt).toBeNull()
  })

  it('refreshes managed accounts as unused, used, or funded', async () => {
    const group = wallets.restoreGroup('状态钱包', MNEMONIC, 3)
    const [unused, used, funded] = group.accounts
    gateway.existingAccounts.add(used.address)
    gateway.setBalance(funded.address, 'APT', 42n)
    expect((await wallets.refresh(unused.id)).accountStatus).toBe('unused')
    expect((await wallets.refresh(used.id)).accountStatus).toBe('used')
    expect((await wallets.refresh(funded.id)).accountStatus).toBe('funded')
  })

  it('retries transient balance failures before marking an account failed', async () => {
    const group = wallets.restoreGroup('重试钱包', MNEMONIC, 1)
    const account = group.accounts[0]
    const originalGetBalances = gateway.getBalances.bind(gateway)
    let calls = 0
    gateway.getBalances = async (address: string) => {
      calls += 1
      if (calls < BALANCE_REFRESH_MAX_ATTEMPTS) throw new Error('temporary gateway timeout')
      return originalGetBalances(address)
    }

    const refreshed = await wallets.refresh(account.id)
    expect(calls).toBe(BALANCE_REFRESH_MAX_ATTEMPTS)
    expect(refreshed.balanceError).toBeNull()
  })

  it('records an error only after all balance attempts fail', async () => {
    const group = wallets.restoreGroup('最终失败钱包', MNEMONIC, 1)
    const account = group.accounts[0]
    let calls = 0
    gateway.getBalances = async () => {
      calls += 1
      throw new Error('gateway unavailable')
    }

    const refreshed = await wallets.refresh(account.id)
    expect(calls).toBe(BALANCE_REFRESH_MAX_ATTEMPTS)
    expect(refreshed.balanceError).toBe('gateway unavailable')
  })

  it('refreshes every account one at a time in the configured account order', async () => {
    const group = wallets.restoreGroup('并发刷新钱包', MNEMONIC, 25)
    const originalGetBalances = gateway.getBalances.bind(gateway)
    let active = 0
    let peak = 0
    let calls = 0
    gateway.getBalances = async (address: string) => {
      active += 1
      calls += 1
      peak = Math.max(peak, active)
      await new Promise((resolve) => setTimeout(resolve, 5))
      try {
        return await originalGetBalances(address)
      } finally {
        active -= 1
      }
    }

    const refreshed = await wallets.refreshAll()
    expect(refreshed).toHaveLength(group.accounts.length)
    expect(calls).toBe(group.accounts.length)
    expect(peak).toBe(BALANCE_REFRESH_CONCURRENCY)
  })

  it('never refreshes archived accounts, including through the global refresh entry point', async () => {
    const group = wallets.restoreGroup('归档不刷新', MNEMONIC, 3)
    const archived = group.accounts[1]
    wallets.archive(archived.id)
    const calls: string[] = []
    const originalGetBalances = gateway.getBalances.bind(gateway)
    gateway.getBalances = async (address: string) => {
      calls.push(address)
      return originalGetBalances(address)
    }

    const refreshed = await wallets.refreshAll()
    expect(refreshed.map((wallet) => wallet.id)).not.toContain(archived.id)
    expect(calls).toHaveLength(2)
    expect(calls).not.toContain(archived.address)
    await expect(wallets.refresh(archived.id)).rejects.toThrow('账户已归档，不能刷新余额')
  })

  it('refreshes only active accounts in one wallet with at most ten concurrent requests', async () => {
    const group = wallets.restoreGroup('局部刷新钱包', MNEMONIC, 22)
    wallets.restoreGroup('其他钱包', 'legal winner thank year wave sausage worth useful legal winner thank yellow', 1)
    wallets.archive(group.accounts[0].id)
    const targetIds = new Set(group.accounts.slice(1).map((account) => account.id))
    const originalGetBalances = gateway.getBalances.bind(gateway)
    let active = 0
    let peak = 0
    const calls: string[] = []
    gateway.getBalances = async (address: string) => {
      active += 1
      peak = Math.max(peak, active)
      calls.push(address)
      await new Promise((resolve) => setTimeout(resolve, 5))
      try {
        return await originalGetBalances(address)
      } finally {
        active -= 1
      }
    }

    const refreshed = await wallets.refreshGroup(group.id)
    expect(refreshed.accounts).toHaveLength(21)
    expect(calls).toHaveLength(21)
    expect(calls.every((address) => [...targetIds].some((id) => wallets.get(id).address === address))).toBe(true)
    expect(peak).toBe(BALANCE_REFRESH_CONCURRENCY)
  })

  it('encrypts revealed secrets to an ephemeral RSA key and audits the operation', () => {
    const group = wallets.restoreGroup('秘密钱包', MNEMONIC, 1)
    const { publicKey, privateKey } = generateKeyPairSync('rsa', { modulusLength: 2048 })
    const response = wallets.revealMnemonicEncrypted(group.id, publicKey.export({ type: 'spki', format: 'pem' }).toString())
    expect(JSON.stringify(response)).not.toContain(MNEMONIC)
    const plaintext = privateDecrypt({ key: privateKey, padding: constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256' }, Buffer.from(response.ciphertext, 'base64')).toString()
    expect(plaintext).toBe(MNEMONIC)
    expect(db.prepare(`SELECT COUNT(*) AS count FROM audit_events WHERE kind = 'wallet_group.mnemonic_revealed'`).get()).toEqual({ count: 1 })
  })

  it('neutralizes spreadsheet formulas in exported address labels', () => {
    const group = wallets.restoreGroup('导出钱包', MNEMONIC, 1)
    wallets.renameGroup(group.id, '=HYPERLINK("https://example.invalid")')
    wallets.rename(group.accounts[0].id, '+SUM(1,1)')
    const csv = wallets.addressCsv()
    expect(csv).toContain(`"'=HYPERLINK(""https://example.invalid"")"`)
    expect(csv).toContain(`"'+SUM(1,1)"`)
  })

  it('lets every account use a trimmed local alias and audits the change', () => {
    const derived = wallets.restoreGroup('别名钱包', MNEMONIC, 1).accounts[0]
    const standalone = wallets.importPrivateKey('独立账户', Account.generate().privateKey.toString())

    expect(wallets.rename(derived.id, '  日常付款  ').label).toBe('日常付款')
    expect(wallets.rename(standalone.id, '归集账户').label).toBe('归集账户')
    expect(() => wallets.rename(derived.id, '   ')).toThrow('账户别名不能为空')
    expect(() => wallets.rename(derived.id, '包含\n换行')).toThrow('账户别名不能包含控制字符')
    expect(db.prepare(`SELECT COUNT(*) AS count FROM audit_events WHERE kind = 'wallet.renamed'`).get()).toEqual({ count: 2 })
  })

  it('migrates legacy mnemonic rows without changing wallet ids', () => {
    const path = APTOS_HD_PATH(1)
    const account = Account.fromDerivationPath({ mnemonic: MNEMONIC, path })
    const id = '11111111-1111-4111-8111-111111111111'
    const now = new Date().toISOString()
    const envelope = vault.encryptSecret({ privateKey: account.privateKey.toString(), mnemonic: MNEMONIC, derivationPath: path })
    db.prepare(`INSERT INTO wallets(id,label,address,source,derivation_path,secret_envelope,created_at,updated_at) VALUES (?, ?, ?, 'mnemonic', ?, ?, ?, ?)`)
      .run(id, '旧派生地址', account.accountAddress.toStringLong(), path, envelope, now, now)
    account.privateKey.clear()

    wallets.migrateLegacyMnemonicWallets()
    const migrated = wallets.get(id)
    expect(migrated.id).toBe(id)
    expect(migrated.accountIndex).toBe(1)
    expect(wallets.listGroups()[0].derivationProfile).toBe('aptos_hd')
    expect(wallets.listGroups()[0].nextAccountIndex).toBe(2)
    expect((db.prepare('SELECT secret_envelope FROM wallets WHERE id = ?').get(id) as { secret_envelope: string }).secret_envelope).toBe('')
  })

  it('recognizes standard high-index paths previously classified as custom', () => {
    const group = wallets.restoreGroup('高位账户', MNEMONIC, 0, [1000])
    const account = group.accounts[0]
    db.prepare(`UPDATE wallet_groups SET derivation_profile = 'legacy_custom', next_account_index = 0 WHERE id = ?`).run(group.id)
    db.prepare('UPDATE wallets SET account_index = NULL WHERE id = ?').run(account.id)

    wallets.migrateLegacyMnemonicWallets()
    const migrated = wallets.getGroup(group.id)
    expect(migrated.derivationProfile).toBe('aptos_hd')
    expect(migrated.accounts[0].id).toBe(account.id)
    expect(migrated.accounts[0].accountIndex).toBe(1000)
    expect(migrated.nextAccountIndex).toBe(1001)
  })

  it('keeps nonstandard derivation paths in read-only legacy groups', () => {
    // Public deterministic test vector. Never use it for a funded wallet.
    const mnemonic = 'bean mountain minute enemy state always weekend accuse flag wait island tortoise' // gitleaks:allow
    const path = "m/44'/637'/0'/1'/0'"
    const account = Account.fromDerivationPath({ mnemonic, path })
    const groupId = '22222222-2222-4222-8222-222222222222'
    const walletId = '33333333-3333-4333-8333-333333333333'
    const now = new Date().toISOString()
    db.prepare(`INSERT INTO wallet_groups(id,label,source,derivation_profile,secret_envelope,mnemonic_fingerprint,created_at,updated_at) VALUES (?,?,'mnemonic','legacy_custom',?,?,?,?)`)
      .run(groupId, '旧自定义钱包', vault.encryptMnemonic(mnemonic), vault.mnemonicFingerprint(mnemonic), now, now)
    db.prepare(`INSERT INTO wallets(id,label,address,source,group_id,account_index,account_status,derivation_path,secret_envelope,created_at,updated_at) VALUES (?,? ,?,'mnemonic',?,NULL,'used',?,'',?,?)`)
      .run(walletId, '自定义账户', account.accountAddress.toStringLong(), groupId, path, now, now)
    account.privateKey.clear()

    wallets.migrateLegacyMnemonicWallets()
    expect(wallets.getGroup(groupId).derivationProfile).toBe('legacy_custom')
    expect(wallets.get(walletId).accountIndex).toBeNull()
    expect(() => wallets.addAccounts(groupId, 1)).toThrow('不能添加 HD 账户')
  })

  it('exports v2 backups and restores legacy v1 group fields', () => {
    const original = wallets.restoreGroup('备份钱包', MNEMONIC, 2)
    const backup = new BackupService(db).export()
    expect(backup.version).toBe(2)
    expect(JSON.stringify(backup)).not.toContain(MNEMONIC)
    expect(JSON.stringify(backup)).not.toContain('scan_ranges_json')

    const legacy = structuredClone(backup) as VaultBackup
    legacy.version = 1
    const groupRow = legacy.tables.wallet_groups[0]
    groupRow.derivation_profile = 'okx_aptos'
    groupRow.scan_ranges_json = '[[0,199]]'
    delete groupRow.next_account_index

    const restoredPath = join(mkdtempSync(join(tmpdir(), 'aptos-hd-restore-')), 'wallet.sqlite')
    const restoredDb = openDatabase(restoredPath)
    try {
      new BackupService(restoredDb).restore(legacy)
      const restoredVault = new EncryptedVault(restoredDb)
      return restoredVault.unlock('correct horse battery staple').then(() => {
        const restoredWallets = new WalletService(restoredDb, restoredVault, new FakeGateway())
        const restored = restoredWallets.listGroups()[0]
        expect(restored.id).toBe(original.id)
        expect(restored.derivationProfile).toBe('aptos_hd')
        expect(restored.nextAccountIndex).toBe(2)
        restoredVault.lock()
      }).finally(() => restoredDb.close())
    } catch (error) {
      restoredDb.close()
      throw error
    }
  })
})
