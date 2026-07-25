import { useEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import { BarChart3, CircleX, MoreHorizontal, ScrollText, Settings2, SlidersHorizontal } from 'lucide-react'
import type { AccountInstance } from '../../types'

interface AccountActionsMenuProps {
  account: AccountInstance
  executionDisabled: boolean
  onClosePositions: (account: AccountInstance) => void
  onOpenExecutions: (account: AccountInstance) => void
  onOpenTradeVolume: (account: AccountInstance) => void
  onAssignStrategy: (account: AccountInstance) => void
  onEdit: (account: AccountInstance) => void
}

const menuWidth = 216
const menuItemHeight = 36
const viewportGap = 8

export function AccountActionsMenu({
  account,
  executionDisabled,
  onClosePositions,
  onOpenExecutions,
  onOpenTradeVolume,
  onAssignStrategy,
  onEdit,
}: AccountActionsMenuProps) {
  const [open, setOpen] = useState(false)
  const [position, setPosition] = useState({ left: 0, top: 0 })
  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)
  const hasExposure = account.exposure.btcLong > 0 || account.exposure.ethShort > 0
  const canShowClose = account.status !== 'running' && hasExposure

  useEffect(() => {
    if (!open) return
    const closeOnPointerDown = (event: PointerEvent) => {
      const target = event.target as Node
      if (!triggerRef.current?.contains(target) && !menuRef.current?.contains(target)) setOpen(false)
    }
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        setOpen(false)
        triggerRef.current?.focus()
      }
    }
    const closeMenu = () => setOpen(false)
    document.addEventListener('pointerdown', closeOnPointerDown)
    document.addEventListener('keydown', closeOnEscape)
    window.addEventListener('resize', closeMenu)
    window.addEventListener('scroll', closeMenu, true)
    return () => {
      document.removeEventListener('pointerdown', closeOnPointerDown)
      document.removeEventListener('keydown', closeOnEscape)
      window.removeEventListener('resize', closeMenu)
      window.removeEventListener('scroll', closeMenu, true)
    }
  }, [open])

  const toggleMenu = () => {
    if (open) {
      setOpen(false)
      return
    }
    const rect = triggerRef.current?.getBoundingClientRect()
    if (!rect) return
    const itemCount = canShowClose ? 5 : 4
    const estimatedHeight = itemCount * menuItemHeight + (canShowClose ? 17 : 8)
    const left = Math.min(
      window.innerWidth - menuWidth - viewportGap,
      Math.max(viewportGap, rect.right - menuWidth),
    )
    const top = window.innerHeight - rect.bottom >= estimatedHeight + viewportGap
      ? rect.bottom + 4
      : Math.max(viewportGap, rect.top - estimatedHeight - 4)
    setPosition({ left, top })
    setOpen(true)
  }

  const run = (action: (account: AccountInstance) => void) => {
    setOpen(false)
    action(account)
  }

  return (
    <>
      <button
        ref={triggerRef}
        className="icon-button"
        type="button"
        onClick={toggleMenu}
        data-tooltip="更多账号操作"
        aria-label={`更多账号操作：${account.name}`}
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <MoreHorizontal size={16} />
        <span className="mobile-action-label">更多</span>
      </button>
      {open && createPortal(
        <div
          ref={menuRef}
          className="account-actions-menu"
          role="menu"
          aria-label={`${account.name} 账号操作`}
          style={{ left: position.left, top: position.top }}
        >
          {canShowClose && (
            <>
              <button
                className="action-menu-item danger-item"
                type="button"
                role="menuitem"
                disabled={executionDisabled}
                onClick={() => run(onClosePositions)}
              >
                <CircleX size={15} />
                <span>一键平仓</span>
                {executionDisabled && <small>由实盘 Campaign 管理</small>}
              </button>
              <div className="action-menu-divider" role="separator" />
            </>
          )}
          <button className="action-menu-item" type="button" role="menuitem" onClick={() => run(onOpenExecutions)}>
            <ScrollText size={15} /><span>策略运行记录</span>
          </button>
          <button className="action-menu-item" type="button" role="menuitem" onClick={() => run(onOpenTradeVolume)}>
            <BarChart3 size={15} /><span>统计近期交易量</span>
          </button>
          <button className="action-menu-item" type="button" role="menuitem" onClick={() => run(onAssignStrategy)}>
            <SlidersHorizontal size={15} /><span>切换绑定策略</span>
          </button>
          <button className="action-menu-item" type="button" role="menuitem" onClick={() => run(onEdit)}>
            <Settings2 size={15} /><span>编辑账号与代理</span>
          </button>
        </div>,
        document.body,
      )}
    </>
  )
}
