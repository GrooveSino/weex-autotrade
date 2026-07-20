import { useEffect, useMemo, useRef, useState, type DragEvent, type FormEvent } from 'react'
import {
  Archive, ArrowDown, ArrowUp, ArrowUpRight, Boxes, Check, ChevronRight, CircleDollarSign, Copy, Download, Ellipsis,
  Eye, FileClock, GripVertical, KeyRound, Layers3, Lock, Pencil, Plus, QrCode, RefreshCw, RotateCcw, Send,
  ShieldAlert, Shuffle, Trash2, Upload, WalletCards, X,
} from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'
import type { AmountMode, AssetId, JobDraftInput, JobPreflight, MnemonicRestorePreview, TransferJob, TransferStepDraft, VaultStatus, WalletGroup, WalletRecord } from '../shared/types'
import { formatAmount } from '../shared/amounts'
import { download, getStatus, loadWorkspace, post, request, saveAndPreviewJob, subscribe } from './api'
import { createMnemonic, parseAccountIndexes, pickConfirmationIndexes } from './mnemonic'
import { requestEncryptedSecret } from './secret-transport'

type View = 'wallets' | 'transfer' | 'jobs'
type Modal = 'create' | 'restore' | 'private' | 'bulk' | 'confirm' | 'secret' | 'password' | 'accounts' | 'archiveGroup' | 'secretAuth' | 'archived' | 'receive' | 'accountDetails' | null
type SecretTarget = { kind: 'mnemonic'; group: WalletGroup } | { kind: 'privateKey'; wallet: WalletRecord }
const statusLabels: Record<string, string> = {
  draft: '草稿', previewed: '待确认', running: '运行中', paused: '已暂停', cancelled: '已取消',
  failed: '失败', uncertain: '待核对', completed: '已完成', pending: '待执行', waiting: '等待中',
  preparing: '准备中', submitting: '提交中', confirmed: '已确认',
  unused: '未激活', used: '已使用', funded: '有余额', standalone: '独立账户',
}

export function App() {
  const [status, setStatus] = useState<VaultStatus | null>(null)
  const [wallets, setWallets] = useState<WalletRecord[]>([])
  const [groups, setGroups] = useState<WalletGroup[]>([])
  const [jobs, setJobs] = useState<TransferJob[]>([])
  const [view, setView] = useState<View>('wallets')
  const [modal, setModal] = useState<Modal>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [previewJob, setPreviewJob] = useState<TransferJob | null>(null)
  const [secret, setSecret] = useState('')
  const [secretTitle, setSecretTitle] = useState('秘密')
  const [selectedGroup, setSelectedGroup] = useState<WalletGroup | null>(null)
  const [selectedWallet, setSelectedWallet] = useState<WalletRecord | null>(null)
  const [secretTarget, setSecretTarget] = useState<SecretTarget | null>(null)
  const [transferSourceWalletId, setTransferSourceWalletId] = useState<string | null>(null)
  const previousJobStatuses = useRef(new Map<string, string>())

  const refreshStatus = async () => setStatus(await getStatus())
  const reload = async () => {
    const snapshot = await loadWorkspace()
    setWallets(snapshot.wallets)
    setGroups(snapshot.groups)
    setJobs(snapshot.jobs)
  }
  const run = async (action: () => Promise<void>, success?: string) => {
    setBusy(true)
    try {
      await action()
      if (success) setToast(success)
    } catch (error) {
      setToast(error instanceof Error ? error.message : '操作失败')
    } finally {
      setBusy(false)
    }
  }

  useEffect(() => { void refreshStatus().catch((error) => setToast(error.message)) }, [])
  useEffect(() => {
    if (!status?.unlocked) return
    void reload()
    return subscribe(({ wallets: nextWallets, groups: nextGroups, jobs: nextJobs }) => {
      setWallets(nextWallets)
      setGroups(nextGroups)
      setJobs(nextJobs)
      setPreviewJob((current) => current ? nextJobs.find((job) => job.id === current.id) ?? current : null)
    })
  }, [status?.unlocked])
  useEffect(() => {
    if (!toast) return
    const timer = window.setTimeout(() => setToast(null), 4_000)
    return () => window.clearTimeout(timer)
  }, [toast])
  useEffect(() => {
    for (const job of jobs) {
      const previous = previousJobStatuses.current.get(job.id)
      if (previous && previous !== job.status && (job.status === 'failed' || job.status === 'uncertain')) {
        setToast(`${job.name}：${job.error ?? (job.status === 'failed' ? '交易失败' : '交易结果需要人工核对')}`)
      }
      previousJobStatuses.current.set(job.id, job.status)
    }
  }, [jobs])

  if (!status) return <div className="loading">正在连接本地钱包...</div>
  if (!status.unlocked) return <VaultGate status={status} onDone={refreshStatus} setToast={setToast} />

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><div className="brand-mark"><Layers3 size={19} /></div><div><strong>Aptos 本地钱包</strong><span>钱包与批量转账</span></div></div>
        <nav>
          <NavButton active={view === 'wallets'} icon={<WalletCards size={18} />} label="钱包" onClick={() => setView('wallets')} />
          <NavButton active={view === 'transfer'} icon={<CircleDollarSign size={18} />} label="转账计划" onClick={() => setView('transfer')} />
          <NavButton active={view === 'jobs'} icon={<FileClock size={18} />} label="执行记录" onClick={() => setView('jobs')} count={jobs.filter((job) => ['running', 'paused', 'uncertain'].includes(job.status)).length} />
        </nav>
        <div className="sidebar-foot">
          <div className={`execution-state ${status.executionEnabled ? 'enabled' : ''}`}>
            <ShieldAlert size={16} />
            <div><strong>Mainnet</strong><span>{status.executionEnabled ? '执行已开启' : '仅预览'}</span></div>
          </div>
          <button className="nav-button" title="锁定钱包" aria-label="锁定钱包" onClick={() => void run(async () => {
            await post('/api/v1/vault/lock')
            setWallets([]); setGroups([]); setJobs([]); await refreshStatus()
          })}><Lock size={18} />锁定钱包</button>
          <button className="nav-button" title="修改主密码" aria-label="修改主密码" onClick={() => setModal('password')}><KeyRound size={18} />修改主密码</button>
        </div>
      </aside>
      <main>
        {view === 'wallets' && <WalletView wallets={wallets} groups={groups} busy={busy} setModal={setModal} run={run} reload={reload}
          onAccounts={(group) => { setSelectedGroup(group); setModal('accounts') }}
          onArchive={(group) => { setSelectedGroup(group); setModal('archiveGroup') }}
          onRevealMnemonic={(group) => { setSecretTarget({ kind: 'mnemonic', group }); setModal('secretAuth') }}
          onReceive={(wallet) => { setSelectedWallet(wallet); setModal('receive') }}
          onDetails={(wallet) => { setSelectedWallet(wallet); setModal('accountDetails') }}
          onTransfer={(wallet) => { setTransferSourceWalletId(wallet.id); setView('transfer') }} />}
        {view === 'transfer' && <TransferView key={transferSourceWalletId ?? 'new'} wallets={wallets} groups={groups} busy={busy} setModal={setModal} run={run} initialSourceWalletId={transferSourceWalletId} onPreview={(job) => { setPreviewJob(job); setModal('confirm') }} />}
        {view === 'jobs' && <JobsView jobs={jobs} wallets={wallets} run={run} setPreviewJob={setPreviewJob} setModal={setModal} />}
      </main>
      {modal === 'create' && <CreateWalletDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'restore' && <RestoreWalletDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'private' && <ImportPrivateKeyDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'confirm' && previewJob && <ConfirmDialog job={previewJob} executionEnabled={status.executionEnabled} close={() => setModal(null)} run={run} onStarted={() => { setModal(null); setView('jobs') }} />}
      {modal === 'secret' && <SecretDialog title={secretTitle} secret={secret} close={() => { setSecret(''); setModal(null) }} />}
      {modal === 'password' && <PasswordDialog close={() => setModal(null)} run={run} />}
      {modal === 'accounts' && selectedGroup && <AccountDialog group={selectedGroup} close={() => { setSelectedGroup(null); setModal(null) }} run={run} reload={reload} />}
      {modal === 'archiveGroup' && selectedGroup && <ArchiveGroupDialog group={selectedGroup} close={() => { setSelectedGroup(null); setModal(null) }} run={run} reload={reload} />}
      {modal === 'secretAuth' && secretTarget && <SecretAuthDialog target={secretTarget} close={() => { setSecretTarget(null); setModal(null) }} run={run} onSecret={(title, value) => { setSecretTitle(title); setSecret(value); setSecretTarget(null); setModal('secret') }} />}
      {modal === 'archived' && <ArchivedDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'receive' && selectedWallet && <ReceiveDialog wallet={selectedWallet} close={() => { setSelectedWallet(null); setModal(null) }} />}
      {modal === 'accountDetails' && selectedWallet && <AccountDetailsDialog wallet={selectedWallet} close={() => { setSelectedWallet(null); setModal(null) }} run={run} reload={reload}
        onReceive={() => setModal('receive')}
        onTransfer={() => { setSelectedWallet(null); setModal(null); setTransferSourceWalletId(selectedWallet.id); setView('transfer') }}
        onReveal={() => { setSecretTarget({ kind: 'privateKey', wallet: selectedWallet }); setSelectedWallet(null); setModal('secretAuth') }} />}
      {toast && <div className="toast">{toast}</div>}
      {busy && <div className="busy-bar" />}
    </div>
  )
}

