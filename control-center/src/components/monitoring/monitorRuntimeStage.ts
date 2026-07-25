import type { ActiveExecutionWait } from '../../types'

export type RuntimeWaitState = 'counting' | 'transitioning' | 'indefinite'

export interface RuntimeWaitPresentation {
  wait: ActiveExecutionWait
  state: RuntimeWaitState
  elapsedMs: number
  remainingMs: number | null
  label: string
  phase: string | null
  countdownLabel: string
}

export function presentRuntimeWait(
  wait: ActiveExecutionWait,
  serverNowMs: number,
): RuntimeWaitPresentation {
  const delta = Math.max(0, serverNowMs - wait.updatedAtMs)
  const elapsedMs = wait.startedAtMs === null
    ? wait.elapsedMs + delta
    : Math.max(0, serverNowMs - wait.startedAtMs)
  const deadlineAtMs = wait.deadlineAtMs ?? (
    wait.remainingMs === null ? null : wait.updatedAtMs + wait.remainingMs
  )
  if (deadlineAtMs === null) {
    return {
      wait, state: 'indefinite', elapsedMs, remainingMs: null, label: wait.label,
      phase: null, countdownLabel: '等待状态',
    }
  }
  const remainingMs = deadlineAtMs - serverNowMs
  if (remainingMs > 0) {
    return {
      wait, state: 'counting', elapsedMs, remainingMs, label: wait.label,
      phase: null, countdownLabel: countdownLabel(wait.key),
    }
  }
  const transition = transitionPresentation(wait.key)
  return {
    wait, state: 'transitioning', elapsedMs, remainingMs: null,
    label: transition.label, phase: transition.phase, countdownLabel: '阶段状态',
  }
}

export function selectPrimaryRuntimeWait(
  waits: ActiveExecutionWait[],
  serverNowMs: number,
): RuntimeWaitPresentation | null {
  const presented = waits.map((wait) => presentRuntimeWait(wait, serverNowMs))
  const active = presented.filter((item) => item.state !== 'transitioning')
  const candidates = active.length ? active : presented
  return candidates.find((item) => item.wait.key === 'hold' || item.wait.key === 'round-gap')
    ?? candidates[0]
    ?? null
}

export function expiredWaitFingerprint(
  wait: ActiveExecutionWait,
  serverNowMs: number,
): string | null {
  const deadline = wait.deadlineAtMs ?? (
    wait.remainingMs === null ? null : wait.updatedAtMs + wait.remainingMs
  )
  return deadline !== null && serverNowMs >= deadline
    ? `${wait.key}:${deadline}`
    : null
}

export function unreconciledExpiredWaitKeys(
  waits: ActiveExecutionWait[],
  serverNowMs: number,
  executionKey: string,
  reconciled: ReadonlySet<string>,
): string[] {
  return waits.flatMap((wait) => {
    const fingerprint = expiredWaitFingerprint(wait, serverNowMs)
    if (!fingerprint) return []
    const key = `${executionKey}:${fingerprint}`
    return reconciled.has(key) ? [] : [key]
  })
}

function countdownLabel(key: string): string {
  if (key === 'hold') return '持仓剩余'
  if (key === 'round-gap') return '下轮开始'
  return '等待剩余'
}

function transitionPresentation(key: string): { label: string; phase: string } {
  if (key === 'hold') return { label: '持仓计时结束，正在进入平仓', phase: '准备平仓' }
  if (key === 'round-gap') return { label: '轮次间隔结束，正在开始下一轮', phase: '准备下一轮' }
  if (key.startsWith('phase-pacing:') || key === 'actor-phase-queue') {
    return { label: '预计槽位时间已到，正在确认执行资格', phase: '确认执行槽位' }
  }
  return { label: '等待时限已到，正在确认下一阶段', phase: '阶段切换中' }
}
