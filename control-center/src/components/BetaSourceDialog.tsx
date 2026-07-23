import { useEffect, useState } from 'react'
import { LoaderCircle, X } from 'lucide-react'
import { fetchBetaSourceSettings, updateBetaSourceSettings } from '../services/controlCenter'
import type { BetaSourceSettings } from '../types'

type BetaSourceDialogProps = {
  onClose: () => void
  onChanged: (settings: BetaSourceSettings) => void
  onToast: (message: string) => void
}

export function BetaSourceDialog({ onClose, onChanged, onToast }: BetaSourceDialogProps) {
  const [settings, setSettings] = useState<BetaSourceSettings | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [saving, setSaving] = useState(false)

  useEffect(() => {
    let mounted = true
    void fetchBetaSourceSettings()
      .then((next) => mounted && setSettings(next))
      .catch((reason: unknown) => mounted && setError(reason instanceof Error ? reason.message : '无法读取 Beta 来源设置'))
    return () => { mounted = false }
  }, [])

  const save = async () => {
    if (!settings) return
    setSaving(true)
    setError(null)
    try {
      const updated = await updateBetaSourceSettings({
        url: settings.url.trim(),
        timeoutSeconds: settings.timeoutSeconds,
        refreshIntervalSeconds: settings.refreshIntervalSeconds,
        backgroundRefreshEnabled: settings.backgroundRefreshEnabled,
      })
      onChanged(updated)
      onToast('Beta 来源已更新；当前执行任务保持其启动时快照')
      onClose()
    } catch (reason: unknown) {
      setError(reason instanceof Error ? reason.message : '无法保存 Beta 来源设置')
    } finally {
      setSaving(false)
    }
  }

  return (
    <div className="modal-backdrop" role="presentation" onMouseDown={onClose}>
      <section className="dialog beta-source-dialog" role="dialog" aria-modal="true" aria-labelledby="beta-source-title" onMouseDown={(event) => event.stopPropagation()}>
        <header className="dialog-header">
          <div><h2 id="beta-source-title">Beta 来源</h2><span>全局运行配置</span></div>
          <button className="icon-button" type="button" onClick={onClose} data-tooltip="关闭" aria-label="关闭"><X size={16} /></button>
        </header>
        {!settings ? (
          <div className="dialog-loading"><LoaderCircle className="spin" size={18} /><span>{error ?? '正在读取来源设置'}</span></div>
        ) : (
          <form className="beta-source-form" onSubmit={(event) => { event.preventDefault(); void save() }}>
            <label><span>HTTPS 来源地址</span><input type="url" value={settings.url} onChange={(event) => setSettings({ ...settings, url: event.target.value })} placeholder="https://example.com/api/v1/hedge-ratio" required spellCheck={false} /></label>
            <div className="beta-source-grid">
              <label><span>请求超时（秒）</span><input type="number" min="0.1" max="60" step="0.1" value={settings.timeoutSeconds} onChange={(event) => setSettings({ ...settings, timeoutSeconds: Number(event.target.value) })} required /></label>
              <label><span>刷新周期（秒）</span><input type="number" min="0.1" max="3600" step="0.1" value={settings.refreshIntervalSeconds} onChange={(event) => setSettings({ ...settings, refreshIntervalSeconds: Number(event.target.value) })} required /></label>
            </div>
            <label className="risk-check"><input type="checkbox" checked={settings.backgroundRefreshEnabled} onChange={(event) => setSettings({ ...settings, backgroundRefreshEnabled: event.target.checked })} /><span>后台集中刷新，账号与浏览器复用同一份 Beta 快照</span></label>
            <p className="form-hint">只接受不含用户名或密码的 HTTP(S) 地址。保存后立即刷新遥测；已经运行的任务继续使用启动时冻结的计划与 Beta 快照。</p>
            {error && <p className="form-error">{error}</p>}
            <footer className="dialog-actions"><button className="button secondary" type="button" onClick={onClose}>取消</button><button className="button primary" type="submit" disabled={saving}>{saving ? <LoaderCircle className="spin" size={14} /> : null}保存来源</button></footer>
          </form>
        )}
      </section>
    </div>
  )
}