function VaultGate({ status, onDone, setToast }: { status: VaultStatus; onDone: () => Promise<void>; setToast: (value: string) => void }) {
  const [password, setPassword] = useState('')
  const [confirm, setConfirm] = useState('')
  const [busy, setBusy] = useState(false)
  const submit = async (event: FormEvent) => {
    event.preventDefault()
    if (!status.initialized && password !== confirm) return setToast('两次密码不一致')
    setBusy(true)
    try {
      await post(status.initialized ? '/api/v1/vault/unlock' : '/api/v1/vault/initialize', { password })
      await onDone()
    } catch (error) { setToast(error instanceof Error ? error.message : '操作失败') }
    finally { setBusy(false) }
  }
  const restore = async (file: File) => {
    try {
      await post('/api/v1/vault/restore', JSON.parse(await file.text()))
      await onDone()
      setToast('备份已恢复，请使用原主密码解锁')
    } catch (error) { setToast(error instanceof Error ? error.message : '恢复失败') }
  }
  return <div className="vault-gate">
    <div className="vault-panel">
      <div className="vault-icon"><KeyRound size={28} /></div>
      <h1>{status.initialized ? '解锁 Aptos 钱包' : '设置本地钱包'}</h1>
      <p>{status.initialized ? '密钥仅在本机内存中解密。' : '主密码不会保存，丢失后无法恢复钱包。'}</p>
      <form onSubmit={submit}>
        <label>主密码<input autoFocus type="password" minLength={12} value={password} onChange={(event) => setPassword(event.target.value)} placeholder="至少 12 个字符" /></label>
        {!status.initialized && <label>确认主密码<input type="password" minLength={12} value={confirm} onChange={(event) => setConfirm(event.target.value)} /></label>}
        <button className="primary wide" disabled={busy}>{busy ? '处理中...' : status.initialized ? '解锁' : '完成设置'}</button>
      </form>
      {!status.initialized && <label className="file-button"><Upload size={16} />从加密备份恢复<input type="file" accept="application/json" onChange={(event) => event.target.files?.[0] && void restore(event.target.files[0])} /></label>}
      <div className="mainnet-notice"><ShieldAlert size={16} />固定连接 Aptos Mainnet，真实执行默认关闭。</div>
    </div>
  </div>
}

