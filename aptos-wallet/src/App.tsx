import { useCallback, useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import {
  Archive, ArrowDownLeft, ArrowRight, ArrowUpRight, BookUser, Check, ChevronRight, CircleDollarSign, Clock3, CloudDownload, Copy, Download, Ellipsis,
  Eye, FileClock, KeyRound, Layers3, Lock, Pencil, Plus, QrCode, RefreshCw, RotateCcw, Search, Send,
  ShieldAlert, Shuffle, Trash2, Upload, WalletCards, X,
} from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'
import type { AccountTransferLog, AccountTransferLogPage, AddressBookEntry, AmountMode, AssetId, JobDraftInput, JobPreflight, MnemonicRestorePreview, TransferJob, TransferStepDraft, VaultStatus, WalletGroup, WalletRecord } from '../shared/types'
import { formatAmount, formatAmountWithMaxDecimals, hasAtMostDecimals } from '../shared/amounts'
import { download, getStatus, loadWorkspace, post, request, saveAndPreviewJob, subscribe, type PreflightProgress } from './api'
import { createMnemonic, parseAccountIndexes, pickConfirmationIndexes } from './mnemonic'
import { requestEncryptedSecret } from './secret-transport'
import { pairTransferEndpoints } from './transfer-pairing'

const AUTO_REFRESH_STORAGE_KEY = 'aptos-wallet.auto-refresh'

function defaultTransferPlanName(date = new Date()) {
  const pad = (value: number) => value.toString().padStart(2, '0')
  return `${date.getFullYear()}年${pad(date.getMonth() + 1)}月${pad(date.getDate())}日 ${pad(date.getHours())}时${pad(date.getMinutes())}分`
}

type View = 'wallets' | 'addressBook' | 'transfer' | 'jobs'
type Modal = 'create' | 'restore' | 'private' | 'confirm' | 'retryFailed' | 'secret' | 'password' | 'accounts' | 'archiveGroup' | 'secretAuth' | 'archived' | 'receive' | 'accountDetails' | 'accountAlias' | 'accountHistory' | null
type SecretTarget = { kind: 'mnemonic'; group: WalletGroup } | { kind: 'privateKey'; wallet: WalletRecord }
const statusLabels: Record<string, string> = {
  draft: '草稿', previewed: '待确认', running: '运行中', paused: '已暂停', cancelled: '已取消',
  failed: '失败', uncertain: '待核对', completed: '已完成', pending: '待执行', waiting: '等待中',
  preparing: '准备中', submitting: '提交中', confirmed: '已确认',
  unused: '未激活', used: '已使用', funded: '有余额', standalone: '独立账户',
}

function presentError(error: string): string {
  const normalized = error.toUpperCase()
  if (normalized.includes('TOO MANY REQUESTS') || normalized.includes('HTTP 429') || normalized.includes('CODE:429')) {
    return 'Aptos 公共节点暂时限流。程序已自动降速；本笔交易没有自动重发，可稍后重新核对或重新预览后发送。'
  }
  return error
}

export function App() {
  const [status, setStatus] = useState<VaultStatus | null>(null)
  const [wallets, setWallets] = useState<WalletRecord[]>([])
  const [groups, setGroups] = useState<WalletGroup[]>([])
  const [jobs, setJobs] = useState<TransferJob[]>([])
  const [addressBook, setAddressBook] = useState<AddressBookEntry[]>([])
  const [view, setView] = useState<View>('wallets')
  const [modal, setModal] = useState<Modal>(null)
  const [toast, setToast] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [previewJob, setPreviewJob] = useState<TransferJob | null>(null)
  const [previewPreflight, setPreviewPreflight] = useState<JobPreflight | null>(null)
  const [preflightProgress, setPreflightProgress] = useState<PreflightProgress | null>(null)
  const [secret, setSecret] = useState('')
  const [secretTitle, setSecretTitle] = useState('秘密')
  const [selectedGroup, setSelectedGroup] = useState<WalletGroup | null>(null)
  const [selectedWallet, setSelectedWallet] = useState<WalletRecord | null>(null)
  const [secretTarget, setSecretTarget] = useState<SecretTarget | null>(null)
  const [transferSourceWalletId, setTransferSourceWalletId] = useState<string | null>(null)
  const [focusedJobId, setFocusedJobId] = useState<string | null>(null)
  const previousJobStatuses = useRef(new Map<string, string>())
  const initialWorkspaceLoaded = useRef(false)

  useEffect(() => {
    const closeActionMenus = (event?: Event) => {
      const target = event?.target
      if (target instanceof Node && (target as Element).closest('details.action-menu')) return
      document.querySelectorAll<HTMLDetailsElement>('details.action-menu[open]').forEach((menu) => menu.removeAttribute('open'))
    }
    const handlePointerDown = (event: PointerEvent) => closeActionMenus(event)
    const handleKeyDown = (event: KeyboardEvent) => { if (event.key === 'Escape') closeActionMenus() }
    document.addEventListener('pointerdown', handlePointerDown, true)
    document.addEventListener('keydown', handleKeyDown)
    return () => {
      document.removeEventListener('pointerdown', handlePointerDown, true)
      document.removeEventListener('keydown', handleKeyDown)
    }
  }, [])

  const refreshStatus = async () => setStatus(await getStatus())
  const reload = async () => {
    const snapshot = await loadWorkspace()
    setWallets(snapshot.wallets)
    setGroups(snapshot.groups)
    setJobs(snapshot.jobs)
    setAddressBook(snapshot.addressBook)
    return snapshot
  }
  const applyWalletUpdate = (updated: WalletRecord) => {
    setWallets((current) => current.map((wallet) => wallet.id === updated.id ? updated : wallet))
    setGroups((current) => current.map((group) => group.id === updated.groupId
      ? (() => {
          const accounts = group.accounts.map((wallet) => wallet.id === updated.id ? updated : wallet)
          const balances = group.balances.map((balance) => {
            const total = accounts.reduce((sum, wallet) => sum + BigInt(wallet.balances.find((item) => item.asset === balance.asset)?.baseUnits ?? '0'), 0n)
            return { ...balance, baseUnits: total.toString(), display: formatAmount(total, balance.asset) }
          })
          return { ...group, accounts, balances }
        })()
      : group))
  }
  const inspectJob = useCallback(async (id: string) => {
    const updated = await request<TransferJob>(`/api/v1/jobs/${id}`)
    setJobs((current) => current.map((job) => job.id === id ? updated : job))
  }, [])
  const handleFocusedJob = useCallback((id: string) => {
    setFocusedJobId((current) => current === id ? null : current)
  }, [])
  const run = async (action: () => Promise<void>, success?: string, showGlobalBusy = true) => {
    if (showGlobalBusy) setBusy(true)
    try {
      await action()
      if (success) setToast(success)
    } catch (error) {
      setToast(error instanceof Error ? presentError(error.message) : '操作失败')
    } finally {
      if (showGlobalBusy) setBusy(false)
    }
  }

  useEffect(() => { void refreshStatus().catch((error) => setToast(error.message)) }, [])
  useEffect(() => {
    if (!status?.unlocked) return
    let active = true
    let unsubscribe = () => {}
    void (async () => {
      try {
        // Read SQLite first so reopening the page immediately shows the durable
        // worker state, even before the first SSE frame arrives.
        const snapshot = await reload()
        if (!active) return
        if (!initialWorkspaceLoaded.current) {
          initialWorkspaceLoaded.current = true
          if (snapshot.jobs.some((job) => job.status === 'running')) setView('jobs')
        }
        unsubscribe = subscribe(({ wallets: nextWallets, groups: nextGroups, jobs: nextJobs, addressBook: nextAddressBook }) => {
          setWallets(nextWallets)
          setGroups(nextGroups)
          setJobs(nextJobs)
          setAddressBook(nextAddressBook)
          setPreviewJob((current) => current ? nextJobs.find((job) => job.id === current.id) ?? current : null)
        }, (progress) => setPreflightProgress((current) => !current || current.jobId === progress.jobId ? progress : current))
      } catch (error) {
        if (active) setToast(error instanceof Error ? error.message : '无法加载本地钱包状态')
      }
    })()
    return () => { active = false; unsubscribe() }
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
          <NavButton active={view === 'addressBook'} icon={<BookUser size={18} />} label="地址簿" onClick={() => setView('addressBook')} count={addressBook.length} />
          <NavButton active={view === 'transfer'} icon={<CircleDollarSign size={18} />} label="转账计划" onClick={() => setView('transfer')} />
          <NavButton active={view === 'jobs'} icon={<FileClock size={18} />} label="执行记录" onClick={() => setView('jobs')} count={jobs.filter((job) => ['running', 'paused', 'uncertain'].includes(job.status)).length} />
        </nav>
        <div className="sidebar-foot">
          <div className={`execution-state ${status.executionEnabled ? 'enabled' : ''}`}>
            <ShieldAlert size={16} />
            <div><strong>Aptos 主网</strong><span>{status.executionEnabled ? '真实转账已开启' : '安全模式 · 真实转账已关闭'}</span></div>
          </div>
          <button className="nav-button" title="锁定钱包" aria-label="锁定钱包" onClick={() => void run(async () => {
            await post('/api/v1/vault/lock')
            setWallets([]); setGroups([]); setJobs([]); setAddressBook([]); await refreshStatus()
          })}><Lock size={18} />锁定钱包</button>
          <button className="nav-button" title="修改主密码" aria-label="修改主密码" onClick={() => setModal('password')}><KeyRound size={18} />修改主密码</button>
        </div>
      </aside>
      <main>
        <div hidden={view !== 'wallets'}><WalletView wallets={wallets} groups={groups} setModal={setModal} run={run} reload={reload} onWalletUpdated={applyWalletUpdate} onToast={setToast}
          onAccounts={(group) => { setSelectedGroup(group); setModal('accounts') }}
          onArchive={(group) => { setSelectedGroup(group); setModal('archiveGroup') }}
          onRevealMnemonic={(group) => { setSecretTarget({ kind: 'mnemonic', group }); setModal('secretAuth') }}
          onReceive={(wallet) => { setSelectedWallet(wallet); setModal('receive') }}
          onHistory={(wallet) => { setSelectedWallet(wallet); setModal('accountHistory') }}
          onAlias={(wallet) => { setSelectedWallet(wallet); setModal('accountAlias') }}
          onDetails={(wallet) => { setSelectedWallet(wallet); setModal('accountDetails') }}
          onTransfer={(wallet) => { setTransferSourceWalletId(wallet.id); setView('transfer') }} /></div>
        {view === 'addressBook' && <AddressBookView entries={addressBook} run={run} reload={reload} />}
        {view === 'transfer' && <TransferView key={transferSourceWalletId ?? 'new'} wallets={wallets} groups={groups} addressBook={addressBook} busy={busy} run={run} initialSourceWalletId={transferSourceWalletId} onPreflightStart={(jobId) => setPreflightProgress({ jobId, phase: 'prepare', message: '正在创建预览检查', completed: 0, total: 0 })} onPreflightFinished={(jobId) => setPreflightProgress((current) => current?.jobId === jobId ? null : current)} onPreview={(result) => { setPreviewPreflight(result); setPreviewJob(result.job); setModal('confirm') }} />}
        {view === 'jobs' && <JobsView jobs={jobs} wallets={wallets} addressBook={addressBook} run={run} inspectJob={inspectJob} focusedJobId={focusedJobId} onFocusedJobHandled={handleFocusedJob} setPreviewJob={(job) => { setPreviewPreflight(null); setPreviewJob(job) }} setModal={setModal} />}
      </main>
      {modal === 'create' && <CreateWalletDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'restore' && <RestoreWalletDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'private' && <ImportPrivateKeyDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'confirm' && previewJob && <ConfirmDialog job={previewJob} wallets={wallets} addressBook={addressBook} initialPreflight={previewPreflight} executionEnabled={status.executionEnabled} close={() => { setModal(null); setPreviewPreflight(null) }} run={run} onChanged={(result) => { setPreviewPreflight(result); setPreviewJob(result.job) }} onStarted={(started) => { setJobs((current) => [started, ...current.filter((job) => job.id !== started.id)]); setFocusedJobId(started.id); setModal(null); setPreviewPreflight(null); setView('jobs') }} />}
      {modal === 'retryFailed' && previewJob && <RetryFailedDialog job={previewJob} executionEnabled={status.executionEnabled} close={() => setModal(null)} run={run} onStarted={(started) => { setJobs((current) => [started, ...current.filter((job) => job.id !== started.id)]); setFocusedJobId(started.id); setModal(null); setView('jobs') }} />}
      {modal === 'secret' && <SecretDialog title={secretTitle} secret={secret} close={() => { setSecret(''); setModal(null) }} />}
      {modal === 'password' && <PasswordDialog close={() => setModal(null)} run={run} />}
      {modal === 'accounts' && selectedGroup && <AccountDialog group={selectedGroup} close={() => { setSelectedGroup(null); setModal(null) }} run={run} reload={reload} />}
      {modal === 'archiveGroup' && selectedGroup && <ArchiveGroupDialog group={selectedGroup} close={() => { setSelectedGroup(null); setModal(null) }} run={run} reload={reload} />}
      {modal === 'secretAuth' && secretTarget && <SecretAuthDialog target={secretTarget} close={() => { setSecretTarget(null); setModal(null) }} run={run} onSecret={(title, value) => { setSecretTitle(title); setSecret(value); setSecretTarget(null); setModal('secret') }} />}
      {modal === 'archived' && <ArchivedDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'receive' && selectedWallet && <ReceiveDialog wallet={selectedWallet} close={() => { setSelectedWallet(null); setModal(null) }} />}
      {modal === 'accountHistory' && selectedWallet && <AccountHistoryDialog wallet={selectedWallet} wallets={wallets} addressBook={addressBook} close={() => { setSelectedWallet(null); setModal(null) }} />}
      {modal === 'accountAlias' && selectedWallet && <AccountAliasDialog wallet={selectedWallet} close={() => { setSelectedWallet(null); setModal(null) }} run={run} onUpdated={applyWalletUpdate} />}
      {modal === 'accountDetails' && selectedWallet && <AccountDetailsDialog wallet={selectedWallet} close={() => { setSelectedWallet(null); setModal(null) }} run={run} reload={reload}
        onReceive={() => setModal('receive')}
        onHistory={() => setModal('accountHistory')}
        onAlias={() => setModal('accountAlias')}
        onTransfer={() => { setSelectedWallet(null); setModal(null); setTransferSourceWalletId(selectedWallet.id); setView('transfer') }}
        onReveal={() => { setSecretTarget({ kind: 'privateKey', wallet: selectedWallet }); setSelectedWallet(null); setModal('secretAuth') }} />}
      {preflightProgress && <PreflightProgressDialog progress={preflightProgress} />}
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
      <div className="mainnet-notice"><ShieldAlert size={16} />固定连接 Aptos Mainnet，{status.executionEnabled ? '真实转账已开启。' : '真实转账已关闭。'}</div>
    </div>
  </div>
}

function AddressBookView({ entries, run, reload }: {
  entries: AddressBookEntry[]
  run: DialogProps['run']
  reload: () => Promise<void>
}) {
  const [search, setSearch] = useState('')
  const [editing, setEditing] = useState<AddressBookEntry | 'new' | null>(null)
  const [deleting, setDeleting] = useState<AddressBookEntry | null>(null)
  const term = search.trim().toLowerCase()
  const visible = entries.filter((entry) => !term || `${entry.label} ${entry.address}`.toLowerCase().includes(term))
  return <>
    <PageHeader title="地址簿" subtitle={`${entries.length} 个常用外部地址`} actions={<button className="primary" onClick={() => setEditing('new')}><Plus size={16} />添加地址</button>} />
    <section className="address-book-page">
      <div className="address-book-toolbar">
        <label className="selection-search"><Search size={15} /><input aria-label="搜索地址簿" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索别名或 Aptos 地址" /></label>
      </div>
      {visible.length === 0 ? <Empty icon={<BookUser size={30} />} text={entries.length ? '没有匹配的地址。' : '还没有常用地址。'} /> : <div className="address-book-table">
        {visible.map((entry) => <article className="address-book-row" key={entry.id}>
          <div className="address-book-mark"><BookUser size={17} /></div>
          <div className="address-book-identity"><strong>{entry.label}</strong><span>外部地址</span></div>
          <div className="address-book-address"><code>{entry.address}</code><IconButton title={`复制 ${entry.label} 地址`} icon={<Copy size={14} />} onClick={() => void navigator.clipboard.writeText(entry.address)} /></div>
          <span className="address-book-updated">更新于 {new Date(entry.updatedAt).toLocaleDateString()}</span>
          <div className="address-book-actions"><IconButton title={`编辑 ${entry.label}`} icon={<Pencil size={15} />} onClick={() => setEditing(entry)} /><IconButton title={`删除 ${entry.label}`} icon={<Trash2 size={15} />} danger onClick={() => setDeleting(entry)} /></div>
        </article>)}
      </div>}
    </section>
    {editing && <AddressBookEntryDialog entry={editing === 'new' ? null : editing} close={() => setEditing(null)} run={run} reload={reload} />}
    {deleting && <Dialog title="删除地址" close={() => setDeleting(null)}><div className="warning-box"><ShieldAlert size={16} />删除后，历史记录不再显示这个地址的别名，但不会影响任何链上交易。</div><div className="detail-list"><div><span>别名</span><strong>{deleting.label}</strong></div><div><span>地址</span><Address value={deleting.address} /></div></div><div className="dialog-actions"><button className="secondary" onClick={() => setDeleting(null)}>取消</button><button className="danger-button" onClick={() => void run(async () => {
      await request(`/api/v1/address-book/${deleting.id}`, { method: 'DELETE' })
      await reload()
      setDeleting(null)
    }, '地址已删除')}>删除地址</button></div></Dialog>}
  </>
}

function AddressBookEntryDialog({ entry, close, run, reload }: {
  entry: AddressBookEntry | null
  close: () => void
  run: DialogProps['run']
  reload: () => Promise<void>
}) {
  const [label, setLabel] = useState(entry?.label ?? '')
  const [address, setAddress] = useState(entry?.address ?? '')
  return <Dialog title={entry ? '编辑常用地址' : '添加常用地址'} close={close}><form onSubmit={(event) => { event.preventDefault(); void run(async () => {
    const body = { label: label.trim(), address: address.trim() }
    if (entry) await request(`/api/v1/address-book/${entry.id}`, { method: 'PATCH', body: JSON.stringify(body) })
    else await post('/api/v1/address-book', body)
    await reload()
    close()
  }, entry ? '地址已更新' : '地址已加入地址簿') }}>
    <label>地址别名<input autoFocus maxLength={120} value={label} onChange={(event) => setLabel(event.target.value)} placeholder="例如：交易所充值、合作方收款" /></label>
    <label>Aptos 地址<textarea aria-label="地址簿 Aptos 地址" value={address} onChange={(event) => setAddress(event.target.value)} placeholder="0x..." /></label>
    <p className="form-hint">别名仅保存在本机，发送前仍会同时显示完整地址供你核对。</p>
    <DialogActions close={close} submit={entry ? '保存修改' : '添加地址'} disabled={!label.trim() || !address.trim()} />
  </form></Dialog>
}

function WalletView({ wallets, groups, setModal, run, reload, onWalletUpdated, onToast, onAccounts, onArchive, onRevealMnemonic, onReceive, onHistory, onAlias, onTransfer, onDetails }: {
  wallets: WalletRecord[]; groups: WalletGroup[]; setModal: (value: Modal) => void
  run: (action: () => Promise<void>, success?: string) => Promise<void>; reload: () => Promise<void>
  onWalletUpdated: (wallet: WalletRecord) => void; onToast: (message: string) => void
  onAccounts: (group: WalletGroup) => void; onArchive: (group: WalletGroup) => void
  onRevealMnemonic: (group: WalletGroup) => void; onReceive: (wallet: WalletRecord) => void; onHistory: (wallet: WalletRecord) => void
  onAlias: (wallet: WalletRecord) => void; onTransfer: (wallet: WalletRecord) => void; onDetails: (wallet: WalletRecord) => void
}) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(groups.map((group) => group.id)))
  const [refreshingWalletIds, setRefreshingWalletIds] = useState<Set<string>>(new Set())
  const [changedWalletIds, setChangedWalletIds] = useState<Set<string>>(new Set())
  const [refreshingAll, setRefreshingAll] = useState(false)
  const [refreshingGroupIds, setRefreshingGroupIds] = useState<Set<string>>(new Set())
  const [autoRefreshEnabled, setAutoRefreshEnabled] = useState(() => {
    try {
      return window.localStorage.getItem(AUTO_REFRESH_STORAGE_KEY) !== 'false'
    } catch {
      return true
    }
  })
  const latestWalletsRef = useRef(wallets)
  const refreshingWalletIdsRef = useRef(new Set<string>())
  const refreshingAllRef = useRef(false)
  const backgroundRefreshInFlightRef = useRef(false)
  latestWalletsRef.current = wallets
  const standalone = wallets.filter((wallet) => !wallet.groupId)
  const totalApt = wallets.reduce((sum, wallet) => sum + BigInt(wallet.balances.find((balance) => balance.asset === 'APT')?.baseUnits ?? '0'), 0n)
  const totalUsdt = wallets.reduce((sum, wallet) => sum + BigInt(wallet.balances.find((balance) => balance.asset === 'USDT')?.baseUnits ?? '0'), 0n)
  const refreshWalletBatch = async (candidates: WalletRecord[]) => {
    const available = candidates.filter((wallet) => !refreshingWalletIdsRef.current.has(wallet.id))
    if (!available.length) return { refreshed: 0, failed: 0 }
    // Reserve the complete batch immediately so background, global, and group refreshes
    // cannot enqueue the same account twice. Only accounts that reach a worker are
    // exposed as loading in the UI.
    available.forEach((wallet) => refreshingWalletIdsRef.current.add(wallet.id))
    let nextIndex = 0
    let failed = 0
    const worker = async () => {
      while (nextIndex < available.length) {
        const wallet = available[nextIndex++]
        setRefreshingWalletIds((current) => new Set(current).add(wallet.id))
        const before = wallet.balances.map((balance) => `${balance.asset}:${balance.baseUnits}`).join('|')
        try {
          const updated = await post<WalletRecord>(`/api/v1/wallets/${wallet.id}/refresh`)
          onWalletUpdated(updated)
          const after = updated.balances.map((balance) => `${balance.asset}:${balance.baseUnits}`).join('|')
          if (before !== after) {
            setChangedWalletIds((current) => new Set(current).add(wallet.id))
            window.setTimeout(() => setChangedWalletIds((current) => {
              const next = new Set(current); next.delete(wallet.id); return next
            }), 1_800)
          }
          if (updated.balanceError) failed += 1
        } catch (error) {
          failed += 1
          onWalletUpdated({ ...wallet, balanceError: error instanceof Error ? error.message : '余额刷新失败' })
        } finally {
          refreshingWalletIdsRef.current.delete(wallet.id)
          setRefreshingWalletIds((current) => { const next = new Set(current); next.delete(wallet.id); return next })
        }
      }
    }
    await Promise.all(Array.from({ length: Math.min(2, available.length) }, worker))
    return { refreshed: available.length, failed }
  }
  const refreshAll = async (source: 'manual' | 'background' = 'manual') => {
    if (source === 'background' && backgroundRefreshInFlightRef.current) return
    if (source === 'manual' && refreshingAllRef.current) return
    const currentWallets = latestWalletsRef.current
    if (!currentWallets.length) return
    if (source === 'background') backgroundRefreshInFlightRef.current = true
    refreshingAllRef.current = true
    setRefreshingAll(true)
    try {
      const result = await refreshWalletBatch(currentWallets)
      if (source === 'manual') onToast(result.failed ? `余额刷新完成，${result.failed} 个账户失败` : `已刷新 ${result.refreshed} 个账户`)
    } finally {
      refreshingAllRef.current = false
      if (source === 'background') backgroundRefreshInFlightRef.current = false
      setRefreshingAll(false)
    }
  }
  useEffect(() => {
    try { window.localStorage.setItem(AUTO_REFRESH_STORAGE_KEY, String(autoRefreshEnabled)) } catch { /* storage may be unavailable */ }
  }, [autoRefreshEnabled])
  useEffect(() => {
    if (!autoRefreshEnabled || !wallets.length) return
    const timer = window.setInterval(() => { void refreshAll('background') }, 30_000)
    return () => window.clearInterval(timer)
  }, [autoRefreshEnabled, wallets.length])
  const refreshGroup = async (group: WalletGroup) => {
    if (refreshingGroupIds.has(group.id)) return
    const before = wallets.filter((wallet) => wallet.groupId === group.id)
    if (before.some((wallet) => refreshingWalletIdsRef.current.has(wallet.id))) {
      onToast(`${group.label} 正在刷新中，请稍后再试`)
      return
    }
    setRefreshingGroupIds((current) => new Set(current).add(group.id))
    try {
      // Use the same per-account queue as global refresh. This keeps the visible
      // loading state truthful and applies the same conservative concurrency cap.
      const result = await refreshWalletBatch(before)
      onToast(result.failed ? `${group.label} 刷新完成，${result.failed} 个账户失败` : `${group.label} 的余额已刷新`)
    } catch (error) {
      onToast(error instanceof Error ? error.message : '钱包余额刷新失败')
    } finally {
      setRefreshingGroupIds((current) => { const next = new Set(current); next.delete(group.id); return next })
    }
  }
  const archiveAccount = async (wallet: WalletRecord) => {
    await run(async () => {
      await post(`/api/v1/wallets/${wallet.id}/archive`)
      await reload()
    }, '账户已归档')
  }
  return <>
      <PageHeader title="钱包" subtitle={`${wallets.length} 个账户 · Aptos 主网 · ${autoRefreshEnabled ? '每 30 秒自动刷新' : '自动刷新已关闭'}`} actions={<>
      <label className="refresh-switch" title="控制后台每 30 秒自动刷新余额">
        <input type="checkbox" checked={autoRefreshEnabled} onChange={(event) => setAutoRefreshEnabled(event.target.checked)} />
        <span className="refresh-switch-track" aria-hidden="true"><span /></span>
        <span>自动刷新</span>
      </label>
      <button className="secondary" title="立即刷新全部余额；不受自动刷新开关影响" onClick={() => void refreshAll()} disabled={refreshingAll || !wallets.length}><RefreshCw className={refreshingAll ? 'spin' : ''} size={16} />{refreshingAll ? '正在刷新' : '刷新全部余额'}</button>
      <button className="secondary" onClick={() => setModal('restore')}><Upload size={16} />恢复钱包</button>
      <button className="primary" onClick={() => setModal('create')}><Plus size={16} />创建钱包</button>
      <details className="action-menu"><summary aria-label="更多操作"><Ellipsis size={18} /></summary><div>
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
                <button disabled={refreshingGroupIds.has(group.id)} onClick={(event) => { event.currentTarget.closest('details')?.removeAttribute('open'); void refreshGroup(group) }}><RefreshCw className={refreshingGroupIds.has(group.id) ? 'spin' : ''} size={15} />{refreshingGroupIds.has(group.id) ? '正在刷新余额' : '刷新钱包余额'}</button>
                <button onClick={() => onRevealMnemonic(group)}><Eye size={15} />查看助记词</button>
                <button onClick={() => { const label = window.prompt('钱包名称', group.label); if (label?.trim()) void run(async () => { await request(`/api/v1/wallets/groups/${group.id}`, { method: 'PATCH', body: JSON.stringify({ label }) }); await reload() }, '钱包已重命名') }}><Pencil size={15} />重命名钱包</button>
                <button className="danger-text" onClick={() => onArchive(group)}><Archive size={15} />归档钱包</button>
              </div></details>
            </div>
          </div>
          {isOpen && <WalletList wallets={wallets.filter((wallet) => wallet.groupId === group.id)} refreshingWalletIds={refreshingWalletIds} changedWalletIds={changedWalletIds} onReceive={onReceive} onHistory={onHistory} onAlias={onAlias} onTransfer={onTransfer} onArchive={archiveAccount} onDetails={onDetails} />}
        </div>
      })}
    </section>
    <section className="table-section standalone-section">
      <div className="section-head"><h2>导入的账户</h2><span>使用私钥单独导入</span></div>
      {wallets.length === 0 ? <Empty icon={<WalletCards size={28} />} text="还没有钱包，先创建或恢复一个钱包。" /> :
        standalone.length === 0 ? <Empty icon={<WalletCards size={28} />} text="没有导入的账户。" /> : <WalletList wallets={standalone} refreshingWalletIds={refreshingWalletIds} changedWalletIds={changedWalletIds} onReceive={onReceive} onHistory={onHistory} onAlias={onAlias} onTransfer={onTransfer} onArchive={archiveAccount} onDetails={onDetails} />}
    </section>
  </>
}

