import { useCallback, useEffect, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import { ArrowLeft, ChevronRight, LoaderCircle, RefreshCw, TriangleAlert, X } from 'lucide-react'
import { api } from '../lib/api'
import type {
  DimItem,
  MemoryCapabilities,
  MemoryProfile,
  MemoryProfileAddresses,
  MemoryProfileAlias,
  MemoryProfileCluster,
  MemoryProfileCounts,
  MemoryProfileEpisode,
  MemoryProfileMemory,
  MemoryTimelinePoint,
} from '../lib/api'
import MemoryHistory, { type OpenedMemory } from './MemoryHistory'
import './MemoryProfile.css'

/* ═══════════════════════════════════════════════════════════════
   记忆剖面 —— 把「一张图」拆成两层正交的结构

    横向：memories 表上七个正交索引（记忆的七种「规格」）
    纵向：L0 原话 → L1 证据 → L2 记忆 → L3 语义簇 → L4 画像/经历
          → L5 长期叙事（数据从原始到抽象的流向）

   类比 CPU 架构：维度是「规格表」，层是「数据通路」。
   所有节点/统计由后端聚合，前端只做展示与「按维度筛选 L2」。
   ═══════════════════════════════════════════════════════════════ */

/* ── 七维索引 ────────────────────────────────────────────────
   前六个可筛选；importance 只有分布（L2 样本不带全量），只展示。
   ──────────────────────────────────────────────────────────── */
const DIM_ORDER = ['key', 'cognitive', 'retention', 'trust', 'status', 'origin'] as const
type FilterDim = (typeof DIM_ORDER)[number]
type Filters = Partial<Record<FilterDim, string>>

const DIM_TITLES: Record<FilterDim, string> = {
  key: '记忆类型',
  cognitive: '认知类型',
  retention: '保留策略',
  trust: '可信度',
  status: '生命周期',
  origin: '来源',
}

const DIM_HINTS: Record<FilterDim, string> = {
  key: '记忆的语法类型：event / said / fact / promise …',
  cognitive: '情景记忆（发生过的事）vs 语义记忆（抽象结论）',
  retention: '缓存层级：permanent 永久 / durable 持久 / normal 普通 / transient 短暂',
  trust: '数据校验状态：corrected / verified / unverified',
  status: '生命周期：active / superseded / retracted / fulfilled',
  origin: '这条记忆从哪来：extracted / self / summarized / backfill',
}

/** 值 → 中文（未知值原样显示，别吞数据） */
const VALUE_LABELS: Record<string, string> = {
  // cognitive
  episodic: '情景',
  semantic: '语义',
  // retention
  permanent: '永久',
  durable: '持久',
  normal: '普通',
  transient: '短暂',
  temporary: '临时',
  // trust
  corrected: '已纠正',
  verified: '已验证',
  unverified: '未验证',
  legacy_unverified: '旧版未验证',
  // status
  active: '活跃',
  superseded: '已被推翻',
  retracted: '已撤回',
  fulfilled: '已兑现',
  // origin
  extracted: '对话提取',
  self: '自我生成',
  summarized: '摘要合成',
  backfill: '补录',
  corrected_origin: '纠正',
  // key（memories.key 是英文枚举）
  event: '事件',
  said: '说过的话',
  fact: '事实',
  promise: '承诺',
  like: '喜好',
  dislike: '不喜欢',
  hate: '厌恶',
  opinion: '观点',
  habit: '习惯',
  preference: '偏好',
  relationship: '关系',
  plan: '计划',
  identity: '身份',
  feeling: '感受',
  // 合成类 key（画像重合成 / 事实合成 等，演化史与 L2 都会遇到）
  fact_synthesis: '事实合成',
  profile_synthesis: '画像重合成',
  fact_correction: '事实纠错',
  action_completed: '行动完成',
  recommend: '推荐',
  profile: '画像',
  未标注: '未标注',
}

const TRUST_LABELS: Record<string, string> = {
  manual: '手动录入',
  verified: '已验证',
  corrected: '已纠正',
  legacy_unverified: '旧版未验证',
  unverified: '未验证',
}

type LayerKey = 'L2' | 'L3' | 'L4' | 'L5' | 'L1' | 'L0'

/** L2 最常用，默认展开；其余层收起，页面才有一眼可扫的骨架 */
const DEFAULT_LAYERS: Record<LayerKey, boolean> = {
  L2: true,
  L3: false,
  L4: false,
  L5: false,
  L1: false,
  L0: false,
}

/** 一层最多渲染多少条记忆（超过给「显示全部」按钮，避免一次挂几百张卡） */
const MEMORY_PAGE = 20
/** 证据原话在卡片里最多显示多少字 */
const EVIDENCE_CLIP = 200
/** 维度卡默认显示多少项 */
const DIM_PREVIEW = 6

/* ── 小工具 ── */

function clamp(value: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, value))
}

function formatCount(value: number): string {
  return Number.isFinite(value) ? Math.max(0, Math.round(value)).toLocaleString() : '—'
}

function formatWhen(raw: string): string {
  if (!raw) return ''
  return raw.replace('T', ' ').slice(0, 16)
}

/** 0~1 → 百分比；后端给百分数也认 */
function formatPercent(value: number): string {
  if (!Number.isFinite(value)) return '—'
  const pct = value <= 1 ? value * 100 : value
  return `${Math.round(clamp(pct, 0, 100))}%`
}

/** first_met → 「认识 N 天 / N 个月 / N.n 年」 */
function formatSince(raw: string): string {
  if (!raw) return ''
  const start = Date.parse(raw.replace(' ', 'T'))
  if (Number.isNaN(start)) return ''
  const days = Math.floor((Date.now() - start) / 86400000)
  if (days < 0) return ''
  if (days < 1) return '今天刚认识'
  if (days < 60) return `认识 ${days} 天`
  const months = Math.floor(days / 30)
  if (months < 24) return `认识 ${months} 个月`
  return `认识 ${(days / 365).toFixed(1)} 年`
}

function truncate(text: string, limit: number): string {
  if (text.length <= limit) return text
  return `${text.slice(0, limit)}…`
}

function metaStr(meta: Record<string, string | number>, key: string): string {
  const value = meta[key]
  if (typeof value === 'string') return value
  if (typeof value === 'number') return String(value)
  return ''
}

