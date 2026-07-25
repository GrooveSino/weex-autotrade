import assert from 'node:assert/strict'
import {
  expiredWaitFingerprint,
  presentRuntimeWait,
  selectPrimaryRuntimeWait,
  unreconciledExpiredWaitKeys,
} from '../src/components/monitoring/monitorRuntimeStage.ts'

const wait = (key, overrides = {}) => ({
  key,
  label: key,
  updatedAtMs: 1_000,
  elapsedMs: 0,
  remainingMs: null,
  detail: '',
  symbol: null,
  action: null,
  startedAtMs: 1_000,
  deadlineAtMs: null,
  ...overrides,
})

const hold = wait('hold', { remainingMs: 5_000, deadlineAtMs: 6_000 })
const counting = presentRuntimeWait(hold, 5_900)
assert.equal(counting.state, 'counting')
assert.equal(counting.remainingMs, 100)

const expiredHold = presentRuntimeWait(hold, 6_000)
assert.equal(expiredHold.state, 'transitioning')
assert.equal(expiredHold.remainingMs, null)
assert.equal(expiredHold.phase, '准备平仓')
assert.match(expiredHold.label, /正在进入平仓/u)

const gap = presentRuntimeWait(
  wait('round-gap', { remainingMs: 3_000, deadlineAtMs: 4_000 }),
  4_001,
)
assert.equal(gap.state, 'transitioning')
assert.equal(gap.phase, '准备下一轮')

const indefinite = wait('pair:1:close')
assert.equal(presentRuntimeWait(indefinite, 10_000).state, 'indefinite')
assert.equal(selectPrimaryRuntimeWait([hold, indefinite], 6_100)?.wait.key, 'pair:1:close')

const fingerprint = expiredWaitFingerprint(hold, 6_000)
assert.equal(fingerprint, 'hold:6000')
const reconciled = new Set([fingerprint])
assert.equal(reconciled.has(expiredWaitFingerprint(hold, 7_000)), true)
assert.equal(expiredWaitFingerprint(hold, 5_999), null)
const executionKey = 'generation:execution'
const deadlineKeys = unreconciledExpiredWaitKeys([hold], 6_000, executionKey, new Set())
assert.deepEqual(deadlineKeys, [`${executionKey}:hold:6000`])
assert.deepEqual(unreconciledExpiredWaitKeys([hold], 7_000, executionKey, new Set(deadlineKeys)), [])

console.log('verified runtime wait transitions and deadline reconciliation keys')
