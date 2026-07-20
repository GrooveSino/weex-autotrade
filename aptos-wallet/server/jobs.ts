import { EventEmitter } from 'node:events'
import { randomInt, randomUUID } from 'node:crypto'
import { AccountAddress } from '@aptos-labs/ts-sdk'
import { formatAmount, parseAmount, randomBigIntInclusive } from '../shared/amounts.js'
import {
  ASSETS,
  type AssetId,
  type FrozenTransferStep,
  type JobDraftInput,
  type JobPreflight,
  type JobStatus,
  type JobSummary,
  type StepStatus,
  type TransferStepCheck,
  type TransferJob,
} from '../shared/types.js'
import type { ChainGateway, TransferRequest } from './aptos-gateway.js'
import type { SqliteDatabase } from './database.js'
import { WalletService } from './wallets.js'

const GAS_RESERVE_BASE_UNITS = 100_000n

interface JobRow {
  id: string
  name: string
  status: JobStatus
  gas_payer_wallet_id: string | null
  interval_min_seconds: number
  interval_max_seconds: number
  shuffle: number
  confirmation_phrase: string | null
  summary_json: string | null
  error: string | null
  created_at: string
  updated_at: string
}

interface StepRow {
  id: string
  job_id: string
  position: number
  source_wallet_id: string
  target_address: string
  target_wallet_id: string | null
  asset: AssetId
  amount_mode: 'fixed' | 'random' | 'max'
  amount_min: string | null
  amount_max: string | null
  frozen_amount_base_units: string | null
  frozen_amount_display: string | null
  wait_after_seconds: number
  status: StepStatus
  tx_hash: string | null
  error: string | null
}

function mapStep(row: StepRow): FrozenTransferStep {
  return {
    id: row.id,
    position: row.position,
    sourceWalletId: row.source_wallet_id,
    targetAddress: row.target_address,
    targetWalletId: row.target_wallet_id,
    asset: row.asset,
    amountMode: row.amount_mode,
    amountMin: row.amount_min,
    amountMax: row.amount_max,
    frozenAmountBaseUnits: row.frozen_amount_base_units,
    frozenAmountDisplay: row.frozen_amount_display,
    waitAfterSeconds: row.wait_after_seconds,
    status: row.status,
    txHash: row.tx_hash,
    error: row.error,
  }
}

export class JobService extends EventEmitter {
  private worker: Promise<void> | null = null
  private pauseRequested = false
  private cancelRequested = false

  constructor(
    private readonly db: SqliteDatabase,
    private readonly wallets: WalletService,
    private readonly gateway: ChainGateway,
    private readonly executionEnabled: boolean,
  ) {
    super()
  }

  list(): TransferJob[] {
    return (this.db.prepare('SELECT * FROM jobs ORDER BY created_at DESC').all() as JobRow[]).map((row) => this.mapJob(row))
  }

  get(id: string): TransferJob {
    const row = this.db.prepare('SELECT * FROM jobs WHERE id = ?').get(id) as JobRow | undefined
    if (!row) throw new Error('任务不存在')
    return this.mapJob(row)
  }

  createDraft(input: JobDraftInput): TransferJob {
    validateDraft(input)
    const id = randomUUID()
    const now = new Date().toISOString()
    this.db.prepare(`
      INSERT INTO jobs(id, name, status, gas_payer_wallet_id, interval_min_seconds, interval_max_seconds, shuffle, created_at, updated_at)
      VALUES (?, ?, 'draft', ?, ?, ?, ?, ?, ?)
    `).run(id, input.name.trim(), input.gasPayerWalletId, input.intervalMinSeconds, input.intervalMaxSeconds, Number(input.shuffle), now, now)
    this.replaceSteps(id, input.steps.map((step, position) => ({ ...step, position })))
    this.audit('job.created', id, { stepCount: input.steps.length })
    return this.get(id)
  }