function metaNum(meta: Record<string, string | number>, key: string): number | null {
  const value = meta[key]
  if (typeof value === 'number') return Number.isFinite(value) ? value : null
  if (typeof value === 'string' && value.trim() !== '') {
    const parsed = Number(value)
    return Number.isFinite(parsed) ? parsed : null
  }
  return null
}

function valueLabel(name: string): string {
  if (!name) return '未标注'
  return VALUE_LABELS[name] ?? name
}

function truthyNumber(value: number | boolean): boolean {
  return typeof value === 'boolean' ? value : value !== 0
}

/* ── 「按维度筛选 L2」─────────────────────────────────────────
   ⚠️ 契约缺口（2026-10-24 实测）：`layers.memories` 的样本只带
   key / origin，不带 cognitive / retention / trust / status。
   所以这里做成「样本带这个字段才真过滤」：字段一到就自动生效，
   现在那四个维度只统计、不逐条筛（UI 上会写清楚，不假装筛了）。
   ──────────────────────────────────────────────────────────── */
type MemoryRowExtras = {
  cognitive?: string
  retention?: string
  trust?: string
  trust_level?: string
  status?: string
}

function readMemoryDim(memory: MemoryProfileMemory, dim: FilterDim): string | undefined {
  if (dim === 'key') return memory.key
  if (dim === 'origin') return memory.origin
  const extra = memory as MemoryProfileMemory & MemoryRowExtras
  if (dim === 'cognitive') return extra.cognitive
  if (dim === 'retention') return extra.retention
  if (dim === 'trust') return extra.trust ?? extra.trust_level
  return extra.status
}

function buildSupport(memories: MemoryProfileMemory[]): Record<FilterDim, boolean> {
  const support: Record<FilterDim, boolean> = {
    key: false,
    cognitive: false,
    retention: false,
    trust: false,
    status: false,
    origin: false,
  }
  for (const memory of memories) {
    for (const dim of DIM_ORDER) {
      if (!support[dim] && readMemoryDim(memory, dim) !== undefined) support[dim] = true
    }
  }
  return support
}

function filterMemories(
  memories: MemoryProfileMemory[],
  filters: Filters,
  support: Record<FilterDim, boolean>,
): MemoryProfileMemory[] {
  const active = DIM_ORDER.filter((dim) => Boolean(filters[dim]) && support[dim])
  if (active.length === 0) return memories
  return memories.filter((memory) =>
    active.every((dim) => {
      const wanted = filters[dim]
      const actual = readMemoryDim(memory, dim) ?? '未标注'
      return wanted === actual
    }),
  )
}

/* ═══════════════════════════════════════════════════════════════
   小组件
   ═══════════════════════════════════════════════════════════════ */

/** 维度卡：标题 + 总数 + 「全部」，值行带比例条，可折叠到前 6 项 */
function DimCard({
  dim,
  title,
  hint,
  items,
  selected,
  onPick,
  open,
  onToggle,
}: {
  dim: FilterDim
  title: string
  hint: string
  items: DimItem[]
  selected: string | null
  onPick: (value: string) => void
  open: boolean
  onToggle: () => void
}) {
  const total = items.reduce((sum, item) => sum + item.count, 0)
  const max = items.reduce((top, item) => Math.max(top, item.count), 0)
  const visible = open ? items : items.slice(0, DIM_PREVIEW)

  return (
    <section className={'mp-dim' + (selected ? ' is-filtered' : '')}>
      <div className="mp-dim-head">
        <span className="mp-dim-title" title={hint}>
          {title}
        </span>
        <span className="mp-dim-total">{formatCount(total)}</span>
        <button
          type="button"
          className={'mp-dim-all' + (selected === null ? ' is-on' : '')}
          onClick={() => onPick('')}
          title="清除这个维度的筛选"
        >
          全部
        </button>
      </div>

      {items.length === 0 ? (
        <div className="mp-empty-line">没有数据</div>
      ) : (
        <div className="mp-dim-list">
          {visible.map((item) => {
            const picked = selected === item.name
            return (
              <button
                type="button"
                key={`${dim}-${item.name}`}
                className={'mp-dim-row' + (picked ? ' is-on' : '')}
                onClick={() => onPick(picked ? '' : item.name)}
                title={`${item.name} · ${item.count} 条`}
                aria-pressed={picked}
              >
                <span className="mp-dim-name">{valueLabel(item.name)}</span>
                <span className="mp-dim-bar">
                  <i style={{ width: `${max > 0 ? (item.count / max) * 100 : 0}%` }} />
                </span>
                <span className="mp-dim-count">{formatCount(item.count)}</span>
              </button>
            )
          })}
          {items.length > DIM_PREVIEW ? (
            <button type="button" className="mp-dim-more" onClick={onToggle}>
              {open ? '收起' : `展开全部 ${formatCount(items.length)} 项`}
            </button>
          ) : null}
        </div>
      )}
    </section>
  )
}

/** 重要度分布：1-10 一排小柱子（只展示，不参与筛选） */
function ImportanceCard({ items }: { items: DimItem[] }) {
  const max = Math.max(1, ...items.map((item) => item.count))
  return (
    <section className="mp-dim">
      <div className="mp-dim-head">
        <span className="mp-dim-title" title="importance：1-10 的分布，只有分布没有逐条数据，因此不筛选">
          重要度
        </span>
        <span className="mp-dim-total">1–10</span>
      </div>
      <div className="mp-bars">
        {items.map((item) => (
          <div className="mp-bar-col" key={item.name} title={`重要度 ${item.name}：${item.count} 条`}>
            <i style={{ height: `${Math.max(2, (item.count / max) * 100)}%` }} />
            <span>{item.name}</span>
          </div>
        ))}
      </div>
    </section>
  )
}

/** 分层卡：L0-L5 的通用壳（层号 + 名称 + 条数 + 折叠） */
function LayerCard({
  code,
  name,
  count,
  note,
  open,
  onToggle,
  children,
}: {
  code: string
  name: string
  count: string
  note?: string
  open: boolean
  onToggle: () => void
  children: ReactNode
}) {
  return (
    <section className={'mp-layer' + (open ? ' is-open' : '')}>
      <button
        type="button"
        className="mp-layer-head"
        onClick={onToggle}
        aria-expanded={open}
      >
        <span className="mp-layer-dot" aria-hidden="true" />
        <span className="mp-layer-tag">{code}</span>
        <span className="mp-layer-name">{name}</span>
        <span className="mp-layer-count">{count}</span>
        {note ? <span className="mp-layer-note">{note}</span> : null}
        <ChevronRight size={13} className={'mp-caret' + (open ? ' is-open' : '')} />
      </button>
      {open ? <div className="mp-layer-body">{children}</div> : null}
    </section>
  )
}

