import { useEffect, useMemo, useState } from 'react'
import {
  Plus, RefreshCw, Trash2, Pencil, Check, X, Download, Loader2,
} from 'lucide-react'
import { api, type ModelProfile, type ModelsResponse, type SecretEntry } from '../../lib/api'
import '../Settings.css'

const PROVIDERS = ['custom', 'deepseek', 'openai', 'anthropic', 'xai']

type Draft = {
  id: string
  label: string
  provider: string
  base_url: string
  api_key: string
  model: string
  max_tokens: string
  temperature: string
  thinking: string
  thinking_effort: string
}

const emptyDraft = (): Draft => ({
  id: '',
  label: '',
  provider: 'custom',
  base_url: '',
  api_key: '',
  model: '',
  max_tokens: '64000',
  temperature: '0.8',
  thinking: 'on',
  thinking_effort: 'high',
})

function toDraft(p: ModelProfile): Draft {
  return {
    id: p.id,
    label: p.label ?? '',
    provider: p.provider ?? 'custom',
    base_url: p.base_url ?? '',
    api_key: p.api_key ?? '',
    model: p.model ?? '',
    max_tokens: String(p.max_tokens ?? ''),
    temperature: String(p.temperature ?? ''),
    thinking: p.thinking ?? '',
    thinking_effort: p.thinking_effort ?? '',
  }
}

