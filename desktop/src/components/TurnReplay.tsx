import { useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import {
  Brain,
  ChevronRight,
  Gavel,
  LoaderCircle,
  MessageCircle,
  Quote,
  Route,
  TriangleAlert,
  Wrench,
  X,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import type {
  ConsoleFlowSummary,
  ConsolePromptLedger,
  ConsoleToolEffect,
  ConsoleToolEffects,
  ConsoleTurnFlowBlock,
  ConsoleTurnFlowPhase,
} from '../lib/api'
import { formatMs } from '../lib/format'
import FlowGraph from './TurnReplayFlow'
import PromptLedger from './PromptLedger'
import './TurnReplay.css'

/* ═══════════════════════════════════════════════════════════════
   回合回放 —— 点开糖糖的某条回复，看她那一回合经历了什么：

     她在想什么（思维链）→ 她做了什么（工具）→
     她决定了什么（决策记录）→ 对方怎么反应（反馈）

   数据由后端采集，前端只做展示：不请求接口、不改任何状态。
   字段名与后端 /api/console/replay 的返回严格对齐（2026-10-24）。
   ═══════════════════════════════════════════════════════════════ */

/** 一次工具调用。args / result 都是后端已序列化好的字符串，可能很长。 */
export interface ReplayToolStep {
  name: string
  args: string
  result: string
  ms: number
}

/** 被回放的那条消息本身。 */
export interface ReplayMessage {
  id: number
  content: string
  /** 'YYYY-MM-DD HH:MM:SS' */
  timestamp: string
  /** '糖糖' 或 '你（桌面代发）' */
  speaker: string
  /** 'private:123456' 或 'group:123456' */
  scope: string
}

/** 这一回合的采集痕迹：模型、思维链、工具、耗时。 */
export interface ReplayTrace {
  run_id: string
  model: string
  /** 思维链全文，可能 0~8000 字符 */
  reasoning: string
  reasoning_chars: number
  tools: ReplayToolStep[]
  duration_ms: number
  recorded_at: string
  /**
   * true = 这段思维链来自旧采集口径，可能混入了情绪分类器的输出。
   * 界面要给黄色警告，别让人误以为那是她的原话。
   */
  suspect: boolean
}

/** 她这一回合的决定（发送侧落库的记录）。 */
export interface ReplayDecision {
  /** 'reply' | 'skip' | 'fade' | 'failed' | '' */
  decision: string
  /** 'completed' | 'failed' | '' */
  status: string
  /** 例如 ['send_stickers'] */
  tool_calls: string[]
  started_at: string
  finished_at: string
  /**
   * 出错时的错误码（空 = 没出错）。2026-10-25 第 47 轮起才显示：
   * 后端 `decision_runs.error_code` 一直有值，前端一直没读——
   * 运营者原来只能看到「status=failed」两个字母，不知道失败在哪。
   */
  error_code?: string
  /** 决策记录自己的模型（可能与生成模型不同） */
  model?: string
  /** 同一回合的关联号：思维链/工具/决策/反馈都按它关联 */
  run_id?: string
  correlation_id?: string
  recorded_at?: string
}

/** 对方对这条回复的反馈（感知系统事后写入）。 */
export interface ReplayFeedback {
  /** 'positive' | 'neutral' | 'negative' */
  sentiment: string
  /** 0~1 */
  confidence: number
  /** 对方说的那句话 */
  user_reaction: string
  reply_ms: number
  timestamp: string
  direction_verified: boolean
  /**
   * 她这一回合实际发出去的整段回复（多气泡拼接后的原文）。
   * 2026-10-25 第 17 轮 ④ 起后端才带；老数据可能没有 → 可选用。
   * 七段流程的第 7 段用它显示「她到底说了什么」。
   */
  bot_reply?: string
}

/**
 * 流程图谱的类型直接用 `lib/api.ts` 里的定义（2026-10-25 二次整改）。
 *
 * 这里只留别名而不是再写一遍字段：字段与后端严格对齐，写两遍必然漂移
 * （本项目实际发生过：`flow` 的类型改了一处、另一处还按旧口径渲染）。
 */
export type ReplayFlowBlock = ConsoleTurnFlowBlock
export type ReplayFlowPhase = ConsoleTurnFlowPhase

export interface ReplayPayload {
  message: ReplayMessage | null
  trace: ReplayTrace | null
  decision: ReplayDecision | null
  feedback: ReplayFeedback | null
  /** ⚠ 可选：老回放（后端加这段之前）没有这个字段 */
  flow?: ReplayFlowPhase[]
  /** 整条链的体检结论（顶部横幅）。老后端没有 → 前端按 status 兜底数一遍 */
  flow_summary?: ConsoleFlowSummary
  /**
   * 她这一轮收到的 prompt 账本（第 47 轮）。**可选**——老后端没有就不渲染，
   * 绝不能显示成「0 token」（那是假绿，见 `_build_prompt_ledger` 的注释）。
   */
  prompt_ledger?: ConsolePromptLedger
  /** 她这一轮的工具效果（分类 + 空手判定 + 决策理由）。同样可选 */
  tool_effects?: ConsoleToolEffects
}

export interface TurnReplayProps {
  open: boolean
  loading: boolean
  error: string
  data: ReplayPayload | null
  onClose: () => void
}

/* ═══════════════════════════════════════════════════════════════
   格式化小工具（都是纯函数，失败一律降级成「—」而不是抛错）

   `formatMs` 已提到 `lib/format.ts`：流程图谱也要显示耗时，
   两处各写一份必然漂移（同一回合一处显示 3.1s、一处显示 3100ms）。
   ═══════════════════════════════════════════════════════════════ */

/** ISO8601 → 本地 HH:MM:SS；解析失败就原样返回，别把记录吞掉 */
function formatClock(iso: string): string {
  if (!iso) return '—'
  const t = Date.parse(iso)
  if (Number.isNaN(t)) return iso
  const d = new Date(t)
  const pad = (n: number) => String(n).padStart(2, '0')
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`
}

/** 0~1 → 百分比字符串；夹紧范围，后端给 1.2 也不显示 120% */
function formatPct(v: number): string {
  if (!Number.isFinite(v)) return '—'
  return `${Math.round(Math.min(1, Math.max(0, v)) * 100)}%`
}

/**
 * 两个 ISO 时间之间的毫秒差。
 *
 * 解析失败或终点早于起点一律返回 0（调用方据此不显示这一行）——
 * **不猜**。时钟回拨或字段损坏时宁可不显示，也不要显示一个负耗时。
 */
function elapsedMs(start: string, end: string): number {
  const a = Date.parse(start || '')
  const b = Date.parse(end || '')
  if (Number.isNaN(a) || Number.isNaN(b) || b < a) return 0
  return b - a
}

/** 决策值翻中文；未知值原样保留（后端口径变了也看得懂） */
function decisionLabel(v: string): string {
  const map: Record<string, string> = {
    reply: '回复',
    skip: '沉默',
    fade: '淡出',
    failed: '失败',
  }
  return v ? (map[v] ?? v) : '未记录'
}

/** 状态翻中文；未知值原样保留 */
function statusLabel(v: string): string {
  const map: Record<string, string> = {
    completed: '已完成',
    failed: '失败',
  }
  return v ? (map[v] ?? v) : '未记录'
}

/** 情绪三分类翻中文；未知值原样保留 */
function sentimentLabel(v: string): string {
  const map: Record<string, string> = {
    positive: '正面',
    neutral: '中性',
    negative: '负面',
  }
  return v ? (map[v] ?? v) : '未知'
}

/* ═══════════════════════════════════════════════════════════════
   小组件
   ═══════════════════════════════════════════════════════════════ */

type SectionKey = 'flow' | 'reasoning' | 'tools' | 'decision' | 'feedback'

/** 默认展开前两段（完整流程 / 她在想什么）——抽屉一开就够用 */
const DEFAULT_EXPANDED: Record<SectionKey, boolean> = {
  flow: true,
  reasoning: true,
  tools: false,
  decision: false,
  feedback: false,
}

/** 一个可折叠区块：标题行本身就是按钮 */
function Section({
  icon: Icon,
  title,
  count,
  badge,
  open,
  onToggle,
  children,
}: {
  icon: LucideIcon
  title: string
  count?: number
  badge?: ReactNode
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  return (
    <section className={'tr-sec' + (open ? ' is-open' : '')}>
      <button
        type="button"
        className="tr-sec-head"
        onClick={onToggle}
        aria-expanded={open}
      >
        <ChevronRight size={13} className={'tr-caret' + (open ? ' is-open' : '')} />
        <Icon size={13} className="tr-sec-icon" />
        <span className="tr-sec-title">{title}</span>
        {typeof count === 'number' ? <span className="tr-sec-count">{count}</span> : null}
        {badge}
      </button>
      {open ? <div className="tr-sec-body">{children}</div> : null}
    </section>
  )
}

/** 参数 / 结果各自一个可折叠 <pre>：默认收起，长文本内部滚动 */
function FoldPre({ label, text }: { label: string; text: string }) {
  const [open, setOpen] = useState(false)

  // 空内容不占位（工具有时没有参数或没有结果摘要）
  if (!text || !text.trim()) return null

  return (
    <div className="tr-fold">
      <button
        type="button"
        className="tr-fold-btn"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
      >
        <ChevronRight size={11} className={'tr-caret' + (open ? ' is-open' : '')} />
        {label}
        <span className="tr-fold-chars">{text.length} 字</span>
      </button>
      {open ? <pre className="tr-pre">{text}</pre> : null}
    </div>
  )
}

/**
 * 一条工具调用：名称 + 性质 + 有效性 + 耗时，参数/结果折叠。
 *
 * 2026-10-25 第 47 轮：加了 `effect`（后端算好的分类/空手判定）。
 * **没有 effect 时退化成旧样子**——老后端/老 trace 不能因为多了个字段就空白。
 */
function ToolStepView({
  step,
  index,
  effect,
}: {
  step: ReplayToolStep
  index: number
  effect?: ConsoleToolEffect
}) {
  const isEmpty = Boolean(effect?.empty)
  return (
    <div className={'tr-tool' + (isEmpty ? ' is-empty' : '')}>
      <div className="tr-tool-head">
        <span className="tr-tool-idx">{index + 1}</span>
        {effect ? <span className="tr-tool-kind">{effect.kind_cn}</span> : null}
        <span className="tr-tool-name" title={step.name}>
          {step.name || '未命名工具'}
        </span>
        {effect ? (
          <span className={'tr-tool-verdict' + (isEmpty ? ' is-empty' : '')}>
            {effect.verdict_cn}
          </span>
        ) : null}
        <span className="tr-tool-ms">{formatMs(step.ms)}</span>
      </div>
      {effect?.brief ? (
        <p className="tr-tool-brief">{effect.brief}</p>
      ) : null}
      <FoldPre label="参数" text={step.args} />
      <FoldPre label="结果" text={step.result} />
    </div>
  )
}

/**
 * 「她做了什么」的汇总条：一句话结论 + 空手率 + 她的决策理由。
 *
 * 主人要的是「有效信息」而不是一串工具名。所以顶部先给结论：
 * 「查了 3 次，2 次拿到材料；另外做了 1 个动作」——这句话本身就是判据，
 * 由后端算（`_build_tool_effects`），前端只显示。
 */
function ToolSummary({ effects }: { effects: ConsoleToolEffects }) {
  const rate = effects.lookups > 0 ? Math.round(effects.empty_rate * 100) : null
  return (
    <div className={'tr-toolsum is-' + effects.verdict}>
      <div className="tr-toolsum-head">
        <span className="tr-toolsum-text">{effects.summary}</span>
        {rate != null ? (
          <span
            className={'tr-toolsum-rate' + (rate > 0 ? ' is-hot' : '')}
            title="检索次数里有多少次什么都没查到——这是「她不知道」的直接证据，不是猜测"
          >
            空手率 {rate}%
          </span>
        ) : null}
      </div>
      {effects.rationale ? (
        <blockquote className="tr-rationale" title="这句话是从决策类工具的入参里取的原文（skip_response.reason 之类）">
          <Quote size={11} />
          <span>{effects.rationale}</span>
        </blockquote>
      ) : null}
      {effects.warnings.map((w, i) => (
        <p className="tr-warn-note" key={`${w.key}-${i}`} title={w.evidence}>
          <TriangleAlert size={12} />
          <span>{w.text}</span>
        </p>
      ))}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   主组件
   ═══════════════════════════════════════════════════════════════ */

export default function TurnReplay({ open, loading, error, data, onClose }: TurnReplayProps) {
  const [expanded, setExpanded] = useState<Record<SectionKey, boolean>>(DEFAULT_EXPANDED)
  const closeRef = useRef<HTMLButtonElement>(null)

  // 每次重新打开都回到默认折叠状态（上一次看的第三段别带过来）
  useEffect(() => {
    if (open) setExpanded(DEFAULT_EXPANDED)
  }, [open])

  // Esc 关闭；只在打开时挂监听，别在后台拦截按键
  useEffect(() => {
    if (!open) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [open, onClose])

  // 打开后把焦点放到关闭按钮：键盘用户不会还留在背后的会话里
  useEffect(() => {
    if (open) closeRef.current?.focus()
  }, [open])

  if (!open) return null

  const msg = data?.message ?? null
  const trace = data?.trace ?? null
  const decision = data?.decision ?? null
  const feedback = data?.feedback ?? null
  // 老后端没有 flow 字段 → 空数组 → 整块不渲染（不要显示一个空时间轴）
  const flow = data?.flow ?? []
  // 第 47 轮：工具效果 / prompt 账本。同样可选——老后端没有就退化成旧样子
  const toolEffects = data?.tool_effects ?? null

  const toggle = (key: SectionKey) => () =>
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }))

  const reasoning = trace?.reasoning.trim() ?? ''
  const tools = trace?.tools ?? []
  const hasAnySection = Boolean(flow.length || trace || decision || feedback)
  // 体检结论由后端算好（`flow_summary`）；老后端没有就交给图谱自己按 status 兜底
  const summary = data?.flow_summary ?? null
  const brokenCount = summary
    ? summary.error + summary.warn
    : flow.filter((p) => p.status === 'error' || p.status === 'warn').length

  return (
    <div className="tr-scrim" onClick={onClose}>
      <aside
        className="tr-drawer"
        role="dialog"
        aria-modal="true"
        aria-label="她这一回合"
        onClick={(e) => e.stopPropagation()}
      >
        <header className="tr-head">
          <div className="tr-head-main">
            <h2 className="tr-title">她这一回合</h2>
            <div className="tr-sub">
              {msg ? <span className="tr-sub-time">{msg.timestamp || '时间未记录'}</span> : null}
              {trace ? (
                <>
                  <span className="tr-sub-sep">·</span>
                  <span className="tr-sub-model" title={trace.model}>
                    {trace.model || '模型未知'}
                  </span>
                  <span className="tr-sub-sep">·</span>
                  <span className="tr-sub-dur">耗时 {formatMs(trace.duration_ms)}</span>
                </>
              ) : null}
              {!msg && !trace ? <span>没有采集到时间与模型</span> : null}
            </div>
          </div>
          <button
            type="button"
            ref={closeRef}
            className="tr-close"
            onClick={onClose}
            title="关闭（Esc）"
            aria-label="关闭"
          >
            <X size={16} />
          </button>
        </header>

        <div className="tr-body">
          {error ? (
            <div className="tr-error" role="alert">
              <TriangleAlert size={13} />
              <span>{error}</span>
            </div>
          ) : null}

          {loading ? (
            <>
              <div className="tr-loading-row" aria-live="polite">
                <LoaderCircle size={14} className="tr-spin" />
                <span>正在读取这一回合…</span>
              </div>
              <div className="tr-skel" aria-hidden="true">
                <span className="tr-skel-line" style={{ width: '92%' }} />
                <span className="tr-skel-line" style={{ width: '78%' }} />
                <span className="tr-skel-line" style={{ width: '86%' }} />
                <span className="tr-skel-line" style={{ width: '54%' }} />
              </div>
            </>
          ) : !data ? (
            <div className="tr-empty-state">还没有这一回合的记录</div>
          ) : (
            <>
              {flow.length > 0 ? (
                <Section
                  icon={Route}
                  title="完整流程"
                  count={flow.length}
                  open={expanded.flow}
                  onToggle={toggle('flow')}
                  badge={
                    brokenCount > 0 ? (
                      <span
                        className="tr-badge is-warn"
                        title={
                          summary
                            ? summary.broken
                                .map((b) => `第 ${b.phase} 站 ${b.label}：${b.note}`)
                                .join('\n')
                            : '有站打了折或异常'
                        }
                      >
                        {summary?.error
                          ? `${summary.error} 站异常`
                          : `${brokenCount} 站打折`}
                      </span>
                    ) : null
                  }
                >
                  {/* 账本放在流程前面：先知道「这一轮花了什么价、哪块材料被砍」，
                      再看九站怎么走。顺序反了读者会先陷入细节。 */}
                  {data.prompt_ledger ? (
                    <PromptLedger ledger={data.prompt_ledger} />
                  ) : null}
                  <FlowGraph phases={flow} summary={summary} />
                </Section>
              ) : null}

              {trace ? (
                <Section
                  icon={Brain}
                  title="她在想什么"
                  open={expanded.reasoning}
                  onToggle={toggle('reasoning')}
                  badge={
                    trace.suspect ? (
                      <span
                        className="tr-badge is-warn"
                        title="早期采集口径会把情绪分类器的输出混进思维链"
                      >
                        旧采集·可能混入分类器
                      </span>
                    ) : null
                  }
                >
                  {trace.suspect ? (
                    <p className="tr-warn-note">
                      <TriangleAlert size={12} />
                      <span>
                        早期采集口径会把情绪分类器的输出混进来，这段文字不完全是她的思考。
                      </span>
                    </p>
                  ) : null}
                  {reasoning ? (
                    <pre className="tr-reasoning">{reasoning}</pre>
                  ) : (
                    <div className="tr-empty">这一回合没有留下思维链</div>
                  )}
                </Section>
              ) : null}

              {trace ? (
                <Section
                  icon={Wrench}
                  title="她做了什么"
                  count={tools.length}
                  open={expanded.tools}
                  onToggle={toggle('tools')}
                  badge={
                    toolEffects && (toolEffects.counts.empty ?? 0) > 0 ? (
                      <span
                        className="tr-badge is-warn"
                        title={toolEffects.warnings.map((w) => w.text).join('\n')}
                      >
                        {toolEffects.counts.empty} 次空手
                      </span>
                    ) : null
                  }
                >
                  {tools.length === 0 ? (
                    <div className="tr-empty">这一回合没调用工具</div>
                  ) : (
                    <>
                      {toolEffects ? <ToolSummary effects={toolEffects} /> : null}
                      <div className="tr-tools">
                        {tools.map((step, i) => (
                          <ToolStepView
                            key={`${step.name}-${i}`}
                            step={step}
                            index={i}
                            /* 与后端同源同序（都从 trace.tools 派生），按位置对齐最准 */
                            effect={toolEffects?.items[i]}
                          />
                        ))}
                      </div>
                    </>
                  )}
                </Section>
              ) : null}

              {decision ? (
                <Section
                  icon={Gavel}
                  title="她决定了什么"
                  open={expanded.decision}
                  onToggle={toggle('decision')}
                  badge={
                    decision.error_code ? (
                      <span className="tr-badge is-error" title={decision.error_code}>
                        出错 {decision.error_code}
                      </span>
                    ) : null
                  }
                >
                  <div className="tr-kv">
                    <span>决定</span>
                    <b>{decisionLabel(decision.decision)}</b>
                  </div>
                  <div className="tr-kv">
                    <span>状态</span>
                    <b className={decision.status === 'failed' ? 'tr-bad' : undefined}>
                      {statusLabel(decision.status)}
                    </b>
                  </div>
                  {/* 她的理由：从决策类工具的入参里取的原文。
                      这是「她凭什么这么决定」唯一的实证——原来界面只有
                      「决定=沉默」两个字母，运营者无从判断沉默得对不对。 */}
                  {toolEffects?.rationale ? (
                    <blockquote className="tr-rationale">
                      <Quote size={11} />
                      <span>{toolEffects.rationale}</span>
                    </blockquote>
                  ) : null}
                  {decision.tool_calls.length > 0 ? (
                    <div className="tr-pill-row">
                      {decision.tool_calls.map((name, i) => (
                        <span className="tr-pill" key={`${name}-${i}`}>
                          {name}
                        </span>
                      ))}
                    </div>
                  ) : null}
                  <div className="tr-kv">
                    <span>开始</span>
                    <b title={decision.started_at}>{formatClock(decision.started_at)}</b>
                  </div>
                  <div className="tr-kv">
                    <span>结束</span>
                    <b title={decision.finished_at}>{formatClock(decision.finished_at)}</b>
                  </div>
                  {elapsedMs(decision.started_at, decision.finished_at) > 0 ? (
                    <div className="tr-kv">
                      <span>决策耗时</span>
                      <b>{formatMs(elapsedMs(decision.started_at, decision.finished_at))}</b>
                    </div>
                  ) : null}
                  {decision.error_code ? (
                    <div className="tr-kv">
                      <span>错误码</span>
                      <b className="tr-bad" title="这一轮中途出错时的错误标识">
                        {decision.error_code}
                      </b>
                    </div>
                  ) : null}
                  {decision.run_id ? (
                    <div className="tr-kv">
                      <span>回合号</span>
                      <b className="tr-mono" title="同一回合的思维链、工具、决策都按这个号关联">
                        {decision.run_id}
                      </b>
                    </div>
                  ) : null}
                </Section>
              ) : null}

              {feedback ? (
                <Section
                  icon={MessageCircle}
                  title="对方怎么反应"
                  open={expanded.feedback}
                  onToggle={toggle('feedback')}
                >
                  <div className="tr-fb-head">
                    <span
                      className={`tr-senti is-${feedback.sentiment || 'unknown'}`}
                    >
                      {sentimentLabel(feedback.sentiment)}
                    </span>
                    <span className="tr-fb-conf">
                      置信度 {formatPct(feedback.confidence)}
                    </span>
                  </div>
                  <div className="tr-kv">
                    <span>反应延迟</span>
                    <b>{formatMs(feedback.reply_ms)}</b>
                  </div>
                  {feedback.timestamp ? (
                    <div className="tr-kv">
                      <span>记录时间</span>
                      <b>{feedback.timestamp}</b>
                    </div>
                  ) : null}
                  {feedback.user_reaction ? (
                    <blockquote className="tr-quote">
                      <Quote size={11} />
                      <span>{feedback.user_reaction}</span>
                    </blockquote>
                  ) : (
                    <div className="tr-empty">对方还没接话</div>
                  )}
                  <div className="tr-note">
                    {feedback.direction_verified
                      ? '方向已核验：这句话确实是对她说的'
                      : '方向未核验：不确定这句话是不是冲她来的'}
                  </div>
                </Section>
              ) : null}

              {!hasAnySection ? (
                <div className="tr-empty-state">
                  这一回合只留下了消息本身，没有可回放的过程记录
                </div>
              ) : null}
            </>
          )}
        </div>
      </aside>
    </div>
  )
}

/* 具名导出与 default 指向同一个组件：挂载方两种写法都能用 */
export { TurnReplay }
