import { expect, test } from '@playwright/test'

const now = new Date().toISOString()
const walletA = { id: '11111111-1111-4111-8111-111111111111', label: '资金账户', address: `0x${'1'.repeat(64)}`, source: 'private_key', groupId: null, accountIndex: null, accountStatus: 'standalone', derivationPath: null, createdAt: now, archivedAt: null, balances: [{ asset: 'APT', baseUnits: '125000000', display: '1.25' }, { asset: 'USDT', baseUnits: '50000000', display: '50' }], balanceError: null, balanceUpdatedAt: now }
const walletB = { ...walletA, id: '22222222-2222-4222-8222-222222222222', label: '账户 1', address: `0x${'2'.repeat(64)}`, source: 'mnemonic', groupId: '55555555-5555-4555-8555-555555555555', accountIndex: 0, accountStatus: 'funded', derivationPath: "m/44'/637'/0'/0'/0'", balances: [{ asset: 'APT', baseUnits: '25000000', display: '0.25' }, { asset: 'USDT', baseUnits: '1000000', display: '1' }] }
const walletGroup = { id: walletB.groupId, label: '日常钱包', source: 'mnemonic', derivationProfile: 'aptos_hd', nextAccountIndex: 1, activeAccountCount: 1, totalAccountCount: 1, archivedAt: null, accounts: [walletB], balances: walletB.balances, createdAt: now, updatedAt: now }

test.beforeEach(async ({ page }) => {
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path === '/api/v1/status') return route.fulfill({ json: { initialized: true, unlocked: true, executionEnabled: false, network: 'mainnet', csrfToken: 'test' } })
    if (path === '/api/v1/wallets') return route.fulfill({ json: [walletA, walletB] })
    if (path === '/api/v1/wallets/groups' && route.request().method() === 'GET') return route.fulfill({ json: [walletGroup] })
    if (path === '/api/v1/jobs' && route.request().method() === 'GET') return route.fulfill({ json: [] })
    if (path === '/api/v1/events') return route.fulfill({ status: 204 })
    if (path === '/api/v1/wallets/groups/create') return route.fulfill({ json: walletGroup })
    if (path.endsWith('/accounts')) return route.fulfill({ json: { ...walletGroup, nextAccountIndex: 3 } })
    if (path === '/api/v1/jobs' && route.request().method() === 'POST') return route.fulfill({ json: { id: '33333333-3333-4333-8333-333333333333' } })
    if (path.endsWith('/check')) {
      const summary = { sourceWalletCount: 1, stepCount: 1, aptBaseUnits: '0', usdtBaseUnits: '1000000', maxStepCount: 0, estimatedGasBaseUnits: '10', warnings: [] }
      const step = { id: crypto.randomUUID(), position: 0, sourceWalletId: walletB.id, targetAddress: walletA.address, targetWalletId: walletA.id, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null, frozenAmountBaseUnits: '1000000', frozenAmountDisplay: '1', waitAfterSeconds: 0, status: 'pending', txHash: null, error: null }
      const job = { id: '33333333-3333-4333-8333-333333333333', name: '转账计划', status: 'previewed', gasPayerWalletId: null, intervalMinSeconds: 5, intervalMaxSeconds: 30, shuffle: false, confirmationPhrase: '执行 333 APTOS MAINNET 1 笔', createdAt: now, updatedAt: now, error: null, summary, steps: [step] }
      return route.fulfill({ json: { valid: true, job, summary, checks: [{ stepId: step.id, position: 0, valid: true, error: null, estimatedGasBaseUnits: '10', gasWalletId: walletB.id, gasBalanceBaseUnits: '25000000' }] } })
    }
    return route.fulfill({ json: {} })
  })
})

