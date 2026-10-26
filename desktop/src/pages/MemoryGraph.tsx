import { useCallback, useEffect, useRef, useState } from 'react'
import type { PointerEvent as ReactPointerEvent, ReactNode } from 'react'
import { ArrowLeft, ChevronRight, LoaderCircle, RefreshCw } from 'lucide-react'
import { api } from '../lib/api'
import type {
  GraphNode,
  GraphNodeKind,
  MemoryGraph,
  MemoryGraphSide,
  MemoryGraphStats,
} from '../lib/api'
import MemoryProfile from './MemoryProfile'
import './MemoryGraph.css'

/* ═══════════════════════════════════════════════════════════════
   记忆图谱 —— 她对每个人的认知长什么样

   两种视图：
     overview：人 —— 语义类别（看「她把世界理解成几块」）
     person  ：人 → 语义簇 → 事实 + 关于 ta 的记忆（点一个人进去）

   所有节点/边由后端组装，这里只负责画。力导向是手写的（约 100 行，
   canvas 2D，零依赖）——数据规模 n≤150，O(n²) 两两斥力足够。
   ═══════════════════════════════════════════════════════════════ */

/* ── 力导向参数 ────────────────────────────────────────────────
   单位是「世界像素」。斥力取 k/d²，弹簧拉向理想长度，重力把图收向原点，
   阻尼 0.85 让总动能衰减；速度上限防止初期重叠时数值炸掉。
   ──────────────────────────────────────────────────────────── */
const DAMPING = 0.85
const REPULSION = 16000
const SPRING = 0.03
const SPRING_LEN = 110
const GRAVITY = 0.0018
const MAX_SPEED = 40
/** 跑满这么多帧就停（约 6.6s）——不要永久 rAF 转圈 */
const MAX_FRAMES = 400
/** 平均动能低于这个值就算稳定，连续 SETTLE_FRAMES 帧后停 */
const SETTLE_KINETIC = 0.05
const SETTLE_FRAMES = 30

/* ── 视图/命中参数 ── */
const MIN_SCALE = 0.4
const MAX_SCALE = 3
const LABEL_MIN_SCALE = 0.6
const LABEL_MIN_RADIUS = 7
const LABEL_MAX_CHARS = 12
const TAU = Math.PI * 2
/** 鼠标移动超过这个距离就算拖拽，不再当点击 */
const CLICK_SLOP = 3

/** 模拟里的节点（位置/速度都在世界坐标里，和画布尺寸无关） */
type SimNode = {
  id: string
  kind: GraphNodeKind
  label: string
  qq: string
  meta: Record<string, string | number>
  /** 后端算好的建议半径，直接用（不套非线性） */
  r: number
  x: number
  y: number
  vx: number
  vy: number
}

type SimEdge = {
  a: SimNode
  b: SimNode
  w: number
}

/** 屏幕 ↔ 世界：screen = world * scale + t */
type View = {
  scale: number
  tx: number
  ty: number
}

type Palette = {
  accent: string
  label: string
  ring: string
  edge: string
  edgeStrong: string
  colors: Record<GraphNodeKind, string>
}

type DragState =
  | {
      mode: 'node'
      pointerId: number
      node: SimNode
      startX: number
      startY: number
      wx: number
      wy: number
      moved: boolean
    }
  | {
      mode: 'pan'
      pointerId: number
      startX: number
      startY: number
      tx: number
      ty: number
      moved: boolean
    }

/* ── 颜色：从 CSS 变量读，深浅主题各一套（见 MemoryGraph.css） ── */
const FALLBACK_COLORS: Record<GraphNodeKind, string> = {
  person: '#4176e6',
  category: '#7c63f0',
  cluster: '#0891b2',
  fact: '#64748b',
  memory: '#ea8a2f',
}

const FALLBACK_PALETTE: Palette = {
  accent: '#4176e6',
  label: '#61666b',
  ring: 'rgba(255,255,255,0.92)',
  edge: 'rgba(15,17,21,0.12)',
  edgeStrong: 'rgba(65,118,230,0.55)',
  colors: FALLBACK_COLORS,
}

function readPalette(): Palette {
  const cs = getComputedStyle(document.documentElement)
  const pick = (name: string, fallback: string): string => {
    const value = cs.getPropertyValue(name).trim()
    return value || fallback
  }
  return {
    accent: pick('--accent', FALLBACK_PALETTE.accent),
    label: pick('--mg-label', FALLBACK_PALETTE.label),
    ring: pick('--mg-ring', FALLBACK_PALETTE.ring),
    edge: pick('--mg-edge', FALLBACK_PALETTE.edge),
    edgeStrong: pick('--mg-edge-strong', FALLBACK_PALETTE.edgeStrong),
    colors: {
      person: pick('--mg-person', FALLBACK_COLORS.person),
      category: pick('--mg-category', FALLBACK_COLORS.category),
      cluster: pick('--mg-cluster', FALLBACK_COLORS.cluster),
      fact: pick('--mg-fact', FALLBACK_COLORS.fact),
      memory: pick('--mg-memory', FALLBACK_COLORS.memory),
    },
  }
}

/* ── 小工具 ── */
function clamp(value: number, lo: number, hi: number): number {
  return Math.min(hi, Math.max(lo, value))
}