  updateDraft(id: string, input: JobDraftInput): TransferJob {
    validateDraft(input)
    const existing = this.get(id)
    if (existing.status !== 'draft' && existing.status !== 'previewed') throw new Error('只有草稿或预览任务可以编辑')
    const now = new Date().toISOString()
    this.db.prepare(`
      UPDATE jobs SET name = ?, status = 'draft', gas_payer_wallet_id = ?, interval_min_seconds = ?,
        interval_max_seconds = ?, shuffle = ?, confirmation_phrase = NULL, summary_json = NULL, error = NULL, updated_at = ?
      WHERE id = ?
    `).run(input.name.trim(), input.gasPayerWalletId, input.intervalMinSeconds, input.intervalMaxSeconds, Number(input.shuffle), now, id)
    this.replaceSteps(id, input.steps.map((step, position) => ({ ...step, position })))
    return this.get(id)
  }

  async preview(id: string): Promise<TransferJob> {
    const result = await this.checkAndPreview(id)
    if (!result.valid) throw new Error(result.checks.find((check) => !check.valid)?.error ?? '转账检查未通过')
    return result.job
  }

  async checkAndPreview(id: string): Promise<JobPreflight> {
    const job = this.get(id)
    if (job.status !== 'draft' && job.status !== 'previewed') throw new Error('当前任务不能重新预览')
    const rows = this.stepRows(id)
    if (!rows.length) throw new Error('任务至少需要一笔转账')
    if (rows.some((row) => row.asset === 'USDT')) await this.gateway.validateUsdt()
    const ordered = job.shuffle ? shuffle(rows) : rows
    const frozen = ordered.map((row, position) => freezeStep(row, position, job.intervalMinSeconds, job.intervalMaxSeconds, position === ordered.length - 1))
    try {
      validateMaxOrdering(frozen)
    } catch (error) {
      const message = safeError(error)
      const position = Number(message.match(/第 (\d+) 笔/)?.[1] ?? '1') - 1
      return {
        valid: false,
        job: { ...job, steps: frozen },
        checks: frozen.map((step) => ({ stepId: step.id, position: step.position, valid: step.position !== position, error: step.position === position ? message : null, estimatedGasBaseUnits: '0', gasWalletId: job.gasPayerWalletId ?? step.sourceWalletId, gasBalanceBaseUnits: '0' })),
        summary: null,
      }
    }
    const { summary, projectedMaxAmounts, checks } = await this.inspectBalances(job, frozen)
    if (checks.some((check) => !check.valid)) return { valid: false, job: { ...job, steps: frozen }, checks, summary }
    const phrase = `执行 ${job.id} APTOS MAINNET ${frozen.length} 笔`
    const now = new Date().toISOString()
    this.db.transaction(() => {
      this.db.prepare('UPDATE job_steps SET position = position + 10000 WHERE job_id = ?').run(id)
      this.db.prepare(`UPDATE jobs SET status = 'previewed', confirmation_phrase = ?, summary_json = ?, error = NULL, updated_at = ? WHERE id = ?`)
        .run(phrase, JSON.stringify(summary), now, id)
      const update = this.db.prepare(`
        UPDATE job_steps SET position = ?, frozen_amount_base_units = ?, frozen_amount_display = ?, wait_after_seconds = ?,
          status = 'pending', tx_hash = NULL, error = NULL, updated_at = ? WHERE id = ?
      `)
      for (const step of frozen) {
        const maxProjection = projectedMaxAmounts.get(step.id)
        update.run(step.position, step.frozenAmountBaseUnits, maxProjection ? formatAmount(maxProjection, step.asset) : step.frozenAmountDisplay,
          step.waitAfterSeconds, now, step.id)
      }
    })()
    this.audit('job.previewed', id, summary)
    this.emitChange()
    return { valid: true, job: this.get(id), checks, summary }
  }

  confirm(id: string, confirmation: string): TransferJob {
    if (!this.executionEnabled) throw new Error('主网执行门禁未开启：APTOS_MAINNET_EXECUTION_ENABLED 必须为 true')
    const job = this.get(id)
    if (job.status !== 'previewed') throw new Error('任务必须先完成预览')
    if (!job.confirmationPhrase || confirmation !== job.confirmationPhrase) throw new Error('确认短语不匹配')
    const active = this.db.prepare(`SELECT id FROM jobs WHERE status IN ('running','paused','uncertain') AND id <> ? LIMIT 1`).get(id)
    if (active) throw new Error('当前已有活动任务')
    this.setJobStatus(id, 'running', null)
    this.pauseRequested = false
    this.cancelRequested = false
    this.worker = this.run(id).finally(() => { this.worker = null })
    return this.get(id)
  }

