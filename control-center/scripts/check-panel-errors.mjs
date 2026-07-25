import assert from 'node:assert/strict'
import { strategyPanelError } from '../src/components/execution/boundStrategyPanelError.ts'

const cases = [
  ['final beta source unavailable: timeout', 'prepare'],
  ['account credentials are unavailable', 'prepare'],
  ['boundary_unavailable:timeouterror', 'prepare'],
  ['this account already has an active execution for another bound strategy', 'prepare'],
  ['this WEEX account is already in use by another live campaign', 'prepare'],
  ['bound strategy preview blocked: account_is_not_flat', 'prepare'],
  ['bound strategy preview blocked: available_balance_insufficient', 'prepare'],
  ['bound strategy changed since preview; create a new preview and confirm again', 'confirm'],
  ['exact campaign confirmation does not match', 'confirm'],
  ['risk acknowledgement is required', 'confirm'],
  ['execution capacity is full', 'confirm'],
  ['command already accepted; query account or campaign state instead of retrying', 'confirm'],
  ['该账号的启动前撤单正在执行', 'cleanup'],
  ['撤单后无法读取挂单状态', 'cleanup'],
  ['当前仓位无法证明属于本次任务', 'stop'],
  ['control-plane request failed (500)', 'confirm'],
  ['Failed to fetch', 'prepare'],
  ['completely unknown English exception payload', 'prepare'],
  ['completely unknown English mutation exception', 'stop'],
]

for (const [message, operation] of cases) {
  const result = strategyPanelError(new Error(message), operation)
  assert.match(result.title, /[\u3400-\u9fff]/u)
  assert.match(result.reason, /[\u3400-\u9fff]/u)
  assert.match(result.nextStep, /[\u3400-\u9fff]/u)
  assert.ok(!result.reason.includes(message), `raw error leaked: ${message}`)
}

for (const message of ['Failed to fetch', 'command already accepted', 'unknown mutation']) {
  const result = strategyPanelError(new Error(message), 'confirm')
  assert.equal(result.action, 'verify_execution')
  assert.match(result.nextStep, /不要.*(重复|再次)/u)
}

console.log(`verified ${cases.length} strategy panel error presentations`)
