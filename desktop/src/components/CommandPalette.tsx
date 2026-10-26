import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Search,
  CornerDownLeft,
  LayoutDashboard,
  MessagesSquare,
  Images,
  ScrollText,
  Settings2,
  Play,
  Square,
  Moon,
  Sun,
  Sigma,
  Loader2,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import { api } from '../lib/api'
import { SETTINGS_FLAT } from '../lib/settingsGroups'
import type { PageKey } from './Sidebar'
import './CommandPalette.css'

type Cmd = {
  id: string
  group: string
  label: string
  hint?: string
  icon: LucideIcon
  keywords?: string
  run: () => void
}

/**
 * ⌘K / Ctrl+K 命令面板 —— DSH 的「一处搜索所有」入口。
 * 纯前端聚合：页面跳转、设置分类、常用动作。不做远程搜索（本地瞬间响应）。
 */
export default function CommandPalette({
  open,
  onClose,
  onNavigate,
  onOpenSettings,
  onToggleTheme,
  theme,
}: {
  open: boolean
  onClose: () => void
  onNavigate: (p: PageKey) => void
  onOpenSettings: (section?: string) => void
  onToggleTheme: () => void
  theme: 'light' | 'dark'
}) {
  const [q, setQ] = useState('')
  const [index, setIndex] = useState(0)
  const [busy, setBusy] = useState(false)
  const [note, setNote] = useState('')
  const inputRef = useRef<HTMLInputElement>(null)
  const listRef = useRef<HTMLDivElement>(null)

  const commands = useMemo<Cmd[]>(() => {
    const pages: Cmd[] = [
      { id: 'page-dashboard', group: '页面', label: '概览', icon: LayoutDashboard, run: () => onNavigate('dashboard') },
      { id: 'page-console', group: '页面', label: '会客厅', icon: MessagesSquare, keywords: 'console 会话 旁观 本体', run: () => onNavigate('console') },
      { id: 'page-gallery', group: '页面', label: '图库', icon: Images, run: () => onNavigate('gallery') },
      { id: 'page-logs', group: '页面', label: '日志', icon: ScrollText, run: () => onNavigate('logs') },
    ]

    const settings: Cmd[] = SETTINGS_FLAT.map((s) => ({
      id: 'set-' + s.key,
      group: '设置',
      label: s.label,
      hint: '设置 → ' + s.label,
      icon: s.icon,
      keywords: 'settings 设置 ' + s.key,
      run: () => onOpenSettings(s.key),
    }))

    const actions: Cmd[] = [
      {
        id: 'act-settings',
        group: '动作',
        label: '打开设置',
        icon: Settings2,
        run: () => onOpenSettings(),
      },
      {
        id: 'act-theme',
        group: '动作',
        label: theme === 'dark' ? '切换到浅色主题' : '切换到深色主题',
        icon: theme === 'dark' ? Sun : Moon,
        keywords: 'theme 主题 外观',
        run: onToggleTheme,
      },
      {
        id: 'act-start',
        group: '动作',
        label: '启动糖糖',
        icon: Play,
        keywords: 'start 启动 运行',
        run: () => void botStart(),
      },
      {
        id: 'act-stop',
        group: '动作',
        label: '停止糖糖',
        icon: Square,
        keywords: 'stop 停止',
        run: () => void botStop(),
      },
    ]

    if (busy) {
      actions.push({
        id: 'act-busy',
        group: '动作',
        label: '正在执行…',
        icon: Loader2,
        run: () => {},
      })
    }

    return [...pages, ...settings, ...actions]

    async function botStart() {
      setBusy(true)
      try {
        const r = await api.botStart()
        setNote(r.already ? '糖糖已在运行' : `已启动（PID ${r.pid ?? '—'}）`)
      } catch (e) {
        setNote((e as Error).message)
      } finally {
        setBusy(false)
      }
    }
    async function botStop() {
      setBusy(true)
      try {
        const r = await api.botStop()
        setNote(r.already ? '糖糖本来就没在跑' : '已停止')
      } catch (e) {
        setNote((e as Error).message)
      } finally {
        setBusy(false)
      }
    }
  }, [theme, onNavigate, onOpenSettings, onToggleTheme, busy])

  const filtered = useMemo(() => {
    const key = q.trim().toLowerCase()
    if (!key) return commands
    return commands.filter((c) =>
      `${c.label} ${c.group} ${c.keywords ?? ''}`.toLowerCase().includes(key),
    )
  }, [commands, q])

  useEffect(() => {
    if (open) {
      setQ('')
      setIndex(0)
      setNote('')
      setTimeout(() => inputRef.current?.focus(), 10)
    }
  }, [open])

  useEffect(() => {
    setIndex(0)
  }, [q])

  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') {
        onClose()
        return
      }
      if (e.key === 'ArrowDown') {
        e.preventDefault()
        setIndex((i) => Math.min(filtered.length - 1, i + 1))
      }
      if (e.key === 'ArrowUp') {
        e.preventDefault()
        setIndex((i) => Math.max(0, i - 1))
      }
      if (e.key === 'Enter') {
        e.preventDefault()
        const item = filtered[index]
        if (item && item.id !== 'act-busy') {
          item.run()
          if (!item.id.startsWith('act-start') && !item.id.startsWith('act-stop')) onClose()
        }
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, filtered, index, onClose])

  useEffect(() => {
    const el = listRef.current?.querySelector('.cmdk-item.is-active')
    el?.scrollIntoView({ block: 'nearest' })
  }, [index])

  if (!open) return null

  let lastGroup = ''
  return (
    <div className="cmdk-overlay" onMouseDown={onClose}>
      <div className="cmdk" onMouseDown={(e) => e.stopPropagation()}>
        <div className="cmdk-search">
          <Search size={15} />
          <input
            ref={inputRef}
            value={q}
            placeholder="搜索页面、设置、动作…"
            onChange={(e) => setQ(e.target.value)}
          />
          <kbd className="cmdk-kbd">Esc</kbd>
        </div>

        <div className="cmdk-list" ref={listRef}>
          {filtered.length === 0 && <div className="cmdk-empty">没有匹配项</div>}
          {filtered.map((c, i) => {
            const Icon = c.icon
            const showGroup = c.group !== lastGroup
            lastGroup = c.group
            return (
              <div key={c.id}>
                {showGroup && <div className="cmdk-group">{c.group}</div>}
                <button
                  className={'cmdk-item' + (i === index ? ' is-active' : '')}
                  onMouseMove={() => setIndex(i)}
                  onClick={() => {
                    c.run()
                    if (!c.id.startsWith('act-start') && !c.id.startsWith('act-stop')) onClose()
                  }}
                >
                  <span className="cmdk-icon">
                    <Icon size={15} className={c.id === 'act-busy' ? 'spin' : ''} />
                  </span>
                  <span className="cmdk-label">{c.label}</span>
                  {c.hint && <span className="cmdk-hint">{c.hint}</span>}
                  {i === index && <CornerDownLeft size={12} className="cmdk-enter" />}
                </button>
              </div>
            )
          })}
        </div>

        <div className="cmdk-foot">
          {note ? (
            <span className="cmdk-note">
              <Sigma size={12} />
              {note}
            </span>
          ) : (
            <>
              <span>
                <kbd>↑</kbd>
                <kbd>↓</kbd> 选择
              </span>
              <span>
                <kbd>Enter</kbd> 执行
              </span>
              <span>
                <kbd>Ctrl</kbd>
                <kbd>K</kbd> 关闭
              </span>
            </>
          )}
        </div>
      </div>
    </div>
  )
}
