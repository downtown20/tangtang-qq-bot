import { Fragment, useEffect, useRef, useState } from 'react'
import {
  ArrowDown,
  ChevronRight,
  CircleAlert,
  CircleCheck,
  CircleDashed,
  CircleX,
  Wrench,
} from 'lucide-react'
import type { LucideIcon } from 'lucide-react'
import type {
  ConsoleFlowSummary,
  ConsoleTurnFlowBlock,
  ConsoleTurnFlowPhase,
} from '../lib/api'
import { formatMs } from '../lib/format'
import './TurnReplayFlow.css'

/* ═══════════════════════════════════════════════════════════════
   一条消息的流程图谱（2026-10-25 第 17 轮 二次整改）

   ## 为什么不是时间轴

   主人原话：「像架构图谱一样，直观地展示一条消息的流程、流程中的环节、
   环节中的参数，以及环节与环节之间如何配合。」「使用者是一个小白运营者：
   他需要看懂整个流程，也需要知道哪个环节没有坏、哪个环节出了问题。」

   时间轴只能回答「按顺序发生了什么」，回答不了「这站在干什么」「为什么
   这样走」「哪里坏了」。所以这里改成三层递进：

     ① 体检横幅  —— 一句话结论 + 四态统计 + 出问题的站（点了能跳）
     ② 缩略图谱  —— 9 个节点横向排开，一眼看出链条形状和断点
     ③ 站点卡片  —— 每站：这站做什么 → 判断依据 → 这轮实际发生什么
                    → 参数（带解释）→ 交给下一站什么

   分组（进入/理解/决策/输出）由后端给，前端只按它画泳道——分组跳来跳去
   比时间轴更难自查，所以顺序与连续性钉在测试里。

   ## 四态的视觉纪律

   · 绿 = 正常，黄 = 打折（能用但要看），红 = 异常（要修）
   · **灰 = 未触发，不是故障**——把「没调工具」标红会让人去修不存在的 bug

   ## 前端不做推理

   `role` / `logic` / `did` / `status` / `carries` / 参数解释全部由后端算好，
   这里只负责画。判据只有一处（第 47 条教训：同一语义 N 个实现点，漏一个就静默错）。
   ═══════════════════════════════════════════════════════════════ */

type Tone = 'ok' | 'warn' | 'error' | 'skip'

/** 四态的显示规格（唯一的中文映射点） */
const STATUS_META: Record<Tone, { label: string; hint: string; Icon: LucideIcon }> = {
  ok: {
    label: '正常',
    hint: '这一站完整走完了',
    Icon: CircleCheck,
  },
  warn: {
    label: '打折',
    hint: '走完了但有损失（材料被裁 / 记录缺失）——能用，但值得看一眼',
    Icon: CircleAlert,
  },
  error: {
    label: '异常',
    hint: '这一站该产出的东西没有出现——这才是要修的',
    Icon: CircleX,
  },
  skip: {
    label: '未触发',
    hint: '这一轮本来就不该有这一站（没调工具 / 她决定沉默 / 对方还没回）——不是故障',
    Icon: CircleDashed,
  },
}

/** 阶段分组的副标题（帮小白建立「大阶段」的地图感） */
const GROUP_HINT: Record<string, string> = {
  进入: '消息进来、确定走哪条路',
  理解: '她此刻知道些什么——身份、历史、背景、记忆',
  决策: '查工具、动笔写',
  输出: '发出去、看对方反应',
}

const TONES: Tone[] = ['ok', 'warn', 'error', 'skip']

function toneOf(p: ConsoleTurnFlowPhase): Tone {
  const st = p.status as Tone
  return TONES.includes(st) ? st : 'ok'
}

/** 背景块三态的显示名（`status` 由后端给，这里是唯一的中文映射点） */
const BLOCK_STATUS: Record<string, { label: string; hint: string }> = {
  kept: { label: '保留', hint: '完整进了 prompt' },
  truncated: { label: '被裁', hint: '只留了开头一部分，后面的她看不到' },
  dropped: { label: '被淘汰', hint: '整块没进 prompt，她这一轮完全看不到它' },
}

