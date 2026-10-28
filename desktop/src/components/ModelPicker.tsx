import { useEffect, useMemo, useRef, useState } from 'react'
import { Check, Loader2, RefreshCw, Search } from 'lucide-react'

import { api, type ModelPickerItem } from '../lib/api'
import './ModelPicker.css'

/**
 * 选模型面板（2026-10-27）。
 *
 * 形状对齐 DSH Harness / Codex 的模型选择器（主人给的两张截图）：
 * **搜索框 + 按 provider 分组 + 当前项打勾 + 上下文窗口徽标**。
 *
 * ## 为什么不用 `<select>`
 *
 * 选项分属两套来源（云端档位 / 本机 Ollama），原生 select 没法分组显示徽标和
 * 「可看图」标记，更没法搜索——而主人装了十几个 Ollama 模型，滚动找名字比打两个
 * 字母慢得多。
 *
 * ## 每次打开都重新拉
 *
 * 之前的 bug 是「改了模型，面板里还是旧名字」：数据在页面挂载时拉一次就不再更新。
 * 这个组件在 `open` 变 true 时重新拉，所以永远是当下的事实。
 */
export default function ModelPicker({
  current,
  onPick,
  onClose,
}: {
  /** 当前值，形如 `ref:<档位id>` / `ollama:<模型名>` / `api:<模型名>` */
  current: string
  onPick: (item: ModelPickerItem) => void | Promise<void>
  onClose: () => void
}) {
  const [groups, setGroups] = useState<{ label: string; items: ModelPickerItem[] }[] | null>(null)
  const [error, setError] = useState('')
  const [ollamaError, setOllamaError] = useState('')
  const [query, setQuery] = useState('')
  const [busyKey, setBusyKey] = useState('')
  const boxRef = useRef<HTMLDivElement | null>(null)

  const load = () => {
    setError('')
    setGroups(null)
    api
      .modelsPicker()
      .then((r) => {
        setGroups(r.groups ?? [])
        setOllamaError(r.ollama_error ?? '')
      })
      .catch((e: Error) => setError(e.message))
  }

  useEffect(() => {
    load()
  }, [])

  // 点面板外面 / 按 Esc 关闭：命令面板的肌肉记忆。
  //
  // ⚠ 监听器必须挂在**捕获阶段**（第三个参数 true）。设置页的弹窗容器上有
  // `onMouseDown={(e) => e.stopPropagation()}`（防止点面板内部时把整个设置关掉），
  // 冒泡阶段的 document 监听器**永远收不到事件**——表现就是「点空白处关不掉」
  // （2026-10-28 主人在真实界面里当场发现，我只跑了 tsc + build 没在浏览器点过）。
  // 捕获阶段先于冒泡，stopPropagation 挡不住它。
  useEffect(() => {
    const onDown = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) onClose()
    }
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    document.addEventListener('mousedown', onDown, true)
    document.addEventListener('keydown', onKey)
    return () => {
      // 移除时的第三个参数必须与添加时一致，否则监听器根本不会摘掉
      document.removeEventListener('mousedown', onDown, true)
      document.removeEventListener('keydown', onKey)
    }
  }, [onClose])

  const q = query.trim().toLowerCase()
  const filtered = useMemo(() => {
    if (!groups) return null
    if (!q) return groups
    return groups
      .map((g) => ({
        label: g.label,
        items: g.items.filter(
          (it) =>
            it.label.toLowerCase().includes(q) ||
            it.sub.toLowerCase().includes(q) ||
            it.provider.toLowerCase().includes(q),
        ),
      }))
      .filter((g) => g.items.length > 0)
  }, [groups, q])

  const pick = async (item: ModelPickerItem) => {
    setBusyKey(item.key)
    try {
      await onPick(item)
    } finally {
      setBusyKey('')
    }
  }

  return (
    <div className="model-picker" ref={boxRef} role="dialog" aria-label="选择模型">
      <div className="model-picker-search">
        <Search size={13} />
        <input
          autoFocus
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="搜索模型…"
          spellCheck={false}
        />
        <button className="icon-btn" onClick={load} title="重新拉取（Ollama 装了新模型点这里）">
          <RefreshCw size={12} />
        </button>
      </div>

      <div className="model-picker-list">
        {error && <div className="model-picker-err">拉不到模型清单：{error}</div>}
        {!error && groups === null && (
          <div className="model-picker-hint">
            <Loader2 size={12} className="spin" /> 正在拉模型清单…（本机 Ollama 没在跑时会多等一会儿）
          </div>
        )}
        {!error && filtered && filtered.length === 0 && (
          <div className="model-picker-hint">没有匹配「{query}」的模型</div>
        )}
        {(filtered ?? []).map((group) => (
          <div key={group.label} className="model-picker-group">
            <div className="model-picker-group-label">{group.label}</div>
            {group.items.map((item) => {
              const active = item.key === current
              return (
                <button
                  key={item.key}
                  className={'model-picker-item' + (active ? ' is-active' : '')}
                  onClick={() => void pick(item)}
                  disabled={Boolean(busyKey)}
                  title={item.sub ? `${item.label} · ${item.sub}` : item.label}
                >
                  <span className="model-picker-name">{item.label}</span>
                  {item.badge && <span className="model-picker-badge">{item.badge}</span>}
                  {item.vision && <span className="model-picker-tag">可看图</span>}
                  {busyKey === item.key ? (
                    <Loader2 size={12} className="spin" />
                  ) : active ? (
                    <Check size={13} className="model-picker-check" />
                  ) : null}
                </button>
              )
            })}
          </div>
        ))}
        {ollamaError && (
          <div className="model-picker-warn">
            Ollama 没响应：{ollamaError}
            <br />
            先启动 Ollama（`ollama serve`），再点右上角刷新。
          </div>
        )}
      </div>
    </div>
  )
}
