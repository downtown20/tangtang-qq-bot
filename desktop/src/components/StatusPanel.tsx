import { useMemo, useState } from 'react'
import {
  Activity,
  ArrowUpRight,
  Brain,
  ChevronDown,
  CircleDot,
  Cpu,
  Database,
  Flame,
  Heart,
  RefreshCw,
  Users,
  Zap,
} from 'lucide-react'
import type { ConsoleSnapshot } from '../lib/api'
import {
  energyLabel,
  expText,
  fmtCloseness,
  fmtUptime,
  moodTintLabel,
  pctOf,
  shortStamp,
  tier100,
} from '../lib/format'
import './StatusPanel.css'

/* ═══════════════════════════════════════════════════════════════
   「糖糖状态」面板（2026-10-25 第 45 轮重做）

   ## 为什么重做

   主人原话：「可视化低、可操作性低……『糖糖状态』这部分一直没动。」
   取证结论：**后端数据一个不缺**（`GET /snapshot` 早就返回 mood / 6 种驱动力 /
   主导 / 自我叙事 / 价值倾向 / 289 人的关系场 / 22669 条记忆 / 近 5 次体验），
   前端也早在调（`Console.tsx:592`）。问题不是缺数据，是**呈现语法贫乏**：
   9 个 section 等权重平铺、数字无锚、只能看不能点。

   ## 三层信息架构（产品经理视角定的）

     1. **结论层**：一句话——「精力偏低 · 心情很好 —— 此刻被『承诺压力』推着」
        （判据：主人打开这一页 5 秒内能不能说出「她现在怎么样」）
     2. **现状层**：情绪三卡（大数字 + 档位 + 量程）、驱动力排序条（主导高亮）
     3. **溯源层**：关系场分布、记忆、自我叙事、最近经历

   ## 可视化语法（前端分析师视角定的）

   | 数据形状 | 图形 |
   |---|---|
   | 单标量 + 量程（精力 31/100） | 仪表卡：大数字 + 档位词 + 迷你条 |
   | 6 个可比标量（驱动力） | 排序条形图 + 主导高亮 + 急躁标记 |
   | 比例分布（289 人） | 堆叠条 + 图例 |
   | 时间线（体验 5 条） | 时间轴 |
   | 长文本（叙事） | 引用块 + 折叠 |

   ## 纪律

   · **不编数据**：后端没给的（例如驱动力阈值）就不画，不为了好看造假刻度。
   · **每个数字都要有锚**：量程（/100）、档位词（偏低）、或对照（总量/占比）。
   · **空态要说话**：`snapshot` 每个分区独立 try，可能为 null；每块都要有降级文案。
   · **两处共用**：`variant='panel'`（会客厅右栏 320px）与 `'full'`（概览页宽版）
     是**同一个组件**，只是密度不同——否则两份实现必然漂移（教训 #47）。

   ## 讲解模式

   带 `explain-only` 的段落是解释性文字，被标题栏「讲解」开关控制。
   数值、图形、结论、异常原因**永远显示**（见 `lib/uiPrefs.ts` 的纪律说明）。
   ═══════════════════════════════════════════════════════════════ */

/** 档位词（只是显示分档，不是业务判据——见 `lib/format.ts::tier100`） */
const ENERGY_LABELS = { low: '偏低', mid: '一般', high: '充足' }
const MOOD_LABELS = { low: '低落', mid: '平稳', high: '很好' }
const PATIENCE_LABELS = { low: '紧张', mid: '一般', high: '充足' }

const MOOD_ROWS = [
  {
    key: 'energy' as const,
    label: '精力',
    labels: ENERGY_LABELS,
    hint: '精力低的时候她更容易累、句子更短，不太会主动挑起新话题。',
  },
  {
    key: 'mood' as const,
    label: '心情',
    labels: MOOD_LABELS,
    hint: '心情决定语气：低落时更容易被一句玩笑戳到，很好的时候爱闹。',
  },
  {
    key: 'patience' as const,
    label: '耐心',
    labels: PATIENCE_LABELS,
    hint: '耐心见底时她会更直接地结束话题，不是针对谁。',
  },
]