function WalletView({ wallets, groups, busy, setModal, run, reload, onAccounts, onArchive, onRevealMnemonic, onReceive, onTransfer, onDetails }: {
  wallets: WalletRecord[]; groups: WalletGroup[]; busy: boolean; setModal: (value: Modal) => void
  run: (action: () => Promise<void>, success?: string) => Promise<void>; reload: () => Promise<void>
  onAccounts: (group: WalletGroup) => void; onArchive: (group: WalletGroup) => void
  onRevealMnemonic: (group: WalletGroup) => void; onReceive: (wallet: WalletRecord) => void
  onTransfer: (wallet: WalletRecord) => void; onDetails: (wallet: WalletRecord) => void
}) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(groups.map((group) => group.id)))
  const standalone = wallets.filter((wallet) => !wallet.groupId)
  const totalApt = wallets.reduce((sum, wallet) => sum + BigInt(wallet.balances.find((balance) => balance.asset === 'APT')?.baseUnits ?? '0'), 0n)
  const totalUsdt = wallets.reduce((sum, wallet) => sum + BigInt(wallet.balances.find((balance) => balance.asset === 'USDT')?.baseUnits ?? '0'), 0n)
  return <>
    <PageHeader title="钱包" subtitle={`${wallets.length} 个账户 · Aptos 主网`} actions={<>
      <button className="secondary" onClick={() => setModal('restore')}><Upload size={16} />恢复钱包</button>
      <button className="primary" onClick={() => setModal('create')}><Plus size={16} />创建钱包</button>
      <details className="action-menu"><summary aria-label="更多操作"><Ellipsis size={18} /></summary><div>
        <button onClick={() => void run(async () => { await post('/api/v1/wallets/refresh-all'); await reload() }, '余额已刷新')} disabled={busy}><RefreshCw size={15} />刷新全部余额</button>
        <button onClick={() => setModal('private')}><KeyRound size={15} />导入私钥</button>
        <button onClick={() => void download('/api/v1/vault/backup', 'aptos-wallet-backup.json')}><Download size={15} />下载加密备份</button>
        <button onClick={() => void download('/api/v1/wallets/addresses.csv', 'aptos-addresses.csv')}><Download size={15} />导出地址 CSV</button>
        <button onClick={() => setModal('archived')}><Archive size={15} />查看归档</button>
      </div></details>
    </>} />
    <section className="metrics-band">
      <Metric label="APT 总余额" value={`${formatAmount(totalApt, 'APT')} APT`} />
      <Metric label="USDt 总余额" value={`${formatAmount(totalUsdt, 'USDT')} USDt`} />
      <Metric label="钱包" value={groups.length.toString()} />
      <Metric label="账户" value={wallets.length.toString()} />
    </section>
    <section className="wallet-groups-section">
      <div className="section-head"><h2>我的钱包</h2><span>{groups.reduce((sum, group) => sum + group.activeAccountCount, 0)} 个账户</span></div>
      {groups.length === 0 ? <Empty icon={<Layers3 size={28} />} text="还没有钱包。" /> : groups.map((group) => {
        const isOpen = expanded.has(group.id)
        return <div className="wallet-group" key={group.id}>
          <div className="wallet-group-head">
            <button className="group-toggle" aria-expanded={isOpen} onClick={() => setExpanded((current) => { const next = new Set(current); isOpen ? next.delete(group.id) : next.add(group.id); return next })}>
              <ChevronRight size={17} /><div><strong>{group.label}</strong><span>{group.derivationProfile === 'aptos_hd' ? '多账户钱包' : '旧版钱包（仅管理现有账户）'}</span></div>
            </button>
            <div className="group-summary"><span>{group.activeAccountCount} / {group.totalAccountCount} 个账户</span><strong>{group.balances.find((item) => item.asset === 'APT')?.display ?? '0'} APT</strong><strong>{group.balances.find((item) => item.asset === 'USDT')?.display ?? '0'} USDt</strong></div>
            <div className="row-actions">
              {group.derivationProfile === 'aptos_hd' && <button className="secondary small" onClick={() => onAccounts(group)}><Plus size={14} />添加账户</button>}
              <details className="action-menu row-menu"><summary aria-label={`${group.label} 更多操作`}><Ellipsis size={17} /></summary><div>
                <button onClick={() => onRevealMnemonic(group)}><Eye size={15} />查看助记词</button>
                <button onClick={() => { const label = window.prompt('钱包名称', group.label); if (label?.trim()) void run(async () => { await request(`/api/v1/wallets/groups/${group.id}`, { method: 'PATCH', body: JSON.stringify({ label }) }); await reload() }, '钱包已重命名') }}><Pencil size={15} />重命名钱包</button>
                <button className="danger-text" onClick={() => onArchive(group)}><Archive size={15} />归档钱包</button>
              </div></details>
            </div>
          </div>
          {isOpen && <WalletList wallets={group.accounts} onReceive={onReceive} onTransfer={onTransfer} onDetails={onDetails} />}
        </div>
      })}
    </section>
    <section className="table-section standalone-section">
      <div className="section-head"><h2>导入的账户</h2><span>使用私钥单独导入</span></div>
      {wallets.length === 0 ? <Empty icon={<WalletCards size={28} />} text="还没有钱包，先创建或恢复一个钱包。" /> :
        standalone.length === 0 ? <Empty icon={<WalletCards size={28} />} text="没有导入的账户。" /> : <WalletList wallets={standalone} onReceive={onReceive} onTransfer={onTransfer} onDetails={onDetails} />}
    </section>
  </>
}

function WalletList({ wallets, onReceive, onTransfer, onDetails }: { wallets: WalletRecord[]; onReceive: (wallet: WalletRecord) => void; onTransfer: (wallet: WalletRecord) => void; onDetails: (wallet: WalletRecord) => void }) {
  return <div className="account-list">{wallets.map((wallet) => <article className="account-row" key={wallet.id}>
    <div className="account-identity"><strong>{accountLabel(wallet)}</strong><Address value={wallet.address} />{wallet.balanceError && <span className="row-error">{wallet.balanceError}</span>}</div>
    <div className="account-state"><Status value={wallet.accountStatus} /></div>
    <AccountBalance label="APT" value={wallet.balances.find((item) => item.asset === 'APT')?.display ?? '-'} />
    <AccountBalance label="USDt" value={wallet.balances.find((item) => item.asset === 'USDT')?.display ?? '-'} />
    <div className="account-actions">
      <button className="secondary small" onClick={() => onReceive(wallet)}><QrCode size={14} />收款</button>
      <button className="primary small" onClick={() => onTransfer(wallet)}><Send size={14} />转账</button>
      <IconButton title={`${accountLabel(wallet)} 详情`} icon={<Ellipsis size={17} />} onClick={() => onDetails(wallet)} />
    </div>
  </article>)}</div>
}

function AccountBalance({ label, value }: { label: string; value: string }) {
  return <div className="account-balance"><span>{label}</span><strong>{value}</strong></div>
}