/**
 * 重要度：固定 10 个点位，实心 = importance（1~10 直白映射，不折算百分比）。
 * importance=10 十颗全亮、=1 只有一颗亮，8px 小点一眼能看出密度差。
 */
function ImportanceDots({ value }: { value: number }) {
  const level = clamp(Math.round(value), 0, 10)
  return (
    <span className="mp-imp" title={`重要度 ${level} / 10`} aria-label={`重要度 ${level} / 10`}>
      {Array.from({ length: 10 }, (_, index) => (
        <i key={index} className={index < level ? 'is-on' : ''} />
      ))}
    </span>
  )
}

/** L2 单条记忆：key/重要度/状态 + 原文 + 来源/时间 + 可展开原话 */
function MemoryCard({ memory }: { memory: MemoryProfileMemory }) {
  const [evidenceOpen, setEvidenceOpen] = useState(false)
  const evidence = memory.evidence ?? []
  // status 目前不在 L2 样本里；后端补了就自动出现徽章
  const status = readMemoryDim(memory, 'status')

  const statusClass =
    status === 'superseded' ? ' is-warn' : status === 'retracted' ? ' is-bad' : ''

  return (
    <article className="mp-mem">
      <div className="mp-mem-head">
        <span className="mp-tag">{valueLabel(memory.key)}</span>
        <ImportanceDots value={memory.importance} />
        {status && status !== 'active' ? (
          <span className={'mp-badge' + statusClass}>{valueLabel(status)}</span>
        ) : null}
        <span className="mp-mem-spacer" />
        <span className="mp-mem-origin">{valueLabel(memory.origin)}</span>
        <span className="mp-mem-time">
          {memory.event_time ? formatWhen(memory.event_time) : '时间未知'}
        </span>
      </div>

      <p className="mp-mem-value">{memory.value || '（空记忆）'}</p>

      {evidence.length > 0 ? (
        <>
          <button
            type="button"
            className="mp-mem-ev-btn"
            onClick={() => setEvidenceOpen((value) => !value)}
            aria-expanded={evidenceOpen}
          >
            <ChevronRight size={11} className={'mp-caret' + (evidenceOpen ? ' is-open' : '')} />
            原话 {evidence.length} 条
          </button>
          {evidenceOpen ? (
            <div className="mp-ev-list">
              {evidence.map((item, index) => (
                <blockquote className="mp-ev" key={`${item.chat_id}-${index}`}>
                  <div className="mp-ev-meta">
                    {item.timestamp ? formatWhen(item.timestamp) : '时间未知'}
                    {' · '}
                    {truthyNumber(item.is_bot_reply) ? '糖糖' : '对方'}
                  </div>
                  <p className="mp-ev-text">{truncate(item.message, EVIDENCE_CLIP)}</p>
                </blockquote>
              ))}
            </div>
          ) : null}
        </>
      ) : null}
    </article>
  )
}

/** L3 语义簇：卡片头常显，展开看簇内事实 */
function ClusterCard({ cluster }: { cluster: MemoryProfileCluster }) {
  const [open, setOpen] = useState(false)
  return (
    <article className="mp-cluster">
      <button
        type="button"
        className="mp-cluster-head"
        onClick={() => setOpen((value) => !value)}
        aria-expanded={open}
      >
        <ChevronRight size={12} className={'mp-caret' + (open ? ' is-open' : '')} />
        <span className="mp-tag is-cat">
          {cluster.category_label || cluster.category || '未分类'}
        </span>
        <span className="mp-cluster-title">{cluster.title || '（未命名簇）'}</span>
        <span className="mp-cluster-meta">
          {formatCount(cluster.fact_count)} 条事实
          {cluster.permanence ? ` · ${valueLabel(cluster.permanence)}` : ''}
        </span>
      </button>
      {open ? (
        <div className="mp-cluster-body">
          {cluster.summary ? <p className="mp-cluster-summary">{cluster.summary}</p> : null}
          {cluster.facts.length === 0 ? (
            <div className="mp-empty-line">簇里还没有事实</div>
          ) : (
            <ul className="mp-facts">
              {cluster.facts.map((fact) => (
                <li className="mp-fact" key={fact.id}>
                  <span className="mp-fact-text">{fact.fact}</span>
                  <span className="mp-fact-meta">
                    <span className="mp-conf" title="置信度">
                      {formatPercent(fact.confidence)}
                    </span>
                    <ImportanceDots value={fact.importance} />
                  </span>
                </li>
              ))}
            </ul>
          )}
        </div>
      ) : null}
    </article>
  )
}

/** L4 共同经历：时间线样式（圆点 + 竖线 + 正文） */
function EpisodeItem({ episode }: { episode: MemoryProfileEpisode }) {
  const start = formatWhen(episode.time_start)
  const end = formatWhen(episode.time_end)
  const timeText = start && end && start !== end ? `${start} ~ ${end}` : start || end || '时间未记录'
  return (
    <article className="mp-episode">
      <span className="mp-episode-dot" aria-hidden="true" />
      <div className="mp-episode-main">
        <div className="mp-episode-title">{episode.title || '未命名经历'}</div>
        {episode.summary ? <p className="mp-episode-summary">{episode.summary}</p> : null}
        <div className="mp-episode-time">{timeText}</div>
      </div>
    </article>
  )
}

/** L5 存储能力的一行（层号 + 名称 + 主数值 + 副信息） */
function SpecRow({
  code,
  name,
  value,
  note,
  children,
}: {
  code: string
  name: string
  value: string
  note?: string
  children?: ReactNode
}) {
  return (
    <div className="mp-spec">
      <span className="mp-spec-code">{code}</span>
      <span className="mp-spec-name">{name}</span>
      <span className="mp-spec-value">{value}</span>
      {note ? <span className="mp-spec-note">{note}</span> : null}
      {children ? <div className="mp-spec-extra">{children}</div> : null}
    </div>
  )
}