/* ── 老后端兜底：没有 flow_summary 时按 status 数一遍 ──────────── */
function fallbackSummary(phases: ConsoleTurnFlowPhase[]): ConsoleFlowSummary {
  const counts: Record<Tone, number> = { ok: 0, warn: 0, error: 0, skip: 0 }
  for (const p of phases) counts[toneOf(p)] += 1
  const firstBad = phases.find((p) => toneOf(p) !== 'ok')
  const verdict = counts.error
    ? `有 ${counts.error} 站异常`
    : counts.warn
      ? `没有故障，但有 ${counts.warn} 站打了折`
      : `${phases.length} 站全部正常`
  return {
    total: phases.length,
    ...counts,
    verdict,
    focus_key: firstBad?.key ?? '',
    focus_label: firstBad?.label ?? '',
    broken: phases
      .filter((p) => toneOf(p) !== 'ok')
      .map((p) => ({
        phase: p.phase,
        key: p.key,
        label: p.label,
        status: p.status,
        note: p.note || p.did || '',
      })),
  }
}

/* ═══════════════════════════════════════════════════════════════
   ① 体检横幅
   ═══════════════════════════════════════════════════════════════ */

function VerdictBanner({
  summary,
  onJump,
}: {
  summary: ConsoleFlowSummary
  onJump: (key: string) => void
}) {
  const tone: Tone = summary.error ? 'error' : summary.warn ? 'warn' : 'ok'
  const meta = STATUS_META[tone]
  return (
    <section className={`fg-verdict is-${tone}`} aria-label="这一回合体检">
      <div className="fg-verdict-head">
        <meta.Icon size={15} className="fg-verdict-icon" />
        <span className="fg-verdict-text">{summary.verdict}</span>
      </div>

      <div className="fg-verdict-stats">
        {TONES.map((t) => {
          const n = Number(summary[t] ?? 0)
          if (!n) return null
          const m = STATUS_META[t]
          return (
            <span className={`fg-stat is-${t}`} key={t} title={m.hint}>
              <b>{n}</b> {m.label}
            </span>
          )
        })}
      </div>

      {summary.broken.length > 0 ? (
        <ul className="fg-broken">
          {summary.broken.map((b) => {
            const t = (TONES.includes(b.status as Tone) ? b.status : 'warn') as Tone
            return (
              <li key={b.key}>
                <button
                  type="button"
                  className={`fg-broken-btn is-${t}`}
                  onClick={() => onJump(b.key)}
                  title="跳到这一站"
                >
                  <span className="fg-broken-idx">第 {b.phase} 站</span>
                  <span className="fg-broken-label">{b.label}</span>
                  <span className={`fg-broken-tag is-${t}`}>{STATUS_META[t].label}</span>
                  <ChevronRight size={11} className="fg-broken-arrow" />
                </button>
                {b.note ? <p className="fg-broken-note">{b.note}</p> : null}
              </li>
            )
          })}
        </ul>
      ) : null}
    </section>
  )
}

/* ═══════════════════════════════════════════════════════════════
   ② 缩略图谱：9 个节点排开，点击跳转
   ═══════════════════════════════════════════════════════════════ */

function MiniGraph({
  phases,
  onJump,
}: {
  phases: ConsoleTurnFlowPhase[]
  onJump: (key: string) => void
}) {
  return (
    <nav className="fg-mini" aria-label="流程概览">
      {phases.map((p, i) => {
        const t = toneOf(p)
        const m = STATUS_META[t]
        return (
          <Fragment key={p.key}>
            {i > 0 ? <span className="fg-mini-seg" aria-hidden="true" /> : null}
            <button
              type="button"
              className={`fg-mini-node is-${t}`}
              onClick={() => onJump(p.key)}
              title={`第 ${p.phase} 站 ${p.label} · ${m.label}：${m.hint}`}
            >
              <span className="fg-mini-dot">{p.phase}</span>
              <span className="fg-mini-label">{p.label}</span>
            </button>
          </Fragment>
        )
      })}
    </nav>
  )
}

/* ═══════════════════════════════════════════════════════════════
   背景块表格（「她这轮拿到了哪些材料」）
   ═══════════════════════════════════════════════════════════════ */

