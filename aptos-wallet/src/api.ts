import type { AddressBookEntry, JobDraftInput, JobPreflight, TransferJob, VaultStatus, WalletGroup, WalletRecord } from '../shared/types'

let csrfToken = ''
const PRODUCTION_WALLET_URL = 'http://127.0.0.1:48271'

export async function getStatus(): Promise<VaultStatus> {
  const status = await request<VaultStatus>('/api/v1/status')
  csrfToken = status.csrfToken
  return status
}

async function refreshCsrfToken(): Promise<VaultStatus> {
  let response: Response
  try {
    response = await fetch('/api/v1/status', { credentials: 'same-origin', headers: { Accept: 'application/json' } })
  } catch {
    throw new Error(`无法连接本地钱包服务，请打开 ${PRODUCTION_WALLET_URL}`)
  }
  const body = await response.json() as VaultStatus & { error?: string }
  if (!response.ok) throw new Error(body.error ?? `请求失败 (${response.status})`)
  csrfToken = body.csrfToken
  return body
}

export async function request<T>(path: string, init: RequestInit = {}, retriedAfterCsrf = false): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  const method = (init.method ?? 'GET').toUpperCase()
  if (!['GET', 'HEAD'].includes(method)) headers.set('x-csrf-token', csrfToken)
  let response: Response
  try {
    response = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  } catch {
    throw new Error(`无法连接本地钱包服务，请打开 ${PRODUCTION_WALLET_URL}`)
  }
  const contentType = response.headers.get('content-type') ?? ''
  const body = contentType.includes('application/json') ? await response.json() : await response.text()
  if (response.status === 403 && !retriedAfterCsrf && method !== 'GET' && method !== 'HEAD'
    && typeof body === 'object' && body?.error === 'CSRF 校验失败') {
    const status = await refreshCsrfToken()
    if (!status.unlocked) {
      window.location.reload()
      throw new Error('本地钱包服务已重启，请重新解锁')
    }
    return request<T>(path, init, true)
  }
  if (!response.ok) throw new Error(typeof body === 'object' && body?.error ? body.error : `请求失败 (${response.status})`)
  return body as T
}

export const post = <T>(path: string, body: unknown = {}) => request<T>(path, { method: 'POST', body: JSON.stringify(body) })
export const put = <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) })
export async function loadWorkspace(): Promise<{ wallets: WalletRecord[]; groups: WalletGroup[]; jobs: TransferJob[]; addressBook: AddressBookEntry[] }> {
  const [wallets, groups, jobs, addressBook] = await Promise.all([
    request<WalletRecord[]>('/api/v1/wallets'),
    request<WalletGroup[]>('/api/v1/wallets/groups'),
    request<TransferJob[]>('/api/v1/jobs'),
    request<AddressBookEntry[]>('/api/v1/address-book'),
  ])
  return { wallets, groups, jobs, addressBook }
}

export async function saveAndPreviewJob(draft: JobDraftInput, id?: string): Promise<JobPreflight> {
  const job = id
    ? await put<TransferJob>(`/api/v1/jobs/${id}`, draft)
    : await post<TransferJob>('/api/v1/jobs', draft)
  return post<JobPreflight>(`/api/v1/jobs/${job.id}/check`)
}

export async function download(path: string, filename: string): Promise<void> {
  const response = await fetch(path, { credentials: 'same-origin' })
  if (!response.ok) throw new Error('下载失败')
  const url = URL.createObjectURL(await response.blob())
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = filename
  anchor.click()
  URL.revokeObjectURL(url)
}

export function subscribe(onSnapshot: (value: { wallets: WalletRecord[]; groups: WalletGroup[]; jobs: TransferJob[]; addressBook: AddressBookEntry[] }) => void): () => void {
  const source = new EventSource('/api/v1/events')
  source.addEventListener('snapshot', (event) => onSnapshot(JSON.parse((event as MessageEvent).data)))
  source.addEventListener('vault-locked', () => window.location.reload())
  source.addEventListener('session-replaced', () => window.location.reload())
  return () => source.close()
}
