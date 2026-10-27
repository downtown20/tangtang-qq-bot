import { useEffect, useMemo, useState } from 'react'
import {
  Check, ChevronDown, ChevronRight, Download, Loader2, Plus, Trash2,
} from 'lucide-react'
import {
  api, type ModelProfile, type ModelsResponse,
} from '../../lib/api'
import SettingsFields, { type FieldDef } from '../../components/SettingsFields'
import '../../components/SettingsFields.css'
import '../Settings.css'

/**
 * AI 模型 —— 形状照 DSH Harness 的 provider → models 两层（2026-10-28 主人给的
 * 第二张截图）。
 *
 * ## 为什么改成两级
 *
 * 改之前是「一个档位 = 一个模型 + 一整套连接信息」，于是同一个中转站的
 * 地址和 Key 在 N 个档位里存 N 份——那正是教训 #54 的反模式（组级属性存成
 * 条目级副本，改哪份生效靠遍历顺序）。现在：
 *
 *   · **provider 组**（这页的每张卡）：名称 / API 地址 / 协议 / 密钥，写一次；
 *   · **模型目录**（卡里每行）：显示名 + 模型 ID + 能力（窗口/看图）+ 用途。
 *
 * 数据层由 `llm.providers[]` + `llm.models[].provider` 引用承载，
 * 换算点只有 `agent/model_refs.py::provider_conn()` 一处。
 *
 * ## 两处保存入口不同（刻意的）
 *
 *   · provider 卡走 `POST /api/providers/save`：它会顺手把引用该组的档位里
 *     那份过期副本清掉，并**不让掩码 Key 写回磁盘**；
 *   · 模型目录走 `POST /api/config`：组表字段在这里被显式剔除（`delete
 *     llm.providers`），因为前端手上的 Key 是掩码，写回去就坏了。
 *
 * ## 保存时机
 *
 * provider 卡是「失焦即存」（三个字段，改完就走，跟 DSH 一致）；
 * 模型行是「展开后显式保存」（九个字段，逐字段写盘会堆一堆备份文件）。
 */

/** provider id → 人话（对应 DSH 的 provider displayName）。id 仍取固定集合：
 *  它的 Key 变量名来自 `agent/secret_refs.PROVIDER_KEY_ENV`，自由起的 id 会让
 *  「写 .env」的白名单挡下来。 */
const PROVIDER_LABELS: Record<string, string> = {
  custom: '自建中转（OpenAI 兼容）',
  deepseek: 'DeepSeek',
  openai: 'OpenAI',
  anthropic: 'Anthropic',
  xai: 'xAI',
  ollama: '本机 Ollama',
}

/** 协议：目前只实现 OpenAI 兼容。列出来是为了让界面说实话，而不是让用户以为
 *  「协议」可以随便填（填了不支持的，请求会静默走同一个实现）。 */
const PROTOCOLS = [{ value: 'openai-completions', label: 'OpenAI 兼容' }]

/** 推理强度（2026-10-28 决策 2）：把 `thinking` + `thinking_effort` 两个字段
 *  **合成一个下拉**。用户要理解「on + medium」= 中等推理，语义成本高于直接选一档；
 *  落盘时再拆回两个后端字段（映射见 `effortValue` / `applyEffort`）。 */
const EFFORT_OPTIONS = [
  { value: 'off', label: '关闭思考' },
  { value: 'on:low', label: '低' },
  { value: 'on:medium', label: '中（默认）' },
  { value: 'on:high', label: '高' },
]

/** 模型档位的界面编辑态：**全是字符串**——输入框里 `0.8` 就是 `"0.8"`，
 *  写成 number 会踩 `<input type="number">` 存不下 `0.` 的坑（教训 #56）。 */