function BlockTable({ blocks }: { blocks: ConsoleTurnFlowBlock[] }) {
  const [openIdx, setOpenIdx] = useState<number | null>(null)
  return (
    <div className="fg-blocks-wrap">
      <div className="fg-legend explain-only">
        {BLOCK_STATUS.kept ? (
          <span className="fg-legend-item is-kept">
            <i aria-hidden="true" />
            {BLOCK_STATUS.kept.label}＝{BLOCK_STATUS.kept.hint}
          </span>
        ) : null}
        <span className="fg-legend-item is-trunc">
          <i aria-hidden="true" />
          {BLOCK_STATUS.truncated.label}＝{BLOCK_STATUS.truncated.hint}
        </span>
        <span className="fg-legend-item is-drop">
          <i aria-hidden="true" />
          {BLOCK_STATUS.dropped.label}＝{BLOCK_STATUS.dropped.hint}
        </span>
      </div>
      <div className="fg-blocks" role="table" aria-label="背景材料">
        <div className="fg-blocks-head" role="row">
          <span role="columnheader">材料</span>
          <span role="columnheader">优先级</span>
          <span role="columnheader">token</span>
          <span role="columnheader">命运</span>
        </div>
        {blocks.map((b, i) => {
          const st = BLOCK_STATUS[b.status] ?? { label: b.status || '—', hint: '' }
          const cost =
            b.status === 'dropped'
              ? `—${b.tokens_before ?? 0}t`
              : b.status === 'truncated'
                ? `${b.tokens_before ?? 0}→${b.tokens}t`
                : `${b.tokens}t`
          const preview = (b.preview ?? '').trim()
          const expanded = openIdx === i
          return (
            <div className={`fg-block is-${b.status}`} key={`${b.label}-${i}`}>
              <button
                type="button"
                className="fg-block-row"
                onClick={() => preview && setOpenIdx(expanded ? null : i)}
                aria-expanded={preview ? expanded : undefined}
                disabled={!preview}
              >
                <span className="fg-block-label" role="cell">
                  {preview ? (
                    <ChevronRight
                      size={10}
                      className={'fg-caret' + (expanded ? ' is-open' : '')}
                    />
                  ) : (
                    <span className="fg-caret-space" />
                  )}
                  {b.label || '（未命名）'}
                </span>
                <span className="fg-block-prio" role="cell" title="数字越小越该留下">
                  P{b.priority}
                </span>
                <span className="fg-block-tok" role="cell">{cost}</span>
                <span className="fg-block-status" role="cell" title={st.hint}>
                  {st.label}
                </span>
              </button>
              {expanded && preview ? (
                <pre className="fg-block-preview">{preview}</pre>
              ) : null}
            </div>
          )
        })}
      </div>
    </div>
  )
}

/* ═══════════════════════════════════════════════════════════════
   ③ 站点卡片
   ═══════════════════════════════════════════════════════════════ */

function ParamList({
  params,
  open,
  onToggle,
}: {
  params: ConsoleTurnFlowPhase['params']
  open: boolean
  onToggle: () => void
}) {
  if (params.length === 0) return null
  return (
    <div className="fg-params">
      <button
        type="button"
        className="fg-params-btn"
        onClick={onToggle}
        aria-expanded={open}
      >
        <ChevronRight size={10} className={'fg-caret' + (open ? ' is-open' : '')} />
        这一站的全部参数
        <span className="fg-params-n">{params.length} 项</span>
      </button>
      {open ? (
        <div className="fg-params-body">
          {params.map((it, i) => (
            <div
              className={'fg-param' + (it.status ? ' is-' + it.status : '')}
              key={`${it.k}-${i}`}
            >
              <div className="fg-param-head">
                <span className="fg-param-k">{it.k}</span>
                <span className="fg-param-v">{it.v}</span>
              </div>
              {it.doc ? <p className="fg-param-doc explain-only">{it.doc}</p> : null}
            </div>
          ))}
        </div>
      ) : null}
    </div>
  )
}