function TransferView({ wallets, groups, busy, setModal, run, onPreview, initialSourceWalletId }: {
  wallets: WalletRecord[]; groups: WalletGroup[]; busy: boolean; setModal: (value: Modal) => void
  run: (action: () => Promise<void>, success?: string) => Promise<void>; onPreview: (job: TransferJob) => void
  initialSourceWalletId: string | null
}) {
  const [name, setName] = useState(`转账计划 ${new Date().toLocaleDateString()}`)
  const [steps, setSteps] = useState<TransferStepDraft[]>(() => initialSourceWalletId ? [blankStep(initialSourceWalletId)] : [])
  const [gasPayerWalletId, setGasPayer] = useState<string | null>(null)
  const [intervalMinSeconds, setIntervalMin] = useState(5)
  const [intervalMaxSeconds, setIntervalMax] = useState(30)
  const [shuffleSteps, setShuffle] = useState(false)
  const [bulkOpen, setBulkOpen] = useState(false)
  const [dragged, setDragged] = useState<number | null>(null)
  const [preflight, setPreflight] = useState<JobPreflight | null>(null)
  const initialSource = wallets.find((wallet) => wallet.id === initialSourceWalletId)
  const addStep = () => { setPreflight(null); setSteps((current) => [...current, blankStep(wallets[0]?.id ?? '')]) }
  const updateStep = (index: number, change: Partial<TransferStepDraft>) => { setPreflight(null); setSteps((current) => current.map((step, position) => position === index ? { ...step, ...change } : step)) }
  const move = (from: number, to: number) => { setPreflight(null); setSteps((current) => { const copy = [...current]; const [item] = copy.splice(from, 1); copy.splice(to, 0, item); return copy }) }
  const preview = () => void run(async () => {
    const draft: JobDraftInput = { name, steps, gasPayerWalletId, intervalMinSeconds, intervalMaxSeconds, shuffle: shuffleSteps }
    const result = await saveAndPreviewJob(draft)
    setPreflight(result)
    if (result.valid) onPreview(result.job)
  })
  return <>
    <PageHeader title="转账计划" subtitle={initialSource ? `从 ${walletOptionLabel(initialSource, groups)} 发起` : '安排并检查每一笔转账'} actions={<>
      <button className="secondary" onClick={() => setBulkOpen(true)} disabled={!wallets.length}><Boxes size={16} />批量向导</button>
      <button className="primary" onClick={preview} disabled={busy || !steps.length}><Eye size={16} />检查并预览</button>
    </>} />
    <section className="builder-settings">
      <label>任务名称<input value={name} onChange={(event) => setName(event.target.value)} /></label>
      <label>手续费账户<select value={gasPayerWalletId ?? ''} onChange={(event) => { setPreflight(null); setGasPayer(event.target.value || null) }}><option value="">由转出账户支付</option><WalletOptions wallets={wallets} groups={groups} /></select></label>
      <label>最短间隔（秒）<input type="number" min="0" max="604800" value={intervalMinSeconds} onChange={(event) => { setPreflight(null); setIntervalMin(Number(event.target.value)) }} /></label>
      <label>最长间隔（秒）<input type="number" min={intervalMinSeconds} max="604800" value={intervalMaxSeconds} onChange={(event) => { setPreflight(null); setIntervalMax(Number(event.target.value)) }} /></label>
      <label className="check-field"><input type="checkbox" checked={shuffleSteps} onChange={(event) => { setPreflight(null); setShuffle(event.target.checked) }} /><Shuffle size={16} />预览时随机排序</label>
    </section>
    {preflight && <div className={`preflight-summary ${preflight.valid ? 'valid' : 'invalid'}`}>
      {preflight.valid ? <Check size={17} /> : <ShieldAlert size={17} />}
      <div><strong>{preflight.valid ? '检查通过' : `${preflight.checks.filter((check) => !check.valid).length} 笔转账需要处理`}</strong><span>预计手续费 {formatAmount(preflight.summary?.estimatedGasBaseUnits ?? '0', 'APT')} APT</span></div>
    </div>}
    <section className="steps-section">
      <div className="section-head"><div><h2>转账步骤</h2><span>{steps.length} / 1000 笔</span></div><button className="secondary small" onClick={addStep} disabled={!wallets.length}><Plus size={15} />添加一笔</button></div>
      {!wallets.length ? <Empty icon={<KeyRound size={28} />} text="需要先导入或生成来源钱包。" /> : !steps.length ? <Empty icon={<CircleDollarSign size={28} />} text="添加单笔转账，或使用批量向导生成一对多和多对多清单。" /> :
        <div className="step-list">{steps.map((step, index) => { const check = preflight?.checks.find((item) => item.stepId === step.id); return <div className={`step-row ${check && !check.valid ? 'step-invalid' : ''}`} draggable key={step.id}
          onDragStart={() => setDragged(index)} onDragOver={(event) => event.preventDefault()} onDrop={() => { if (dragged !== null && dragged !== index) move(dragged, index); setDragged(null) }}>
          <div className="drag-handle"><GripVertical size={16} /><span>{index + 1}</span></div>
          <label>来源<select value={step.sourceWalletId} onChange={(event) => updateStep(index, { sourceWalletId: event.target.value })}><WalletOptions wallets={wallets} groups={groups} /></select></label>
          <label className="target-field">目标地址<input list="wallet-addresses" value={step.targetAddress} onChange={(event) => {
            const target = wallets.find((wallet) => wallet.address === event.target.value)
            updateStep(index, { targetAddress: event.target.value, targetWalletId: target?.id ?? null })
          }} placeholder="0x..." /></label>
          <label>资产<select value={step.asset} onChange={(event) => updateStep(index, { asset: event.target.value as AssetId })}><option value="APT">APT</option><option value="USDT">USDt</option></select></label>
          <label>金额方式<select value={step.amountMode} onChange={(event) => updateStep(index, { amountMode: event.target.value as AmountMode })}><option value="fixed">固定金额</option><option value="random">随机范围</option><option value="max">全额</option></select></label>
          {step.amountMode !== 'max' && <label>最小/固定<input inputMode="decimal" value={step.amountMin ?? ''} onChange={(event) => updateStep(index, { amountMin: event.target.value })} placeholder="0.0" /></label>}
          {step.amountMode === 'random' && <label>最大<input inputMode="decimal" value={step.amountMax ?? ''} onChange={(event) => updateStep(index, { amountMax: event.target.value })} placeholder="0.0" /></label>}
          <div className="row-actions"><IconButton title="上移" icon={<ArrowUp size={15} />} disabled={index === 0} onClick={() => move(index, index - 1)} /><IconButton title="下移" icon={<ArrowDown size={15} />} disabled={index === steps.length - 1} onClick={() => move(index, index + 1)} /><IconButton title="删除" danger icon={<Trash2 size={15} />} onClick={() => { setPreflight(null); setSteps((current) => current.filter((_, position) => position !== index)) }} /></div>
          {check && <div className={`step-check ${check.valid ? 'valid' : 'invalid'}`}>{check.valid ? <Check size={14} /> : <ShieldAlert size={14} />}<span>{check.error ?? '余额与手续费检查通过'}</span><strong>{BigInt(check.estimatedGasBaseUnits) > 0n ? `预计 ${formatAmount(check.estimatedGasBaseUnits, 'APT')} APT` : '手续费待估算'}</strong></div>}
        </div> })}</div>}
      <datalist id="wallet-addresses">{wallets.map((wallet) => <option key={wallet.id} value={wallet.address}>{walletOptionLabel(wallet, groups)}</option>)}</datalist>
    </section>
    {bulkOpen && <BulkDialog wallets={wallets} groups={groups} close={() => setBulkOpen(false)} onAdd={(generated) => { setPreflight(null); setSteps((current) => [...current, ...generated]); setBulkOpen(false) }} />}
  </>
}

function JobsView({ jobs, wallets, run, setPreviewJob, setModal }: {
  jobs: TransferJob[]; wallets: WalletRecord[]; run: (action: () => Promise<void>, success?: string) => Promise<void>
  setPreviewJob: (job: TransferJob) => void; setModal: (value: Modal) => void
}) {
  const [selectedId, setSelectedId] = useState<string | null>(jobs[0]?.id ?? null)
  const selected = jobs.find((job) => job.id === selectedId) ?? jobs[0]
  return <>
    <PageHeader title="执行记录" subtitle={`${jobs.length} 个任务`} />
    <div className="jobs-layout"><section className="job-list">
      {jobs.length === 0 ? <Empty icon={<FileClock size={28} />} text="还没有转账任务。" /> : jobs.map((job) => <button key={job.id} className={`job-item ${selected?.id === job.id ? 'selected' : ''}`} onClick={() => setSelectedId(job.id)}>
        <div><strong>{job.name}</strong><span>{new Date(job.createdAt).toLocaleString()}</span></div><div><Status value={job.status} /><ChevronRight size={16} /></div>
      </button>)}
    </section>{selected && <section className="job-detail">
      <div className="job-detail-head"><div><h2>{selected.name}</h2><Status value={selected.status} /></div><div className="header-actions">
        {selected.status === 'previewed' && <button className="primary" onClick={() => { setPreviewJob(selected); setModal('confirm') }}><Check size={16} />确认执行</button>}
        {selected.status === 'running' && <button className="secondary" onClick={() => void run(async () => { await post(`/api/v1/jobs/${selected.id}/pause`) })}>暂停</button>}
        {selected.status === 'paused' && <button className="primary" onClick={() => void run(async () => { await post(`/api/v1/jobs/${selected.id}/resume`) })}>恢复</button>}
        {['draft', 'previewed', 'running', 'paused'].includes(selected.status) && <button className="danger-button" onClick={() => void run(async () => { await post(`/api/v1/jobs/${selected.id}/cancel`) })}>取消</button>}
      </div></div>
      {selected.error && <div className="error-banner"><ShieldAlert size={17} />{selected.error}</div>}
      <div className="progress-line"><span style={{ width: `${selected.steps.length ? selected.steps.filter((step) => step.status === 'confirmed').length / selected.steps.length * 100 : 0}%` }} /></div>
      <div className="detail-meta"><span>{selected.steps.length} 笔</span><span>间隔 {selected.intervalMinSeconds}-{selected.intervalMaxSeconds} 秒</span><span>{selected.shuffle ? '随机顺序' : '清单顺序'}</span></div>
      <div className="table-scroll"><table><thead><tr><th>#</th><th>来源</th><th>目标</th><th>资产</th><th>金额</th><th>等待</th><th>状态</th><th>交易</th></tr></thead><tbody>{selected.steps.map((step) => <tr key={step.id}>
        <td>{step.position + 1}</td><td>{walletLabel(step.sourceWalletId, wallets)}</td><td><Address value={step.targetAddress} /></td><td>{step.asset === 'USDT' ? 'USDt' : 'APT'}</td>
        <td className="amount">{step.amountMode === 'max' ? `全额${step.frozenAmountDisplay ? ` (~${step.frozenAmountDisplay})` : ''}` : step.frozenAmountDisplay}</td><td>{step.waitAfterSeconds}s</td><td><Status value={step.status} /></td>
        <td>{step.txHash ? <a className="tx-link" href={`https://explorer.aptoslabs.com/txn/${step.txHash}?network=mainnet`} target="_blank" rel="noreferrer">{short(step.txHash)}</a> : step.error ? <span className="row-error">{step.error}</span> : '-'}</td>
      </tr>)}</tbody></table></div>
    </section>}</div>
  </>
}