/** L5 全局存储能力（不限于这个人） */
function CapabilitySpecs({ caps }: { caps: MemoryCapabilities }) {
  return (
    <div className="mp-specs">
      <SpecRow code="L0" name="原始对话" value={`${formatCount(caps.chat_log)} 条`} />
      <SpecRow code="L1" name="证据链" value={`${formatCount(caps.evidence)} 条`} />
      <SpecRow
        code="L2"
        name="记忆"
        value={`${formatCount(caps.memories)} 条`}
        note={`活跃 ${formatCount(caps.memories_active)} · 可注入 ${formatCount(
          caps.trusted_injectable,
        )}`}
      />
      {/* 信任门（2026-10-24 立项研究第三期）：门一直在工作，但「挡了多少」此前
          在任何界面都看不到。把它摆在 L2 后面，是因为「记得多少」和「能用上多少」
          是两个数，只看前者的直觉是错的。 */}
      <SpecRow
        code="⚖"
        name="信任门"
        value={`被挡 ${formatCount(caps.trusted_blocked)} 条`}
        note={`可注入 ${formatCount(caps.trusted_injectable)} 条 · 有证据 ${formatCount(
          caps.memories_with_evidence,
        )}`}
      >
        <div className="mp-cover">
          <div className="mp-cover-bar">
            <i style={{ width: `${clamp(caps.trusted_cover, 0, 1) * 100}%` }} />
          </div>
          <span className="mp-cover-text">
            可信召回覆盖 {formatPercent(caps.trusted_cover)} · 证据覆盖{' '}
            {formatPercent(caps.evidence_cover)}
          </span>
        </div>
      </SpecRow>
      <SpecRow
        code="L2b"
        name="语义向量"
        value={`${formatCount(caps.embeddings)} 条`}
        note={`${formatCount(caps.embedding_dims)} 维`}
      >
        <div className="mp-cover">
          <div className="mp-cover-bar">
            <i style={{ width: `${clamp(caps.embedding_cover, 0, 1) * 100}%` }} />
          </div>
          <span className="mp-cover-text">
            覆盖率 {formatPercent(caps.embedding_cover)} · 失败 {formatCount(caps.embedding_failures)}
          </span>
        </div>
      </SpecRow>
      <SpecRow
        code="L3"
        name="语义簇"
        value={`${formatCount(caps.clusters)} 个`}
        note={`${formatCount(caps.facts)} 条事实`}
      />
      <SpecRow
        code="L4"
        name="画像"
        value={`${formatCount(caps.people)} 人`}
        note={`已成型 ${formatCount(caps.people_profiled)} · 可注入 ${formatCount(
          caps.people_profiled_injectable,
        )} · 经历 ${formatCount(caps.episodes)} 段`}
      />
      <SpecRow
        code="L5"
        name="长期叙事"
        value={`${formatCount(caps.journal)} 篇日记`}
        note={`${formatCount(caps.digests)} 篇摘要`}
      />
      <SpecRow
        code="⚙"
        name="提取流水线"
        value={`${formatCount(caps.extraction_jobs)} 个任务`}
        note={`${formatCount(caps.extraction_cursors)} 个游标`}
      />
      <SpecRow
        code="⟲"
        name="变更留痕"
        value={`${formatCount(caps.revisions ?? 0)} 条`}
        note={
          caps.revision_since
            ? `自 ${caps.revision_since.slice(0, 10)} 起可追溯 · 见「演化史」`
            : "尚未开始记录"
        }
      />
    </div>
  )
}

/** L0 时间线：柱子高度用 count，底部深色叠「真知道发生在哪天」的 by_event */
function TimelineChart({
  timeline,
  undated,
}: {
  timeline: MemoryTimelinePoint[]
  undated: number
}) {
  if (timeline.length === 0) {
    return <div className="mp-empty-line">还没有带日期的记忆</div>
  }
  const max = Math.max(1, ...timeline.map((point) => point.count))
  return (
    <div className="mp-timeline">
      <div className="mp-tl-bars">
        {timeline.map((point) => {
          const byEvent = point.by_event
          const height = Math.max(3, (point.count / max) * 100)
          const eventPart = point.count > 0 ? (byEvent / point.count) * 100 : 0
          const supPart = point.count > 0 ? (point.superseded / point.count) * 100 : 0
          return (
            <div
              className="mp-tl-col"
              key={point.month}
              title={`${point.month}：${point.count} 条（${byEvent} 条有发生日期，${
                point.count - byEvent
              } 条按写入日归档）${
                point.superseded > 0 ? `，其中 ${point.superseded} 条被推翻` : ''
              }`}
            >
              <div className="mp-tl-bar" style={{ height: `${height}%` }}>
                {byEvent > 0 ? (
                  <i className="mp-tl-event" style={{ height: `${Math.max(2, eventPart)}%` }} />
                ) : null}
                {point.superseded > 0 ? (
                  <i className="mp-tl-sup" style={{ height: `${Math.max(3, supPart)}%` }} />
                ) : null}
              </div>
              <span className="mp-tl-label">{point.month.slice(5)}</span>
            </div>
          )
        })}
      </div>
      <div className="mp-tl-legend explain-only">
        <span className="mp-tl-legend-item">
          <i className="mp-tl-key" />
          每月记忆
        </span>
        <span className="mp-tl-legend-item">
          <i className="mp-tl-key is-event" />
          有发生日期
        </span>
        <span className="mp-tl-legend-item">
          <i className="mp-tl-key is-sup" />
          被推翻
        </span>
        {undated > 0 ? (
          <span className="mp-tl-undated">另有 {formatCount(undated)} 条完全无日期</span>
        ) : null}
      </div>
    </div>
  )
}

/* ── 称呼：昵称与自称必须分开、并标明可信度 ──────────────────
   `self_declared` 排最前（对方亲口说的名字最值得先看），但它也可能是
   角色扮演；`auto` 是系统从群名片之类自动记的（有噪音）；`llm` 是模型
   顺手记的。三组用不同标签色区分，绝不合并成一个列表。
   ──────────────────────────────────────────────────────────── */

/**
 * 称呼数据来自后端 `memory_profile.addresses`（强类型，不再是塞在 person 里的
 * 异构字段）。仍然在这里做一次窄化：它是**跨进程边界**的数据，字段缺失或类型
 * 不对时宁可显示空，也不能让整页崩。
 */