function Station({
  p,
  isFirstInGroup,
  isLast,
  open,
  onToggle,
  paramsOpen,
  onToggleParams,
  stationRef,
}: {
  p: ConsoleTurnFlowPhase
  isFirstInGroup: boolean
  isLast: boolean
  open: boolean
  onToggle: () => void
  paramsOpen: boolean
  onToggleParams: () => void
  stationRef: (el: HTMLLIElement | null) => void
}) {
  const t = toneOf(p)
  const meta = STATUS_META[t]
  const blocks = p.blocks ?? []
  const entries = p.entries ?? []

  return (
    <li
      ref={stationRef}
      className={
        'fg-station' +
        ` is-${t}` +
        (open ? ' is-open' : ' is-closed') +
        (isFirstInGroup ? ' is-group-first' : '')
      }
    >
      <span className="fg-node" aria-hidden="true">
        <span className="fg-node-dot">
          <meta.Icon size={11} />
        </span>
      </span>

      <div className="fg-card">
        <button
          type="button"
          className="fg-head"
          onClick={onToggle}
          aria-expanded={open}
          title={open ? '折叠这一站' : '展开这一站'}
        >
          <span className="fg-idx">{String(p.phase).padStart(2, '0')}</span>
          <span className="fg-label">{p.label}</span>
          {p.ms > 0 ? <span className="fg-ms">{formatMs(p.ms)}</span> : null}
          <span className={`fg-pill is-${t}`} title={meta.hint}>
            <meta.Icon size={10} />
            {meta.label}
          </span>
          <ChevronRight
            size={12}
            className={'fg-caret fg-head-caret' + (open ? ' is-open' : '')}
          />
        </button>

        {/* 「这站在干什么」——`explain-only`：讲解模式关掉后隐藏。
            它是解释不是数据；关掉后站点标题 + 状态灯 + 结论仍然完整。 */}
        <p className="fg-role explain-only">{p.role}</p>

        {open ? (
          <>
            {/* 「这一轮实际发生了什么」——结论，主人说「仅做参考」 */}
            {p.did ? (
              <div className="fg-did">
                <meta.Icon size={12} className="fg-did-icon" />
                <span>{p.did}</span>
              </div>
            ) : null}

            {/* 「判断依据」——解释，讲解模式可关 */}
            {p.logic ? (
              <div className="fg-logic explain-only">
                <span className="fg-logic-tag">判断依据</span>
                <span className="fg-logic-text">{p.logic}</span>
              </div>
            ) : null}

            {p.items.length > 0 ? (
              <div className="fg-kvs">
                {p.items.map((it, i) => (
                  <div className="fg-kv" key={`${it.k}-${i}`}>
                    <span>{it.k}</span>
                    <b title={it.v}>{it.v}</b>
                  </div>
                ))}
              </div>
            ) : null}

            {blocks.length > 0 ? <BlockTable blocks={blocks} /> : null}

            {entries.length > 0 ? (
              <div className="fg-entries-wrap">
                <div className="fg-entries-head">
                  <span>她这一轮想起来的</span>
                  <span className="fg-entries-n">{entries.length} 条</span>
                </div>
                <ul className="fg-entries">
                  {entries.map((e, i) => (
                    <li key={i}>{e}</li>
                  ))}
                </ul>
              </div>
            ) : null}

            <ParamList params={p.params} open={paramsOpen} onToggle={onToggleParams} />

            {p.note ? (
              <p className="fg-note">
                <CircleAlert size={11} />
                <span>{p.note}</span>
              </p>
            ) : null}

            {/* 「交给下一站什么」——环节之间如何配合。
                异常站要改口：它这一轮**没交出任何东西**，写「交给下一站」会撒谎。 */}
            {p.carries.length > 0 ? (
              <div className="fg-carries">
                <ArrowDown size={11} className="fg-carries-icon" />
                <span className="fg-carries-label">
                  {isLast
                    ? '留给下一轮'
                    : t === 'error'
                      ? '本该交给下一站'
                      : '交给下一站'}
                </span>
                <span className="fg-chips">
                  {p.carries.map((c, i) => (
                    <span className="fg-chip" key={`${c}-${i}`}>
                      {c}
                    </span>
                  ))}
                </span>
              </div>
            ) : null}
          </>
        ) : null}
      </div>
    </li>
  )
}

/* ═══════════════════════════════════════════════════════════════
   主组件
   ═══════════════════════════════════════════════════════════════ */

export interface FlowGraphProps {
  phases: ConsoleTurnFlowPhase[]
  /** 后端算好的体检结论；老后端没有时按 status 兜底数一遍 */
  summary?: ConsoleFlowSummary | null
}

