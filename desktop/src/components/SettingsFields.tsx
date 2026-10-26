import { useCallback, useEffect, useState } from 'react'
import { Check, Loader2 } from 'lucide-react'

import { getAt, setAt, coerce } from '../lib/configStore'
import { api, type SecretsResponse } from '../lib/api'
import './SettingsFields.css'

export type FieldDef = {
  path: string
  label: string
  type: string
  hint?: string
  /** 仅 `secret` 类型：要写进 .env 的变量名。`auto` = 跟着当前 provider 走。 */
  secret_env?: string
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

  // 密钥状态（2026-10-27）：密钥不再有独立设置页，改成跟着它所属的那块配置走。
  // 变量名固定由后端给（`provider_env` 跟着 provider 走，单一事实源在
  // agent/secret_refs.py），这里**不另写一份映射**。
  const [secrets, setSecrets] = useState<SecretsResponse | null>(null)
  const refreshSecrets = useCallback(() => {
    api
      .secrets()
      .then(setSecrets)
      .catch(() => setSecrets(null))
  }, [])
  const hasSecretField = fields.some((f) => f.type === 'secret')
  useEffect(() => {
    if (hasSecretField) refreshSecrets()
  }, [hasSecretField, refreshSecrets])

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

        if (f.type === 'secret') {
          // `auto` → 后端给的实际变量名（跟着当前 provider）
          const envName =
            f.secret_env === 'auto' ? (secrets?.provider_env ?? '') : (f.secret_env ?? '')
          const entry = secrets?.entries.find((e) => e.name === envName)
          return (
            <div className="dsl-row" key={f.path}>
              <div className="dsl-text">
                <div className="dsl-title">{f.label}</div>
                {/* 机制说明可关；「保存后要重启」这类操作提示必须留在 explain-only 外面 */}
                {f.hint && <div className="dsl-desc explain-only">{f.hint}</div>}
                <div className="dsl-desc">
                  {entry?.configured ? (
                    <>
                      已保存 <code>{entry.name}</code> = <code>{entry.masked}</code>
                    </>
                  ) : (
                    <>
                      还没填 <code>{envName || '（先在「提供商」里选一家）'}</code>
                    </>
                  )}
                </div>
              </div>
              <div className="dsl-control">
                <SecretInput envName={envName} onSaved={refreshSecrets} />
              </div>
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

/**
 * 内联的密钥输入（2026-10-27）。
 *
 * 主人要求：不要独立的「连接密钥」设置页，密钥跟着它所属的那块配置走，
 * 但值统一写 `.env`（`config.yaml` 只留 `${VAR}`）。
 *
 * 安全边界全在后端（白名单、只回掩码、行边界拒绝、写入后注入自检 + 回滚），
 * 这里只负责把「粘贴新值 → 保存」这一小步接上界面。
 */
function SecretInput({ envName, onSaved }: { envName: string; onSaved: () => void }) {
  const [value, setValue] = useState('')
  const [busy, setBusy] = useState(false)
  const [tip, setTip] = useState('')
  const [err, setErr] = useState('')

  const save = async () => {
    if (!envName) {
      setErr('先在「提供商」里选一家，才知道密钥写进哪个变量')
      return
    }
    if (!value.trim()) {
      setErr('先粘贴一个值')
      return
    }
    setBusy(true)
    setErr('')
    setTip('')
    try {
      await api.saveSecret(envName, value.trim())
      setValue('')
      setTip('已写入 .env —— 重启糖糖后生效')
      onSaved()
    } catch (e) {
      setErr((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="dsl-secret">
      <div className="dsl-secret-line">
        <input
          type="password"
          className="dsl-text-input"
          placeholder="粘贴新值（不回显）"
          spellCheck={false}
          value={value}
          onChange={(e) => setValue(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void save()
          }}
        />
        <button type="button" className="dsl-secret-save" disabled={busy} onClick={() => void save()}>
          {busy ? <Loader2 size={14} className="spin" /> : <Check size={14} />}
          保存
        </button>
      </div>
      {tip && <div className="dsl-secret-tip">{tip}</div>}
      {err && <div className="dsl-secret-err">{err}</div>}
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
