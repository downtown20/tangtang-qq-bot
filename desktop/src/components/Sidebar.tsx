import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react'
import {
  LayoutDashboard,
  ScrollText,
  Images,
  Users,
  Network,
  Play,
  Square,
  Loader2,
  Search,
  RefreshCw,
  Plus,
  PanelLeft,
  X,
  Settings2,
  Pin,
  PinOff,
  Trash2,
  Copy,
  PenLine,
  SlidersHorizontal,
} from 'lucide-react'
import {
  api,
  type BotStatus,
  type ConsoleSearchHit,
  type ConsoleSearchResult,
  type ConsoleSource,
  type ConsoleSourceKind,
} from '../lib/api'
import { hasUnread, loadRead, saveRead, type ReadMap } from '../lib/consoleRead'
import { useBotControl } from '../lib/useBotControl'
import ContextMenu, { type MenuItem, type MenuState } from './ContextMenu'
import './Sidebar.css'

export type PageKey = 'dashboard' | 'console' | 'gallery' | 'logs' | 'groups' | 'graph' | 'assets' | 'tuning'

/** 相对时间：DSH 工作区列表那种「刚刚 / 12分钟 / 3小时 / 15天」 */
function fmtRel(raw: number): string {
  if (!Number.isFinite(raw) || raw <= 0) return ''
  const ms = raw > 1e11 ? raw : raw * 1000
  const diff = (Date.now() - ms) / 1000
  if (diff < 60) return '刚刚'
  if (diff < 3600) return `${Math.floor(diff / 60)}分钟`
  if (diff < 86400) return `${Math.floor(diff / 3600)}小时`
  if (diff < 86400 * 30) return `${Math.floor(diff / 86400)}天`
  const d = new Date(ms)
  return `${d.getMonth() + 1}-${d.getDate()}`
}

const KIND_TAG: Record<ConsoleSourceKind, string> = {
  desktop: '本地',
  private: '私聊',
  group: '群',
  monologue: '独白',
}

/* ── 置顶会话（localStorage）──────────────────────────────────────
   只存 key 的数组；解析失败一律当空数组——一条脏数据不该让侧栏崩掉。
   置顶只是「同组内排前面」，后端给的 updated_at 相对顺序保持不变。 */
const PIN_KEY = 'tangtang.pinnedSessions'

function loadPinned(): Set<string> {
  try {
    const raw = localStorage.getItem(PIN_KEY)
    const parsed = raw ? JSON.parse(raw) : null
    if (!Array.isArray(parsed)) return new Set()
    return new Set(parsed.filter((v): v is string => typeof v === 'string'))
  } catch {
    return new Set()
  }
}

function savePinned(keys: Set<string>) {
  try {
    localStorage.setItem(PIN_KEY, JSON.stringify([...keys]))
  } catch {
    /* 隐私模式忽略 */
  }
}

/** 命中片段高亮：把 [match_start, match_start + match_len) 包成 <mark>。
    后端下标理论上不会越界，但这里仍然夹紧——越界只该少高亮几个字，不该白屏。 */
function HitSnippet({ hit }: { hit: ConsoleSearchHit }) {
  const text = hit.snippet || ''
  const start = Math.max(0, Math.min(Math.trunc(hit.match_start) || 0, text.length))
  const len = Math.max(0, Math.min(Math.trunc(hit.match_len) || 0, text.length - start))
  const head = text.slice(0, start)
  const marked = text.slice(start, start + len)
  const tail = text.slice(start + len)
  return (
    <>
      {head}
      {marked && <mark className="sb-hit-mark">{marked}</mark>}
      {tail}
    </>
  )
}

// 会客厅不在这里——它是默认主界面（打开应用即是），导航里再放一个
// 「点一下才进对话」的入口就违背了「打开就能聊」。点左上角品牌名可回到对话。
const NAV: { key: PageKey; label: string; icon: typeof LayoutDashboard }[] = [
  { key: 'dashboard', label: '概览', icon: LayoutDashboard },
  { key: 'groups', label: '群管理', icon: Users },
  { key: 'graph', label: '记忆', icon: Network },
  // 2026-10-26：人格卡 / 协议条文 / 场景 / 知识库的统一编辑器
  { key: 'assets', label: '人格规则', icon: PenLine },
  // 2026-10-26：代码常量开放（当前值/默认值/作用/文件:行 + 范围校验）
  { key: 'tuning', label: '高级调参', icon: SlidersHorizontal },
  { key: 'gallery', label: '图库', icon: Images },
  { key: 'logs', label: '日志', icon: ScrollText },
]