export default function FlowGraph({ phases, summary }: FlowGraphProps) {
  // 站点默认全展开：主人要「直白、毫无保留」，一进来就该看到每一站在干什么
  const [openStations, setOpenStations] = useState<Record<string, boolean>>({})
  const [openParams, setOpenParams] = useState<Record<string, boolean>>({})
  const nodes = useRef<Record<string, HTMLLIElement | null>>({})

  // 换一条回放就重置折叠状态（上一条展开的部位别带过来）
  useEffect(() => {
    setOpenStations({})
    setOpenParams({})
  }, [phases])

  const summaryView = summary ?? fallbackSummary(phases)
  const totalParams = phases.reduce((n, p) => n + p.params.length, 0)
  const allParamsOpen =
    phases.length > 0 && phases.every((p) => p.params.length === 0 || openParams[p.key])

  const isStationOpen = (key: string) => openStations[key] !== false

  const jump = (key: string) => {
    const el = nodes.current[key]
    if (!el) return
    // 跳到那一站并**保证它是展开的**——跳到一个折叠的空壳上等于没跳
    setOpenStations((prev) => ({ ...prev, [key]: true }))
    el.scrollIntoView({ behavior: 'smooth', block: 'center' })
  }

  const toggleAllParams = () => {
    if (allParamsOpen) {
      setOpenParams({})
      return
    }
    const next: Record<string, boolean> = {}
    for (const p of phases) if (p.params.length) next[p.key] = true
    setOpenParams(next)
  }

  // 分组：后端给 group，连续同名归为一组（连续性有后端测试钉着）
  const groups: { name: string; items: ConsoleTurnFlowPhase[] }[] = []
  for (const p of phases) {
    const last = groups[groups.length - 1]
    if (last && last.name === p.group) last.items.push(p)
    else groups.push({ name: p.group, items: [p] })
  }

  return (
    <div className="fg-graph">
      <VerdictBanner summary={summaryView} onJump={jump} />

      <p className="fg-howto explain-only">
        从上往下看，就是一条消息在她这里走完的一生。每站先说<b>这站在做什么</b>，
        再说<b>这一轮实际发生了什么</b>；点站头可折叠，「全部参数」里每个数字都带解释。
      </p>

      <MiniGraph phases={phases} onJump={jump} />

      <div className="fg-toolbar">
        <span className="fg-toolbar-count">
          {phases.length} 站 · {totalParams} 项参数
        </span>
        {totalParams > 0 ? (
          <button type="button" className="fg-toolbar-btn" onClick={toggleAllParams}>
            {allParamsOpen ? '收起全部参数' : '展开全部参数'}
          </button>
        ) : null}
      </div>

      <div className="fg-track">
        {groups.map((g) => (
          <section className="fg-group" key={g.name}>
            <div className="fg-group-head">
              <span className="fg-group-name">{g.name}</span>
              {GROUP_HINT[g.name] ? (
                <span className="fg-group-hint explain-only">{GROUP_HINT[g.name]}</span>
              ) : null}
              <span className="fg-group-rule" aria-hidden="true" />
              <span className="fg-group-n">{g.items.length} 站</span>
            </div>
            <ol className="fg-stations">
              {g.items.map((p, i) => (
                <Station
                  key={p.key}
                  p={p}
                  isFirstInGroup={i === 0}
                  isLast={p.key === phases[phases.length - 1]?.key}
                  open={isStationOpen(p.key)}
                  onToggle={() =>
                    setOpenStations((prev) => ({
                      ...prev,
                      [p.key]: prev[p.key] === false,
                    }))
                  }
                  paramsOpen={Boolean(openParams[p.key])}
                  onToggleParams={() =>
                    setOpenParams((prev) => ({ ...prev, [p.key]: !prev[p.key] }))
                  }
                  stationRef={(el) => {
                    nodes.current[p.key] = el
                  }}
                />
              ))}
            </ol>
          </section>
        ))}
      </div>

      {/* 空状态不该出现（后端永远给 9 段），但真出现了要说人话而不是白屏 */}
      {phases.length === 0 ? (
        <div className="fg-empty">
          <Wrench size={13} />
          <span>这一回合没有流程记录（后端没返回 flow 字段）</span>
        </div>
      ) : null}
    </div>
  )
}

/* 具名导出与 default 指向同一个组件 */
export { FlowGraph }
