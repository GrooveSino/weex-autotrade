

export const sleep = (ms: number) => new Promise((resolve) => window.setTimeout(resolve, ms))
export const configuredBaseUrl = import.meta.env.VITE_API_BASE_URL?.trim().replace(/\/$/, '')

export const controlPlaneEnabled = Boolean(configuredBaseUrl)
export const dataSourceLabel = controlPlaneEnabled ? '控制平面 API' : '内置 Mock 服务'

export type LocalUserSession = { userId: string }

export class ControlPlaneRequestError extends Error {
  readonly status: number
  readonly commandId: string | null

  constructor(message: string, status: number, commandId: string | null) {
    super(message)
    this.name = 'ControlPlaneRequestError'
    this.status = status
    this.commandId = commandId
  }
}

export async function apiRequest<T>(path: string, init?: RequestInit): Promise<T> {
  if (!configuredBaseUrl) throw new Error('控制平面 API 尚未配置')
  const headers = new Headers(init?.headers)
  if (init?.body && !headers.has('Content-Type')) headers.set('Content-Type', 'application/json')
  if (init?.method && !['GET', 'HEAD'].includes(init.method.toUpperCase()) && !headers.has('X-Fleet-Command-Id')) {
    headers.set('X-Fleet-Command-Id', crypto.randomUUID())
  }
  const response = await fetch(`${configuredBaseUrl}${path}`, {
    ...init,
    headers,
    credentials: 'include',
  })
  if (!response.ok) {
    const payload = await response.json().catch(() => null) as { detail?: unknown } | null
    const detail = payload?.detail
    const message = typeof detail === 'string'
      ? detail
      : Array.isArray(detail)
        ? detail
            .map((item) => {
              if (!item || typeof item !== 'object') return null
              const error = item as { loc?: unknown; msg?: unknown }
              const location = Array.isArray(error.loc) ? error.loc.slice(1).join('.') : ''
              const description = typeof error.msg === 'string' ? error.msg : ''
              return [location, description].filter(Boolean).join(': ')
            })
            .filter(Boolean)
            .join('; ')
        : ''
    throw new ControlPlaneRequestError(
      message || `请求失败（HTTP ${response.status}）：服务端没有返回可读的错误原因，请稍后重试并联系管理员检查执行器日志`,
      response.status,
      headers.get('X-Fleet-Command-Id'),
    )
  }
  if (response.status === 204) return undefined as T
  return response.json() as Promise<T>
}
