import { useEffect, useMemo, useRef, useState, type FormEvent } from 'react'
import {
  Archive, ArrowDownLeft, ArrowRight, ArrowUpRight, Check, ChevronRight, CircleDollarSign, Clock3, Copy, Download, Ellipsis,
  Eye, FileClock, KeyRound, Layers3, Lock, Pencil, Plus, QrCode, RefreshCw, RotateCcw, Search, Send,
  ShieldAlert, Shuffle, Upload, WalletCards, X,
} from 'lucide-react'
import { QRCodeSVG } from 'qrcode.react'
import type { AccountTransferLog, AccountTransferLogPage, AmountMode, AssetId, JobDraftInput, JobPreflight, MnemonicRestorePreview, TransferJob, TransferStepDraft, VaultStatus, WalletGroup, WalletRecord } from '../shared/types'
import { formatAmount, hasAtMostDecimals } from '../shared/amounts'
import { download, getStatus, loadWorkspace, post, request, saveAndPreviewJob, subscribe } from './api'
import { createMnemonic, parseAccountIndexes, pickConfirmationIndexes } from './mnemonic'
import { requestEncryptedSecret } from './secret-transport'
import { pairTransferEndpoints } from './transfer-pairing'

type View = 'wallets' | 'transfer' | 'jobs'
type Modal = 'create' | 'restore' | 'private' | 'confirm' | 'secret' | 'password' | 'accounts' | 'archiveGroup' | 'secretAuth' | 'archived' | 'receive' | 'accountDetails' | 'accountAlias' | 'accountHistory' | null
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
  const [previewPreflight, setPreviewPreflight] = useState<JobPreflight | null>(null)
  const [secret, setSecret] = useState('')
  const [secretTitle, setSecretTitle] = useState('秘密')
  const [selectedGroup, setSelectedGroup] = useState<WalletGroup | null>(null)
  const [selectedWallet, setSelectedWallet] = useState<WalletRecord | null>(null)
  const [secretTarget, setSecretTarget] = useState<SecretTarget | null>(null)
  const [transferSourceWalletId, setTransferSourceWalletId] = useState<string | null>(null)
  const previousJobStatuses = useRef(new Map<string, string>())

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
  }
  const applyWalletUpdate = (updated: WalletRecord) => {
    setWallets((current) => current.map((wallet) => wallet.id === updated.id ? updated : wallet))
    setGroups((current) => current.map((group) => group.id === updated.groupId
      ? { ...group, accounts: group.accounts.map((wallet) => wallet.id === updated.id ? updated : wallet) }
      : group))
  }
  const applyGroupUpdate = (updated: WalletGroup) => {
    setGroups((current) => current.map((group) => group.id === updated.id ? updated : group))
    const accounts = new Map(updated.accounts.map((wallet) => [wallet.id, wallet]))
    setWallets((current) => current.map((wallet) => accounts.get(wallet.id) ?? wallet))
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
            <div><strong>Aptos 主网</strong><span>{status.executionEnabled ? '真实转账已开启' : '安全模式 · 真实转账已关闭'}</span></div>
          </div>
          <button className="nav-button" title="锁定钱包" aria-label="锁定钱包" onClick={() => void run(async () => {
            await post('/api/v1/vault/lock')
            setWallets([]); setGroups([]); setJobs([]); await refreshStatus()
          })}><Lock size={18} />锁定钱包</button>
          <button className="nav-button" title="修改主密码" aria-label="修改主密码" onClick={() => setModal('password')}><KeyRound size={18} />修改主密码</button>
        </div>
      </aside>
      <main>
        <div hidden={view !== 'wallets'}><WalletView wallets={wallets} groups={groups} setModal={setModal} run={run} reload={reload} onWalletUpdated={applyWalletUpdate} onGroupUpdated={applyGroupUpdate} onToast={setToast}
          onAccounts={(group) => { setSelectedGroup(group); setModal('accounts') }}
          onArchive={(group) => { setSelectedGroup(group); setModal('archiveGroup') }}
          onRevealMnemonic={(group) => { setSecretTarget({ kind: 'mnemonic', group }); setModal('secretAuth') }}
          onReceive={(wallet) => { setSelectedWallet(wallet); setModal('receive') }}
          onHistory={(wallet) => { setSelectedWallet(wallet); setModal('accountHistory') }}
          onAlias={(wallet) => { setSelectedWallet(wallet); setModal('accountAlias') }}
          onDetails={(wallet) => { setSelectedWallet(wallet); setModal('accountDetails') }}
          onTransfer={(wallet) => { setTransferSourceWalletId(wallet.id); setView('transfer') }} /></div>
        {view === 'transfer' && <TransferView key={transferSourceWalletId ?? 'new'} wallets={wallets} groups={groups} busy={busy} run={run} initialSourceWalletId={transferSourceWalletId} onPreview={(result) => { setPreviewPreflight(result); setPreviewJob(result.job); setModal('confirm') }} />}
        {view === 'jobs' && <JobsView jobs={jobs} wallets={wallets} run={run} setPreviewJob={(job) => { setPreviewPreflight(null); setPreviewJob(job) }} setModal={setModal} />}
      </main>
      {modal === 'create' && <CreateWalletDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'restore' && <RestoreWalletDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'private' && <ImportPrivateKeyDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'confirm' && previewJob && <ConfirmDialog job={previewJob} wallets={wallets} initialPreflight={previewPreflight} executionEnabled={status.executionEnabled} close={() => { setModal(null); setPreviewPreflight(null) }} run={run} onChanged={(result) => { setPreviewPreflight(result); setPreviewJob(result.job) }} onStarted={() => { setModal(null); setPreviewPreflight(null); setView('jobs') }} />}
      {modal === 'secret' && <SecretDialog title={secretTitle} secret={secret} close={() => { setSecret(''); setModal(null) }} />}
      {modal === 'password' && <PasswordDialog close={() => setModal(null)} run={run} />}
      {modal === 'accounts' && selectedGroup && <AccountDialog group={selectedGroup} close={() => { setSelectedGroup(null); setModal(null) }} run={run} reload={reload} />}
      {modal === 'archiveGroup' && selectedGroup && <ArchiveGroupDialog group={selectedGroup} close={() => { setSelectedGroup(null); setModal(null) }} run={run} reload={reload} />}
      {modal === 'secretAuth' && secretTarget && <SecretAuthDialog target={secretTarget} close={() => { setSecretTarget(null); setModal(null) }} run={run} onSecret={(title, value) => { setSecretTitle(title); setSecret(value); setSecretTarget(null); setModal('secret') }} />}
      {modal === 'archived' && <ArchivedDialog close={() => setModal(null)} run={run} reload={reload} />}
      {modal === 'receive' && selectedWallet && <ReceiveDialog wallet={selectedWallet} close={() => { setSelectedWallet(null); setModal(null) }} />}
      {modal === 'accountHistory' && selectedWallet && <AccountHistoryDialog wallet={selectedWallet} wallets={wallets} close={() => { setSelectedWallet(null); setModal(null) }} />}
      {modal === 'accountAlias' && selectedWallet && <AccountAliasDialog wallet={selectedWallet} close={() => { setSelectedWallet(null); setModal(null) }} run={run} onUpdated={applyWalletUpdate} />}
      {modal === 'accountDetails' && selectedWallet && <AccountDetailsDialog wallet={selectedWallet} close={() => { setSelectedWallet(null); setModal(null) }} run={run} reload={reload}
        onReceive={() => setModal('receive')}
        onHistory={() => setModal('accountHistory')}
        onAlias={() => setModal('accountAlias')}
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

function WalletView({ wallets, groups, setModal, run, reload, onWalletUpdated, onGroupUpdated, onToast, onAccounts, onArchive, onRevealMnemonic, onReceive, onHistory, onAlias, onTransfer, onDetails }: {
  wallets: WalletRecord[]; groups: WalletGroup[]; setModal: (value: Modal) => void
  run: (action: () => Promise<void>, success?: string) => Promise<void>; reload: () => Promise<void>
  onWalletUpdated: (wallet: WalletRecord) => void; onGroupUpdated: (group: WalletGroup) => void; onToast: (message: string) => void
  onAccounts: (group: WalletGroup) => void; onArchive: (group: WalletGroup) => void
  onRevealMnemonic: (group: WalletGroup) => void; onReceive: (wallet: WalletRecord) => void; onHistory: (wallet: WalletRecord) => void
  onAlias: (wallet: WalletRecord) => void; onTransfer: (wallet: WalletRecord) => void; onDetails: (wallet: WalletRecord) => void
}) {
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set(groups.map((group) => group.id)))
  const [refreshingWalletIds, setRefreshingWalletIds] = useState<Set<string>>(new Set())
  const [changedWalletIds, setChangedWalletIds] = useState<Set<string>>(new Set())
  const [refreshingAll, setRefreshingAll] = useState(false)
  const [refreshingGroupIds, setRefreshingGroupIds] = useState<Set<string>>(new Set())
  const latestWalletsRef = useRef(wallets)
  const refreshingWalletIdsRef = useRef(new Set<string>())
  const refreshingAllRef = useRef(false)
  const backgroundRefreshInFlightRef = useRef(false)
  latestWalletsRef.current = wallets
  const standalone = wallets.filter((wallet) => !wallet.groupId)
  const totalApt = wallets.reduce((sum, wallet) => sum + BigInt(wallet.balances.find((balance) => balance.asset === 'APT')?.baseUnits ?? '0'), 0n)
  const totalUsdt = wallets.reduce((sum, wallet) => sum + BigInt(wallet.balances.find((balance) => balance.asset === 'USDT')?.baseUnits ?? '0'), 0n)
  const markChangedWallets = (before: WalletRecord[], after: WalletRecord[]) => {
    const previous = new Map(before.map((wallet) => [wallet.id, wallet.balances.map((balance) => `${balance.asset}:${balance.baseUnits}`).join('|')]))
    const changed = after.filter((wallet) => previous.get(wallet.id) !== wallet.balances.map((balance) => `${balance.asset}:${balance.baseUnits}`).join('|')).map((wallet) => wallet.id)
    if (!changed.length) return
    setChangedWalletIds((current) => new Set([...current, ...changed]))
    window.setTimeout(() => setChangedWalletIds((current) => {
      const next = new Set(current); changed.forEach((id) => next.delete(id)); return next
    }), 1_800)
  }
  const refreshWalletBatch = async (candidates: WalletRecord[]) => {
    const available = candidates.filter((wallet) => !refreshingWalletIdsRef.current.has(wallet.id))
    if (!available.length) return { refreshed: 0, failed: 0 }
    available.forEach((wallet) => refreshingWalletIdsRef.current.add(wallet.id))
    setRefreshingWalletIds((current) => new Set([...current, ...available.map((wallet) => wallet.id)]))
    let nextIndex = 0
    let failed = 0
    const worker = async () => {
      while (nextIndex < available.length) {
        const wallet = available[nextIndex++]
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
    await Promise.all(Array.from({ length: Math.min(10, available.length) }, worker))
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
    if (!wallets.length) return
    const timer = window.setInterval(() => { void refreshAll('background') }, 30_000)
    return () => window.clearInterval(timer)
  }, [wallets.length])
  const refreshGroup = async (group: WalletGroup) => {
    if (refreshingGroupIds.has(group.id)) return
    const before = wallets.filter((wallet) => wallet.groupId === group.id)
    const ids = before.map((wallet) => wallet.id)
    if (ids.some((id) => refreshingWalletIdsRef.current.has(id))) {
      onToast(`${group.label} 正在刷新中，请稍后再试`)
      return
    }
    ids.forEach((id) => refreshingWalletIdsRef.current.add(id))
    setRefreshingGroupIds((current) => new Set(current).add(group.id))
    setRefreshingWalletIds((current) => new Set([...current, ...ids]))
    try {
      const updated = await post<WalletGroup>(`/api/v1/wallets/groups/${group.id}/refresh`)
      onGroupUpdated(updated)
      markChangedWallets(before, updated.accounts)
      const failed = updated.accounts.filter((wallet) => wallet.balanceError).length
      onToast(failed ? `${group.label} 刷新完成，${failed} 个账户失败` : `${group.label} 的余额已刷新`)
    } catch (error) {
      onToast(error instanceof Error ? error.message : '钱包余额刷新失败')
    } finally {
      ids.forEach((id) => refreshingWalletIdsRef.current.delete(id))
      setRefreshingGroupIds((current) => { const next = new Set(current); next.delete(group.id); return next })
      setRefreshingWalletIds((current) => { const next = new Set(current); ids.forEach((id) => next.delete(id)); return next })
    }
  }
  return <>
      <PageHeader title="钱包" subtitle={`${wallets.length} 个账户 · Aptos 主网 · 每 30 秒自动刷新`} actions={<>
      <button className="secondary" title="立即刷新全部余额；后台也会每 30 秒自动刷新" onClick={() => void refreshAll()} disabled={refreshingAll || !wallets.length}><RefreshCw className={refreshingAll ? 'spin' : ''} size={16} />{refreshingAll ? '正在刷新' : '刷新全部余额'}</button>
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
          {isOpen && <WalletList wallets={wallets.filter((wallet) => wallet.groupId === group.id)} refreshingWalletIds={refreshingWalletIds} changedWalletIds={changedWalletIds} onReceive={onReceive} onHistory={onHistory} onAlias={onAlias} onTransfer={onTransfer} onDetails={onDetails} />}
        </div>
      })}
    </section>
    <section className="table-section standalone-section">
      <div className="section-head"><h2>导入的账户</h2><span>使用私钥单独导入</span></div>
      {wallets.length === 0 ? <Empty icon={<WalletCards size={28} />} text="还没有钱包，先创建或恢复一个钱包。" /> :
        standalone.length === 0 ? <Empty icon={<WalletCards size={28} />} text="没有导入的账户。" /> : <WalletList wallets={standalone} refreshingWalletIds={refreshingWalletIds} changedWalletIds={changedWalletIds} onReceive={onReceive} onHistory={onHistory} onAlias={onAlias} onTransfer={onTransfer} onDetails={onDetails} />}
    </section>
  </>
}