export type StatusPanelProps = {
  snapshot: ConsoleSnapshot | null
  /** 首屏加载中（没有数据时才显示骨架） */
  loading?: boolean
  /** 刷新按钮：会客厅有轮询，概览页靠它手动拉 */
  onRefresh?: () => void
  /** 去记忆图谱（真实存在的路由，不造假按钮） */
  onOpenMemory?: () => void
  /** panel = 会客厅窄右栏；full = 概览页宽版 */
  variant?: 'panel' | 'full'
}

/* ── 小件 ────────────────────────────────────────────────────── */

function Bar({
  value,
  full,
  tone,
}: {
  value: number
  full: number
  tone: 'energy' | 'mood' | 'patience' | 'drive' | 'relation'
}) {
  return (
    <span className="sp-bar" aria-hidden="true">
      <i className={`sp-bar-fill is-${tone}`} style={{ width: `${pctOf(value, full)}%` }} />
    </span>
  )
}

function SectionHead({
  icon: Icon,
  title,
  note,
  action,
}: {
  icon: typeof Heart
  title: string
  note?: string
  action?: { label: string; onClick: () => void }
}) {
  return (
    <div className="sp-sec-head">
      <Icon size={12} className="sp-sec-icon" />
      <span className="sp-sec-title">{title}</span>
      {note ? <span className="sp-sec-note">{note}</span> : null}
      {action ? (
        <button type="button" className="sp-sec-action" onClick={action.onClick}>
          {action.label}
          <ArrowUpRight size={10} />
        </button>
      ) : null}
    </div>
  )
}

/* ── ① 结论层：一句话 ────────────────────────────────────────── */

function Headline({ snapshot }: { snapshot: ConsoleSnapshot }) {
  const mood = snapshot.mood
  const dominant = snapshot.dominant
  const urgent = snapshot.drives.filter((d) => d.urgent)

  const parts: string[] = []
  if (mood) {
    parts.push(`精力${tier100(mood.energy, ENERGY_LABELS).text}`)
    parts.push(`心情${tier100(mood.mood, MOOD_LABELS).text}`)
  }
  const tail = dominant
    ? `此刻被「${dominant.label}」推着`
    : '此刻没有哪股驱动力占上风'

  const tone =
    urgent.length > 0 ? 'busy' : mood && mood.energy < 35 ? 'low' : 'calm'

  return (
    <div className={`sp-headline is-${tone}`}>
      <Activity size={13} className="sp-headline-icon" />
      <div className="sp-headline-main">
        <div className="sp-headline-text">
          {parts.length > 0 ? <b>{parts.join(' · ')}</b> : null}
          {parts.length > 0 ? <span className="sp-headline-sep"> —— </span> : null}
          <span>{tail}</span>
        </div>
        {urgent.length > 0 ? (
          <div className="sp-headline-sub">
            {urgent.length} 项驱动力已经急躁：{urgent.map((d) => d.label).join('、')}
          </div>
        ) : null}
      </div>
    </div>
  )
}

/* ── ② 现状层：情绪三卡 ──────────────────────────────────────── */

function MoodCards({ snapshot }: { snapshot: ConsoleSnapshot }) {
  const mood = snapshot.mood
  if (!mood) return <div className="sp-empty">暂无情绪数据</div>
  return (
    <div className="sp-mood">
      {MOOD_ROWS.map((row) => {
        const v = mood[row.key]
        const tier = tier100(v, row.labels)
        return (
          <div className={`sp-mood-card is-${tier.tone}`} key={row.key}>
            <div className="sp-mood-top">
              <span className="sp-mood-label">{row.label}</span>
              <span className="sp-mood-tier">{tier.text}</span>
            </div>
            <div className="sp-mood-value">
              {Number.isFinite(v) ? Math.round(v) : '—'}
              <span className="sp-mood-full">/100</span>
            </div>
            <Bar value={v ?? 0} full={100} tone={row.key} />
            <p className="sp-mood-hint explain-only">{row.hint}</p>
          </div>
        )
      })}
    </div>
  )
}

