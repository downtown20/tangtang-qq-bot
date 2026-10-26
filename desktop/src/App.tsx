import { useCallback, useEffect, useRef, useState } from 'react'
import TitleBar, { type Theme } from './components/TitleBar'
import Sidebar, { type PageKey } from './components/Sidebar'
import Composer from './components/Composer'
import CommandPalette from './components/CommandPalette'
import Dashboard from './pages/Dashboard'
import Gallery from './pages/Gallery'
import GroupManage from './pages/GroupManage'
import MemoryGraph from './pages/MemoryGraph'
import Console from './pages/Console'
import Logs from './pages/Logs'
import Assets from './pages/Assets'
import Tuning from './pages/Tuning'
import Settings from './pages/Settings'
import { api, type ConsoleSource } from './lib/api'
import { readExplainMode, writeExplainMode } from './lib/uiPrefs'
import './App.css'

/** 会话源列表刷新间隔（侧栏列表 + 标题用的都是这一份数据） */
const SRC_POLL_MS = 15000

function readTheme(): Theme {
  try {
    return (localStorage.getItem('tangtang.theme') as Theme) || 'light'
  } catch {
    return 'light'
  }
}

export default function App() {
  // 默认进会客厅：打开就能对话，不需要先点导航、再点会话
  // （2026-10-24 主人要求「把会客厅转变成一个默认显示的功能」）
  const [page, setPage] = useState<PageKey>('console')
  const [settingsOpen, setSettingsOpen] = useState(false)
  const [settingsSection, setSettingsSection] = useState<string | undefined>(undefined)
  const [collapsed, setCollapsed] = useState(false)
  const [theme, setTheme] = useState<Theme>(readTheme)
  /* 讲解模式（2026-10-25 第 45 轮）：控制解释性文字的显隐。
     默认开——新用户先看懂；老手在标题栏点一下只剩数据。
     落 localStorage，重开软件保持上次选择。 */
  const [explain, setExplain] = useState<boolean>(readExplainMode)
  const [paletteOpen, setPaletteOpen] = useState(false)

  /* ── 会客厅会话状态：**提升到 App**（2026-10-24 侧栏重构）────────────
     Sidebar 渲染会话列表、Console 渲染对话流，两边必须看同一份数据——
     否则会各拉各的、切会话不同步。这里持有唯一的一份 scope + sources，
     轮询也只跑一处。 */
  const [scope, setScope] = useState('')
  const [sources, setSources] = useState<ConsoleSource[]>([])
  const [srcLoading, setSrcLoading] = useState(false)
  const srcBusy = useRef(false)
  const sourcesRef = useRef<ConsoleSource[]>([])

  useEffect(() => {
    sourcesRef.current = sources
  }, [sources])

  /** 拉会话源；返回最新列表（调用方需要「拉完立刻选中新建的会话」）。 */
  const loadSources = useCallback(async (silent = true): Promise<ConsoleSource[]> => {
    if (srcBusy.current) return sourcesRef.current
    srcBusy.current = true
    if (!silent) setSrcLoading(true)
    try {
      const res = await api.consoleSources()
      const list = res.sources || []
      setSources(list)
      return list
    } catch {
      // 糖糖本体没跑时这里是常态——静默降级成空列表，不弹错
      return sourcesRef.current
    } finally {
      srcBusy.current = false
      if (!silent) setSrcLoading(false)
    }
  }, [])

  useEffect(() => {
    void loadSources(false)
  }, [loadSources])

  // 会话源轮询（页面不可见时跳过，切回来立刻补一次）
  useEffect(() => {
    const visible = () => document.visibilityState === 'visible'
    const tick = () => {
      if (visible()) void loadSources(true)
    }
    const id = window.setInterval(tick, SRC_POLL_MS)
    const onVis = () => {
      if (visible()) void loadSources(true)
    }
    document.addEventListener('visibilitychange', onVis)
    return () => {
      window.clearInterval(id)
      document.removeEventListener('visibilitychange', onVis)
    }
  }, [loadSources])

  /** 默认选中：优先「和你」的私聊（主场景、且有回合痕迹），其次任一私聊。 */
  useEffect(() => {
    if (!sources.length) return
    if (scope && sources.some((s) => s.key === scope)) return
    const self = sources.find((s) => s.kind === 'private' && s.title.includes('（和你）'))
    const priv = sources.find((s) => s.kind === 'private')
    setScope((self || priv || sources[0]).key)
  }, [sources, scope])

  /* ── 搜索命中定位（2026-10-24）────────────────────────────────
     侧栏搜的是消息正文，点一条命中要：切到那个会话 + 滚到那条消息 + 高亮一下。
     状态住这里，因为「切会话」和「滚到消息」分属侧栏和主区。 */
  const [focusId, setFocusId] = useState<number | null>(null)

  const focusHit = useCallback((scopeKey: string, msgId: number) => {
    setScope(scopeKey)
    setFocusId(msgId)
    setPage('console')
  }, [])

  useEffect(() => {
    document.documentElement.dataset.theme = theme
    try {
      localStorage.setItem('tangtang.theme', theme)
    } catch {
      /* 隐私模式忽略 */
    }
  }, [theme])

  /* 讲解模式落到 <html data-explain>：全局 CSS 据此隐藏 `.explain-only`。
     走 CSS 而不是逐组件传 prop——解释区块散在十几个组件里，
     传 prop 要改十几条链路，漏一处就是「关了还漏一条」。 */
  useEffect(() => {
    document.documentElement.dataset.explain = explain ? 'on' : 'off'
    writeExplainMode(explain)
  }, [explain])

  // Ctrl+K / ⌘K：DSH 的全局搜索面板
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'k') {
        e.preventDefault()
        setPaletteOpen((v) => !v)
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [])

  const toggleTheme = () => setTheme((t) => (t === 'light' ? 'dark' : 'light'))

  const openSettings = (section?: string) => {
    setSettingsSection(section)
    setSettingsOpen(true)
  }

  return (
    <div className="app-shell">
      <Sidebar
        current={page}
        onNavigate={setPage}
        onOpenSettings={() => openSettings()}
        onCollapse={() => setCollapsed(true)}
        collapsed={collapsed}
        scope={scope}
        onSelectScope={setScope}
        sources={sources}
        onRefreshSources={loadSources}
        srcLoading={srcLoading}
        onFocusHit={focusHit}
      />
      <main className="app-main">
        <TitleBar
          collapsed={collapsed}
          onExpand={() => setCollapsed(false)}
          theme={theme}
          onToggleTheme={toggleTheme}
          onOpenSearch={() => setPaletteOpen(true)}
          explain={explain}
          onToggleExplain={() => setExplain((v) => !v)}
        />
        <div className="app-content">
          {page === 'dashboard' && (
            <Dashboard
              onOpenSettings={() => openSettings()}
              onOpenMemory={() => setPage('graph')}
            />
          )}
          {page === 'console' && (
            <Console
              scope={scope}
              sources={sources}
              onScopeChange={setScope}
              onGoDashboard={() => setPage('dashboard')}
              onOpenMemory={() => setPage('graph')}
              focusId={focusId}
              onFocusDone={() => setFocusId(null)}
            />
          )}
          {page === 'gallery' && (
            <Gallery onOpenSettings={(section) => openSettings(section)} />
          )}
          {page === 'groups' && <GroupManage />}
          {page === 'graph' && <MemoryGraph />}
          {page === 'assets' && <Assets />}
          {page === 'tuning' && <Tuning />}
          {page === 'logs' && <Logs />}
        </div>
        {page === 'dashboard' && <Composer onOpenSettings={() => openSettings()} />}
      </main>

      {settingsOpen && (
        <Settings onClose={() => setSettingsOpen(false)} initialSection={settingsSection} />
      )}

      <CommandPalette
        open={paletteOpen}
        onClose={() => setPaletteOpen(false)}
        onNavigate={setPage}
        onOpenSettings={openSettings}
        onToggleTheme={toggleTheme}
        theme={theme}
      />
    </div>
  )
}