type ModelDraft = {
  id: string
  label: string
  model: string
  provider: string
  max_tokens: string
  temperature: string
  thinking: string
  thinking_effort: string
  context_window: string
  vision: boolean
  /** 用途（chat/vision/tag）**界面上不再让用户勾**（2026-10-28 决策 1：
   *  它与 `input` 语义重叠）。但保存不能把它丢掉——后端 `/api/models/save`
   *  是整份覆盖 `llm.models`，不原样带回就会把所有老档位的标注静默清空。 */
  kinds: string[]
}

type ProviderDraft = { label: string; base_url: string; api: string }

const emptyModel = (provider: string): ModelDraft => ({
  // id 一开始就定下来：保存时 `active_model` 要指向它，临时 id 会让「当前模型」
  // 指向一个不存在的档位（`_get_llm_config` 会静默退回第一条 —— 用错模型）
  id: `m${Date.now().toString(36)}${Math.random().toString(36).slice(2, 5)}`,
  label: '',
  model: '',
  provider,
  max_tokens: '64000',
  temperature: '0.8',
  thinking: 'on',
  thinking_effort: 'medium',
  context_window: '',
  vision: false,
  kinds: ['chat'],
})

function toDraft(p: ModelProfile): ModelDraft {
  return {
    id: p.id,
    label: p.label ?? '',
    model: p.model ?? '',
    provider: p.provider ?? 'custom',
    max_tokens: p.max_tokens == null ? '' : String(p.max_tokens),
    temperature: p.temperature == null ? '' : String(p.temperature),
    thinking: p.thinking ?? '',
    thinking_effort: p.thinking_effort ?? '',
    context_window: p.context_window ? String(p.context_window) : '',
    vision: (p.input ?? []).includes('image'),
    kinds: p.kinds ?? [],
  }
}

function toEntry(d: ModelDraft): ModelProfile {
  const entry: ModelProfile = {
    id: d.id || `m${Date.now().toString(36)}`,
    label: d.label || d.model || d.id,
    provider: d.provider,
    model: d.model,
    max_tokens: d.max_tokens,
    temperature: d.temperature,
    thinking: d.thinking,
    // 「关闭思考」时强度没有意义：归一成空串，免得落盘成
    // thinking=off + thinking_effort=high 这种自相矛盾的组合被中转站拒绝。
    thinking_effort: d.thinking === 'off' ? '' : d.thinking_effort,
    context_window: Number(d.context_window) || 0,
    input: d.vision ? ['text', 'image'] : ['text'],
  }
  // 用途不再有编辑入口，但已有值原样写回（后端整份覆盖 llm.models，见类型注释）
  if (d.kinds.length) entry.kinds = d.kinds
  return entry
}

/** 「推理强度」下拉的当前值：`off` / `on:low` / `on:medium` / `on:high`。
 *  反向映射（2026-10-28 决策 2）：thinking=off → 关闭；否则按 effort 显示，
 *  effort 是空的 / `auto` / 认不出的值一律显示「中」。 */
function effortValue(d: ModelDraft): string {
  if (d.thinking === 'off') return 'off'
  const effort = (d.thinking_effort || '').toLowerCase()
  return effort === 'low' || effort === 'high' ? `on:${effort}` : 'on:medium'
}

/** 下拉的一个选项 → 两个后端字段（拆点只有这一处） */
function applyEffort(value: string): Partial<ModelDraft> {
  if (value === 'off') return { thinking: 'off', thinking_effort: '' }
  return { thinking: 'on', thinking_effort: value.slice(3) }
}

/** 「高级参数」收起时也能看到温度：折叠不是藏起来，是不占地方 */
function temperatureText(d: ModelDraft): string {
  return d.temperature.trim() || '0.8'
}

/** `65536` → `64K` / `1048576` → `1M`（DSH 的 contextWindow 徽标） */
function ctxBadge(n?: number | null): string {
  if (!n) return ''
  if (n >= 1048576) return `${Math.round(n / 1048576)}M`
  if (n >= 1024) return `${Math.round(n / 1024)}K`
  return String(n)
}

