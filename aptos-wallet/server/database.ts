import { mkdirSync } from 'node:fs'
import { dirname } from 'node:path'
import Database from 'better-sqlite3'

export type SqliteDatabase = Database.Database

function ensureColumn(db: SqliteDatabase, table: string, column: string, definition: string): void {
  const columns = db.prepare(`PRAGMA table_info(${table})`).all() as Array<{ name: string }>
  if (!columns.some((item) => item.name === column)) db.exec(`ALTER TABLE ${table} ADD COLUMN ${column} ${definition}`)
}

export function openDatabase(path: string): SqliteDatabase {
  mkdirSync(dirname(path), { recursive: true })
  const db = new Database(path)
  db.pragma('foreign_keys = ON')
  db.pragma('journal_mode = WAL')
  db.pragma('synchronous = FULL')
  db.exec(`
    CREATE TABLE IF NOT EXISTS vault_meta (
      id INTEGER PRIMARY KEY CHECK (id = 1),
      salt TEXT NOT NULL,
      wrapped_dek TEXT NOT NULL,
      kdf_version INTEGER NOT NULL DEFAULT 1,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS wallet_groups (
      id TEXT PRIMARY KEY,
      label TEXT NOT NULL,
      source TEXT NOT NULL,
      derivation_profile TEXT NOT NULL,
      secret_envelope TEXT NOT NULL,
      mnemonic_fingerprint TEXT NOT NULL UNIQUE,
      scan_ranges_json TEXT NOT NULL DEFAULT '[]',
      next_account_index INTEGER NOT NULL DEFAULT 0,
      archived_at TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS wallets (
      id TEXT PRIMARY KEY,
      label TEXT NOT NULL,
      address TEXT NOT NULL UNIQUE,
      source TEXT NOT NULL,
      group_id TEXT,
      account_index INTEGER,
      account_status TEXT NOT NULL DEFAULT 'standalone',
      derivation_path TEXT,
      secret_envelope TEXT NOT NULL,
      balances_json TEXT NOT NULL DEFAULT '[]',
      balance_error TEXT,
      balance_updated_at TEXT,
      archived_at TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY(group_id) REFERENCES wallet_groups(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS jobs (
      id TEXT PRIMARY KEY,
      name TEXT NOT NULL,
      status TEXT NOT NULL,
      gas_payer_wallet_id TEXT,
      interval_min_seconds INTEGER NOT NULL,
      interval_max_seconds INTEGER NOT NULL,
      shuffle INTEGER NOT NULL,
      confirmation_phrase TEXT,
      summary_json TEXT,
      error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY(gas_payer_wallet_id) REFERENCES wallets(id) ON DELETE SET NULL
    );

    CREATE TABLE IF NOT EXISTS job_steps (
      id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL,
      position INTEGER NOT NULL,
      source_wallet_id TEXT NOT NULL,
      target_address TEXT NOT NULL,
      target_wallet_id TEXT,
      asset TEXT NOT NULL,
      amount_mode TEXT NOT NULL,
      amount_min TEXT,
      amount_max TEXT,
      frozen_amount_base_units TEXT,
      frozen_amount_display TEXT,
      wait_after_seconds INTEGER NOT NULL DEFAULT 0,
      status TEXT NOT NULL,
      tx_hash TEXT,
      error TEXT,
      updated_at TEXT NOT NULL,
      FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
      FOREIGN KEY(source_wallet_id) REFERENCES wallets(id),
      FOREIGN KEY(target_wallet_id) REFERENCES wallets(id) ON DELETE SET NULL,
      UNIQUE(job_id, position)
    );

    CREATE TABLE IF NOT EXISTS transaction_attempts (
      id TEXT PRIMARY KEY,
      job_id TEXT NOT NULL,
      step_id TEXT NOT NULL,
      sender_address TEXT NOT NULL,
      sequence_number TEXT,
      tx_hash TEXT,
      state TEXT NOT NULL,
      error TEXT,
      created_at TEXT NOT NULL,
      updated_at TEXT NOT NULL,
      FOREIGN KEY(job_id) REFERENCES jobs(id) ON DELETE CASCADE,
      FOREIGN KEY(step_id) REFERENCES job_steps(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS audit_events (
      id TEXT PRIMARY KEY,
      kind TEXT NOT NULL,
      entity_id TEXT,
      detail_json TEXT NOT NULL,
      created_at TEXT NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_steps_job_position ON job_steps(job_id, position);
    CREATE INDEX IF NOT EXISTS idx_attempts_job ON transaction_attempts(job_id, created_at);
    CREATE INDEX IF NOT EXISTS idx_audit_created ON audit_events(created_at);
  `)

  ensureColumn(db, 'wallets', 'group_id', 'TEXT REFERENCES wallet_groups(id) ON DELETE CASCADE')
  ensureColumn(db, 'wallets', 'account_index', 'INTEGER')
  ensureColumn(db, 'wallets', 'account_status', "TEXT NOT NULL DEFAULT 'standalone'")
  ensureColumn(db, 'wallets', 'archived_at', 'TEXT')
  ensureColumn(db, 'wallet_groups', 'next_account_index', 'INTEGER NOT NULL DEFAULT 0')
  ensureColumn(db, 'wallet_groups', 'archived_at', 'TEXT')
  db.exec("UPDATE wallet_groups SET derivation_profile = 'aptos_hd' WHERE derivation_profile = 'okx_aptos'")
  db.exec("UPDATE wallet_groups SET derivation_profile = 'legacy_custom' WHERE derivation_profile = 'custom'")
  db.exec(`
    UPDATE wallet_groups
    SET next_account_index = COALESCE((
      SELECT MAX(account_index) + 1 FROM wallets
      WHERE wallets.group_id = wallet_groups.id AND account_index IS NOT NULL
    ), 0)
    WHERE next_account_index <= COALESCE((
      SELECT MAX(account_index) FROM wallets
      WHERE wallets.group_id = wallet_groups.id AND account_index IS NOT NULL
    ), -1)
  `)
  db.exec('CREATE INDEX IF NOT EXISTS idx_wallets_group_index ON wallets(group_id, account_index)')
  db.exec('CREATE UNIQUE INDEX IF NOT EXISTS idx_wallets_group_account_unique ON wallets(group_id, account_index) WHERE group_id IS NOT NULL AND account_index IS NOT NULL')

  const recoveryTime = new Date().toISOString()
  db.prepare(`
    UPDATE jobs SET status = 'paused', error = '服务重启后需要人工恢复', updated_at = ?
    WHERE status = 'running'
  `).run(recoveryTime)
  db.prepare(`
    UPDATE job_steps SET status = 'uncertain', error = '服务在交易终态确认前重启', updated_at = ?
    WHERE status IN ('preparing', 'submitting')
  `).run(recoveryTime)
  return db
}
