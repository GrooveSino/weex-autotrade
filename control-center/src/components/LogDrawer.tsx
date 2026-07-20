import { useEffect, useRef, useState } from 'react'
import { ArrowDownToLine, CircleDot, Copy, RefreshCw, Terminal, X } from 'lucide-react'
import { fetchInstanceLogs } from '../services/controlCenter'
import type { AccountInstance, LogLine } from '../types'

interface LogDrawerProps {
  account: AccountInstance | null
  onClose: () => void
}

function displayTime(value: string): string {
  const parsed = new Date(value)
  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleTimeString('zh-CN', { hour12: false })
}

const levelLabel: Record<LogLine['level'], string> = {
  info: '信息',
  success: '成功',
  warn: '警告',
  error: '错误',
}

export function LogDrawer({ account, onClose }: LogDrawerProps) {
  const accountRef = useRef(account)
  const terminalRef = useRef<HTMLDivElement>(null)
  const [requestNonce, setRequestNonce] = useState(0)
  const [result, setResult] = useState<{
    key: string
    accountId: string
    lines: LogLine[]
    error: string | null
    connection: 'connecting' | 'connected' | 'retrying'
  }>({
    key: '',
    accountId: '',
    lines: [],
    error: null,
    connection: 'connecting',
  })
  accountRef.current = account
  const accountId = account?.id ?? ''
  const requestKey = accountId ? `${accountId}:${requestNonce}` : ''
  const loading = Boolean(account) && result.key !== requestKey
  const lines = result.accountId === accountId ? result.lines : []
  const error = result.accountId === accountId ? result.error : null
  const connection = loading ? 'connecting' : result.connection

  useEffect(() => {
    const selected = accountRef.current
    if (!selected || selected.id !== accountId) return
    let active = true
    let timer: number | undefined
    let cursor: string | null = null

    const poll = async () => {
      try {
        const batch = await fetchInstanceLogs(accountRef.current ?? selected, cursor)
        if (!active) return
        const replace = cursor === null || batch.reset
        cursor = batch.cursor
        setResult((previous) => {
          const existing = !replace && previous.accountId === accountId ? previous.lines : []
          const byId = new Map(existing.map((line) => [line.id, line]))
          batch.lines.forEach((line) => byId.set(line.id, line))
          return {
            key: requestKey,
            accountId,
            lines: [...byId.values()].slice(-500),
            error: null,
            connection: 'connected',
          }
        })
      } catch (reason: unknown) {
        if (!active) return
        setResult((previous) => ({
          key: requestKey,
          accountId,
          lines: previous.accountId === accountId ? previous.lines : [],
          error: reason instanceof Error ? reason.message : '日志加载失败',
          connection: 'retrying',
        }))
      } finally {
        if (active) timer = window.setTimeout(poll, 2_000)
      }
    }

    void poll()
    return () => {
      active = false
      if (timer !== undefined) window.clearTimeout(timer)
    }
  }, [accountId, requestKey])

  useEffect(() => {
    const terminal = terminalRef.current
    if (terminal) terminal.scrollTop = terminal.scrollHeight
  }, [accountId, lines.length])

  const plainText = lines.map((line) => `[${line.timestamp}] ${levelLabel[line.level]} ${line.message}`).join('\n')

  if (!account) return null

  const downloadLogs = () => {
    const url = URL.createObjectURL(new Blob([plainText], { type: 'text/plain;charset=utf-8' }))
    const anchor = document.createElement('a')
    anchor.href = url
    anchor.download = `${account.id}-runtime.log`
    anchor.click()
    URL.revokeObjectURL(url)
  }

  return (
    <div className="drawer-backdrop" role="presentation" onMouseDown={onClose}>
      <aside className="log-drawer" role="dialog" aria-modal="true" aria-labelledby="log-title" onMouseDown={(event) => event.stopPropagation()}>
        <header className="drawer-header">
          <div className="drawer-title">
            <span className="terminal-mark"><Terminal size={16} /></span>
            <div>
              <h2 id="log-title">{account.name}</h2>
              <span>{account.id} / 实时日志</span>
            </div>
          </div>
          <div className="drawer-actions">
            <button className="icon-button dark" type="button" onClick={() => navigator.clipboard.writeText(plainText)} data-tooltip="复制日志" aria-label="复制日志"><Copy size={15} /></button>
            <button className="icon-button dark" type="button" onClick={() => setRequestNonce((value) => value + 1)} data-tooltip="重新加载日志" aria-label="重新加载日志"><RefreshCw size={15} /></button>
            <button className="icon-button dark" type="button" onClick={downloadLogs} data-tooltip="下载日志" aria-label="下载日志"><ArrowDownToLine size={15} /></button>
            <button className="icon-button dark" type="button" onClick={onClose} data-tooltip="关闭日志" aria-label="关闭日志"><X size={16} /></button>
          </div>
        </header>

        <div className={`terminal-statusbar ${connection}`}>
          <span><CircleDot size={12} />{
            connection === 'connecting' ? '正在建立连接' : connection === 'retrying' ? '连接中断，自动重试' : '实时日志已连接'
          }</span>
          <span>仅当前实例 / 2s</span>
        </div>

        <div className="terminal-body" aria-live="polite" ref={terminalRef}>
          {loading && lines.length === 0 ? (
            <div className="terminal-loading"><span className="terminal-cursor" />正在请求 {account.id} 的日志...</div>
          ) : error && lines.length === 0 ? (
            <div className="terminal-error">错误：{error}</div>
          ) : (
            <>
              {lines.map((line) => (
                <div className={`log-line ${line.level}`} key={line.id}>
                  <time>{displayTime(line.timestamp)}</time>
                  <span className="log-level">{levelLabel[line.level]}</span>
                  <span className="log-message">{line.message}</span>
                </div>
              ))}
              {error && <div className="terminal-error terminal-retry">重试：{error}</div>}
            </>
          )}
        </div>
      </aside>
    </div>
  )
}