function WalletList({ wallets, refreshingWalletIds, changedWalletIds, onReceive, onHistory, onAlias, onTransfer, onDetails }: { wallets: WalletRecord[]; refreshingWalletIds: Set<string>; changedWalletIds: Set<string>; onReceive: (wallet: WalletRecord) => void; onHistory: (wallet: WalletRecord) => void; onAlias: (wallet: WalletRecord) => void; onTransfer: (wallet: WalletRecord) => void; onDetails: (wallet: WalletRecord) => void }) {
  return <div className="account-list">{wallets.map((wallet) => { const refreshing = refreshingWalletIds.has(wallet.id); const changed = changedWalletIds.has(wallet.id); return <article className={`account-row ${refreshing ? 'refreshing' : ''} ${changed ? 'balance-changed' : ''}`} key={wallet.id} aria-busy={refreshing}>
    <div className="account-identity"><div className="account-name-line"><strong className="account-name-badge">{accountLabel(wallet)}</strong><IconButton title={`设置 ${accountLabel(wallet)} 的别名`} icon={<Pencil size={15} />} onClick={() => onAlias(wallet)} /></div><Address value={wallet.address} />{wallet.balanceError && <span className="row-error">{wallet.balanceError}</span>}</div>
    <div className="account-state"><Status value={wallet.accountStatus} /></div>
    <AccountBalance label="APT" value={wallet.balances.find((item) => item.asset === 'APT')?.display ?? '-'} loading={refreshing} />
    <AccountBalance label="USDt" value={wallet.balances.find((item) => item.asset === 'USDT')?.display ?? '-'} loading={refreshing} />
    <div className="account-actions">
      <button className="secondary small" onClick={() => onReceive(wallet)}><QrCode size={14} />收款</button>
      <button className="secondary small" onClick={() => onHistory(wallet)}><FileClock size={14} />日志</button>
      <button className="primary small" onClick={() => onTransfer(wallet)}><Send size={14} />转账</button>
      <IconButton title={`${accountLabel(wallet)} 详情`} icon={<Ellipsis size={17} />} onClick={() => onDetails(wallet)} />
    </div>
  </article> })}</div>
}