export default function ModelSettings() {
  const [cfg, setCfg] = useState<Record<string, any> | null>(null)
  const [data, setData] = useState<ModelsResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [editing, setEditing] = useState<Draft | null>(null)

  const load = async () => {
    try {
      const [c, m] = await Promise.all([api.config(), api.models()])
      setCfg(c.raw)
      setData(m)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    }
  }

  useEffect(() => {
    load()
  }, [])

  const profiles = data?.profiles ?? []
  const active = data?.active ?? ''

  /** 把 models 数组写回 config（旧扁平结构同时被覆盖为选中项，保证 agent 兼容） */
  const persist = async (profilesNext: ModelProfile[], activeNext: string) => {
    if (!cfg) return
    setBusy(true)
    try {
      const sel = profilesNext.find((p) => p.id === activeNext) ?? profilesNext[0]
      const llmNext = {
        ...(cfg.llm ?? {}),
        models: profilesNext,
        active_model: activeNext,
        ...(sel
          ? {
              provider: sel.provider,
              base_url: sel.base_url,
              model: sel.model,
              max_tokens: Number(sel.max_tokens) || cfg.llm?.max_tokens,
              temperature: Number(sel.temperature) || cfg.llm?.temperature,
              thinking: sel.thinking || cfg.llm?.thinking,
              thinking_effort: sel.thinking_effort || cfg.llm?.thinking_effort,
              // 密钥不在这里覆盖：保持原来的 ${VAR} 引用或用户填的值
              api_key: sel.api_key || cfg.llm?.api_key,
            }
          : {}),
      }
      await api.saveConfig({ ...cfg, llm: llmNext })
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const activate = (id: string) => persist(profiles, id)

  const remove = async (id: string) => {
    const next = profiles.filter((p) => p.id !== id)
    const activeNext = active === id ? (next[0]?.id ?? '') : active
    await persist(next, activeNext)
  }

  const saveDraft = async (d: Draft) => {
    const id = d.id || `m${Date.now().toString(36)}`
    const entry: ModelProfile = {
      id,
      label: d.label || d.model || id,
      provider: d.provider,
      base_url: d.base_url,
      api_key: d.api_key,
      model: d.model,
      max_tokens: d.max_tokens,
      temperature: d.temperature,
      thinking: d.thinking,
      thinking_effort: d.thinking_effort,
    }
    const next = profiles.some((p) => p.id === id)
      ? profiles.map((p) => (p.id === id ? entry : p))
      : [...profiles, entry]
    setEditing(null)
    await persist(next, active || id)
  }

  return (
    <div className="settings-body">
      <div className="settings-head">
        <div>
          <p className="settings-desc explain-only" style={{ margin: '0 0 14px' }}>
            糖糖的「大脑」。可添加多个模型档位：填接口地址与 API Key 后一键拉取可用模型，
            选中即切换。旧版扁平配置会自动兼容。
          </p>
        </div>
        <button className="btn btn-primary" onClick={() => setEditing(emptyDraft())}>
          <Plus size={14} />
          添加模型
        </button>
      </div>

      {error && <div className="notice notice-error">{error}</div>}

      {editing && (
        <ModelForm
          draft={editing}
          onChange={setEditing}
          onCancel={() => setEditing(null)}
          onSave={saveDraft}
        />
      )}

      <div className="model-list">
        {profiles.length === 0 && (
          <div className="settings-card">
            <div className="settings-empty">还没有模型档位，点右上角「添加模型」。</div>
          </div>
        )}
        {profiles.map((p) => (
          <div
            key={p.id}
            className={'model-card' + (p.id === active ? ' is-active' : '')}
          >
            <button className="model-card-main" onClick={() => activate(p.id)}>
              <span className="model-radio" />
              <span className="model-info">
                <span className="model-name">
                  {p.model || p.label}
                  {p.id === active && <span className="badge badge-accent">当前使用</span>}
                </span>
                <span className="model-meta">
                  {p.provider} · {p.base_url || '默认地址'} · {p.label}
                </span>
              </span>
            </button>
            <div className="model-params">
              <span>温度 {p.temperature ?? '—'}</span>
              <span>最大 {p.max_tokens ?? '—'}</span>
              <span>思考 {p.thinking_effort || p.thinking || '—'}</span>
            </div>
            <button className="btn btn-ghost" title="编辑" onClick={() => setEditing(toDraft(p))}>
              <Pencil size={13} />
            </button>
            <button
              className="btn btn-ghost btn-danger"
              title="删除"
              onClick={() => remove(p.id)}
              disabled={busy}
            >
              <Trash2 size={13} />
            </button>
          </div>
        ))}
      </div>
    </div>
  )
}

function ModelForm({
  draft,
  onChange,
  onCancel,
  onSave,
}: {
  draft: Draft
  onChange: (d: Draft) => void
  onCancel: () => void
  onSave: (d: Draft) => void
}) {
  const [fetching, setFetching] = useState(false)
  const [fetched, setFetched] = useState<string[]>([])
  const [fetchError, setFetchError] = useState<string | null>(null)

  // ── 密钥：档位里的 api_key 是 `${VAR}` 引用，真值在 .env（2026-10-27）──
  // 原来这里把 `${CUSTOM_LLM_KEY}` 当普通值显示在输入框里，placeholder 还写着
  // 「或直接填 sk-...」——那是在诱导用户填字面量。填进去的后果不是「多个选择」：
  // `agent/secret_refs.normalize_secret_fields()` 会把字面量当真值，`.env` 里那个
  // key 从此被静默无视（2026-09-20 真实踩过，排查两小时）。
  // 所以：引用形态 → 只显示「已绑定 .env: VAR + 掩码」，写入口单独一个框。
  const [secretEntries, setSecretEntries] = useState<SecretEntry[]>([])
  const [newKey, setNewKey] = useState('')
  const [keyBusy, setKeyBusy] = useState(false)
  const [keyTip, setKeyTip] = useState('')
  const [keyError, setKeyError] = useState('')

  useEffect(() => {
    api
      .secrets()
      .then((s) => setSecretEntries(s.entries))
      .catch(() => setSecretEntries([]))
  }, [])

  const saveKey = async (envName: string) => {
    const v = newKey.trim()
    if (!v) return
    setKeyBusy(true)
    setKeyError('')
    setKeyTip('')
    try {
      await api.saveSecret(envName, v)
      setNewKey('')
      setKeyTip(`已写进 .env 的 ${envName} —— 重启糖糖后生效`)
      const s = await api.secrets()
      setSecretEntries(s.entries)
    } catch (e) {
      setKeyError((e as Error).message)
    } finally {
      setKeyBusy(false)
    }
  }

  const set = (k: keyof Draft) => (e: React.ChangeEvent<HTMLInputElement | HTMLSelectElement>) =>
    onChange({ ...draft, [k]: e.target.value })

  const doFetch = async () => {
    setFetching(true)
    setFetchError(null)
    try {
      const res = await api.fetchModels(draft.base_url, draft.api_key)
      setFetched(res.models)
    } catch (e) {
      setFetchError((e as Error).message)
    } finally {
      setFetching(false)
    }
  }

  const canFetch = useMemo(
    () => draft.base_url.trim().length > 0 && draft.api_key.trim().length > 0,
    [draft.base_url, draft.api_key],
  )

  return (
    <div className="settings-card form-card">
      <div className="form-head">
        <span>{draft.id ? '编辑模型' : '添加模型'}</span>
        <button className="btn btn-ghost" onClick={onCancel}>
          <X size={14} />
        </button>
      </div>

      <div className="form-grid">
        <label className="field">
          <span>名称</span>
          <input value={draft.label} onChange={set('label')} placeholder="例如：主力模型" />
        </label>
        <label className="field">
          <span>Provider</span>
          <select value={draft.provider} onChange={set('provider')}>
            {PROVIDERS.map((p) => (
              <option key={p} value={p}>{p}</option>
            ))}
          </select>
        </label>
        <label className="field field-wide">
          <span>接口地址</span>
          <input
            value={draft.base_url}
            onChange={set('base_url')}
            placeholder="https://api.example.com  （会自动补 /v1）"
          />
        </label>
        <label className="field field-wide">
          <span>API Key</span>
          {(() => {
            // `${VAR}` = 引用 .env；真值不在这里显示，也不允许在这里写进 config
            const m = /^\$\{([A-Z_][A-Z0-9_]*)\}$/.exec(draft.api_key.trim())
            if (!m) {
              return (
                <input
                  value={draft.api_key}
                  onChange={set('api_key')}
                  placeholder="填 ${变量名} 引用 .env 里的密钥；留空则运行时用 .env 默认值"
                />
              )
            }
            const envName = m[1]
            const entry = secretEntries.find((e) => e.name === envName)
            return (
              <div className="secret-bound">
                <div className="secret-bound-state">
                  已绑定 .env：<code>{envName}</code>
                  {entry?.configured ? (
                    <>
                      {' · '}
                      <code>{entry.masked}</code>
                    </>
                  ) : (
                    <span className="muted"> · 还没填</span>
                  )}
                </div>
                <div className="secret-bound-line">
                  <input
                    type="password"
                    placeholder="粘贴新值（写进 .env，不回显）"
                    spellCheck={false}
                    value={newKey}
                    onChange={(e) => setNewKey(e.target.value)}
                  />
                  <button
                    className="btn btn-ghost"
                    disabled={keyBusy || !newKey.trim()}
                    onClick={() => void saveKey(envName)}
                  >
                    {keyBusy ? <Loader2 size={14} className="spin" /> : <Check size={14} />}
                    保存到 .env
                  </button>
                </div>
                {keyTip && <div className="form-hint">{keyTip}</div>}
                {keyError && <div className="form-error">{keyError}</div>}
              </div>
            )
          })()}
        </label>

        <div className="field field-wide fetch-row">
          <button
            className="btn btn-ghost"
            onClick={doFetch}
            disabled={!canFetch || fetching}
          >
            {fetching ? <Loader2 size={14} className="spin" /> : <Download size={14} />}
            获取模型列表
          </button>
          {fetchError && <span className="form-error">{fetchError}</span>}
          {fetched.length > 0 && (
            <span className="form-hint">拉到 {fetched.length} 个模型，选择：</span>
          )}
          {fetched.length > 0 && (
            <select
              className="model-picker"
              value={draft.model}
              onChange={set('model')}
            >
              <option value="">— 选择模型 —</option>
              {fetched.map((m) => (
                <option key={m} value={m}>{m}</option>
              ))}
            </select>
          )}
        </div>

        <label className="field field-wide">
          <span>模型 ID</span>
          <input
            value={draft.model}
            onChange={set('model')}
            placeholder="deepseek-v4.1-flash"
          />
        </label>

        <label className="field">
          <span>最大 tokens</span>
          <input value={draft.max_tokens} onChange={set('max_tokens')} />
        </label>
        <label className="field">
          <span>温度</span>
          <input value={draft.temperature} onChange={set('temperature')} />
        </label>
        <label className="field">
          <span>思考</span>
          <select value={draft.thinking} onChange={set('thinking')}>
            <option value="on">on</option>
            <option value="off">off</option>
            <option value="auto">auto</option>
          </select>
        </label>
        <label className="field">
          <span>思考强度</span>
          <select value={draft.thinking_effort} onChange={set('thinking_effort')}>
            <option value="">默认</option>
            <option value="low">low</option>
            <option value="medium">medium</option>
            <option value="high">high</option>
          </select>
        </label>
      </div>

      <div className="form-actions">
        <button className="btn btn-ghost" onClick={onCancel}>取消</button>
        <button className="btn btn-primary" onClick={() => onSave(draft)}>
          <Check size={14} />
          保存
        </button>
      </div>
    </div>
  )
}
