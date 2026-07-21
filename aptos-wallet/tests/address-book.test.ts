import { mkdtempSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import { afterEach, beforeEach, describe, expect, it } from 'vitest'
import { AddressBookService } from '../server/address-book.js'
import { BackupService } from '../server/backup.js'
import { openDatabase, type SqliteDatabase } from '../server/database.js'

let db: SqliteDatabase
let addressBook: AddressBookService

beforeEach(() => {
  db = openDatabase(join(mkdtempSync(join(tmpdir(), 'aptos-address-book-')), 'wallet.sqlite'))
  addressBook = new AddressBookService(db)
})

afterEach(() => db.close())

describe('local address book', () => {
  it('normalizes addresses and supports audited create, update, and delete', () => {
    const firstAddress = `0x${'a'.repeat(64)}`
    const secondAddress = `0x${'b'.repeat(64)}`
    const created = addressBook.create('  交易所充值  ', firstAddress)
    expect(created.label).toBe('交易所充值')
    expect(created.address).toBe(firstAddress)
    expect(addressBook.list()).toEqual([created])

    const updated = addressBook.update(created.id, '交易所主账户', secondAddress)
    expect(updated).toMatchObject({ id: created.id, label: '交易所主账户' })
    expect(updated.address).toBe(secondAddress)

    addressBook.remove(created.id)
    expect(addressBook.list()).toEqual([])
    expect(db.prepare("SELECT kind FROM audit_events WHERE kind LIKE 'address_book.%' ORDER BY rowid").all())
      .toEqual([{ kind: 'address_book.created' }, { kind: 'address_book.updated' }, { kind: 'address_book.deleted' }])
  })

  it('rejects invalid, duplicate, and locally managed addresses', () => {
    expect(() => addressBook.create('错误', 'not-an-address')).toThrow('Aptos 地址格式无效')
    const entry = addressBook.create('常用地址', `0x${'1'.repeat(64)}`)
    expect(() => addressBook.create('重复地址', entry.address)).toThrow('已存在于地址簿')

    const now = new Date().toISOString()
    const managedAddress = `0x${'4'.repeat(64)}`
    db.prepare(`INSERT INTO wallets(id, label, address, source, secret_envelope, created_at, updated_at)
      VALUES (?, '本机账户', ?, 'private_key', '{}', ?, ?)`)
      .run(crypto.randomUUID(), managedAddress, now, now)
    expect(() => addressBook.create('不应重复', managedAddress)).toThrow('已经是本机钱包账户')
  })

  it('includes address book entries in encrypted vault backups', () => {
    const created = addressBook.create('备份地址', `0x${'5'.repeat(64)}`)
    const backup = new BackupService(db).export()
    expect(backup.tables.address_book_entries).toEqual([expect.objectContaining({ id: created.id, label: '备份地址', address: created.address })])
  })
})
