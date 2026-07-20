import { existsSync } from 'node:fs'
import { randomBytes, randomUUID } from 'node:crypto'
import { resolve } from 'node:path'
import Fastify, { type FastifyReply, type FastifyRequest } from 'fastify'
import cookie from '@fastify/cookie'
import fastifyStatic from '@fastify/static'
import { z } from 'zod'
import type { JobDraftInput } from '../shared/types.js'
import { AptosMainnetGateway, type ChainGateway } from './aptos-gateway.js'
import { BackupService, type VaultBackup } from './backup.js'
import type { AppConfig } from './config.js'
import { openDatabase, type SqliteDatabase } from './database.js'
import { JobService } from './jobs.js'
import { EncryptedVault, VaultLockedError } from './vault.js'
import { WalletService } from './wallets.js'

const passwordSchema = z.object({ password: z.string().min(12).max(1024) })
const walletIdParams = z.object({ id: z.string().uuid() })
const walletGroupIdParams = z.object({ id: z.string().uuid() })
const jobIdParams = z.object({ id: z.string().uuid() })
const accountIndexSchema = z.number().int().min(0).max(0x7fffffff)
const restoreSelectionSchema = z.object({
  accountCount: z.number().int().min(0).max(200).default(1),
  accountIndexes: z.array(accountIndexSchema).max(200).default([]),
})
const browserPublicKeySchema = z.string().min(400).max(2048)
const stepSchema = z.object({
  id: z.string().uuid().optional().default(() => randomUUID()),
  sourceWalletId: z.string().uuid(),
  targetAddress: z.string().min(1).max(128),
  targetWalletId: z.string().uuid().nullable().default(null),
  asset: z.enum(['APT', 'USDT']),
  amountMode: z.enum(['fixed', 'random', 'max']),
  amountMin: z.string().nullable().default(null),
  amountMax: z.string().nullable().default(null),
})
const jobDraftSchema = z.object({
  name: z.string().min(1).max(120),
  steps: z.array(stepSchema).min(1).max(1000),
  gasPayerWalletId: z.string().uuid().nullable().default(null),
  intervalMinSeconds: z.number().int().min(0).max(604800),
  intervalMaxSeconds: z.number().int().min(0).max(604800),
  shuffle: z.boolean(),
})

export interface AppServices {
  db: SqliteDatabase
  vault: EncryptedVault
  wallets: WalletService
  jobs: JobService
  backup: BackupService
  gateway: ChainGateway
}

