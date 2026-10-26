import { useCallback, useEffect, useRef, useState } from 'react'
import {
  AlertCircle,
  Brain,
  ChevronRight,
  ImageOff,
  Loader2,
  Lock,
  MessagesSquare,
  PanelRight,
  PanelRightClose,
  RefreshCw,
  Sparkles,
  Wrench,
  X,
} from 'lucide-react'
import {
  api,
  consoleMediaUrl,
  getBridge,
  type BridgeInfo,
  type ConsoleFeedMessage,
  type ConsoleMediaItem,
  type ConsoleSnapshot,
  type ConsoleSource,
  type ConsoleSourceKind,
  type ConsoleStatus,
  type ConsoleReplay,
  type ConsoleTrace,
  type ConsoleTraceSummary,
} from '../lib/api'
import StatusPanel from '../components/StatusPanel'
import TurnReplay from '../components/TurnReplay'
import './Page.css'
import './Console.css'

/* ═══════════════════════════════════════════════════════════════
   会客厅 v3 —— 对话主界面（2026-10-24 侧栏重构）

   v2 → v3 的变化：
   · **左栏搬到了应用侧栏**（Sidebar）：会话源列表 + 未读 + 搜索
   · 本组件只剩「中栏（消息流 + 痕迹 + 输入）」和「右栏（糖糖状态）」
   · `scope` / `sources` 由 App 持有并通过 props 传入 —— 侧栏和这里看同一份，
     轮询也只有一份在跑（App 拉 sources，这里只拉 feed + snapshot）

   后端契约见 tools/desktop_bridge.py + agent/console_api.py。
   ═══════════════════════════════════════════════════════════════ */

/** 当前会话增量轮询间隔（会话源列表轮询已上提到 App） */
const FEED_POLL_MS = 3000
/** 右栏状态面板刷新间隔 */
const SNAP_POLL_MS = 10000
/** 首次进入一次拉多少条 */
const FEED_LIMIT = 100

const PLACEHOLDER: Record<ConsoleSourceKind, string> = {
  desktop: '对糖糖说…（只有你们两个人看见）',
  private: '说点什么…（Enter 发送）',
  group: '在这个群说一句…',
  // 独白频道不渲染输入框，这里只是撑住 Record 的完整性
  monologue: '独白是只读的',
}

const KIND_TAG: Record<ConsoleSourceKind, string> = {
  desktop: '本地',
  private: '私聊',
  group: '群',
  monologue: '独白',
}

/* ═══════════════════════════════════════════════════════════════
   小工具：时间 / 数值 / 未读存储
   ═══════════════════════════════════════════════════════════════ */

/** 后端时间戳可能是秒也可能是毫秒，统一成毫秒 */
function toMs(raw: number): number {
  if (!Number.isFinite(raw) || raw <= 0) return 0
  return raw > 1e11 ? raw : raw * 1000
}

/** 消息时间：今天只显示 时:分，跨天补日期 */
function fmtClock(raw: number): string {
  const ms = toMs(raw)
  if (!ms) return ''
  const d = new Date(ms)
  const now = new Date()
  const hhmm = `${String(d.getHours()).padStart(2, '0')}:${String(d.getMinutes()).padStart(2, '0')}`
  if (d.toDateString() === now.toDateString()) return hhmm
  const md = `${d.getMonth() + 1}-${d.getDate()}`
  return d.getFullYear() === now.getFullYear() ? `${md} ${hhmm}` : `${d.getFullYear()}-${md} ${hhmm}`
}

/** 秒 → 「20.4s」，太小的显示毫秒 */
function fmtDur(ms: number): string {
  if (!Number.isFinite(ms) || ms <= 0) return '—'
  return ms >= 1000 ? `${(ms / 1000).toFixed(1)}s` : `${Math.round(ms)}ms`
}

/** 截断文本（tool 参数/结果动辄上千字，卡片里只给个引子） */
function clip(text: string, n: number): string {
  const flat = String(text ?? '').replace(/\s+/g, ' ').trim()
  return flat.length > n ? flat.slice(0, n) + '…' : flat
}

/* ═══════════════════════════════════════════════════════════════
   回合痕迹卡片：默认折叠，点开才拉详情并渲染
   ═══════════════════════════════════════════════════════════════ */

