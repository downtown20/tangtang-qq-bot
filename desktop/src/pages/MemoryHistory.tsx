import { useCallback, useEffect, useRef, useState } from 'react'
import { ChevronRight, Info, LoaderCircle, RefreshCw } from 'lucide-react'
import { api } from '../lib/api'
import type {
  HistoryMerged,
  HistoryVersion,
  MemoryChain,
  MemoryHistory,
  MemoryHistoryGroup,
  MemoryRevisionRow,
  MemoryRevisions,
} from '../lib/api'
import './MemoryHistory.css'

/* ═══════════════════════════════════════════════════════════════
   记忆演化史 —— 她对同一个人的理解是怎么一版版变过来的

   数据侧：`superseded_by` 是 N→1 的指针（画像重合成时多条旧事实并入
   同一条新画像），所以一条链 = 一条主线 + 被并入的兄弟节点（merged）。
   另外 1261 条 status='superseded' 却没有指针的「断链」如实展示——
   那是数据缺口，暴露出来比藏起来有用。

   展示侧两个重点：
     · 版本 diff：按标点切片段 + LCS，删掉的标删除线、新增的标绿底
     · 并入的兄弟节点：新画像不是「改写」而是「重合成」的证据
   ═══════════════════════════════════════════════════════════════ */

/** 正文超过这个长度就折叠（展开后仍是完整原文，diff 只在展开后算） */
const VALUE_CLIP = 400
/** 并入节点单行截断长度（全文挂 title） */
const MERGED_CLIP = 80
/** 主题演化、断链列表的单行截断长度 */
const LINE_CLIP = 120
/** 变更流水 before / after 的截断长度（展开后看全文；delete 快照走 JSON 折叠） */
const REV_CLIP = 120
/** 片段总数超过这个值就退化成整段显示，不做 LCS */
const MAX_DIFF_SEGMENTS = 200
/** LCS 的 DP 单元格上限：200×200 = 4 万，再多就退化（防卡死） */
const MAX_DIFF_CELLS = 40000
/**
 * 片段级零公共时的字符级兜底上限：prev 字数 × next 字数。
 * 250×240 ≈ 6 万格 DP，几毫秒；再大就放弃 diff（整段显示）。
 */
const MAX_CHAR_CELLS = 60000

/** key → 中文（后端的 key 是英文枚举；未知值原样显示，别吞数据） */
const KEY_LABELS: Record<string, string> = {
  event: '事件',
  said: '说过的话',
  fact: '事实',
  fact_synthesis: '事实合成',
  fact_correction: '事实纠错',
  profile_synthesis: '画像重合成',
  profile: '画像',
  promise: '承诺',
  like: '喜好',
  dislike: '不喜欢',
  hate: '厌恶',
  opinion: '看法',
  habit: '习惯',
  recommend: '推荐',
  action_completed: '行动完成',
  feeling: '感受',
  relationship: '关系',
  plan: '计划',
  identity: '身份',
  preference: '偏好',
  未分类: '未分类',
}

const STATUS_LABELS: Record<string, string> = {
  active: '当前',
  superseded: '已被取代',
  retracted: '已撤回',
  fulfilled: '已兑现',
}

const ORIGIN_LABELS: Record<string, string> = {
  extracted: '对话提取',
  self: '自我生成',
  summarized: '摘要合成',
  backfill: '补录',
  corrected: '纠正',
  manual: '手动录入',
}

/* ── 变更流水字典 ────────────────────────────────────────────
   流水是「写操作的事实记录」（2026-10-24 起），和上面的版本链是两套
   证据：版本链是从 superseded_by 推断的，流水是当时记下来的。
   actor 未知时绝不糊成「其他」——`unknown` 本身就是数据质量信号。
   ──────────────────────────────────────────────────────────── */

const ACTOR_LABELS: Record<string, string> = {
  'tool:correct_memory': '纠正工具',
  'tool:forget_memory': '遗忘工具',
  'synthesis:profile': '画像重合成',
  'synthesis:fact': '事实合成',
  'synthesis:fact_cluster': '事实簇摘要',
  'synthesis:cluster_merge': '事实簇合并',
  'synthesis:relationship': '关系档案重写',
  'dedup:exact': '精确去重',
  'dedup:semantic': '语义去重',
  'dedup:cache': '同批去重',
  'decay:stale_cleanup': '陈旧清理',
  'system:fulfill': '承诺结算',
  'system:identity_sync': '身份同步',
  'system:daily_bonus': '每日加成',
  'extract:llm': '自动提取',
  unknown: '来源未标注',
}

