import { expect, test } from '@playwright/test'

const now = new Date().toISOString()
const walletA = { id: '11111111-1111-4111-8111-111111111111', label: '资金账户', address: `0x${'1'.repeat(64)}`, source: 'private_key', groupId: null, accountIndex: null, accountStatus: 'standalone', derivationPath: null, createdAt: now, archivedAt: null, balances: [{ asset: 'APT', baseUnits: '125000000', display: '1.25' }, { asset: 'USDT', baseUnits: '50000000', display: '50' }], balanceError: null, balanceUpdatedAt: now }
const walletB = { ...walletA, id: '22222222-2222-4222-8222-222222222222', label: '账户 1', address: `0x${'2'.repeat(64)}`, source: 'mnemonic', groupId: '55555555-5555-4555-8555-555555555555', accountIndex: 0, accountStatus: 'funded', derivationPath: "m/44'/637'/0'/0'/0'", balances: [{ asset: 'APT', baseUnits: '25000000', display: '0.25' }, { asset: 'USDT', baseUnits: '1000000', display: '1' }] }
const walletGroup = { id: walletB.groupId, label: '日常钱包', source: 'mnemonic', derivationProfile: 'aptos_hd', nextAccountIndex: 1, activeAccountCount: 1, totalAccountCount: 1, archivedAt: null, accounts: [walletB], balances: walletB.balances, createdAt: now, updatedAt: now }