function CreateWalletDialog({ close, run, reload }: DialogProps) {
  const [label, setLabel] = useState('我的 Aptos 钱包')
  const [mnemonic, setMnemonic] = useState(() => createMnemonic())
  const [phase, setPhase] = useState<'backup' | 'confirm'>('backup')
  const [indexes] = useState(() => pickConfirmationIndexes())
  const [answers, setAnswers] = useState<string[]>(['', '', '', ''])
  const words = mnemonic.split(' ')
  const finish = () => { setMnemonic(''); close() }
  return <Dialog title="创建钱包" close={finish} wide>
    {phase === 'backup' ? <><label>钱包名称<input value={label} onChange={(event) => setLabel(event.target.value)} /></label>
      <div className="warning-box"><ShieldAlert size={16} />这是恢复整个钱包的唯一凭证。请离线记录并妥善保管。</div>
      <div className="mnemonic-grid">{words.map((word, index) => <div key={index}><span>{index + 1}</span><strong>{word}</strong></div>)}</div>
      <div className="dialog-actions"><button className="secondary" type="button" onClick={finish}>取消</button><button className="primary" type="button" onClick={() => setPhase('confirm')}>我已完成离线备份</button></div></> :
      <form onSubmit={(event) => { event.preventDefault(); void run(async () => {
        await post('/api/v1/wallets/groups/create', { label, mnemonic, confirmationIndexes: indexes, confirmationWords: answers })
        await reload(); finish()
      }, '钱包已创建') }}>
        <div className="confirmation-grid">{indexes.map((index, offset) => <label key={index}>第 {index + 1} 个单词<input autoComplete="off" spellCheck={false} value={answers[offset]} onChange={(event) => setAnswers((current) => current.map((value, position) => position === offset ? event.target.value : value))} /></label>)}</div>
        <div className="dialog-actions"><button className="secondary" type="button" onClick={() => setPhase('backup')}>返回检查</button><button className="primary" disabled={answers.some((answer) => !answer.trim())}>确认并创建钱包</button></div>
      </form>}
  </Dialog>
}

function RestoreWalletDialog({ close, run, reload }: DialogProps) {
  const [label, setLabel] = useState('恢复的钱包')
  const [mnemonic, setMnemonic] = useState('')
  const [accountCount, setAccountCount] = useState(1)
  const [indexText, setIndexText] = useState('')
  const [preview, setPreview] = useState<MnemonicRestorePreview | null>(null)
  const selection = () => ({ accountCount, accountIndexes: parseAccountIndexes(indexText) })
  return <Dialog title="恢复钱包" close={close} wide><form onSubmit={(event) => { event.preventDefault(); void run(async () => {
    await post('/api/v1/wallets/groups/restore', { label, mnemonic, ...selection() }); setMnemonic(''); await reload(); close()
  }, '钱包已恢复') }}>
    <label>钱包名称<input value={label} onChange={(event) => setLabel(event.target.value)} /></label>
    <label>12 / 24 词英文助记词<textarea autoComplete="off" spellCheck={false} value={mnemonic} onChange={(event) => { setMnemonic(event.target.value); setPreview(null) }} /></label>
    <div className="restore-grid"><label>恢复前几个账户<input type="number" min="0" max="200" value={accountCount} onChange={(event) => { setAccountCount(Number(event.target.value)); setPreview(null) }} /></label><label>指定账户编号（可选）<input value={indexText} onChange={(event) => { setIndexText(event.target.value); setPreview(null) }} placeholder="例如 5, 37, 100-105" /></label></div>
    <button type="button" className="secondary" onClick={() => void run(async () => setPreview(await post<MnemonicRestorePreview>('/api/v1/wallets/groups/restore/preview', { mnemonic, ...selection() })))}><Eye size={16} />预览账户地址</button>
    {preview && <DerivedPreview result={preview} />}
    <DialogActions close={close} submit="恢复钱包" />
  </form></Dialog>
}

function ImportPrivateKeyDialog({ close, run, reload }: DialogProps) {
  const [label, setLabel] = useState('独立账户')
  const [privateKey, setPrivateKey] = useState('')
  return <Dialog title="导入独立私钥" close={close}><form onSubmit={(event) => { event.preventDefault(); void run(async () => {
    await post('/api/v1/wallets/import/private-key', { label, privateKey }); setPrivateKey(''); await reload(); close()
  }, '私钥账户已导入') }}>
    <label>账户名称<input value={label} onChange={(event) => setLabel(event.target.value)} /></label>
    <label>AIP-80 / Ed25519 私钥<textarea autoComplete="off" spellCheck={false} value={privateKey} onChange={(event) => setPrivateKey(event.target.value)} /></label>
    <DialogActions close={close} submit="导入账户" />
  </form></Dialog>
}