/* ── ② 现状层：驱动力排序条 ──────────────────────────────────── */

function DriveBars({ snapshot }: { snapshot: ConsoleSnapshot }) {
  const dominantKey = snapshot.dominant?.key || ''
  // 排序而不是原样平铺：6 根等长条看不出「谁在推她」，排序后才是一张排行
  const rows = useMemo(
    () => [...snapshot.drives].sort((a, b) => b.value - a.value),
    [snapshot.drives],
  )
  if (rows.length === 0) return <div className="sp-empty">暂无驱动力数据</div>
  return (
    <div className="sp-drives">
      {rows.map((d) => {
        const isDominant = d.key === dominantKey
        return (
          <div
            className={
              'sp-drive' +
              (isDominant ? ' is-dominant' : '') +
              (d.urgent ? ' is-urgent' : '') +
              (d.active ? ' is-active' : '')
            }
            key={d.key}
            title={d.intensity || undefined}
          >
            <div className="sp-drive-row">
              <span className="sp-drive-label">
                {isDominant ? <Flame size={10} className="sp-drive-flame" /> : null}
                {d.label}
                {d.urgent ? <span className="sp-tag is-urgent">急躁</span> : null}
                {!d.active ? <span className="sp-tag is-muted">未激活</span> : null}
              </span>
              <span className="sp-drive-val">{d.value.toFixed(2)}</span>
            </div>
            <Bar value={d.value} full={1} tone="drive" />
            {d.intensity ? <div className="sp-drive-hint">{d.intensity}</div> : null}
          </div>
        )
      })}
    </div>
  )
}

/* ── ③ 溯源层：关系场 ────────────────────────────────────────── */

function RelationBlock({
  snapshot,
  variant,
}: {
  snapshot: ConsoleSnapshot
  variant: 'panel' | 'full'
}) {
  const [openQq, setOpenQq] = useState('')
  const rels = snapshot.relationships
  if (!rels) return <div className="sp-empty">暂无关系场数据</div>
  const other = Math.max(0, rels.total - rels.close - rels.familiar)
  const pct = (n: number) => (rels.total > 0 ? (n / rels.total) * 100 : 0)
  const top = variant === 'full' ? rels.top.slice(0, 8) : rels.top.slice(0, 5)

  return (
    <>
      {/* 堆叠条：289 人里 12% 是知己——一行文字说不出来，一条比例条可以 */}
      <div className="sp-stack" role="img" aria-label="关系分布">
        <span className="sp-stack-seg is-close" style={{ width: `${pct(rels.close)}%` }} />
        <span className="sp-stack-seg is-familiar" style={{ width: `${pct(rels.familiar)}%` }} />
        <span className="sp-stack-seg is-other" style={{ width: `${pct(other)}%` }} />
      </div>
      <div className="sp-legend">
        <span className="sp-legend-item is-close">
          <i aria-hidden="true" />知己 {rels.close}
        </span>
        <span className="sp-legend-item is-familiar">
          <i aria-hidden="true" />熟悉 {rels.familiar}
        </span>
        <span className="sp-legend-item is-other">
          <i aria-hidden="true" />其他 {other}
        </span>
      </div>

      {top.length === 0 ? (
        <div className="sp-empty">还没有记下的人</div>
      ) : (
        <div className="sp-rels">
          {top.map((r) => {
            const open = openQq === r.qq
            return (
              <button
                type="button"
                className={'sp-rel' + (open ? ' is-open' : '')}
                key={r.qq}
                onClick={() => setOpenQq(open ? '' : r.qq)}
                title="点一下看详情"
              >
                <span className="sp-rel-name">{r.nickname || r.qq}</span>
                <span className="sp-rel-bar">
                  <i style={{ width: `${pctOf(r.closeness, 1)}%` }} />
                </span>
                <span className="sp-rel-val">{fmtCloseness(r.closeness)}</span>
                <ChevronDown size={10} className={'sp-rel-caret' + (open ? ' is-open' : '')} />
                {open ? (
                  <span className="sp-rel-detail">
                    QQ {r.qq}
                    {r.tier ? ` · ${r.tier}` : ''}
                  </span>
                ) : null}
              </button>
            )
          })}
        </div>
      )}
    </>
  )
}

