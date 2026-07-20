import type { JobDraftInput, JobPreflight, TransferJob, VaultStatus, WalletGroup, WalletRecord } from '../shared/types'

let csrfToken = ''

export async function getStatus(): Promise<VaultStatus> {
  const status = await request<VaultStatus>('/api/v1/status')
  csrfToken = status.csrfToken
  return status
}

export async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers = new Headers(init.headers)
  if (init.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (!['GET', 'HEAD'].includes((init.method ?? 'GET').toUpperCase())) headers.set('x-csrf-token', csrfToken)
  const response = await fetch(path, { ...init, headers, credentials: 'same-origin' })
  const contentType = response.headers.get('content-type') ?? ''
  const body = contentType.includes('application/json') ? await response.json() : await response.text()
  if (!response.ok) throw new Error(typeof body === 'object' && body?.error ? body.error : `请求失败 (${response.status})`)
  return body as T
}

export const post = <T>(path: string, body: unknown = {}) => request<T>(path, { method: 'POST', body: JSON.stringify(body) })
export const put = <T>(path: string, body: unknown) => request<T>(path, { method: 'PUT', body: JSON.stringify(body) })
export async function loadWorkspace(): Promise<{ wallets: WalletRecord[]; groups: WalletGroup[]; jobs: TransferJob[] }> {
  const [wallets, groups, jobs] = await Promise.all([
    request<WalletRecord[]>('/api/v1/wallets'),
    request<WalletGroup[]>('/api/v1/wallets/groups'),
    request<TransferJob[]>('/api/v1/jobs'),
  ])
  return { wallets, groups, jobs }
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

export function subscribe(onSnapshot: (value: { wallets: WalletRecord[]; groups: WalletGroup[]; jobs: TransferJob[] }) => void): () => void {
  const source = new EventSource('/api/v1/events')
  source.addEventListener('snapshot', (event) => onSnapshot(JSON.parse((event as MessageEvent).data)))
  return () => source.close()
}