  pause(id: string): TransferJob {
    const job = this.get(id)
    if (job.status !== 'running') throw new Error('只有运行中的任务可以暂停')
    this.pauseRequested = true
    this.audit('job.pause_requested', id, {})
    return job
  }

  resume(id: string): TransferJob {
    const job = this.get(id)
    if (job.status !== 'paused') throw new Error('只有暂停任务可以恢复')
    if (job.steps.some((step) => step.status === 'uncertain')) throw new Error('存在结果不确定的交易，禁止恢复')
    const active = this.db.prepare(`SELECT id FROM jobs WHERE status IN ('running','uncertain') AND id <> ? LIMIT 1`).get(id)
    if (active) throw new Error('当前已有其他活动任务')
    this.setJobStatus(id, 'running', null)
    this.pauseRequested = false
    this.cancelRequested = false
    this.worker = this.run(id).finally(() => { this.worker = null })
    return this.get(id)
  }

  cancel(id: string): TransferJob {
    const job = this.get(id)
    if (!['draft', 'previewed', 'running', 'paused'].includes(job.status)) throw new Error('当前任务不能取消')
    this.cancelRequested = true
    if (job.status !== 'running') this.finishCancel(id)
    this.audit('job.cancel_requested', id, {})
    return this.get(id)
  }

  attempts(jobId: string): Record<string, unknown>[] {
    this.get(jobId)
    return this.db.prepare('SELECT * FROM transaction_attempts WHERE job_id = ? ORDER BY created_at ASC').all(jobId) as Record<string, unknown>[]
  }

  private async run(jobId: string): Promise<void> {
    const job = this.get(jobId)
    for (const step of job.steps.filter((candidate) => candidate.status === 'pending' || candidate.status === 'waiting')) {
      if (this.cancelRequested) return this.finishCancel(jobId)
      if (this.pauseRequested) return this.setJobStatus(jobId, 'paused', '用户暂停')
      if (step.position > 0) {
        const previous = this.get(jobId).steps[step.position - 1]
        if (previous?.waitAfterSeconds) {
          this.setStepStatus(step.id, 'waiting', null)
          await sleepInterruptible(previous.waitAfterSeconds * 1000, () => this.pauseRequested || this.cancelRequested)
          if (this.cancelRequested) return this.finishCancel(jobId)
          if (this.pauseRequested) return this.setJobStatus(jobId, 'paused', '用户暂停')
        }
      }
      const outcome = await this.executeStep(jobId, step)
      if (outcome !== 'confirmed') return
    }
    this.setJobStatus(jobId, 'completed', null)
    this.audit('job.completed', jobId, {})
  }