function AccountDialog({ group, close, run, reload }: { group: WalletGroup } & DialogProps) {
  const [mode, setMode] = useState<'add' | 'restore'>('add')
  const [count, setCount] = useState(1)
  const [indexes, setIndexes] = useState('')
  const submit = (event: FormEvent) => { event.preventDefault(); void run(async () => {
    if (mode === 'add') await post(`/api/v1/wallets/groups/${group.id}/accounts`, { count })
    else await post(`/api/v1/wallets/groups/${group.id}/accounts/restore`, { accountIndexes: parseAccountIndexes(indexes) })
    await reload(); close()
  }, mode === 'add' ? '账户已添加' : '账户已找回') }
  return <Dialog title={`${group.label} · 管理账户`} close={close}><div className="segmented"><button className={mode === 'add' ? 'active' : ''} onClick={() => setMode('add')}>添加新账户</button><button className={mode === 'restore' ? 'active' : ''} onClick={() => setMode('restore')}>找回已有账户</button></div><form onSubmit={submit}>
    {mode === 'add' ? <><label>添加数量<input type="number" min="1" max="200" value={count} onChange={(event) => setCount(Number(event.target.value))} /></label><div className="address-preview">将添加 {count} 个新账户</div></> : <label>高级账户编号（从 0 开始）<input value={indexes} onChange={(event) => setIndexes(event.target.value)} placeholder="例如 5, 37, 100-105" /></label>}
    <DialogActions close={close} submit={mode === 'add' ? '添加账户' : '找回账户'} />
  </form></Dialog>
}

function ArchiveGroupDialog({ group, close, run, reload }: { group: WalletGroup } & DialogProps) {
  const [password, setPassword] = useState('')
  const [confirmationName, setName] = useState('')
  return <Dialog title="归档钱包" close={close}><form onSubmit={(event) => { event.preventDefault(); void run(async () => {
    await post(`/api/v1/wallets/groups/${group.id}/archive`, { password, confirmationName }); await reload(); close()
  }, '钱包已归档') }}>
    <div className="warning-box"><Archive size={16} />归档后，这个钱包里的账户不会出现在转账选择器中。</div>
    <label>主密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
    <label>输入完整钱包名称<input value={confirmationName} onChange={(event) => setName(event.target.value)} placeholder={group.label} /></label>
    <DialogActions close={close} submit="确认归档" />
  </form></Dialog>
}

function SecretAuthDialog({ target, close, run, onSecret }: { target: SecretTarget; close: () => void; run: DialogProps['run']; onSecret: (title: string, value: string) => void }) {
  const entity = target.kind === 'mnemonic' ? target.group : target.wallet
  const [password, setPassword] = useState('')
  const [confirmationName, setName] = useState('')
  return <Dialog title={target.kind === 'mnemonic' ? '查看助记词' : '查看私钥'} close={close}><form onSubmit={(event) => { event.preventDefault(); void run(async () => {
    const value = await requestEncryptedSecret((publicKey) => target.kind === 'mnemonic'
      ? post(`/api/v1/wallets/groups/${target.group.id}/reveal-mnemonic`, { password, confirmationName, publicKey })
      : post(`/api/v1/wallets/${target.wallet.id}/reveal`, { password, confirmationName, publicKey }))
    onSecret(target.kind === 'mnemonic' ? `${entity.label} · 助记词` : `${entity.label} · 私钥`, value)
  }) }}>
    <label>主密码<input type="password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
    <label>输入完整名称<input value={confirmationName} onChange={(event) => setName(event.target.value)} placeholder={entity.label} /></label>
    <DialogActions close={close} submit="安全显示" />
  </form></Dialog>
}

function ArchivedDialog({ close, run, reload }: DialogProps) {
  const [wallets, setWallets] = useState<WalletRecord[]>([])
  const [groups, setGroups] = useState<WalletGroup[]>([])
  const load = async () => {
    const [allWallets, allGroups] = await Promise.all([request<WalletRecord[]>('/api/v1/wallets?includeArchived=true'), request<WalletGroup[]>('/api/v1/wallets/groups?includeArchived=true')])
    setWallets(allWallets.filter((wallet) => wallet.archivedAt)); setGroups(allGroups.filter((group) => group.archivedAt))
  }
  useEffect(() => { void load() }, [])
  const restore = (path: string, success: string) => void run(async () => { await post(path); await load(); await reload() }, success)
  const groupIds = new Set(groups.map((group) => group.id))
  const individual = wallets.filter((wallet) => !wallet.groupId || !groupIds.has(wallet.groupId))
  return <Dialog title="归档" close={close} wide><div className="archive-list">
    {groups.map((group) => <div key={group.id}><div><strong>{group.label}</strong><span>{group.totalAccountCount} 个账户</span></div><button className="secondary" onClick={() => restore(`/api/v1/wallets/groups/${group.id}/unarchive`, '钱包已恢复')}><RotateCcw size={15} />恢复</button></div>)}
    {individual.map((wallet) => <div key={wallet.id}><div><strong>{wallet.label}</strong><Address value={wallet.address} /></div><button className="secondary" onClick={() => restore(`/api/v1/wallets/${wallet.id}/unarchive`, '账户已恢复')}><RotateCcw size={15} />恢复</button></div>)}
    {!groups.length && !individual.length && <Empty icon={<Archive size={28} />} text="归档为空。" />}
  </div></Dialog>
}

function ReceiveDialog({ wallet, close }: { wallet: WalletRecord; close: () => void }) {
  const [copied, setCopied] = useState(false)
  const copyAddress = async () => {
    await navigator.clipboard.writeText(wallet.address)
    setCopied(true)
    window.setTimeout(() => setCopied(false), 2_000)
  }
  return <Dialog title={`收款到 ${accountLabel(wallet)}`} close={close}>
    <div className="network-pill">Aptos 主网</div>
    <div className="receive-qr"><QRCodeSVG value={wallet.address} size={196} level="H" marginSize={2} /></div>
    <div className="receive-address"><span>收款地址</span><code>{wallet.address}</code></div>
    <div className="warning-box"><ShieldAlert size={16} />仅接收 Aptos 网络上的资产。通过其他网络转入可能导致资产丢失。</div>
    <div className="dialog-actions receive-actions">
      <a className="secondary" href={`https://explorer.aptoslabs.com/account/${wallet.address}?network=mainnet`} target="_blank" rel="noreferrer">浏览器查看<ArrowUpRight size={15} /></a>
      <button className="primary" onClick={() => void copyAddress()}>{copied ? <Check size={16} /> : <Copy size={16} />}{copied ? '已复制' : '复制地址'}</button>
    </div>
  </Dialog>
}