export default function Sidebar({
  current,
  onNavigate,
  onOpenSettings,
  onCollapse,
  collapsed,
  scope,
  onSelectScope,
  sources,
  onRefreshSources,
  srcLoading,
  onFocusHit,
}: {
  current: PageKey
  onNavigate: (k: PageKey) => void
  onOpenSettings: () => void
  onCollapse: () => void
  collapsed: boolean
  /** 当前选中的会话 key（`desktop:<id>` / `private:<qq>` / `group:<gid>` / `monologue:…`） */
  scope: string
  onSelectScope: (key: string) => void
  /** 会话源列表——状态住在 App，这里只渲染（保证只有一份轮询） */
  sources: ConsoleSource[]
  onRefreshSources: (silent?: boolean) => Promise<ConsoleSource[]>
  srcLoading: boolean
  /** 点了搜索命中：切到那个会话，并让会客厅滚到那条消息 */
  onFocusHit: (scope: string, msgId: number) => void
}) {
  const [status, setStatus] = useState<BotStatus | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [searchOpen, setSearchOpen] = useState(false)
  const [filter, setFilter] = useState('')
  const [creating, setCreating] = useState(false)
  const [read, setRead] = useState<ReadMap>(loadRead)
  const [pinned, setPinned] = useState<Set<string>>(loadPinned)
  const [menu, setMenu] = useState<MenuState>(null)

  /* 搜索面板的状态机：searching → hits / 空结果 / error。
     seq 是「请求序号」：快速输入时旧请求的响应会被丢弃，否则结果错位。
     防抖 250ms —— 本地过滤仍然是即时的，不受这个延迟影响。 */
  const [searching, setSearching] = useState(false)
  const [searchRes, setSearchRes] = useState<ConsoleSearchResult | null>(null)
  const [searchErr, setSearchErr] = useState<string | null>(null)
  const searchSeq = useRef(0)

  const refreshStatus = () => {
    api
      .status()
      .then(setStatus)
      .catch((e: Error) => setError(e.message))
  }

  useEffect(() => {
    refreshStatus()
    const timer = setInterval(refreshStatus, 15000)
    return () => clearInterval(timer)
  }, [])

  // 启停交给共用 hook（2026-10-27）：概览页和这里的按钮从此是**同一个实现**，
  // 且会等状态真的翻转才解除 busy —— 原来这里只做了一次不 await 的 refreshStatus，
  // `python main.py` 起来要好几秒，UI 立刻回到「启动糖糖」，看着就是没生效。
  const bot = useBotControl()
  const running = bot.running

  const toggle = async () => {
    setError(null)
    await bot.toggle()
    if (bot.error) setError(bot.error)
    // 起停之后会话源一定变了（起来才有数据），顺手刷一下
    void onRefreshSources(true)
  }

  /* 已读位置跟随当前会话：正看着的会话不该给自己留未读点。
     单独跟随 sources 而不是跟随切会话，是因为轮询会持续带来新的 last_id。 */
  useEffect(() => {
    const src = sources.find((s) => s.key === scope)
    if (!src || !src.last_id) return
    setRead((prev) => {
      if ((prev[src.key] || 0) >= src.last_id) return prev
      const next = { ...prev, [src.key]: src.last_id }
      saveRead(next)
      return next
    })
  }, [scope, sources])

  /* 搜消息正文（不是搜会话标题）：输入 ≥1 个字符后防抖 250ms 请求。
     清理时既取消定时器、又靠序号丢弃在途响应。 */
  const q = filter.trim()
  const searchActive = searchOpen && q.length > 0

  useEffect(() => {
    const query = filter.trim()
    if (!searchOpen || query.length < 1) {
      searchSeq.current += 1 // 作废在途请求
      setSearching(false)
      setSearchRes(null)
      setSearchErr(null)
      return
    }
    const seq = ++searchSeq.current
    setSearching(true)
    const timer = window.setTimeout(() => {
      api
        .consoleSearch(query, 40)
        .then((res) => {
          if (seq !== searchSeq.current) return
          setSearchRes(res)
          setSearchErr(null)
        })
        .catch((e: Error) => {
          if (seq !== searchSeq.current) return
          setSearchErr(e.message)
          setSearchRes(null)
        })
        .finally(() => {
          if (seq !== searchSeq.current) return
          setSearching(false)
        })
    }, 250)
    return () => window.clearTimeout(timer)
  }, [filter, searchOpen])

  const createLocal = async () => {
    if (creating) return
    setCreating(true)
    setError(null)
    try {
      const created = await api.consoleCreateSession()
      const list = await onRefreshSources(false)
      const hit =
        list.find((s) => s.key === `desktop:${created.id}`) ||
        list.find((s) => s.kind === 'desktop')
      onSelectScope(hit ? hit.key : `desktop:${created.id}`)
      onNavigate('console')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setCreating(false)
    }
  }

  const togglePin = (key: string) => {
    setPinned((prev) => {
      const next = new Set(prev)
      next.has(key) ? next.delete(key) : next.add(key)
      savePinned(next)
      return next
    })
  }

  /* 删除本地会话。QQ 镜像（私聊/群聊/独白）不提供删除——那些是真实数据的
     镜像，删了数据库里还在，删了只会让人以为清理干净了。 */
  const deleteLocal = async (s: ConsoleSource) => {
    if (!window.confirm('删除这个本地会话？删掉之后里面的对话就没了。')) return
    setError(null)
    try {
      const id = s.key.startsWith('desktop:') ? s.key.slice('desktop:'.length) : s.key
      await api.consoleDeleteSession(id)
      // 置顶集合跟着清掉，别留下指向已删会话的死键
      if (pinned.has(s.key)) {
        const next = new Set(pinned)
        next.delete(s.key)
        savePinned(next)
        setPinned(next)
      }
      const list = await onRefreshSources(false)
      if (scope === s.key) {
        const first = list.find((x) => x.key !== s.key)
        onSelectScope(first ? first.key : '')
      }
    } catch (e) {
      setError((e as Error).message)
    }
  }

  const copyScopeKey = (key: string) => {
    try {
      void navigator.clipboard.writeText(key).catch((err: Error) => {
        setError(`复制失败：${err.message}`)
      })
    } catch {
      setError('复制失败：剪贴板不可用')
    }
  }

  const openItemMenu = (e: ReactMouseEvent, s: ConsoleSource) => {
    e.preventDefault()
    e.stopPropagation()
    if (s.kind === 'desktop') {
      const isPinned = pinned.has(s.key)
      const items: MenuItem[] = [
        {
          key: 'pin',
          label: isPinned ? '取消置顶' : '置顶',
          icon: isPinned ? PinOff : Pin,
          onSelect: () => togglePin(s.key),
        },
        {
          key: 'delete',
          label: '删除会话',
          icon: Trash2,
          danger: true,
          separator: true,
          onSelect: () => void deleteLocal(s),
        },
      ]
      setMenu({ x: e.clientX, y: e.clientY, items })
      return
    }
    setMenu({
      x: e.clientX,
      y: e.clientY,
      items: [
        {
          key: 'copy',
          label: '复制会话标识',
          icon: Copy,
          onSelect: () => copyScopeKey(s.key),
        },
      ],
    })
  }

  const groups = useMemo(() => {
    const needle = filter.trim().toLowerCase()
    /* 组内排序：置顶项置前，各自保持 sources 里的相对顺序（后端按 updated_at 排好）。
       用两次 filter 而不是 sort，避免 sort 比较函数把顺序变成依赖 locale 的猜测。 */
    const pick = (k: ConsoleSourceKind) => {
      const items = sources.filter(
        (s) =>
          s.kind === k &&
          (!needle || `${s.title} ${s.subtitle}`.toLowerCase().includes(needle)),
      )
      return [
        ...items.filter((s) => pinned.has(s.key)),
        ...items.filter((s) => !pinned.has(s.key)),
      ]
    }
    return [
      { kind: 'desktop' as const, label: '本地', items: pick('desktop') },
      { kind: 'private' as const, label: '私聊', items: pick('private') },
      { kind: 'group' as const, label: '群聊', items: pick('group') },
      // 独白是只读频道（她没发出去的内在活动），最后单独一组
      { kind: 'monologue' as const, label: '独白', items: pick('monologue') },
    ]
  }, [sources, filter, pinned])

  const totalShown = groups.reduce((n, g) => n + g.items.length, 0)

  /* 会话列表（本地过滤 = 搜索第一层：即时、不走网络）。
     网络搜索未就绪（防抖中 / 失败）时就显示这一层，命中列表就绪后才替换它——
     否则那层本地过滤会变成「写了但从不生效」。 */
  const sessionGroups = (
    <>
      {groups.map((g) => (
        <div className="sb-group" key={g.kind}>
          <div className="sb-group-head">
            <span>{g.label}</span>
            <span className="sb-group-n">{g.items.length}</span>
          </div>

          {/* 只有本地会话能新建；独白是只读频道 */}
          {g.kind === 'desktop' && (
            <button className="sb-new" onClick={() => void createLocal()} disabled={creating}>
              {creating ? <Loader2 size={12} className="spin" /> : <Plus size={12} />}
              新建本地会话
            </button>
          )}

          {g.items.map((s) => {
            const unread = hasUnread(s.kind, s.key, s.last_id, read)
            const isPinned = pinned.has(s.key)
            return (
              <button
                key={s.key}
                className={'sb-item' + (s.key === scope ? ' is-active' : '')}
                onClick={() => {
                  onSelectScope(s.key)
                  onNavigate('console')
                }}
                onContextMenu={(e) => openItemMenu(e, s)}
                title={`${s.title}${s.subtitle ? '\n' + s.subtitle : ''}\n${s.message_count} 条消息`}
              >
                <span className="sb-item-top">
                  <span className="sb-kind">{KIND_TAG[s.kind]}</span>
                  {isPinned && <Pin size={10} className="sb-pin" />}
                  <span className="sb-title">{s.title || '未命名'}</span>
                  <span className="sb-time">{fmtRel(s.updated_at)}</span>
                </span>
                <span className="sb-item-sub">
                  <span className="sb-sub">{s.subtitle || '（还没有消息）'}</span>
                  {unread && <span className="sb-unread" title="有新消息" />}
                </span>
              </button>
            )
          })}
        </div>
      ))}

      {totalShown === 0 && (
        <div className="sidebar-empty">
          {filter
            ? '没有匹配的会话'
            : sources.length === 0
              ? '糖糖本体没启动时，这里还没有会话'
              : '还没有会话记录'}
        </div>
      )}
    </>
  )

  return (
    <aside className={'sidebar' + (collapsed ? ' is-collapsed' : '')}>
      <div className="sidebar-head">
        <div
          className="sidebar-brand"
          onClick={() => onNavigate('console')}
          role="button"
          tabIndex={0}
          title="回到对话"
          onKeyDown={(e) => {
            if (e.key === 'Enter' || e.key === ' ') onNavigate('console')
          }}
        >
          <span className="brand-mark">🍬</span>
          <span className="brand-text">小糖糖</span>
          <span className="brand-tag">CONSOLE</span>
        </div>
        <button className="sidebar-icon-btn" onClick={onCollapse} title="收起侧边栏">
          <PanelLeft size={16} />
        </button>
      </div>

      <button
        className={'sidebar-primary' + (running ? ' is-running' : '')}
        onClick={() => void toggle()}
        disabled={bot.busy}
        title={bot.error || bot.pending || ''}
      >
        {bot.busy ? (
          <Loader2 size={15} className="spin" />
        ) : running ? (
          <Square size={13} />
        ) : (
          <Play size={15} />
        )}
        <span>{running ? '停止糖糖' : '启动糖糖'}</span>
      </button>
      {(bot.error || bot.pending) && (
        <div className="sidebar-bot-note" role="status">
          {bot.error || bot.pending}
        </div>
      )}

      <nav className="sidebar-nav">
        {NAV.map(({ key, label, icon: Icon }) => (
          <button
            key={key}
            className={'nav-item' + (current === key ? ' is-active' : '')}
            onClick={() => onNavigate(key)}
          >
            <Icon size={16} />
            <span>{label}</span>
          </button>
        ))}
        <button className="nav-item" onClick={onOpenSettings}>
          <Settings2 size={16} />
          <span>糖糖的设置</span>
        </button>
      </nav>

      <div className="sidebar-section">
        <div className="section-head">
          <span className="section-title">会话</span>
          <div className="section-actions">
            <button
              className={'sidebar-icon-btn' + (searchOpen ? ' is-active' : '')}
              title="搜索消息内容"
              onClick={() => {
                setSearchOpen((v) => !v)
                setFilter('')
              }}
            >
              <Search size={14} />
            </button>
            <button
              className="sidebar-icon-btn"
              title="刷新会话列表"
              onClick={() => void onRefreshSources(false)}
              disabled={srcLoading}
            >
              <RefreshCw size={14} className={srcLoading ? 'spin' : ''} />
            </button>
          </div>
        </div>

        {searchOpen && (
          <div className="sidebar-search">
            <Search size={13} />
            <input
              autoFocus
              value={filter}
              placeholder="搜索消息内容…"
              onChange={(e) => setFilter(e.target.value)}
            />
            {filter && (
              <button className="sidebar-icon-btn" onClick={() => setFilter('')} title="清空">
                <X size={12} />
              </button>
            )}
          </div>
        )}

        {searchActive ? (
          /* 命中列表：搜的是消息正文，点一条就跳到那条消息 */
          <div className="sidebar-list">
            {searching && (
              <div className="sb-search-state sb-search-inline">
                <Loader2 size={13} className="spin" />
                搜索中…
              </div>
            )}

            {!searching && searchErr && (
              <div className="sidebar-error">搜索失败：{searchErr}</div>
            )}

            {!searching && !searchErr && searchRes && searchRes.hits.length === 0 && (
              <div className="sb-search-state">没搜到「{q}」</div>
            )}

            {!searching && !searchErr && searchRes && searchRes.hits.length > 0 && (
              <>
                {searchRes.hits.map((hit) => (
                  <button
                    key={`${hit.scope}:${hit.id}`}
                    className="sb-hit"
                    title={hit.snippet}
                    onClick={() => {
                      onFocusHit(hit.scope, hit.id)
                      setFilter('')
                      setSearchOpen(false)
                    }}
                  >
                    <span className="sb-hit-top">
                      <span className="sb-kind">{KIND_TAG[hit.kind]}</span>
                      <span className="sb-hit-title">{hit.title || '未命名'}</span>
                      <span className="sb-time">{fmtRel(hit.ts)}</span>
                    </span>
                    <span className="sb-hit-sub">
                      {hit.role === 'tangtang' && <span className="sb-hit-who">糖糖：</span>}
                      <span className="sb-hit-snippet">
                        <HitSnippet hit={hit} />
                      </span>
                    </span>
                  </button>
                ))}
                {searchRes.truncated && (
                  <div className="sb-search-more">
                    共 {searchRes.total} 条，只显示前 {searchRes.hits.length} 条
                  </div>
                )}
              </>
            )}

            {/* 网络搜索未就绪（防抖中 / 失败）时，本地过滤结果即时顶上 */}
            {!(searchRes && !searching && !searchErr) && sessionGroups}
          </div>
        ) : (
          <div className="sidebar-list">{sessionGroups}</div>
        )}
      </div>

      {error && <div className="sidebar-error">{error}</div>}

      <div className="sidebar-foot">
        <button className="foot-row" onClick={onOpenSettings}>
          <span className={'status-dot ' + (running ? 'online' : 'offline')} />
          <span>{running ? '在线' : '离线'}</span>
          <span className="foot-model">{status?.llm.model ?? '—'}</span>
        </button>
      </div>

      <ContextMenu state={menu} onClose={() => setMenu(null)} />
    </aside>
  )
}