const FIELD_LABELS: Record<string, string> = {
  value: '内容',
  status: '状态',
  importance: '权重',
  trust_level: '可信度',
  superseded_by: '取代指针',
  evidence_ids: '证据',
  notes: '画像',
  notes_trust_level: '画像可信度',
  notes_source_ids: '画像来源',
  summary: '摘要',
  title: '标题',
  birthday: '生日',
  relationship_summary: '关系档案',
  source_group_id: '作用域',
  key: '类型',
  category: '类别',
  cognitive: '认知类型',
  retention: '保留策略',
  origin: '来源',
  event_time: '发生时间',
  permanence: '持久性',
  fact_count: '事实数',
  nickname: '昵称',
  intimacy: '亲密度',
  relationship: '关系',
  age: '年龄',
  sex: '性别',
}

const OP_LABELS: Record<string, string> = {
  update: '改写',
  status: '状态变更',
  supersede: '被取代',
  retract: '被否定',
  fulfill: '已履行',
  merge: '证据合并',
  delete: '删除',
  migrate: '迁移',
}

const ENTITY_LABELS: Record<string, string> = {
  memories: '记忆',
  cluster_facts: '事实',
  fact_clusters: '语义簇',
  people: '画像',
}

/** actor 的分类色：工具 / 合成 / 去重 / 命令 / 系统 / 未标注 */
type ActorTone = 'tool' | 'synth' | 'dedup' | 'command' | 'system' | 'unknown'

function actorTone(actor: string): ActorTone {
  if (!actor || actor === 'unknown') return 'unknown'
  if (actor.startsWith('tool:')) return 'tool'
  if (actor.startsWith('synthesis:')) return 'synth'
  if (actor.startsWith('dedup:') || actor.startsWith('decay:')) return 'dedup'
  if (actor.startsWith('command:') || actor.startsWith('review:')) return 'command'
  return 'system'
}

function actorLabel(actor: string): string {
  if (!actor) return '来源未标注'
  const known = ACTOR_LABELS[actor]
  if (known) return known
  if (actor.startsWith('command:')) return `命令：${actor.slice(8)}`
  if (actor.startsWith('review:')) return `人工复核：${actor.slice(7)}`
  if (actor.startsWith('migration:')) return `系统迁移：${actor.slice(10)}`
  // 没见过的来源原样显示——比糊成「其他」诚实
  return actor
}

function opLabel(op: string): string {
  if (!op) return '未标注'
  return OP_LABELS[op] ?? op
}

function fieldLabel(field: string): string {
  if (!field) return ''
  return FIELD_LABELS[field] ?? field
}

function entityLabel(entity: string): string {
  if (!entity) return '记录'
  return ENTITY_LABELS[entity] ?? entity
}

/** 点开一条记忆时交给上层的信息（用于详情弹层） */
export type OpenedMemory = {
  id: number
  value: string
  key: string
  status: string
  time: string
}

/* ── 小工具 ── */

function formatCount(value: number): string {
  return Number.isFinite(value) ? Math.max(0, Math.round(value)).toLocaleString() : '—'
}

function formatWhen(raw: string): string {
  if (!raw) return '时间未知'
  return raw.replace('T', ' ').slice(0, 16)
}

function formatSpan(span: [string, string]): string {
  const start = (span[0] || '').slice(0, 10)
  const end = (span[1] || '').slice(0, 10)
  if (start && end) return `${start} → ${end}`
  return start || end || '时间未知'
}

function truncate(text: string, limit: number): string {
  if (text.length <= limit) return text
  return `${text.slice(0, limit)}…`
}

function keyLabel(key: string): string {
  if (!key) return '未分类'
  return KEY_LABELS[key] ?? key
}

function statusLabel(status: string): string {
  if (!status) return '状态未知'
  return STATUS_LABELS[status] ?? status
}

function statusTone(status: string): 'ok' | 'sup' | 'bad' | 'plain' {
  if (status === 'active') return 'ok'
  if (status === 'superseded') return 'sup'
  if (status === 'retracted') return 'bad'
  return 'plain'
}

function originLabel(origin: string): string {
  if (!origin) return '来源未记录'
  return ORIGIN_LABELS[origin] ?? origin
}

/* ── 版本 diff ───────────────────────────────────────────────
   切分：按中文标点（。；！？，、\n）切，标点留在前一段末尾——
   「片段」正好是一句/一个分句，diff 出来的结果读起来才像话。
   LCS：朴素 DP（O(n·m)），n,m ≤ 200、单元格 ≤ 4 万，一次性算完。
   ──────────────────────────────────────────────────────────── */

type DiffKind = 'same' | 'added' | 'removed'
type DiffPiece = { kind: DiffKind; text: string }

const SEGMENT_SEPARATORS = new Set(['。', '；', '！', '？', '，', '、', '\n'])