function TraceCard({
  summary,
  open,
  busy,
  failed,
  trace,
  onToggle,
  onReplay,
}: {
  summary: ConsoleTraceSummary
  open: boolean
  busy: boolean
  failed: boolean
  trace: ConsoleTrace | null
  onToggle: () => void
  /** 打开完整回合回放（还带「决定了什么」和「对方怎么反应」） */
  onReplay: () => void
}) {
  const isFailed = summary.status === 'failed'
  const head = [
    isFailed ? '这一轮失败了' : '她怎么想的',
    summary.tool_count > 0 ? `${summary.tool_count} 步工具` : '',
    fmtDur(summary.duration_ms),
  ].filter(Boolean)

  return (
    <div className={'cs-trace' + (isFailed ? ' is-failed' : '') + (open ? ' is-open' : '')}>
      <button className="cs-trace-head" onClick={onToggle} title="展开/收起这一轮的思维链与工具调用">
        <ChevronRight size={12} className={'cs-trace-caret' + (open ? ' is-open' : '')} />
        <span className="cs-trace-title">{head.join(' · ')}</span>
        {isFailed && <span className="cs-trace-badge">失败</span>}
      </button>

      {open && (
        <div className="cs-trace-body">
          {busy && (
            <div className="cs-trace-loading">
              <Loader2 size={12} className="spin" />
              正在读取痕迹…
            </div>
          )}

          {!busy && failed && <div className="cs-trace-empty">痕迹读不出来</div>}

          {!busy && !failed && !trace && <div className="cs-trace-empty">这一轮没有留下痕迹</div>}

          {!busy && trace && (
            <>
              <div className="cs-trace-block">
                <div className="cs-trace-label">
                  <Brain size={11} />
                  思维链
                  {trace.reasoning ? ` · ${trace.reasoning.length} 字` : ''}
                </div>
                {trace.reasoning ? (
                  <pre className="cs-reasoning">{trace.reasoning}</pre>
                ) : (
                  <div className="cs-trace-empty">这一轮模型没有输出思考内容</div>
                )}
              </div>

              <div className="cs-trace-block">
                <div className="cs-trace-label">
                  <Wrench size={11} />
                  工具调用
                  {trace.tool_steps.length > 0 ? ` · ${trace.tool_steps.length} 步` : ''}
                </div>
                {trace.tool_steps.length === 0 ? (
                  <div className="cs-trace-empty">这一轮没有调用工具</div>
                ) : (
                  <div className="cs-steps">
                    {trace.tool_steps.map((s, i) => (
                      <div className="cs-step" key={i}>
                        <div className="cs-step-line">
                          <code className="cs-step-name" title={s.args}>
                            {s.name}({clip(s.args, 60)})
                          </code>
                          <span className="cs-step-ms">{fmtDur(s.ms)}</span>
                        </div>
                        <div className="cs-step-result" title={s.result}>
                          {clip(s.result, 160) || '（空结果）'}
                        </div>
                      </div>
                    ))}
                  </div>
                )}
              </div>

              <div className="cs-trace-meta">
                {[trace.model, fmtDur(trace.duration_ms), trace.prompt_chars > 0 ? `${trace.prompt_chars} 字上下文` : '']
                  .filter(Boolean)
                  .join(' · ')}
              </div>

              <button
                className="cs-trace-more"
                onClick={onReplay}
                title="看她这一回合的完整过程：思维链、工具链、决定、对方反应"
              >
                <PanelRight size={12} />
                完整回合
              </button>
            </>
          )}
        </div>
      )}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   媒体附件（图片 / 语音）

   QQ 的图片/语音 URL 带 rkey，几个小时后就 404 —— 所以 <img>/<audio>
   的 onError 一律降级成一行「媒体已过期」，不在界面上留裂图和破播放器。
   URL 必须由 bridge（host + token）拼出来；bridge 没拿到之前不渲染，
   绝不用假 URL 占位。
   ═══════════════════════════════════════════════════════════════ */

/** 语音时长：7 秒 → 「0:07」；后端没给时长就显示「语音」 */
function fmtMediaDur(sec: number): string {
  if (!Number.isFinite(sec) || sec <= 0) return '语音'
  const s = Math.round(sec)
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`
}

/** 单个媒体项：图片可点开大图，语音给浏览器原生播放器 */
function MediaItemView({
  item,
  bridge,
  onPreview,
}: {
  item: ConsoleMediaItem
  bridge: BridgeInfo
  onPreview: (url: string, name: string) => void
}) {
  const [failed, setFailed] = useState(false)
  const url = consoleMediaUrl(bridge, item.path)

  /* `cached === false`：后端已经确认本地没有这份字节，而 QQ 的临时链接
     几小时就失效——**不要发请求**，直接给占位。否则打开一个带几十张图的
     群会话会当场打出几十个注定 410 的请求（2026-10-24 实测：51 张只有 1 张
     能显示，其余全在空转）。 */
  if (failed || item.cached === false) {
    return (
      <div
        className="cs-media-dead"
        title={`${item.name}——QQ 的临时链接已过期，本地也没有缓存`}
      >
        <ImageOff size={11} />
        {item.type === 'audio' ? '语音已过期' : '图片已过期'}
      </div>
    )
  }

  if (item.type === 'audio') {
    return (
      <div className="cs-media-audio">
        <audio controls preload="none" src={url} onError={() => setFailed(true)} />
        <span className="cs-media-dur">{fmtMediaDur(item.duration)}</span>
      </div>
    )
  }

  return (
    <button
      type="button"
      className="cs-media-thumb"
      title={`${item.name}（点击放大）`}
      onClick={() => onPreview(url, item.name)}
    >
      <img
        className="cs-media-img"
        src={url}
        alt={item.name}
        loading="lazy"
        onError={() => setFailed(true)}
      />
    </button>
  )
}

/** 一条消息的媒体区：bridge 未就绪时整体不渲染 */
function MediaBlock({
  items,
  bridge,
  onPreview,
}: {
  items: ConsoleMediaItem[]
  bridge: BridgeInfo | null
  onPreview: (url: string, name: string) => void
}) {
  if (!bridge || items.length === 0) return null
  return (
    <div className="cs-media">
      {items.map((it, i) => (
        <MediaItemView
          key={`${it.type}-${it.name}-${i}`}
          item={it}
          bridge={bridge}
          onPreview={onPreview}
        />
      ))}
    </div>
  )
}

/** 图片放大预览：点背景 / 右上角按钮 / Esc 都能关掉 */
function Lightbox({ url, name, onClose }: { url: string; name: string; onClose: () => void }) {
  const [failed, setFailed] = useState(false)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  return (
    <div className="cs-lightbox" role="dialog" aria-modal="true" onClick={onClose}>
      <button
        type="button"
        className="cs-lightbox-x"
        title="关闭（Esc）"
        onClick={(e) => {
          e.stopPropagation()
          onClose()
        }}
      >
        <X size={16} />
      </button>

      {failed ? (
        <div className="cs-lightbox-dead" onClick={(e) => e.stopPropagation()}>
          <ImageOff size={18} />
          媒体已过期（QQ 的图片链接只有几小时有效期）
        </div>
      ) : (
        <img
          className="cs-lightbox-img"
          src={url}
          alt={name}
          onClick={(e) => e.stopPropagation()}
          onError={() => setFailed(true)}
        />
      )}

      <div className="cs-lightbox-name" onClick={(e) => e.stopPropagation()}>
        {name}
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   主组件
   ═══════════════════════════════════════════════════════════════ */

export default function Console({
  scope,
  sources,
  onScopeChange,
  onGoDashboard,
  onOpenMemory,
  focusId,
  onFocusDone,
}: {
  /** 当前选中的会话 key —— 由 App 持有（侧栏和这里共用同一份） */
  scope: string
  /** 会话源列表 —— 同上，侧栏和标题都从这里取 */
  sources: ConsoleSource[]
  /** 切换会话（新建本地会话后由 App/Sidebar 调用） */
  onScopeChange: (key: string) => void
  onGoDashboard?: () => void
  /** 去记忆图谱（状态面板「记忆」区块的入口——真实路由，不造假按钮） */
  onOpenMemory?: () => void
  /** 搜索命中的消息 id：滚过去 + 高亮一下；null = 没有待定位的命中 */
  focusId: number | null
  /** 定位完成（或放弃）后回调，App 会把 focusId 置回 null */
  onFocusDone: () => void
}) {
  const [status, setStatus] = useState<ConsoleStatus | null>(null)
  const [messages, setMessages] = useState<ConsoleFeedMessage[]>([])
  // 当前 messages 属于哪个会话（搜索定位要等消息流切到目标会话再滚）
  const [feedScope, setFeedScope] = useState('')
  const [composer, setComposer] = useState('')
  const [confirming, setConfirming] = useState(false)
  const [sending, setSending] = useState(false)
  const [snapshot, setSnapshot] = useState<ConsoleSnapshot | null>(null)
  const [panelOpen, setPanelOpen] = useState(true)
  const [err, setErr] = useState<string | null>(null)
  const [toast, setToast] = useState('')
  const [feedLoading, setFeedLoading] = useState(false)
  const [snapLoading, setSnapLoading] = useState(false)

  // 媒体（图片/语音）要靠 bridge 拼 host+token；拿不到就整个媒体区不渲染
  const [bridge, setBridge] = useState<BridgeInfo | null>(null)
  // 图片放大预览层；null = 关着
  const [preview, setPreview] = useState<{ url: string; name: string } | null>(null)
  // 搜索命中高亮的消息 id（1.6 秒后清掉）
  const [highlightId, setHighlightId] = useState<number | null>(null)

  // 痕迹：按 run_id（或 event_key）缓存，同一轮只拉一次
  const [expanded, setExpanded] = useState<Record<string, boolean>>({})
  const [traces, setTraces] = useState<Record<string, ConsoleTrace | null>>({})
  const [traceBusy, setTraceBusy] = useState<Record<string, boolean>>({})
  const [traceErr, setTraceErr] = useState<Record<string, boolean>>({})
  // 回合回放抽屉：一次只看一回合，所以不做按 run_id 的字典
  const [replayOpen, setReplayOpen] = useState(false)
  const [replayLoading, setReplayLoading] = useState(false)
  const [replayError, setReplayError] = useState('')
  const [replayData, setReplayData] = useState<ConsoleReplay | null>(null)

  const scrollRef = useRef<HTMLDivElement>(null)
  const stickRef = useRef(true)
  const scopeRef = useRef('')
  const messagesRef = useRef<ConsoleFeedMessage[]>([])
  const composerRef = useRef<HTMLTextAreaElement>(null)

  // 静默重入锁：轮询和手动刷新并发时丢弃后到的那次
  const feedBusy = useRef(false)
  const snapBusy = useRef(false)

  const toastTimer = useRef<number | null>(null)
  // 高亮淡出计时 / 命中兜底计时（消息一直没到就放弃）
  const focusTimer = useRef<number | null>(null)
  const settleTimer = useRef<number | null>(null)
  // onFocusDone 是 App 的内联箭头函数（每次渲染都换引用），用 ref 钉住
  const onFocusDoneRef = useRef(onFocusDone)

  useEffect(() => {
    messagesRef.current = messages
  }, [messages])
  useEffect(() => {
    scopeRef.current = scope
  }, [scope])
  useEffect(() => {
    onFocusDoneRef.current = onFocusDone
  }, [onFocusDone])

  // bridge 取一次就够。不在桌面端环境 / 桥起不来时静默失败：媒体不渲染，其余照常。
  useEffect(() => {
    let alive = true
    getBridge()
      .then((b) => {
        if (alive) setBridge(b)
      })
      .catch(() => {
        /* 静默：没有 bridge 就没有媒体 URL，界面不该因此报错 */
      })
    return () => {
      alive = false
    }
  }, [])

  const flash = (msg: string) => {
    setToast(msg)
    if (toastTimer.current) window.clearTimeout(toastTimer.current)
    toastTimer.current = window.setTimeout(() => setToast(''), 2400)
  }

  const visible = () => document.visibilityState === 'visible'

  /* ── 数据加载 ─────────────────────────────────── */

  const loadFeed = useCallback(
    async (key: string, after: number, silent: boolean) => {
      if (!key || feedBusy.current) return
      feedBusy.current = true
      if (!silent) setFeedLoading(true)
      try {
        const res = await api.consoleFeed(key, after, FEED_LIMIT)
        // 拉取期间用户切走了 → 丢弃，别把 A 的消息塞进 B
        if (scopeRef.current !== key) return
        // 记住这批消息属于哪个会话：搜索定位必须等消息流切到目标会话再滚，
        // 否则不同会话里撞了同一个 id 时会滚到上一个会话的消息。
        setFeedScope(key)
        setMessages((prev) => {
          if (after <= 0) return res.messages
          const seen = new Set(prev.map((m) => m.id))
          const add = res.messages.filter((m) => !seen.has(m.id))
          return add.length ? [...prev, ...add] : prev
        })
      } catch (e) {
        if (!silent) setErr((e as Error).message)
      } finally {
        feedBusy.current = false
        if (!silent) setFeedLoading(false)
      }
    },
    [],
  )

  const loadSnapshot = useCallback(async (silent: boolean) => {
    if (snapBusy.current) return
    snapBusy.current = true
    if (!silent) setSnapLoading(true)
    try {
      setSnapshot(await api.consoleSnapshot())
    } catch {
      if (!silent) setErr('状态读不出来——糖糖可能没在运行')
    } finally {
      snapBusy.current = false
      if (!silent) setSnapLoading(false)
    }
  }, [])

  const bootstrap = useCallback(async () => {
    try {
      const st = await api.consoleStatus()
      setStatus(st)
      if (!st.available) return
      void loadSnapshot(true)
      // 会话源的拉取与默认选中都在 App（侧栏和这里共用同一份）——
      // 这里只负责「糖糖本体在不在」这一个判断。
    } catch (e) {
      setStatus({ available: false, port: 0, bot_online: false, reason: (e as Error).message })
    }
  }, [loadSnapshot])

  useEffect(() => {
    void bootstrap()
  }, [bootstrap])

  /* ── 切换会话：清空 + 首拉 ─────────────────────────── */

  // 只依赖 scope：sources 变化（QQ 来了新消息 → 源数量/预览变了）绝不能
  // 触发这里，否则正在看的会话会被清空重拉，闪一下还丢滚动位置。
  // 已读位置也跟着 sources 单独走，但那是侧栏的职责了（Sidebar 里）。
  useEffect(() => {
    if (!scope) return
    setMessages([])
    setFeedScope('')
    setConfirming(false)
    setHighlightId(null)
    stickRef.current = true
    void loadFeed(scope, 0, false)
  }, [scope, loadFeed])

  /* ── 轮询 ─────────────────────────────────────────── */

  useEffect(() => {
    if (!scope) return
    const tick = () => {
      if (!visible()) return
      const list = messagesRef.current
      const after = list.length ? list[list.length - 1].id : 0
      void loadFeed(scope, after, true)
    }
    const id = window.setInterval(tick, FEED_POLL_MS)
    return () => window.clearInterval(id)
  }, [scope, loadFeed])

  useEffect(() => {
    const tick = () => {
      if (!visible()) return
      void loadSnapshot(true)
    }
    const id = window.setInterval(tick, SNAP_POLL_MS)
    return () => window.clearInterval(id)
  }, [loadSnapshot])

  // 切回前台：立刻补一次（轮询可能在后台被跳过很久）。
  // 会话源列表的补拉在 App 那边，这里只管消息流和状态。
  useEffect(() => {
    const onVis = () => {
      if (!visible()) return
      void loadSnapshot(true)
      if (scopeRef.current) {
        const list = messagesRef.current
        const after = list.length ? list[list.length - 1].id : 0
        void loadFeed(scopeRef.current, after, true)
      }
    }
    document.addEventListener('visibilitychange', onVis)
    return () => document.removeEventListener('visibilitychange', onVis)
  }, [loadFeed, loadSnapshot])

  /* ── 滚动贴底 ──────────────────────────────────────── */

  useEffect(() => {
    const el = scrollRef.current
    if (!el || !stickRef.current) return
    el.scrollTop = el.scrollHeight
  }, [messages, feedLoading])

  const onScroll = () => {
    const el = scrollRef.current
    if (!el) return
    stickRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 60
  }

  /* ── 搜索命中定位：滚到那条 + 高亮 1.6 秒 ──────────────
     顺序很重要：这个 effect 必须排在「滚动贴底」之后——同一批 DOM 更新里
     先贴底、后 scrollIntoView，否则贴底会把定位覆盖掉。 */

  useEffect(() => {
    if (focusId == null) return
    const root = scrollRef.current
    if (!root) return

    // 兜底：6 秒还等不到目标消息就放弃，别让 App 的 focusId 永远挂着
    const armSettle = () => {
      if (settleTimer.current != null) return
      settleTimer.current = window.setTimeout(() => {
        settleTimer.current = null
        onFocusDoneRef.current()
      }, 6000)
    }

    // 消息流还没切到目标会话时不能滚：切会话的那一帧 messages 还是旧会话的，
    // 而不同会话的消息 id 可能撞车 —— 会误滚到上一个会话里同 id 的消息。
    if (feedScope !== scope || !messages.some((m) => m.id === focusId)) {
      armSettle()
      return
    }
    if (settleTimer.current != null) {
      window.clearTimeout(settleTimer.current)
      settleTimer.current = null
    }

    const el = root.querySelector<HTMLElement>(`[data-mid="${focusId}"]`)
    if (!el) {
      armSettle()
      return
    }

    // 别让贴底逻辑把定位顶回去
    stickRef.current = false
    setHighlightId(focusId)
    el.scrollIntoView({ block: 'center', behavior: 'smooth' })

    if (focusTimer.current != null) window.clearTimeout(focusTimer.current)
    focusTimer.current = window.setTimeout(() => {
      focusTimer.current = null
      setHighlightId(null)
    }, 1600)

    // 定位完成立刻回报——App 把 focusId 置 null，避免 effect 反复触发
    onFocusDoneRef.current()
  }, [focusId, messages, feedScope, scope])

  // 卸载时把两个计时器收干净（高亮计时不在上面的 cleanup 里清，
  // 否则轮询一更新 messages 就会把 1.6 秒的淡出掐掉）
  useEffect(() => {
    return () => {
      if (focusTimer.current != null) window.clearTimeout(focusTimer.current)
      if (settleTimer.current != null) window.clearTimeout(settleTimer.current)
      focusTimer.current = null
      settleTimer.current = null
    }
  }, [])

  /* ── 痕迹展开 ──────────────────────────────────────── */

  const toggleTrace = useCallback(
    async (summary: ConsoleTraceSummary) => {
      const rid = summary.run_id || summary.event_key
      if (!rid) return
      setExpanded((prev) => ({ ...prev, [rid]: !prev[rid] }))
      // 已经拉过 / 正在拉 / 上次失败 → 不重复请求
      if (traces[rid] !== undefined || traceBusy[rid]) return
      setTraceBusy((prev) => ({ ...prev, [rid]: true }))
      try {
        const res = await api.consoleTrace(
          summary.run_id ? { run_id: summary.run_id } : { event_key: summary.event_key },
        )
        setTraces((prev) => ({ ...prev, [rid]: res.trace }))
      } catch {
        setTraces((prev) => ({ ...prev, [rid]: null }))
        setTraceErr((prev) => ({ ...prev, [rid]: true }))
      } finally {
        setTraceBusy((prev) => ({ ...prev, [rid]: false }))
      }
    },
    [traces, traceBusy],
  )

  /* ── 回合回放 ──────────────────────────────────────── */

  /** 打开完整回放抽屉：思维链 → 工具链 → 决定 → 对方反应。 */
  const openReplay = useCallback(async (eventKey: string) => {
    if (!eventKey) return
    setReplayOpen(true)
    setReplayError('')
    setReplayData(null)
    setReplayLoading(true)
    try {
      const res = await api.consoleReplay(eventKey)
      setReplayData(res.replay)
      if (!res.replay) {
        setReplayError('这一回合没有留下痕迹（痕迹采集 2026-10-24 才上线，更早的回合没有）')
      }
    } catch (e) {
      setReplayError(e instanceof Error ? e.message : '读取回放失败')
    } finally {
      setReplayLoading(false)
    }
  }, [])

  /* ── 操作 ──────────────────────────────────────────── */

  /** 点图片 → 打开放大预览层 */
  const openPreview = useCallback((url: string, name: string) => {
    setPreview({ url, name })
  }, [])

  const doSend = async (force: boolean) => {
    const text = composer.trim()
    if (!text || !scope || sending) return
    const src = sources.find((s) => s.key === scope)
    // QQ 镜像会话：先弹确认条，避免一个误按真的发到群里
    if (!force && src && src.kind !== 'desktop') {
      setConfirming(true)
      return
    }
    setSending(true)
    setErr(null)
    try {
      const res = await api.consoleSend({ scope, text })
      setComposer('')
      setConfirming(false)
      if (res.mode === 'qq') flash(`已发到「${src?.title || 'QQ'}」`)
      const list = messagesRef.current
      const after = list.length ? list[list.length - 1].id : 0
      await loadFeed(scope, after, false)
    } catch (e) {
      // 失败不清输入框——别让用户白写一遍
      setErr((e as Error).message)
      setConfirming(false)
    } finally {
      setSending(false)
    }
  }

  const autonomousTurn = async () => {
    const local = sources.find((s) => s.key === scope && s.kind === 'desktop')
    const target = local || sources.find((s) => s.kind === 'desktop')
    if (!target) {
      setErr('先在左侧「本地」建一个会话，「让糖糖说点什么」只对本地会话有效')
      return
    }
    if (target.key !== scope) onScopeChange(target.key)
    try {
      await api.consoleTurn(target.key)
      const list = messagesRef.current
      const after = list.length ? list[list.length - 1].id : 0
      await loadFeed(target.key, after, false)
      void loadSnapshot(true)
    } catch (e) {
      setErr((e as Error).message)
    }
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      void doSend(false)
    }
    if (e.key === 'Escape' && confirming) {
      e.preventDefault()
      setConfirming(false)
    }
  }

  /* ── 派生数据 ──────────────────────────────────────── */

  const available = status?.available === true

  const active = sources.find((s) => s.key === scope) ?? null
  // sources 还没拉回来（或后端暂缺这条）时按 scope 的 `kind:id` 前缀兜底——
  // 否则独白频道会先按普通气泡渲染一下，等 sources 到了才变成卡片。
  const activeKind: ConsoleSourceKind =
    active?.kind ?? (scope.startsWith('monologue:') ? 'monologue' : 'desktop')
  const title =
    active?.title || (scope ? (activeKind === 'monologue' ? '独白' : '未命名会话') : '会客厅')
  const online = status?.bot_online === true

  const mood = snapshot?.mood ?? null
  const self = snapshot?.self ?? null
  const rels = snapshot?.relationships ?? null
  const mem = snapshot?.memory ?? null
  const model = snapshot?.model ?? null
  const lastTurn = snapshot?.turn ?? null

  /* ── 引导态：糖糖本体没跑 ──────────────────────────── */

  if (!available) {
    return (
      <div className="cs-page">
        <div className="cs-gate">
          <div className="cs-gate-icon">
            {status === null ? <Loader2 size={22} className="spin" /> : <MessagesSquare size={22} />}
          </div>
          <h1 className="cs-gate-title">会客厅</h1>
          <p className="cs-gate-desc explain-only">
            这里能看到糖糖在 QQ 里的全部对话，也能看她每一句是怎么想出来的。
            <br />
            但她的「本体」跑在 bot 进程里——本体没启动，这里就没有可看的。
          </p>
          {status?.reason && <div className="cs-gate-reason">{status.reason}</div>}
          <div className="cs-gate-actions">
            <button className="btn btn-primary" onClick={() => void bootstrap()}>
              <RefreshCw size={14} />
              重新检测
            </button>
            {onGoDashboard && (
              <button className="btn btn-ghost" onClick={onGoDashboard}>
                去概览页启动糖糖
              </button>
            )}
          </div>
          <p className="cs-gate-hint">
            启动方式：概览页点「启动糖糖」，或命令行 <code>python main.py</code>
          </p>
        </div>
      </div>
    )
  }

  /* ── 正常态 ────────────────────────────────────────── */

  return (
    <div className="cs-page">
      {/* ══════════ 中栏：消息流 + 输入（左栏已搬到应用侧栏） ══════════ */}
      <section className="cs-stream">
        <header className="cs-stream-head">
          <div className="cs-stream-title">
            <span className={'cs-conn' + (online ? ' is-online' : '')} title={online ? '糖糖在线' : '糖糖未连接'}>
              <i className="cs-dot" />
              {online ? '糖糖在线' : '未连接'}
            </span>
            <h1 className="cs-title" title={title}>
              {title}
            </h1>
            {active && <span className="cs-chip">{KIND_TAG[active.kind]}</span>}
            {model?.model && <span className="cs-chip">{model.model}</span>}
          </div>
          <div className="cs-head-actions">
            <button
              className="btn btn-ghost btn-sm"
              onClick={() => void loadSnapshot(false)}
              disabled={snapLoading}
              title="刷新右栏状态"
            >
              <RefreshCw size={13} className={snapLoading ? 'spin' : ''} />
              刷新状态
            </button>
            {/* 独白是只读频道——在这里点「让她说点什么」会跳到本地会话去，
                跟当前视野自相矛盾，所以只在有输入能力的会话里出现。 */}
            {activeKind !== 'monologue' && (
              <button
                className="btn btn-ghost btn-sm"
                onClick={() => void autonomousTurn()}
                title="自主回合：让她按此刻的驱动力主动说一句（只对本地会话有效）"
              >
                <Sparkles size={13} />
                让糖糖说点什么
              </button>
            )}
            <button
              className={'icon-btn' + (panelOpen ? ' is-on' : '')}
              title={panelOpen ? '收起状态栏' : '展开状态栏'}
              onClick={() => setPanelOpen((v) => !v)}
            >
              {panelOpen ? <PanelRightClose size={14} /> : <PanelRight size={14} />}
            </button>
          </div>
        </header>

        {err && (
          <div className="cs-err">
            <AlertCircle size={13} />
            <span className="cs-err-text">{err}</span>
            <button className="cs-err-x" title="关闭" onClick={() => setErr(null)}>
              <X size={12} />
            </button>
          </div>
        )}

        <div className="cs-messages" ref={scrollRef} onScroll={onScroll}>
          {!scope && (
            <div className="cs-empty">
              <div className="cs-empty-title">选一个会话</div>
              <p className="cs-empty-sub">
                左侧栏是糖糖在 QQ 里的全部对话，点一个就能看。
                <br />
                想单独跟她说话，就在左侧「本地」里新建一个会话。
              </p>
            </div>
          )}

          {scope && feedLoading && messages.length === 0 && (
            <div className="cs-empty">
              <div className="cs-empty-sub">正在读取这段对话…</div>
            </div>
          )}

          {scope && !feedLoading && messages.length === 0 && (
            <div className="cs-empty">
              {activeKind === 'monologue' ? (
                <>
                  <div className="cs-empty-title">还没有独白</div>
                  <p className="cs-empty-sub">
                    她每次写日记、或者有欲望但选择安静的时候，这里就会多一条。
                  </p>
                </>
              ) : (
                <>
                  <div className="cs-empty-title">这个会话还没有消息</div>
                  <p className="cs-empty-sub">
                    {activeKind === 'desktop'
                      ? '在下面说一句，她会用自己的记忆和情绪回你。'
                      : '等 QQ 里有人说话，这里就会同步出现。'}
                  </p>
                </>
              )}
            </div>
          )}

          {messages.map((m) => {
            const rid = m.trace ? m.trace.run_id || m.trace.event_key : ''
            const focusCls = highlightId === m.id ? ' is-focus' : ''

            // ── 独白频道：一条条卡片，不是左右气泡 ──
            if (activeKind === 'monologue') {
              const digest = m.entry_kind === 'digest'
              return (
                <article
                  className={'cs-diario' + (digest ? ' is-digest' : '') + focusCls}
                  key={m.id}
                  data-mid={m.id}
                >
                  <div className="cs-diario-head">
                    <span className="cs-diario-title">
                      {m.heading || (digest ? '内心消化' : '独白')}
                    </span>
                    {m.mood && <span className="cs-diario-mood">{m.mood}</span>}
                    <span className="cs-diario-time">{fmtClock(m.ts)}</span>
                  </div>
                  <div className="cs-diario-body">{m.content}</div>
                  <MediaBlock items={m.media ?? []} bridge={bridge} onPreview={openPreview} />
                  {m.trace && rid && (
                    <TraceCard
                      summary={m.trace}
                      open={!!expanded[rid]}
                      busy={!!traceBusy[rid]}
                      failed={!!traceErr[rid]}
                      trace={traces[rid] ?? null}
                      onToggle={() => void toggleTrace(m.trace!)}
                      onReplay={() => void openReplay(m.event_key || m.trace!.event_key)}
                    />
                  )}
                </article>
              )
            }

            return (
              <div className={'cs-msg is-' + m.role + focusCls} key={m.id} data-mid={m.id}>
                <div className="cs-name">{m.speaker || (m.role === 'tangtang' ? '糖糖' : '某人')}</div>
                <div className="cs-bubble">{m.content}</div>
                <MediaBlock items={m.media ?? []} bridge={bridge} onPreview={openPreview} />
                <div className="cs-foot">
                  <span className="cs-time">{fmtClock(m.ts)}</span>
                </div>
                {m.trace && rid && (
                  <TraceCard
                    summary={m.trace}
                    open={!!expanded[rid]}
                    busy={!!traceBusy[rid]}
                    failed={!!traceErr[rid]}
                    trace={traces[rid] ?? null}
                    onToggle={() => void toggleTrace(m.trace!)}
                    onReplay={() => void openReplay(m.event_key || m.trace!.event_key)}
                  />
                )}
              </div>
            )
          })}
        </div>

        {activeKind === 'monologue' ? (
          // 独白是只读的内在活动频道——没有输入框，别让人以为能在这里说话
          <div className="cs-mono-note">
            <Lock size={12} />
            独白是她的内在活动——这里只读，想说话去私聊
          </div>
        ) : (
          <footer className="cs-composer">
            {confirming && (
              <div className="cs-confirm-bar">
                <div className="cs-confirm-text">
                  将以你的身份发到「{title}」
                  <span className="cs-confirm-preview">{clip(composer, 90)}</span>
                </div>
                <div className="cs-confirm-actions">
                  <button
                    className="btn btn-primary btn-sm"
                    onClick={() => void doSend(true)}
                    disabled={sending}
                  >
                    {sending ? <Loader2 size={13} className="spin" /> : null}
                    发送
                  </button>
                  <button className="btn btn-ghost btn-sm" onClick={() => setConfirming(false)}>
                    取消
                  </button>
                </div>
              </div>
            )}

            <div className="cs-composer-box">
              <textarea
                ref={composerRef}
                rows={1}
                value={composer}
                placeholder={PLACEHOLDER[activeKind]}
                onChange={(e) => setComposer(e.target.value)}
                onKeyDown={onKeyDown}
                disabled={!scope}
              />
              <button
                className="cs-send"
                onClick={() => void doSend(false)}
                disabled={!composer.trim() || !scope || sending}
                title={
                  activeKind === 'desktop'
                    ? '发送（Enter）'
                    : `发送前会确认——${activeKind === 'group' ? '这真的会发到群里' : '这真的会发到 QQ'}`
                }
              >
                {sending ? <Loader2 size={15} className="spin" /> : <ChevronRight size={16} />}
              </button>
            </div>
          </footer>
        )}
      </section>

      {/* ══════════ 右栏：糖糖状态（2026-10-25 第 45 轮重做） ══════════
          旧版是 9 个 section 平铺的「标题 + 文字 + 细进度条」——数据一个不缺
          （/snapshot 早就全给了），但看不出「她现在怎么样」，也点不动。
          现在换成 <StatusPanel>：结论层（一句话）→ 现状层（情绪三卡 /
          驱动力排行）→ 溯源层（关系分布 / 记忆 / 自我叙事 / 最近经历）。
          同一个组件也用于概览页（variant='full'），避免两份实现漂移。 */}
      {panelOpen && (
        <aside className="cs-panel">
          <div className="cs-panel-head">
            <span className="cs-panel-title">糖糖状态</span>
            <button className="icon-btn" title="收起状态栏" onClick={() => setPanelOpen(false)}>
              <PanelRightClose size={14} />
            </button>
          </div>
          <div className="cs-panel-body">
            <StatusPanel
              snapshot={snapshot}
              loading={snapLoading}
              onRefresh={() => void loadSnapshot(false)}
              onOpenMemory={onOpenMemory}
            />
          </div>
        </aside>
      )}

      {/* 回合回放抽屉：她这一回合想了什么、做了什么、决定了什么、对方怎么反应 */}
      <TurnReplay
        open={replayOpen}
        loading={replayLoading}
        error={replayError}
        data={replayData}
        onClose={() => setReplayOpen(false)}
      />

      {/* 图片放大预览层（点图打开，Esc/背景/右上角关闭） */}
      {preview && (
        <Lightbox url={preview.url} name={preview.name} onClose={() => setPreview(null)} />
      )}

      {toast && (
        <div className="cs-toast">
          <Sparkles size={13} />
          {toast}
        </div>
      )}
    </div>
  )
}