function normalizeAddresses(raw: MemoryProfileAddresses | undefined): MemoryProfileAddresses {
  const source: Record<string, unknown> =
    raw && typeof raw === 'object' ? (raw as unknown as Record<string, unknown>) : {}
  const nickname = typeof source.nickname === 'string' ? source.nickname : ''
  const aliases: MemoryProfileAlias[] = []
  if (Array.isArray(source.aliases)) {
    for (const item of source.aliases) {
      if (typeof item !== 'object' || item === null) continue
      const record = item as Record<string, unknown>
      if (typeof record.alias !== 'string') continue
      aliases.push({
        alias: record.alias,
        source: typeof record.source === 'string' ? record.source : '',
        memory_id: typeof record.memory_id === 'number' ? record.memory_id : 0,
        created_at: typeof record.created_at === 'string' ? record.created_at : '',
      })
    }
  }
  return {
    nickname,
    nickname_missing: source.nickname_missing === true || !nickname,
    aliases,
  }
}

const ALIAS_GROUP_ORDER = ['self_declared', 'auto', 'llm', 'unknown'] as const
type AliasGroupKey = (typeof ALIAS_GROUP_ORDER)[number]

const ALIAS_GROUP_META: Record<AliasGroupKey, { title: string; note: string }> = {
  self_declared: { title: '自称', note: '自称可能是角色扮演，不代表本名' },
  auto: { title: '自动记录', note: '系统从群名片等自动记的，可能有噪音' },
  llm: { title: '模型记录', note: '模型当时顺手记的' },
  unknown: { title: '来源未标注', note: '旧数据没写来源' },
}

function aliasGroupKey(source: string): AliasGroupKey {
  if (source === 'self_declared' || source === 'auto' || source === 'llm') return source
  return 'unknown'
}

/** 称呼块：QQ 昵称（可能缺失）+ 自称/外号（按来源分组，自称优先） */
function AliasesBlock({
  nickname,
  nicknameMissing,
  aliases,
  findMemory,
  onOpenMemory,
}: {
  nickname: string
  nicknameMissing: boolean
  aliases: MemoryProfileAlias[]
  findMemory: (memoryId: number) => MemoryProfileMemory | null
  onOpenMemory: (memory: OpenedMemory) => void
}) {
  const groups = ALIAS_GROUP_ORDER.map((key) => ({
    key,
    meta: ALIAS_GROUP_META[key],
    items: aliases.filter((item) => aliasGroupKey(item.source) === key),
  })).filter((group) => group.items.length > 0)

  return (
    <section className="mp-aliases">
      <div className="mp-aliases-head">
        <span className="mp-aliases-title">称呼</span>
        {nickname ? (
          <span className="mp-alias-chip is-nickname" title="QQ 昵称：跨群稳定身份">
            QQ 昵称 {nickname}
          </span>
        ) : nicknameMissing ? (
          <span className="mp-alias-missing">
            <TriangleAlert size={11} />
            昵称未记录（档案创建时来源路径没带昵称，下次她发言会自动补）
          </span>
        ) : null}
      </div>

      {groups.length === 0 ? (
        <span className="mp-alias-empty">还没有记录到自称/外号</span>
      ) : (
        <div className="mp-alias-groups">
          {groups.map((group) => (
            <div className="mp-alias-group" key={group.key}>
              <div className="mp-alias-group-head">
                <span className={'mp-alias-group-title is-' + group.key}>
                  {group.meta.title}
                </span>
                <span className="mp-alias-group-note">{group.meta.note}</span>
              </div>
              <div className="mp-alias-chips">
                {group.items.map((item) => {
                  // 依据记忆能在 L2 样本里找到时，才能打开详情弹层（没有单条接口）
                  const memory = item.memory_id > 0 ? findMemory(item.memory_id) : null
                  const refLabel = item.memory_id > 0 ? `依据记忆 #${item.memory_id}` : ''
                  return (
                    <span
                      className={'mp-alias-chip is-' + group.key}
                      key={`${item.source}-${item.alias}-${item.memory_id}`}
                      title={`来源：${group.meta.title} · 记录于 ${
                        item.created_at || '时间未记录'
                      }${refLabel ? ` · ${refLabel}` : ''}`}
                    >
                      {item.alias}
                      {refLabel ? (
                        memory ? (
                          <button
                            type="button"
                            className="mp-alias-ref is-link"
                            title="打开这条记忆的原文"
                            onClick={() =>
                              onOpenMemory({
                                id: memory.id,
                                value: memory.value,
                                key: memory.key,
                                status: memory.status,
                                time: memory.event_time,
                              })
                            }
                          >
                            {refLabel}
                          </button>
                        ) : (
                          <span className="mp-alias-ref">{refLabel}</span>
                        )
                      ) : null}
                    </span>
                  )
                })}
              </div>
            </div>
          ))}
        </div>
      )}
    </section>
  )
}