/** `${CUSTOM_LLM_KEY}` → `CUSTOM_LLM_KEY`；不是引用就返回空串 */
function envNameOf(value: string): string {
  const m = /^\$\{([A-Z_][A-Z0-9_]*)\}$/.exec((value || '').trim())
  return m ? m[1] : ''
}

export default function ModelSettings({
  fields = [],
  config,
  baseline,
  onChange,
}: {
  /** 留在本页的「请求行为」类系统开关（schema 的 `llm` 组剩下的那几个）。
   *  模型本身不在这里配——它们在 provider 卡和模型目录里。 */
  fields?: FieldDef[]
  config: Record<string, unknown> | null
  baseline?: Record<string, unknown> | null
  onChange: (next: Record<string, unknown>) => void
}) {
  const [data, setData] = useState<ModelsResponse | null>(null)
  const [error, setError] = useState('')
  const [tip, setTip] = useState('')
  const [busy, setBusy] = useState('')
  const [openId, setOpenId] = useState('')
  // provider 卡**默认全部收起**，与 DSH Harness 一致（它的 `expanded` 初始是
  // `new Set()`，每行只露一行头，点箭头才展开编辑区）。2026-10-28 主人批评：
  // 「AI模型每个卡片都是打开状态」——一屏全是输入框，找不到重点。
  // 用 Set 而不是单值：可以同时展开两个组对比（DSH 也是这个语义）。
  const [openProviders, setOpenProviders] = useState<Set<string>>(() => new Set())
  const [models, setModels] = useState<ModelDraft[]>([])
  const [providers, setProviders] = useState<ProviderDraft[]>([])
  const [secrets, setSecrets] = useState<Record<string, boolean>>({})
  const [fetched, setFetched] = useState<{ pid: string; names: string[] } | null>(null)
  const [fetchErr, setFetchErr] = useState('')

  const load = async () => {
    try {
      // 不再自己拉 `/api/config`：设置页已经有一份，两份 config state 会互相
      // 覆盖（教训 #51：同一事实只许一个派生点）。模型这一侧只读 `/api/models`。
      const [m, s] = await Promise.all([
        api.models(),
        api.secrets().catch(() => null),
      ])
      setData(m)
      setModels((m.profiles ?? []).map(toDraft))
      setProviders(
        (m.providers ?? []).map((p) => ({
          label: p.label || PROVIDER_LABELS[p.id] || p.id,
          base_url: p.base_url || '',
          api: p.api || 'openai-completions',
        })),
      )
      if (s) {
        const map: Record<string, boolean> = {}
        for (const e of s.entries) map[e.name] = e.configured
        setSecrets(map)
      }
      setError('')
    } catch (e) {
      setError((e as Error).message)
    }
  }

  useEffect(() => {
    void load()
  }, [])

  const active = data?.active ?? ''
  const providerList = data?.providers ?? []

  const toggleProvider = (pid: string) => {
    setOpenProviders((prev) => {
      const next = new Set(prev)
      if (next.has(pid)) next.delete(pid)
      else next.add(pid)
      return next
    })
  }

  /** 模型目录按 provider 分组（顺序 = 组表顺序，不打乱用户的排列） */
  const grouped = useMemo(() => {
    const byProvider = new Map<string, number[]>()
    models.forEach((m, i) => {
      const key = m.provider || 'custom'
      if (!byProvider.has(key)) byProvider.set(key, [])
      byProvider.get(key)!.push(i)
    })
    return byProvider
  }, [models])

  // ── provider 卡：失焦即存 ──────────────────────────────────
  const patchProvider = (pid: string, patch: Partial<ProviderDraft>) => {
    const idx = providerList.findIndex((p) => p.id === pid)
    if (idx < 0) return
    setProviders((prev) =>
      prev.map((p, i) => (i === idx ? { ...p, ...patch } : p)),
    )
  }

  const commitProvider = async (pid: string) => {
    const group = providerList.find((p) => p.id === pid)
    const draft = providers[providerList.findIndex((p) => p.id === pid)]
    if (!group || !draft) return
    const same =
      draft.label === (group.label || '') &&
      draft.base_url === (group.base_url || '') &&
      draft.api === (group.api || 'openai-completions')
    if (same) return
    setBusy(pid)
    setError('')
    try {
      await api.saveProvider({
        id: pid,
        label: draft.label,
        base_url: draft.base_url,
        api: draft.api,
      })
      setTip('provider 已保存')
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  /** 写 Key：引用形态写 `.env`（真值不进 config.yaml），否则先落引用再写值 */
  const saveProviderKey = async (pid: string, rawValue: string) => {
    const v = rawValue.trim()
    if (!v) return
    const group = providerList.find((p) => p.id === pid)
    if (!group) return
    setBusy(pid)
    setError('')
    try {
      let env = envNameOf(group.api_key)
      if (!env) {
        // 新组：先生成一个 `${...}` 引用落盘，再写 .env——顺序不能反，
        // .env 的白名单只放行「配置里已经引用到的」变量名。
        env = `${pid.toUpperCase().replace(/[^A-Z0-9]/g, '_')}_KEY`
        await api.saveProvider({ id: pid, api_key: `\${${env}}` })
      }
      await api.saveSecret(env, v)
      setTip(`已写进 .env 的 ${env} —— 重启糖糖后生效`)
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  /** 失败时不留空壳：建组后立刻可编辑，但删掉它是安全的（没有模型引用） */
  const addProvider = async () => {
    const id = `p${Date.now().toString(36)}`
    setBusy('new-provider')
    try {
      await api.saveProvider({
        id,
        label: '新的 provider',
        base_url: '',
        api: 'openai-completions',
      })
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const removeProvider = async (pid: string) => {
    setBusy(pid)
    setError('')
    try {
      await api.saveProvider({ id: pid, remove: true })
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const fetchProviderModels = async (pid: string) => {
    setBusy(`fetch:${pid}`)
    setFetchErr('')
    setFetched(null)
    try {
      const res = await api.fetchModels({ provider_id: pid })
      setFetched({ pid, names: res.models })
    } catch (e) {
      setFetchErr((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  // ── 模型目录：显式保存（只经 `/api/models/save`，不碰设置页的配置快照）──
  const saveModels = async (next: ModelDraft[], activeNext: string) => {
    setBusy('models')
    setError('')
    try {
      const entries = next.filter((d) => d.model.trim()).map(toEntry)
      await api.saveModels({ models: entries, active: activeNext })
      setTip('模型目录已保存')
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const activate = async (id: string) => {
    setBusy(id)
    try {
      await api.activateModel({ id })
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy('')
    }
  }

  const draftAt = (i: number, patch: Partial<ModelDraft>) => {
    setModels((prev) => prev.map((d, j) => (j === i ? { ...d, ...patch } : d)))
  }

  const removeModel = async (i: number) => {
    const gone = models[i]
    const next = models.filter((_, j) => j !== i)
    const entries = next.filter((d) => d.model.trim()).map(toEntry)
    const activeNext =
      gone.id && gone.id === active ? (entries[0]?.id ?? '') : active
    setOpenId('')
    await saveModels(next, activeNext)
  }

  /** 从「获取可用模型」里点一个 → 追加到该 provider 的目录（同名不重复加） */
  const addFetched = async (pid: string, name: string) => {
    if (models.some((m) => m.provider === pid && m.model === name)) {
      setTip(`${name} 已经在目录里了`)
      return
    }
    const d = { ...emptyModel(pid), model: name, label: name }
    const next = [...models, d]
    setModels(next)
    await saveModels(next, active || d.id)
  }

  /** 新模型行立刻进 `models`（单一数据源），并展开它 */
  const addModel = (pid: string) => {
    const d = emptyModel(pid)
    setModels((prev) => [...prev, d])
    setOpenId(d.id)
  }

  return (
    <div className="settings-body">
      <div className="settings-head">
        <p className="settings-desc explain-only">
          provider 管连接（地址 / 协议 / 密钥，一组一份），模型目录管每个模型能做什么
          （上下文窗口、最大输出、能不能看图）。别处选模型一律从这里选，不再单独填
          地址和 Key。
        </p>
        <button
          className="btn btn-ghost"
          disabled={busy === 'new-provider'}
          onClick={() => void addProvider()}
        >
          <Plus size={14} />
          添加 provider
        </button>
      </div>

      {error && <div className="notice notice-error">{error}</div>}
      {tip && <div className="notice">{tip}</div>}

      <div className="model-list">
        {providerList.length === 0 && (
          <div className="settings-card">
            <div className="settings-empty">
              还没有 provider。点右上角「添加 provider」，填好接口地址和 Key 之后
              就能在它下面挂模型了。
            </div>
          </div>
        )}

        {providerList.map((group, pi) => {
          const draft = providers[pi] ?? {
            label: group.label, base_url: group.base_url, api: group.api,
          }
          const indices = grouped.get(group.id) ?? []
          const env = envNameOf(group.api_key)
          const configured = env ? Boolean(secrets[env]) : Boolean(group.api_key)
          const isOpen = openProviders.has(group.id)
          return (
            <div
              key={group.id}
              className={'provider-card' + (isOpen ? ' is-open' : '')}
            >
              {/* 行头：一行 = 状态点 + 名称 + 协议 + 模型数 + 箭头 + 删除。
                  收起时**不渲染任何输入框**——这是照 DSH Harness 的
                  `rowCard → rowHead + (expanded ? body : null)` 做的。 */}
              <div className="provider-head">
                <span
                  className={'provider-dot' + (configured ? ' is-ok' : ' is-missing')}
                  title={configured ? 'API Key 已配' : 'API Key 还没配'}
                />
                <button
                  className="provider-name-btn"
                  onClick={() => toggleProvider(group.id)}
                  title={isOpen ? '收起' : '展开'}
                >
                  {draft.label || group.id}
                </button>
                <span className="provider-chip">
                  {PROTOCOLS.find((p) => p.value === draft.api)?.label ?? draft.api}
                </span>
                {indices.length > 0 && (
                  <span className="provider-chip">{indices.length} 个模型</span>
                )}
                {busy === group.id && <Loader2 size={13} className="spin" />}
                <button
                  className="icon-btn"
                  onClick={() => toggleProvider(group.id)}
                  aria-expanded={isOpen}
                  title={isOpen ? '收起' : '展开'}
                >
                  {isOpen ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                </button>
                <button
                  className="btn btn-ghost btn-danger"
                  title="删除这个 provider（还有模型引用时会被拒绝）"
                  onClick={() => void removeProvider(group.id)}
                >
                  <Trash2 size={13} />
                </button>
              </div>

              {isOpen && (
                <>
                  {group.derived && (
                    <div className="provider-note">
                      这一组是从旧配置里读出来的（地址 / Key 还写在模型档位上）——改一次
                      就落成正式的 provider 组。
                    </div>
                  )}

                  <div className="provider-grid">
                    <label className="field field-wide">
                      <span>名称</span>
                      <input
                        className="provider-name"
                        value={draft.label}
                        placeholder="provider 名称（例如：自建中转）"
                        onChange={(e) => patchProvider(group.id, { label: e.target.value })}
                        onBlur={() => void commitProvider(group.id)}
                      />
                    </label>
                    <label className="field field-wide">
                      <span>API 地址</span>
                      <input
                        value={draft.base_url}
                        placeholder="https://api.example.com（会自动补 /v1）"
                        onChange={(e) => patchProvider(group.id, { base_url: e.target.value })}
                        onBlur={() => void commitProvider(group.id)}
                      />
                    </label>
                    <label className="field">
                      <span>API 协议</span>
                      <select
                        value={draft.api}
                        onChange={(e) => {
                          patchProvider(group.id, { api: e.target.value })
                          void commitProvider(group.id)
                        }}
                      >
                        {PROTOCOLS.map((p) => (
                          <option key={p.value} value={p.value}>{p.label}</option>
                        ))}
                      </select>
                    </label>
                  </div>

              <div className="provider-key">
                <span className="provider-key-state">
                  {env ? (
                    <>
                      API Key：已绑定 <code>.env</code> 的 <code>{env}</code>
                      {configured ? <span className="ok">（已填）</span>
                        : <span className="miss">（还没填）</span>}
                    </>
                  ) : (
                    <>
                      API Key：{group.api_key ? '写在 config 里（明文）' : '还没配'}
                    </>
                  )}
                </span>
                <div className="secret-bound-line">
                  <input
                    type="password"
                    placeholder={env
                      ? `粘贴新值（写进 .env 的 ${env}）`
                      : '粘贴 API Key（会写进 .env，不回显）'}
                    spellCheck={false}
                    onKeyDown={(e) => {
                      if (e.key !== 'Enter') return
                      const el = e.currentTarget
                      void saveProviderKey(group.id, el.value)
                      el.value = ''
                    }}
                  />
                  <span className="form-hint">回车保存</span>
                </div>
              </div>

              <div className="provider-models-head">
                <span>模型目录</span>
                <span className="provider-models-count">{indices.length} 个</span>
                <button
                  className="btn btn-ghost"
                  disabled={busy === `fetch:${group.id}`}
                  onClick={() => void fetchProviderModels(group.id)}
                >
                  {busy === `fetch:${group.id}`
                    ? <Loader2 size={13} className="spin" />
                    : <Download size={13} />}
                  获取可用模型
                </button>
              </div>

              {fetchErr && fetched?.pid !== group.id && (
                <div className="form-error">{fetchErr}</div>
              )}

              {fetched?.pid === group.id && (
                <div className="fetched-list">
                  {fetched.names.length === 0 && (
                    <span className="form-hint">这个端点没有返回任何模型</span>
                  )}
                  {fetched.names.map((name) => (
                    <button
                      key={name}
                      className="chip"
                      onClick={() => void addFetched(group.id, name)}
                    >
                      <Plus size={11} />
                      {name}
                    </button>
                  ))}
                </div>
              )}

              {indices.length === 0 && (
                <div className="provider-empty">
                  还没有模型。用上面的「获取可用模型」拉一份，或者在下面手填。
                </div>
              )}

              {indices.length > 0 && (
                <div className="model-rows">
                  {indices.map((i) => (
                    <ModelRow
                      key={models[i].id || `new:${i}`}
                      draft={models[i]}
                      isActive={models[i].id === active}
                      open={openId === models[i].id}
                      busy={busy === 'models'}
                      onChange={(patch) => draftAt(i, patch)}
                      onToggle={() => setOpenId(
                        openId === models[i].id ? '' : models[i].id)}
                      onSave={() => {
                        // 模型 ID 为空的行 `saveModels` 会整行过滤掉（后端不需要
                        // 没有 ID 的档位），但那样只提示「已保存」= 静默丢数据。
                        if (!models[i].model.trim()) {
                          setError('模型 ID 不能为空 —— 这一行还没保存')
                          return
                        }
                        setOpenId('')
                        void saveModels(models, active || models[i].id)
                      }}
                      onCancel={() => {
                        setOpenId('')
                        void load()
                      }}
                      onActivate={() => void activate(models[i].id)}
                      onRemove={() => void removeModel(i)}
                    />
                  ))}
                </div>
              )}

                  <button
                    className="btn btn-ghost provider-add-model"
                    onClick={() => addModel(group.id)}
                  >
                    <Plus size={13} />
                    添加模型
                  </button>
                </>
              )}
            </div>
          )
        })}
      </div>

      {/* 模型之外的「请求行为」开关：这几项不属于某个 provider 或某个档位，
          所以留在本页底部。它们改的是设置页那份 config state，由设置页底部的
          「保存」统一落盘（模型目录则走 `/api/models/save` 立即生效）。

          左右内边距由卡片负责（`.settings-card-fields`）：`.settings-fields`
          只有 padding-bottom、`.dsl-row` 只有 padding 16px 0，之前字段是贴着
          卡片边框的（2026-10-28 主人说的「左右间距太紧」）。 */}
      {fields.length > 0 && config && (
        <div className="settings-card request-behavior">
          <div className="form-head">请求行为（不属于某个模型）</div>
          <div className="settings-card-fields">
            <SettingsFields
              fields={fields}
              config={config}
              baseline={baseline}
              onChange={onChange}
            />
          </div>
        </div>
      )}
    </div>
  )
}

/** 一行模型。
 *
 *  收起态 = **一条信息行**：单选 · 显示名 · 模型 ID · 能力标签组 · 当前 · 展开 · 删除。
 *  这里刻意**不渲染输入框**（改之前是两个常驻输入框）：收起态是「浏览 / 切换」
 *  的场景，输入框让每一行看起来像表单；更要紧的是收起态改了名字没有保存入口
 *  （保存按钮只在展开区里），改动会静默丢失。编辑收敛到展开区第一组，和
 *  「保存参数」在一起。 */
function ModelRow({
  draft, isActive, open, busy, onChange, onToggle, onSave, onCancel,
  onActivate, onRemove,
}: {
  draft: ModelDraft
  isActive: boolean
  open: boolean
  busy: boolean
  onChange: (patch: Partial<ModelDraft>) => void
  onToggle: () => void
  onSave: () => void
  onCancel: () => void
  onActivate: () => void
  onRemove: () => void
}) {
  const name = draft.label || draft.model || '（未命名）'
  // 模型 ID 只在「和显示名不一样」时占第二行：一样的时候再写一遍是纯重复
  const showId = Boolean(draft.model) && draft.model !== draft.label
  const ctx = ctxBadge(Number(draft.context_window) || 0)
  return (
    <div
      className={
        'model-row-card' + (isActive ? ' is-active' : '') + (open ? ' is-open' : '')
      }
    >
      <div className="model-row">
        <button
          className="model-radio-btn"
          title={isActive ? '当前就在用它' : '设为当前使用的模型'}
          aria-pressed={isActive}
          onClick={onActivate}
        >
          <span className="model-radio" />
        </button>
        <div className="model-identity">
          <span className="model-row-name" title={name}>{name}</span>
          {showId && <span className="model-row-id" title={draft.model}>{draft.model}</span>}
        </div>
        {(ctx || draft.vision) && (
          <span className="model-caps">
            {ctx && <span className="model-cap">{ctx}</span>}
            {draft.vision && <span className="model-cap is-vision">可看图</span>}
          </span>
        )}
        {isActive && <span className="badge badge-accent">当前</span>}
        <button
          className="icon-btn"
          title={open ? '收起参数' : '展开参数'}
          aria-expanded={open}
          disabled={busy}
          onClick={onToggle}
        >
          {open ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
        </button>
        <button
          className="icon-btn danger"
          title="删除这个模型"
          disabled={busy}
          onClick={onRemove}
        >
          <Trash2 size={13} />
        </button>
      </div>
      {open && (
        <ModelEditor
          draft={draft}
          onChange={onChange}
          onSave={onSave}
          onCancel={onCancel}
        />
      )}
    </div>
  )
}

/** 展开后的编辑区。层级（自上而下，只用间距分组，不加边框）：
 *
 *   身份（显示名 | 模型 ID）
 *   能力（上下文窗口 | 最大输出 tokens | 输入类型跨两列）
 *   高级参数（默认收起：推理强度 | 温度）
 *   底部操作（取消 | 保存参数）
 *
 * 尺寸关系照 DSH 的 `.modelAdvanced` 实测值：两列网格 + 标签在上控件在下
 * （`.modelField` 是 column），本页内容宽约 520，所以 gap 从 8 放到 10、
 * 卡片内边距放到 10~14px，比 DSH 略松一点但保持同一套比例。 */
function ModelEditor({
  draft, onChange, onSave, onCancel,
}: {
  draft: ModelDraft
  onChange: (patch: Partial<ModelDraft>) => void
  onSave: () => void
  onCancel: () => void
}) {
  // 每次展开都是收起态：高级参数是「一般不用改」的东西，默认不占地方
  const [advanced, setAdvanced] = useState(false)
  const effort = effortValue(draft)
  // 摘要里去掉「（默认）」后缀：折叠头是给「我知道大概是什么」用的，
  // 完整选项在下拉里看
  const effortLabel = (EFFORT_OPTIONS.find((o) => o.value === effort)?.label ?? '中')
    .replace('（默认）', '')
  return (
    <div className="model-editor">
      <div className="model-grid">
        <label className="model-field">
          <span className="model-field-label">显示名</span>
          <input
            className="model-input"
            value={draft.label}
            placeholder="例如 DeepSeek V4 Flash"
            onChange={(e) => onChange({ label: e.target.value })}
          />
        </label>
        <label className="model-field">
          <span className="model-field-label">模型 ID</span>
          <input
            className="model-input model-input-mono"
            value={draft.model}
            placeholder="例如 deepseek-v4.1-flash"
            spellCheck={false}
            onChange={(e) => onChange({ model: e.target.value })}
          />
        </label>
        <label className="model-field">
          <span className="model-field-label">上下文窗口（tokens）</span>
          <input
            className="model-input"
            value={draft.context_window}
            inputMode="numeric"
            placeholder="留空 = 不知道"
            onChange={(e) => onChange({ context_window: e.target.value })}
          />
        </label>
        <label className="model-field">
          <span className="model-field-label">最大输出 tokens</span>
          <input
            className="model-input"
            value={draft.max_tokens}
            inputMode="numeric"
            placeholder="例如 64000"
            onChange={(e) => onChange({ max_tokens: e.target.value })}
          />
        </label>
        <div className="model-field is-wide">
          <span className="model-field-label">输入类型</span>
          <div className="model-flags">
            <span className="model-fixed" title="文本输入始终开启">文本</span>
            <label className="model-check">
              <input
                type="checkbox"
                checked={draft.vision}
                onChange={(e) => onChange({ vision: e.target.checked })}
              />
              图片
            </label>
            <span className="model-field-hint">勾上图片才能用它识图 / 打标</span>
          </div>
        </div>
      </div>

      <div className="model-advanced">
        <button
          type="button"
          className="model-advanced-head"
          aria-expanded={advanced}
          onClick={() => setAdvanced((v) => !v)}
        >
          {advanced ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
          <span className="model-advanced-title">高级参数（一般不用改）</span>
          <span className="model-advanced-sum">
            温度 {temperatureText(draft)} · 推理强度 {effortLabel}
          </span>
        </button>
        {advanced && (
          <div className="model-grid">
            <label className="model-field">
              <span className="model-field-label">推理强度</span>
              <select
                className="model-input"
                value={effort}
                onChange={(e) => onChange(applyEffort(e.target.value))}
              >
                {EFFORT_OPTIONS.map((o) => (
                  <option key={o.value} value={o.value}>{o.label}</option>
                ))}
              </select>
            </label>
            <label className="model-field">
              <span className="model-field-label">温度</span>
              <input
                className="model-input"
                inputMode="decimal"
                value={draft.temperature}
                placeholder="0.8（留空 = 用接口默认）"
                onChange={(e) => onChange({ temperature: e.target.value })}
              />
            </label>
          </div>
        )}
      </div>

      <div className="model-editor-actions">
        {!draft.model.trim() && (
          <span className="model-editor-hint">模型 ID 必填，填了才能保存</span>
        )}
        <button className="btn btn-ghost" onClick={onCancel}>取消</button>
        <button
          className="btn btn-primary"
          onClick={onSave}
          disabled={!draft.model.trim()}
        >
          <Check size={14} />
          保存参数
        </button>
      </div>
    </div>
  )
}
