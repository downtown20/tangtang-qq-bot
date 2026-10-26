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
  Plug,
  ShieldCheck,
  RotateCw,
} from 'lucide-react'
import {
  api,
  type BotStatus,
  type CheckResultItem,
  type ConsoleSnapshot,
  type SnowlumaStatus,
  type StatCounts,
} from '../lib/api'
import { useBotControl } from '../lib/useBotControl'
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
  // SnowLuma 与连接自检（2026-10-26 从旧 Qt 控制台搬入）
  const [snowluma, setSnowluma] = useState<SnowlumaStatus | null>(null)
  const [snowBusy, setSnowBusy] = useState(false)
  const [snowTip, setSnowTip] = useState('')
  const [check, setCheck] = useState<CheckResultItem[] | null>(null)
  const [checkBusy, setCheckBusy] = useState(false)

  const load = useCallback(() => {
    setLoading(true)
    Promise.all([
      api.stats(),
      api.status(),
      // 单点失败不拖垮整页：状态面板自己有「还没有状态数据」的空态
      api.consoleSnapshot().catch(() => null),
      api.snowlumaStatus().catch(() => null),
    ])
      .then(([s, st, snap, snow]) => {
        setStats(s)
        setStatus(st)
        setSnapshot(snap)
        setSnowluma(snow)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  // 启停交给共用 hook（2026-10-27）：**和左上角那个按钮是同一个实现**，
  // 而且它会等状态真的翻转才解除 busy —— 原来的做法是发完命令立刻刷新，
  // 而 `python main.py` 起来要几秒，界面看着就像「按了没反应」。
  const bot = useBotControl()
  const running = bot.running

  const toggle = async () => {
    setBusy(true)
    try {
      await bot.toggle()
      if (bot.error) setError(bot.error)
      // 起停之后其余面板也变了（进程/会话源/统计），一并刷
      await Promise.all([
        api.stats().then(setStats),
        api.status().then(setStatus),
        api.consoleSnapshot().then(setSnapshot).catch(() => undefined),
      ])
    } finally {
      setBusy(false)
    }
  }

  /**
   * 一个按钮，按状态决定做什么（2026-10-27）。
   *
   * 之前文案固定写「启动」，运行中点了只会走 `already` 分支回来 —— 界面上
   * 看起来「按了没反应」。运行中该给的是「重启」，所以这里分两路：
   *   · 未运行 → /api/snowluma/start
   *   · 运行中 → /api/snowluma/restart（杀 5099 的进程 → 等端口释放 → 重新拉起）
   */
  const snowAction = async () => {
    const running = Boolean(snowluma?.running)
    setSnowBusy(true)
    setSnowTip('')
    try {
      if (running) {
        const r = await api.restartSnowluma()
        setSnowTip(r.restarted ? '已重启 SnowLuma' : '没找到在跑的进程，已重新启动')
      } else {
        const r = await api.startSnowluma()
        setSnowTip(r.already ? 'SnowLuma 已在运行' : r.note || '已启动 SnowLuma')
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSnowBusy(false)
      api.snowlumaStatus().then(setSnowluma).catch(() => undefined)
    }
  }

  const runCheck = async () => {
    setCheckBusy(true)
    try {
      const r = await api.connectCheck()
      setCheck(r.results)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setCheckBusy(false)
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

          {/* SnowLuma：QQ 通道。首次启动的网页面板密码只在它自己的新窗口里打印一次 */}
          <div className="row">
            <div className="row-icon is-cyan">
              <Plug size={17} />
            </div>
            <div className="row-main">
              <div className="row-title">
                SnowLuma
                {snowluma ? (
                  <span className={'badge ' + (snowluma.running ? 'badge-ok' : 'badge-muted')} style={{ marginLeft: 8 }}>
                    {snowluma.running ? '运行中' : snowluma.installed ? '未启动' : '未安装'}
                  </span>
                ) : null}
              </div>
              <div className="row-desc explain-only">
                QQ 通道（第三方协议端，不随项目分发）。必须先开 QQ 客户端并登录机器人账号。
              </div>
              {snowTip && <div className="row-desc">{snowTip}</div>}
            </div>
            <button
              className="btn btn-primary"
              onClick={() => void snowAction()}
              disabled={snowBusy || snowluma?.installed === false}
              title={
                snowluma?.installed === false
                  ? '先到官方发布页下载 Windows x64 版解压到 SnowLuma/'
                  : snowluma?.running
                    ? '杀掉当前进程再重新拉起（首次启动的初始密码只在它自己的新窗口里打印一次）'
                    : ''
              }
            >
              {snowBusy ? (
                <Loader2 size={14} className="spin" />
              ) : snowluma?.running ? (
                <RotateCw size={14} />
              ) : (
                <Play size={14} />
              )}
              {snowluma?.installed === false ? '未安装' : snowluma?.running ? '重启' : '启动'}
            </button>
          </div>

          {/* 连接自检：把「连不上」翻译成「第几步还没做、下一步填什么」 */}
          <div className="row">
            <div className="row-icon is-green">
              <ShieldCheck size={17} />
            </div>
            <div className="row-main">
              <div className="row-title">连接自检</div>
              <div className="row-desc explain-only">
                逐项核对 SnowLuma 安装、网页面板、OneBot 网络配置与糖糖这一侧的 Token/端口。
              </div>
            </div>
            <button className="btn btn-ghost" onClick={runCheck} disabled={checkBusy}>
              {checkBusy ? <Loader2 size={14} className="spin" /> : <ShieldCheck size={14} />}
              自检
            </button>
          </div>
        </div>
      </div>

      {check && (
        <div className="panel">
          <div className="panel-head">
            <span>连接自检结果</span>
            <span className="muted">{check.length} 项</span>
          </div>
          <div className="panel-body">
            {check.map((r) => (
              <div className="row" key={r.key}>
                <div className={'check-mark check-' + r.status}>
                  {({ ok: '[√]', warn: '[!]', fail: '[×]', skip: '[ ]' } as Record<string, string>)[r.status] ??
                    '[?]'}
                </div>
                <div className="row-main">
                  <div className="row-title">{r.title}</div>
                  {r.detail && <div className="row-desc">{r.detail}</div>}
                  {r.next_step && <div className="row-desc check-next">→ {r.next_step}</div>}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

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
