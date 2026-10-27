import { useCallback, useEffect, useState } from 'react'
import { Check, Loader2, RefreshCw } from 'lucide-react'

import { getAt, setAt, coerce } from '../lib/configStore'
import { api, type ModelProfile, type OllamaModel, type SecretsResponse } from '../lib/api'
import './SettingsFields.css'

export type FieldDef = {
  path: string
  label: string
  type: string
  hint?: string
  /** 仅 `secret` 类型：要写进 .env 的变量名。`auto` = 跟着当前 provider 走。 */
  secret_env?: string
}

/** 用途的中文名（与 `agent/model_refs.KIND_LABELS` 同一套词） */
const KIND_LABELS: Record<string, string> = {
  chat: '聊天',
  vision: '识图',
  tag: '打标',
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
              <FieldControl
                def={f}
                value={value}
                config={config}
                onCommit={(v) => set(f.path, f.type, v)}
              />
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
  config,
  onCommit,
}: {
  def: FieldDef
  value: unknown
  /** 整份配置：有几个控件要看**别的字段**才能干活（Ollama 模型列表要看 ollama_host） */
  config: Record<string, unknown>
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

  if (def.type === 'ollama_model') {
    return <OllamaModelField value={value} config={config} onCommit={onCommit} />
  }

  if (def.type === 'model_ref') {
    return <ModelRefField value={value} onCommit={onCommit} />
  }

  if (def.type === 'int' || def.type === 'float') {
    return (
      <NumberField kind={def.type} value={value} onCommit={onCommit} className="dsl-number" />
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

/**
 * 数字输入（2026-10-27 修：0.0x 与负数打不进去）。
 *
 * 这里踩的是**两层**坑，缺一层都修不好。
 *
 * ① `type="number"` 的 DOM 存不下小数/负数的中间态。实测（DevTools 里对
 *    `input[type=number]` 直接设 value 再读回）：
 *      "0" → "0"    "0." → ""    "0.0" → "0.0"    "0.08" → "0.08"
 *      ".5" → ".5"  "-"  → ""
 *    也就是说用户敲「0」再敲「.」的那一刻，`input.value` 返回的是**空串**。
 *
 * ② 旧实现把 value 直连配置（`value={String(value)}` + `onChange={coerce}`），
 *    拿到空串就走 `coerce("") = 0`，再把 value 重置成 "0" —— 于是用户想输默认的
 *    `0.08`，实际看到的是：
 *      打「0」→ 显示 0 ／ 打「.」→ **点消失** ／ 打「0」→ 0 ／ 打「8」→ 8
 *    最终只能得到 `0.8` 或 `8`，永远输不进 `0.08`。而群聊饥渴度默认正是 0.08，
 *    主人要的「精确调参」在 UI 这一层就被锁死成一位小数。
 *
 * 修法：换成 `type="text"` + `inputMode`，本地 draft 保留用户正在敲的原始字符串，
 * 只有「看起来已经是一个完整数字」时才 commit；失焦时再兜底 commit 一次
 * （点「保存」按钮之前浏览器一定先派发 blur）。
 */
/**
 * 「选模型档位」（2026-10-27）。
 *
 * 图库打标的接口地址 / 模型名 / Key 以前要各填一套；现在可以从「AI 模型」页
 * 已有的档位里选一个，三项一起带过来（`agent/model_refs.resolve_model_ref` 是
 * 唯一的翻译点）。用途（聊天/识图/打标）只作提示、**不是门禁**——老档位没有
 * 这个标记，一律按聊天档显示，但一样能选来打标。
 */
function ModelRefField({
  value,
  onCommit,
}: {
  value: unknown
  onCommit: (v: unknown) => void
}) {
  const [options, setOptions] = useState<ModelProfile[] | null>(null)
  const [error, setError] = useState('')

  useEffect(() => {
    let alive = true
    api
      .models()
      .then((d) => {
        if (alive) setOptions(d.profiles ?? [])
      })
      .catch((e: Error) => {
        if (alive) setError(e.message)
      })
    return () => {
      alive = false
    }
  }, [])

  const current = String(value ?? '')
  const kindsText = (p: ModelProfile) =>
    (p.kinds ?? ['chat']).map((k) => KIND_LABELS[k] ?? k).join('+')

  return (
    <div className="dsl-modelref">
      <select value={current} onChange={(e) => onCommit(e.target.value)}>
        <option value="">不用档位（用下面的地址 / 模型 / Key）</option>
        {(options ?? []).map((p) => (
          <option key={p.id} value={p.id}>
            {`${p.label || p.id} · ${kindsText(p)}`}
          </option>
        ))}
      </select>
      {error && <div className="dsl-secret-err">拉不到档位列表：{error}</div>}
      {!error && options === null && <div className="dsl-secret-tip">正在读档位…</div>}
      {!error && options !== null && options.length === 0 && (
        <div className="dsl-secret-tip">
          还没有档位——去「AI 模型」加一个，或继续用下面三项
        </div>
      )}
    </div>
  )
}

/**
 * Ollama 模型下拉（2026-10-27）。
 *
 * 以前 `ollama_model` 是**手填字符串**，填错要等真跑起来（一张 1.5 秒 + 报错）
 * 才发现。现在自动向 `/api/models/ollama` 拉列表——后端调 `/api/tags` 拿清单、
 * 再对每个模型 `/api/show` 拿 `capabilities`，**能看图的会标出来**（打标/识图
 * 必须选那种）。形状对齐 DSH Harness 的 `provider.models[]`。
 *
 * 地址取同级字段 `ollama_host`，不另加输入框——两处填地址必然不一致。
 */
function OllamaModelField({
  value,
  config,
  onCommit,
}: {
  value: unknown
  config: Record<string, unknown>
  onCommit: (v: unknown) => void
}) {
  const host = String(getAt(config, 'image_tagging.ollama_host') ?? '')
  const [models, setModels] = useState<OllamaModel[] | null>(null)
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState('')

  const load = useCallback(async () => {
    setBusy(true)
    setErr('')
    try {
      const r = await api.ollamaModels(host)
      if (!r.ok) {
        setErr(r.error || '拉不到模型列表')
        setModels([])
      } else {
        setModels(r.models)
      }
    } catch (e) {
      setErr((e as Error).message)
      setModels([])
    } finally {
      setBusy(false)
    }
  }, [host])

  useEffect(() => {
    void load()
  }, [load])

  const current = String(value ?? '')
  const known = (models ?? []).some((m) => m.id === current)

  return (
    <div className="dsl-ollama">
      <select value={current} onChange={(e) => onCommit(e.target.value)}>
        <option value="">{models === null ? '正在拉模型列表…' : '（未选）'}</option>
        {!known && current ? <option value={current}>{current}（不在列表里）</option> : null}
        {(models ?? []).map((m) => (
          <option key={m.id} value={m.id}>
            {m.vision ? `${m.id} · 可看图` : m.id}
          </option>
        ))}
      </select>
      <button
        type="button"
        className="btn btn-ghost btn-sm"
        onClick={() => void load()}
        disabled={busy}
        title="重新向 Ollama 拉取模型列表（装了新模型后点一下）"
      >
        {busy ? <Loader2 size={12} className="spin" /> : <RefreshCw size={12} />}
        刷新
      </button>
      {err && <div className="dsl-secret-err">{err}</div>}
    </div>
  )
}

const COMPLETE_NUMBER = /^-?(\d+\.?\d*|\.\d+)([eE][-+]?\d+)?$/
/** 输入过程中允许出现的字符（挡掉字母等噪音，但放行 `0.` / `-` / `1e` 这类中间态） */
const DRAFT_CHARS = /^[-+]?[\d.]*([eE][-+]?\d*)?$/

export function NumberField({
  kind,
  value,
  onCommit,
  className,
}: {  kind: 'int' | 'float'
  value: unknown
  onCommit: (v: unknown) => void
  className?: string
}) {
  const outer = value === undefined || value === null ? '' : String(value)
  const [draft, setDraft] = useState(outer)
  const [editing, setEditing] = useState(false)

  useEffect(() => {
    // 不在编辑时以外部值为准：切页 / 恢复默认 / 保存后重载都会走到这里。
    // 编辑中绝不覆盖 draft —— 覆盖就等于又把用户打的字吃掉（就是这个 bug 本身）。
    if (!editing) setDraft(outer)
  }, [outer, editing])

  const emit = (raw: string) => {
    if (COMPLETE_NUMBER.test(raw)) onCommit(coerce(kind, raw))
  }

  return (
    <input
      className={className}
      type="text"
      inputMode={kind === 'float' ? 'decimal' : 'numeric'}
      autoComplete="off"
      value={draft}
      onFocus={() => setEditing(true)}
      onChange={(e) => {
        const raw = e.target.value
        if (!DRAFT_CHARS.test(raw)) return
        setDraft(raw)
        emit(raw)
      }}
      onBlur={() => {
        setEditing(false)
        // 清空后直接点保存时保持原值，比悄悄写成 0 安全；真想清零就输「0」
        if (draft === '') {
          setDraft(outer)
          return
        }
        emit(draft)
      }}
    />
  )
}