function WalletList({ wallets, refreshingWalletIds, changedWalletIds, onReceive, onHistory, onAlias, onTransfer, onArchive, onDetails }: { wallets: WalletRecord[]; refreshingWalletIds: Set<string>; changedWalletIds: Set<string>; onReceive: (wallet: WalletRecord) => void; onHistory: (wallet: WalletRecord) => void; onAlias: (wallet: WalletRecord) => void; onTransfer: (wallet: WalletRecord) => void; onArchive: (wallet: WalletRecord) => Promise<void>; onDetails: (wallet: WalletRecord) => void }) {
  return <div className="account-list">{wallets.map((wallet) => { const refreshing = refreshingWalletIds.has(wallet.id); const changed = changedWalletIds.has(wallet.id); return <article className={`account-row ${refreshing ? 'refreshing' : ''} ${changed ? 'balance-changed' : ''}`} key={wallet.id} aria-busy={refreshing}>
    <div className="account-identity"><div className="account-name-line"><strong className="account-name-badge">{accountLabel(wallet)}</strong><IconButton title={`设置 ${accountLabel(wallet)} 的别名`} icon={<Pencil size={15} />} onClick={() => onAlias(wallet)} /></div><Address value={wallet.address} />{wallet.balanceError && <span className="row-error">{wallet.balanceError}</span>}</div>
    <div className="account-state"><Status value={wallet.accountStatus} /></div>
    <AccountBalance label="APT" value={wallet.balances.find((item) => item.asset === 'APT')?.display ?? '-'} loading={refreshing} />
    <AccountBalance label="USDt" value={wallet.balances.find((item) => item.asset === 'USDT')?.display ?? '-'} loading={refreshing} />
    <div className="account-actions">
      <button className="secondary small" onClick={() => onReceive(wallet)}><QrCode size={14} />收款</button>
      <ArchiveAccountButton wallet={wallet} onArchive={onArchive} />
      <button className="secondary small" onClick={() => onHistory(wallet)}><FileClock size={14} />日志</button>
      <button className="primary small" onClick={() => onTransfer(wallet)}><Send size={14} />转账</button>
      <IconButton title={`${accountLabel(wallet)} 详情`} icon={<Ellipsis size={17} />} onClick={() => onDetails(wallet)} />
    </div>
  </article> })}</div>
}

