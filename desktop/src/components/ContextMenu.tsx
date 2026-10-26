import { useEffect, useLayoutEffect, useRef, useState } from 'react'
import type { LucideIcon } from 'lucide-react'
import './ContextMenu.css'

export type MenuItem = {
  key: string
  label: string
  icon?: LucideIcon
  danger?: boolean
  disabled?: boolean
  /** 在这一项之前画一条分隔线 */
  separator?: boolean
  onSelect: () => void
}

export type MenuState = { x: number; y: number; items: MenuItem[] } | null

/**
 * 右键菜单 —— 对齐 DSH 的浮层菜单规格（model_select_css 的 .menu / .cell）：
 *   fixed 定位 / padding 4 / radius lg / elevation-prominent / 玻璃底
 *   cell：height 34 / padding 0 8px / gap 6 / font 13
 * 定位会翻转：靠近视口右/下边缘时向左/上展开，避免出屏。
 */
export default function ContextMenu({
  state,
  onClose,
}: {
  state: MenuState
  onClose: () => void
}) {
  const ref = useRef<HTMLDivElement>(null)
  const [pos, setPos] = useState<{ left: number; top: number } | null>(null)

  useLayoutEffect(() => {
    if (!state || !ref.current) {
      setPos(null)
      return
    }
    const r = ref.current.getBoundingClientRect()
    const pad = 8
    let left = state.x
    let top = state.y
    if (left + r.width + pad > window.innerWidth) left = state.x - r.width
    if (top + r.height + pad > window.innerHeight) top = state.y - r.height
    setPos({ left: Math.max(pad, left), top: Math.max(pad, top) })
  }, [state])

  useEffect(() => {
    if (!state) return
    const close = () => onClose()
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('mousedown', close)
    window.addEventListener('blur', close)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', close)
      window.removeEventListener('blur', close)
      window.removeEventListener('keydown', onKey)
    }
  }, [state, onClose])

  if (!state) return null

  return (
    <div
      ref={ref}
      className="ctx-menu"
      style={{
        left: pos?.left ?? state.x,
        top: pos?.top ?? state.y,
        visibility: pos ? 'visible' : 'hidden',
      }}
      onMouseDown={(e) => e.stopPropagation()}
      onContextMenu={(e) => e.preventDefault()}
    >
      {state.items.map((it, i) => {
        const Icon = it.icon
        return (
          <div key={it.key}>
            {(it.separator || i === 0) && i > 0 && <div className="ctx-sep" />}
            <button
              className={'ctx-cell' + (it.danger ? ' is-danger' : '')}
              disabled={it.disabled}
              onClick={() => {
                onClose()
                it.onSelect()
              }}
            >
              {Icon ? <Icon size={14} className="ctx-icon" /> : <span className="ctx-icon" />}
              <span className="ctx-label">{it.label}</span>
            </button>
          </div>
        )
      })}
    </div>
  )
}
