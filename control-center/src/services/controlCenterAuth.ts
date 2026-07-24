import type { AccountInstance, ControlPlaneHealth, ExecutionCapacity, VolumeSessionProjection } from '../types'
import { apiRequest, type LocalUserSession } from './controlCenterCore'

export async function fetchLocalUserSession(): Promise<LocalUserSession> {
  return apiRequest<LocalUserSession>('/auth/me')
}

export async function loginLocalUser(username: string, password: string): Promise<LocalUserSession> {
  return apiRequest<LocalUserSession>('/auth/login', {
    method: 'POST',
    body: JSON.stringify({ username, password }),
  })
}

export async function logoutLocalUser(): Promise<void> {
  await apiRequest<void>('/auth/logout', { method: 'POST' })
}

export async function listAccountInstances(): Promise<AccountInstance[]> {
  return apiRequest<AccountInstance[]>('/instances')
}

export async function getVolumeSession(sessionId: string): Promise<VolumeSessionProjection> {
  return apiRequest<VolumeSessionProjection>(`/volume-sessions/${sessionId}`)
}

export async function syncVolumeSession(sessionId: string): Promise<VolumeSessionProjection> {
  return apiRequest<VolumeSessionProjection>(`/volume-sessions/${sessionId}/sync`, { method: 'POST' })
}

export async function fetchControlPlaneHealth(): Promise<ControlPlaneHealth> {
  return apiRequest<ControlPlaneHealth>('/health')
}

export async function fetchExecutionCapacity(): Promise<ExecutionCapacity> {
  return apiRequest<ExecutionCapacity>('/executor/capacity')
}