function AccountDetailsDialog({ wallet, close, run, reload, onReceive, onTransfer, onReveal }: {
  wallet: WalletRecord; close: () => void; run: DialogProps['run']; reload: () => Promise<void>
  onReceive: () => void; onTransfer: () => void; onReveal: () => void
}) {
  const rename = () => {
    const label = window.prompt('账户名称', accountLabel(wallet))
    if (!label?.trim()) return
    void run(async () => { await request(`/api/v1/wallets/${wallet.id}`, { method: 'PATCH', body: JSON.stringify({ label }) }); await reload(); close() }, '账户已重命名')
  }
  const archive = () => {
    if (!window.confirm(`归档 ${accountLabel(wallet)}？归档后它不会出现在转账和收款列表中。`)) return
    void run(async () => { await post(`/api/v1/wallets/${wallet.id}/archive`); await reload(); close() }, '账户已归档')
  }
  return <Dialog title={accountLabel(wallet)} close={close}>
    <div className="account-detail-balances">
      <AccountBalance label="APT" value={wallet.balances.find((item) => item.asset === 'APT')?.display ?? '-'} />
      <AccountBalance label="USDt" value={wallet.balances.find((item) => item.asset === 'USDT')?.display ?? '-'} />
    </div>
    <div className="detail-list">
      <div><span>网络</span><strong>Aptos 主网</strong></div>
      <div><span>地址</span><Address value={wallet.address} /></div>
      <div><span>状态</span><Status value={wallet.accountStatus} /></div>
      <div><span>余额更新</span><strong>{wallet.balanceUpdatedAt ? new Date(wallet.balanceUpdatedAt).toLocaleString() : '尚未刷新'}</strong></div>
    </div>
    {wallet.derivationPath && <details className="advanced-info"><summary>高级信息</summary><div><span>账户编号</span><code>{wallet.accountIndex}</code></div><div><span>账户路径</span><code>{wallet.derivationPath}</code></div></details>}
    <div className="account-detail-primary"><button className="secondary" onClick={onReceive}><QrCode size={16} />收款</button><button className="primary" onClick={onTransfer}><Send size={16} />转账</button></div>
    <div className="account-detail-tools">
      <button onClick={() => void run(async () => { await post(`/api/v1/wallets/${wallet.id}/refresh`); await reload(); close() }, '余额已刷新')}><RefreshCw size={15} />刷新余额</button>
      <button onClick={rename}><Pencil size={15} />重命名</button>
      <button onClick={onReveal}><Eye size={15} />查看私钥</button>
      <button className="danger-text" onClick={archive}><Archive size={15} />归档账户</button>
    </div>
  </Dialog>
}

function DerivedPreview({ result }: { result: MnemonicRestorePreview }) {
  return <div className="derived-preview">{result.accounts.map((account) => <div key={account.accountIndex}><strong>账户 {account.accountIndex + 1}</strong><Address value={account.address} /></div>)}</div>
}

function BulkDialog({ wallets, groups, close, onAdd }: { wallets: WalletRecord[]; groups: WalletGroup[]; close: () => void; onAdd: (steps: TransferStepDraft[]) => void }) {
  const [sources, setSources] = useState<string[]>(wallets[0] ? [wallets[0].id] : []); const [targets, setTargets] = useState<string[]>([]); const [external, setExternal] = useState(''); const [asset, setAsset] = useState<AssetId>('USDT'); const [mode, setMode] = useState<AmountMode>('random'); const [min, setMin] = useState('1'); const [max, setMax] = useState('5')
  const toggle = (list: string[], value: string, setter: (values: string[]) => void) => setter(list.includes(value) ? list.filter((item) => item !== value) : [...list, value])
  const create = () => {
    const targetValues = [...targets.map((id) => ({ address: wallets.find((wallet) => wallet.id === id)!.address, id })), ...external.split(/[\n,]/).map((value) => value.trim()).filter(Boolean).map((address) => ({ address, id: null }))]
    const generated = sources.flatMap((sourceWalletId) => targetValues.filter((target) => target.id !== sourceWalletId).map((target) => ({ id: crypto.randomUUID(), sourceWalletId, targetAddress: target.address, targetWalletId: target.id, asset, amountMode: mode, amountMin: mode === 'max' ? null : min, amountMax: mode === 'random' ? max : null })))
    if (!generated.length) return
    onAdd(generated)
  }
  return <Dialog title="批量生成转账步骤" close={close} wide><div className="bulk-grid"><fieldset><legend>来源钱包</legend><GroupedWalletChecks wallets={wallets} groups={groups} selected={sources} setSelected={setSources} toggle={toggle} /></fieldset><fieldset><legend>内部目标</legend><GroupedWalletChecks wallets={wallets} groups={groups} selected={targets} setSelected={setTargets} toggle={toggle} /></fieldset></div>
    <label>外部目标地址<textarea value={external} onChange={(event) => setExternal(event.target.value)} placeholder="每行一个 0x 地址" /></label><div className="form-grid"><label>资产<select value={asset} onChange={(event) => setAsset(event.target.value as AssetId)}><option value="APT">APT</option><option value="USDT">USDt</option></select></label><label>金额方式<select value={mode} onChange={(event) => setMode(event.target.value as AmountMode)}><option value="fixed">固定</option><option value="random">随机范围</option><option value="max">全额</option></select></label>{mode !== 'max' && <label>最小/固定<input value={min} onChange={(event) => setMin(event.target.value)} /></label>}{mode === 'random' && <label>最大<input value={max} onChange={(event) => setMax(event.target.value)} /></label>}</div><DialogActions close={close} submit="生成转账清单" onSubmit={create} /></Dialog>
}

function ConfirmDialog({ job, executionEnabled, close, run, onStarted }: { job: TransferJob; executionEnabled: boolean; close: () => void; run: DialogProps['run']; onStarted: () => void }) {
  const [confirmation, setConfirmation] = useState('')
  const summary = job.summary!
  return <Dialog title="确认主网转账" close={close} wide><div className="confirm-banner"><ShieldAlert size={20} /><div><strong>Aptos Mainnet</strong><span>金额、顺序和等待时间已经确定，提交后链上交易不可撤销。</span></div></div>
    <div className="confirm-metrics"><Metric label="来源钱包" value={summary.sourceWalletCount.toString()} /><Metric label="转账笔数" value={summary.stepCount.toString()} /><Metric label="APT 总额" value={formatAmount(summary.aptBaseUnits, 'APT')} /><Metric label="USDt 总额" value={formatAmount(summary.usdtBaseUnits, 'USDT')} /><Metric label="预计手续费" value={`${formatAmount(summary.estimatedGasBaseUnits, 'APT')} APT`} /></div>
    {summary.warnings.map((warning) => <div className="warning-line" key={warning}>{warning}</div>)}
    <div className="preview-list">{job.steps.map((step) => <div key={step.id}><span>{step.position + 1}</span><code>{short(step.targetAddress)}</code><strong>{step.amountMode === 'max' ? '全额' : step.frozenAmountDisplay} {step.asset === 'USDT' ? 'USDt' : 'APT'}</strong><small>之后等待 {step.waitAfterSeconds}s</small></div>)}</div>
    <label>输入完整确认短语<code className="phrase">{job.confirmationPhrase}</code><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
    {!executionEnabled && <div className="error-banner"><Lock size={17} />当前为仅预览模式。启动服务前设置 `APTOS_MAINNET_EXECUTION_ENABLED=true` 才能提交。</div>}
    <div className="dialog-actions"><button className="secondary" onClick={close}>返回编辑</button><button className="danger-primary" disabled={!executionEnabled || confirmation !== job.confirmationPhrase} onClick={() => void run(async () => { await post(`/api/v1/jobs/${job.id}/confirm`, { confirmation }); onStarted() }, '任务已开始')}>确认并执行主网转账</button></div></Dialog>
}