/** 身份条：昵称/QQ/亲密度/关系/认识多久/对话次数/画像信任 + 画像原文 */
function IdentityHead({
  person,
  addresses,
  qq,
  notesOpen,
  onToggleNotes,
  findMemory,
  onOpenMemory,
}: {
  person: Record<string, string | number>
  addresses: MemoryProfileAddresses
  qq: string
  notesOpen: boolean
  onToggleNotes: () => void
  findMemory: (memoryId: number) => MemoryProfileMemory | null
  onOpenMemory: (memory: OpenedMemory) => void
}) {
  // 昵称为空时**不拿 QQ 号充数**，也不静默留空——见下方 is-missing + warn 提示
  const extras = normalizeAddresses(addresses)
  const nickname = extras.nickname.trim()
  const nicknameMissing = extras.nickname_missing || !nickname
  const aliases = extras.aliases
  const qqId = metaStr(person, 'qq_id') || qq
  const intimacy = metaNum(person, 'intimacy')
  const relationship = metaStr(person, 'relationship')
  const firstMet = metaStr(person, 'first_met')
  const lastChat = metaStr(person, 'last_chat')
  const totalChats = metaNum(person, 'total_chats')
  const trust = metaStr(person, 'notes_trust_level')
  const notes = metaStr(person, 'notes')
  // 后端用 "" / "0" 表示「没记录」——0 不是「零岁」，整段藏掉别糊弄人
  const birthday = metaStr(person, 'birthday').trim()
  const sexRaw = metaStr(person, 'sex').trim()
  const ageRaw = metaNum(person, 'age')
  const age = ageRaw !== null && ageRaw > 0 ? ageRaw : null
  const since = formatSince(firstMet)

  const sexLabel =
    sexRaw === 'female' || sexRaw === '女'
      ? '女'
      : sexRaw === 'male' || sexRaw === '男'
        ? '男'
        : sexRaw
  const trustWarn = trust === 'legacy_unverified'

  return (
    <>
      <div className="mp-identity">
        <div className="mp-id-main">
          {nickname ? (
            <div className="mp-id-name">{nickname}</div>
          ) : (
            <div className="mp-id-name is-missing">昵称未记录</div>
          )}
          <div className="mp-id-sub">
            <span className="mp-mono">QQ {qqId}</span>
            {relationship ? (
              <>
                <span className="mp-sep">·</span>
                <span>{relationship}</span>
              </>
            ) : null}
            {since ? (
              <>
                <span className="mp-sep">·</span>
                <span>{since}</span>
              </>
            ) : null}
            {totalChats !== null ? (
              <>
                <span className="mp-sep">·</span>
                <span>对话 {formatCount(totalChats)} 次</span>
              </>
            ) : null}
            {lastChat ? (
              <>
                <span className="mp-sep">·</span>
                <span>最近 {formatWhen(lastChat)}</span>
              </>
            ) : null}
            {birthday ? (
              <>
                <span className="mp-sep">·</span>
                <span>生日 {birthday}</span>
              </>
            ) : null}
            {sexLabel ? (
              <>
                <span className="mp-sep">·</span>
                <span>{sexLabel}</span>
              </>
            ) : null}
            {age !== null ? (
              <>
                <span className="mp-sep">·</span>
                <span>{age} 岁</span>
              </>
            ) : null}
          </div>
        </div>

        <div className="mp-id-side">
          <div className="mp-intimacy">
            <div className="mp-intimacy-head">
              <span>亲密度</span>
              <b>{intimacy === null ? '—' : clamp(intimacy, 0, 100)}</b>
            </div>
            <div className="mp-intimacy-bar">
              <i style={{ width: `${clamp(intimacy ?? 0, 0, 100)}%` }} />
            </div>
          </div>
          <div className={'mp-trust' + (trustWarn ? ' is-warn' : '')} title="画像（notes）的可信等级">
            <span>画像信任</span>
            <b>{trust ? (TRUST_LABELS[trust] ?? trust) : '未记录'}</b>
          </div>
        </div>
      </div>

      {/* 称呼：QQ 昵称 + 自称/外号（按来源分组，自称优先） */}
      <AliasesBlock
        nickname={nickname}
        nicknameMissing={nicknameMissing}
        aliases={aliases}
        findMemory={findMemory}
        onOpenMemory={onOpenMemory}
      />

      {notes ? (
        <div className={'mp-notes' + (notesOpen ? ' is-open' : '')}>
          <div className="mp-notes-head">
            <span className="mp-notes-title">关于 ta 的画像</span>
            <button type="button" className="mp-notes-toggle" onClick={onToggleNotes}>
              {notesOpen ? '收起' : '展开全文'}
            </button>
          </div>
          <p className="mp-notes-body">{notes}</p>
        </div>
      ) : (
        <div className="mp-notes is-empty">还没有形成画像</div>
      )}
    </>
  )
}

/** 统计带：一行紧凑数字 */
function StatsBar({ counts }: { counts: MemoryProfileCounts }) {
  const items: { label: string; value: number }[] = [
    { label: '记忆', value: counts.memories },
    { label: '活跃', value: counts.active },
    { label: '被推翻', value: counts.superseded },
    { label: '撤回', value: counts.retracted },
    { label: '情景', value: counts.episodic },
    { label: '语义', value: counts.semantic },
    { label: '语义簇', value: counts.clusters },
    { label: '事实', value: counts.facts },
    { label: '经历', value: counts.episodes },
  ]
  return (
    <div className="mp-stats">
      {items.map((item) => (
        <span className="mp-stat" key={item.label}>
          <span className="mp-stat-label">{item.label}</span>
          <b className="mp-stat-num">{formatCount(item.value)}</b>
        </span>
      ))}
    </div>
  )
}

