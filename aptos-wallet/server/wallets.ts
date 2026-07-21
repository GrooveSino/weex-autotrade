import {
  constants,
  createPublicKey,
  publicEncrypt,
  randomUUID,
} from 'node:crypto'
import {
  Account,
  AccountAddress,
  type Ed25519Account,
  Ed25519PrivateKey,
} from '@aptos-labs/ts-sdk'
import { validateMnemonic } from '@scure/bip39'
import { wordlist } from '@scure/bip39/wordlists/english.js'
import { formatAmount } from '../shared/amounts.js'
import type {
  AssetBalance,
  DerivedAccountPreview,
  EncryptedSecretResponse,
  WalletAccountStatus,
  WalletDerivationProfile,
  WalletGroup,
  WalletRecord,
  WalletSource,
} from '../shared/types.js'
import type { SqliteDatabase } from './database.js'
import type { ReadPriority } from './aptos-gateway.js'
import { EncryptedVault, type WalletSecret } from './vault.js'

export const MAX_ACCOUNT_INDEX = 0x7fffffff
export const MAX_ACCOUNT_BATCH = 200
// Keep wallet refreshes below the public fullnode's anonymous request budget.
// The gateway has a second global limiter; matching it here avoids building a
// large burst of queued account reads during a full refresh.
export const BALANCE_REFRESH_CONCURRENCY = 2
export const BALANCE_REFRESH_MAX_ATTEMPTS = 3
export const BALANCE_REFRESH_RETRY_DELAY_MS = 250
export const APTOS_HD_PATH = (index: number) => `m/44'/637'/${index}'/0'/0'`
const APTOS_HD_PATH_PATTERN = /^m\/44'\/637'\/(\d+)'\/0'\/0'$/
const ACTIVE_JOB_STATUSES = ['draft', 'previewed', 'running', 'paused', 'uncertain'] as const

interface WalletRow {
  id: string
  label: string
  address: string
  source: WalletSource
  group_id: string | null
  account_index: number | null
  account_status: WalletAccountStatus
  derivation_path: string | null
  secret_envelope: string
  balances_json: string
  balance_error: string | null
  balance_updated_at: string | null
  archived_at: string | null
  created_at: string
  updated_at: string
}

interface WalletGroupRow {
  id: string
  label: string
  source: 'mnemonic'
  derivation_profile: WalletDerivationProfile
  secret_envelope: string
  mnemonic_fingerprint: string
  next_account_index: number
  archived_at: string | null
  created_at: string
  updated_at: string
}

export interface BalanceReader {
  getBalances(address: string, priority?: ReadPriority): Promise<AssetBalance[]>
  accountExists(address: string, priority?: ReadPriority): Promise<boolean>
}

function normalizeAddress(address: string): string {
  return AccountAddress.fromString(address).toStringLong()
}

function normalizeMnemonic(input: string, requiredWords?: 24): string {
  const mnemonic = input.trim().toLowerCase().split(/\s+/).join(' ')
  const count = mnemonic ? mnemonic.split(' ').length : 0
  if (requiredWords && count !== requiredWords) throw new Error('新钱包必须使用 24 词助记词')
  if (count !== 12 && count !== 24) throw new Error('助记词必须是 12 或 24 个英文单词')
  if (!validateMnemonic(mnemonic, wordlist)) throw new Error('助记词校验失败')
  return mnemonic
}

function project(row: WalletRow): WalletRecord {
  return {
    id: row.id,
    label: row.label,
    address: row.address,
    source: row.source,
    groupId: row.group_id ?? null,
    accountIndex: row.account_index ?? null,
    accountStatus: row.account_status ?? 'standalone',
    derivationPath: row.derivation_path,
    createdAt: row.created_at,
    balances: JSON.parse(row.balances_json) as AssetBalance[],
    balanceError: row.balance_error,
    balanceUpdatedAt: row.balance_updated_at,
    archivedAt: row.archived_at,
  }
}

function isFunded(balances: AssetBalance[]): boolean {
  return balances.some((balance) => BigInt(balance.baseUnits) > 0n)
}