  private async executeStep(jobId: string, originalStep: FrozenTransferStep): Promise<StepStatus> {
    this.setStepStatus(originalStep.id, 'preparing', null)
    const job = this.get(jobId)
    const step = job.steps.find((candidate) => candidate.id === originalStep.id)!
    try {
      let amount = step.frozenAmountBaseUnits ? BigInt(step.frozenAmountBaseUnits) : null
      const sponsored = Boolean(job.gasPayerWalletId && job.gasPayerWalletId !== step.sourceWalletId)
      if (step.amountMode === 'max') {
        const balance = await this.gateway.getBalance(this.wallets.get(step.sourceWalletId).address, step.asset)
        if (step.asset === 'APT' && !sponsored) {
          const estimate = await this.gateway.estimateGas(this.transferRequest(job, step, 1n))
          const networkReserve = estimate.gasUnitPrice * estimate.maxGasAmount
          const reserve = networkReserve > GAS_RESERVE_BASE_UNITS ? networkReserve : GAS_RESERVE_BASE_UNITS
          amount = balance - reserve
        } else amount = balance
      }
      if (!amount || amount <= 0n) throw new Error('可转金额不足')
      const sourceAddress = this.wallets.get(step.sourceWalletId).address
      const assetBalance = await this.gateway.getBalance(sourceAddress, step.asset)
      if (assetBalance < amount) throw new Error('链上余额已变化，任务已暂停')
      const prepared = await this.gateway.prepareTransfer(this.transferRequest(job, step, amount))
      const attemptId = randomUUID()
      const now = new Date().toISOString()
      this.db.transaction(() => {
        this.db.prepare(`
          INSERT INTO transaction_attempts(id, job_id, step_id, sender_address, sequence_number, tx_hash, state, created_at, updated_at)
          VALUES (?, ?, ?, ?, ?, ?, 'prepared', ?, ?)
        `).run(attemptId, jobId, step.id, sourceAddress, prepared.sequenceNumber, prepared.txHash, now, now)
        this.db.prepare(`UPDATE job_steps SET status = 'submitting', tx_hash = ?, updated_at = ? WHERE id = ?`)
          .run(prepared.txHash, now, step.id)
      })()
      try {
        await prepared.submit()
        this.updateAttempt(attemptId, 'submitted', null)
        const result = await prepared.wait()
        if (!result.success) {
          this.updateAttempt(attemptId, 'failed', result.vmStatus)
          this.setStepStatus(step.id, 'failed', result.vmStatus)
          this.setJobStatus(jobId, 'failed', `链上执行失败：${result.vmStatus}`)
          return 'failed'
        }
      } catch (error) {
        const resolved = await this.reconcile(prepared.txHash)
        if (!resolved.found || resolved.success === undefined) {
          const message = '提交结果无法确认，禁止自动重发'
          this.updateAttempt(attemptId, 'uncertain', message)
          this.setStepStatus(step.id, 'uncertain', message)
          this.setJobStatus(jobId, 'uncertain', message)
          return 'uncertain'
        }
        if (!resolved.success) {
          const message = resolved.vmStatus ?? '链上执行失败'
          this.updateAttempt(attemptId, 'failed', message)
          this.setStepStatus(step.id, 'failed', message)
          this.setJobStatus(jobId, 'failed', message)
          return 'failed'
        }
      }
      this.updateAttempt(attemptId, 'confirmed', null)
      this.setStepStatus(step.id, 'confirmed', null)
      await this.wallets.refresh(step.sourceWalletId)
      if (step.targetWalletId) await this.wallets.refresh(step.targetWalletId)
      return 'confirmed'
    } catch (error) {
      const message = safeError(error)
      this.setStepStatus(step.id, 'failed', message)
      this.setJobStatus(jobId, 'paused', message)
      return 'failed'
    }
  }

  private transferRequest(job: TransferJob, step: FrozenTransferStep, amount: bigint): TransferRequest {
    return {
      sender: this.wallets.getAccount(step.sourceWalletId),
      feePayer: job.gasPayerWalletId && job.gasPayerWalletId !== step.sourceWalletId
        ? this.wallets.getAccount(job.gasPayerWalletId)
        : null,
      recipient: step.targetAddress,
      asset: step.asset,
      amount,
    }
  }

  private async reconcile(hash: string): Promise<{ found: boolean; success?: boolean; vmStatus?: string }> {
    for (let attempt = 0; attempt < 3; attempt += 1) {
      try {
        const result = await this.gateway.findTransaction(hash)
        if (result.found && result.success !== undefined) return result
      } catch {
        // Read retries are allowed; transaction submissions are never retried.
      }
      await new Promise((resolve) => setTimeout(resolve, 1_000))
    }
    return { found: false }
  }

