import { useCallback, useEffect, useState } from 'react'
import {
  RefreshCw,
  Cpu,
  Radio,
  Database,
  Images,
  MessageSquare,
  Users,
  Settings2,
  ScrollText,
  ChevronRight,
  Play,
  Square,
  Loader2,
} from 'lucide-react'
import { api, type BotStatus, type ConsoleSnapshot, type StatCounts } from '../lib/api'
import StatusPanel from '../components/StatusPanel'
import './Page.css'

/* ═══════════════════════════════════════════════════════════════
   概览页（2026-10-25 第 45 轮重做）

   ## 改之前

   4 个数字卡（认识的人/记忆条数/对话记录/表情包）+ 进程开关 + 模型。
   **她的状态一个都没有**——而数据全在（`GET /snapshot` 早就有情绪/驱动力/
   自我叙事/关系场/记忆）。主人原话：「糖糖状态这部分一直没动。」

   ## 改之后

   这一页和会客厅右栏是**同一个 `<StatusPanel>`**，只是 `variant='full'`：
   同一份数据、同一套可视化语法，只差密度。两处各写一份必然漂移（教训 #47）。

   页面顺序按「主人最关心什么」排：
     1. **她现在怎么样**（StatusPanel full：结论 → 现状 → 溯源）
     2. 数据规模（四卡）
     3. 运行状态（进程 / 模型，可以启停）
     4. 快捷入口
   ═══════════════════════════════════════════════════════════════ */

const STAT_DEFS: { key: keyof StatCounts; label: string; icon: typeof Users }[] = [
  { key: 'people', label: '认识的人', icon: Users },
  { key: 'memories', label: '记忆条数', icon: Database },
  { key: 'chats', label: '对话记录', icon: MessageSquare },
  { key: 'stickers', label: '表情包', icon: Images },
]

export default function Dashboard({
  onOpenSettings,
  onOpenMemory,
}: {
  onOpenSettings?: () => void
  /** 去记忆图谱（状态面板「记忆」区块的入口） */
  onOpenMemory?: () => void
}) {
  const [stats, setStats] = useState<StatCounts | null>(null)
  const [status, setStatus] = useState<BotStatus | null>(null)
  // 状态快照可能拿不到（糖糖本体没跑）——降级成 null，页面照常显示其它区块
  const [snapshot, setSnapshot] = useState<ConsoleSnapshot | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(true)
  const [busy, setBusy] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    Promise.all([
      api.stats(),
      api.status(),
      // 单点失败不拖垮整页：状态面板自己有「还没有状态数据」的空态
      api.consoleSnapshot().catch(() => null),
    ])
      .then(([s, st, snap]) => {
        setStats(s)
        setStatus(st)
        setSnapshot(snap)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const running = status?.bot.running ?? false

  const toggle = async () => {
    setBusy(true)
    try {
      running ? await api.botStop() : await api.botStart()
      await Promise.all([
        api.stats().then(setStats),
        api.status().then(setStatus),
        api.consoleSnapshot().then(setSnapshot).catch(() => undefined),
      ])
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="page">
      <div className="page-head">
        <div>
          <h1 className="page-title">概览</h1>
          <p className="page-sub">她现在怎么样、数据规模、运行状态与快捷入口</p>
        </div>
        <div className="page-actions">
          <button className="btn btn-ghost" onClick={load} disabled={loading}>
            <RefreshCw size={14} className={loading ? 'spin' : ''} />
            刷新
          </button>
        </div>
      </div>

      {error && (
        <div className="notice notice-error">
          <strong>后端桥未连接：</strong>
          {error}
          <div className="notice-hint">
            请通过桌面快捷方式启动（会自动拉起 Python 桥）。
          </div>
        </div>
      )}

      {/* ① 她现在怎么样 —— 与会客厅右栏共用同一个组件，只是宽版 */}
      <StatusPanel
        snapshot={snapshot}
        loading={loading}
        onRefresh={load}
        onOpenMemory={onOpenMemory}
        variant="full"
      />

      {/* ② 数据规模 */}
      <div className="section-head">
        <span className="section-title">数据规模</span>
      </div>
      <div className="stat-grid">
        {STAT_DEFS.map((s) => {
          const Icon = s.icon
          return (
            <div className="stat-card" key={s.key}>
              <div className="stat-value">{stats ? (stats[s.key] ?? '—') : '—'}</div>
              <div className="stat-label">
                <Icon size={12} style={{ marginRight: 5, verticalAlign: -1 }} />
                {s.label}
              </div>
            </div>
          )
        })}
      </div>

      {/* ③ 运行状态（可操作：启停进程） */}
      <div className="section-head">
        <span className="section-title">运行状态</span>
        <span className={'badge ' + (running ? 'badge-ok' : 'badge-muted')}>
          {running ? `运行中 · PID ${status?.bot.pid}` : '未启动'}
        </span>
      </div>

      <div className="panel">
        <div className="panel-body">
          <div className="row">
            <div className="row-icon is-blue">
              <Radio size={17} />
            </div>
            <div className="row-main">
              <div className="row-title">糖糖进程</div>
              <div className="row-desc explain-only">
                {running
                  ? 'QQ 连接由 SnowLuma 提供，消息经 WebSocket 进入'
                  : '点击右侧按钮启动 python main.py'}
              </div>
            </div>
            <button className={'btn ' + (running ? 'btn-ghost' : 'btn-primary')} onClick={toggle} disabled={busy}>
              {busy ? (
                <Loader2 size={14} className="spin" />
              ) : running ? (
                <Square size={12} />
              ) : (
                <Play size={14} />
              )}
              {running ? '停止' : '启动'}
            </button>
          </div>

          <div className="row">
            <div className="row-icon is-violet">
              <Cpu size={17} />
            </div>
            <div className="row-main">
              <div className="row-title">{status?.llm.model || '未配置模型'}</div>
              <div className="row-desc explain-only">
                Provider {status?.llm.provider || '—'} ·{' '}
                {status?.llm.models_count ? `${status.llm.models_count} 个模型档位` : '旧版单模型配置'}
              </div>
            </div>
            <button className="btn btn-ghost" onClick={onOpenSettings}>
              <Settings2 size={14} />
              切换
            </button>
          </div>
        </div>
      </div>

      {/* ④ 快捷入口 */}
      <div className="section-head">
        <span className="section-title">快捷入口</span>
      </div>
      <div className="panel">
        <div className="panel-body">
          <button className="row" onClick={onOpenSettings} style={{ width: '100%', textAlign: 'left' }}>
            <div className="row-icon is-cyan">
              <Settings2 size={17} />
            </div>
            <div className="row-main">
              <div className="row-title">设置</div>
              <div className="row-desc explain-only">模型、人格、行为、连接与任务</div>
            </div>
            <ChevronRight size={16} className="muted" />
          </button>
          {onOpenMemory ? (
            <button className="row" onClick={onOpenMemory} style={{ width: '100%', textAlign: 'left' }}>
              <div className="row-icon is-green">
                <ScrollText size={17} />
              </div>
              <div className="row-main">
                <div className="row-title">记忆图谱</div>
                <div className="row-desc explain-only">她还记得什么、谁跟她有关、记忆之间怎么连</div>
              </div>
              <ChevronRight size={16} className="muted" />
            </button>
          ) : null}
        </div>
      </div>
    </div>
  )
}
