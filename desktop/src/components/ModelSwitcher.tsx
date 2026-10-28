import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  Loader2,
  Search,
  Settings2,
  X,
  AlertCircle,
  Sparkles,
} from 'lucide-react'
import { api, type ModelProfile, type ModelsResponse } from '../lib/api'
import './ModelSwitcher.css'

/** 推理档位：DSH 的 reasoningEfforts 是「每个模型一组档位 + 关闭」。
 *  我们映射到 config 的 (thinking, thinking_effort) 两个字段 —— 只有这两条
 *  是 agent/handler.py 真正读的，多造字段等于造死代码。 */
const EFFORTS = [
  { key: 'default', label: '默认', desc: '跟随模型自身默认', effort: '', thinking: undefined as string | undefined },
  { key: 'off', label: '关闭', desc: '不思考，最快', effort: '', thinking: 'off' },
  { key: 'low', label: '低', desc: '轻量思考', effort: 'low', thinking: 'on' },
  { key: 'medium', label: '中', desc: '平衡', effort: 'medium', thinking: 'on' },
  { key: 'high', label: '高', desc: '最慢，最深', effort: 'high', thinking: 'on' },
]

function effortOf(p: ModelProfile | undefined): (typeof EFFORTS)[number] {
  if (!p) return EFFORTS[0]
  if ((p.thinking ?? '') === 'off') return EFFORTS[1]
  const e = (p.thinking_effort ?? '') as string
  return EFFORTS.find((x) => x.effort === e && x.key !== 'default') ?? EFFORTS[0]
}