function ArchiveAccountButton({ wallet, onArchive }: { wallet: WalletRecord; onArchive: (wallet: WalletRecord) => Promise<void> }) {
  const [armed, setArmed] = useState(false)
  const [archiving, setArchiving] = useState(false)
  const resetTimer = useRef<number | null>(null)
  const clearResetTimer = () => {
    if (resetTimer.current !== null) window.clearTimeout(resetTimer.current)
    resetTimer.current = null
  }
  useEffect(() => () => clearResetTimer(), [])
  const handleClick = async () => {
    if (archiving) return
    if (!armed) {
      setArmed(true)
      clearResetTimer()
      resetTimer.current = window.setTimeout(() => {
        setArmed(false)
        resetTimer.current = null
      }, 5_000)
      return
    }
    clearResetTimer()
    setArchiving(true)
    await onArchive(wallet)
    setArchiving(false)
    setArmed(false)
  }
  return <button className={`account-archive-button ${armed ? 'is-confirming' : ''}`} aria-label={armed ? `确认归档 ${accountLabel(wallet)}` : `归档 ${accountLabel(wallet)}`} title={armed ? '再次点击，归档此账户' : '归档此账户'} onClick={() => void handleClick()} disabled={archiving}>
    {armed ? <Check size={14} /> : <Archive size={14} />}{archiving ? '归档中' : armed ? '点击确认' : '归档'}
  </button>
}

function AccountBalance({ label, value, loading = false }: { label: string; value: string; loading?: boolean }) {
  return <div className="account-balance" aria-live="polite"><span>{label}</span><strong>{loading ? <RefreshCw className="spin" size={16} aria-label={`${label} 余额刷新中`} /> : value}</strong></div>
}