function AccountBalance({ label, value, loading = false }: { label: string; value: string; loading?: boolean }) {
  return <div className="account-balance" aria-live="polite"><span>{label}</span><strong>{loading ? <RefreshCw className="spin" size={16} aria-label={`${label} 余额刷新中`} /> : value}</strong></div>
}

function TransferView({ wallets, groups, busy, run, onPreview, initialSourceWalletId }: {
  wallets: WalletRecord[]; groups: WalletGroup[]; busy: boolean
  run: (action: () => Promise<void>, success?: string) => Promise<void>; onPreview: (result: JobPreflight) => void
  initialSourceWalletId: string | null
}) {
  const [name, setName] = useState(`转账计划 ${new Date().toLocaleDateString()}`)
  const [sourceWalletIds, setSourceWalletIds] = useState<string[]>(() => initialSourceWalletId ? [initialSourceWalletId] : [])
  const [internalTargetWalletIds, setInternalTargetWalletIds] = useState<string[]>([])
  const [externalTargets, setExternalTargets] = useState('')
  const [sourceSearch, setSourceSearch] = useState('')
  const [targetPickerOpen, setTargetPickerOpen] = useState(false)
  const [asset, setAsset] = useState<AssetId>('USDT')
  const [amountMode, setAmountMode] = useState<AmountMode>('random')
  const [amountMin, setAmountMin] = useState('1')
  const [amountMax, setAmountMax] = useState('5')
  const [steps, setSteps] = useState<TransferStepDraft[]>([])
  const [gasPayerWalletId, setGasPayer] = useState<string | null>(null)
  const [intervalMinSeconds, setIntervalMin] = useState(5)
  const [intervalMaxSeconds, setIntervalMax] = useState(30)
  const [preflight, setPreflight] = useState<JobPreflight | null>(null)
  const initialSource = wallets.find((wallet) => wallet.id === initialSourceWalletId)
  const invalidate = () => { setPreflight(null); setSteps([]) }
  const toggle = (values: string[], id: string, setter: (next: string[]) => void) => {
    invalidate()
    setter(values.includes(id) ? values.filter((value) => value !== id) : [...values, id])
  }
  const orderedWallets = useMemo(() => [
    ...groups.flatMap((group) => wallets.filter((wallet) => wallet.groupId === group.id)),
    ...wallets.filter((wallet) => !wallet.groupId),
  ], [groups, wallets])
  const orderedSourceWalletIds = useMemo(() => orderedWallets.filter((wallet) => sourceWalletIds.includes(wallet.id)).map((wallet) => wallet.id), [orderedWallets, sourceWalletIds])
  const targets = useMemo(() => {
    const selected = orderedWallets.filter((wallet) => internalTargetWalletIds.includes(wallet.id))
      .map((wallet) => ({ walletId: wallet.id, address: wallet.address, label: walletOptionLabel(wallet, groups) }))
    const typed = externalTargets.split(/[\n,]/).map((value) => value.trim()).filter(Boolean).map((address) => {
      const wallet = wallets.find((item) => item.address.toLowerCase() === address.toLowerCase())
      return { walletId: wallet?.id ?? null, address: wallet?.address ?? address, label: wallet ? walletOptionLabel(wallet, groups) : '外部地址' }
    })
    const unique = new Map<string, { walletId: string | null; address: string; label: string }>()
    for (const target of [...selected, ...typed]) unique.set(target.address.toLowerCase(), target)
    return [...unique.values()]
  }, [externalTargets, groups, internalTargetWalletIds, orderedWallets, wallets])
  const pairing = useMemo(() => pairTransferEndpoints(orderedSourceWalletIds, targets), [orderedSourceWalletIds, targets])
  const pairs = pairing.pairs
  const maxConflict = amountMode === 'max' && pairing.mode === 'one_to_many' && pairs.length > 1
  const invalidInterval = intervalMinSeconds < 0 || intervalMaxSeconds < intervalMinSeconds
  const randomPrecisionIssue = amountMode === 'random' && [amountMin, amountMax].some((value) => value.trim() && !hasAtMostDecimals(value, 2))
  const canPreview = pairs.length > 0 && pairs.length <= 1000 && !pairing.issue && !maxConflict && !invalidInterval && !randomPrecisionIssue && (amountMode === 'max' || Boolean(amountMin.trim())) && (amountMode !== 'random' || Boolean(amountMax.trim()))
  const preview = () => void run(async () => {
    const generated = pairs.map(({ sourceWalletId, target }) => ({
      id: crypto.randomUUID(), sourceWalletId, targetAddress: target.address, targetWalletId: target.walletId,
      asset, amountMode, amountMin: amountMode === 'max' ? null : amountMin,
      amountMax: amountMode === 'random' ? amountMax : null,
    }))
    setSteps(generated)
    const draft: JobDraftInput = { name, steps: generated, gasPayerWalletId, intervalMinSeconds, intervalMaxSeconds, shuffle: false }
    const result = await saveAndPreviewJob(draft)
    setPreflight(result)
    onPreview(result)
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
        <div className="interval-field setting-card"><span className="setting-label">执行节奏</span><div className="interval-inputs"><label><span>最短间隔（秒）</span><input type="number" min="0" max="604800" value={intervalMinSeconds} onChange={(event) => { invalidate(); setIntervalMin(Number(event.target.value)) }} /></label><span className="interval-separator">至</span><label><span>最长间隔（秒）</span><input type="number" min={intervalMinSeconds} max="604800" value={intervalMaxSeconds} onChange={(event) => { invalidate(); setIntervalMax(Number(event.target.value)) }} /></label></div></div>
      </div>
    </section>
    {preflight && <div className={`preflight-summary ${preflight.valid ? 'valid' : 'invalid'}`}>
      {preflight.valid ? <Check size={17} /> : <ShieldAlert size={17} />}
      <div><strong>{preflight.valid ? '检查通过' : `${preflight.checks.filter((check) => !check.valid).length} 笔转账需要处理`}</strong><span>预计手续费 {formatAmount(preflight.summary?.estimatedGasBaseUnits ?? '0', 'APT')} APT</span></div>
    </div>}
    <section className="transfer-compose-grid">
      <div className="selection-panel source-panel">
        <div className="selection-head"><div><span className="step-number">1</span><div><h2>转出账户</h2><p>已选 {sourceWalletIds.length} 个</p></div></div>{sourceWalletIds.length > 0 && <button className="text-button" onClick={() => { invalidate(); setSourceWalletIds([]) }}>清空</button>}</div>
        <label className="selection-search"><Search size={15} /><input aria-label="搜索转出账户" value={sourceSearch} onChange={(event) => setSourceSearch(event.target.value)} placeholder="搜索账户或地址" /></label>
        <WalletSelectionList wallets={wallets} groups={groups} selected={sourceWalletIds} search={sourceSearch} onToggle={(id) => toggle(sourceWalletIds, id, setSourceWalletIds)} onSelectGroup={(ids) => { invalidate(); setSourceWalletIds(ids.every((id) => sourceWalletIds.includes(id)) ? sourceWalletIds.filter((id) => !ids.includes(id)) : [...new Set([...sourceWalletIds, ...ids])]) }} />
      </div>
      <div className="compose-arrow" aria-hidden="true"><ArrowRight size={20} /><span>{pairs.length} 笔</span></div>
      <div className="selection-panel target-panel">
        <div className="selection-head"><div><span className="step-number">2</span><div><h2>收款地址</h2><p>已选 {targets.length} 个</p></div></div><button className="secondary small" onClick={() => setTargetPickerOpen(true)}><WalletCards size={14} />从我的账户选择</button></div>
        <label className="target-entry">外部 Aptos 地址<textarea aria-label="外部 Aptos 地址" value={externalTargets} onChange={(event) => { invalidate(); setExternalTargets(event.target.value) }} placeholder="每行填写一个 0x 地址，也可用逗号分隔" /></label>
        <div className="selected-targets" aria-live="polite">
          {targets.length === 0 ? <div className="target-empty">尚未添加收款地址</div> : targets.map((target) => <div className="selected-target" key={target.address}><div><strong>{target.label}</strong><code>{short(target.address)}</code></div><IconButton title={`移除 ${target.label}`} icon={<X size={15} />} onClick={() => {
            invalidate()
            if (target.walletId) setInternalTargetWalletIds((current) => current.filter((id) => id !== target.walletId))
            setExternalTargets((current) => current.split(/[\n,]/).map((value) => value.trim()).filter((value) => value && value.toLowerCase() !== target.address.toLowerCase()).join('\n'))
          }} /></div>)}
        </div>
      </div>
    </section>
    {pairing.mode === 'one_to_one' && pairs.length > 1 && !pairing.issue && <section className="pairing-preview" aria-label="一一配对预览">
      <div className="pairing-preview-head"><strong>按顺序一一配对</strong><span>第 N 个转出账户对应第 N 个收款地址</span></div>
      {pairs.slice(0, 10).map(({ sourceWalletId, target }, position) => <div className="pairing-preview-row" key={`${sourceWalletId}:${target.address}`}><span>{position + 1}</span><TransferParty wallet={wallets.find((wallet) => wallet.id === sourceWalletId)} address={wallets.find((wallet) => wallet.id === sourceWalletId)?.address ?? sourceWalletId} /><ArrowRight size={14} /><TransferParty wallet={target.walletId ? wallets.find((wallet) => wallet.id === target.walletId) : null} address={target.address} /></div>)}
      {pairs.length > 10 && <div className="list-overflow-note">另有 {pairs.length - 10} 组配对，将继续按当前顺序处理。</div>}
    </section>}
    <section className="transfer-summary-band">
      <div><strong>{sourceWalletIds.length} 个转出账户 → {targets.length} 个收款地址</strong><span>{pairing.mode === 'one_to_many' ? `一对多，共生成 ${pairs.length} 笔转账` : pairing.mode === 'many_to_one' ? `多对一，共生成 ${pairs.length} 笔转账` : pairing.mode === 'one_to_one' ? `按顺序一一对应，共生成 ${pairs.length} 笔转账` : '请选择可配对的账户与地址'}</span></div>
      <div className="summary-amount"><span>统一金额</span><strong>{amountMode === 'max' ? '全部余额' : amountMode === 'random' ? `${amountMin || '-'} - ${amountMax || '-'} ${asset === 'USDT' ? 'USDt' : 'APT'}` : `${amountMin || '-'} ${asset === 'USDT' ? 'USDt' : 'APT'}`}</strong></div>
      <button className="primary" onClick={preview} disabled={busy || !canPreview}><Eye size={16} />进入转账预览</button>
    </section>
    {(pairing.issue || maxConflict || pairs.length > 1000 || invalidInterval || randomPrecisionIssue) && <div className="error-banner"><ShieldAlert size={17} />{pairing.issue?.kind === 'count_mismatch' ? `多对多转账必须一一对应：当前有 ${sourceWalletIds.length} 个转出账户和 ${targets.length} 个收款地址，请调整为相同数量。` : pairing.issue?.kind === 'self_transfer' ? `第 ${pairing.issue.position + 1} 组的转出账户和收款账户相同，请调整对应顺序或收款地址。` : maxConflict ? '全部余额模式下，一个转出账户只能对应一个收款地址。' : pairs.length > 1000 ? '转账超过 1000 笔，请减少账户或地址。' : invalidInterval ? '最长间隔不能小于最短间隔。' : '随机金额最多保留 2 位小数，例如 1.25。'}</div>}
    {preflight && <TransferCheckList steps={steps} checks={preflight.checks} wallets={wallets} />}
    {targetPickerOpen && <AddressBookDialog wallets={wallets} groups={groups} selected={internalTargetWalletIds} setSelected={(ids) => { invalidate(); setInternalTargetWalletIds(ids) }} close={() => setTargetPickerOpen(false)} />}
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
        <td>{step.position + 1}</td><td><TransferParty wallet={wallets.find((wallet) => wallet.id === step.sourceWalletId)} address={wallets.find((wallet) => wallet.id === step.sourceWalletId)?.address ?? step.sourceWalletId} /></td><td><TransferParty wallet={step.targetWalletId ? wallets.find((wallet) => wallet.id === step.targetWalletId) : null} address={step.targetAddress} /></td><td>{step.asset === 'USDT' ? 'USDt' : 'APT'}</td>
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

function AccountHistoryDialog({ wallet, wallets, close }: { wallet: WalletRecord; wallets: WalletRecord[]; close: () => void }) {
  const pageSize = 50
  const [direction, setDirection] = useState<'all' | 'in' | 'out'>('all')
  const [page, setPage] = useState(0)
  const [result, setResult] = useState<AccountTransferLogPage | null>(null)
  const [loading, setLoading] = useState(true)
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
  useEffect(() => { void load() }, [wallet.id, direction, page])
  const counts = result?.counts ?? { all: 0, in: 0, out: 0 }
  return <Dialog title={`${accountLabel(wallet)} · 转账日志`} close={close} wide>
    <div className="history-toolbar">
      <div className="segmented history-filter" role="group" aria-label="日志方向">
        {([['all', '全部', counts.all], ['out', '转出', counts.out], ['in', '转入', counts.in]] as const).map(([value, label, count]) => <button className={direction === value ? 'active' : ''} aria-pressed={direction === value} key={value} onClick={() => { setDirection(value); setPage(0) }}>{label}<span>{count}</span></button>)}
      </div>
      <div className="history-tools"><IconButton title="刷新日志" icon={<RefreshCw className={loading ? 'spin' : ''} size={16} />} onClick={() => void load()} /><a className="secondary small" href={`https://explorer.aptoslabs.com/account/${wallet.address}?network=mainnet`} target="_blank" rel="noreferrer">链上记录<ArrowUpRight size={14} /></a></div>
    </div>
    {error && <div className="error-banner"><ShieldAlert size={17} />{error}</div>}
    {loading && !result ? <div className="history-loading"><RefreshCw className="spin" size={20} />正在读取日志...</div> : result?.items.length === 0 ? <Empty icon={<FileClock size={28} />} text="暂无转账记录。" /> : <div className="account-history-list">
      {result?.items.map((item) => <AccountHistoryRow key={item.id} item={item} wallets={wallets} />)}
    </div>}
    {result && result.total > pageSize && <div className="history-pagination"><span>第 {page + 1} 页 · 共 {result.total} 条</span><div><button className="secondary small" disabled={page === 0 || loading} onClick={() => setPage((current) => current - 1)}>上一页</button><button className="secondary small" disabled={(page + 1) * pageSize >= result.total || loading} onClick={() => setPage((current) => current + 1)}>下一页</button></div></div>}
  </Dialog>
}

function AccountHistoryRow({ item, wallets }: { item: AccountTransferLog; wallets: WalletRecord[] }) {
  const counterparty = item.counterpartyWalletId ? wallets.find((wallet) => wallet.id === item.counterpartyWalletId) : null
  return <article className={`account-history-row direction-${item.direction}`}>
    <div className={`history-direction ${item.direction}`}>{item.direction === 'out' ? <ArrowUpRight size={17} /> : <ArrowDownLeft size={17} />}<span>{item.direction === 'out' ? '转出' : '转入'}</span></div>
    <div className="history-main"><div><strong>{historyAmount(item)}</strong><Status value={item.status} /></div><span>{item.jobName} · 第 {item.position + 1} 笔</span></div>
    <div className="history-counterparty"><span>{item.direction === 'out' ? '收款方' : '转出方'}</span><strong>{counterparty ? accountLabel(counterparty) : '外部地址'}</strong><Address value={item.counterpartyAddress} /></div>
    <div className="history-time"><span>{new Date(item.updatedAt).toLocaleString()}</span>{item.txHash ? <a href={`https://explorer.aptoslabs.com/txn/${item.txHash}?network=mainnet`} target="_blank" rel="noreferrer">{short(item.txHash)}<ArrowUpRight size={12} /></a> : <small>暂无交易哈希</small>}</div>
    {item.error && <div className="history-error"><ShieldAlert size={13} />{item.error}</div>}
  </article>
}

function DerivedPreview({ result }: { result: MnemonicRestorePreview }) {
  return <div className="derived-preview">{result.accounts.map((account) => <div key={account.accountIndex}><strong>账户 {account.accountIndex + 1}</strong><Address value={account.address} /></div>)}</div>
}

function TransferCheckList({ steps, checks, wallets }: { steps: TransferStepDraft[]; checks: JobPreflight['checks']; wallets: WalletRecord[] }) {
  const rows = steps.map((step, index) => ({ step, index, check: checks.find((item) => item.stepId === step.id) }))
  const visible = [...rows.filter((row) => row.check && !row.check.valid), ...rows.filter((row) => !row.check || row.check.valid)].slice(0, 100)
  return <section className="transfer-check-list">
    <div className="section-head"><div><h2>检查结果</h2><span>{steps.length} 笔转账，手续费按链上模拟估算</span></div></div>
    {visible.map(({ step, index, check }) => <div className={`step-row transfer-check-row ${check && !check.valid ? 'step-invalid' : ''}`} key={step.id}>
      <span className="check-position">{index + 1}</span>
      <div><span>转出</span><TransferParty wallet={wallets.find((wallet) => wallet.id === step.sourceWalletId)} address={wallets.find((wallet) => wallet.id === step.sourceWalletId)?.address ?? step.sourceWalletId} /></div>
      <div><span>收款</span><TransferParty wallet={step.targetWalletId ? wallets.find((wallet) => wallet.id === step.targetWalletId) : null} address={step.targetAddress} /></div>
      <div><span>金额</span><strong>{step.amountMode === 'max' ? '全部余额' : step.amountMode === 'random' ? `${step.amountMin} - ${step.amountMax}` : step.amountMin} {step.asset === 'USDT' ? 'USDt' : 'APT'}</strong></div>
      <div className={`step-check ${check?.valid ? 'valid' : 'invalid'}`}>{check?.valid ? <Check size={14} /> : <ShieldAlert size={14} />}<span>{check?.error ?? '余额与手续费检查通过'}</span><strong>{check && BigInt(check.estimatedGasBaseUnits) > 0n ? `约 ${formatAmount(check.estimatedGasBaseUnits, 'APT')} APT` : '待估算'}</strong></div>
    </div>)}
    {steps.length > visible.length && <div className="list-overflow-note">仅展示前 100 笔，完整清单将在确认页冻结。</div>}
  </section>
}

function AddressBookDialog({ wallets, groups, selected, setSelected, close }: { wallets: WalletRecord[]; groups: WalletGroup[]; selected: string[]; setSelected: (ids: string[]) => void; close: () => void }) {
  const [draft, setDraft] = useState(selected)
  const [search, setSearch] = useState('')
  const toggle = (id: string) => setDraft((current) => current.includes(id) ? current.filter((value) => value !== id) : [...current, id])
  const selectGroup = (ids: string[]) => setDraft((current) => ids.every((id) => current.includes(id)) ? current.filter((id) => !ids.includes(id)) : [...new Set([...current, ...ids])])
  return <Dialog title="从我的账户选择" close={close} wide>
    <label className="selection-search dialog-search"><Search size={15} /><input aria-label="搜索收款账户" value={search} onChange={(event) => setSearch(event.target.value)} placeholder="搜索账户或地址" /></label>
    <div className="address-book-list"><WalletSelectionList wallets={wallets} groups={groups} selected={draft} search={search} onToggle={toggle} onSelectGroup={selectGroup} /></div>
    <div className="dialog-actions"><button className="secondary" onClick={close}>取消</button><button className="primary" onClick={() => { setSelected(draft); close() }}><Check size={16} />确定选择 {draft.length} 个</button></div>
  </Dialog>
}

function ConfirmDialog({ job, wallets, initialPreflight, executionEnabled, close, run, onChanged, onStarted }: {
  job: TransferJob; wallets: WalletRecord[]; initialPreflight: JobPreflight | null; executionEnabled: boolean; close: () => void
  run: DialogProps['run']; onChanged: (result: JobPreflight) => void; onStarted: () => void
}) {
  const [preview, setPreview] = useState<JobPreflight>(() => initialPreflight ?? { valid: true, job, checks: [], summary: job.summary })
  const [confirmation, setConfirmation] = useState('')
  const [liveExecutionEnabled, setLiveExecutionEnabled] = useState(executionEnabled)
  const currentJob = preview.job
  const summary = preview.summary ?? { sourceWalletCount: new Set(currentJob.steps.map((step) => step.sourceWalletId)).size, stepCount: currentJob.steps.length, aptBaseUnits: '0', usdtBaseUnits: '0', maxStepCount: currentJob.steps.filter((step) => step.amountMode === 'max').length, estimatedGasBaseUnits: '0', warnings: [] }
  const checks = new Map(preview.checks.map((check) => [check.stepId, check]))
  const shuffle = () => void run(async () => {
    if (!preview.valid || currentJob.steps.length < 2) return
    const ids = secureShuffle(currentJob.steps.map((step) => step.id))
    const result = await post<JobPreflight>(`/api/v1/jobs/${currentJob.id}/reorder`, { stepIds: ids })
    setPreview(result)
    setConfirmation('')
    onChanged(result)
  }, '已重新生成转账顺序')
  return <Dialog title="转账预览" close={close} wide><div className="transfer-preview-dialog">
    <div className={`confirm-banner ${preview.valid ? 'valid' : ''}`}><ShieldAlert size={20} /><div><strong>{preview.valid ? '预览已生成' : '预览未通过检查'}</strong><span>{preview.valid ? '下面显示本次将要执行的全部转账。发送前仍需输入完整确认短语。' : '所有转账条目仍保留在下方；修正余额、手续费或顺序问题后返回编辑。'}</span></div></div>
    <div className="confirm-metrics"><Metric label="来源钱包" value={summary.sourceWalletCount.toString()} /><Metric label="转账笔数" value={summary.stepCount.toString()} /><Metric label="APT 总额" value={formatAmount(summary.aptBaseUnits, 'APT')} /><Metric label="USDt 总额" value={formatAmount(summary.usdtBaseUnits, 'USDT')} /><Metric label="预计手续费" value={`${formatAmount(summary.estimatedGasBaseUnits, 'APT')} APT`} /></div>
    {summary.warnings.map((warning) => <div className="warning-line" key={warning}>{warning}</div>)}
    <div className="preview-toolbar"><div><strong>执行顺序</strong><span>每一行的来源、目标、金额和等待时间始终绑定</span></div><button className="secondary" disabled={!preview.valid || currentJob.steps.length < 2} onClick={shuffle}><Shuffle size={16} />随机打乱条目</button></div>
    <div className="preview-list">{currentJob.steps.map((step) => { const check = checks.get(step.id); return <div className={`preview-step ${check && !check.valid ? 'invalid' : ''}`} key={step.id}><span className="preview-step-position">{step.position + 1}</span><div className="preview-step-source"><small>转出</small><TransferParty wallet={wallets.find((wallet) => wallet.id === step.sourceWalletId)} address={wallets.find((wallet) => wallet.id === step.sourceWalletId)?.address ?? step.sourceWalletId} /></div><div className="preview-step-target"><small>收款</small><TransferParty wallet={step.targetWalletId ? wallets.find((wallet) => wallet.id === step.targetWalletId) : null} address={step.targetAddress} /></div><strong className="preview-step-amount"><span>{step.amountMode === 'max' ? '全额' : step.frozenAmountDisplay ?? step.amountMin}</span><em>{step.asset === 'USDT' ? 'USDt' : 'APT'}</em></strong><small className="preview-step-wait"><Clock3 size={13} />{step.waitAfterSeconds > 0 ? `下一笔前等待 ${step.waitAfterSeconds} 秒` : '最后一笔，无需等待'}</small>{check && !check.valid && <div className="preview-step-error"><ShieldAlert size={13} /><span>{check.error ?? '检查未通过，请返回编辑修正'}</span></div>}</div> })}</div>
    {preview.valid && <label>输入完整确认短语<div className="phrase-row"><code className="phrase">{currentJob.confirmationPhrase}</code><button type="button" className="secondary small phrase-copy" title="复制确认短语" aria-label="复制确认短语" onClick={() => void navigator.clipboard.writeText(currentJob.confirmationPhrase ?? '')}><Copy size={14} />复制</button></div><input value={confirmation} onChange={(event) => setConfirmation(event.target.value)} /></label>}
    {!liveExecutionEnabled && <div className="error-banner"><Lock size={17} />当前页面记录的是仅预览状态。发送前会重新检查本机服务；若仍未开启真实转账，会保留在预览页面。</div>}
    <div className="dialog-actions transfer-preview-actions"><button className="secondary" onClick={close}>返回编辑</button><button className="danger-primary" disabled={!preview.valid || confirmation !== currentJob.confirmationPhrase} onClick={() => void run(async () => {
      const latestStatus = await getStatus()
      setLiveExecutionEnabled(latestStatus.executionEnabled)
      if (!latestStatus.unlocked) throw new Error('保险库已锁定，请重新解锁')
      if (!latestStatus.executionEnabled) throw new Error('本机服务仍是仅预览模式，请使用 --enable-mainnet 启动')
      await post(`/api/v1/jobs/${currentJob.id}/confirm`, { confirmation })
      onStarted()
    }, '任务已开始')}>{preview.valid ? '发送并执行' : '修正后再发送'}</button></div>
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
function DialogActions({ close, submit, onSubmit, disabled = false }: { close: () => void; submit: string; onSubmit?: () => void; disabled?: boolean }) { return <div className="dialog-actions"><button type="button" className="secondary" onClick={close}>取消</button><button type={onSubmit ? 'button' : 'submit'} className="primary" onClick={onSubmit} disabled={disabled}>{submit}</button></div> }
interface DialogProps { close: () => void; run: (action: () => Promise<void>, success?: string) => Promise<void>; reload: () => Promise<void> }

function PageHeader({ title, subtitle, actions }: { title: string; subtitle: string; actions?: React.ReactNode }) { return <header className="page-header"><div><h1>{title}</h1><p>{subtitle}</p></div>{actions && <div className="header-actions">{actions}</div>}</header> }
function NavButton({ active, icon, label, onClick, count }: { active: boolean; icon: React.ReactNode; label: string; onClick: () => void; count?: number }) { return <button className={`nav-button ${active ? 'active' : ''}`} aria-label={label} onClick={onClick}>{icon}<span>{label}</span>{Boolean(count) && <b>{count}</b>}</button> }
function Metric({ label, value, tone }: { label: string; value: string; tone?: string }) { return <div className={`metric ${tone ?? ''}`}><span>{label}</span><strong>{value}</strong></div> }
function IconButton({ title, icon, onClick, danger, disabled }: { title: string; icon: React.ReactNode; onClick: () => void; danger?: boolean; disabled?: boolean }) { return <button type="button" className={`icon-button ${danger ? 'danger' : ''}`} title={title} aria-label={title} onClick={onClick} disabled={disabled}>{icon}</button> }
function Empty({ icon, text }: { icon: React.ReactNode; text: string }) { return <div className="empty">{icon}<p>{text}</p></div> }
function Address({ value }: { value: string }) { return <button className="address" title={value} onClick={() => void navigator.clipboard.writeText(value)}><code>{short(value)}</code><Copy size={13} /></button> }
function TransferParty({ wallet, address }: { wallet?: WalletRecord | null; address: string }) {
  const alias = wallet ? walletAlias(wallet) : null
  return <div className="transfer-party">{alias && <strong>{alias}</strong>}<Address value={address} /></div>
}
function Status({ value }: { value: string }) { return <span className={`status status-${value}`}>{statusLabels[value] ?? value}</span> }
function short(value: string) { return value.length > 18 ? `${value.slice(0, 8)}...${value.slice(-6)}` : value }
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
