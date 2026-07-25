import { useEffect } from 'react'
import type { BetaCampaign } from '../types'
import { listBoundStrategyExecutions } from '../services'

const recoveryStatus = new Set<BetaCampaign['status']>(['recovering', 'uncertain'])

/** Follow the server journal only while a dialog is resolving a prior task. */
export function useBoundStrategyRecovery(
  accountId: string,
  enabled: boolean,
  execution: BetaCampaign | null,
  onTerminal: () => void,
) {
  const campaignId = execution?.campaignId ?? null
  const status = execution?.status ?? null

  useEffect(() => {
    if (!enabled || !campaignId || !status || !recoveryStatus.has(status)) return
    let cancelled = false
    let timer: number | undefined

    const check = async () => {
      try {
        const executions = await listBoundStrategyExecutions({ id: accountId })
        const current = executions.find((item) => item.campaignId === campaignId)
        if (!cancelled && current && !recoveryStatus.has(current.status)) {
          onTerminal()
          return
        }
      } catch {
        // Preserve the last verified panel state. The server recovery loop is
        // authoritative, so a failed browser read must not reset it to zero.
      }
      if (!cancelled) timer = window.setTimeout(check, 1_500)
    }
    void check()
    return () => {
      cancelled = true
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [accountId, campaignId, enabled, onTerminal, status])
}