function splitSegments(text: string): string[] {
  const parts = text.split(/([。；！？，、\n])/)
  const segments: string[] = []
  let buffer = ''
  for (const part of parts) {
    if (!part) continue
    buffer += part
    if (SEGMENT_SEPARATORS.has(part)) {
      segments.push(buffer)
      buffer = ''
    }
  }
  if (buffer) segments.push(buffer)
  // ⚠️ 不能按 trim 过滤：纯空白片段（换行、缩进）也是原文的一部分，
  // 丢掉它们 diff 渲染就会少字。只丢掉长度 0 的空串。
  return segments.filter((segment) => segment.length > 0)
}

/**
 * 通用 LCS diff：输入是已切好的序列（片段或字符），返回 inline diff 片段。
 * 朴素的 DP + 回溯，规模由调用方保证（≤ MAX_DIFF_CELLS）。
 */
function lcsDiff(prev: string[], next: string[]): DiffPiece[] {
  const rows = prev.length
  const cols = next.length
  const dp: number[][] = Array.from({ length: rows + 1 }, () =>
    new Array<number>(cols + 1).fill(0),
  )
  for (let i = rows - 1; i >= 0; i -= 1) {
    for (let j = cols - 1; j >= 0; j -= 1) {
      dp[i][j] =
        prev[i] === next[j]
          ? dp[i + 1][j + 1] + 1
          : Math.max(dp[i + 1][j], dp[i][j + 1])
    }
  }

  const pieces: DiffPiece[] = []
  const push = (kind: DiffKind, text: string): void => {
    const last = pieces[pieces.length - 1]
    if (last && last.kind === kind) last.text += text
    else pieces.push({ kind, text })
  }

  let i = 0
  let j = 0
  while (i < rows && j < cols) {
    if (prev[i] === next[j]) {
      push('same', prev[i])
      i += 1
      j += 1
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      push('removed', prev[i])
      i += 1
    } else {
      push('added', next[j])
      j += 1
    }
  }
  while (i < rows) {
    push('removed', prev[i])
    i += 1
  }
  while (j < cols) {
    push('added', next[j])
    j += 1
  }
  return pieces
}

/**
 * 求两版之间的 inline diff。
 * 返回 null = 不做 diff（太短 / 片段太多 / 矩阵太大），调用方退回整段显示。
 *
 * 两级策略：先按中文标点切片段做 LCS（句子级改动看得最清楚）；
 * 若片段级一个公共片段都没有（画像重合成实测就是整段改写），
 * 退到字符级 LCS——只有这样才能看出「大二学生」→「大学大二学生」
 * 这种词内改动，而不是整段红绿一大片。
 */
function diffPieces(prevText: string, nextText: string): DiffPiece[] | null {
  if (!prevText || !nextText) return null
  const prev = splitSegments(prevText)
  const next = splitSegments(nextText)
  // 片段太少时 diff 没意义（一整个句子变了，标红整段不如直接看）
  if (prev.length < 2 || next.length < 2) return null
  if (prev.length > MAX_DIFF_SEGMENTS || next.length > MAX_DIFF_SEGMENTS) return null
  if (prev.length * next.length > MAX_DIFF_CELLS) return null

  const pieces = lcsDiff(prev, next)
  if (pieces.some((piece) => piece.kind === 'same')) return pieces

  // 片段级零公共：字符级兜底（按码点切，emoji 也不会被拆坏）
  if (prevText.length * nextText.length <= MAX_CHAR_CELLS) {
    const charPieces = lcsDiff(Array.from(prevText), Array.from(nextText))
    if (charPieces.some((piece) => piece.kind === 'same')) return charPieces
  }
  return pieces
}

/* ── 小组件 ── */

/** 重要度：固定 10 个点位，实心数 = importance（与剖面页同一套读法） */
function ImportanceDots({ value }: { value: number }) {
  const level = Math.min(10, Math.max(0, Math.round(value)))
  return (
    <span className="mh-imp" title={`重要度 ${level} / 10`} aria-label={`重要度 ${level} / 10`}>
      {Array.from({ length: 10 }, (_, index) => (
        <i key={index} className={index < level ? 'is-on' : ''} />
      ))}
    </span>
  )
}

/** 一版正文：第一版全文；之后的版本做 inline diff（新增绿底、删除线标灰） */
function DiffText({ prev, next }: { prev: string; next: string }) {
  if (!prev) return <>{next}</>
  const pieces = diffPieces(prev, next)
  if (!pieces) return <>{next}</>
  return (
    <>
      {pieces.map((piece, index) => {
        if (piece.kind === 'same') return <span key={index}>{piece.text}</span>
        if (piece.kind === 'added') {
          return (
            <mark className="mh-add" key={index}>
              {piece.text}
            </mark>
          )
        }
        return (
          <del className="mh-del" key={index} title="上一版有、这一版没有">
            {piece.text}
          </del>
        )
      })}
    </>
  )
}