test('wallet accounts and transfer builder remain usable', async ({ page }, testInfo) => {
  await page.goto('/')
  await expect(page.getByRole('heading', { name: '钱包', exact: true })).toBeVisible()
  await expect(page.getByText('资金账户').first()).toBeVisible()
  await expect(page.getByText('日常钱包', { exact: true }).first()).toBeVisible()
  await page.getByRole('button', { name: /日常钱包/ }).click()
  await expect(page.getByText('账户 1').first()).toBeVisible()
  await page.getByRole('main').getByRole('button', { name: '添加账户', exact: true }).click()
  await page.getByLabel('添加数量').fill('2')
  await expect(page.getByText('将添加 2 个新账户')).toBeVisible()
  await page.getByRole('dialog').getByRole('button', { name: '添加账户', exact: true }).click()
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true)
  await page.screenshot({ path: `artifacts/wallets-${testInfo.project.name}.png`, fullPage: true })

  await page.locator('.account-row').filter({ hasText: '账户 1' }).getByRole('button', { name: '收款' }).click()
  await expect(page.getByRole('heading', { name: '收款到 账户 1' })).toBeVisible()
  await expect(page.locator('.receive-qr svg')).toBeVisible()
  await expect(page.getByText(walletB.address, { exact: true })).toBeVisible()
  await page.screenshot({ path: `artifacts/receive-${testInfo.project.name}.png`, fullPage: true })
  await page.getByRole('button', { name: '关闭' }).click()

  await page.locator('.account-row').filter({ hasText: '账户 1' }).getByRole('button', { name: '转账' }).click()
  await expect(page.getByText('从 日常钱包 · 账户 1 发起')).toBeVisible()
  await expect(page.getByLabel('来源')).toHaveValue(walletB.id)
  await page.getByPlaceholder('0x...').fill(walletA.address)
  await expect(page.getByLabel('资产')).toHaveValue('USDT')
  await page.getByRole('button', { name: '检查并预览' }).click()
  await expect(page.getByRole('heading', { name: '确认主网转账' })).toBeVisible()
  await expect(page.getByText('当前为仅预览模式')).toBeVisible()
  await page.screenshot({ path: `artifacts/preview-${testInfo.project.name}.png`, fullPage: true })
})

test('insufficient APT marks the transfer row and blocks confirmation', async ({ page }, testInfo) => {
  let stepId = ''
  await page.route('**/api/v1/jobs', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const body = route.request().postDataJSON()
    stepId = body.steps[0].id
    return route.fulfill({ json: { id: '44444444-4444-4444-8444-444444444444' } })
  })
  await page.route('**/api/v1/jobs/*/check', async (route) => {
    const check = { stepId, position: 0, valid: false, error: 'APT 手续费不足，预计需要 0.0001 APT，可用 0 APT', estimatedGasBaseUnits: '10000', gasWalletId: walletB.id, gasBalanceBaseUnits: '0' }
    const job = { id: '44444444-4444-4444-8444-444444444444', name: '转账计划', status: 'draft', gasPayerWalletId: null, intervalMinSeconds: 5, intervalMaxSeconds: 30, shuffle: false, confirmationPhrase: null, createdAt: now, updatedAt: now, error: null, summary: null, steps: [] }
    return route.fulfill({ json: { valid: false, job, checks: [check], summary: { sourceWalletCount: 1, stepCount: 1, aptBaseUnits: '0', usdtBaseUnits: '0', maxStepCount: 0, estimatedGasBaseUnits: '10000', warnings: [] } } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: /日常钱包/ }).click()
  await page.locator('.account-row').filter({ hasText: '账户 1' }).getByRole('button', { name: '转账' }).click()
  await page.getByPlaceholder('0x...').fill(walletA.address)
  await page.getByRole('button', { name: '检查并预览' }).click()
  await expect(page.locator('.step-row.step-invalid')).toBeVisible()
  await expect(page.getByText(/APT 手续费不足/)).toBeVisible()
  await expect(page.getByRole('dialog', { name: '确认主网转账' })).toHaveCount(0)
  await page.screenshot({ path: `artifacts/insufficient-gas-${testInfo.project.name}.png`, fullPage: true })
})

test('wallet creation requires four mnemonic confirmations', async ({ page }, testInfo) => {
  await page.goto('/')
  await page.getByRole('button', { name: '创建钱包' }).click()
  await expect(page.getByRole('heading', { name: '创建钱包' })).toBeVisible()
  const words = await page.locator('.mnemonic-grid strong').allTextContents()
  expect(words).toHaveLength(24)
  await page.getByRole('button', { name: '我已完成离线备份' }).click()
  const labels = page.locator('.confirmation-grid label')
  await expect(labels).toHaveCount(4)
  for (let offset = 0; offset < 4; offset += 1) {
    const text = await labels.nth(offset).innerText()
    const position = Number(text.match(/\d+/)?.[0])
    await labels.nth(offset).locator('input').fill(words[position - 1])
  }
  await page.getByRole('button', { name: '确认并创建钱包' }).click()
  await expect(page.getByText('钱包已创建')).toBeVisible()
  await page.screenshot({ path: `artifacts/create-${testInfo.project.name}.png`, fullPage: true })
})