function SecretDialog({ title, secret, close }: { title: string; secret: string; close: () => void }) {
  useEffect(() => {
    const timer = window.setTimeout(close, 60_000)
    const hide = () => { if (document.hidden) close() }
    document.addEventListener('visibilitychange', hide)
    return () => { window.clearTimeout(timer); document.removeEventListener('visibilitychange', hide) }
  }, [close])
  return <Dialog title={title} close={close}><div className="warning-box"><ShieldAlert size={16} />该内容将在 60 秒后或页面隐藏时清除。</div><code className="secret-value">{secret}</code><button className="secondary wide" onClick={() => void navigator.clipboard.writeText(secret)}><Copy size={16} />复制当前秘密</button></Dialog>
}

function PasswordDialog({ close, run }: { close: () => void; run: DialogProps['run'] }) {
  const [currentPassword, setCurrent] = useState(''); const [nextPassword, setNext] = useState(''); const [confirmPassword, setConfirm] = useState('')
  return <Dialog title="修改主密码" close={close}><form onSubmit={(event) => { event.preventDefault(); if (nextPassword !== confirmPassword) return; void run(async () => { await post('/api/v1/vault/change-password', { currentPassword, nextPassword }); close() }, '主密码已修改，请更新离线记录') }}>
    <label>当前主密码<input type="password" minLength={12} value={currentPassword} onChange={(event) => setCurrent(event.target.value)} /></label>
    <label>新主密码<input type="password" minLength={12} value={nextPassword} onChange={(event) => setNext(event.target.value)} /></label>
    <label>确认新主密码<input type="password" minLength={12} value={confirmPassword} onChange={(event) => setConfirm(event.target.value)} /></label>
    {confirmPassword && nextPassword !== confirmPassword && <span className="row-error">两次新密码不一致</span>}
    <DialogActions close={close} submit="修改密码" />
  </form></Dialog>
}

function WalletOptions({ wallets, groups }: { wallets: WalletRecord[]; groups: WalletGroup[] }) {
  const standalone = wallets.filter((wallet) => !wallet.groupId)
  return <>{groups.map((group) => <optgroup key={group.id} label={group.label}>{group.accounts.map((wallet) => <option key={wallet.id} value={wallet.id}>{accountLabel(wallet)} · {short(wallet.address)}</option>)}</optgroup>)}{standalone.length > 0 && <optgroup label="导入的账户">{standalone.map((wallet) => <option key={wallet.id} value={wallet.id}>{wallet.label}</option>)}</optgroup>}</>
}

function GroupedWalletChecks({ wallets, groups, selected, setSelected, toggle }: {
  wallets: WalletRecord[]; groups: WalletGroup[]; selected: string[]; setSelected: (values: string[]) => void
  toggle: (list: string[], value: string, setter: (values: string[]) => void) => void
}) {
  const standalone = wallets.filter((wallet) => !wallet.groupId)
  const toggleGroup = (ids: string[]) => setSelected(ids.every((id) => selected.includes(id)) ? selected.filter((id) => !ids.includes(id)) : [...new Set([...selected, ...ids])])
  return <>{groups.map((group) => { const ids = group.accounts.map((wallet) => wallet.id); return <div className="check-group" key={group.id}><label className="check-row check-group-title"><input type="checkbox" checked={ids.length > 0 && ids.every((id) => selected.includes(id))} onChange={() => toggleGroup(ids)} />{group.label}<span>{ids.length} 个账户</span></label>{group.accounts.map((wallet) => <label className="check-row check-child" key={wallet.id}><input type="checkbox" checked={selected.includes(wallet.id)} onChange={() => toggle(selected, wallet.id, setSelected)} />{accountLabel(wallet)}<span>{short(wallet.address)}</span></label>)}</div> })}{standalone.length > 0 && <div className="check-group"><div className="check-section-label">导入的账户</div>{standalone.map((wallet) => <label className="check-row" key={wallet.id}><input type="checkbox" checked={selected.includes(wallet.id)} onChange={() => toggle(selected, wallet.id, setSelected)} />{wallet.label}<span>{short(wallet.address)}</span></label>)}</div>}</>
}

function Dialog({ title, close, children, wide = false }: { title: string; close: () => void; children: React.ReactNode; wide?: boolean }) { return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && close()}><div className={`dialog ${wide ? 'wide-dialog' : ''}`} role="dialog" aria-modal="true" aria-label={title}><div className="dialog-head"><h2>{title}</h2><IconButton title="关闭" icon={<X size={18} />} onClick={close} /></div>{children}</div></div> }
function DialogActions({ close, submit, onSubmit }: { close: () => void; submit: string; onSubmit?: () => void }) { return <div className="dialog-actions"><button type="button" className="secondary" onClick={close}>取消</button><button type={onSubmit ? 'button' : 'submit'} className="primary" onClick={onSubmit}>{submit}</button></div> }
interface DialogProps { close: () => void; run: (action: () => Promise<void>, success?: string) => Promise<void>; reload: () => Promise<void> }

function PageHeader({ title, subtitle, actions }: { title: string; subtitle: string; actions?: React.ReactNode }) { return <header className="page-header"><div><h1>{title}</h1><p>{subtitle}</p></div>{actions && <div className="header-actions">{actions}</div>}</header> }
function NavButton({ active, icon, label, onClick, count }: { active: boolean; icon: React.ReactNode; label: string; onClick: () => void; count?: number }) { return <button className={`nav-button ${active ? 'active' : ''}`} aria-label={label} onClick={onClick}>{icon}<span>{label}</span>{Boolean(count) && <b>{count}</b>}</button> }
function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) { return <div className={`metric ${tone ?? ''}`}><span>{label}</span><strong>{value}</strong></div> }
function IconButton({ title, icon, onClick, danger, disabled }: { title: string; icon: React.ReactNode; onClick: () => void; danger?: boolean; disabled?: boolean }) { return <button type="button" className={`icon-button ${danger ? 'danger' : ''}`} title={title} aria-label={title} onClick={onClick} disabled={disabled}>{icon}</button> }
function Empty({ icon, text }: { icon: React.ReactNode; text: string }) { return <div className="empty">{icon}<p>{text}</p></div> }
function Address({ value }: { value: string }) { return <button className="address" title={value} onClick={() => void navigator.clipboard.writeText(value)}><code>{short(value)}</code><Copy size={13} /></button> }
function Status({ value }: { value: string }) { return <span className={`status status-${value}`}>{statusLabels[value] ?? value}</span> }
function short(value: string) { return value.length > 18 ? `${value.slice(0, 8)}...${value.slice(-6)}` : value }
function walletOptionLabel(wallet: WalletRecord, groups: WalletGroup[]) { const group = groups.find((item) => item.id === wallet.groupId); return group ? `${group.label} · ${accountLabel(wallet)}` : wallet.label }
function walletLabel(id: string, wallets: WalletRecord[]) { const wallet = wallets.find((item) => item.id === id); return wallet ? accountLabel(wallet) : id }
function accountLabel(wallet: WalletRecord) {
  return wallet.accountIndex !== null && wallet.label === `账户 #${wallet.accountIndex}` ? `账户 ${wallet.accountIndex + 1}` : wallet.label
}
function blankStep(sourceWalletId: string): TransferStepDraft { return { id: crypto.randomUUID(), sourceWalletId, targetAddress: '', targetWalletId: null, asset: 'USDT', amountMode: 'fixed', amountMin: '1', amountMax: null } }
