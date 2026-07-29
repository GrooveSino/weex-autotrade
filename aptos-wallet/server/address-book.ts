import { randomUUID } from 'node:crypto'
import { AccountAddress } from '@aptos-labs/ts-sdk'
import type { AddressBookEntry } from '../shared/types.js'
import type { SqliteDatabase } from './database.js'

const MAX_ADDRESS_BOOK_ENTRIES = 2_000

interface AddressBookRow {
  id: string
  label: string
  address: string
  created_at: string
  updated_at: string
}

function mapEntry(row: AddressBookRow): AddressBookEntry {
  return {
    id: row.id,
    label: row.label,
    address: row.address,
    createdAt: row.created_at,
    updatedAt: row.updated_at,
  }
}

export function normalizeAptosAddress(value: string): string {
  try {
    return AccountAddress.fromString(value.trim()).toStringLong()
  } catch {
    throw new Error('Aptos 地址格式无效')
  }
}

export class AddressBookService {
  constructor(private readonly db: SqliteDatabase) {}

  list(): AddressBookEntry[] {
    const rows = this.db.prepare('SELECT * FROM address_book_entries ORDER BY label COLLATE NOCASE ASC, created_at ASC').all() as AddressBookRow[]
    return rows.map(mapEntry)
  }

  create(label: string, address: string): AddressBookEntry {
    const normalizedLabel = normalizeLabel(label)
    const normalizedAddress = normalizeAptosAddress(address)
    this.assertCanUseAddress(normalizedAddress)
    const count = (this.db.prepare('SELECT COUNT(*) AS count FROM address_book_entries').get() as { count: number }).count
    if (count >= MAX_ADDRESS_BOOK_ENTRIES) throw new Error(`地址簿最多保存 ${MAX_ADDRESS_BOOK_ENTRIES} 个地址`)
    const id = randomUUID()
    const now = new Date().toISOString()
    this.db.transaction(() => {
      this.db.prepare('INSERT INTO address_book_entries(id, label, address, created_at, updated_at) VALUES (?, ?, ?, ?, ?)')
        .run(id, normalizedLabel, normalizedAddress, now, now)
      this.audit('address_book.created', id)
    })()
    return this.get(id)
  }

  createMany(entries: Array<{ label: string; address: string }>): AddressBookEntry[] {
    if (!entries.length) throw new Error('至少需要一条地址簿记录')
    const normalized = entries.map(({ label, address }) => ({
      id: randomUUID(),
      label: normalizeLabel(label),
      address: normalizeAptosAddress(address),
    }))
    const addresses = new Set<string>()
    for (const entry of normalized) {
      if (addresses.has(entry.address)) throw new Error('批量地址簿中存在重复地址')
      addresses.add(entry.address)
      this.assertCanUseAddress(entry.address)
    }
    const count = (this.db.prepare('SELECT COUNT(*) AS count FROM address_book_entries').get() as { count: number }).count
    if (count + normalized.length > MAX_ADDRESS_BOOK_ENTRIES) throw new Error(`地址簿最多保存 ${MAX_ADDRESS_BOOK_ENTRIES} 个地址`)
    const now = new Date().toISOString()
    this.db.transaction(() => {
      for (const entry of normalized) {
        this.db.prepare('INSERT INTO address_book_entries(id, label, address, created_at, updated_at) VALUES (?, ?, ?, ?, ?)')
          .run(entry.id, entry.label, entry.address, now, now)
        this.audit('address_book.created', entry.id)
      }
    })()
    return normalized.map((entry) => this.get(entry.id))
  }

  update(id: string, label: string, address: string): AddressBookEntry {
    this.get(id)
    const normalizedLabel = normalizeLabel(label)
    const normalizedAddress = normalizeAptosAddress(address)
    this.assertCanUseAddress(normalizedAddress, id)
    const now = new Date().toISOString()
    this.db.transaction(() => {
      this.db.prepare('UPDATE address_book_entries SET label = ?, address = ?, updated_at = ? WHERE id = ?')
        .run(normalizedLabel, normalizedAddress, now, id)
      this.audit('address_book.updated', id)
    })()
    return this.get(id)
  }

  remove(id: string): void {
    const entry = this.get(id)
    this.db.transaction(() => {
      this.audit('address_book.deleted', entry.id)
      this.db.prepare('DELETE FROM address_book_entries WHERE id = ?').run(entry.id)
    })()
  }

  get(id: string): AddressBookEntry {
    const row = this.db.prepare('SELECT * FROM address_book_entries WHERE id = ?').get(id) as AddressBookRow | undefined
    if (!row) throw new Error('地址簿条目不存在')
    return mapEntry(row)
  }

  private assertCanUseAddress(address: string, currentId?: string): void {
    if (this.db.prepare('SELECT 1 FROM wallets WHERE address = ? COLLATE NOCASE LIMIT 1').get(address)) {
      throw new Error('该地址已经是本机钱包账户，无需加入地址簿')
    }
    const existing = this.db.prepare('SELECT id FROM address_book_entries WHERE address = ? COLLATE NOCASE').get(address) as { id: string } | undefined
    if (existing && existing.id !== currentId) throw new Error('该地址已存在于地址簿')
  }

  private audit(kind: string, id: string): void {
    this.db.prepare('INSERT INTO audit_events(id, kind, entity_id, detail_json, created_at) VALUES (?, ?, ?, ?, ?)')
      .run(randomUUID(), kind, id, '{}', new Date().toISOString())
  }
}

function normalizeLabel(value: string): string {
  const label = value.trim()
  if (!label) throw new Error('地址别名不能为空')
  if (label.length > 120) throw new Error('地址别名不能超过 120 个字符')
  return label
}
