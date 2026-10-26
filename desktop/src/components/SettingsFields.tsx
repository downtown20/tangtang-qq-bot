import { getAt, setAt, coerce } from '../lib/configStore'
import './SettingsFields.css'

export type FieldDef = {
  path: string
  label: string
  type: string
  hint?: string
}

const CHOICES: Record<string, string[]> = {
  'llm.provider': ['custom', 'deepseek', 'openai', 'anthropic', 'xai'],
  'llm.thinking': ['on', 'off', 'auto'],
  'llm.thinking_effort': ['', 'low', 'medium', 'high'],
  'llm.light_thinking': ['auto', 'on', 'off'],
  'llm.vision.provider': ['main', 'api', 'local'],
  'image_share.caption': ['none', 'random', 'llm'],
  'image_share.play_mode': ['random', 'sequential'],
  // 图库打标（2026-10-26 第 63 轮）：off / 本地 Ollama / 云端 OpenAI 兼容 API。
  // 只有这两种 provider —— 再加一种就在 agent/image_tagger.py 加一个 `_ask_xxx`，
  // 而不是在这两个里塞分支（否则「本地」和「云端」的边界会越来越糊）。
  'image_tagging.provider': ['off', 'ollama', 'api'],
}

/**
 * 设置字段渲染 —— 结构对齐 DSH 的通用设置项：
 *   settings-general 包 .PgWN5G_row  border-bottom .5px / space-between / gap 24 / padding 16px 0
 *   .PgWN5G_title      14px/20px
 *   .PgWN5G_description 12px/18px secondary + margin-top 4px
 * 也就是「左标题+说明、右控件、行间一条细线」，**不是**每行一个卡片。
 */
export default function SettingsFields({
  fields,
  config,
  baseline,
  onChange,
}: {
  fields: FieldDef[]
  config: Record<string, unknown>
  baseline?: Record<string, unknown> | null
  onChange: (next: Record<string, unknown>) => void
}) {
  const set = (path: string, type: string, value: unknown) =>
    onChange(setAt(config, path, value))

  return (
    <div className="dsl">
      {fields.map((f) => {
        const value = getAt(config, f.path)

        if (f.type === 'label') {
          return (
            <div className="dsl-note" key={f.path}>
              {f.hint || f.label}
            </div>
          )
        }

        const wide = f.type === 'list'
        const dirty =
          !!baseline &&
          JSON.stringify(getAt(baseline, f.path)) !== JSON.stringify(value)

        return (
          <div
            key={f.path}
            className={
              'dsl-row' + (wide ? ' is-wide' : '') + (dirty ? ' is-dirty' : '')
            }
          >
            <div className="dsl-text">
              <div className="dsl-title">
                {f.label}
                {dirty && <span className="dsl-dirty-dot" title="未保存的修改" />}
              </div>
              {/* 字段说明 = 解释（讲解模式可关）。标签、当前值、控件永远显示。
                  ⚠ hint 里只准写「这是什么 / 为什么」，**不许**写操作步骤和警告——
                  那些必须留在 `.dsl-desc` 外面，否则关讲解就看不到「改完要重启」了 */}
              {f.hint && <div className="dsl-desc explain-only">{f.hint}</div>}
            </div>
            <div className="dsl-control">
              <FieldControl def={f} value={value} onCommit={(v) => set(f.path, f.type, v)} />
            </div>
          </div>
        )
      })}
    </div>
  )
}

function FieldControl({
  def,
  value,
  onCommit,
}: {
  def: FieldDef
  value: unknown
  onCommit: (v: unknown) => void
}) {
  if (def.type === 'bool') {
    const on = Boolean(value)
    return (
      <button
        type="button"
        role="switch"
        aria-checked={on}
        className={'switch' + (on ? ' is-on' : '')}
        onClick={() => onCommit(!on)}
        title={on ? '已开启' : '已关闭'}
      >
        <span className="switch-knob" />
      </button>
    )
  }

  if (def.type === 'list') {
    const text = Array.isArray(value) ? value.join('\n') : String(value ?? '')
    return (
      <textarea
        rows={Math.min(10, Math.max(3, text.split('\n').length))}
        value={text}
        placeholder="一行一个"
        spellCheck={false}
        onChange={(e) =>
          onCommit(
            e.target.value
              .split('\n')
              .map((s) => s.trim())
              .filter(Boolean),
          )
        }
      />
    )
  }

  const options = CHOICES[def.path]
  if (options) {
    const current = String(value ?? '')
    return (
      <select value={current} onChange={(e) => onCommit(e.target.value)}>
        {!options.includes(current) && <option value={current}>{current || '（默认）'}</option>}
        {options.map((o) => (
          <option key={o} value={o}>
            {o || '（默认）'}
          </option>
        ))}
      </select>
    )
  }

  if (def.type === 'int' || def.type === 'float') {
    return (
      <input
        className="dsl-number"
        type="number"
        step={def.type === 'float' ? 'any' : 1}
        value={value === undefined || value === null ? '' : String(value)}
        onChange={(e) => onCommit(coerce(def.type, e.target.value))}
      />
    )
  }

  const isSecret = /key|token|secret|password/i.test(def.path)
  return (
    <input
      type={isSecret ? 'password' : 'text'}
      className="dsl-text-input"
      value={value === undefined || value === null ? '' : String(value)}
      spellCheck={false}
      onChange={(e) => onCommit(e.target.value)}
    />
  )
}
