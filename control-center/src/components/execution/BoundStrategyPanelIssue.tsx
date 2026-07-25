import { AlertTriangle, RefreshCw, Settings } from 'lucide-react'
import type { StrategyPanelIssue } from './boundStrategyPanelError'

interface BoundStrategyPanelIssueProps {
  issue: StrategyPanelIssue
  busy: boolean
  onRetryPrepare: () => void
  onVerifyExecution: () => void
  onEditAccount: () => void
  onOpenBetaSettings: () => void
}

export function BoundStrategyPanelIssueView({
  issue,
  busy,
  onRetryPrepare,
  onVerifyExecution,
  onEditAccount,
  onOpenBetaSettings,
}: BoundStrategyPanelIssueProps) {
  const handler = {
    retry_prepare: onRetryPrepare,
    verify_execution: onVerifyExecution,
    edit_account: onEditAccount,
    open_beta_settings: onOpenBetaSettings,
    reload_page: () => window.location.reload(),
    none: null,
  }[issue.action]
  return (
    <div className="execution-warning strategy-panel-issue" role="alert">
      <AlertTriangle size={16} />
      <div>
        <strong>{issue.title}</strong>
        <p><span>原因：</span>{issue.reason}</p>
        <p><span>下一步：</span>{issue.nextStep}</p>
        {handler && <button className="button secondary compact-button" type="button" disabled={busy} onClick={handler}>
          {issue.action === 'edit_account' || issue.action === 'open_beta_settings' ? <Settings size={13} /> : <RefreshCw size={13} />}
          {issue.actionLabel ?? '继续处理'}
        </button>}
      </div>
    </div>
  )
}