  private async inspectBalances(job: TransferJob, steps: FrozenTransferStep[]): Promise<{ summary: JobSummary; projectedMaxAmounts: Map<string, bigint>; checks: TransferStepCheck[] }> {
    const walletById = new Map(this.wallets.list().map((wallet) => [wallet.id, wallet]))
    const requiredIds = new Set(steps.flatMap((step) => [step.sourceWalletId, ...(step.targetWalletId ? [step.targetWalletId] : [])]))
    if (job.gasPayerWalletId) requiredIds.add(job.gasPayerWalletId)
    for (const id of requiredIds) if (!walletById.has(id)) throw new Error(`钱包不存在：${id}`)
    const ledger = new Map<string, bigint>()
    const key = (walletId: string, asset: AssetId) => `${walletId}:${asset}`
    for (const id of requiredIds) {
      const wallet = walletById.get(id)!
      for (const asset of ['APT', 'USDT'] as const) ledger.set(key(id, asset), await this.gateway.getBalance(wallet.address, asset))
    }
    let aptTotal = 0n
    let usdtTotal = 0n
    let estimatedGas = 0n
    const projectedMaxAmounts = new Map<string, bigint>()
    const checks: TransferStepCheck[] = []
    const warnings: string[] = []
    const pairCounts = new Map<string, number>()
    for (const step of steps) {
      const pair = `${step.sourceWalletId}:${step.targetAddress}:${step.asset}`
      pairCounts.set(pair, (pairCounts.get(pair) ?? 0) + 1)
      const sponsored = Boolean(job.gasPayerWalletId && job.gasPayerWalletId !== step.sourceWalletId)
      const gasWalletId = sponsored ? job.gasPayerWalletId! : step.sourceWalletId
      const sourceKey = key(step.sourceWalletId, step.asset)
      const sourceBalance = ledger.get(sourceKey) ?? 0n
      const gasKey = key(gasWalletId, 'APT')
      const gasBalance = ledger.get(gasKey) ?? 0n
      const check: TransferStepCheck = {
        stepId: step.id,
        position: step.position,
        valid: false,
        error: null,
        estimatedGasBaseUnits: '0',
        gasWalletId,
        gasBalanceBaseUnits: gasBalance.toString(),
      }
      if (walletById.get(step.sourceWalletId)!.address === step.targetAddress) {
        check.error = '不能转账给同一个账户'
        checks.push(check)
        continue
      }
      let amount = step.frozenAmountBaseUnits ? BigInt(step.frozenAmountBaseUnits) : sourceBalance
      const estimateAmount = step.asset === 'APT' && step.amountMode === 'max' && !sponsored ? 1n : amount > 0n ? amount : 1n
      let gasCost: bigint
      try {
        const estimate = await this.gateway.estimateGas(this.transferRequest(job, step, estimateAmount))
        gasCost = estimate.gasUnitPrice * estimate.maxGasAmount
        check.estimatedGasBaseUnits = gasCost.toString()
        estimatedGas += gasCost
      } catch (error) {
        check.error = `交易模拟失败：${safeError(error).replace(/^交易模拟失败：/, '')}`
        checks.push(check)
        continue
      }
      if (step.asset === 'APT' && step.amountMode === 'max' && !sponsored) {
        const reserve = gasCost > GAS_RESERVE_BASE_UNITS ? gasCost : GAS_RESERVE_BASE_UNITS
        amount = sourceBalance - reserve
      }
      if (amount <= 0n) {
        check.error = `${ASSETS[step.asset].symbol} 可转余额不足`
        checks.push(check)
        continue
      }
      if (sourceBalance < amount) {
        check.error = `${ASSETS[step.asset].symbol} 余额不足，需要 ${formatAmount(amount, step.asset)} ${ASSETS[step.asset].symbol}`
        checks.push(check)
        continue
      }
      const assetAfterTransfer = sourceBalance - amount
      const gasAvailableAfterTransfer = gasWalletId === step.sourceWalletId && step.asset === 'APT' ? assetAfterTransfer : gasBalance
      if (gasAvailableAfterTransfer < gasCost) {
        check.error = `APT 手续费不足，预计需要 ${formatAmount(gasCost, 'APT')} APT，可用 ${formatAmount(gasAvailableAfterTransfer, 'APT')} APT`
        checks.push(check)
        continue
      }
      ledger.set(sourceKey, assetAfterTransfer)
      ledger.set(gasKey, gasAvailableAfterTransfer - gasCost)
      if (step.targetWalletId) {
        const targetKey = key(step.targetWalletId, step.asset)
        ledger.set(targetKey, (ledger.get(targetKey) ?? 0n) + amount)
      }
      if (step.amountMode === 'max') projectedMaxAmounts.set(step.id, amount)
      if (step.asset === 'APT') aptTotal += amount
      else usdtTotal += amount
      check.valid = true
      checks.push(check)
    }
    if ([...pairCounts.values()].some((count) => count > 1)) warnings.push('清单包含重复的来源、目标与资产组合')
    if (steps.some((step) => step.amountMode === 'max')) warnings.push('全额转账以执行时链上余额为准，预览金额仅供参考')
    return {
      projectedMaxAmounts,
      checks,
      summary: {
        sourceWalletCount: new Set(steps.map((step) => step.sourceWalletId)).size,
        stepCount: steps.length,
        aptBaseUnits: aptTotal.toString(),
        usdtBaseUnits: usdtTotal.toString(),
        maxStepCount: steps.filter((step) => step.amountMode === 'max').length,
        estimatedGasBaseUnits: estimatedGas.toString(),
        warnings,
      },
    }
  }