test.beforeEach(async ({ page }) => {
  let lastDraftSteps: Array<Record<string, unknown>> = []
  await page.route('**/api/v1/**', async (route) => {
    const url = new URL(route.request().url())
    const path = url.pathname
    if (path === '/api/v1/status') return route.fulfill({ json: { initialized: true, unlocked: true, executionEnabled: false, network: 'mainnet', csrfToken: 'test' } })
    if (path === '/api/v1/wallets') return route.fulfill({ json: [walletA, walletB] })
    if (path === '/api/v1/wallets/groups' && route.request().method() === 'GET') return route.fulfill({ json: [walletGroup] })
    if (path === '/api/v1/jobs' && route.request().method() === 'GET') return route.fulfill({ json: [] })
    if (path === '/api/v1/events') return route.fulfill({ status: 204 })
    if (path === `/api/v1/wallets/${walletB.id}/transfers`) {
      const logs = [
        { id: '66666666-6666-4666-8666-666666666666', jobId: '33333333-3333-4333-8333-333333333333', jobName: '内部归集', jobStatus: 'completed', position: 0, direction: 'in', counterpartyAddress: walletA.address, counterpartyWalletId: walletA.id, asset: 'USDT', amountMode: 'fixed', amountMin: '2', amountMax: null, frozenAmountDisplay: '2', status: 'confirmed', txHash: `0x${'a'.repeat(64)}`, error: null, createdAt: now, updatedAt: now },
        { id: '77777777-7777-4777-8777-777777777777', jobId: '44444444-4444-4444-8444-444444444444', jobName: '外部付款', jobStatus: 'paused', position: 0, direction: 'out', counterpartyAddress: `0x${'3'.repeat(64)}`, counterpartyWalletId: null, asset: 'USDT', amountMode: 'fixed', amountMin: '0.5', amountMax: null, frozenAmountDisplay: '0.5', status: 'failed', txHash: null, error: 'APT 手续费不足', createdAt: now, updatedAt: now },
      ]
      const direction = url.searchParams.get('direction') ?? 'all'
      const items = direction === 'all' ? logs : logs.filter((item) => item.direction === direction)
      return route.fulfill({ json: { items, total: items.length, counts: { all: 2, in: 1, out: 1 } } })
    }
    if (path === '/api/v1/wallets/groups/create') return route.fulfill({ json: walletGroup })
    if (path.endsWith('/accounts')) return route.fulfill({ json: { ...walletGroup, nextAccountIndex: 3 } })
    if (path === '/api/v1/jobs' && route.request().method() === 'POST') {
      lastDraftSteps = route.request().postDataJSON().steps
      return route.fulfill({ json: { id: '33333333-3333-4333-8333-333333333333' } })
    }
    if (path.endsWith('/check')) {
      const frozenSteps = lastDraftSteps.map((step, position) => ({ ...step, position, frozenAmountBaseUnits: '1000000', frozenAmountDisplay: '1', waitAfterSeconds: position === 0 ? 0 : 5, status: 'pending', txHash: null, error: null }))
      const summary = { sourceWalletCount: new Set(lastDraftSteps.map((step) => step.sourceWalletId)).size, stepCount: frozenSteps.length, aptBaseUnits: '0', usdtBaseUnits: String(frozenSteps.length * 1000000), maxStepCount: 0, estimatedGasBaseUnits: String(frozenSteps.length * 10), warnings: [] }
      const job = { id: '33333333-3333-4333-8333-333333333333', name: '转账计划', status: 'previewed', gasPayerWalletId: null, intervalMinSeconds: 5, intervalMaxSeconds: 30, shuffle: false, confirmationPhrase: `执行 333 APTOS MAINNET ${frozenSteps.length} 笔`, createdAt: now, updatedAt: now, error: null, summary, steps: frozenSteps }
      return route.fulfill({ json: { valid: true, job, summary, checks: frozenSteps.map((step, position) => ({ stepId: step.id, position, valid: true, error: null, estimatedGasBaseUnits: '10', gasWalletId: step.sourceWalletId, gasBalanceBaseUnits: '25000000' })) } })
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
  await expect(page.getByText('已选择 日常钱包 · 账户 1')).toBeVisible()
  await expect(page.locator('.source-panel .selection-row').filter({ hasText: '账户 1' }).locator('input')).toBeChecked()
  await page.getByLabel('外部 Aptos 地址').fill(walletA.address)
  await expect(page.getByRole('radio', { name: 'USDt' })).toBeChecked()
  await expect(page.getByRole('radio', { name: '随机范围' })).toBeChecked()
  await page.getByRole('button', { name: '进入转账预览' }).click()
  await expect(page.getByRole('dialog', { name: '转账预览' })).toBeVisible()
  await expect(page.getByText('当前页面记录的是仅预览状态')).toBeVisible()
  await page.screenshot({ path: `artifacts/preview-${testInfo.project.name}.png`, fullPage: true })
})

test('insufficient APT marks the transfer row and blocks confirmation', async ({ page }, testInfo) => {
  let stepId = ''
  let lastDraftSteps: Array<Record<string, unknown>> = []
  await page.route('**/api/v1/jobs', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    const body = route.request().postDataJSON()
    lastDraftSteps = body.steps
    stepId = body.steps[0].id
    return route.fulfill({ json: { id: '44444444-4444-4444-8444-444444444444' } })
  })
  await page.route('**/api/v1/jobs/*/check', async (route) => {
    const check = { stepId, position: 0, valid: false, error: 'APT 手续费不足，预计需要 0.0001 APT，可用 0 APT', estimatedGasBaseUnits: '10000', gasWalletId: walletB.id, gasBalanceBaseUnits: '0' }
    const frozenSteps = lastDraftSteps.map((step, position) => ({ ...step, position, frozenAmountBaseUnits: '1000000', frozenAmountDisplay: '1', waitAfterSeconds: 0, status: 'pending', txHash: null, error: null }))
    const job = { id: '44444444-4444-4444-8444-444444444444', name: '转账计划', status: 'draft', gasPayerWalletId: null, intervalMinSeconds: 5, intervalMaxSeconds: 30, shuffle: false, confirmationPhrase: null, createdAt: now, updatedAt: now, error: null, summary: null, steps: frozenSteps }
    return route.fulfill({ json: { valid: false, job, checks: [check], summary: { sourceWalletCount: 1, stepCount: 1, aptBaseUnits: '0', usdtBaseUnits: '0', maxStepCount: 0, estimatedGasBaseUnits: '10000', warnings: [] } } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: /日常钱包/ }).click()
  await page.locator('.account-row').filter({ hasText: '账户 1' }).getByRole('button', { name: '转账' }).click()
  await page.getByLabel('外部 Aptos 地址').fill(walletA.address)
  await page.getByRole('button', { name: '进入转账预览' }).click()
  const preview = page.getByRole('dialog', { name: '转账预览' })
  await expect(preview).toBeVisible()
  await expect(preview.locator('.preview-step.invalid')).toBeVisible()
  await expect(preview.getByText(/APT 手续费不足/)).toBeVisible()
  await expect(preview.getByRole('button', { name: '修正后再发送' })).toBeDisabled()
  await page.screenshot({ path: `artifacts/insufficient-gas-${testInfo.project.name}.png`, fullPage: true })
})

test('multi-source transfers pair equal target counts one-to-one in display order', async ({ page }, testInfo) => {
  let submitted: Record<string, unknown> | null = null
  await page.route('**/api/v1/jobs', async (route) => {
    if (route.request().method() !== 'POST') return route.fallback()
    submitted = route.request().postDataJSON()
    return route.fallback()
  })
  await page.goto('/')
  await page.getByRole('button', { name: '转账计划' }).click()
  await page.locator('.source-panel .selection-row').filter({ hasText: '资金账户' }).locator('input').check()
  await page.locator('.source-panel .selection-row').filter({ hasText: '账户 1' }).locator('input').check()
  const targetA = `0x${'3'.repeat(64)}`
  const targetB = `0x${'4'.repeat(64)}`
  await page.getByLabel('外部 Aptos 地址').fill(`${targetA}\n${targetB}\n0x${'5'.repeat(64)}`)
  await expect(page.getByText(/多对多转账必须一一对应/)).toBeVisible()
  await expect(page.getByRole('button', { name: '进入转账预览' })).toBeDisabled()

  await page.getByLabel('外部 Aptos 地址').fill(`${targetA}\n${targetB}`)
  await expect(page.getByText('2 个转出账户 → 2 个收款地址')).toBeVisible()
  await expect(page.getByText('按顺序一一对应，共生成 2 笔转账')).toBeVisible()
  await expect(page.getByRole('region', { name: '一一配对预览' }).locator('.pairing-preview-row')).toHaveCount(2)
  await page.screenshot({ path: `artifacts/transfer-compose-${testInfo.project.name}.png`, fullPage: true })
  await page.getByRole('button', { name: '进入转账预览' }).click()
  expect(submitted).not.toBeNull()
  const steps = (submitted as { steps: Array<Record<string, unknown>> }).steps
  expect(steps).toHaveLength(2)
  expect(steps[0]).toMatchObject({ sourceWalletId: walletB.id, targetAddress: targetA })
  expect(steps[1]).toMatchObject({ sourceWalletId: walletA.id, targetAddress: targetB })
  expect(steps.every((step) => step.asset === 'USDT' && step.amountMode === 'random' && step.amountMin === '1' && step.amountMax === '5')).toBe(true)
})

test('wallet account selectors can collapse groups without losing selection', async ({ page }) => {
  await page.goto('/')
  await page.getByRole('button', { name: '转账计划' }).click()
  const sourcePanel = page.locator('.source-panel')
  const account = sourcePanel.locator('.selection-row').filter({ hasText: '账户 1' })
  await expect(account).toBeVisible()
  await account.locator('input').check()

  await sourcePanel.getByRole('button', { name: '折叠 日常钱包' }).click()
  await expect(account).toBeHidden()
  await expect(sourcePanel.getByText('1 / 1 已选')).toBeVisible()

  await sourcePanel.getByRole('button', { name: '展开 日常钱包' }).click()
  await expect(account).toBeVisible()
  await expect(account.locator('input')).toBeChecked()
})

test('refresh all updates accounts concurrently and highlights changed balances', async ({ page }, testInfo) => {
  let active = 0
  let peak = 0
  await page.route('**/api/v1/wallets/*/refresh', async (route) => {
    active += 1
    peak = Math.max(peak, active)
    const id = new URL(route.request().url()).pathname.split('/').at(-2)
    await new Promise((resolve) => setTimeout(resolve, 250))
    active -= 1
    const source = id === walletA.id ? walletA : walletB
    const updated = { ...source, balances: source.balances.map((balance) => balance.asset === 'APT' ? { ...balance, baseUnits: id === walletA.id ? '250000000' : '50000000', display: id === walletA.id ? '2.5' : '0.5' } : balance), balanceUpdatedAt: new Date().toISOString() }
    return route.fulfill({ json: updated })
  })
  await page.goto('/')
  await page.getByRole('button', { name: /日常钱包/ }).click()
  await page.getByRole('button', { name: '刷新全部余额' }).click()
  await expect(page.locator('.account-row.refreshing')).toHaveCount(2)
  await page.screenshot({ path: `artifacts/wallet-refreshing-${testInfo.project.name}.png`, fullPage: true })
  await expect(page.locator('.account-row.balance-changed')).toHaveCount(2)
  await expect(page.getByText('2.5', { exact: true })).toBeVisible()
  await expect(page.getByText('0.5', { exact: true })).toBeVisible()
  expect(peak).toBe(2)
  await page.screenshot({ path: `artifacts/wallet-refreshed-${testInfo.project.name}.png`, fullPage: true })
})

test('wallet menu refreshes only that wallet and highlights its changed accounts', async ({ page }, testInfo) => {
  await page.route(`**/api/v1/wallets/groups/${walletGroup.id}/refresh`, async (route) => {
    await new Promise((resolve) => setTimeout(resolve, 250))
    const updatedWallet = { ...walletB, balances: walletB.balances.map((balance) => balance.asset === 'APT' ? { ...balance, baseUnits: '75000000', display: '0.75' } : balance), balanceUpdatedAt: new Date().toISOString() }
    return route.fulfill({ json: { ...walletGroup, accounts: [updatedWallet], balances: updatedWallet.balances, updatedAt: new Date().toISOString() } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: /日常钱包/ }).click()
  await page.getByLabel('日常钱包 更多操作').click()
  await page.getByRole('button', { name: '刷新钱包余额' }).click()
  await expect(page.locator('.wallet-group .account-row.refreshing')).toHaveCount(1)
  await expect(page.locator('.standalone-section .account-row.refreshing')).toHaveCount(0)
  await page.screenshot({ path: `artifacts/wallet-group-refreshing-${testInfo.project.name}.png`, fullPage: true })
  await expect(page.locator('.wallet-group .account-row.balance-changed')).toHaveCount(1)
  await expect(page.getByText('0.75', { exact: true })).toBeVisible()
  await expect(page.locator('.standalone-section').getByText('1.25', { exact: true })).toBeVisible()
  await page.screenshot({ path: `artifacts/wallet-group-refreshed-${testInfo.project.name}.png`, fullPage: true })
})

test('each account exposes clear incoming and outgoing transfer logs', async ({ page }, testInfo) => {
  await page.goto('/')
  await page.getByRole('button', { name: /日常钱包/ }).click()
  await page.locator('.account-row').filter({ hasText: '账户 1' }).getByRole('button', { name: '日志' }).click()
  const dialog = page.getByRole('dialog', { name: '账户 1 · 转账日志' })
  await expect(dialog).toBeVisible()
  await expect(dialog.locator('.account-history-row')).toHaveCount(2)
  await expect(dialog.getByText('内部归集')).toBeVisible()
  await expect(dialog.getByText('已确认')).toBeVisible()
  await expect(dialog.getByText('APT 手续费不足')).toBeVisible()
  await expect(dialog.locator('a[href*="/txn/"]')).toHaveCount(1)
  await dialog.getByRole('button', { name: /^转出/ }).click()
  await expect(dialog.locator('.account-history-row')).toHaveCount(1)
  await expect(dialog.getByText('外部付款')).toBeVisible()
  await page.screenshot({ path: `artifacts/account-history-${testInfo.project.name}.png`, fullPage: true })
})

test('mnemonic and imported accounts can each use a local alias', async ({ page }) => {
  await page.route('**/api/v1/wallets/*', async (route) => {
    if (route.request().method() !== 'PATCH') return route.fallback()
    const id = new URL(route.request().url()).pathname.split('/').at(-1)
    const label = route.request().postDataJSON().label as string
    return route.fulfill({ json: { ...(id === walletB.id ? walletB : walletA), label } })
  })
  await page.goto('/')
  await page.getByRole('button', { name: /日常钱包/ }).click()

  await page.locator('.account-row').filter({ hasText: '账户 1' }).getByRole('button', { name: '设置 账户 1 的别名' }).click()
  await page.getByRole('dialog', { name: '设置账户别名' }).getByRole('textbox', { name: '账户别名' }).fill('日常付款')
  await page.getByRole('button', { name: '保存别名' }).click()
  await expect(page.locator('.wallet-group .account-row').filter({ hasText: '日常付款' })).toBeVisible()

  await page.locator('.standalone-section .account-row').filter({ hasText: '资金账户' }).getByRole('button', { name: '设置 资金账户 的别名' }).click()
  await page.getByRole('dialog', { name: '设置账户别名' }).getByRole('textbox', { name: '账户别名' }).fill('归集账户')
  await page.getByRole('button', { name: '保存别名' }).click()
  await expect(page.locator('.standalone-section .account-row').filter({ hasText: '归集账户' })).toBeVisible()
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
