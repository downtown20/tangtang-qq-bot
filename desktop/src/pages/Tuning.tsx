/**
 * 高级调参（2026-10-26 第 49 轮 B4）——「常量也开放」的入口。
 *
 * ## 主人要的四件事，一项都不能少
 *
 * 每个值必须同时给出 **当前值 / 默认值 / 作用 / 文件:行**。
 * 少任何一样，这个数字就只是噪音（教训 #50：「状态页只把接口字段摆一遍」）。
 * 「现在是什么」和「出厂是什么」分开显示，才知道自己有没有动过它。
 *
 * ## 判据不在这里（教训 #38）
 *
 * 范围校验、跨项约束（单块上限 ≤ 总预算）、能不能写盘——全在后端
 * `agent/tuning.py`（43 条闸门盯着）。本页的即时校验只是**提前提示**，
 * 真判据以后端返回的 `errors` 为准：前端拒绝但它接受，或反过来，都是 bug。
 *
 * ## 三条界面纪律
 *
 * 1. **改完要重启就钉在顶栏**：`note` 常驻，不藏 tooltip 里。
 * 2. **改了哪些行必须一眼看见**：行首竖条 + 「已修改 N 项」计数 + 只看已改开关。
 * 3. **输错当场说**：越界/非数字立即标红并写出合法范围，不等点保存。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  AlertTriangle,
  Check,
  LoaderCircle,
  RotateCcw,
  Save,
  SlidersHorizontal,
} from 'lucide-react'
import { api } from '../lib/api'
import SpecialMode from '../components/SpecialMode'
import type { TuningGroup, TuningPayload } from '../lib/api'
import './Tuning.css'

type Notice = { kind: 'ok' | 'warn' | 'error'; text: string } | null

/** 一行的即时校验结果（后端仍是最终判据） */
type FieldError = string | null

function validate(item: TuningGroup['items'][number], raw: string): FieldError {
  const text = raw.trim()
  if (text === '') return '不能为空'
  if (!/^-?\d+(\.\d+)?$/.test(text)) return '只能填数字'
  const num = Number(text)
  if (item.kind === 'int' && !Number.isInteger(num)) return '这一项必须是整数'
  if (num < item.lo) return `不能小于 ${item.lo}`
  if (num > item.hi) return `不能大于 ${item.hi}`
  return null
}