function truncateLabel(label: string): string {
  const text = label.trim()
  if (text.length <= LABEL_MAX_CHARS) return text
  return `${text.slice(0, LABEL_MAX_CHARS)}…`
}

function formatCount(value: number): string {
  return Number.isFinite(value) ? Math.max(0, Math.round(value)).toLocaleString() : '—'
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

/** 置信度 0~1 → 百分比；后端万一给的是百分数也认 */
function formatConfidence(value: number | null): string {
  if (value === null) return '—'
  const pct = value <= 1 ? value * 100 : value
  return `${Math.round(pct)}%`
}

function formatWhen(raw: string): string {
  if (!raw) return ''
  return raw.replace('T', ' ').slice(0, 16)
}

function episodeTime(start: string, end: string): string {
  const a = formatWhen(start)
  const b = formatWhen(end)
  if (a && b && a !== b) return `${a} → ${b}`
  return a || b || '时间未记录'
}

/* ── 图数据 → 模拟状态 ── */

/** 初始布局：中心节点在原点，其余按黄金角螺旋铺满一个圆盘（分布均匀，不靠随机） */
function layoutNodes(nodes: SimNode[], centerId: string | null): void {
  const total = nodes.length
  if (total === 0) return
  const ring = 80 + Math.sqrt(total) * 46
  const others = centerId ? total - 1 : total
  let placed = 0
  for (const node of nodes) {
    if (centerId && node.id === centerId) {
      node.x = 0
      node.y = 0
      continue
    }
    const angle = (placed + 1) * 2.399963229728653
    const radius = ring * Math.sqrt((placed + 0.5) / Math.max(1, others))
    node.x = Math.cos(angle) * radius
    node.y = Math.sin(angle) * radius
    placed += 1
  }
}

function buildSim(graph: MemoryGraph): { nodes: SimNode[]; edges: SimEdge[] } {
  const nodes: SimNode[] = graph.nodes.map((node) => ({
    id: node.id,
    kind: node.kind,
    label: node.label,
    qq: node.qq ?? '',
    meta: node.meta,
    r: clamp(Number.isFinite(node.size) ? node.size : 6, 3, 48),
    x: 0,
    y: 0,
    vx: 0,
    vy: 0,
  }))
  const byId = new Map<string, SimNode>()
  for (const node of nodes) byId.set(node.id, node)

  const edges: SimEdge[] = []
  for (const edge of graph.edges) {
    const a = byId.get(edge.source)
    const b = byId.get(edge.target)
    if (!a || !b || a === b) continue
    edges.push({ a, b, w: Number.isFinite(edge.weight) ? edge.weight : 1 })
  }

  const centerId = graph.mode === 'person' ? `person:${graph.center}` : null
  layoutNodes(nodes, centerId)
  return { nodes, edges }
}

function buildAdjacency(edges: SimEdge[]): Map<string, Set<string>> {
  const map = new Map<string, Set<string>>()
  const link = (id: string, other: string): void => {
    let set = map.get(id)
    if (!set) {
      set = new Set<string>()
      map.set(id, set)
    }
    set.add(other)
  }
  for (const edge of edges) {
    link(edge.a.id, edge.b.id)
    link(edge.b.id, edge.a.id)
  }
  return map
}

/**
 * 一步物理积分。返回平均动能（用来判定「已经稳定，可以停 rAF」）。
 * 斥力/弹簧都直接累加成速度增量，再统一乘阻尼——经典 velocity-Verlet 简化版。
 */
function stepPhysics(nodes: SimNode[], edges: SimEdge[]): number {
  const count = nodes.length
  if (count === 0) return 0

  // 两两斥力 k/d²（n≤150，O(n²) 约 1.1 万次/帧，够用）
  for (let i = 0; i < count; i += 1) {
    const a = nodes[i]
    for (let j = i + 1; j < count; j += 1) {
      const b = nodes[j]
      const dx = a.x - b.x
      const dy = a.y - b.y
      const d2 = Math.max(dx * dx + dy * dy, 1)
      const d = Math.sqrt(d2)
      // 类别节点之间额外加权：它们被几十条边同时拽向中心，默认斥力压不住，
      // 8 个紫色球会在正中间挤成一坨互相重叠（2026-10-24 实拍确认）。
      const boost = a.kind === 'category' && b.kind === 'category' ? 6 : 1
      const force = (REPULSION / d2) * boost
      const ux = dx / d
      const uy = dy / d
      a.vx += ux * force
      a.vy += uy * force
      b.vx -= ux * force
      b.vy -= uy * force
    }
  }

  // 边当弹簧，理想长度随两端半径放大（大节点需要更多空间）
  for (const edge of edges) {
    const dx = edge.b.x - edge.a.x
    const dy = edge.b.y - edge.a.y
    const d = Math.max(Math.sqrt(dx * dx + dy * dy), 0.01)
    const ideal = SPRING_LEN + edge.a.r + edge.b.r
    const force = (d - ideal) * SPRING
    const ux = dx / d
    const uy = dy / d
    edge.a.vx += ux * force
    edge.a.vy += uy * force
    edge.b.vx -= ux * force
    edge.b.vy -= uy * force
  }

  let kinetic = 0
  for (const node of nodes) {
    // 轻微向心力：没有它，孤立节点会飘到无穷远
    // 类别节点少受向心力：它们本来就该待在外圈把人群分开，
    // 被拉进中心只会跟别的类别撞上。
    const gravity = node.kind === 'category' ? GRAVITY * 0.25 : GRAVITY
    node.vx -= node.x * gravity
    node.vy -= node.y * gravity
    node.vx *= DAMPING
    node.vy *= DAMPING
    const speed = Math.sqrt(node.vx * node.vx + node.vy * node.vy)
    if (speed > MAX_SPEED) {
      const k = MAX_SPEED / speed
      node.vx *= k
      node.vy *= k
    }
    node.x += node.vx
    node.y += node.vy
    kinetic += node.vx * node.vx + node.vy * node.vy
  }
  return kinetic / count
}

/* ── 侧栏展示用的静态映射 ── */

const KIND_LABELS: Record<GraphNodeKind, string> = {
  person: '人',
  category: '类别',
  cluster: '语义簇',
  fact: '事实',
  memory: '记忆',
}

const STAT_DEFS: { key: keyof MemoryGraphStats; label: string; hint: string }[] = [
  { key: 'people_known', label: '人', hint: 'people_known：记录了昵称的人' },
  { key: 'clusters', label: '个语义簇', hint: 'fact_clusters' },
  { key: 'facts', label: '条事实', hint: 'cluster_facts' },
  { key: 'memories', label: '条记忆', hint: 'memories' },
  { key: 'evidence', label: '条证据链', hint: 'memory_evidence' },
  { key: 'episodes', label: '段经历', hint: 'episodes' },
]

const LEGEND: { kind: GraphNodeKind; label: string; note: string }[] = [
  { kind: 'person', label: '人', note: '她认识的人' },
  { kind: 'category', label: '类别', note: '语义簇的归类' },
  { kind: 'cluster', label: '语义簇', note: '对一个人形成的理解' },
  { kind: 'fact', label: '事实', note: '簇里的具体事实' },
  { kind: 'memory', label: '记忆', note: '关于 ta 的记忆' },
]

/* ── 侧栏小组件 ── */

function Dot({ kind }: { kind: GraphNodeKind }) {
  return <span className="mg-dot" style={{ background: `var(--mg-${kind})` }} />
}

/** overview 未选中：说明 + 五色图例 */
function OverviewLegend() {
  return (
    <div className="mg-side-inner">
      <div className="mg-side-title is-sm">她脑子里的世界</div>
      <p className="mg-side-desc">点击一个人，看她对他形成了什么理解。</p>
      <div className="mg-legend explain-only">
        {LEGEND.map((item) => (
          <div className="mg-legend-item" key={item.kind}>
            <Dot kind={item.kind} />
            <span className="mg-legend-label">{item.label}</span>
            <span className="mg-legend-note">{item.note}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

/** overview 选中一个人：这里只看「她记得多少」，深入请看完整认知 */
function OverviewPerson({ node, onEnter }: { node: GraphNode; onEnter: () => void }) {
  const intimacy = metaNum(node.meta, 'intimacy')
  const clusters = metaNum(node.meta, 'clusters')
  const memories = metaNum(node.meta, 'memories')
  const qq = node.qq || metaStr(node.meta, 'qq_id')
  return (
    <div className="mg-side-inner">
      <div className="mg-side-head">
        <div className="mg-kind">
          <Dot kind="person" />
          <span>人</span>
        </div>
        <div className="mg-side-title">{node.label || '未记录昵称'}</div>
        <div className="mg-side-sub">{qq ? `QQ ${qq}` : 'QQ 未记录'}</div>
      </div>
      <div className="mg-rows">
        <div className="mg-row">
          <span>亲密度</span>
          <b>{intimacy ?? '—'}</b>
        </div>
        <div className="mg-row">
          <span>簇数</span>
          <b>{clusters ?? '—'}</b>
        </div>
        <div className="mg-row">
          <span>记忆数</span>
          <b>{memories ?? '—'}</b>
        </div>
      </div>
      <button className="btn btn-primary mg-enter" onClick={onEnter} disabled={!qq}>
        查看完整认知
        <ChevronRight size={14} />
      </button>
      <div className="mg-hint">点空白处取消选中</div>
    </div>
  )
}

/** 节点详情：按 kind 显示后端给的 meta（键名见 console_api.memory_graph） */
function nodeMetaRows(node: GraphNode): { label: string; value: string }[] {
  const meta = node.meta
  const numberText = (value: number | null): string => (value === null ? '—' : String(value))
  switch (node.kind) {
    case 'cluster':
      return [
        {
          label: '类别',
          value: metaStr(meta, 'category_label') || metaStr(meta, 'category') || '—',
        },
        { label: '事实数', value: numberText(metaNum(meta, 'fact_count')) },
        { label: '长期性', value: metaStr(meta, 'permanence') || '—' },
      ]
    case 'fact':
      return [
        { label: '置信度', value: formatConfidence(metaNum(meta, 'confidence')) },
        { label: '重要度', value: numberText(metaNum(meta, 'importance')) },
      ]
    case 'memory':
      return [
        { label: 'key', value: metaStr(meta, 'key') || '—' },
        { label: '来源', value: metaStr(meta, 'origin') || '—' },
        { label: '时间', value: formatWhen(metaStr(meta, 'event_time')) || '—' },
        { label: '重要度', value: numberText(metaNum(meta, 'importance')) },
      ]
    case 'person':
      return [
        { label: '昵称', value: node.label || '—' },
        { label: 'QQ', value: node.qq || metaStr(meta, 'qq_id') || '—' },
        { label: '亲密度', value: numberText(metaNum(meta, 'intimacy')) },
        { label: '关系', value: metaStr(meta, 'relationship') || '—' },
      ]
    case 'category':
      return [
        { label: '类别', value: metaStr(meta, 'category') || '—' },
        { label: '簇数', value: numberText(metaNum(meta, 'clusters')) },
        { label: '事实数', value: numberText(metaNum(meta, 'facts')) },
      ]
    default:
      return []
  }
}

function NodeDetail({ node }: { node: GraphNode }) {
  const rows = nodeMetaRows(node)
  const summary = metaStr(node.meta, 'summary')
  return (
    <div className="mg-side-inner">
      <div className="mg-side-head">
        <div className="mg-kind">
          <Dot kind={node.kind} />
          <span>{KIND_LABELS[node.kind]}</span>
        </div>
        <div className="mg-side-title is-wrap">{node.label || '（无标题）'}</div>
      </div>
      {rows.length > 0 ? (
        <div className="mg-rows">
          {rows.map((row) => (
            <div className="mg-row" key={row.label}>
              <span>{row.label}</span>
              <b>{row.value}</b>
            </div>
          ))}
        </div>
      ) : null}
      {summary ? (
        <div className="mg-block">
          <div className="mg-block-title">摘要</div>
          <p className="mg-quote">{summary}</p>
        </div>
      ) : null}
      <div className="mg-hint">点空白处取消选中</div>
    </div>
  )
}

/** person 模式未选中：这个人的关系摘要 + 记忆 + 共同经历 */
function PersonSide({ side }: { side: MemoryGraphSide }) {
  const person = side.person
  const nickname = metaStr(person, 'nickname') || '未记录昵称'
  const qq = metaStr(person, 'qq_id') || metaStr(person, 'qq')
  const intimacy = metaNum(person, 'intimacy')
  const relationship = metaStr(person, 'relationship')
  const summary = metaStr(person, 'relationship_summary')

  return (
    <div className="mg-side-inner">
      <div className="mg-side-head">
        <div className="mg-kind">
          <Dot kind="person" />
          <span>人</span>
        </div>
        <div className="mg-side-title">{nickname}</div>
        <div className="mg-side-sub">{qq ? `QQ ${qq}` : 'QQ 未记录'}</div>
      </div>

      <div className="mg-pills">
        <span className="mg-pill">亲密度 {intimacy ?? '—'}</span>
        {relationship ? <span className="mg-pill">{relationship}</span> : null}
      </div>

      {summary ? (
        <div className="mg-block">
          <div className="mg-block-title">关系摘要</div>
          <p className="mg-quote">{summary}</p>
        </div>
      ) : null}

      <div className="mg-block">
        <div className="mg-block-title">
          记忆
          <span className="mg-block-count">{side.memories.length}</span>
        </div>
        {side.memories.length === 0 ? (
          <div className="mg-empty-line">还没有关于 ta 的记忆</div>
        ) : (
          <ul className="mg-list mg-scroll">
            {side.memories.map((memory) => (
              <li className="mg-item" key={memory.id}>
                <div className="mg-item-head">
                  <span className="mg-item-key">{memory.key || '未分类'}</span>
                  <span className="mg-item-meta">重要度 {memory.importance}</span>
                </div>
                <div className="mg-item-text">{memory.value}</div>
                <div className="mg-item-foot">
                  {memory.origin ? `来源 ${memory.origin}` : '来源未记录'}
                  {memory.event_time ? ` · ${formatWhen(memory.event_time)}` : ''}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mg-block">
        <div className="mg-block-title">
          共同经历
          <span className="mg-block-count">{side.episodes.length}</span>
        </div>
        {side.episodes.length === 0 ? (
          <div className="mg-empty-line">还没有共同经历</div>
        ) : (
          <ul className="mg-list mg-scroll">
            {side.episodes.map((episode) => (
              <li className="mg-item" key={episode.id}>
                <div className="mg-item-head">
                  <span className="mg-item-key">{episode.title || '未命名经历'}</span>
                </div>
                {episode.summary ? <div className="mg-item-text">{episode.summary}</div> : null}
                <div className="mg-item-foot">
                  {episodeTime(episode.time_start, episode.time_end)}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      <div className="mg-hint">点任意节点看她对它的细节</div>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   页面主组件
   ═══════════════════════════════════════════════════════════════ */

export default function MemoryGraphPage() {
  const [data, setData] = useState<MemoryGraph | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState('')
  const [selectedId, setSelectedId] = useState<string | null>(null)
  /**
   * 非空 = 正在看这个人的「记忆剖面」（七维索引 + L0-L5 分层）。
   * 剖面盖住整个图谱区域，onBack 回到图谱（2026-10-24）。
   */
  const [profileQq, setProfileQq] = useState('')

  const wrapRef = useRef<HTMLDivElement>(null)
  const canvasRef = useRef<HTMLCanvasElement>(null)

  /* 以下全部是「画布自己的状态」：放 ref 里，动画帧不碰 React state */
  const sizeRef = useRef({ w: 0, h: 0, dpr: 1 })
  const viewRef = useRef<View>({ scale: 1, tx: 0, ty: 0 })
  const simRef = useRef<{ nodes: SimNode[]; edges: SimEdge[] } | null>(null)
  const adjRef = useRef<Map<string, Set<string>>>(new Map())
  const hoverRef = useRef<string | null>(null)
  const selectedRef = useRef<string | null>(null)
  const dragRef = useRef<DragState | null>(null)
  const paletteRef = useRef<Palette>(FALLBACK_PALETTE)
  const rafRef = useRef(0)
  const frameRef = useRef(0)
  const settleRef = useRef(0)
  /** 请求序号：切换视图时丢弃迟到的旧响应 */
  const requestRef = useRef(0)

  /** 选中：state 给 React 渲染侧栏，ref 给 canvas 绘制（避免每帧闭包里读到旧值） */
  const select = useCallback((id: string | null) => {
    selectedRef.current = id
    setSelectedId(id)
  }, [])

  /* ── 绘制：一次完整的 canvas 重绘（DPR 缩放 + 世界变换） ── */
  const drawScene = useCallback(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const ctx = canvas.getContext('2d')
    if (!ctx) return

    // 清屏按设备像素来清，否则高分屏会留残影
    ctx.setTransform(1, 0, 0, 1, 0, 0)
    ctx.clearRect(0, 0, canvas.width, canvas.height)

    const sim = simRef.current
    const { w, h, dpr } = sizeRef.current
    if (!sim || w === 0 || h === 0) return

    const view = viewRef.current
    const palette = paletteRef.current
    const hovered = hoverRef.current
    const neighbors = hovered !== null ? adjRef.current.get(hovered) : undefined
    const selected = selectedRef.current

    // 先按 DPR 放大，再做「世界 → 屏幕」的平移缩放
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0)
    ctx.translate(view.tx, view.ty)
    ctx.scale(view.scale, view.scale)

    /* 边：悬停时只留和它相连的那些，其余淡掉 */
    ctx.lineCap = 'round'
    for (const edge of sim.edges) {
      const active = hovered !== null && (edge.a.id === hovered || edge.b.id === hovered)
      const dimmed = hovered !== null && !active
      ctx.globalAlpha = dimmed ? 0.07 : active ? 0.6 : 0.85
      ctx.strokeStyle = active ? palette.edgeStrong : palette.edge
      ctx.lineWidth = (active ? 1.6 : 0.9) / view.scale
      ctx.beginPath()
      ctx.moveTo(edge.a.x, edge.a.y)
      ctx.lineTo(edge.b.x, edge.b.y)
      ctx.stroke()
    }

    /* 节点 + 标签 */
    for (const node of sim.nodes) {
      const linked =
        hovered === null || node.id === hovered || (neighbors?.has(node.id) ?? false)
      const alpha = linked ? 1 : 0.18

      ctx.globalAlpha = alpha
      ctx.beginPath()
      ctx.arc(node.x, node.y, node.r, 0, TAU)
      ctx.fillStyle = palette.colors[node.kind]
      ctx.fill()
      ctx.lineWidth = 1.2 / view.scale
      ctx.strokeStyle = palette.ring
      ctx.stroke()

      if (node.id === selected) {
        ctx.globalAlpha = 1
        ctx.beginPath()
        ctx.arc(node.x, node.y, node.r + 3.5, 0, TAU)
        ctx.lineWidth = 2 / view.scale
        ctx.strokeStyle = palette.accent
        ctx.stroke()
      }

      // 标签：缩放太小或节点太小就不画，免得糊成一团；悬停的节点总是画
      const showLabel =
        node.id === hovered ||
        (view.scale >= LABEL_MIN_SCALE && node.r * view.scale >= LABEL_MIN_RADIUS)
      if (showLabel) {
        ctx.globalAlpha = linked ? 0.95 : 0.3
        const weight = node.id === hovered ? 600 : 400
        ctx.font = `${weight} ${11 / view.scale}px "Segoe UI", "PingFang SC", "Microsoft YaHei", sans-serif`
        ctx.textAlign = 'center'
        ctx.textBaseline = 'top'
        ctx.fillStyle = palette.label
        ctx.fillText(truncateLabel(node.label), node.x, node.y + node.r + 4 / view.scale)
      }
    }
    ctx.globalAlpha = 1
  }, [])

  /* ── 尺寸：ResizeObserver 跟进容器；canvas 宽高乘 DPR 再 ctx.scale ── */
  const measure = useCallback(() => {
    const wrap = wrapRef.current
    const canvas = canvasRef.current
    if (!wrap || !canvas) return
    const rect = wrap.getBoundingClientRect()
    const w = Math.max(1, Math.round(rect.width))
    const h = Math.max(1, Math.round(rect.height))
    const dpr = Math.max(1, window.devicePixelRatio || 1)
    const prev = sizeRef.current
    const view = viewRef.current

    // 尺寸变化时让世界原点继续落在画布中心（首次 prev 为 0，等于居中）
    view.tx += (w - prev.w) / 2
    view.ty += (h - prev.h) / 2
    sizeRef.current = { w, h, dpr }
    canvas.width = Math.max(1, Math.round(w * dpr))
    canvas.height = Math.max(1, Math.round(h * dpr))
    drawScene()
  }, [drawScene])

  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap) return
    measure()
    const observer = new ResizeObserver(() => measure())
    observer.observe(wrap)
    return () => observer.disconnect()
    // profileQq 变化会整页卸载/重挂 canvas（剖面盖住图谱），
    // 必须重新测量并重新 observe 新的容器节点，否则返回后画布尺寸不跟手。
  }, [measure, profileQq])

  /* 进入剖面时把物理循环停掉：canvas 已经不在 DOM 里，继续算只是白烧 CPU */
  useEffect(() => {
    if (!profileQq) return
    cancelAnimationFrame(rafRef.current)
    rafRef.current = 0
  }, [profileQq])

  /* ── 力导向循环：跑满 400 帧或动能足够小就停，停止即不再 rAF ── */
  const startSim = useCallback(() => {
    cancelAnimationFrame(rafRef.current)
    rafRef.current = 0
    frameRef.current = 0
    settleRef.current = 0

    const tick = () => {
      rafRef.current = 0
      const sim = simRef.current
      if (!sim || sim.nodes.length === 0) {
        drawScene()
        return
      }
      const drag = dragRef.current
      const dragging = drag !== null && drag.mode === 'node'
      const kinetic = stepPhysics(sim.nodes, sim.edges)

      // 被拖的节点钉在指针上：位置写死、速度清零，否则弹簧会把它拽回去
      if (drag && drag.mode === 'node') {
        drag.node.x = drag.wx
        drag.node.y = drag.wy
        drag.node.vx = 0
        drag.node.vy = 0
      }

      if (dragging) {
        // 拖动期间一直跑（松手后再按正常规则收敛/停止）
        settleRef.current = 0
      } else {
        frameRef.current += 1
        if (kinetic < SETTLE_KINETIC) settleRef.current += 1
        else settleRef.current = 0
      }

      drawScene()

      if (!dragging && (frameRef.current >= MAX_FRAMES || settleRef.current >= SETTLE_FRAMES)) {
        return
      }
      rafRef.current = requestAnimationFrame(tick)
    }
    rafRef.current = requestAnimationFrame(tick)
  }, [drawScene])

  /** 拖节点时把循环叫醒（已停就重启，不会叠两条 rAF） */
  const ensureSim = useCallback(() => {
    if (rafRef.current === 0) startSim()
  }, [startSim])

  /* ── 数据：图数据变化时重建模拟，并重启收敛 ── */
  useEffect(() => {
    if (!data) return
    const sim = buildSim(data)
    simRef.current = sim
    adjRef.current = buildAdjacency(sim.edges)
    hoverRef.current = null
    dragRef.current = null
    selectedRef.current = null
    const { w, h } = sizeRef.current
    viewRef.current = { scale: 1, tx: w / 2, ty: h / 2 }
    startSim()
    return () => {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = 0
    }
  }, [data, startSim])

  /* 卸载时确保循环停掉 */
  useEffect(
    () => () => {
      cancelAnimationFrame(rafRef.current)
      rafRef.current = 0
    },
    [],
  )

  /* ── 主题：浅色/深色各一套颜色，切主题时重读 CSS 变量并重绘 ── */
  useEffect(() => {
    paletteRef.current = readPalette()
    drawScene()
    const observer = new MutationObserver(() => {
      paletteRef.current = readPalette()
      drawScene()
    })
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    })
    return () => observer.disconnect()
  }, [drawScene])

  /* ── 滚轮缩放：以鼠标位置为锚点，0.4x~3x ── */
  useEffect(() => {
    const canvas = canvasRef.current
    if (!canvas) return
    const onWheel = (event: WheelEvent) => {
      event.preventDefault()
      const rect = canvas.getBoundingClientRect()
      const sx = event.clientX - rect.left
      const sy = event.clientY - rect.top
      const view = viewRef.current
      // 行模式的 deltaY 单位是「行」，换算成像素再算缩放
      const delta = event.deltaMode === 1 ? event.deltaY * 16 : event.deltaY
      const nextScale = clamp(view.scale * Math.exp(-delta * 0.0015), MIN_SCALE, MAX_SCALE)
      if (nextScale === view.scale) return
      // 锚点下的世界坐标缩放前后保持不动
      const wx = (sx - view.tx) / view.scale
      const wy = (sy - view.ty) / view.scale
      view.scale = nextScale
      view.tx = sx - wx * nextScale
      view.ty = sy - wy * nextScale
      hoverRef.current = null
      drawScene()
    }
    canvas.addEventListener('wheel', onWheel, { passive: false })
    return () => canvas.removeEventListener('wheel', onWheel)
    // 依赖里必须带 `profileQq`：进剖面时组件**不卸载**、只是 return 了
    // `<MemoryProfile>`，canvas 子树被 React 移除；返回图谱时又新建了一个
    // canvas 元素。effect 不重跑就还挂在那个**已经被移除的旧元素**上，
    // 新 canvas 收不到 wheel —— 表现就是「进过一次剖面后滚轮再也缩不了」。
    // （指针拖拽走的是 JSX onPointerDown，React 会自动重绑到新元素，所以没这个坑。）
  }, [drawScene, profileQq])

  /* ── 命中测试：屏幕坐标 → 世界坐标 → 由后往前找（后画的在上层） ── */
  const hitTest = useCallback((sx: number, sy: number): SimNode | null => {
    const sim = simRef.current
    if (!sim) return null
    const view = viewRef.current
    const wx = (sx - view.tx) / view.scale
    const wy = (sy - view.ty) / view.scale
    for (let i = sim.nodes.length - 1; i >= 0; i -= 1) {
      const node = sim.nodes[i]
      const dx = wx - node.x
      const dy = wy - node.y
      // 小节点很难点，命中半径额外放宽 4 个屏幕像素
      const r = node.r + 4 / view.scale
      if (dx * dx + dy * dy <= r * r) return node
    }
    return null
  }, [])

  /* ── 数据加载 ── */
  const load = useCallback(
    async (center: string) => {
      const seq = requestRef.current + 1
      requestRef.current = seq
      setLoading(true)
      setError('')
      try {
        const graph = await api.memoryGraph(center)
        if (requestRef.current !== seq) return
        setData(graph)
        select(null)
      } catch (e) {
        if (requestRef.current !== seq) return
        setError(e instanceof Error ? e.message : String(e))
      } finally {
        if (requestRef.current === seq) setLoading(false)
      }
    },
    [select],
  )

  useEffect(() => {
    void load('')
  }, [load])

  const mode = data?.mode ?? 'overview'
  const center = data?.mode === 'person' ? data.center : ''
  const stats = data?.stats ?? null
  const selectedNode =
    data && selectedId ? (data.nodes.find((node) => node.id === selectedId) ?? null) : null
  const personName =
    data?.mode === 'person'
      ? metaStr(data.side?.person ?? {}, 'nickname') || `QQ${data.center}`
      : ''

  const enterPerson = (qq: string) => {
    if (qq) void load(qq)
  }

  /* ── 指针交互：拖节点 / 拖空白平移 / 点击选中 ── */
  const localPoint = (
    event: ReactPointerEvent<HTMLCanvasElement>,
  ): { x: number; y: number } => {
    const rect = event.currentTarget.getBoundingClientRect()
    return { x: event.clientX - rect.left, y: event.clientY - rect.top }
  }

  /**
   * 点击节点：
   *  - overview 点人 → 选中（右栏显示「她对 ta 的认知」概要，按钮进 person 模式）
   *  - person 点任意节点 → 右栏显示该节点 meta
   *  - 点空白 → 取消选中
   */
  const handleNodeClick = (node: SimNode) => {
    if (!data) return
    if (data.mode === 'overview') {
      // overview 只让「人」可选：类别/簇/事实在全局图里只是聚合锚点
      if (node.kind === 'person') select(node.id)
      return
    }
    select(node.id)
  }

  const onPointerDown = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    if (event.button !== 0) return
    const canvas = event.currentTarget
    const { x, y } = localPoint(event)
    const hit = hitTest(x, y)
    canvas.setPointerCapture(event.pointerId)

    if (hit) {
      hit.vx = 0
      hit.vy = 0
      dragRef.current = {
        mode: 'node',
        pointerId: event.pointerId,
        node: hit,
        startX: x,
        startY: y,
        wx: hit.x,
        wy: hit.y,
        moved: false,
      }
      canvas.style.cursor = 'grabbing'
      ensureSim()
    } else {
      const view = viewRef.current
      dragRef.current = {
        mode: 'pan',
        pointerId: event.pointerId,
        startX: x,
        startY: y,
        tx: view.tx,
        ty: view.ty,
        moved: false,
      }
      canvas.style.cursor = 'grabbing'
    }
  }

  const onPointerMove = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const canvas = event.currentTarget
    const { x, y } = localPoint(event)
    const drag = dragRef.current

    if (drag && drag.pointerId === event.pointerId) {
      const dx = x - drag.startX
      const dy = y - drag.startY
      if (!drag.moved && (Math.abs(dx) > CLICK_SLOP || Math.abs(dy) > CLICK_SLOP)) {
        drag.moved = true
      }
      if (drag.mode === 'node') {
        if (drag.moved) {
          const view = viewRef.current
          drag.wx = (x - view.tx) / view.scale
          drag.wy = (y - view.ty) / view.scale
          drag.node.x = drag.wx
          drag.node.y = drag.wy
          ensureSim()
          drawScene()
        }
      } else {
        viewRef.current.tx = drag.tx + dx
        viewRef.current.ty = drag.ty + dy
        drawScene()
      }
      return
    }

    // 悬停高亮：只更新 ref 再重绘，不进 React state（否则每动一下就 render）
    const hit = hitTest(x, y)
    const nextId = hit ? hit.id : null
    if (nextId !== hoverRef.current) {
      hoverRef.current = nextId
      drawScene()
    }
    canvas.style.cursor = hit ? 'pointer' : 'grab'
  }

  const onPointerUp = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    const canvas = event.currentTarget
    const drag = dragRef.current
    dragRef.current = null
    if (canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId)
    }
    canvas.style.cursor = 'grab'
    if (!drag) return
    if (!drag.moved) {
      if (drag.mode === 'node') handleNodeClick(drag.node)
      else select(null)
    }
    drawScene()
  }

  /** 触摸被系统打断：只清拖拽状态，不要把它当成一次点击 */
  const onPointerCancel = (event: ReactPointerEvent<HTMLCanvasElement>) => {
    dragRef.current = null
    const canvas = event.currentTarget
    if (canvas.hasPointerCapture(event.pointerId)) {
      canvas.releasePointerCapture(event.pointerId)
    }
    canvas.style.cursor = 'grab'
    drawScene()
  }

  const onPointerLeave = () => {
    if (dragRef.current) return
    if (hoverRef.current !== null) {
      hoverRef.current = null
      drawScene()
    }
    const canvas = canvasRef.current
    if (canvas) canvas.style.cursor = 'grab'
  }

  /* ── 侧栏：按「视图 + 选中」决定显示哪一块 ── */
  const renderSide = (): ReactNode => {
    if (!data) return null
    if (data.mode === 'overview') {
      if (selectedNode && selectedNode.kind === 'person') {
        return (
          <OverviewPerson
            node={selectedNode}
            onEnter={() => enterPerson(selectedNode.qq ?? '')}
          />
        )
      }
      return <OverviewLegend />
    }
    if (selectedNode) return <NodeDetail node={selectedNode} />
    if (data.side) return <PersonSide side={data.side} />
    return <OverviewLegend />
  }

  /* 剖面盖住整页（含图谱画布）——返回时回到原来的 person 视图 */
  if (profileQq) {
    return <MemoryProfile qq={profileQq} onBack={() => setProfileQq('')} />
  }

  return (
    <div className="mg-page">
      <div className="mg-top">
        <div className="mg-top-main">
          {data?.mode === 'person' ? (
            <div className="mg-crumb">
              <button
                type="button"
                className="mg-back"
                onClick={() => void load('')}
                disabled={loading}
              >
                <ArrowLeft size={13} />
                全部人
              </button>
              <span className="mg-crumb-sep">/</span>
              <span className="mg-crumb-cur">{personName}</span>
            </div>
          ) : null}
          <h1 className="mg-title">记忆图谱</h1>
          <p className="mg-sub">她对每个人的认知：人 → 语义簇 → 事实，外加记忆与共同经历。</p>
        </div>
        <div className="mg-actions">
          <button
            type="button"
            className="btn btn-ghost"
            onClick={() => void load(center)}
            disabled={loading}
          >
            <RefreshCw size={14} className={loading ? 'mg-spin' : ''} />
            刷新
          </button>
        </div>
      </div>

      {mode === 'overview' && stats ? (
        <div className="mg-stats">
          {STAT_DEFS.map((def) => (
            <div className="mg-stat" key={def.key} title={def.hint}>
              <span className="mg-stat-num">{formatCount(stats[def.key])}</span>
              <span className="mg-stat-label">{def.label}</span>
            </div>
          ))}
        </div>
      ) : null}

      {error ? (
        <div className="mg-error" role="alert">
          <span className="mg-error-text">{error}</span>
          <button
            type="button"
            className="btn btn-ghost btn-sm"
            onClick={() => void load(center)}
            disabled={loading}
          >
            重试
          </button>
        </div>
      ) : null}

      <div className="mg-body">
        <div className="mg-canvas-wrap" ref={wrapRef}>
          <canvas
            ref={canvasRef}
            className="mg-canvas"
            onPointerDown={onPointerDown}
            onPointerMove={onPointerMove}
            onPointerUp={onPointerUp}
            onPointerCancel={onPointerCancel}
            onPointerLeave={onPointerLeave}
            onContextMenu={(event) => event.preventDefault()}
          />
          {loading && !data ? (
            <div className="mg-overlay">
              <LoaderCircle size={16} className="mg-spin" />
              正在读取她的记忆…
            </div>
          ) : null}
          {data && data.nodes.length === 0 ? (
            <div className="mg-overlay">她还没有对任何人形成理解</div>
          ) : null}
          {!loading && !data && !error ? (
            <div className="mg-overlay">她还没有对任何人形成理解</div>
          ) : null}
          {mode === 'person' && data && !selectedNode ? (
            <div className="mg-corner-hint">点节点看细节 · 滚轮缩放 · 拖空白平移</div>
          ) : null}
        </div>

        <aside className="mg-side">
          {data?.mode === 'person' && center ? (
            <div className="mg-side-profile">
              <button
                type="button"
                className="mg-profile-btn"
                onClick={() => setProfileQq(center)}
              >
                展开完整记忆剖面
                <ChevronRight size={14} />
              </button>
            </div>
          ) : null}
          {renderSide()}
        </aside>
      </div>
    </div>
  )
}

/* 具名导出与 default 指向同一个组件：接线方两种写法都能用 */
export { MemoryGraphPage }
