import { createCipheriv, createDecipheriv, createHmac, randomBytes, scrypt as scryptCallback, timingSafeEqual } from 'node:crypto'
import type { SqliteDatabase } from './database.js'

const SCRYPT_OPTIONS = { N: 1 << 15, r: 8, p: 1, maxmem: 64 * 1024 * 1024 }

interface Envelope {
  version: 1
  iv: string
  tag: string
  ciphertext: string
}

interface VaultMetaRow {
  salt: string
  wrapped_dek: string
}

export interface WalletSecret {
  privateKey: string
  mnemonic?: string
  derivationPath?: string
}

interface MnemonicSecret {
  mnemonic: string
}

export class VaultLockedError extends Error {}

async function deriveKey(password: string, salt: Buffer): Promise<Buffer> {
  if (password.length < 12) throw new Error('主密码至少需要 12 个字符')
  return new Promise((resolve, reject) => {
    scryptCallback(password, salt, 32, SCRYPT_OPTIONS, (error, key) => {
      if (error) reject(error)
      else resolve(Buffer.from(key))
    })
  })
}

function encrypt(key: Buffer, plaintext: Buffer): string {
  const iv = randomBytes(12)
  const cipher = createCipheriv('aes-256-gcm', key, iv)
  const ciphertext = Buffer.concat([cipher.update(plaintext), cipher.final()])
  const envelope: Envelope = {
    version: 1,
    iv: iv.toString('base64'),
    tag: cipher.getAuthTag().toString('base64'),
    ciphertext: ciphertext.toString('base64'),
  }
  return JSON.stringify(envelope)
}

function decrypt(key: Buffer, serialized: string): Buffer {
  const envelope = JSON.parse(serialized) as Envelope
  if (envelope.version !== 1) throw new Error('不支持的密文版本')
  const decipher = createDecipheriv('aes-256-gcm', key, Buffer.from(envelope.iv, 'base64'))
  decipher.setAuthTag(Buffer.from(envelope.tag, 'base64'))
  return Buffer.concat([decipher.update(Buffer.from(envelope.ciphertext, 'base64')), decipher.final()])
}

export class EncryptedVault {
  private dek: Buffer | null = null

  constructor(private readonly db: SqliteDatabase) {}

  get initialized(): boolean {
    return Boolean(this.db.prepare('SELECT 1 FROM vault_meta WHERE id = 1').get())
  }

  get unlocked(): boolean {
    return this.dek !== null
  }

  async initialize(password: string): Promise<void> {
    if (this.initialized) throw new Error('保险库已经初始化')
    const salt = randomBytes(16)
    const kek = await deriveKey(password, salt)
    const dek = randomBytes(32)
    const now = new Date().toISOString()
    try {
      this.db.prepare('INSERT INTO vault_meta(id, salt, wrapped_dek, updated_at) VALUES (1, ?, ?, ?)')
        .run(salt.toString('base64'), encrypt(kek, dek), now)
      this.setDek(dek)
    } finally {
      kek.fill(0)
      dek.fill(0)
    }
  }

  async unlock(password: string): Promise<void> {
    const row = this.db.prepare('SELECT salt, wrapped_dek FROM vault_meta WHERE id = 1').get() as VaultMetaRow | undefined
    if (!row) throw new Error('保险库尚未初始化')
    const kek = await deriveKey(password, Buffer.from(row.salt, 'base64'))
    try {
      const dek = decrypt(kek, row.wrapped_dek)
      this.setDek(dek)
      dek.fill(0)
    } catch {
      throw new Error('主密码错误或保险库已损坏')
    } finally {
      kek.fill(0)
    }
  }

  lock(): void {
    this.dek?.fill(0)
    this.dek = null
  }

  async changePassword(currentPassword: string, nextPassword: string): Promise<void> {
    const row = this.db.prepare('SELECT salt, wrapped_dek FROM vault_meta WHERE id = 1').get() as VaultMetaRow | undefined
    if (!row) throw new Error('保险库尚未初始化')
    const oldKek = await deriveKey(currentPassword, Buffer.from(row.salt, 'base64'))
    let dek: Buffer
    try {
      dek = decrypt(oldKek, row.wrapped_dek)
    } catch {
      throw new Error('当前主密码错误')
    } finally {
      oldKek.fill(0)
    }
    const newSalt = randomBytes(16)
    const newKek = await deriveKey(nextPassword, newSalt)
    try {
      this.db.prepare('UPDATE vault_meta SET salt = ?, wrapped_dek = ?, updated_at = ? WHERE id = 1')
        .run(newSalt.toString('base64'), encrypt(newKek, dek), new Date().toISOString())
      this.setDek(dek)
    } finally {
      newKek.fill(0)
      dek.fill(0)
    }
  }

  async verifyPassword(password: string): Promise<boolean> {
    const row = this.db.prepare('SELECT salt, wrapped_dek FROM vault_meta WHERE id = 1').get() as VaultMetaRow | undefined
    if (!row) return false
    const kek = await deriveKey(password, Buffer.from(row.salt, 'base64'))
    try {
      const candidate = decrypt(kek, row.wrapped_dek)
      const current = this.requireDek()
      const valid = candidate.length === current.length && timingSafeEqual(candidate, current)
      candidate.fill(0)
      return valid
    } catch {
      return false
    } finally {
      kek.fill(0)
    }
  }

  encryptSecret(secret: WalletSecret): string {
    return encrypt(this.requireDek(), Buffer.from(JSON.stringify(secret), 'utf8'))
  }

  decryptSecret(envelope: string): WalletSecret {
    const plaintext = decrypt(this.requireDek(), envelope)
    try {
      return JSON.parse(plaintext.toString('utf8')) as WalletSecret
    } finally {
      plaintext.fill(0)
    }
  }

  encryptMnemonic(mnemonic: string): string {
    return encrypt(this.requireDek(), Buffer.from(JSON.stringify({ mnemonic } satisfies MnemonicSecret), 'utf8'))
  }

  decryptMnemonic(envelope: string): string {
    const plaintext = decrypt(this.requireDek(), envelope)
    try {
      const value = JSON.parse(plaintext.toString('utf8')) as MnemonicSecret
      if (!value.mnemonic) throw new Error('钱包组助记词密文无效')
      return value.mnemonic
    } finally {
      plaintext.fill(0)
    }
  }

  mnemonicFingerprint(mnemonic: string): string {
    return createHmac('sha256', this.requireDek()).update(mnemonic, 'utf8').digest('hex')
  }

  private requireDek(): Buffer {
    if (!this.dek) throw new VaultLockedError('保险库已锁定')
    return this.dek
  }

  private setDek(value: Buffer): void {
    this.lock()
    this.dek = Buffer.from(value)
  }
}