/** 版本正文：>400 字先折叠；展开后才算 diff（长文本 diff 也才用得上） */
function VersionBody({ version, prev }: { version: HistoryVersion; prev: HistoryVersion | null }) {
  const [open, setOpen] = useState(false)
  const isLong = version.value.length > VALUE_CLIP

  if (!isLong) {
    return (
      <p className="mh-version-text">
        <DiffText prev={prev ? prev.value : ''} next={version.value} />
      </p>
    )
  }
  if (!open) {
    return (
      <div className="mh-version-text">
        <p>{version.value.slice(0, VALUE_CLIP)}…</p>
        <button type="button" className="mh-expand" onClick={() => setOpen(true)}>
          展开全文（共 {formatCount(version.value.length)} 字）
        </button>
      </div>
    )
  }
  return (
    <div className="mh-version-text">
      <p>
        <DiffText prev={prev ? prev.value : ''} next={version.value} />
      </p>
      <button type="button" className="mh-expand" onClick={() => setOpen(false)}>
        收起
      </button>
    </div>
  )
}

/** 被并入当前这一版的兄弟节点（N→1 的证据），默认折叠 */
function MergedBlock({
  items,
  open,
  onToggle,
}: {
  items: HistoryMerged[]
  open: boolean
  onToggle: () => void
}) {
  return (
    <div className="mh-merged">
      <button
        type="button"
        className="mh-merged-toggle"
        onClick={onToggle}
        aria-expanded={open}
      >
        <ChevronRight size={11} className={'mh-caret' + (open ? ' is-open' : '')} />
        另有 {items.length} 条被并入这一版
      </button>
      {open ? (
        <ul className="mh-merged-list">
          {items.map((item) => (
            <li className="mh-merged-item" key={item.id} title={item.value}>
              <span className="mh-merged-key">{keyLabel(item.key)}</span>
              <span className="mh-merged-text">{truncate(item.value, MERGED_CLIP)}</span>
              <span className="mh-merged-time">{formatWhen(item.time)}</span>
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

/** 一条版本链：主线 head → leaf 的垂直时间线 + 每个版本上挂的并入节点 */
function ChainCard({
  chain,
  expandedMerged,
  onToggleMerged,
  onOpenMemory,
}: {
  chain: MemoryChain
  expandedMerged: Set<number>
  onToggleMerged: (versionId: number) => void
  onOpenMemory?: (memory: OpenedMemory) => void
}) {
  const lastIndex = chain.versions.length - 1
  return (
    <article className="mh-chain">
      <div className="mh-chain-head">
        <span className="mh-chain-kind">{keyLabel(chain.kind)}</span>
        <span className="mh-chain-span">{formatSpan(chain.span)}</span>
        <span className="mh-chain-count">{chain.versions.length} 版</span>
        {chain.merged.length > 0 ? (
          <span className="mh-chain-merged-badge">另有 {chain.merged.length} 条被并入</span>
        ) : null}
      </div>

      <ol className="mh-versions">
        {chain.versions.map((version, index) => {
          const prev = index > 0 ? chain.versions[index - 1] : null
          const mergedItems = chain.merged.filter((item) => item.into === version.id)
          const isCurrent = index === lastIndex
          return (
            <li className="mh-version" key={version.id}>
              <span
                className={'mh-dot' + (isCurrent ? ' is-current' : '')}
                aria-hidden="true"
                title={isCurrent ? '当前版本' : '已被下一版取代'}
              />
              <div className="mh-version-main">
                <div className="mh-version-head">
                  <span className="mh-vnum">v{index + 1}</span>
                  <span className="mh-vtime">{formatWhen(version.time)}</span>
                  <span className="mh-vorigin">{originLabel(version.origin)}</span>
                  <ImportanceDots value={version.importance} />
                  <span className={'mh-badge is-' + statusTone(version.status)}>
                    {statusLabel(version.status)}
                  </span>
                  {onOpenMemory ? (
                    <button
                      type="button"
                      className="mh-detail-btn"
                      onClick={() =>
                        onOpenMemory({
                          id: version.id,
                          value: version.value,
                          key: version.key,
                          status: version.status,
                          time: version.time,
                        })
                      }
                    >
                      详情
                    </button>
                  ) : null}
                </div>
                <VersionBody version={version} prev={prev} />
                {mergedItems.length > 0 ? (
                  <MergedBlock
                    items={mergedItems}
                    open={expandedMerged.has(version.id)}
                    onToggle={() => onToggleMerged(version.id)}
                  />
                ) : null}
              </div>
            </li>
          )
        })}
      </ol>
    </article>
  )
}

/** 主题演化区里的一个 key 折叠块 */
function GroupBlock({
  group,
  open,
  onToggle,
  onOpenMemory,
}: {
  group: MemoryHistoryGroup
  open: boolean
  onToggle: () => void
  onOpenMemory?: (memory: OpenedMemory) => void
}) {
  return (
    <div className="mh-group">
      <button type="button" className="mh-group-head" onClick={onToggle} aria-expanded={open}>
        <ChevronRight size={12} className={'mh-caret' + (open ? ' is-open' : '')} />
        <span className="mh-group-key">{keyLabel(group.key)}</span>
        <span className="mh-group-count">{formatCount(group.count)} 条</span>
        {group.count > group.items.length ? (
          <span className="mh-group-note">只列前 {formatCount(group.items.length)} 条</span>
        ) : null}
      </button>
      {open ? (
        <ul className="mh-group-items">
          {group.items.map((item) => (
            <li className="mh-group-item" key={item.id}>
              <span className="mh-group-time">{formatWhen(item.time)}</span>
              <span className="mh-group-value">{truncate(item.value, LINE_CLIP)}</span>
              <span className={'mh-badge is-' + statusTone(item.status)}>
                {statusLabel(item.status)}
              </span>
              {onOpenMemory ? (
                <button
                  type="button"
                  className="mh-detail-btn"
                  onClick={() =>
                    onOpenMemory({
                      id: item.id,
                      value: item.value,
                      key: item.key,
                      status: item.status,
                      time: item.time,
                    })
                  }
                >
                  详情
                </button>
              ) : null}
            </li>
          ))}
        </ul>
      ) : null}
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   变更流水（memory_revisions）—— 与版本链并列、口径不同的另一套证据
   ═══════════════════════════════════════════════════════════════ */

/** delete 行的 before 是整行 JSON 快照：能格式化就格式化，不能就原样 */
function prettyJson(raw: string): string {
  try {
    const parsed: unknown = JSON.parse(raw)
    return JSON.stringify(parsed, null, 2)
  } catch {
    return raw
  }
}

/** before → after：删除线标红、新增标绿；delete 快照单独折叠成 JSON */
function RevisionValues({ row }: { row: MemoryRevisionRow }) {
  const [open, setOpen] = useState(false)
  const [jsonOpen, setJsonOpen] = useState(false)
  const before = row.before
  const after = row.after

  // 删除类：field 为空 + before 是 JSON 快照——别把长 JSON 直接摊在卡片上
  if (!row.field && before.startsWith('{')) {
    return (
      <div className="mh-rev-snapshot">
        <button
          type="button"
          className="mh-rev-json-btn"
          onClick={() => setJsonOpen((value) => !value)}
          aria-expanded={jsonOpen}
        >
          <ChevronRight size={11} className={'mh-caret' + (jsonOpen ? ' is-open' : '')} />
          {jsonOpen ? '收起删除前快照' : '查看删除前的整行快照（JSON）'}
        </button>
        {jsonOpen ? <pre className="mh-rev-pre">{prettyJson(before)}</pre> : null}
      </div>
    )
  }

  const needsExpand = before.length > REV_CLIP || after.length > REV_CLIP
  const show = (text: string): string => (open ? text : truncate(text, REV_CLIP))
  const expandButton = needsExpand ? (
    <button type="button" className="mh-rev-expand" onClick={() => setOpen((value) => !value)}>
      {open ? '收起' : '展开全文'}
    </button>
  ) : null

  // 没有实际差异（append 一些操作会原样记一遍）：不要渲染假的 diff
  if (before !== '' && before === after) {
    return (
      <div className="mh-rev-same">
        <span>{show(before)}</span>
        {expandButton}
      </div>
    )
  }

  return (
    <div className={'mh-rev-values' + (open ? ' is-open' : '')}>
      {before ? <span className="mh-rev-before">{show(before)}</span> : null}
      {before && after ? <span className="mh-rev-arrow">→</span> : null}
      {after ? <span className="mh-rev-after">{show(after)}</span> : null}
      {expandButton}
    </div>
  )
}

/** 一条流水：时间 / 谁改的 / 改哪条 / 怎么改 / 为什么 */
function RevisionRowView({ row }: { row: MemoryRevisionRow }) {
  return (
    <li className="mh-rev-item">
      <div className="mh-rev-head">
        <span className="mh-rev-time">{formatWhen(row.at)}</span>
        <span
          className={'mh-actor is-' + actorTone(row.actor)}
          title={row.actor ? `来源标识：${row.actor}` : '这条变更没标来源（数据质量信号）'}
        >
          {actorLabel(row.actor)}
        </span>
        <span className="mh-rev-target">
          {entityLabel(row.entity)}
          <span className="mh-rev-id">#{row.entity_id || '—'}</span>
        </span>
        <span className="mh-rev-op">{opLabel(row.op)}</span>
        {row.field ? <span className="mh-rev-field">{fieldLabel(row.field)}</span> : null}
      </div>
      <RevisionValues row={row} />
      {row.reason ? <div className="mh-rev-reason">原因：{row.reason}</div> : null}
    </li>
  )
}

/** 变更流水区块：可折叠，独立加载（后端没重启时不影响版本链） */
function RevisionSection({
  data,
  loading,
  error,
  open,
  onToggle,
  onReload,
}: {
  data: MemoryRevisions | null
  loading: boolean
  error: string
  open: boolean
  onToggle: () => void
  onReload: () => void
}) {
  const summary = data?.summary ?? null
  const rows = data?.rows ?? []
  const actors = summary?.by_actor ?? []
  const ops = summary?.by_op ?? []
  const actorTotal = actors.reduce((sum, item) => sum + item.count, 0)

  return (
    <section className="mh-section mh-revisions">
      <button type="button" className="mh-section-head" onClick={onToggle} aria-expanded={open}>
        <ChevronRight size={13} className={'mh-caret' + (open ? ' is-open' : '')} />
        <span className="mh-section-title">变更流水</span>
        <span className="mh-section-count">
          {summary ? `${formatCount(summary.total)} 条` : '写操作留痕'}
        </span>
        <span className="mh-section-note">2026-10-24 起</span>
      </button>

      {open ? (
        <div className="mh-rev-body">
          {loading && !data ? (
            <div className="mh-center is-inline">
              <LoaderCircle size={14} className="mh-spin" />
              正在读取变更流水…
            </div>
          ) : null}

          {error ? (
            <div className="mh-error is-inline" role="alert">
              <span className="mh-error-text">{error}</span>
              <button
                type="button"
                className="btn btn-ghost btn-sm"
                onClick={onReload}
                disabled={loading}
              >
                重试
              </button>
            </div>
          ) : null}

          {data ? (
            <>
              {data.note ? <p className="mh-rev-note">{data.note}</p> : null}

              {rows.length === 0 ? (
                <div className="mh-rev-empty">
                  还没有记录——留痕从 2026-10-24 开始生效，此前的变更请看下面的版本链
                </div>
              ) : (
                <>
                  <div className="mh-rev-summary">
                    <span className="mh-rev-total">
                      共 <b>{formatCount(summary?.total ?? rows.length)}</b> 条
                      {summary && summary.total > summary.returned
                        ? `，本批展示 ${formatCount(summary.returned)} 条`
                        : ''}
                    </span>
                    {summary && (summary.first_at || summary.last_at) ? (
                      <span className="mh-rev-range">
                        {formatWhen(summary.first_at)} ~ {formatWhen(summary.last_at)}
                      </span>
                    ) : null}
                  </div>

                  {/* 谁在改她的记忆：分布条 + 标签组 */}
                  {actors.length > 0 ? (
                    <div className="mh-rev-actors">
                      <div className="mh-rev-actor-head">谁在改她的记忆</div>
                      <div className="mh-rev-actor-bar">
                        {actors.map((item) => (
                          <i
                            key={item.actor || 'unknown'}
                            className={'is-' + actorTone(item.actor)}
                            style={{
                              width: `${actorTotal > 0 ? (item.count / actorTotal) * 100 : 0}%`,
                            }}
                            title={`${actorLabel(item.actor)}（${item.actor || 'unknown'}）：${item.count} 次`}
                          />
                        ))}
                      </div>
                      <div className="mh-rev-actor-chips">
                        {actors.map((item) => (
                          <span
                            className={'mh-actor-chip is-' + actorTone(item.actor)}
                            key={item.actor || 'unknown'}
                            title={item.actor || 'unknown'}
                          >
                            <span className="mh-actor-chip-name">{actorLabel(item.actor)}</span>
                            <b>{formatCount(item.count)}</b>
                          </span>
                        ))}
                      </div>
                    </div>
                  ) : null}

                  {/* 次一级：操作类型分布 */}
                  {ops.length > 0 ? (
                    <div className="mh-rev-ops">
                      <span className="mh-rev-ops-label">操作类型</span>
                      {ops.map((item) => (
                        <span className="mh-op-chip" key={item.op || 'unknown'}>
                          {opLabel(item.op)}
                          <b>{formatCount(item.count)}</b>
                        </span>
                      ))}
                    </div>
                  ) : null}

                  <ol className="mh-rev-list">
                    {rows.map((row) => (
                      <RevisionRowView key={row.id} row={row} />
                    ))}
                  </ol>
                </>
              )}
            </>
          ) : null}
        </div>
      ) : null}
    </section>
  )
}

/* ═══════════════════════════════════════════════════════════════
   页面主组件
   ═══════════════════════════════════════════════════════════════ */

export default function MemoryHistoryView({
  qq,
  onOpenMemory,
}: {
  qq: string
  onOpenMemory?: (memory: OpenedMemory) => void
}) {
  const [data, setData] = useState<MemoryHistory | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [byKeyOpen, setByKeyOpen] = useState(false)
  const [orphansOpen, setOrphansOpen] = useState(false)
  const [expandedKeys, setExpandedKeys] = useState<Set<string>>(new Set())
  const [expandedMerged, setExpandedMerged] = useState<Set<number>>(new Set())
  const requestRef = useRef(0)

  /* 变更流水是**独立请求**：后端若还没重启/没这张表，只让这一块报错，
     不能把版本链一起拖垮。 */
  const [revData, setRevData] = useState<MemoryRevisions | null>(null)
  const [revLoading, setRevLoading] = useState(true)
  const [revError, setRevError] = useState('')
  const [revOpen, setRevOpen] = useState(true)
  const revRequestRef = useRef(0)

  const load = useCallback(async () => {
    const seq = requestRef.current + 1
    requestRef.current = seq
    setLoading(true)
    setError('')
    try {
      const history = await api.memoryHistory(qq)
      if (requestRef.current !== seq) return
      setData(history)
      // 换人 = 换一份数据：折叠状态归零
      setByKeyOpen(false)
      setOrphansOpen(false)
      setExpandedKeys(new Set())
      setExpandedMerged(new Set())
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

  const loadRevisions = useCallback(async () => {
    const seq = revRequestRef.current + 1
    revRequestRef.current = seq
    setRevLoading(true)
    setRevError('')
    try {
      const revisions = await api.memoryRevisions(qq)
      if (revRequestRef.current !== seq) return
      setRevData(revisions)
    } catch (e) {
      if (revRequestRef.current !== seq) return
      setRevError(e instanceof Error ? e.message : String(e))
    } finally {
      if (revRequestRef.current === seq) setRevLoading(false)
    }
  }, [qq])

  useEffect(() => {
    void loadRevisions()
  }, [loadRevisions])

  const toggleGroup = (key: string) => {
    setExpandedKeys((prev) => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  const toggleMerged = (versionId: number) => {
    setExpandedMerged((prev) => {
      const next = new Set(prev)
      if (next.has(versionId)) next.delete(versionId)
      else next.add(versionId)
      return next
    })
  }

  const summary = data?.summary ?? null
  const chains = data?.chains ?? []
  const orphans = data?.orphans ?? []
  const byKey = data?.by_key ?? []
  const empty = chains.length === 0 && orphans.length === 0

  return (
    <div className="mh-root">
      <div className="mh-summary">
        <span className="mh-sum-item">
          <span className="mh-sum-label">记忆</span>
          <b className="mh-sum-num">{formatCount(summary?.total ?? 0)}</b>
        </span>
        <span className="mh-sum-item">
          <span className="mh-sum-label">活跃</span>
          <b className="mh-sum-num">{formatCount(summary?.active ?? 0)}</b>
        </span>
        <span className="mh-sum-item">
          <span className="mh-sum-label">被推翻</span>
          <b className="mh-sum-num">{formatCount(summary?.superseded ?? 0)}</b>
        </span>
        <span className="mh-sum-item">
          <span className="mh-sum-label">成链</span>
          <b className="mh-sum-num">{formatCount(summary?.chained ?? 0)}</b>
        </span>
        <span className="mh-sum-item">
          <span className="mh-sum-label">断链</span>
          <b className="mh-sum-num">{formatCount(summary?.orphan_superseded ?? 0)}</b>
          <span
            className="mh-sum-info"
            title="这些记忆被推翻了，却没有记录「被谁取代」——数据缺口，不是显示问题"
          >
            <Info size={11} />
          </span>
        </span>
        <button
          type="button"
          className="mh-refresh"
          onClick={() => {
            void load()
            void loadRevisions()
          }}
          disabled={loading}
          title="重新读取演化史"
          aria-label="重新读取演化史"
        >
          <RefreshCw size={13} className={loading ? 'mh-spin' : ''} />
        </button>
      </div>

      {error ? (
        <div className="mh-error" role="alert">
          <span className="mh-error-text">{error}</span>
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
        <div className="mh-center">
          <LoaderCircle size={16} className="mh-spin" />
          正在读取演化史…
        </div>
      ) : null}

      {!loading && !data && !error ? (
        <div className="mh-center">没有拿到这个人的演化史</div>
      ) : null}

      <div className="mh-body">
        {/* 变更流水：写操作的事实记录（有留痕才有），与下面的版本链是两套证据。
            它**放在 `data ?` 判断之外**：版本链接口挂了，留痕仍然要看得到——
            这是「谁改了她的记忆」的唯一证据，不该被另一块数据的错误连坐。 */}
        <RevisionSection
          data={revData}
          loading={revLoading}
          error={revError}
          open={revOpen}
          onToggle={() => setRevOpen((value) => !value)}
          onReload={() => void loadRevisions()}
        />

        {data ? (
          <>
          {empty ? (
            <div className="mh-center">她的记忆还没有发生过版本更迭</div>
          ) : (
            <>
              <div className="mh-block-caption">
                <span className="mh-block-caption-title">版本链</span>
                <span className="mh-block-caption-note">
                  从 superseded_by / valid_to 推断的「整行被替代」——与上面的变更流水口径不同
                </span>
              </div>
              {/* ── 版本链（核心） ── */}
              {chains.length > 0 ? (
                <div className="mh-chain-list">
                  {chains.map((chain) => (
                    <ChainCard
                      key={chain.head_id}
                      chain={chain}
                      expandedMerged={expandedMerged}
                      onToggleMerged={toggleMerged}
                      onOpenMemory={onOpenMemory}
                    />
                  ))}
                </div>
              ) : (
                <div className="mh-empty-line">
                  还没有形成版本链——记忆被推翻过，但没记下「被谁取代」（看下面的断链区）
                </div>
              )}

              {/* ── 主题演化（次区域，默认折叠） ── */}
              {byKey.length > 0 ? (
                <section className="mh-section">
                  <button
                    type="button"
                    className="mh-section-head"
                    onClick={() => setByKeyOpen((value) => !value)}
                    aria-expanded={byKeyOpen}
                  >
                    <ChevronRight
                      size={13}
                      className={'mh-caret' + (byKeyOpen ? ' is-open' : '')}
                    />
                    <span className="mh-section-title">主题演化</span>
                    <span className="mh-section-count">{formatCount(byKey.length)} 个主题</span>
                  </button>
                  {byKeyOpen ? (
                    <div className="mh-groups">
                      {byKey.map((group) => (
                        <GroupBlock
                          key={group.key}
                          group={group}
                          open={expandedKeys.has(group.key)}
                          onToggle={() => toggleGroup(group.key)}
                          onOpenMemory={onOpenMemory}
                        />
                      ))}
                    </div>
                  ) : null}
                </section>
              ) : null}

              {/* ── 断链区（默认折叠，暴露数据缺口） ── */}
              {orphans.length > 0 ? (
                <section className="mh-section">
                  <button
                    type="button"
                    className="mh-section-head"
                    onClick={() => setOrphansOpen((value) => !value)}
                    aria-expanded={orphansOpen}
                  >
                    <ChevronRight
                      size={13}
                      className={'mh-caret' + (orphansOpen ? ' is-open' : '')}
                    />
                    <span className="mh-section-title">断链的记忆</span>
                    <span className="mh-section-count">
                      {formatCount(summary?.orphan_superseded ?? orphans.length)} 条
                    </span>
                  </button>
                  {orphansOpen ? (
                    <div className="mh-orphans">
                      <p className="mh-orphan-note">
                        这些记忆被推翻了，但没有记录「被谁取代」——这是数据缺口，不是显示问题。
                        {summary && summary.orphan_superseded > orphans.length
                          ? `下面只列最近的 ${formatCount(orphans.length)} 条。`
                          : ''}
                      </p>
                      <ul className="mh-orphan-list">
                        {orphans.map((item) => (
                          <li className="mh-orphan-item" key={item.id}>
                            <span className="mh-orphan-time">{formatWhen(item.time)}</span>
                            <span className="mh-orphan-key">{keyLabel(item.key)}</span>
                            <span className="mh-orphan-value">
                              {truncate(item.value, LINE_CLIP)}
                            </span>
                            <span className={'mh-badge is-' + statusTone(item.status)}>
                              {statusLabel(item.status)}
                            </span>
                            {onOpenMemory ? (
                              <button
                                type="button"
                                className="mh-detail-btn"
                                onClick={() =>
                                  onOpenMemory({
                                    id: item.id,
                                    value: item.value,
                                    key: item.key,
                                    status: item.status,
                                    time: item.time,
                                  })
                                }
                              >
                                详情
                              </button>
                            ) : null}
                          </li>
                        ))}
                      </ul>
                    </div>
                  ) : null}
                </section>
              ) : null}
            </>
          )}
          </>
        ) : null}
      </div>
    </div>
  )
}

export { MemoryHistoryView }