/* ── ③ 溯源层：记忆 ──────────────────────────────────────────── */

function MemoryBlock({
  snapshot,
  onOpenMemory,
}: {
  snapshot: ConsoleSnapshot
  onOpenMemory?: () => void
}) {
  const mem = snapshot.memory
  if (!mem) return <div className="sp-empty">暂无记忆数据</div>
  const recent = mem.recent.slice(0, 3)
  return (
    <>
      <div className="sp-mem-total">
        <b>{mem.total.toLocaleString('zh-CN')}</b>
        <span>条记忆</span>
        {onOpenMemory ? (
          <button type="button" className="sp-mem-link" onClick={onOpenMemory}>
            看图谱
            <ArrowUpRight size={10} />
          </button>
        ) : null}
      </div>
      {recent.length === 0 ? (
        <div className="sp-empty">还没有记忆</div>
      ) : (
        <div className="sp-memos">
          {recent.map((m, i) => (
            <div className="sp-memo" key={i}>
              <span className="sp-memo-text">{m.text}</span>
              <span className="sp-memo-meta">
                {m.importance ? <span className="sp-memo-imp">重 {m.importance}</span> : null}
                <span className="sp-memo-time">{shortStamp(m.time)}</span>
              </span>
            </div>
          ))}
        </div>
      )}
    </>
  )
}

/* ── ③ 溯源层：自我叙事（长文本折叠） ────────────────────────── */

function NarrativeBlock({ snapshot }: { snapshot: ConsoleSnapshot }) {
  const [open, setOpen] = useState(false)
  const self = snapshot.self
  if (!self) return <div className="sp-empty">暂无自我状态数据</div>
  const narrative = self.narrative || '（还没有形成叙事）'
  const long = narrative.length > 90
  const values = (self.values || '')
    .split(/[；;]/)
    .map((s) => s.trim())
    .filter(Boolean)
    .slice(0, 6)

  return (
    <>
      <div className="sp-tags">
        {self.energy ? <span className="sp-tag is-dim">能量 {energyLabel(self.energy)}</span> : null}
        {self.mood_tint ? (
          <span className="sp-tag is-dim">基调 {moodTintLabel(self.mood_tint)}</span>
        ) : null}
      </div>
      <blockquote className={'sp-quote' + (open || !long ? ' is-open' : '')}>
        {open || !long ? narrative : narrative.slice(0, 88) + '…'}
      </blockquote>
      {long ? (
        <button type="button" className="sp-more" onClick={() => setOpen((v) => !v)}>
          {open ? '收起' : '展开全文'}
          <ChevronDown size={10} className={'sp-rel-caret' + (open ? ' is-open' : '')} />
        </button>
      ) : null}
      {values.length > 0 ? (
        <>
          {/* 分区标题不是「解释」——它标明这堆 chips 是什么，关讲解也要留 */}
          <div className="sp-sub-title">价值倾向</div>
          <div className="sp-value-chips">
            {values.map((v, i) => (
              <span className="sp-value-chip" key={i}>
                {v}
              </span>
            ))}
          </div>
        </>
      ) : null}
    </>
  )
}

/* ── ③ 溯源层：最近经历（时间轴） ────────────────────────────── */

function ExperienceTimeline({
  snapshot,
  limit,
}: {
  snapshot: ConsoleSnapshot
  limit: number
}) {
  const self = snapshot.self
  // 空摘要的体验（她自己的内部活动）不显示——时间轴上一条「（空）· 20:27」没有意义
  const items = (self?.experiences || []).filter((x) => (x.summary || '').trim()).slice(0, limit)
  if (items.length === 0) return <div className="sp-empty">还没有可展示的近期经历</div>
  return (
    <ol className="sp-timeline">
      {items.map((x, i) => (
        <li className="sp-tl-item" key={i}>
          <span className="sp-tl-dot" aria-hidden="true" />
          <span className="sp-tl-text">{expText(x)}</span>
          <span className="sp-tl-time">{shortStamp(x.time)}</span>
        </li>
      ))}
    </ol>
  )
}