export async function createApp(config: AppConfig, overrides: Partial<AppServices> = {}) {
  const db = overrides.db ?? openDatabase(config.databasePath)
  const vault = overrides.vault ?? new EncryptedVault(db)
  const gateway = overrides.gateway ?? new AptosMainnetGateway(config)
  const wallets = overrides.wallets ?? new WalletService(db, vault, gateway)
  const jobs = overrides.jobs ?? new JobService(db, wallets, gateway, config.executionEnabled)
  const backup = overrides.backup ?? new BackupService(db)
  const csrfToken = randomBytes(24).toString('base64url')
  let sessionToken: string | null = null
  const sseClients = new Set<NodeJS.WritableStream>()
  const app = Fastify({ logger: false, bodyLimit: 12 * 1024 * 1024 })
  await app.register(cookie)

  const allowedOrigins = new Set([config.webOrigin, `http://${config.host}:${config.port}`])
  app.addHook('onRequest', async (request, reply) => {
    if (!request.url.startsWith('/api/')) return
    const origin = request.headers.origin
    if (origin && !allowedOrigins.has(origin)) return reply.code(403).send({ error: '不允许的请求来源' })
    if (!['GET', 'HEAD', 'OPTIONS'].includes(request.method) && request.headers['x-csrf-token'] !== csrfToken) {
      return reply.code(403).send({ error: 'CSRF 校验失败' })
    }
  })

  app.setErrorHandler((error, _request, reply) => {
    const normalized = error instanceof Error ? error : new Error(String(error))
    const status = normalized instanceof z.ZodError ? 400
      : normalized instanceof VaultLockedError ? 423
        : /不存在/.test(normalized.message) ? 404
          : /已存在/.test(normalized.message) ? 409 : 400
    reply.code(status).send({ error: safeMessage(normalized) })
  })

  const setSession = (reply: FastifyReply) => {
    sessionToken = randomBytes(32).toString('base64url')
    reply.setCookie('aptos_wallet_session', sessionToken, {
      httpOnly: true,
      sameSite: 'strict',
      secure: false,
      path: '/',
    })
  }
  const requireSession = async (request: FastifyRequest, reply: FastifyReply) => {
    if (!sessionToken || request.cookies.aptos_wallet_session !== sessionToken || !vault.unlocked) {
      return reply.code(423).send({ error: '保险库未解锁' })
    }
  }
  const broadcast = () => {
    const payload = `event: snapshot\ndata: ${JSON.stringify({ wallets: vault.unlocked ? wallets.list() : [], groups: vault.unlocked ? wallets.listGroups() : [], jobs: vault.unlocked ? jobs.list() : [] })}\n\n`
    for (const client of sseClients) client.write(payload)
  }
  jobs.on('change', broadcast)

  app.get('/api/v1/status', async () => ({
    initialized: vault.initialized,
    unlocked: vault.unlocked,
    executionEnabled: config.executionEnabled,
    network: 'mainnet',
    csrfToken,
  }))

  app.post('/api/v1/vault/initialize', async (request, reply) => {
    const { password } = passwordSchema.parse(request.body)
    await vault.initialize(password)
    setSession(reply)
    return { ok: true }
  })
  app.post('/api/v1/vault/unlock', async (request, reply) => {
    const { password } = passwordSchema.parse(request.body)
    await vault.unlock(password)
    wallets.migrateLegacyMnemonicWallets()
    setSession(reply)
    return { ok: true }
  })
  app.post('/api/v1/vault/lock', { preHandler: requireSession }, async (_request, reply) => {
    vault.lock()
    sessionToken = null
    reply.clearCookie('aptos_wallet_session', { path: '/' })
    return { ok: true }
  })
  app.post('/api/v1/vault/change-password', { preHandler: requireSession }, async (request) => {
    const body = z.object({ currentPassword: z.string().min(12), nextPassword: z.string().min(12) }).parse(request.body)
    await vault.changePassword(body.currentPassword, body.nextPassword)
    return { ok: true }
  })
  app.get('/api/v1/vault/backup', { preHandler: requireSession }, async (_request, reply) => {
    reply.header('Content-Disposition', `attachment; filename="aptos-wallet-${new Date().toISOString().slice(0, 10)}.json"`)
    return backup.export()
  })
  app.post('/api/v1/vault/restore', async (request) => {
    backup.restore(request.body as VaultBackup)
    return { ok: true }
  })

  app.get('/api/v1/wallets', { preHandler: requireSession }, async (request) => {
    const query = z.object({ includeArchived: z.enum(['true', 'false']).optional() }).parse(request.query)
    return wallets.list(query.includeArchived === 'true')
  })
  app.get('/api/v1/wallets/groups', { preHandler: requireSession }, async (request) => {
    const query = z.object({ includeArchived: z.enum(['true', 'false']).optional() }).parse(request.query)
    return wallets.listGroups(query.includeArchived === 'true')
  })
  app.post('/api/v1/wallets/import/private-key', { preHandler: requireSession }, async (request) => {
    const body = z.object({ label: z.string().min(1).max(120), privateKey: z.string().min(32).max(256) }).parse(request.body)
    const result = wallets.importPrivateKey(body.label, body.privateKey)
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/groups/create', { preHandler: requireSession }, async (request) => {
    const body = z.object({
      label: z.string().min(1).max(120),
      mnemonic: z.string().min(20).max(1000),
      confirmationIndexes: z.array(z.number().int().min(0).max(23)).length(4),
      confirmationWords: z.array(z.string().min(1).max(20)).length(4),
    }).parse(request.body)
    const result = wallets.createGroup(body.label, body.mnemonic, body.confirmationIndexes, body.confirmationWords)
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/groups/restore/preview', { preHandler: requireSession }, async (request) => {
    const body = z.object({ mnemonic: z.string().min(20).max(1000) }).and(restoreSelectionSchema).parse(request.body)
    return { accounts: wallets.previewRestore(body.mnemonic, body.accountCount, body.accountIndexes) }
  })
  app.post('/api/v1/wallets/groups/restore', { preHandler: requireSession }, async (request) => {
    const body = z.object({ label: z.string().min(1).max(120), mnemonic: z.string().min(20).max(1000) }).and(restoreSelectionSchema).parse(request.body)
    const result = wallets.restoreGroup(body.label, body.mnemonic, body.accountCount, body.accountIndexes)
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/groups/:id/accounts', { preHandler: requireSession }, async (request) => {
    const { id } = walletGroupIdParams.parse(request.params)
    const { count } = z.object({ count: z.number().int().min(1).max(200) }).parse(request.body)
    const result = wallets.addAccounts(id, count)
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/groups/:id/accounts/restore', { preHandler: requireSession }, async (request) => {
    const { id } = walletGroupIdParams.parse(request.params)
    const { accountIndexes } = z.object({ accountIndexes: z.array(accountIndexSchema).min(1).max(200) }).parse(request.body)
    const result = wallets.restoreAccounts(id, accountIndexes)
    broadcast()
    return result
  })
  app.patch('/api/v1/wallets/groups/:id', { preHandler: requireSession }, async (request) => {
    const { id } = walletGroupIdParams.parse(request.params)
    const { label } = z.object({ label: z.string().min(1).max(120) }).parse(request.body)
    const result = wallets.renameGroup(id, label)
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/groups/:id/archive', { preHandler: requireSession }, async (request) => {
    const { id } = walletGroupIdParams.parse(request.params)
    const body = z.object({ password: z.string().min(12).max(1024), confirmationName: z.string().min(1).max(120) }).parse(request.body)
    const group = wallets.getGroup(id)
    if (!await vault.verifyPassword(body.password)) throw new Error('主密码错误')
    if (body.confirmationName !== group.label) throw new Error('钱包名称确认不匹配')
    const result = wallets.archiveGroup(id)
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/groups/:id/unarchive', { preHandler: requireSession }, async (request) => {
    const result = wallets.unarchiveGroup(walletGroupIdParams.parse(request.params).id)
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/groups/:id/reveal-mnemonic', { preHandler: requireSession }, async (request) => {
    const { id } = walletGroupIdParams.parse(request.params)
    const body = z.object({
      password: z.string().min(12).max(1024),
      confirmationName: z.string().min(1).max(120),
      publicKey: browserPublicKeySchema,
    }).parse(request.body)
    const group = wallets.getGroup(id)
    if (!await vault.verifyPassword(body.password)) throw new Error('主密码错误')
    if (body.confirmationName !== group.label) throw new Error('钱包名称确认不匹配')
    return wallets.revealMnemonicEncrypted(id, body.publicKey)
  })
  app.patch('/api/v1/wallets/:id', { preHandler: requireSession }, async (request) => {
    const { id } = walletIdParams.parse(request.params)
    const { label } = z.object({ label: z.string().min(1).max(120) }).parse(request.body)
    const result = wallets.rename(id, label)
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/:id/archive', { preHandler: requireSession }, async (request) => {
    const result = wallets.archive(walletIdParams.parse(request.params).id)
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/:id/unarchive', { preHandler: requireSession }, async (request) => {
    const result = wallets.unarchive(walletIdParams.parse(request.params).id)
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/:id/refresh', { preHandler: requireSession }, async (request) => {
    const result = await wallets.refresh(walletIdParams.parse(request.params).id)
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/refresh-all', { preHandler: requireSession }, async () => {
    const result = await wallets.refreshAll()
    broadcast()
    return result
  })
  app.post('/api/v1/wallets/:id/reveal', { preHandler: requireSession }, async (request) => {
    const { id } = walletIdParams.parse(request.params)
    const body = z.object({
      password: z.string().min(12).max(1024),
      confirmationName: z.string().min(1).max(120),
      publicKey: browserPublicKeySchema,
    }).parse(request.body)
    if (!await vault.verifyPassword(body.password)) throw new Error('主密码错误')
    if (body.confirmationName !== wallets.get(id).label) throw new Error('账户名称确认不匹配')
    return wallets.revealPrivateKeyEncrypted(id, body.publicKey)
  })
  app.get('/api/v1/wallets/addresses.csv', { preHandler: requireSession }, async (_request, reply) => {
    reply.type('text/csv; charset=utf-8').header('Content-Disposition', 'attachment; filename="aptos-addresses.csv"')
    return wallets.addressCsv()
  })

  app.get('/api/v1/jobs', { preHandler: requireSession }, async () => jobs.list())
  app.get('/api/v1/jobs/:id', { preHandler: requireSession }, async (request) => jobs.get(jobIdParams.parse(request.params).id))
  app.post('/api/v1/jobs', { preHandler: requireSession }, async (request) => jobs.createDraft(jobDraftSchema.parse(request.body) as JobDraftInput))
  app.put('/api/v1/jobs/:id', { preHandler: requireSession }, async (request) => jobs.updateDraft(jobIdParams.parse(request.params).id, jobDraftSchema.parse(request.body) as JobDraftInput))
  app.post('/api/v1/jobs/:id/check', { preHandler: requireSession }, async (request) => jobs.checkAndPreview(jobIdParams.parse(request.params).id))
  app.post('/api/v1/jobs/:id/preview', { preHandler: requireSession }, async (request) => jobs.preview(jobIdParams.parse(request.params).id))
  app.post('/api/v1/jobs/:id/confirm', { preHandler: requireSession }, async (request) => {
    const { confirmation } = z.object({ confirmation: z.string().min(1).max(500) }).parse(request.body)
    return jobs.confirm(jobIdParams.parse(request.params).id, confirmation)
  })
  app.post('/api/v1/jobs/:id/pause', { preHandler: requireSession }, async (request) => jobs.pause(jobIdParams.parse(request.params).id))
  app.post('/api/v1/jobs/:id/resume', { preHandler: requireSession }, async (request) => jobs.resume(jobIdParams.parse(request.params).id))
  app.post('/api/v1/jobs/:id/cancel', { preHandler: requireSession }, async (request) => jobs.cancel(jobIdParams.parse(request.params).id))
  app.get('/api/v1/jobs/:id/attempts', { preHandler: requireSession }, async (request) => jobs.attempts(jobIdParams.parse(request.params).id))

  app.get('/api/v1/events', { preHandler: requireSession }, async (request, reply) => {
    reply.hijack()
    reply.raw.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache, no-transform',
      Connection: 'keep-alive',
    })
    sseClients.add(reply.raw)
    reply.raw.write(`event: snapshot\ndata: ${JSON.stringify({ wallets: wallets.list(), groups: wallets.listGroups(), jobs: jobs.list() })}\n\n`)
    const heartbeat = setInterval(() => reply.raw.write(': heartbeat\n\n'), 15_000)
    request.raw.on('close', () => {
      clearInterval(heartbeat)
      sseClients.delete(reply.raw)
    })
  })

  const webDist = resolve(process.cwd(), 'dist')
  if (existsSync(webDist)) {
    await app.register(fastifyStatic, { root: webDist, wildcard: false })
    app.get('/*', async (_request, reply) => reply.sendFile('index.html'))
  }

  app.addHook('onClose', async () => {
    vault.lock()
    db.close()
  })
  return Object.assign(app, { services: { db, vault, wallets, jobs, backup, gateway } })
}

function safeMessage(error: Error): string {
  return error.message.replace(/ed25519-priv-[^\s"']+/gi, '[REDACTED]').slice(0, 500)
}