function safeError(error: unknown): string {
  const message = error instanceof Error ? error.message : '钱包操作失败'
  return message
    .replace(/ed25519-priv-[^\s"']+/gi, '[REDACTED]')
    .replace(/\b0x[0-9a-f]{64}\b/gi, '[ADDRESS]')
    .slice(0, 200)
}

function validateIndex(index: number): void {
  if (!Number.isInteger(index) || index < 0 || index > MAX_ACCOUNT_INDEX) {
    throw new Error(`账户索引必须在 0 到 ${MAX_ACCOUNT_INDEX} 之间`)
  }
}

function normalizeIndexes(accountCount: number, explicitIndexes: number[]): number[] {
  if (!Number.isInteger(accountCount) || accountCount < 0 || accountCount > MAX_ACCOUNT_BATCH) {
    throw new Error(`连续账户数量必须在 0 到 ${MAX_ACCOUNT_BATCH} 之间`)
  }
  const explicit = explicitIndexes.map(Number)
  explicit.forEach(validateIndex)
  if (new Set(explicit).size !== explicit.length) throw new Error('账户索引不能重复')
  const indexes = [...new Set([...Array.from({ length: accountCount }, (_, index) => index), ...explicit])].sort((a, b) => a - b)
  if (!indexes.length) throw new Error('至少需要选择一个账户索引')
  if (indexes.length > MAX_ACCOUNT_BATCH) throw new Error(`单次最多处理 ${MAX_ACCOUNT_BATCH} 个账户`)
  return indexes
}

export class WalletService {
  private readonly activeRefreshes = new Map<string, Promise<WalletRecord>>()

  constructor(
    private readonly db: SqliteDatabase,
    private readonly vault: EncryptedVault,
    private readonly balanceReader: BalanceReader,
  ) {}

  list(includeArchived = false): WalletRecord[] {
    const where = includeArchived ? '' : 'WHERE archived_at IS NULL'
    return (this.db.prepare(`SELECT * FROM wallets ${where} ORDER BY group_id IS NULL DESC, created_at ASC, account_index ASC`).all() as WalletRow[]).map(project)
  }

  listGroups(includeArchived = false): WalletGroup[] {
    const allWallets = this.list(true)
    const where = includeArchived ? '' : 'WHERE archived_at IS NULL'
    return (this.db.prepare(`SELECT * FROM wallet_groups ${where} ORDER BY created_at ASC`).all() as WalletGroupRow[])
      .map((row) => this.projectGroup(row, allWallets.filter((wallet) => wallet.groupId === row.id), includeArchived))
  }

  get(id: string): WalletRecord {
    return project(this.getRow(id))
  }

  getGroup(id: string, includeArchivedAccounts = true): WalletGroup {
    const row = this.getGroupRow(id)
    return this.projectGroup(row, this.list(true).filter((wallet) => wallet.groupId === id), includeArchivedAccounts)
  }

  importPrivateKey(label: string, privateKeyInput: string): WalletRecord {
    const privateKey = new Ed25519PrivateKey(privateKeyInput.trim())
    const account = Account.fromPrivateKey({ privateKey })
    try {
      const insert = this.db.prepare(`
        INSERT INTO wallets(id, label, address, source, derivation_path, secret_envelope, created_at, updated_at)
        VALUES (?, ?, ?, 'private_key', NULL, ?, ?, ?)
      `)
      const record = this.insertStandaloneAccount(insert, account, label, 'private_key')
      this.audit('wallet.imported', record.id, { source: 'private_key', address: record.address })
      return record
    } finally {
      privateKey.clear()
    }
  }

  previewRestore(mnemonicInput: string, accountCount = 1, explicitIndexes: number[] = []): DerivedAccountPreview[] {
    const mnemonic = normalizeMnemonic(mnemonicInput)
    return this.deriveAccounts(mnemonic, normalizeIndexes(accountCount, explicitIndexes))
  }

  createGroup(
    labelInput: string,
    mnemonicInput: string,
    confirmationIndexes: number[],
    confirmationWords: string[],
  ): WalletGroup {
    const label = this.validateLabel(labelInput)
    const mnemonic = normalizeMnemonic(mnemonicInput, 24)
    this.validateBackupConfirmation(mnemonic, confirmationIndexes, confirmationWords)
    return this.createNewGroup(label, mnemonic, [0], 'wallet_group.created')
  }

  restoreGroup(labelInput: string, mnemonicInput: string, accountCount = 1, explicitIndexes: number[] = []): WalletGroup {
    const label = this.validateLabel(labelInput)
    const mnemonic = normalizeMnemonic(mnemonicInput)
    const indexes = normalizeIndexes(accountCount, explicitIndexes)
    return this.createNewGroup(label, mnemonic, indexes, 'wallet_group.restored')
  }

  addAccounts(id: string, count: number): WalletGroup {
    if (!Number.isInteger(count) || count < 1 || count > MAX_ACCOUNT_BATCH) {
      throw new Error(`单次添加数量必须在 1 到 ${MAX_ACCOUNT_BATCH} 之间`)
    }
    const group = this.getGroupRow(id)
    this.assertActiveHdGroup(group)
    const end = group.next_account_index + count - 1
    validateIndex(end)
    const indexes = Array.from({ length: count }, (_, offset) => group.next_account_index + offset)
    this.persistDerivedAccounts(group, this.vault.decryptMnemonic(group.secret_envelope), indexes)
    this.audit('wallet_group.accounts_added', id, { indexes })
    return this.getGroup(id)
  }

  restoreAccounts(id: string, indexesInput: number[]): WalletGroup {
    if (!indexesInput.length || indexesInput.length > MAX_ACCOUNT_BATCH) throw new Error(`单次最多恢复 ${MAX_ACCOUNT_BATCH} 个账户`)
    const indexes = indexesInput.map(Number)
    indexes.forEach(validateIndex)
    if (new Set(indexes).size !== indexes.length) throw new Error('账户索引不能重复')
    indexes.sort((a, b) => a - b)
    const group = this.getGroupRow(id)
    this.assertActiveHdGroup(group)
    this.persistDerivedAccounts(group, this.vault.decryptMnemonic(group.secret_envelope), indexes)
    this.audit('wallet_group.accounts_restored', id, { indexes })
    return this.getGroup(id)
  }

  migrateLegacyMnemonicWallets(): void {
    this.normalizeExistingGroups()
    const rows = this.db.prepare(`SELECT * FROM wallets WHERE source = 'mnemonic' AND group_id IS NULL AND secret_envelope <> ''`).all() as WalletRow[]
    let migrated = 0
    for (const row of rows) {
      try {
        const legacy = this.vault.decryptSecret(row.secret_envelope)
        if (!legacy.mnemonic || !row.derivation_path) continue
        const mnemonic = normalizeMnemonic(legacy.mnemonic)
        const index = this.hdIndex(row.derivation_path)
        const profile: WalletDerivationProfile = index === null ? 'legacy_custom' : 'aptos_hd'
        const group = this.ensureMnemonicGroup(row.label, mnemonic, profile)
        this.db.transaction(() => {
          this.db.prepare(`UPDATE wallets SET group_id = ?, account_index = ?, account_status = ?, secret_envelope = '', updated_at = ? WHERE id = ?`)
            .run(group.id, index, isFunded(JSON.parse(row.balances_json) as AssetBalance[]) ? 'funded' : 'used', new Date().toISOString(), row.id)
          if (index !== null) this.bumpNextIndex(group.id, index + 1)
        })()
        migrated += 1
      } catch (error) {
        this.audit('wallet.migration_failed', row.id, { error: safeError(error) })
      }
    }
    if (migrated) this.audit('wallets.migrated_to_hd_groups', null, { count: migrated })
  }

  rename(id: string, label: string): WalletRecord {
    const result = this.db.prepare('UPDATE wallets SET label = ?, updated_at = ? WHERE id = ?')
      .run(this.validateLabel(label, '账户别名'), new Date().toISOString(), id)
    if (!result.changes) throw new Error('钱包不存在')
    this.audit('wallet.renamed', id, {})
    return this.get(id)
  }

  renameGroup(id: string, label: string): WalletGroup {
    const result = this.db.prepare('UPDATE wallet_groups SET label = ?, updated_at = ? WHERE id = ?')
      .run(this.validateLabel(label), new Date().toISOString(), id)
    if (!result.changes) throw new Error('钱包组不存在')
    this.audit('wallet_group.renamed', id, {})
    return this.getGroup(id)
  }

  archive(id: string): WalletRecord {
    const row = this.getRow(id)
    if (row.archived_at) return project(row)
    this.assertNoActiveJobForWallet(id)
    const now = new Date().toISOString()
    this.db.prepare('UPDATE wallets SET archived_at = ?, updated_at = ? WHERE id = ?').run(now, now, id)
    this.audit('wallet.archived', id, {})
    return this.get(id)
  }

  unarchive(id: string): WalletRecord {
    const row = this.getRow(id)
    if (row.group_id && this.getGroupRow(row.group_id).archived_at) throw new Error('请先恢复钱包组')
    const now = new Date().toISOString()
    this.db.prepare('UPDATE wallets SET archived_at = NULL, updated_at = ? WHERE id = ?').run(now, id)
    this.audit('wallet.unarchived', id, {})
    return this.get(id)
  }

  archiveGroup(id: string): WalletGroup {
    const group = this.getGroupRow(id)
    if (group.archived_at) return this.getGroup(id)
    this.assertNoActiveJobForGroup(id)
    const now = new Date().toISOString()
    this.db.transaction(() => {
      this.db.prepare('UPDATE wallet_groups SET archived_at = ?, updated_at = ? WHERE id = ?').run(now, now, id)
      this.db.prepare('UPDATE wallets SET archived_at = COALESCE(archived_at, ?), updated_at = ? WHERE group_id = ?').run(now, now, id)
    })()
    this.audit('wallet_group.archived', id, {})
    return this.getGroup(id)
  }

  unarchiveGroup(id: string): WalletGroup {
    const group = this.getGroupRow(id)
    if (!group.archived_at) return this.getGroup(id)
    const now = new Date().toISOString()
    this.db.transaction(() => {
      this.db.prepare('UPDATE wallet_groups SET archived_at = NULL, updated_at = ? WHERE id = ?').run(now, id)
      this.db.prepare('UPDATE wallets SET archived_at = NULL, updated_at = ? WHERE group_id = ? AND archived_at = ?').run(now, id, group.archived_at)
    })()
    this.audit('wallet_group.unarchived', id, {})
    return this.getGroup(id)
  }

  async refresh(id: string, priority: ReadPriority = 'normal'): Promise<WalletRecord> {
    const active = this.activeRefreshes.get(id)
    if (active) return active
    const operation = this.refreshOnce(id, priority)
    this.activeRefreshes.set(id, operation)
    try {
      return await operation
    } finally {
      if (this.activeRefreshes.get(id) === operation) this.activeRefreshes.delete(id)
    }
  }

  private async refreshOnce(id: string, priority: ReadPriority): Promise<WalletRecord> {
    const wallet = this.get(id)
    try {
      const balances = await retryBalanceRead(() => this.balanceReader.getBalances(wallet.address, priority))
      const exists = wallet.groupId ? (isFunded(balances) || await this.balanceReader.accountExists(wallet.address, priority)) : true
      const now = new Date().toISOString()
      const status: WalletAccountStatus = wallet.groupId ? (isFunded(balances) ? 'funded' : exists ? 'used' : 'unused') : 'standalone'
      this.db.prepare(`UPDATE wallets SET balances_json = ?, account_status = ?, balance_error = NULL, balance_updated_at = ?, updated_at = ? WHERE id = ?`)
        .run(JSON.stringify(balances), status, now, now, id)
    } catch (error) {
      const message = safeError(error)
      const now = new Date().toISOString()
      this.db.prepare('UPDATE wallets SET balance_error = ?, balance_updated_at = ?, updated_at = ? WHERE id = ?').run(message, now, now, id)
    }
    return this.get(id)
  }

  async refreshAll(): Promise<WalletRecord[]> {
    const ids = this.list().map((wallet) => wallet.id)
    await this.refreshIds(ids)
    return this.list()
  }

  async refreshGroup(id: string): Promise<WalletGroup> {
    const group = this.getGroupRow(id)
    if (group.archived_at) throw new Error('钱包已归档，不能刷新余额')
    const ids = (this.db.prepare('SELECT id FROM wallets WHERE group_id = ? AND archived_at IS NULL ORDER BY account_index, created_at').all(id) as Array<{ id: string }>)
      .map((wallet) => wallet.id)
    await this.refreshIds(ids)
    return this.getGroup(id, false)
  }

  private async refreshIds(ids: string[]): Promise<void> {
    let nextIndex = 0
    const worker = async () => {
      while (nextIndex < ids.length) {
        const id = ids[nextIndex++]
        await this.refresh(id)
      }
    }
    await Promise.all(Array.from({ length: Math.min(BALANCE_REFRESH_CONCURRENCY, ids.length) }, worker))
  }

  revealPrivateKeyEncrypted(id: string, publicKeyPem: string): EncryptedSecretResponse {
    const account = this.getAccount(id)
    try {
      const encrypted = this.encryptForBrowser(account.privateKey.toString(), publicKeyPem)
      this.audit('wallet.private_key_revealed', id, {})
      return encrypted
    } finally {
      account.privateKey.clear()
    }
  }

  revealMnemonicEncrypted(id: string, publicKeyPem: string): EncryptedSecretResponse {
    const group = this.getGroupRow(id)
    const encrypted = this.encryptForBrowser(this.vault.decryptMnemonic(group.secret_envelope), publicKeyPem)
    this.audit('wallet_group.mnemonic_revealed', id, {})
    return encrypted
  }

  getAccount(id: string): Ed25519Account {
    const row = this.getRow(id)
    if (row.group_id) {
      if (!row.derivation_path) throw new Error('派生地址缺少路径')
      const group = this.getGroupRow(row.group_id)
      const account = Account.fromDerivationPath({ mnemonic: this.vault.decryptMnemonic(group.secret_envelope), path: row.derivation_path })
      if (normalizeAddress(account.accountAddress.toString()) !== row.address) {
        account.privateKey.clear()
        throw new Error('派生地址校验失败')
      }
      return account
    }
    const secret = this.vault.decryptSecret(row.secret_envelope)
    return Account.fromPrivateKey({ privateKey: new Ed25519PrivateKey(secret.privateKey) })
  }

  getSecret(id: string): WalletSecret {
    const account = this.getAccount(id)
    try {
      return { privateKey: account.privateKey.toString(), derivationPath: this.getRow(id).derivation_path ?? undefined }
    } finally {
      account.privateKey.clear()
    }
  }

  addressCsv(): string {
    const groups = new Map(this.listGroups(true).map((group) => [group.id, group.label]))
    const quote = (value: string) => {
      const safe = /^[=+\-@\t\r]/.test(value) ? `'${value}` : value
      return `"${safe.replaceAll('"', '""')}"`
    }
    const header = ['group,label,address,source,account_index,derivation_path,archived_at']
    const rows = this.list(true).map((wallet) => [
      wallet.groupId ? groups.get(wallet.groupId) ?? '' : '',
      wallet.label,
      wallet.address,
      wallet.source,
      wallet.accountIndex?.toString() ?? '',
      wallet.derivationPath ?? '',
      wallet.archivedAt ?? '',
    ].map(quote).join(','))
    return [...header, ...rows].join('\n')
  }

  private createNewGroup(label: string, mnemonic: string, indexes: number[], auditKind: string): WalletGroup {
    const fingerprint = this.vault.mnemonicFingerprint(mnemonic)
    const existing = this.db.prepare('SELECT id FROM wallet_groups WHERE mnemonic_fingerprint = ?').get(fingerprint) as { id: string } | undefined
    if (existing) throw new Error(`该助记词钱包已存在（钱包组 ID: ${existing.id}）`)
    const derived = this.deriveAccounts(mnemonic, indexes)
    const id = randomUUID()
    const now = new Date().toISOString()
    this.db.transaction(() => {
      this.db.prepare(`
        INSERT INTO wallet_groups(id, label, source, derivation_profile, secret_envelope, mnemonic_fingerprint,
          next_account_index, created_at, updated_at)
        VALUES (?, ?, 'mnemonic', 'aptos_hd', ?, ?, ?, ?, ?)
      `).run(id, label, this.vault.encryptMnemonic(mnemonic), fingerprint, Math.max(...indexes) + 1, now, now)
      for (const account of derived) this.insertDerivedAccount(id, account, `账户 ${account.accountIndex + 1}`)
    })()
    this.audit(auditKind, id, { accountIndexes: indexes })
    return this.getGroup(id)
  }

  private persistDerivedAccounts(group: WalletGroupRow, mnemonic: string, indexes: number[]): void {
    const derived = this.deriveAccounts(mnemonic, indexes)
    const existingIndexes = new Set((this.db.prepare('SELECT account_index FROM wallets WHERE group_id = ? AND account_index IS NOT NULL').all(group.id) as Array<{ account_index: number }>).map((row) => row.account_index))
    const now = new Date().toISOString()
    this.db.transaction(() => {
      for (const account of derived) {
        if (!existingIndexes.has(account.accountIndex)) this.insertDerivedAccount(group.id, account, `账户 ${account.accountIndex + 1}`)
      }
      this.bumpNextIndex(group.id, Math.max(group.next_account_index, ...indexes.map((index) => index + 1)), now)
    })()
  }

  private deriveAccounts(mnemonic: string, indexes: number[]): DerivedAccountPreview[] {
    return indexes.map((accountIndex) => {
      validateIndex(accountIndex)
      const derivationPath = APTOS_HD_PATH(accountIndex)
      const account = Account.fromDerivationPath({ mnemonic, path: derivationPath })
      try {
        return { accountIndex, derivationPath, address: normalizeAddress(account.accountAddress.toString()) }
      } finally {
        account.privateKey.clear()
      }
    })
  }

  private insertDerivedAccount(groupId: string, account: DerivedAccountPreview, label: string): void {
    const now = new Date().toISOString()
    this.db.prepare(`
      INSERT INTO wallets(id, label, address, source, group_id, account_index, account_status, derivation_path,
        secret_envelope, balances_json, created_at, updated_at)
      VALUES (?, ?, ?, 'mnemonic', ?, ?, 'unused', ?, '', '[]', ?, ?)
    `).run(randomUUID(), label, account.address, groupId, account.accountIndex, account.derivationPath, now, now)
  }

  private insertStandaloneAccount(
    insert: ReturnType<SqliteDatabase['prepare']>,
    account: Ed25519Account,
    label: string,
    source: Exclude<WalletSource, 'mnemonic'>,
  ): WalletRecord {
    const id = randomUUID()
    const now = new Date().toISOString()
    const address = normalizeAddress(account.accountAddress.toString())
    const secret: WalletSecret = { privateKey: account.privateKey.toString() }
    const run = insert.run.bind(insert) as (...params: unknown[]) => unknown
    run(id, this.validateLabel(label), address, this.vault.encryptSecret(secret), now, now)
    return this.get(id)
  }

  private ensureMnemonicGroup(label: string, mnemonic: string, profile: WalletDerivationProfile): WalletGroupRow {
    const fingerprint = this.vault.mnemonicFingerprint(mnemonic)
    const existing = this.db.prepare('SELECT * FROM wallet_groups WHERE mnemonic_fingerprint = ?').get(fingerprint) as WalletGroupRow | undefined
    if (existing) return existing
    const id = randomUUID()
    const now = new Date().toISOString()
    this.db.prepare(`
      INSERT INTO wallet_groups(id, label, source, derivation_profile, secret_envelope, mnemonic_fingerprint,
        next_account_index, created_at, updated_at)
      VALUES (?, ?, 'mnemonic', ?, ?, ?, 0, ?, ?)
    `).run(id, this.validateLabel(label), profile, this.vault.encryptMnemonic(mnemonic), fingerprint, now, now)
    return this.getGroupRow(id)
  }

  private normalizeExistingGroups(): void {
    const groups = this.db.prepare('SELECT * FROM wallet_groups').all() as WalletGroupRow[]
    for (const group of groups) {
      const accounts = this.db.prepare('SELECT * FROM wallets WHERE group_id = ? ORDER BY created_at').all(group.id) as WalletRow[]
      const indexes = accounts.map((account) => account.derivation_path ? this.hdIndex(account.derivation_path) : null)
      const isHd = accounts.length > 0 && indexes.every((index) => index !== null)
      const profile: WalletDerivationProfile = isHd ? 'aptos_hd' : 'legacy_custom'
      const nextIndex = isHd ? Math.max(...indexes.map((index) => index!)) + 1 : 0
      this.db.transaction(() => {
        for (let offset = 0; offset < accounts.length; offset += 1) {
          if (indexes[offset] !== null && accounts[offset].account_index !== indexes[offset]) {
            this.db.prepare('UPDATE wallets SET account_index = ?, updated_at = ? WHERE id = ?')
              .run(indexes[offset], new Date().toISOString(), accounts[offset].id)
          }
        }
        this.db.prepare('UPDATE wallet_groups SET derivation_profile = ?, next_account_index = MAX(next_account_index, ?), updated_at = ? WHERE id = ?')
          .run(profile, nextIndex, new Date().toISOString(), group.id)
      })()
    }
  }

  private projectGroup(row: WalletGroupRow, allAccounts: WalletRecord[], includeArchivedAccounts: boolean): WalletGroup {
    const activeAccounts = allAccounts.filter((wallet) => !wallet.archivedAt)
    const accounts = includeArchivedAccounts ? allAccounts : activeAccounts
    const apt = activeAccounts.reduce((sum, wallet) => sum + BigInt(wallet.balances.find((item) => item.asset === 'APT')?.baseUnits ?? '0'), 0n)
    const usdt = activeAccounts.reduce((sum, wallet) => sum + BigInt(wallet.balances.find((item) => item.asset === 'USDT')?.baseUnits ?? '0'), 0n)
    return {
      id: row.id,
      label: row.label,
      source: row.source,
      derivationProfile: row.derivation_profile,
      nextAccountIndex: row.next_account_index,
      activeAccountCount: activeAccounts.length,
      totalAccountCount: allAccounts.length,
      archivedAt: row.archived_at,
      accounts,
      balances: [
        { asset: 'APT', baseUnits: apt.toString(), display: formatAmount(apt, 'APT') },
        { asset: 'USDT', baseUnits: usdt.toString(), display: formatAmount(usdt, 'USDT') },
      ],
      createdAt: row.created_at,
      updatedAt: row.updated_at,
    }
  }

  private validateBackupConfirmation(mnemonic: string, indexes: number[], words: string[]): void {
    if (indexes.length !== 4 || words.length !== 4 || new Set(indexes).size !== 4) throw new Error('必须确认 4 个不同的助记词位置')
    const mnemonicWords = mnemonic.split(' ')
    for (let offset = 0; offset < indexes.length; offset += 1) {
      const index = indexes[offset]
      if (!Number.isInteger(index) || index < 0 || index >= 24 || words[offset]?.trim().toLowerCase() !== mnemonicWords[index]) {
        throw new Error('助记词备份确认失败')
      }
    }
  }

  private encryptForBrowser(secret: string, publicKeyPem: string): EncryptedSecretResponse {
    const key = createPublicKey(publicKeyPem)
    if (key.asymmetricKeyType !== 'rsa' || (key.asymmetricKeyDetails?.modulusLength ?? 0) < 2048) {
      throw new Error('临时公钥必须是至少 2048 位的 RSA 公钥')
    }
    const ciphertext = publicEncrypt({ key, padding: constants.RSA_PKCS1_OAEP_PADDING, oaepHash: 'sha256' }, Buffer.from(secret, 'utf8')).toString('base64')
    return { algorithm: 'RSA-OAEP-256', ciphertext }
  }

  private assertActiveHdGroup(group: WalletGroupRow): void {
    if (group.archived_at) throw new Error('钱包组已归档')
    if (group.derivation_profile !== 'aptos_hd') throw new Error('旧自定义路径钱包不能添加 HD 账户')
  }

  private assertNoActiveJobForWallet(id: string): void {
    const placeholders = ACTIVE_JOB_STATUSES.map(() => '?').join(',')
    const referenced = this.db.prepare(`
      SELECT 1 FROM jobs WHERE status IN (${placeholders}) AND gas_payer_wallet_id = ?
      UNION SELECT 1 FROM job_steps JOIN jobs ON jobs.id = job_steps.job_id
        WHERE jobs.status IN (${placeholders}) AND (source_wallet_id = ? OR target_wallet_id = ?)
      LIMIT 1
    `).get(...ACTIVE_JOB_STATUSES, id, ...ACTIVE_JOB_STATUSES, id, id)
    if (referenced) throw new Error('账户仍被活动任务引用，不能归档')
  }

  private assertNoActiveJobForGroup(id: string): void {
    const walletIds = (this.db.prepare('SELECT id FROM wallets WHERE group_id = ?').all(id) as Array<{ id: string }>).map((row) => row.id)
    for (const walletId of walletIds) this.assertNoActiveJobForWallet(walletId)
  }

  private bumpNextIndex(groupId: string, nextIndex: number, now = new Date().toISOString()): void {
    this.db.prepare('UPDATE wallet_groups SET next_account_index = MAX(next_account_index, ?), updated_at = ? WHERE id = ?')
      .run(nextIndex, now, groupId)
  }

  private hdIndex(path: string): number | null {
    const match = APTOS_HD_PATH_PATTERN.exec(path)
    if (!match) return null
    const index = Number(match[1])
    return Number.isInteger(index) && index <= MAX_ACCOUNT_INDEX ? index : null
  }

  private validateLabel(label: string, fieldName = '钱包名称'): string {
    const normalized = label.trim()
    if (!normalized) throw new Error(`${fieldName}不能为空`)
    if (normalized.length > 120) throw new Error(`${fieldName}不能超过 120 个字符`)
    if (/\p{Cc}/u.test(normalized)) throw new Error(`${fieldName}不能包含控制字符`)
    return normalized
  }

  private getRow(id: string): WalletRow {
    const row = this.db.prepare('SELECT * FROM wallets WHERE id = ?').get(id) as WalletRow | undefined
    if (!row) throw new Error('钱包不存在')
    return row
  }

  private getGroupRow(id: string): WalletGroupRow {
    const row = this.db.prepare('SELECT * FROM wallet_groups WHERE id = ?').get(id) as WalletGroupRow | undefined
    if (!row) throw new Error('钱包组不存在')
    return row
  }

  private audit(kind: string, entityId: string | null, detail: Record<string, unknown>): void {
    this.db.prepare('INSERT INTO audit_events(id, kind, entity_id, detail_json, created_at) VALUES (?, ?, ?, ?, ?)')
      .run(randomUUID(), kind, entityId, JSON.stringify(detail), new Date().toISOString())
  }
}

async function retryBalanceRead<T>(action: () => Promise<T>): Promise<T> {
  let lastError: unknown
  for (let attempt = 1; attempt <= BALANCE_REFRESH_MAX_ATTEMPTS; attempt += 1) {
    try {
      return await action()
    } catch (error) {
      lastError = error
      if (attempt === BALANCE_REFRESH_MAX_ATTEMPTS) throw error
      await new Promise((resolve) => setTimeout(resolve, attempt * BALANCE_REFRESH_RETRY_DELAY_MS))
    }
  }
  throw lastError
}