export default function Tuning() {
  const [payload, setPayload] = useState<TuningPayload | null>(null)
  const [loadError, setLoadError] = useState('')
  const [draft, setDraft] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<Notice>(null)
  const [activeGroup, setActiveGroup] = useState('')
  const [onlyChanged, setOnlyChanged] = useState(false)

  const load = useCallback(async () => {
    try {
      const data = await api.tuning()
      setPayload(data)
      setDraft(
        Object.fromEntries(
          data.groups.flatMap((g) => g.items.map((i) => [i.key, String(i.value)])),
        ),
      )
      setLoadError('')
      setActiveGroup((prev) => prev || data.groups[0]?.key || '')
    } catch (e) {
      setLoadError((e as Error).message)
    }
  }, [])

  useEffect(() => {
    void load()
  }, [load])

  /** 每一项的编辑状态：dirty = 输入框值和磁盘上的当前值不同 */
  const rows = useMemo(() => {
    if (!payload) return []
    return payload.groups.flatMap((g) =>
      g.items.map((item) => {
        const raw = draft[item.key] ?? String(item.value)
        const err = validate(item, raw)
        const dirty = !err && Number(raw) !== item.value
        return { ...item, group: g.key, groupTitle: g.title, raw, err, dirty }
      }),
    )
  }, [payload, draft])

  const dirtyRows = rows.filter((r) => r.dirty)
  const errorRows = rows.filter((r) => r.err && r.raw.trim() !== '')

  const setField = (key: string, value: string) => {
    setDraft((prev) => ({ ...prev, [key]: value }))
    setNotice(null)
  }

  const resetField = (key: string, def: number) => setField(key, String(def))

  const save = async () => {
    if (!dirtyRows.length) return
    setSaving(true)
    setNotice(null)
    try {
      const values = Object.fromEntries(dirtyRows.map((r) => [r.key, Number(r.raw)]))
      const res = await api.tuningSave(values)
      if (!res.ok) {
        setNotice({ kind: 'error', text: (res.errors || ['保存失败']).join('；') })
        return
      }
      if (res.tuning) setPayload(res.tuning)
      setDraft((prev) => {
        const next = { ...prev }
        Object.entries(res.saved || {}).forEach(([k, v]) => (next[k] = String(v)))
        return next
      })
      setNotice({ kind: 'ok', text: res.note || '已保存' })
    } catch (e) {
      setNotice({ kind: 'error', text: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }

  const groupItems = (g: TuningGroup) => {
    const items = rows.filter((r) => r.group === g.key)
    return onlyChanged ? items.filter((r) => r.dirty) : items
  }
  const current = payload?.groups.find((g) => g.key === activeGroup) || payload?.groups[0]

  if (loadError) {
    return (
      <div className="tuning-page">
        <div className="tuning-empty">
          <AlertTriangle size={20} />
          <p>读不到调参台账：{loadError}</p>
          <button className="btn-ghost" onClick={() => void load()}>
            重试
          </button>
        </div>
      </div>
    )
  }

  if (!payload) {
    return (
      <div className="tuning-page">
        <div className="tuning-empty">
          <LoaderCircle size={20} className="spin" />
          <p>正在读台账…</p>
        </div>
      </div>
    )
  }

  return (
    <div className="tuning-page">
      {/* 特殊模式放最上面：它不在 TuningSpec 台账里（是多包配置，不是单个常量），
          但主人是在这一页找它的（原话：「高级调参的场景触发范本的特殊模式是前端的哪个」）。 */}
      <SpecialMode />

      <header className="tuning-head">
        <div className="tuning-title">
          <SlidersHorizontal size={18} />
          <h1>高级调参</h1>
          <span className="tuning-count">
            {payload.total} 项可调
            {payload.changed > 0 && ` · 已保存 ${payload.changed} 项偏离默认`}
            {dirtyRows.length > 0 && (
              <em className="tuning-count-pending"> · 未保存 {dirtyRows.length} 项</em>
            )}
          </span>
        </div>
        <div className="tuning-head-right">
          <label className="tuning-toggle">
            <input
              type="checkbox"
              checked={onlyChanged}
              onChange={(e) => setOnlyChanged(e.target.checked)}
            />
            只看已修改
          </label>
          <button
            className="btn-primary"
            disabled={saving || !dirtyRows.length || !!errorRows.length}
            onClick={() => void save()}
            title={
              errorRows.length
                ? '有输入不合法，先改好再保存'
                : !dirtyRows.length
                  ? '还没有改动'
                  : '写入 config.yaml 的 tuning 节'
            }
          >
            {saving ? <LoaderCircle size={14} className="spin" /> : <Save size={14} />}
            保存{dirtyRows.length ? `（${dirtyRows.length}）` : ''}
          </button>
        </div>
      </header>

      <p className="tuning-note">
        <AlertTriangle size={13} />
        {payload.note} 值都存在 <code>config.yaml</code> 的 <code>tuning</code> 节里，
        删掉某项即回到默认值。
      </p>

      {notice && (
        <div className={`tuning-notice ${notice.kind}`}>
          {notice.kind === 'ok' ? <Check size={14} /> : <AlertTriangle size={14} />}
          {notice.text}
        </div>
      )}

      <div className="tuning-body">
        <nav className="tuning-groups">
          {payload.groups.map((g) => {
            const items = rows.filter((r) => r.group === g.key)
            const changed = items.filter((r) => r.dirty).length
            return (
              <button
                key={g.key}
                className={`tuning-group ${g.key === current?.key ? 'active' : ''}`}
                onClick={() => setActiveGroup(g.key)}
              >
                <span>{g.title}</span>
                <span className="tuning-group-meta">
                  {changed > 0 && <em>{changed}</em>}
                  {items.length}
                </span>
              </button>
            )
          })}
        </nav>

        <div className="tuning-list">
          {current && groupItems(current).length === 0 && (
            <p className="tuning-hint">
              {onlyChanged ? '这一组没有改动过的项。' : '这一组没有可调项。'}
            </p>
          )}
          {current &&
            groupItems(current).map((item) => (
              <div
                key={item.key}
                className={`tuning-row ${item.dirty ? 'dirty' : ''} ${item.err ? 'bad' : ''}`}
              >
                <div className="tuning-row-main">
                  <div className="tuning-row-title">
                    <span className="tuning-label">{item.label}</span>
                    <code className="tuning-const">{item.const}</code>
                  </div>
                  {/* 参数作用 = 解释（讲解模式可关）；默认值/范围/文件:行是数据，永远显示 */}
                  <p className="tuning-desc explain-only">{item.desc}</p>
                  <p className="tuning-meta">
                    默认 <b>{item.default}</b>
                    <span className="sep">·</span>
                    范围 {item.lo} – {item.hi}
                    <span className="sep">·</span>
                    <code className="tuning-source">{item.source}</code>
                  </p>
                  {item.err && <p className="tuning-err">{item.err}</p>}
                </div>
                <div className="tuning-row-input">
                  <input
                    type="number"
                    step={item.kind === 'int' ? 1 : 0.01}
                    value={item.raw}
                    onChange={(e) => setField(item.key, e.target.value)}
                    className={item.err ? 'invalid' : ''}
                  />
                  <button
                    className="btn-ghost tiny"
                    onClick={() => resetField(item.key, item.default)}
                    disabled={Number(item.raw) === item.default}
                    title={`改回默认值 ${item.default}`}
                  >
                    <RotateCcw size={12} />
                  </button>
                </div>
              </div>
            ))}
        </div>
      </div>
    </div>
  )
}
