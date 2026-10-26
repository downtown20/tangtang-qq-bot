/**
 * 她这一轮「收到的 prompt」账本（2026-10-25 第 47 轮）。
 *
 * 主人原话：「后端还有很多参数可以调节，把必要的参数放到前端，比如各环节的
 * token 预算等等」+「作为运营者我需要更多的参数显示，以及有效的预警，
 * 作为一个取代日志的存在」。
 *
 * 这个组件就是那个「取代日志」的东西：一条消息花在哪儿、哪块材料被砍了、
 * 离上限还有多远，全在一屏里，不用去 grep 日志。
 *
 * ## 三条设计纪律
 *
 * 1. **分母不混**：`tools`（工具定义）与 `system/history/user` 是不同分母，
 *    横条同宽比较可以，**绝不把它们加一起算百分比**——上一轮那个「9265t /
 *    预算 6000t = 超支 54%」就是这么来的假警报。
 * 2. **缺数据说缺数据**：`partial` / `!measured` 时明说「没采到」，
 *    不显示 0（0 会被读成「没花钱」，而真相是「不知道」）。
 * 3. **预警带证据**：每条预警的 tooltip 是触发它的原始字段，
 *    运营者能自己去核对，不用信我。
 */
import { AlertTriangle, CircleAlert, Info } from 'lucide-react'
import type { ConsolePromptLedger } from '../lib/api'
import { fmtTokens } from '../lib/format'
import './PromptLedger.css'

const VERDICT_CN: Record<string, string> = {
  ok: '材料齐',
  warn: '有折扣',
  error: '注入有问题',
  skip: '无账本',
}

const BLOCK_STATUS_CN: Record<string, string> = {
  kept: '保留',
  truncated: '被裁',
  dropped: '被淘汰',
}

/** 背景块三态的颜色（四态纪律：被淘汰是灰/红，不是绿） */
function blockTone(status: string): string {
  if (status === 'dropped') return 'is-drop'
  if (status === 'truncated') return 'is-cut'
  return 'is-keep'
}