  private replaceSteps(jobId: string, steps: Array<FrozenTransferStep | (FrozenTransferStep & { position: number }) | (Record<string, unknown> & { position: number })>): void {
    const now = new Date().toISOString()
    this.db.transaction(() => {
      this.db.prepare('DELETE FROM job_steps WHERE job_id = ?').run(jobId)
      const insert = this.db.prepare(`
        INSERT INTO job_steps(id, job_id, position, source_wallet_id, target_address, target_wallet_id, asset,
          amount_mode, amount_min, amount_max, status, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'pending', ?)
      `)
      for (const raw of steps) {
        const step = raw as unknown as { id: string; position: number; sourceWalletId: string; targetAddress: string; targetWalletId: string | null; asset: AssetId; amountMode: string; amountMin: string | null; amountMax: string | null }
        const address = AccountAddress.fromString(step.targetAddress).toStringLong()
        insert.run(step.id || randomUUID(), jobId, step.position, step.sourceWalletId, address, step.targetWalletId,
          step.asset, step.amountMode, step.amountMin, step.amountMax, now)
      }
    })()
  }

  private stepRows(jobId: string): StepRow[] {
    return this.db.prepare('SELECT * FROM job_steps WHERE job_id = ? ORDER BY position ASC').all(jobId) as StepRow[]
  }

  private mapJob(row: JobRow): TransferJob {
    return {
      id: row.id,
      name: row.name,
      status: row.status,
      steps: this.stepRows(row.id).map(mapStep),
      gasPayerWalletId: row.gas_payer_wallet_id,
      intervalMinSeconds: row.interval_min_seconds,
      intervalMaxSeconds: row.interval_max_seconds,
      shuffle: Boolean(row.shuffle),
      confirmationPhrase: row.confirmation_phrase,
      summary: row.summary_json ? JSON.parse(row.summary_json) as JobSummary : null,
      createdAt: row.created_at,
      updatedAt: row.updated_at,
      error: row.error,
    }
  }

  private setJobStatus(id: string, status: JobStatus, error: string | null): void {
    this.db.prepare('UPDATE jobs SET status = ?, error = ?, updated_at = ? WHERE id = ?')
      .run(status, error, new Date().toISOString(), id)
    this.audit(`job.${status}`, id, error ? { error } : {})
    this.emitChange()
  }

  private setStepStatus(id: string, status: StepStatus, error: string | null): void {
    this.db.prepare('UPDATE job_steps SET status = ?, error = ?, updated_at = ? WHERE id = ?')
      .run(status, error, new Date().toISOString(), id)
    this.emitChange()
  }

  private updateAttempt(id: string, state: string, error: string | null): void {
    this.db.prepare('UPDATE transaction_attempts SET state = ?, error = ?, updated_at = ? WHERE id = ?')
      .run(state, error, new Date().toISOString(), id)
  }

