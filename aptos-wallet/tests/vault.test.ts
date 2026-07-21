import { mkdtempSync, readFileSync, statSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, describe, expect, it } from 'vitest'
import { openDatabase, type SqliteDatabase } from '../server/database.js'
import { EncryptedVault } from '../server/vault.js'

let db: SqliteDatabase | null = null
afterEach(() => { db?.close(); db = null })

describe('encrypted vault', () => {
  it('encrypts wallet material and supports password rotation', async () => {
    const path = join(mkdtempSync(join(tmpdir(), 'aptos-vault-')), 'wallet.sqlite')
    db = openDatabase(path)
    expect(statSync(path).mode & 0o777).toBe(0o600)
    const vault = new EncryptedVault(db)
    await vault.initialize('correct horse battery staple')
    const plaintext = 'ed25519-priv-0xabc123supersecret'
    const envelope = vault.encryptSecret({ privateKey: plaintext })
    expect(envelope).not.toContain(plaintext)
    db.prepare(`INSERT INTO wallets(id,label,address,source,secret_envelope,created_at,updated_at) VALUES ('w','w','0x1','generated',?,? ,?)`)
      .run(envelope, new Date().toISOString(), new Date().toISOString())
    expect(readFileSync(path).toString('utf8')).not.toContain(plaintext)
    await vault.changePassword('correct horse battery staple', 'another secure password')
    vault.lock()
    await expect(vault.unlock('correct horse battery staple')).rejects.toThrow('主密码错误')
    await vault.unlock('another secure password')
    expect(vault.decryptSecret(envelope).privateKey).toBe(plaintext)
    expect(statSync(path).mode & 0o777).toBe(0o600)
  })
})
