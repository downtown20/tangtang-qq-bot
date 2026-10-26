import { useEffect, useState } from 'react'
import { Minus, Square, Copy, X, PanelLeftOpen, Moon, Sun, Search, Lightbulb } from 'lucide-react'
import './TitleBar.css'

export type Theme = 'light' | 'dark'

export default function TitleBar({
  collapsed,
  onExpand,
  theme,
  onToggleTheme,
  onOpenSearch,
  explain,
  onToggleExplain,
}: {
  collapsed: boolean
  onExpand: () => void
  theme: Theme
  onToggleTheme: () => void
  onOpenSearch: () => void
  /** 讲解模式：开 = 显示解释文字，关 = 只留数据 */
  explain: boolean
  onToggleExplain: () => void
}) {
  const [maximized, setMaximized] = useState(false)
  const desktop = typeof window !== 'undefined' ? window.desktop : undefined

  useEffect(() => {
    if (!desktop) return
    desktop.isMaximized().then(setMaximized).catch(() => {})
    return desktop.onWindowState((s) => setMaximized(s.maximized))
  }, [desktop])

  return (
    <header className="titlebar drag-region">
      <div className="titlebar-left">
        {collapsed && (
          <button className="tb-btn no-drag" title="展开侧边栏" onClick={onExpand}>
            <PanelLeftOpen size={15} />
          </button>
        )}
      </div>
      <div className="titlebar-spacer" />
      <div className="titlebar-controls no-drag">
        <button className="tb-btn" title="搜索（Ctrl+K）" onClick={onOpenSearch}>
          <Search size={14} />
        </button>
        {/* 讲解模式：解释性文字（这站干什么/判断依据/参数解释/图例）的显隐。
            放在标题栏是因为它是**全局**偏好，不属于任何一页的设置。 */}
        <button
          className={'tb-btn tb-explain' + (explain ? ' is-on' : '')}
          title={
            explain
              ? '讲解模式：开（点一下只留数据，隐藏解释文字）'
              : '讲解模式：关（点一下显示解释文字）'
          }
          aria-pressed={explain}
          onClick={onToggleExplain}
        >
          <Lightbulb size={14} />
          讲解
        </button>
        <button
          className="tb-btn"
          title={theme === 'light' ? '切换到深色' : '切换到浅色'}
          onClick={onToggleTheme}
        >
          {theme === 'light' ? <Moon size={14} /> : <Sun size={14} />}
        </button>
        {desktop && (
          <>
            <button className="tb-btn" title="最小化" onClick={() => desktop.minimize()}>
              <Minus size={14} />
            </button>
            <button
              className="tb-btn"
              title={maximized ? '还原' : '最大化'}
              onClick={() => desktop.toggleMaximize().then(setMaximized)}
            >
              {maximized ? <Copy size={12} /> : <Square size={12} />}
            </button>
            <button className="tb-btn tb-btn-close" title="关闭" onClick={() => desktop.close()}>
              <X size={14} />
            </button>
          </>
        )}
      </div>
    </header>
  )
}