  private finishCancel(id: string): void {
    const now = new Date().toISOString()
    this.db.transaction(() => {
      this.db.prepare(`UPDATE job_steps SET status = 'cancelled', updated_at = ? WHERE job_id = ? AND status IN ('pending','waiting')`).run(now, id)
      this.db.prepare(`UPDATE jobs SET status = 'cancelled', error = NULL, updated_at = ? WHERE id = ?`).run(now, id)
    })()
    this.emitChange()
  }

  private audit(kind: string, entityId: string, detail: Record<string, unknown> | JobSummary): void {
    this.db.prepare('INSERT INTO audit_events(id, kind, entity_id, detail_json, created_at) VALUES (?, ?, ?, ?, ?)')
      .run(randomUUID(), kind, entityId, JSON.stringify(detail), new Date().toISOString())
  }

  private emitChange(): void {
    this.emit('change', this.list())
  }
}

function validateDraft(input: JobDraftInput): void {
  if (!input.name.trim()) throw new Error('任务名称不能为空')
  if (!input.steps.length || input.steps.length > 1000) throw new Error('任务步骤必须在 1 到 1000 笔之间')
  if (!Number.isInteger(input.intervalMinSeconds) || !Number.isInteger(input.intervalMaxSeconds)
    || input.intervalMinSeconds < 0 || input.intervalMaxSeconds < input.intervalMinSeconds || input.intervalMaxSeconds > 604800) {
    throw new Error('随机间隔必须是 0 到 604800 秒的有效范围')
  }
  for (const step of input.steps) {
    if (!ASSETS[step.asset]) throw new Error('不支持的资产')
    if (step.amountMode === 'fixed') {
      if (!step.amountMin || parseAmount(step.amountMin, step.asset) <= 0n) throw new Error('固定金额必须大于零')
    } else if (step.amountMode === 'random') {
      if (!step.amountMin || !step.amountMax) throw new Error('随机金额需要最小值和最大值')
      const min = parseAmount(step.amountMin, step.asset)
      const max = parseAmount(step.amountMax, step.asset)
      if (min <= 0n || min > max) throw new Error('随机金额范围无效')
    }
  }
}

function freezeStep(row: StepRow, position: number, intervalMin: number, intervalMax: number, last: boolean): FrozenTransferStep {
  let amount: bigint | null = null
  if (row.amount_mode === 'fixed') amount = parseAmount(row.amount_min!, row.asset)
  if (row.amount_mode === 'random') amount = randomBigIntInclusive(parseAmount(row.amount_min!, row.asset), parseAmount(row.amount_max!, row.asset))
  return {
    ...mapStep(row),
    position,
    frozenAmountBaseUnits: amount?.toString() ?? null,
    frozenAmountDisplay: amount === null ? null : formatAmount(amount, row.asset),
    waitAfterSeconds: last ? 0 : randomInt(intervalMin, intervalMax + 1),
    status: 'pending',
    txHash: null,
    error: null,
  }
}

function validateMaxOrdering(steps: FrozenTransferStep[]): void {
  const lastOutgoing = new Map<string, number>()
  steps.forEach((step, index) => lastOutgoing.set(`${step.sourceWalletId}:${step.asset}`, index))
  steps.forEach((step, index) => {
    if (step.amountMode === 'max' && lastOutgoing.get(`${step.sourceWalletId}:${step.asset}`) !== index) {
      throw new Error(`第 ${index + 1} 笔全额转账必须是该钱包对应资产的最后一笔出账`)
    }
  })
}

function shuffle<T>(values: T[]): T[] {
  const copy = [...values]
  for (let index = copy.length - 1; index > 0; index -= 1) {
    const selected = randomInt(index + 1)
    ;[copy[index], copy[selected]] = [copy[selected], copy[index]]
  }
  return copy
}

async function sleepInterruptible(milliseconds: number, interrupted: () => boolean): Promise<void> {
  const end = Date.now() + milliseconds
  while (Date.now() < end && !interrupted()) await new Promise((resolve) => setTimeout(resolve, Math.min(500, end - Date.now())))
}

function safeError(error: unknown): string {
  const message = error instanceof Error ? error.message : String(error)
  return message.replace(/ed25519-priv-[^\s"']+/gi, '[REDACTED]').slice(0, 500)
}