function TransferView({ wallets, groups, addressBook, busy, run, onPreview, onPreflightStart, onPreflightFinished, initialSourceWalletId }: {
  wallets: WalletRecord[]; groups: WalletGroup[]; addressBook: AddressBookEntry[]; busy: boolean
  run: (action: () => Promise<void>, success?: string) => Promise<void>; onPreview: (result: JobPreflight) => void
  onPreflightStart: (jobId: string) => void; onPreflightFinished: (jobId: string) => void
  initialSourceWalletId: string | null
}) {
  const [name, setName] = useState(() => defaultTransferPlanName())
  const [sourceWalletIds, setSourceWalletIds] = useState<string[]>(() => initialSourceWalletId ? [initialSourceWalletId] : [])
  const [internalTargetWalletIds, setInternalTargetWalletIds] = useState<string[]>([])
  const [externalTargets, setExternalTargets] = useState('')
  const [sourcePickerOpen, setSourcePickerOpen] = useState(false)
  const [targetPickerOpen, setTargetPickerOpen] = useState(false)
  const [addressBookPickerOpen, setAddressBookPickerOpen] = useState(false)
  const [externalTargetOpen, setExternalTargetOpen] = useState(false)
  const [asset, setAsset] = useState<AssetId>('USDT')
  const [amountMode, setAmountMode] = useState<AmountMode>('random')
  const [amountMin, setAmountMin] = useState('1')
  const [amountMax, setAmountMax] = useState('5')
  const [steps, setSteps] = useState<TransferStepDraft[]>([])
  const [gasPayerWalletId, setGasPayer] = useState<string | null>(null)
  const [intervalEnabled, setIntervalEnabled] = useState(true)
  const [intervalMinSeconds, setIntervalMin] = useState(5)
  const [intervalMaxSeconds, setIntervalMax] = useState(30)
  const [preflight, setPreflight] = useState<JobPreflight | null>(null)
  const initialSource = wallets.find((wallet) => wallet.id === initialSourceWalletId)
  const invalidate = () => { setPreflight(null); setSteps([]) }
  const orderedWallets = useMemo(() => [
    ...groups.flatMap((group) => wallets.filter((wallet) => wallet.groupId === group.id)),
    ...wallets.filter((wallet) => !wallet.groupId),
  ], [groups, wallets])
  const orderedSourceWalletIds = useMemo(() => orderedWallets.filter((wallet) => sourceWalletIds.includes(wallet.id)).map((wallet) => wallet.id), [orderedWallets, sourceWalletIds])
  const targets = useMemo(() => {
    const selected = orderedWallets.filter((wallet) => internalTargetWalletIds.includes(wallet.id))
      .map((wallet) => ({ walletId: wallet.id, address: wallet.address, label: walletOptionLabel(wallet, groups), addressBookEntryId: null as string | null }))
    const typed = externalTargets.split(/[\n,]/).map((value) => value.trim()).filter(Boolean).map((address) => {
      const wallet = wallets.find((item) => item.address.toLowerCase() === address.toLowerCase())
      const entry = addressBookEntryFor(address, addressBook)
      return { walletId: wallet?.id ?? null, address: wallet?.address ?? entry?.address ?? address, label: wallet ? walletOptionLabel(wallet, groups) : entry?.label ?? '外部地址', addressBookEntryId: entry?.id ?? null }
    })
    const unique = new Map<string, { walletId: string | null; address: string; label: string; addressBookEntryId: string | null }>()
    for (const target of [...selected, ...typed]) unique.set(aptosAddressKey(target.address), target)
    return [...unique.values()]
  }, [addressBook, externalTargets, groups, internalTargetWalletIds, orderedWallets, wallets])
  const selectedAddressBookIds = useMemo(() => targets.map((target) => target.addressBookEntryId).filter((id): id is string => Boolean(id)), [targets])
  const pairing = useMemo(() => pairTransferEndpoints(orderedSourceWalletIds, targets), [orderedSourceWalletIds, targets])
  const pairs = pairing.pairs
  const maxConflict = amountMode === 'max' && pairing.mode === 'one_to_many' && pairs.length > 1
  const invalidInterval = intervalEnabled && (!hasAtMostOneDecimalNumber(intervalMinSeconds) || !hasAtMostOneDecimalNumber(intervalMaxSeconds)
    || intervalMinSeconds < 0 || intervalMaxSeconds < intervalMinSeconds || intervalMaxSeconds > 604800
  )
  const randomPrecisionIssue = amountMode === 'random' && [amountMin, amountMax].some((value) => value.trim() && !hasAtMostDecimals(value, 2))
  const canPreview = pairs.length > 0 && pairs.length <= 1000 && !pairing.issue && !maxConflict && !invalidInterval && !randomPrecisionIssue && (amountMode === 'max' || Boolean(amountMin.trim())) && (amountMode !== 'random' || Boolean(amountMax.trim()))
  const preview = () => void run(async () => {
    const generated = pairs.map(({ sourceWalletId, target }) => ({
      id: crypto.randomUUID(), sourceWalletId, targetAddress: target.address, targetWalletId: target.walletId,
      asset, amountMode, amountMin: amountMode === 'max' ? null : amountMin,
      amountMax: amountMode === 'random' ? amountMax : null,
    }))
    setSteps(generated)
    const draft: JobDraftInput = { name, steps: generated, gasPayerWalletId, intervalMinSeconds: intervalEnabled ? intervalMinSeconds : 0, intervalMaxSeconds: intervalEnabled ? intervalMaxSeconds : 0, shuffle: false }
    let checkingJobId: string | null = null
    try {
      const result = await saveAndPreviewJob(draft, undefined, (jobId) => {
        checkingJobId = jobId
        onPreflightStart(jobId)
      })
      setPreflight(result)
      onPreview(result)
    } finally {
      if (checkingJobId) onPreflightFinished(checkingJobId)
    }
  })
  return <>
    <PageHeader title="转账计划" subtitle={initialSource ? `已选择 ${walletOptionLabel(initialSource, groups)}` : '选择转出账户与收款地址，统一设置本次转账'} />
    <section className="transfer-global-settings" aria-label="转账统一设置">
      <div className="settings-intro"><div><span className="settings-kicker">TRANSFER WORKSPACE</span><strong>本次转账规则</strong></div><span>先设定统一规则，再选择账户与地址</span></div>
      <div className="global-settings-row primary-settings">
        <label className="task-name setting-card"><span className="setting-label">计划名称</span><input value={name} onChange={(event) => { invalidate(); setName(event.target.value) }} /></label>
        <fieldset className="segmented-field setting-card"><legend>资产</legend><div className="segmented-control">
          <label><input type="radio" name="asset" value="USDT" checked={asset === 'USDT'} onChange={() => { invalidate(); setAsset('USDT') }} /><span>USDt</span></label>
          <label><input type="radio" name="asset" value="APT" checked={asset === 'APT'} onChange={() => { invalidate(); setAsset('APT') }} /><span>APT</span></label>
        </div></fieldset>
        <fieldset className="segmented-field amount-mode setting-card"><legend>金额方式</legend><div className="segmented-control">
          <label><input type="radio" name="amount-mode" value="random" checked={amountMode === 'random'} onChange={() => { invalidate(); setAmountMode('random') }} /><span>随机范围</span></label>
          <label><input type="radio" name="amount-mode" value="fixed" checked={amountMode === 'fixed'} onChange={() => { invalidate(); setAmountMode('fixed') }} /><span>固定金额</span></label>
          <label><input type="radio" name="amount-mode" value="max" checked={amountMode === 'max'} onChange={() => { invalidate(); setAmountMode('max') }} /><span>全部余额</span></label>
        </div></fieldset>
        {amountMode !== 'max' && <div className={`amount-range-field setting-card ${amountMode === 'random' ? 'is-range' : 'is-fixed'}`}>
          <span className="setting-label">{amountMode === 'random' ? '金额范围' : '转账金额'}</span>
          <div className="amount-range-inputs">
            <label><span className="sr-only">{amountMode === 'random' ? '最小金额' : '转账金额'}</span><div className="amount-input"><input aria-label={amountMode === 'random' ? '最小金额' : '转账金额'} inputMode="decimal" step={amountMode === 'random' ? '0.01' : 'any'} value={amountMin} onChange={(event) => { invalidate(); setAmountMin(event.target.value) }} /><span>{asset === 'USDT' ? 'USDt' : 'APT'}</span></div></label>
            {amountMode === 'random' && <><span className="range-separator" aria-hidden="true">至</span><label><span className="sr-only">最大金额</span><div className="amount-input"><input aria-label="最大金额" inputMode="decimal" step="0.01" value={amountMax} onChange={(event) => { invalidate(); setAmountMax(event.target.value) }} /><span>{asset === 'USDT' ? 'USDt' : 'APT'}</span></div></label></>}
          </div>
          {amountMode === 'random' && <small className="field-hint">每笔独立随机，精确到 0.01</small>}
        </div>}
        {amountMode === 'max' && <div className="amount-range-field setting-card max-amount-note"><span className="setting-label">金额范围</span><strong>执行时读取最新余额</strong><small className="field-hint">每个转出账户的最后一笔出账</small></div>}
      </div>
      <div className="global-settings-row secondary-settings">
        <label className="gas-field setting-card"><span className="setting-label">手续费账户</span><select value={gasPayerWalletId ?? ''} onChange={(event) => { invalidate(); setGasPayer(event.target.value || null) }}><option value="">由每个转出账户支付</option><WalletOptions wallets={wallets} groups={groups} /></select></label>
        <div className={`interval-field setting-card ${intervalEnabled ? '' : 'is-disabled'}`}><div className="interval-field-head"><span className="setting-label">执行节奏</span><label className="interval-switch"><input type="checkbox" aria-label="启用转账间隔" checked={intervalEnabled} onChange={(event) => { invalidate(); setIntervalEnabled(event.target.checked) }} /><span className="interval-switch-track"><span /></span><em>{intervalEnabled ? '随机间隔' : '连续执行'}</em></label></div>{intervalEnabled ? <><div className="interval-inputs"><label><span>最短间隔（秒）</span><input type="number" min="0" max="604800" step="0.1" value={intervalMinSeconds} onChange={(event) => { invalidate(); setIntervalMin(Number(event.target.value)) }} /></label><span className="interval-separator">至</span><label><span>最长间隔（秒）</span><input type="number" min={intervalMinSeconds} max="604800" step="0.1" value={intervalMaxSeconds} onChange={(event) => { invalidate(); setIntervalMax(Number(event.target.value)) }} /></label></div><small className="field-hint">按 0.1 秒随机，打乱顺序后重新抽取</small></> : <div className="interval-off-state"><Clock3 size={17} /><div><strong>不额外等待</strong><span>上一笔确认后立即执行下一笔</span></div></div>}</div>
      </div>
    </section>
    {preflight && <div className={`preflight-summary ${preflight.valid ? 'valid' : 'invalid'}`}>
      {preflight.valid ? <Check size={17} /> : <ShieldAlert size={17} />}
      <div><strong>{preflight.valid ? '检查通过' : `${preflight.checks.filter((check) => !check.valid).length} 笔转账需要处理`}</strong><span>预计手续费 {formatAmount(preflight.summary?.estimatedGasBaseUnits ?? '0', 'APT')} APT</span></div>
    </div>}
    <section className="transfer-compose-grid">
      <div className="selection-panel source-panel">
        <div className="selection-head"><div><span className="step-number">1</span><div><h2>转出账户</h2><p>已选 {sourceWalletIds.length} 个</p></div></div><div className="selection-head-actions">{sourceWalletIds.length > 0 && <button className="text-button" onClick={() => { invalidate(); setSourceWalletIds([]) }}>清空</button>}<IconButton title="添加转出账户" icon={<Plus size={17} />} onClick={() => setSourcePickerOpen(true)} /></div></div>
        <div className="selected-targets selected-sources" aria-live="polite">
          {orderedSourceWalletIds.length === 0 ? <div className="target-empty">尚未添加转出账户</div> : orderedSourceWalletIds.map((walletId) => { const wallet = wallets.find((item) => item.id === walletId)!; return <div className="selected-target selected-source" key={wallet.id}><div><strong>{walletOptionLabel(wallet, groups)}</strong><code>{short(wallet.address)}</code></div><div className="selected-source-meta"><span>{wallet.balances.find((balance) => balance.asset === 'USDT')?.display ?? '-'} USDt</span><span>{wallet.balances.find((balance) => balance.asset === 'APT')?.display ?? '-'} APT</span></div><IconButton title={`移除 ${walletOptionLabel(wallet, groups)}`} icon={<X size={15} />} onClick={() => { invalidate(); setSourceWalletIds((current) => current.filter((id) => id !== wallet.id)) }} /></div> })}
        </div>
      </div>
      <div className="compose-arrow" aria-hidden="true"><ArrowRight size={20} /><span>{pairs.length} 笔</span></div>
      <div className="selection-panel target-panel">
        <div className="selection-head"><div><span className="step-number">2</span><div><h2>收款地址</h2><p>已选 {targets.length} 个</p></div></div><div className="selection-head-actions"><button className="secondary small" onClick={() => setTargetPickerOpen(true)}><WalletCards size={14} />从我的账户选择</button><button className="secondary small" onClick={() => setAddressBookPickerOpen(true)}><BookUser size={14} />地址簿</button><IconButton title="添加外部地址" icon={<Plus size={17} />} onClick={() => setExternalTargetOpen(true)} /></div></div>
        <div className="selected-targets" aria-live="polite">
          {targets.length === 0 ? <div className="target-empty">尚未添加收款地址</div> : targets.map((target) => <div className={`selected-target ${target.addressBookEntryId ? 'from-address-book' : ''}`} key={target.address}><div><strong>{target.label}</strong>{target.addressBookEntryId && <span className="address-book-chip"><BookUser size={11} />地址簿</span>}<code>{short(target.address)}</code></div><IconButton title={`移除 ${target.label}`} icon={<X size={15} />} onClick={() => {
            invalidate()
            if (target.walletId) setInternalTargetWalletIds((current) => current.filter((id) => id !== target.walletId))
            setExternalTargets((current) => current.split(/[\n,]/).map((value) => value.trim()).filter((value) => value && value.toLowerCase() !== target.address.toLowerCase()).join('\n'))
          }} /></div>)}
        </div>
      </div>
    </section>
    {pairing.mode === 'one_to_one' && pairs.length > 1 && !pairing.issue && <section className="pairing-preview" aria-label="一一配对预览">
      <div className="pairing-preview-head"><strong>按顺序一一配对</strong><span>第 N 个转出账户对应第 N 个收款地址</span></div>
      {pairs.slice(0, 10).map(({ sourceWalletId, target }, position) => <div className="pairing-preview-row" key={`${sourceWalletId}:${target.address}`}><span>{position + 1}</span><TransferParty wallet={wallets.find((wallet) => wallet.id === sourceWalletId)} address={wallets.find((wallet) => wallet.id === sourceWalletId)?.address ?? sourceWalletId} /><ArrowRight size={14} /><TransferParty wallet={target.walletId ? wallets.find((wallet) => wallet.id === target.walletId) : null} address={target.address} addressBook={addressBook} /></div>)}
      {pairs.length > 10 && <div className="list-overflow-note">另有 {pairs.length - 10} 组配对，将继续按当前顺序处理。</div>}
    </section>}
    <section className="transfer-summary-band">
      <div><strong>{sourceWalletIds.length} 个转出账户 → {targets.length} 个收款地址</strong><span>{pairing.mode === 'one_to_many' ? `一对多，共生成 ${pairs.length} 笔转账` : pairing.mode === 'many_to_one' ? `多对一，共生成 ${pairs.length} 笔转账` : pairing.mode === 'one_to_one' ? `按顺序一一对应，共生成 ${pairs.length} 笔转账` : '请选择可配对的账户与地址'}</span></div>
      <div className="summary-amount"><span>统一金额</span><strong>{amountMode === 'max' ? '全部余额' : amountMode === 'random' ? `${amountMin || '-'} - ${amountMax || '-'} ${asset === 'USDT' ? 'USDt' : 'APT'}` : `${amountMin || '-'} ${asset === 'USDT' ? 'USDt' : 'APT'}`}</strong></div>
      <button className="primary" onClick={preview} disabled={busy || !canPreview}><Eye size={16} />进入转账预览</button>
    </section>
    {(pairing.issue || maxConflict || pairs.length > 1000 || invalidInterval || randomPrecisionIssue) && <div className="error-banner"><ShieldAlert size={17} />{pairing.issue?.kind === 'count_mismatch' ? `多对多转账必须一一对应：当前有 ${sourceWalletIds.length} 个转出账户和 ${targets.length} 个收款地址，请调整为相同数量。` : pairing.issue?.kind === 'self_transfer' ? `第 ${pairing.issue.position + 1} 组的转出账户和收款账户相同，请调整对应顺序或收款地址。` : maxConflict ? '全部余额模式下，一个转出账户只能对应一个收款地址。' : pairs.length > 1000 ? '转账超过 1000 笔，请减少账户或地址。' : invalidInterval ? '最长间隔不能小于最短间隔。' : '随机金额最多保留 2 位小数，例如 1.25。'}</div>}
    {preflight && <TransferCheckList steps={steps} checks={preflight.checks} wallets={wallets} addressBook={addressBook} />}
    {sourcePickerOpen && <AccountPickerDialog title="选择转出账户" searchLabel="搜索转出账户" wallets={wallets} groups={groups} selected={sourceWalletIds} setSelected={(ids) => { invalidate(); setSourceWalletIds(ids) }} close={() => setSourcePickerOpen(false)} />}
    {targetPickerOpen && <AccountPickerDialog title="从我的账户选择" searchLabel="搜索收款账户" wallets={wallets} groups={groups} selected={internalTargetWalletIds} setSelected={(ids) => { invalidate(); setInternalTargetWalletIds(ids) }} close={() => setTargetPickerOpen(false)} />}
    {addressBookPickerOpen && <AddressBookPickerDialog entries={addressBook} selected={selectedAddressBookIds} close={() => setAddressBookPickerOpen(false)} setSelected={(ids) => {
      invalidate()
      const knownKeys = new Set(addressBook.map((entry) => aptosAddressKey(entry.address)))
      const manual = externalTargets.split(/[\n,]/).map((value) => value.trim()).filter((value) => value && !knownKeys.has(aptosAddressKey(value)))
      const selectedAddresses = addressBook.filter((entry) => ids.includes(entry.id)).map((entry) => entry.address)
      setExternalTargets([...manual, ...selectedAddresses].join('\n'))
    }} />}
    {externalTargetOpen && <ExternalTargetDialog close={() => setExternalTargetOpen(false)} onAdd={(value) => {
      invalidate()
      setExternalTargets((current) => [current.trim(), value.trim()].filter(Boolean).join('\n'))
      setExternalTargetOpen(false)
    }} />}
  </>
}