export default function ModelSwitcher({ onOpenSettings }: { onOpenSettings: () => void }) {
  const [models, setModels] = useState<ModelsResponse | null>(null)
  const [open, setOpen] = useState(false)
  const [panel, setPanel] = useState<'models' | 'effort'>('models')
  const [query, setQuery] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pos, setPos] = useState<{ left: number; top?: number; bottom?: number } | null>(null)

  const triggerRef = useRef<HTMLButtonElement>(null)
  const menuRef = useRef<HTMLDivElement>(null)

  const load = useCallback(() => {
    api
      .models()
      .then((m) => {
        setModels(m)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(load, [load])

  const profiles = models?.profiles ?? []
  const active = profiles.find((p) => p.id === models?.active) ?? profiles[0]
  const effort = effortOf(active)
  const label = active?.model || active?.label || '未配置模型'

  const openMenu = () => {
    if (!triggerRef.current) return
    const r = triggerRef.current.getBoundingClientRect()
    const menuW = 330
    const left = Math.min(Math.max(8, r.left), Math.max(8, window.innerWidth - menuW - 8))
    const useTop = window.innerHeight - r.bottom < 360
    setPos(
      useTop
        ? { left, bottom: window.innerHeight - r.top + 8 }
        : { left, top: r.bottom + 8 },
    )
    setPanel('models')
    setQuery('')
    setOpen(true)
  }

  useEffect(() => {
    if (!open) return
    const onDown = (e: MouseEvent) => {
      const t = e.target as Node
      if (menuRef.current?.contains(t) || triggerRef.current?.contains(t)) return
      setOpen(false)
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setOpen(false)
    }
    window.addEventListener('mousedown', onDown)
    window.addEventListener('keydown', onKey)
    return () => {
      window.removeEventListener('mousedown', onDown)
      window.removeEventListener('keydown', onKey)
    }
  }, [open])

  const filtered = useMemo(() => {
    const q = query.trim().toLowerCase()
    if (!q) return profiles
    return profiles.filter((p) =>
      `${p.label} ${p.model} ${p.provider}`.toLowerCase().includes(q),
    )
  }, [profiles, query])

  const selectModel = async (p: ModelProfile) => {
    if (p.id === models?.active) {
      setOpen(false)
      return
    }
    setBusy(true)
    setError(null)
    try {
      await api.activateModel({ id: p.id })
      load()
      setOpen(false)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const selectEffort = async (key: string) => {
    const target = EFFORTS.find((x) => x.key === key)
    if (!target || !active) return
    setBusy(true)
    setError(null)
    try {
      await api.activateModel({
        id: active.id,
        thinking_effort: target.effort,
        thinking: target.thinking ?? '',
      })
      load()
      setPanel('models')
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="msw-root">
      <button
        ref={triggerRef}
        className="msw-trigger"
        onClick={() => (open ? setOpen(false) : openMenu())}
        title={`${label}${effort.key !== 'default' ? ' · 推理' + effort.label : ''}（点击切换）`}
      >
        <Sparkles size={13} className="msw-trigger-icon" />
        <span className="msw-trigger-label">{label}</span>
        <span className="msw-trigger-effort">{effort.key === 'default' ? '' : effort.label}</span>
        <ChevronDown size={13} className={'msw-chevron' + (open ? ' is-open' : '')} />
      </button>

      {open && pos && (
        <div
          ref={menuRef}
          className="msw-menu"
          style={{ left: pos.left, top: pos.top, bottom: pos.bottom }}
        >
          {panel === 'models' ? (
            <>
              {profiles.length > 5 && (
                <div className="msw-search-row">
                  <div className="msw-search">
                    <Search size={13} />
                    <input
                      autoFocus
                      value={query}
                      placeholder="搜索模型"
                      onChange={(e) => setQuery(e.target.value)}
                    />
                    {query && (
                      <button className="msw-search-clear" onClick={() => setQuery('')}>
                        <X size={12} />
                      </button>
                    )}
                  </div>
                </div>
              )}

              {error && (
                <div className="msw-error">
                  <AlertCircle size={12} />
                  <span>{error}</span>
                </div>
              )}

              <div className="msw-groups">
                {filtered.length === 0 && (
                  <div className="msw-empty">
                    {models?.legacy
                      ? '当前是旧版单模型配置，去设置里添加模型档位'
                      : '没有匹配的模型'}
                  </div>
                )}
                {filtered.map((p) => {
                  const isActive = p.id === models?.active
                  return (
                    <button
                      key={p.id}
                      className={'msw-option' + (isActive ? ' is-selected' : '')}
                      onClick={() => selectModel(p)}
                      disabled={busy}
                    >
                      <span className="msw-option-copy">
                        {/* 主行 = 你在「AI 模型」页起的显示名（label）。
                            2026-10-28 主人：「预览界面的模型名字没有和 AI 模型设置的名字
                            同步」——原来这里写的是 `p.model || p.label`，优先显示模型 ID，
                            你起的名字只在副行当附注，等于没同步。 */}
                        <span className="msw-model-name">{p.label || p.model}</span>
                        <span className="msw-model-meta">
                          {/* 副行 = provider 的人话名（不是组 id），后面跟模型 ID 以便对账 */}
                          {p.provider_label || p.provider}
                          {p.model && p.model !== p.label ? ` · ${p.model}` : ''}
                        </span>
                      </span>
                      <span className="msw-check">{isActive && <Check size={14} />}</span>
                    </button>
                  )
                })}
              </div>

              <div className="msw-divider" />

              <button className="msw-cell" onClick={() => setPanel('effort')} disabled={!active}>
                <span className="msw-cell-label">推理档位</span>
                <span className="msw-cell-value">{effort.label}</span>
                <ChevronRight size={12} className="msw-cell-chevron" />
              </button>

              <button
                className="msw-cell"
                onClick={() => {
                  setOpen(false)
                  onOpenSettings()
                }}
              >
                <span className="msw-cell-label">管理模型</span>
                <span className="msw-cell-value" />
                <Settings2 size={12} className="msw-cell-chevron" />
              </button>
            </>
          ) : (
            <>
              <div className="msw-subhead">
                <button className="msw-back" onClick={() => setPanel('models')} title="返回">
                  <ChevronLeft size={14} />
                </button>
                <span>推理档位</span>
                <span className="msw-subhead-model">{label}</span>
              </div>
              <div className="msw-groups">
                {EFFORTS.map((x) => (
                  <button
                    key={x.key}
                    className={'msw-option' + (x.key === effort.key ? ' is-selected' : '')}
                    onClick={() => selectEffort(x.key)}
                    disabled={busy}
                  >
                    <span className="msw-option-copy">
                      <span className="msw-model-name">{x.label}</span>
                      <span className="msw-model-meta">{x.desc}</span>
                    </span>
                    <span className="msw-check">{x.key === effort.key && <Check size={14} />}</span>
                  </button>
                ))}
              </div>
            </>
          )}

          {busy && (
            <div className="msw-busy">
              <Loader2 size={13} className="spin" />
              正在写入配置…
            </div>
          )}
        </div>
      )}
    </div>
  )
}
