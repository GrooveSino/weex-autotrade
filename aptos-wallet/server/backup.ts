import { randomUUID } from 'node:crypto'
import type { SqliteDatabase } from './database.js'

const BACKUP_TABLES = ['vault_meta', 'wallet_groups', 'wallets', 'jobs', 'job_steps', 'transaction_attempts', 'chain_transfer_logs', 'chain_transfer_sync', 'address_book_entries', 'audit_events'] as const

export interface VaultBackup {
  format: 'aptos-local-wallet-backup'
  version: 1 | 2
  createdAt: string
  tables: Record<string, Record<string, unknown>[]>
}

export class BackupService {
  constructor(private readonly db: SqliteDatabase) {}

  export(): VaultBackup {
    const tables: Record<string, Record<string, unknown>[]> = {}
    for (const table of BACKUP_TABLES) {
      const rows = this.db.prepare(`SELECT * FROM ${table}`).all() as Record<string, unknown>[]
      tables[table] = table === 'wallet_groups'
        ? rows.map(({ scan_ranges_json: _obsolete, ...row }) => row)
        : rows
    }
    this.audit('vault.backup_exported')
    return { format: 'aptos-local-wallet-backup', version: 2, createdAt: new Date().toISOString(), tables }
  }

  restore(backup: VaultBackup): void {
    if (backup.format !== 'aptos-local-wallet-backup' || ![1, 2].includes(backup.version)) throw new Error('不支持的备份格式')
    if (this.db.prepare('SELECT 1 FROM vault_meta WHERE id = 1').get()) throw new Error('只能恢复到未初始化的空保险库')
    const tables = backup.tables
    if (!tables.vault_meta?.length) throw new Error('备份缺少保险库元数据')
    this.db.transaction(() => {
      for (const table of BACKUP_TABLES) {
        const rows = tables[table] ?? []
        const allowedColumns = new Set((this.db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>).map((column) => column.name))
        for (const original of rows) {
          const row = this.upgradeRow(table, original)
          const keys = Object.keys(row).filter((key) => allowedColumns.has(key))
          if (!keys.length) continue
          const placeholders = keys.map(() => '?').join(',')
          this.db.prepare(`INSERT INTO ${table} (${keys.join(',')}) VALUES (${placeholders})`).run(...keys.map((key) => row[key]))
        }
      }
      this.db.exec(`
        UPDATE wallet_groups
        SET next_account_index = MAX(next_account_index, COALESCE((
          SELECT MAX(account_index) + 1 FROM wallets
          WHERE wallets.group_id = wallet_groups.id AND account_index IS NOT NULL
        ), 0))
      `)
    })()
    this.audit('vault.backup_restored')
  }

  private upgradeRow(table: string, original: Record<string, unknown>): Record<string, unknown> {
    const row = { ...original }
    if (table === 'wallet_groups') {
      delete row.scan_ranges_json
      if (row.derivation_profile === 'okx_aptos') row.derivation_profile = 'aptos_hd'
      if (row.derivation_profile === 'custom') row.derivation_profile = 'legacy_custom'
      row.next_account_index ??= 0
      row.archived_at ??= null
    }
    if (table === 'wallets') row.archived_at ??= null
    return row
  }

  private audit(kind: string): void {
    this.db.prepare('INSERT INTO audit_events(id, kind, entity_id, detail_json, created_at) VALUES (?, ?, NULL, ?, ?)')
      .run(randomUUID(), kind, '{}', new Date().toISOString())
  }
}