function JobsView({ jobs, wallets, addressBook, run, inspectJob, focusedJobId, onFocusedJobHandled, setPreviewJob, setModal }: {
  jobs: TransferJob[]; wallets: WalletRecord[]; addressBook: AddressBookEntry[]; run: (action: () => Promise<void>, success?: string) => Promise<void>
  inspectJob: (id: string) => Promise<void>; focusedJobId: string | null; onFocusedJobHandled: (id: string) => void
  setPreviewJob: (job: TransferJob) => void; setModal: (value: Modal) => void
}) {
  const pageSize = 10
  const [selectedId, setSelectedId] = useState<string | null>(jobs[0]?.id ?? null)
  const [dateFrom, setDateFrom] = useState('')
  const [dateTo, setDateTo] = useState('')
  const [pageIndex, setPageIndex] = useState(1)
  const dateFromTime = dateFrom ? new Date(`${dateFrom}T00:00:00`).getTime() : null
  const dateToTime = dateTo ? new Date(`${dateTo}T23:59:59.999`).getTime() : null
  const filteredJobs = useMemo(() => jobs.filter((job) => {
    const createdAt = Date.parse(job.createdAt)
    return Number.isFinite(createdAt)
      && (dateFromTime === null || !Number.isFinite(dateFromTime) || createdAt >= dateFromTime)
      && (dateToTime === null || !Number.isFinite(dateToTime) || createdAt <= dateToTime)
  }), [dateFromTime, dateToTime, jobs])
  const pageCount = Math.max(1, Math.ceil(filteredJobs.length / pageSize))
  const currentPage = Math.min(pageIndex, pageCount)
  const visibleJobs = filteredJobs.slice((currentPage - 1) * pageSize, currentPage * pageSize)
  const selected = filteredJobs.find((job) => job.id === selectedId) ?? filteredJobs[0]
  const [clock, setClock] = useState(() => Date.now())
  useEffect(() => {
    if (!focusedJobId || !jobs.some((job) => job.id === focusedJobId)) return
    setDateFrom('')
    setDateTo('')
    setPageIndex(Math.floor(jobs.findIndex((job) => job.id === focusedJobId) / pageSize) + 1)
    setSelectedId(focusedJobId)
    onFocusedJobHandled(focusedJobId)
  }, [focusedJobId, jobs, onFocusedJobHandled])
  useEffect(() => { setPageIndex(1) }, [dateFrom, dateTo])
  useEffect(() => {
    if (pageIndex !== currentPage) setPageIndex(currentPage)
  }, [currentPage, pageIndex])
  useEffect(() => {
    if (selectedId && filteredJobs.some((job) => job.id === selectedId)) return
    setSelectedId(filteredJobs[0]?.id ?? null)
  }, [filteredJobs, selectedId])
  useEffect(() => {
    if (selected?.status !== 'running') return
    const timer = window.setInterval(() => setClock(Date.now()), 250)
    return () => window.clearInterval(timer)
  }, [selected?.id, selected?.status])
  useEffect(() => {
    if (!selected?.id) return
    // Opening a record opportunistically fills legacy gas fees from its known
    // transaction hashes. Failures stay silent so history remains available.
    void inspectJob(selected.id).catch(() => undefined)
  }, [inspectJob, selected?.id])
  const waitingStep = selected?.steps.find((step) => step.status === 'waiting')
  const waitingPrevious = waitingStep && selected ? selected.steps.find((step) => step.position === waitingStep.position - 1) : undefined
  const waitingTotalSeconds = waitingPrevious?.waitAfterSeconds ?? 0
  const waitingStartedAt = waitingStep?.updatedAt ? Date.parse(waitingStep.updatedAt) : Number.NaN
  const waitingElapsedSeconds = Number.isFinite(waitingStartedAt) ? Math.max(0, (clock - waitingStartedAt) / 1000) : 0
  const waitingRemainingSeconds = waitingStep ? Math.max(0, Math.ceil((waitingTotalSeconds - waitingElapsedSeconds) * 10) / 10) : 0
  const activeStep = selected?.steps.find((step) => step.status === 'preparing' || step.status === 'submitting')
  const confirmedCount = selected?.steps.filter((step) => step.status === 'confirmed').length ?? 0
  const actualGasFeeBaseUnits = selected?.steps.reduce((total, step) => total + BigInt(step.gasFeeBaseUnits ?? '0'), 0n) ?? 0n
  const hasActualGasFee = selected?.steps.some((step) => step.gasFeeBaseUnits !== null && step.gasFeeBaseUnits !== undefined) ?? false
  const activityText = selected?.status !== 'running' ? null
    : waitingStep ? `正在等待第 ${waitingStep.position + 1} 笔`
      : activeStep ? `${activeStep.status === 'submitting' ? '正在提交' : '正在准备'}第 ${activeStep.position + 1} 笔`
        : confirmedCount >= (selected?.steps.length ?? 0) ? '正在收尾' : '正在检查下一笔'
  return <>
    <PageHeader title="执行记录" subtitle={filteredJobs.length === jobs.length ? `${jobs.length} 个任务` : `筛选结果 ${filteredJobs.length} / ${jobs.length} 个任务`} />
    <div className="jobs-layout"><section className="job-list">
      <div className="job-list-filter">
        <div className="job-date-fields">
          <label><span>开始日期</span><input aria-label="执行记录开始日期" type="date" value={dateFrom} max={dateTo || undefined} onChange={(event) => setDateFrom(event.target.value)} /></label>
          <span className="job-date-separator">至</span>
          <label><span>结束日期</span><input aria-label="执行记录结束日期" type="date" value={dateTo} min={dateFrom || undefined} onChange={(event) => setDateTo(event.target.value)} /></label>
        </div>
        <button className="text-button job-date-clear" disabled={!dateFrom && !dateTo} onClick={() => { setDateFrom(''); setDateTo('') }}>清空日期</button>
      </div>
      {filteredJobs.length === 0 ? <Empty icon={<FileClock size={28} />} text={jobs.length ? '这个日期范围内没有转账记录。' : '还没有转账任务。'} /> : <>
        <div className="job-list-items">{visibleJobs.map((job) => <button key={job.id} className={`job-item ${selected?.id === job.id ? 'selected' : ''}`} onClick={() => setSelectedId(job.id)}>
          <div><strong>{job.name}</strong><span>{new Date(job.createdAt).toLocaleString()}</span></div><div><Status value={job.status} /><ChevronRight size={16} /></div>
        </button>)}</div>
        <div className="job-list-pagination" aria-label="执行记录分页">
          <button className="secondary small" disabled={currentPage <= 1} onClick={() => setPageIndex((page) => Math.max(1, page - 1))}>上一页</button>
          <span>第 {currentPage} / {pageCount} 页</span>
          <label className="job-page-jump"><span>跳至</span><input aria-label="跳转执行记录页码" type="number" min="1" max={pageCount} value={currentPage} onChange={(event) => {
            const next = Number(event.target.value)
            if (Number.isInteger(next)) setPageIndex(Math.min(pageCount, Math.max(1, next)))
          }} /><span>页</span></label>
          <button className="secondary small" disabled={currentPage >= pageCount} onClick={() => setPageIndex((page) => Math.min(pageCount, page + 1))}>下一页</button>
        </div>
      </>}
    </section>{selected && <section className="job-detail">
      <div className="job-detail-head"><div><h2>{selected.name}</h2><Status value={selected.status} /></div><div className="header-actions">
        {selected.status === 'previewed' && <button className="primary" onClick={() => { setPreviewJob(selected); setModal('confirm') }}><Check size={16} />确认执行</button>}
        {selected.status === 'running' && <button className="secondary" onClick={() => void run(async () => { await post(`/api/v1/jobs/${selected.id}/pause`) })}>暂停</button>}
        {selected.status === 'paused' && <button className="primary" onClick={() => void run(async () => { await post(`/api/v1/jobs/${selected.id}/resume`) })}>恢复</button>}
        {selected.status === 'uncertain' && <button className="secondary" onClick={() => void run(async () => { await post(`/api/v1/jobs/${selected.id}/reconcile`) }, '已重新核对链上状态')}>重新核对</button>}
        {selected.status === 'failed' && selected.steps.find((step) => step.status === 'failed') && <button className="primary" onClick={() => { setPreviewJob(selected); setModal('retryFailed') }}><RotateCcw size={16} />从第 {selected.steps.find((step) => step.status === 'failed')!.position + 1} 笔重试</button>}
        {['draft', 'previewed', 'running', 'paused'].includes(selected.status) && <button className="danger-button" onClick={() => void run(async () => { await post(`/api/v1/jobs/${selected.id}/cancel`) })}>取消</button>}
      </div></div>
      {selected.error && <div className="error-banner"><ShieldAlert size={17} />{presentError(selected.error)}</div>}
      <div className="progress-line"><span style={{ width: `${selected.steps.length ? selected.steps.filter((step) => step.status === 'confirmed').length / selected.steps.length * 100 : 0}%` }} /></div>
      <div className="detail-meta"><span>{confirmedCount}/{selected.steps.length} 笔已确认</span><span>实际手续费 {hasActualGasFee ? formatGasFee(actualGasFeeBaseUnits.toString()) : '-'}</span><span>{selected.intervalMaxSeconds > 0 ? `间隔 ${formatSeconds(selected.intervalMinSeconds)}-${formatSeconds(selected.intervalMaxSeconds)} 秒` : '连续执行'}</span><span>{selected.shuffle ? '随机顺序' : '清单顺序'}</span></div>
      {activityText && <div className={`job-activity ${waitingStep ? 'is-waiting' : 'is-active'}`} aria-live="polite">
        <div className="job-activity-copy"><span className="job-activity-dot" /><div><strong>{activityText}</strong><span>{waitingStep ? `第 ${waitingStep.position} 笔已完成，下一笔将在倒计时结束后开始` : `已完成 ${confirmedCount} / ${selected.steps.length} 笔`}</span></div></div>
        {waitingStep && <div className="job-countdown"><strong>{formatSeconds(waitingRemainingSeconds)}</strong><span>秒</span></div>}
      </div>}
      <div className="table-scroll"><table><thead><tr><th>#</th><th>来源</th><th>目标</th><th>资产</th><th>金额</th><th>手续费</th><th>等待</th><th>状态</th><th>交易</th></tr></thead><tbody>{selected.steps.map((step) => <tr className={step.id === waitingStep?.id ? 'job-step-waiting' : step.id === activeStep?.id ? 'job-step-active' : ''} key={step.id}>
        <td>{step.position + 1}</td><td><TransferParty wallet={wallets.find((wallet) => wallet.id === step.sourceWalletId)} address={wallets.find((wallet) => wallet.id === step.sourceWalletId)?.address ?? step.sourceWalletId} /></td><td><TransferParty wallet={step.targetWalletId ? wallets.find((wallet) => wallet.id === step.targetWalletId) : null} address={step.targetAddress} addressBook={addressBook} /></td><td>{step.asset === 'USDT' ? 'USDt' : 'APT'}</td>
        <td className="amount">{step.amountMode === 'max' ? `全额${step.frozenAmountDisplay ? ` (~${step.frozenAmountDisplay})` : ''}` : step.frozenAmountDisplay}</td><td className="gas-fee">{formatGasFee(step.gasFeeBaseUnits)}</td><td>{step.waitAfterSeconds ? `${formatSeconds(step.waitAfterSeconds)}s` : '-'}</td><td>{step.id === waitingStep?.id ? <span className="status status-waiting status-countdown"><Clock3 size={12} />等待 {formatSeconds(waitingRemainingSeconds)}s</span> : <Status value={step.status} />}</td>
        <td>{step.txHash ? <a className="tx-link" href={`https://explorer.aptoslabs.com/txn/${step.txHash}?network=mainnet`} target="_blank" rel="noreferrer">{short(step.txHash)}</a> : step.error ? <span className="row-error">{presentError(step.error)}</span> : '-'}</td>
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
  const [confirmationName, setName] = useState('')
  const nameMatches = confirmationName === group.label
  return <Dialog title="归档钱包" close={close}><form onSubmit={(event) => { event.preventDefault(); void run(async () => {
    if (!nameMatches) return
    await post(`/api/v1/wallets/groups/${group.id}/archive`, { confirmationName }); await reload(); close()
  }, '钱包已归档') }}>
    <div className="warning-box"><Archive size={16} />归档后，这个钱包里的账户不会出现在转账选择器中。恢复钱包时需要主密码。</div>
    <label>手动输入完整钱包名称<input autoComplete="off" required value={confirmationName} onChange={(event) => setName(event.target.value)} placeholder={`请手动输入：${group.label}`} />{confirmationName && !nameMatches && <span className="row-error">钱包名称不匹配，请按上方提示完整输入。</span>}</label>
    <DialogActions close={close} submit="确认归档" disabled={!nameMatches} />
  </form></Dialog>
}

function SecretAuthDialog({ target, close, run, onSecret }: { target: SecretTarget; close: () => void; run: DialogProps['run']; onSecret: (title: string, value: string) => void }) {
  const entity = target.kind === 'mnemonic' ? target.group : target.wallet
  const [password, setPassword] = useState('')
  const [confirmationName, setName] = useState('')
  const nameMatches = confirmationName === entity.label
  return <Dialog title={target.kind === 'mnemonic' ? '查看助记词' : '查看私钥'} close={close}><form onSubmit={(event) => { event.preventDefault(); void run(async () => {
    const value = await requestEncryptedSecret((publicKey) => target.kind === 'mnemonic'
      ? post(`/api/v1/wallets/groups/${target.group.id}/reveal-mnemonic`, { password, confirmationName, publicKey })
      : post(`/api/v1/wallets/${target.wallet.id}/reveal`, { password, confirmationName, publicKey }))
    onSecret(target.kind === 'mnemonic' ? `${entity.label} · 助记词` : `${entity.label} · 私钥`, value)
  }) }}>
    <label>主密码<input type="password" minLength={12} required value={password} onChange={(event) => setPassword(event.target.value)} /></label>
    <label>手动输入完整名称<input autoComplete="off" required value={confirmationName} onChange={(event) => setName(event.target.value)} placeholder={`请手动输入：${entity.label}`} />{confirmationName && !nameMatches && <span className="row-error">名称不匹配，请按上方提示完整输入。</span>}</label>
    <DialogActions close={close} submit="安全显示" disabled={password.length < 12 || !nameMatches} />
  </form></Dialog>
}

function ArchivedDialog({ close, run, reload }: DialogProps) {
  const [wallets, setWallets] = useState<WalletRecord[]>([])
  const [groups, setGroups] = useState<WalletGroup[]>([])
  const [restoreTarget, setRestoreTarget] = useState<{ path: string; label: string; success: string; kind: 'group' | 'wallet' } | null>(null)
  const load = async () => {
    const [allWallets, allGroups] = await Promise.all([request<WalletRecord[]>('/api/v1/wallets?includeArchived=true'), request<WalletGroup[]>('/api/v1/wallets/groups?includeArchived=true')])
    setWallets(allWallets.filter((wallet) => wallet.archivedAt)); setGroups(allGroups.filter((group) => group.archivedAt))
  }
  useEffect(() => { void load() }, [])
  const groupIds = new Set(groups.map((group) => group.id))
  const individual = wallets.filter((wallet) => !wallet.groupId || !groupIds.has(wallet.groupId))
  return <>
    <Dialog title="归档" close={close} wide><div className="archive-list">
      {groups.map((group) => <div key={group.id}><div><strong>{group.label}</strong><span>{group.totalAccountCount} 个账户</span></div><button className="secondary" onClick={() => setRestoreTarget({ path: `/api/v1/wallets/groups/${group.id}/unarchive`, label: group.label, success: '钱包已恢复', kind: 'group' })}><RotateCcw size={15} />恢复</button></div>)}
      {individual.map((wallet) => <div key={wallet.id}><div><strong>{wallet.label}</strong><Address value={wallet.address} /></div><button className="secondary" onClick={() => setRestoreTarget({ path: `/api/v1/wallets/${wallet.id}/unarchive`, label: wallet.label, success: '账户已恢复', kind: 'wallet' })}><RotateCcw size={15} />恢复</button></div>)}
      {!groups.length && !individual.length && <Empty icon={<Archive size={28} />} text="归档为空。" />}
    </div></Dialog>
    {restoreTarget && <ArchiveRestoreDialog target={restoreTarget} close={() => setRestoreTarget(null)} run={run} reload={async () => { await load(); await reload() }} />}
  </>
}

function ArchiveRestoreDialog({ target, close, run, reload }: {
  target: { path: string; label: string; success: string; kind: 'group' | 'wallet' }
} & DialogProps) {
  const [password, setPassword] = useState('')
  return <Dialog title={`恢复${target.kind === 'group' ? '钱包' : '账户'}`} close={close}><form onSubmit={(event) => { event.preventDefault(); void run(async () => {
    await post(target.path, { password }); await reload(); close()
  }, target.success) }}>
    <div className="warning-box"><RotateCcw size={16} />恢复“{target.label}”后，它会重新出现在钱包和转账选择器中。</div>
    <label>主密码<input type="password" minLength={12} required autoComplete="current-password" value={password} onChange={(event) => setPassword(event.target.value)} /></label>
    <DialogActions close={close} submit="验证并恢复" disabled={password.length < 12} />
  </form></Dialog>
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

function AccountDetailsDialog({ wallet, close, run, reload, onReceive, onHistory, onAlias, onTransfer, onReveal }: {
  wallet: WalletRecord; close: () => void; run: DialogProps['run']; reload: () => Promise<void>
  onReceive: () => void; onHistory: () => void; onAlias: () => void; onTransfer: () => void; onReveal: () => void
}) {
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
      <button onClick={onHistory}><FileClock size={15} />转账日志</button>
      <button onClick={onAlias}><Pencil size={15} />设置别名</button>
      <button onClick={onReveal}><Eye size={15} />查看私钥</button>
      <button className="danger-text" onClick={archive}><Archive size={15} />归档账户</button>
    </div>
  </Dialog>
}

function AccountAliasDialog({ wallet, close, run, onUpdated }: {
  wallet: WalletRecord; close: () => void; run: DialogProps['run']; onUpdated: (wallet: WalletRecord) => void
}) {
  const [alias, setAlias] = useState(wallet.label)
  const normalized = alias.trim()
  const unchanged = normalized === wallet.label
  return <Dialog title="设置账户别名" close={close}><form onSubmit={(event) => {
    event.preventDefault()
    if (!normalized || unchanged) return
    void run(async () => {
      const updated = await request<WalletRecord>(`/api/v1/wallets/${wallet.id}`, { method: 'PATCH', body: JSON.stringify({ label: normalized }) })
      onUpdated(updated)
      close()
    }, '账户别名已保存')
  }}>
    <label>账户别名<input autoFocus maxLength={120} value={alias} onChange={(event) => setAlias(event.target.value)} placeholder="例如：日常付款、归集账户" /></label>
    <div className="detail-list alias-account-summary"><div><span>当前账户</span><strong>{accountLabel(wallet)}</strong></div><div><span>地址</span><Address value={wallet.address} /></div></div>
    <p className="form-hint">别名只保存在本机，用于钱包列表、转账选择和日志识别，不会改变链上地址。</p>
    <div className="dialog-actions"><button type="button" className="secondary" onClick={close}>取消</button><button type="submit" className="primary" disabled={!normalized || unchanged}>保存别名</button></div>
  </form></Dialog>
}

function AccountHistoryDialog({ wallet, wallets, addressBook, close }: { wallet: WalletRecord; wallets: WalletRecord[]; addressBook: AddressBookEntry[]; close: () => void }) {
  const pageSize = 50
  const [direction, setDirection] = useState<'all' | 'in' | 'out'>('all')
  const [page, setPage] = useState(0)
  const [result, setResult] = useState<AccountTransferLogPage | null>(null)
  const [loading, setLoading] = useState(true)
  const [syncing, setSyncing] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const loadRequest = useRef(0)
  const load = async () => {
    const requestId = ++loadRequest.current
    setLoading(true)
    setError(null)
    try {
      const next = await request<AccountTransferLogPage>(`/api/v1/wallets/${wallet.id}/transfers?direction=${direction}&limit=${pageSize}&offset=${page * pageSize}`)
      if (requestId === loadRequest.current) setResult(next)
    } catch (loadError) {
      if (requestId === loadRequest.current) setError(loadError instanceof Error ? loadError.message : '日志加载失败')
    } finally {
      if (requestId === loadRequest.current) setLoading(false)
    }
  }
  const syncOlder = async () => {
    setSyncing(true)
    setError(null)
    try {
      const next = await request<AccountTransferLogPage>(`/api/v1/wallets/${wallet.id}/transfers/sync?direction=${direction}&limit=${pageSize}&offset=${page * pageSize}`, { method: 'POST', body: '{}' })
      setResult(next)
    } catch (syncError) {
      setError(syncError instanceof Error ? syncError.message : '链上日志同步失败')
    } finally {
      setSyncing(false)
    }
  }
  useEffect(() => { void load() }, [wallet.id, direction, page])
  const counts = result?.counts ?? { all: 0, in: 0, out: 0 }
  return <Dialog title={`${accountLabel(wallet)} · 转账日志`} close={close} wide>
    <div className="history-toolbar">
      <div className="segmented history-filter" role="group" aria-label="日志方向">
        {([['all', '全部', counts.all], ['out', '转出', counts.out], ['in', '转入', counts.in]] as const).map(([value, label, count]) => <button className={direction === value ? 'active' : ''} aria-pressed={direction === value} key={value} onClick={() => { setDirection(value); setPage(0) }}>{label}<span>{count}</span></button>)}
      </div>
      <div className="history-tools">{result?.sync.hasMore && <button className="secondary small chain-sync-button" disabled={syncing} onClick={() => void syncOlder()}><CloudDownload className={syncing ? 'spin' : ''} size={14} />{syncing ? '正在同步' : '与链上同步'}</button>}<IconButton title="刷新日志" icon={<RefreshCw className={loading ? 'spin' : ''} size={16} />} onClick={() => void load()} /><a className="secondary small" href={`https://explorer.aptoslabs.com/account/${wallet.address}?network=mainnet`} target="_blank" rel="noreferrer">链上记录<ArrowUpRight size={14} /></a></div>
    </div>
    {error && <div className="error-banner"><ShieldAlert size={17} />{presentError(error)}</div>}
    {result?.sync.error && <div className="error-banner"><ShieldAlert size={17} />链上日志暂时同步失败：{presentError(result.sync.error)}</div>}
    {result && (result.sync.added > 0 || result.sync.hasMore) && <div className="chain-sync-status"><CloudDownload size={15} /><div><strong>{result.sync.added > 0 ? `本次补充 ${result.sync.added} 条链上记录` : '本地日志尚未追平链上'}</strong><span>{result.sync.hasMore ? '为控制请求量，每次最多继续同步 5 笔。' : '当前可见链上记录已经同步完成。'}</span></div></div>}
    {loading && !result ? <div className="history-loading"><RefreshCw className="spin" size={20} />正在读取日志...</div> : result?.items.length === 0 ? <Empty icon={<FileClock size={28} />} text="暂无转账记录。" /> : <div className="account-history-list">
      {result?.items.map((item) => <AccountHistoryRow key={item.id} item={item} wallets={wallets} addressBook={addressBook} />)}
    </div>}
    {result && result.total > pageSize && <div className="history-pagination"><span>第 {page + 1} 页 · 共 {result.total} 条</span><div><button className="secondary small" disabled={page === 0 || loading} onClick={() => setPage((current) => current - 1)}>上一页</button><button className="secondary small" disabled={(page + 1) * pageSize >= result.total || loading} onClick={() => setPage((current) => current + 1)}>下一页</button></div></div>}
  </Dialog>
}

function AccountHistoryRow({ item, wallets, addressBook }: { item: AccountTransferLog; wallets: WalletRecord[]; addressBook: AddressBookEntry[] }) {
  const counterparty = item.counterpartyWalletId ? wallets.find((wallet) => wallet.id === item.counterpartyWalletId) : null
  const addressBookEntry = counterparty ? null : addressBookEntryFor(item.counterpartyAddress, addressBook)
  return <article className={`account-history-row direction-${item.direction}`}>
    <div className={`history-direction ${item.direction}`}>{item.direction === 'out' ? <ArrowUpRight size={17} /> : <ArrowDownLeft size={17} />}<span>{item.direction === 'out' ? '转出' : '转入'}</span></div>
    <div className="history-main"><div><strong>{historyAmount(item)}</strong><Status value={item.status} /></div><span>{item.source === 'chain' ? '链上同步记录' : `${item.jobName} · 第 ${item.position + 1} 笔`}</span><small>实际手续费 {formatGasFee(item.gasFeeBaseUnits)}</small></div>
    <div className={`history-counterparty ${addressBookEntry ? 'from-address-book' : ''}`}><span>{item.direction === 'out' ? '收款方' : '转出方'}</span><strong>{counterparty ? accountLabel(counterparty) : addressBookEntry?.label ?? '外部地址'}</strong>{addressBookEntry && <small className="address-book-chip"><BookUser size={11} />地址簿</small>}<Address value={item.counterpartyAddress} /></div>
    <div className="history-time"><span>{new Date(item.updatedAt).toLocaleString()}</span>{item.txHash ? <a href={`https://explorer.aptoslabs.com/txn/${item.txHash}?network=mainnet`} target="_blank" rel="noreferrer">{short(item.txHash)}<ArrowUpRight size={12} /></a> : <small>暂无交易哈希</small>}</div>
    {item.error && <div className="history-error"><ShieldAlert size={13} />{item.error}</div>}
  </article>
}

function DerivedPreview({ result }: { result: MnemonicRestorePreview }) {
  return <div className="derived-preview">{result.accounts.map((account) => <div key={account.accountIndex}><strong>账户 {account.accountIndex + 1}</strong><Address value={account.address} /></div>)}</div>
}

function TransferCheckList({ steps, checks, wallets, addressBook }: { steps: TransferStepDraft[]; checks: JobPreflight['checks']; wallets: WalletRecord[]; addressBook: AddressBookEntry[] }) {
  const rows = steps.map((step, index) => ({ step, index, check: checks.find((item) => item.stepId === step.id) }))
  const visible = [...rows.filter((row) => row.check && !row.check.valid), ...rows.filter((row) => !row.check || row.check.valid)].slice(0, 100)
  return <section className="transfer-check-list">
    <div className="section-head"><div><h2>检查结果</h2><span>{steps.length} 笔转账，手续费按链上模拟估算</span></div></div>
    {visible.map(({ step, index, check }) => <div className={`step-row transfer-check-row ${check && !check.valid ? 'step-invalid' : ''}`} key={step.id}>
      <span className="check-position">{index + 1}</span>
      <div><span>转出</span><TransferParty wallet={wallets.find((wallet) => wallet.id === step.sourceWalletId)} address={wallets.find((wallet) => wallet.id === step.sourceWalletId)?.address ?? step.sourceWalletId} /></div>
      <div><span>收款</span><TransferParty wallet={step.targetWalletId ? wallets.find((wallet) => wallet.id === step.targetWalletId) : null} address={step.targetAddress} addressBook={addressBook} /></div>
      <div><span>金额</span><strong>{step.amountMode === 'max' ? '全部余额' : step.amountMode === 'random' ? `${step.amountMin} - ${step.amountMax}` : step.amountMin} {step.asset === 'USDT' ? 'USDt' : 'APT'}</strong></div>
      <div className={`step-check ${check?.valid ? 'valid' : 'invalid'}`}>{check?.valid ? <Check size={14} /> : <ShieldAlert size={14} />}<span>{check?.error ?? '余额与手续费检查通过'}</span><strong>{check && BigInt(check.estimatedGasBaseUnits) > 0n ? `约 ${formatAmount(check.estimatedGasBaseUnits, 'APT')} APT` : '待估算'}</strong></div>
    </div>)}
    {steps.length > visible.length && <div className="list-overflow-note">仅展示前 100 笔，完整清单将在确认页冻结。</div>}
  </section>
}

function AccountPickerDialog({ title, searchLabel, wallets, groups, selected, setSelected, close }: { title: string; searchLabel: string; wallets: WalletRecord[]; groups: WalletGroup[]; selected: string[]; setSelected: (ids: string[]) => void; close: () => void }) {
  const [draft, setDraft] = useState(selected)
  const [search, setSearch] = useState('')
  const toggle = (id: string) => setDraft((current) => current.includes(id) ? current.filter((value) => value !== id) : [...current, id])
  const selectGroup = (ids: string[]) => setDraft((current) => ids.every((id) => current.includes(id)) ? current.filter((id) => !ids.includes(id)) : [...new Set([...current, ...ids])])
  return <Dialog title={title} close={close} wide>
    <label className="selection-search dialog-search"><Search size={15} /><input aria-label={searchLabel} value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索账户或地址" /></label>
    <div className="address-book-list"><WalletSelectionList wallets={wallets} groups={groups} selected={draft} search={search} onToggle={toggle} onSelectGroup={selectGroup} /></div>
    <div className="dialog-actions"><button className="secondary" onClick={close}>取消</button><button className="primary" onClick={() => { setSelected(draft); close() }}><Check size={16} />确定选择 {draft.length} 个</button></div>
  </Dialog>
}

function AddressBookPickerDialog({ entries, selected, setSelected, close }: {
  entries: AddressBookEntry[]; selected: string[]; setSelected: (ids: string[]) => void; close: () => void
}) {
  const [draft, setDraft] = useState(selected)
  const [search, setSearch] = useState('')
  const term = search.trim().toLowerCase()
  const visible = entries.filter((entry) => !term || `${entry.label} ${entry.address}`.toLowerCase().includes(term))
  const toggle = (id: string) => setDraft((current) => current.includes(id) ? current.filter((value) => value !== id) : [...current, id])
  return <Dialog title="从地址簿选择" close={close} wide>
    <label className="selection-search dialog-search"><Search size={15} /><input aria-label="搜索地址簿收款地址" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索别名或地址" /></label>
    <div className="address-book-picker-list">{visible.length === 0 ? <Empty icon={<BookUser size={28} />} text={entries.length ? '没有匹配的地址。' : '地址簿还是空的，请先在地址簿页面添加。'} /> : visible.map((entry) => <label className={`address-book-picker-row ${draft.includes(entry.id) ? 'selected' : ''}`} key={entry.id}><input type="checkbox" checked={draft.includes(entry.id)} onChange={() => toggle(entry.id)} /><div><strong>{entry.label}</strong><code>{entry.address}</code></div><span className="address-book-chip"><BookUser size={11} />地址簿</span></label>)}</div>
    <div className="dialog-actions"><button className="secondary" onClick={close}>取消</button><button className="primary" onClick={() => { setSelected(draft); close() }}><Check size={16} />确定选择 {draft.length} 个</button></div>
  </Dialog>
}

function ExternalTargetDialog({ close, onAdd }: { close: () => void; onAdd: (value: string) => void }) {
  const [value, setValue] = useState('')
  return <Dialog title="添加外部收款地址" close={close}><form onSubmit={(event) => { event.preventDefault(); if (value.trim()) onAdd(value) }}>
    <label>外部 Aptos 地址<textarea autoFocus aria-label="外部 Aptos 地址" value={value} onChange={(event) => setValue(event.target.value)} placeholder="0x..." /></label>
    <DialogActions close={close} submit="添加地址" disabled={!value.trim()} />
  </form></Dialog>
}

function ConfirmDialog({ job, wallets, addressBook, initialPreflight, executionEnabled, close, run, onChanged, onStarted }: {
  job: TransferJob; wallets: WalletRecord[]; addressBook: AddressBookEntry[]; initialPreflight: JobPreflight | null; executionEnabled: boolean; close: () => void
  run: DialogProps['run']; onChanged: (result: JobPreflight) => void; onStarted: (job: TransferJob) => void
}) {
  const [preview, setPreview] = useState<JobPreflight>(() => initialPreflight ?? { valid: true, job, checks: [], summary: job.summary })
  const [confirmation, setConfirmation] = useState('')
  const [liveExecutionEnabled, setLiveExecutionEnabled] = useState(executionEnabled)
  const [shuffling, setShuffling] = useState(false)
  const currentJob = preview.job
  const summary = preview.summary ?? { sourceWalletCount: new Set(currentJob.steps.map((step) => step.sourceWalletId)).size, stepCount: currentJob.steps.length, aptBaseUnits: '0', usdtBaseUnits: '0', maxStepCount: currentJob.steps.filter((step) => step.amountMode === 'max').length, estimatedGasBaseUnits: '0', warnings: [] }
  const checks = new Map(preview.checks.map((check) => [check.stepId, check]))
  const shuffle = () => {
    if (!preview.valid || currentJob.steps.length < 2 || shuffling) return
    setShuffling(true)
    void run(async () => {
      const ids = secureShuffle(currentJob.steps.map((step) => step.id))
      const result = await post<JobPreflight>(`/api/v1/jobs/${currentJob.id}/reorder`, { stepIds: ids })
      setPreview(result)
      setConfirmation('')
      onChanged(result)
    }, '已重新生成转账顺序', false).finally(() => setShuffling(false))
  }
  return <Dialog title="转账预览" close={close} wide><div className="transfer-preview-dialog">
    <div className={`confirm-banner ${preview.valid ? 'valid' : ''}`}><ShieldAlert size={20} /><div><strong>{preview.valid ? '预览已生成' : '预览未通过检查'}</strong><span>{preview.valid ? '下面显示本次将要执行的全部转账。发送前仍需输入完整确认短语。' : '所有转账条目仍保留在下方；修正余额、手续费或顺序问题后返回编辑。'}</span></div></div>
    <div className="confirm-metrics"><Metric label="来源钱包" value={summary.sourceWalletCount.toString()} /><Metric label="转账笔数" value={summary.stepCount.toString()} /><Metric label="APT 总额" value={formatAmount(summary.aptBaseUnits, 'APT')} /><Metric label="USDt 总额" value={formatAmount(summary.usdtBaseUnits, 'USDT')} /><Metric label="预计手续费" value={`${formatAmount(summary.estimatedGasBaseUnits, 'APT')} APT`} /></div>
    {summary.warnings.map((warning) => <div className="warning-line" key={warning}>{warning}</div>)}
    <div className="preview-toolbar"><div><strong>执行顺序</strong><span>每一行的来源、目标、金额和等待时间始终绑定</span></div><button className="secondary" disabled={!preview.valid || currentJob.steps.length < 2 || shuffling} onClick={shuffle}><Shuffle className={shuffling ? 'spin' : ''} size={16} />{shuffling ? '正在打乱' : '随机打乱条目'}</button></div>
    <div className="preview-list">{currentJob.steps.map((step) => { const check = checks.get(step.id); return <div className={`preview-step ${check && !check.valid ? 'invalid' : ''}`} key={step.id}><span className="preview-step-position">{step.position + 1}</span><div className="preview-step-source"><small>转出</small><TransferParty wallet={wallets.find((wallet) => wallet.id === step.sourceWalletId)} address={wallets.find((wallet) => wallet.id === step.sourceWalletId)?.address ?? step.sourceWalletId} /></div><div className="preview-step-target"><small>收款</small><TransferParty wallet={step.targetWalletId ? wallets.find((wallet) => wallet.id === step.targetWalletId) : null} address={step.targetAddress} addressBook={addressBook} /></div><strong className="preview-step-amount"><span>{step.amountMode === 'max' ? '全额' : step.frozenAmountDisplay ?? step.amountMin}</span><em>{step.asset === 'USDT' ? 'USDt' : 'APT'}</em></strong><small className="preview-step-wait"><Clock3 size={13} />{step.waitAfterSeconds > 0 ? `下一笔前等待 ${formatSeconds(step.waitAfterSeconds)} 秒` : step.position === currentJob.steps.length - 1 ? '最后一笔，无需等待' : '连续执行，无额外等待'}</small>{check && !check.valid && <div className="preview-step-error"><ShieldAlert size={13} /><span>{check.error ?? '检查未通过，请返回编辑修正'}</span></div>}</div> })}</div>
    {preview.valid && <label>输入完整确认短语<div className="phrase-row"><code className="phrase">{currentJob.confirmationPhrase}</code><button type="button" className="secondary small phrase-copy" title="复制确认短语" aria-label="复制确认短语" onClick={() => void navigator.clipboard.writeText(currentJob.confirmationPhrase ?? '')}><Copy size={14} />复制</button></div><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>}
    {!liveExecutionEnabled && <div className="error-banner"><Lock size={17} />当前页面记录的是仅预览状态。发送前会重新检查本机服务；若仍未开启真实转账，会保留在预览页面。</div>}
    <div className="dialog-actions transfer-preview-actions"><button className="secondary" onClick={close}>返回编辑</button><button className="danger-primary" disabled={!preview.valid || confirmation !== currentJob.confirmationPhrase} onClick={() => void run(async () => {
      const latestStatus = await getStatus()
      setLiveExecutionEnabled(latestStatus.executionEnabled)
      if (!latestStatus.unlocked) throw new Error('保险库已锁定，请重新解锁')
      if (!latestStatus.executionEnabled) throw new Error('本机服务仍是仅预览模式，请使用 --enable-mainnet 启动')
      const started = await post<TransferJob>(`/api/v1/jobs/${currentJob.id}/confirm`, { confirmation })
      onStarted(started)
    }, '任务已开始')}>{preview.valid ? '发送并执行' : '修正后再发送'}</button></div>
  </div></Dialog>
}

function RetryFailedDialog({ job, executionEnabled, close, run, onStarted }: {
  job: TransferJob; executionEnabled: boolean; close: () => void; run: DialogProps['run']; onStarted: (job: TransferJob) => void
}) {
  const [confirmation, setConfirmation] = useState('')
  const [liveExecutionEnabled, setLiveExecutionEnabled] = useState(executionEnabled)
  const failedStep = job.steps.find((step) => step.status === 'failed')
  if (!failedStep) return null
  const confirmedCount = job.steps.filter((step) => step.status === 'confirmed').length
  return <Dialog title="从失败位置继续" close={close}><div className="retry-failed-dialog">
    <div className="warning-box"><ShieldAlert size={17} /><div><strong>原失败交易不会重发</strong><span>系统只会从第 {failedStep.position + 1} 笔重新构建一笔新交易。已确认的 {confirmedCount} 笔保持不变，原失败哈希会留在记录中。</span></div></div>
    <div className="retry-step-summary"><span>重新开始位置</span><strong>第 {failedStep.position + 1} 笔 · {failedStep.asset === 'USDT' ? 'USDt' : 'APT'} · {failedStep.amountMode === 'max' ? '全部余额' : failedStep.frozenAmountDisplay ?? failedStep.amountMin}</strong><small>{presentError(failedStep.error ?? job.error ?? '链上执行失败')}</small></div>
    <label>再次输入完整确认短语<div className="phrase-row"><code className="phrase">{job.confirmationPhrase}</code><button type="button" className="secondary small phrase-copy" title="复制确认短语" aria-label="复制确认短语" onClick={() => void navigator.clipboard.writeText(job.confirmationPhrase ?? '')}><Copy size={14} />复制</button></div><input autoComplete="off" value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>
    {!liveExecutionEnabled && <div className="error-banner"><Lock size={17} />本机服务未开启真实转账，不能从失败位置继续。</div>}
    <div className="dialog-actions"><button className="secondary" onClick={close}>暂不重试</button><button className="danger-primary" disabled={confirmation !== job.confirmationPhrase} onClick={() => void run(async () => {
      const latestStatus = await getStatus()
      setLiveExecutionEnabled(latestStatus.executionEnabled)
      if (!latestStatus.unlocked) throw new Error('保险库已锁定，请重新解锁')
      if (!latestStatus.executionEnabled) throw new Error('本机服务未开启真实转账')
      onStarted(await post<TransferJob>(`/api/v1/jobs/${job.id}/retry-failed`, { confirmation }))
    }, `已从第 ${failedStep.position + 1} 笔重新开始`)}><RotateCcw size={16} />确认并继续执行</button></div>
  </div></Dialog>
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

function WalletSelectionList({ wallets, groups, selected, search, onToggle, onSelectGroup }: {
  wallets: WalletRecord[]; groups: WalletGroup[]; selected: string[]; search: string
  onToggle: (id: string) => void; onSelectGroup: (ids: string[]) => void
}) {
  const [collapsedGroupIds, setCollapsedGroupIds] = useState<Set<string>>(new Set())
  const term = search.trim().toLowerCase()
  const matches = (wallet: WalletRecord, groupLabel = '') => !term || `${accountLabel(wallet)} ${wallet.label} ${wallet.address} ${groupLabel}`.toLowerCase().includes(term)
  const standalone = wallets.filter((wallet) => !wallet.groupId)
  const visibleGroups = groups.map((group) => ({ group, accounts: wallets.filter((wallet) => wallet.groupId === group.id && matches(wallet, group.label)) })).filter(({ accounts }) => accounts.length)
  const visibleStandalone = standalone.filter((wallet) => matches(wallet, '导入的账户'))
  if (!visibleGroups.length && !visibleStandalone.length) return <div className="selection-empty">没有匹配的账户</div>
  return <div className="selection-list">{visibleGroups.map(({ group, accounts }) => { const ids = accounts.map((wallet) => wallet.id); const collapsed = collapsedGroupIds.has(group.id); const selectedCount = ids.filter((id) => selected.includes(id)).length; return <div className="selection-group" key={group.id}>
    <div className="selection-group-title">
      <button type="button" className="selection-group-toggle" aria-expanded={!collapsed} aria-label={`${collapsed ? '展开' : '折叠'} ${group.label}`} onClick={() => setCollapsedGroupIds((current) => { const next = new Set(current); collapsed ? next.delete(group.id) : next.add(group.id); return next })}><ChevronRight size={15} /><strong>{group.label}</strong></button>
      <label className="selection-group-check"><input type="checkbox" aria-label={`选择 ${group.label} 全部账户`} checked={selectedCount === ids.length} onChange={() => onSelectGroup(ids)} /><span>{selectedCount ? `${selectedCount} / ${accounts.length} 已选` : `${accounts.length} 个账户`}</span></label>
    </div>
    {!collapsed && accounts.map((wallet) => <label className={`selection-row ${selected.includes(wallet.id) ? 'selected' : ''}`} key={wallet.id}><input type="checkbox" checked={selected.includes(wallet.id)} onChange={() => onToggle(wallet.id)} /><div><strong>{accountLabel(wallet)}</strong><code>{short(wallet.address)}</code></div><div className="selection-balances"><span>{wallet.balances.find((balance) => balance.asset === 'USDT')?.display ?? '-'} USDt</span><span>{wallet.balances.find((balance) => balance.asset === 'APT')?.display ?? '-'} APT</span></div></label>)}
  </div> })}{visibleStandalone.length > 0 && <div className="selection-group"><div className="selection-section-label">导入的账户</div>{visibleStandalone.map((wallet) => <label className={`selection-row ${selected.includes(wallet.id) ? 'selected' : ''}`} key={wallet.id}><input type="checkbox" checked={selected.includes(wallet.id)} onChange={() => onToggle(wallet.id)} /><div><strong>{wallet.label}</strong><code>{short(wallet.address)}</code></div><div className="selection-balances"><span>{wallet.balances.find((balance) => balance.asset === 'USDT')?.display ?? '-'} USDt</span><span>{wallet.balances.find((balance) => balance.asset === 'APT')?.display ?? '-'} APT</span></div></label>)}</div>}</div>
}

function Dialog({ title, close, children, wide = false }: { title: string; close: () => void; children: React.ReactNode; wide?: boolean }) { return <div className="modal-backdrop" onMouseDown={(event) => event.target === event.currentTarget && close()}><div className={`dialog ${wide ? 'wide-dialog' : ''}`} role="dialog" aria-modal="true" aria-label={title}><div className="dialog-head"><h2>{title}</h2><IconButton title="关闭" icon={<X size={18} />} onClick={close} /></div>{children}</div></div> }

function PreflightProgressDialog({ progress }: { progress: PreflightProgress }) {
  const [expanded, setExpanded] = useState(false)
  const [logs, setLogs] = useState<string[]>([])
  useEffect(() => {
    setLogs((current) => current.at(-1) === progress.message ? current : [...current.slice(-15), progress.message])
  }, [progress.message])
  const percent = progress.total > 0 ? Math.min(100, Math.max(4, Math.round((progress.completed / progress.total) * 100))) : 8
  return <div className="preflight-backdrop" role="status" aria-live="polite" aria-label="正在检查转账计划">
    <section className="preflight-progress-dialog">
      <div className="preflight-progress-icon"><RefreshCw className="spin" size={23} /></div>
      <div className="preflight-progress-heading"><span>正在检查转账计划</span><strong>{preflightPhaseLabel(progress.phase)}</strong></div>
      <p>{progress.message}</p>
      <div className="preflight-progress-track" aria-label={progress.total > 0 ? `检查进度 ${progress.completed} / ${progress.total}` : '正在准备检查'}><span style={{ width: `${percent}%` }} /></div>
      <div className="preflight-progress-meta"><span>{progress.total > 0 ? `${progress.completed} / ${progress.total}` : '正在连接本地服务'}</span><span>{progress.phase === 'simulation' ? '逐笔模拟中' : '安全检查中'}</span></div>
      <button type="button" className="preflight-log-toggle" onClick={() => setExpanded((value) => !value)}>{expanded ? '收起详细日志' : '展开详细日志'}</button>
      {expanded && <ol className="preflight-log">{logs.map((entry, index) => <li key={`${index}-${entry}`}>{entry}</li>)}</ol>}
    </section>
  </div>
}

function preflightPhaseLabel(phase: PreflightProgress['phase']) {
  return ({ prepare: '整理转账清单', asset: '校验 USDt', balances: '读取必要余额', simulation: '模拟链上交易', finalizing: '冻结执行计划', complete: '检查完成', failed: '检查未通过' })[phase]
}
function DialogActions({ close, submit, onSubmit, disabled = false }: { close: () => void; submit: string; onSubmit?: () => void; disabled?: boolean }) { return <div className="dialog-actions"><button type="button" className="secondary" onClick={close}>取消</button><button type={onSubmit ? 'button' : 'submit'} className="primary" onClick={onSubmit} disabled={disabled}>{submit}</button></div> }
interface DialogProps { close: () => void; run: (action: () => Promise<void>, success?: string, showGlobalBusy?: boolean) => Promise<void>; reload: () => Promise<void> }

function PageHeader({ title, subtitle, actions }: { title: string; subtitle: string; actions?: React.ReactNode }) { return <header className="page-header"><div><h1>{title}</h1><p>{subtitle}</p></div>{actions && <div className="header-actions">{actions}</div>}</header> }
function NavButton({ active, icon, label, onClick, count }: { active: boolean; icon: React.ReactNode; label: string; onClick: () => void; count?: number }) { return <button className={`nav-button ${active ? 'active' : ''}`} aria-label={label} onClick={onClick}>{icon}<span>{label}</span>{Boolean(count) && <b>{count}</b>}</button> }
function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) { return <div className={`metric ${tone ?? ''}`}><span>{label}</span><strong>{value}</strong></div> }
function IconButton({ title, icon, onClick, danger, disabled }: { title: string; icon: React.ReactNode; onClick: () => void; danger?: boolean; disabled?: boolean }) { return <button type="button" className={`icon-button ${danger ? 'danger' : ''}`} title={title} aria-label={title} onClick={onClick} disabled={disabled}>{icon}</button> }
function Empty({ icon, text }: { icon: React.ReactNode; text: string }) { return <div className="empty">{icon}<p>{text}</p></div> }
function Address({ value }: { value: string }) { return <button className="address" title={value} onClick={() => void navigator.clipboard.writeText(value)}><code>{short(value)}</code><Copy size={13} /></button> }
function TransferParty({ wallet, address, addressBook = [] }: { wallet?: WalletRecord | null; address: string; addressBook?: AddressBookEntry[] }) {
  const entry = wallet ? null : addressBookEntryFor(address, addressBook)
  const alias = wallet ? walletAlias(wallet) : entry?.label ?? null
  return <div className={`transfer-party ${entry ? 'from-address-book' : ''}`}>{alias && <strong>{entry && <BookUser size={12} />}{alias}</strong>}{entry && <span className="address-book-chip">地址簿</span>}<Address value={address} /></div>
}
function Status({ value }: { value: string }) { return <span className={`status status-${value}`}>{statusLabels[value] ?? value}</span> }
function short(value: string) { return value.length > 18 ? `${value.slice(0, 8)}...${value.slice(-6)}` : value }
function aptosAddressKey(value: string) {
  const hex = value.trim().toLowerCase().replace(/^0x/, '').replace(/^0+/, '')
  return hex || '0'
}
function addressBookEntryFor(address: string, entries: AddressBookEntry[]) {
  const key = aptosAddressKey(address)
  return entries.find((entry) => aptosAddressKey(entry.address) === key) ?? null
}
function hasAtMostOneDecimalNumber(value: number) { return Number.isFinite(value) && Math.abs(value * 10 - Math.round(value * 10)) < 1e-9 }
function formatSeconds(value: number) { return value.toFixed(1) }
function secureShuffle<T>(values: T[]): T[] {
  const copy = [...values]
  const random = new Uint32Array(1)
  for (let index = copy.length - 1; index > 0; index -= 1) {
    crypto.getRandomValues(random)
    const selected = random[0] % (index + 1)
    ;[copy[index], copy[selected]] = [copy[selected], copy[index]]
  }
  return copy
}
function walletOptionLabel(wallet: WalletRecord, groups: WalletGroup[]) { const group = groups.find((item) => item.id === wallet.groupId); return group ? `${group.label} · ${accountLabel(wallet)}` : wallet.label }
function walletLabel(id: string, wallets: WalletRecord[]) { const wallet = wallets.find((item) => item.id === id); return wallet ? accountLabel(wallet) : id }
function walletAlias(wallet: WalletRecord): string | null {
  if (wallet.accountIndex !== null && wallet.label === `账户 #${wallet.accountIndex}`) return null
  return wallet.label.trim() || null
}
function accountLabel(wallet: WalletRecord) {
  return wallet.accountIndex !== null && wallet.label === `账户 #${wallet.accountIndex}` ? `账户 ${wallet.accountIndex + 1}` : wallet.label
}
function historyAmount(item: AccountTransferLog) {
  const symbol = item.asset === 'USDT' ? 'USDt' : 'APT'
  if (item.frozenAmountDisplay) return `${item.frozenAmountDisplay} ${symbol}`
  if (item.amountMode === 'max') return `全部余额 ${symbol}`
  if (item.amountMode === 'random') return `${item.amountMin ?? '-'} - ${item.amountMax ?? '-'} ${symbol}`
  return `${item.amountMin ?? '-'} ${symbol}`
}
function formatGasFee(value: string | null | undefined) {
  return value === null || value === undefined ? '-' : `${formatAmountWithMaxDecimals(value, 'APT', 4)} APT`
}