/** 记忆详情弹层：演化史里点「详情」打开，看未截断的原文 + 元信息（Esc/点遮罩关） */
function MemoryDetailDialog({
  memory,
  onClose,
}: {
  memory: OpenedMemory
  onClose: () => void
}) {
  return (
    <div className="mp-dialog-mask" onClick={onClose}>
      <div
        className="mp-dialog"
        role="dialog"
        aria-modal="true"
        aria-label="记忆详情"
        onClick={(event) => event.stopPropagation()}
      >
        <div className="mp-dialog-head">
          <span className="mp-dialog-title">记忆 #{memory.id}</span>
          <button type="button" className="mp-dialog-x" onClick={onClose} aria-label="关闭">
            <X size={14} />
          </button>
        </div>
        <div className="mp-dialog-meta">
          <span className="mp-tag">{valueLabel(memory.key)}</span>
          <span className="mp-dialog-status">{valueLabel(memory.status)}</span>
          <span className="mp-dialog-time">{formatWhen(memory.time) || '时间未知'}</span>
        </div>
        <p className="mp-dialog-body">{memory.value || '（空记忆）'}</p>
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   主组件
   ═══════════════════════════════════════════════════════════════ */

export default function MemoryProfile({ qq, onBack }: { qq: string; onBack: () => void }) {
  const [data, setData] = useState<MemoryProfile | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [filters, setFilters] = useState<Filters>({})
  const [notesOpen, setNotesOpen] = useState(false)
  /** 顶部两个 tab：规格剖面（七维 + L0-L5）/ 演化史（版本链、diff） */
  const [tab, setTab] = useState<'profile' | 'history'>('profile')
  /** 演化史里点「详情」打开的记忆全文弹层 */
  const [openedMemory, setOpenedMemory] = useState<OpenedMemory | null>(null)
  const [expanded, setExpanded] = useState<Record<LayerKey, boolean>>(DEFAULT_LAYERS)
  const [dimOpen, setDimOpen] = useState<Partial<Record<FilterDim, boolean>>>({})
  /**
   * L2 一次最多渲染多少条。样本上限 1000 条——绝不一次挂满 DOM，
   * 「显示更多」每次加一屏，滚动位置和浏览器都受得住。
   */
  const [visibleLimit, setVisibleLimit] = useState(MEMORY_PAGE)
  /** 请求序号：快速切人时丢掉迟到的旧响应 */
  const requestRef = useRef(0)

  const load = useCallback(async () => {
    const seq = requestRef.current + 1
    requestRef.current = seq
    setLoading(true)
    setError('')
    try {
      const profile = await api.memoryProfile(qq)
      if (requestRef.current !== seq) return
      setData(profile)
      // 换人 = 换一份数据：筛选/展开状态全部归零
      setFilters({})
      setNotesOpen(false)
      setExpanded(DEFAULT_LAYERS)
      setDimOpen({})
      setVisibleLimit(MEMORY_PAGE)
    } catch (e) {
      if (requestRef.current !== seq) return
      setError(e instanceof Error ? e.message : String(e))
    } finally {
      if (requestRef.current === seq) setLoading(false)
    }
  }, [qq])

  useEffect(() => {
    void load()
  }, [load])

  /* 记忆详情弹层：Esc 关闭（点遮罩关闭在 JSX 里） */
  useEffect(() => {
    if (!openedMemory) return
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpenedMemory(null)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [openedMemory])

  /* ── 维度筛选：同维度再点取消；不同维度叠加，最多同时 2 个 ── */
  const toggleFilter = (dim: FilterDim, value: string) => {
    // 条件一变，分页就回到第一屏（否则可能停在第 30 屏的空白里）
    setVisibleLimit(MEMORY_PAGE)
    setFilters((prev) => {
      const next: Filters = { ...prev }
      const used = (Object.keys(prev) as FilterDim[]).filter((key) => Boolean(prev[key]))
      if (!value || next[dim] === value) {
        delete next[dim]
        return next
      }
      // 已经选了两个维度：挤掉最早选的那个（第三个维度没意义，界面也要收敛）
      if (!prev[dim] && used.length >= 2) delete next[used[0]]
      next[dim] = value
      return next
    })
  }

  const toggleLayer = (key: LayerKey) => () => {
    setExpanded((prev) => ({ ...prev, [key]: !prev[key] }))
  }

  const activeFilters: { dim: FilterDim; value: string }[] = []
  for (const dim of DIM_ORDER) {
    const value = filters[dim]
    if (value) activeFilters.push({ dim, value })
  }

  const dimensions = data?.dimensions
  const counts = data?.counts
  const memories = data?.layers.memories ?? []
  const support = buildSupport(memories)
  const filtered = filterMemories(memories, filters, support)
  const visible = filtered.slice(0, visibleLimit)
  /** 选了但样本不带这个维度 → 只统计，不能逐条筛（见文件头的契约说明） */
  const unsupported = activeFilters.filter((item) => !support[item.dim])

  return (
    <div className="mp-page">
      <header className="mp-top">
        <button type="button" className="mp-back" onClick={onBack}>
          <ArrowLeft size={14} />
          返回图谱
        </button>
        <span className="mp-top-title">记忆剖面</span>
        <div className="mp-tabs" role="tablist" aria-label="记忆视图">
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'profile'}
            className={'mp-tab' + (tab === 'profile' ? ' is-on' : '')}
            onClick={() => setTab('profile')}
          >
            规格剖面
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={tab === 'history'}
            className={'mp-tab' + (tab === 'history' ? ' is-on' : '')}
            onClick={() => setTab('history')}
          >
            演化史
          </button>
        </div>
        {tab === 'profile' ? (
          <button
            type="button"
            className="mp-icon-btn"
            onClick={() => void load()}
            disabled={loading}
            title="重新读取"
            aria-label="重新读取"
          >
            <RefreshCw size={14} className={loading ? 'mp-spin' : ''} />
          </button>
        ) : (
          <span className="mp-top-gap" aria-hidden="true" />
        )}
      </header>

      <div className={'mp-tabpane' + (tab === 'profile' ? '' : ' is-hidden')}>
        {error ? (
        <div className="mp-error" role="alert">
          <span className="mp-error-text">{error}</span>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void load()}
            disabled={loading}
          >
            重试
          </button>
        </div>
      ) : null}

      {loading && !data ? (
        <div className="mp-center">
          <LoaderCircle size={16} className="mp-spin" />
          正在读取她的记忆…
        </div>
      ) : null}

      {!loading && !data && !error ? (
        <div className="mp-center">没有拿到这个人的记忆剖面</div>
      ) : null}

      {data && counts && dimensions ? (
        <>
          <IdentityHead
            person={data.person}
            addresses={data.addresses}
            qq={qq}
            notesOpen={notesOpen}
            onToggleNotes={() => setNotesOpen((value) => !value)}
            findMemory={(memoryId) =>
              data.layers.memories.find((memory) => memory.id === memoryId) ?? null
            }
            onOpenMemory={(memory) => setOpenedMemory(memory)}
          />
          <StatsBar counts={counts} />

          {activeFilters.length > 0 ? (
            <div className="mp-filters">
              {activeFilters.map((item) => {
                const notInSample = !support[item.dim]
                return (
                  <button
                    type="button"
                    className={'mp-chip' + (notInSample ? ' is-weak' : '')}
                    key={item.dim}
                    onClick={() => toggleFilter(item.dim, '')}
                    title={
                      notInSample
                        ? 'L2 样本未携带这个维度：条件只体现在统计里'
                        : '清除这个筛选条件'
                    }
                  >
                    <span className="mp-chip-dim">{DIM_TITLES[item.dim]}</span>
                    <span className="mp-chip-val">{valueLabel(item.value)}</span>
                    {notInSample ? <span className="mp-chip-note">仅统计</span> : null}
                    <X size={11} />
                  </button>
                )
              })}
              <button
                type="button"
                className="mp-filter-clear"
                onClick={() => {
                  setFilters({})
                  setVisibleLimit(MEMORY_PAGE)
                }}
              >
                清除全部
              </button>
            </div>
          ) : null}

          {counts.memories === 0 ? (
            <div className="mp-center">她还没有关于这个人的记忆</div>
          ) : (
            <div className="mp-main">
              {/* ── 左栏：七维矩阵（各卡可折叠） ── */}
              <aside className="mp-left">
                <div className="mp-col-head">
                  <span className="mp-col-title">七维索引</span>
                  <span className="mp-col-note">点维度值 → 筛 L2</span>
                </div>
                <div className="mp-dims">
                  {DIM_ORDER.map((dim) => (
                    <DimCard
                      key={dim}
                      dim={dim}
                      title={DIM_TITLES[dim]}
                      hint={DIM_HINTS[dim]}
                      items={dimensions[dim]}
                      selected={filters[dim] ?? null}
                      onPick={(value) => toggleFilter(dim, value)}
                      open={Boolean(dimOpen[dim])}
                      onToggle={() =>
                        setDimOpen((prev) => ({ ...prev, [dim]: !prev[dim] }))
                      }
                    />
                  ))}
                  <ImportanceCard items={dimensions.importance} />
                </div>
              </aside>

              {/* ── 右栏：L0-L5 分层架构（从抽象到原始） ── */}
              <div className="mp-right">
                <div className="mp-col-head">
                  <span className="mp-col-title">分层架构</span>
                  <span className="mp-col-note">抽象 → 原始</span>
                </div>
                <div className="mp-layers">
                  <LayerCard
                    code="L2"
                    name="记忆条目"
                    count={`${formatCount(counts.memories)} 条`}
                    note={`样本 ${formatCount(memories.length)} · 可注入 ${formatCount(
                      counts.trusted_injectable,
                    )}`}
                    open={expanded.L2}
                    onToggle={toggleLayer('L2')}
                  >
                    <div className="mp-layer-line">
                      {memories.length === 0
                        ? '后端没有带回记忆样本'
                        : `本次带回 ${formatCount(counts.returned)} 条样本（真实总数 ${formatCount(
                            counts.memories,
                          )} 条）${
                            activeFilters.length > 0
                              ? `，筛选后 ${formatCount(filtered.length)} 条`
                              : ''
                          }`}
                    </div>
                    {counts.trusted_blocked > 0 ? (
                      <div className="mp-layer-line is-warn">
                        {formatCount(counts.trusted_blocked)} 条被信任门挡住（旧版未验证）——她
                        「记得」它们，但召回时不会出现
                      </div>
                    ) : null}
                    {unsupported.length > 0 ? (
                      <div className="mp-layer-line is-warn">
                        {unsupported.map((item) => DIM_TITLES[item.dim]).join(' / ')}
                        维度不在 L2 样本里（样本只带 key/origin），条件只作用于上面的统计
                      </div>
                    ) : null}
                    {filtered.length === 0 ? (
                      <div className="mp-empty-line">没有符合筛选条件的记忆</div>
                    ) : (
                      <div className="mp-mems">
                        {visible.map((memory) => (
                          <MemoryCard key={memory.id} memory={memory} />
                        ))}
                        {filtered.length > visible.length ? (
                          <button
                            type="button"
                            className="mp-more"
                            onClick={() => setVisibleLimit((limit) => limit + MEMORY_PAGE)}
                          >
                            再显示 {MEMORY_PAGE} 条
                            <span className="mp-more-note">
                              已显示 {formatCount(visible.length)} / 共 {formatCount(filtered.length)} 条
                            </span>
                          </button>
                        ) : null}
                      </div>
                    )}
                  </LayerCard>

                  <LayerCard
                    code="L3"
                    name="语义簇"
                    count={`${formatCount(counts.clusters)} 个`}
                    note={`${formatCount(counts.facts)} 条事实`}
                    open={expanded.L3}
                    onToggle={toggleLayer('L3')}
                  >
                    {data.layers.clusters.length === 0 ? (
                      <div className="mp-empty-line">还没有形成语义簇</div>
                    ) : (
                      <div className="mp-clusters">
                        {data.layers.clusters.map((cluster) => (
                          <ClusterCard key={cluster.id} cluster={cluster} />
                        ))}
                      </div>
                    )}
                  </LayerCard>

                  <LayerCard
                    code="L4"
                    name="共同经历"
                    count={`${formatCount(counts.episodes)} 段`}
                    open={expanded.L4}
                    onToggle={toggleLayer('L4')}
                  >
                    {data.layers.episodes.length === 0 ? (
                      <div className="mp-empty-line">还没有共同经历</div>
                    ) : (
                      <div className="mp-episodes">
                        {data.layers.episodes.map((episode) => (
                          <EpisodeItem key={episode.id} episode={episode} />
                        ))}
                      </div>
                    )}
                  </LayerCard>

                  <LayerCard
                    code="L5"
                    name="存储能力"
                    count="全局规格"
                    note="不分人"
                    open={expanded.L5}
                    onToggle={toggleLayer('L5')}
                  >
                    <CapabilitySpecs caps={data.capabilities} />
                  </LayerCard>

                  <LayerCard
                    code="L1"
                    name="证据链"
                    count={`${formatCount(counts.evidence)} 条`}
                    open={expanded.L1}
                    onToggle={toggleLayer('L1')}
                  >
                    <div className="mp-layer-line">
                      {formatCount(counts.evidence)} 条证据覆盖本次带回的{' '}
                      {formatCount(counts.returned)} 条记忆
                    </div>
                    {data.layers.evidence.length === 0 ? (
                      <div className="mp-empty-line">还没有证据样本</div>
                    ) : (
                      <div className="mp-ev-list is-samples">
                        {data.layers.evidence.map((item, index) => (
                          <blockquote
                            className="mp-ev"
                            key={`${item.memory_id}-${item.chat_id}-${index}`}
                          >
                            <div className="mp-ev-meta">
                              {item.timestamp ? formatWhen(item.timestamp) : '时间未知'}
                              {' · '}
                              {truthyNumber(item.is_bot_reply) ? '糖糖' : '对方'}
                              {' · '}
                              记忆 #{item.memory_id}
                            </div>
                            <p className="mp-ev-text">{truncate(item.message, EVIDENCE_CLIP)}</p>
                          </blockquote>
                        ))}
                      </div>
                    )}
                  </LayerCard>

                  <LayerCard
                    code="L0"
                    name="时间线"
                    count={`${formatCount(data.timeline.length)} 个月`}
                    open={expanded.L0}
                    onToggle={toggleLayer('L0')}
                  >
                    <TimelineChart timeline={data.timeline} undated={counts.undated} />
                  </LayerCard>
                </div>
              </div>
            </div>
          )}
        </>
      ) : null}
      </div>

      {tab === 'history' ? (
        <MemoryHistory qq={qq} onOpenMemory={(memory) => setOpenedMemory(memory)} />
      ) : null}

      {openedMemory ? (
        <MemoryDetailDialog memory={openedMemory} onClose={() => setOpenedMemory(null)} />
      ) : null}
    </div>
  )
}

export { MemoryProfile }