/* ══════════════════════════════════════════════════════════════
   主组件
   ══════════════════════════════════════════════════════════════ */

export default function StatusPanel({
  snapshot,
  loading = false,
  onRefresh,
  onOpenMemory,
  variant = 'panel',
}: StatusPanelProps) {
  if (!snapshot) {
    return (
      <div className={`sp-panel is-${variant} is-empty`}>
        <div className="sp-loading">
          <Brain size={15} className={loading ? 'spin' : ''} />
          {loading ? '读取中…' : '还没有状态数据'}
        </div>
      </div>
    )
  }

  const rels = snapshot.relationships
  const model = snapshot.model
  const turn = snapshot.turn

  return (
    <div className={`sp-panel is-${variant}`}>
      {onRefresh ? (
        <div className="sp-toolbar">
          <span className="sp-toolbar-note">
            {snapshot.bot?.uptime_s ? `已运行 ${fmtUptime(snapshot.bot.uptime_s)}` : ''}
          </span>
          <button
            type="button"
            className="sp-refresh"
            title="刷新状态"
            onClick={onRefresh}
          >
            <RefreshCw size={11} className={loading ? 'spin' : ''} />
            刷新
          </button>
        </div>
      ) : null}

      <Headline snapshot={snapshot} />

      <section className="sp-sec">
        <SectionHead icon={Heart} title="情绪" note="0~100" />
        <MoodCards snapshot={snapshot} />
      </section>

      <section className="sp-sec">
        <SectionHead
          icon={Zap}
          title="驱动力"
          note={`${snapshot.drives.filter((d) => d.active).length}/${snapshot.drives.length} 在活跃`}
        />
        <DriveBars snapshot={snapshot} />
        {snapshot.dominant ? (
          <div className="sp-dominant">
            <CircleDot size={11} className="sp-dominant-icon" />
            <span>
              占上风：<b>{snapshot.dominant.label}</b>
              {snapshot.dominant.text ? ` · ${snapshot.dominant.text}` : ''}
            </span>
          </div>
        ) : null}
      </section>

      <section className="sp-sec">
        <SectionHead
          icon={Users}
          title="关系场"
          note={rels ? `${rels.total} 人` : undefined}
        />
        <RelationBlock snapshot={snapshot} variant={variant} />
      </section>

      <section className="sp-sec">
        <SectionHead icon={Database} title="记忆" />
        <MemoryBlock snapshot={snapshot} onOpenMemory={onOpenMemory} />
      </section>

      <section className="sp-sec">
        <SectionHead icon={Brain} title="自我叙事" note="她怎么理解自己" />
        <NarrativeBlock snapshot={snapshot} />
      </section>

      <section className="sp-sec">
        <SectionHead icon={Activity} title="最近经历" note="她记得的最近几件事" />
        <ExperienceTimeline snapshot={snapshot} limit={variant === 'full' ? 8 : 4} />
      </section>

      <section className="sp-sec sp-sec-meta">
        <SectionHead icon={Cpu} title="模型" />
        <div className="sp-kv">
          <span>Provider</span>
          <b>{model?.provider || '—'}</b>
        </div>
        <div className="sp-kv">
          <span>Model</span>
          <b>{model?.model || '—'}</b>
        </div>
        {turn ? (
          <div className="sp-turn">
            上一回合 · {Math.round(turn.ms)} ms
            {turn.tokens > 0 ? ` · ${turn.tokens} tokens` : ''}
            {turn.autonomous ? ' · 自主开口' : ''}
          </div>
        ) : null}
      </section>
    </div>
  )
}

export { StatusPanel }