export default function PromptLedger({ ledger }: { ledger: ConsolePromptLedger }) {
  const {
    sections, context, background, warnings, verdict, headline, partial,
    measured_total, system_sections, user_bg_labels, memory_marks,
    history_count, total_chars,
  } = ledger

  // 没有账本：只留一句灰字。**不要渲染一排 0**——那些 0 会被读成「没花钱」。
  if (verdict === 'skip') {
    return (
      <div className="pl-wrap pl-empty" role="note">
        <Info size={13} />
        <span>{headline}</span>
      </div>
    )
  }

  // 横条宽度的共同基准：取最大一段。工具定义通常最大，所以它能一眼看出
  // 「固定开销比上下文预算还大」——这是运营者最该先知道的一件事。
  const barMax = Math.max(1, ...sections.map((s) => s.tokens))
  const hasContext = sections.some((s) => s.in_context) && context.budget > 0

  return (
    <div className={'pl-wrap ' + `is-${verdict}`}>
      <header className="pl-head">
        <span className={`pl-badge is-${verdict}`}>{VERDICT_CN[verdict] ?? verdict}</span>
        <span className="pl-headline">{headline}</span>
        {partial ? (
          <span className="pl-flag" title="四段 token 分账没采到，只有 prompt 结构摘要">
            分账缺失
          </span>
        ) : null}
      </header>

      {/* ── 上下文预算：唯一可以放心算占比的同分母 ── */}
      {hasContext ? (
        <div className="pl-ctx">
          <div className="pl-ctx-top">
            <span className="pl-ctx-label">上下文预算</span>
            <span className="pl-ctx-num">
              <b>{fmtTokens(context.used)}</b>
              <span className="pl-dim"> / {fmtTokens(context.budget)}t</span>
              {context.used_pct != null ? (
                <span className="pl-pct">{context.used_pct.toFixed(0)}%</span>
              ) : null}
            </span>
          </div>
          <div className="pl-bar pl-bar-ctx" role="img"
               aria-label={`上下文已用 ${context.used}，预算 ${context.budget}`}>
            <span
              className={'pl-bar-fill' + (context.over ? ' is-over' : '')}
              style={{ width: `${Math.min(100, context.used_pct ?? 0)}%` }}
            />
          </div>
          <div className="pl-ctx-foot">
            <span className="pl-dim">
              {context.left != null ? `还剩 ${fmtTokens(context.left)}t` : '预算未知'}
            </span>
          </div>
          <p className="pl-note pl-explain-only">{context.doc}</p>
        </div>
      ) : null}

      {/* ── 花在哪：四段横条 ── */}
      {sections.length ? (
        <section className="pl-sec">
          <h4 className="pl-sec-title">
            <span>这一轮花在哪</span>
            <span className="pl-dim pl-sec-hint">
              <span className="explain-only">条长 = 相对大小，不代表同分母</span>
            </span>
          </h4>
          <ul className="pl-rows">
            {sections.map((s) => (
              <li key={s.key} className={'pl-row' + (s.in_context ? '' : ' is-fixed')}>
                <span className="pl-row-label" title={s.doc}>
                  {s.label}
                  {s.in_context ? null : <em className="pl-fixed-tag">固定</em>}
                </span>
                <span className="pl-row-bar">
                  <span
                    className={'pl-row-fill' + (s.in_context ? '' : ' is-fixed')}
                    style={{ width: `${Math.max(1, (s.tokens / barMax) * 100)}%` }}
                  />
                </span>
                <span className="pl-row-num">{fmtTokens(s.tokens)}t</span>
                <span className="pl-row-share">
                  {s.share != null ? `${(s.share * 100).toFixed(0)}%` : '—'}
                </span>
              </li>
            ))}
          </ul>
          {measured_total > 0 ? (
            <p className="pl-note">
              实测输入合计 <b>{fmtTokens(measured_total)}t</b>
              <span className="pl-dim">
                （含工具定义，与上面的上下文预算 <b>不同分母</b>，不要相除）
              </span>
            </p>
          ) : total_chars > 0 ? (
            <p className="pl-note">
              这一轮 prompt 共 <b>{fmtTokens(total_chars)}</b> 字符
              <span className="pl-dim">（token 数没采到）</span>
            </p>
          ) : null}
        </section>
      ) : null}

      {/* ── 背景材料：唯一能算百分比的同分母 ── */}
      <section className="pl-sec">
        <h4 className="pl-sec-title">
          <span>背景材料</span>
          {background.measured && background.limit ? (
            <span
              className="pl-dim pl-sec-hint"
              title={
                (background.used_pct ?? 0) > 100
                  ? '保护块（引用/协议）允许超预算，所以用量会超过上限——不是算错'
                  : '背景材料共用这一份预算，按优先级淘汰'
              }
            >
              {fmtTokens(background.used)} / {fmtTokens(background.limit)}t
              {background.used_pct != null ? ` · ${background.used_pct.toFixed(0)}%` : ''}
            </span>
          ) : (
            <span className="pl-dim pl-sec-hint">
              {background.injected ? '用量没采到' : '这一轮没有背景材料'}
            </span>
          )}
        </h4>

        {background.measured && background.limit ? (
          <div className="pl-bar pl-bar-bg" role="img"
               aria-label={`背景预算已用 ${background.used}，上限 ${background.limit}`}>
            <span
              className={'pl-bar-fill' + ((background.used_pct ?? 0) >= 90 ? ' is-hot' : '')}
              style={{ width: `${Math.min(100, background.used_pct ?? 0)}%` }}
            />
          </div>
        ) : null}

        {background.blocks.length ? (
          <ul className="pl-rows pl-rows-block">
            {background.blocks.map((b, i) => (
              <li key={`${b.label}-${i}`} className={'pl-block ' + blockTone(b.status)}>
                <span className="pl-block-label" title={b.preview || b.label}>
                  {b.label}
                  {b.priority != null ? <em className="pl-prio">P{b.priority}</em> : null}
                </span>
                <span className="pl-block-cost">
                  {b.status === 'truncated' ? (
                    <>
                      <s>{fmtTokens(b.before)}</s> → {fmtTokens(b.tokens)}t
                      {b.cut_pct != null ? (
                        <em className="pl-cut">裁 {b.cut_pct.toFixed(0)}%</em>
                      ) : null}
                    </>
                  ) : b.status === 'dropped' ? (
                    <span>整块没进（原 {fmtTokens(b.before)}t）</span>
                  ) : (
                    <span>{fmtTokens(b.tokens)}t</span>
                  )}
                </span>
                <span className="pl-block-status">{BLOCK_STATUS_CN[b.status] ?? b.status}</span>
              </li>
            ))}
          </ul>
        ) : null}

        <div className="pl-facts">
          {background.gap_notice ? (
            <span className="pl-fact" title="有材料被裁/淘汰时，系统会告诉她「材料不全，别硬编」">
              已告知她「材料不全」
            </span>
          ) : null}
          {background.protected_overflow ? (
            <span className="pl-fact is-warn" title="保护块（引用/协议）可以超预算，会挤压其它块">
              保护块溢出了
            </span>
          ) : null}
          {background.evidence_reserve_used ? (
            <span className="pl-fact" title="当前轮的直接证据（图/文件/@提及）用了额外保底额度">
              动用了证据保底
            </span>
          ) : null}
          {memory_marks.corrected ? (
            <span className="pl-fact" title="对方纠正过、已改掉旧版本的记忆（最可靠）">
              ✏️ 纠正 {memory_marks.corrected}
            </span>
          ) : null}
          {memory_marks.uncertain ? (
            <span className="pl-fact" title="置信度偏低的记忆，她会被提示「这条不一定准」">
              💭~ 模糊 {memory_marks.uncertain}
            </span>
          ) : null}
          {history_count ? (
            <span className="pl-fact" title="这一会话拼进 prompt 的历史消息条数">
              历史 {history_count} 条
            </span>
          ) : null}
        </div>
      </section>

      {/* ── 系统提示词构成 + 注入的背景标签 ── */}
      {system_sections.length || user_bg_labels.length ? (
        <section className="pl-sec pl-explain-only">
          {system_sections.length ? (
            <>
              <h4 className="pl-sec-title">
                <span>身份提示词 {system_sections.length} 节</span>
              </h4>
              <div className="pl-chips">
                {system_sections.map((s, i) => (
                  <span className="pl-chip" key={`${s}-${i}`}>{s}</span>
                ))}
              </div>
            </>
          ) : null}
          {user_bg_labels.length ? (
            <>
              <h4 className="pl-sec-title">
                <span>注入了哪些背景</span>
              </h4>
              <div className="pl-chips">
                {user_bg_labels.map((s, i) => (
                  <span className="pl-chip is-bg" key={`${s}-${i}`}>{s}</span>
                ))}
              </div>
            </>
          ) : null}
        </section>
      ) : null}

      {/* ── 预警：每条都带证据 ── */}
      {warnings.length ? (
        <section className="pl-warns">
          {warnings.map((w, i) => (
            <div className={'pl-warn is-' + w.level} key={`${w.key}-${i}`} title={w.evidence}>
              {w.level === 'error' ? <CircleAlert size={13} /> : <AlertTriangle size={13} />}
              <span>{w.text}</span>
            </div>
          ))}
        </section>
      ) : null}
    </div>
  )
}
