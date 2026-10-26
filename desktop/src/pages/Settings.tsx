import { useEffect, useMemo, useState } from 'react'
import { Save, Loader2, X } from 'lucide-react'
import { api } from '../lib/api'
import schemaJson from '../lib/settingsSchema.json'
import { SETTINGS_GROUPS as GROUPS } from '../lib/settingsGroups'
import SettingsFields, { type FieldDef } from '../components/SettingsFields'
import ModelSettings from './settings/ModelSettings'
import './Settings.css'
import '../components/SettingsFields.css'

type SectionSchema = { key: string; title: string; fields: FieldDef[] }
const SCHEMA = schemaJson as SectionSchema[]

/** DSH 同款：设置是**居中模态**（800 宽 / 28 圆角 / 左 188 导航 + 遮罩），不是整页。 */
export default function Settings({
  onClose,
  initialSection,
}: {
  onClose: () => void
  initialSection?: string
}) {
  const [section, setSection] = useState(initialSection ?? 'llm')

  useEffect(() => {
    if (initialSection) setSection(initialSection)
  }, [initialSection])
  const [config, setConfig] = useState<Record<string, unknown> | null>(null)
  const [baseline, setBaseline] = useState<Record<string, unknown> | null>(null)
  const [savedSnapshot, setSavedSnapshot] = useState('')
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [savedTip, setSavedTip] = useState('')

  useEffect(() => {
    api
      .config()
      .then((c) => {
        setConfig(c.raw)
        setBaseline(c.raw)
        setSavedSnapshot(JSON.stringify(c.raw))
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onClose])

  const dirty = useMemo(
    () => !!config && JSON.stringify(config) !== savedSnapshot,
    [config, savedSnapshot],
  )

  const save = async () => {
    if (!config) return
    setSaving(true)
    setError(null)
    try {
      const res = await api.saveConfig(config)
      setSavedSnapshot(JSON.stringify(config))
      setBaseline(config)
      const parts = [`已保存（备份 ${res.backup}）`]
      if (res.normalized?.length) parts.push(`密钥归一: ${res.normalized.join('、')}`)
      setSavedTip(parts.join('；'))
      setTimeout(() => setSavedTip(''), 6000)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving(false)
    }
  }

  const current = SCHEMA.find((s) => s.key === section)
  const label = GROUPS.flatMap((g) => g.items).find((i) => i.key === section)?.label ?? section

  return (
    <div className="settings-overlay" onMouseDown={onClose}>
      <div
        className="settings-panel"
        role="dialog"
        aria-modal="true"
        onMouseDown={(e) => e.stopPropagation()}
      >
        <div className="settings-panel-body">
          <nav className="settings-nav">
            {GROUPS.map((g) => (
              <div key={g.group} className="settings-group">
                <div className="settings-group-title">{g.group}</div>
                {g.items.map(({ key, label: itemLabel, icon: Icon }) => (
                  <button
                    key={key}
                    className={'settings-nav-item' + (section === key ? ' is-active' : '')}
                    onClick={() => setSection(key)}
                  >
                    <Icon size={15} />
                    <span>{itemLabel}</span>
                  </button>
                ))}
              </div>
            ))}
          </nav>

          <section className="settings-content">
            <header className="settings-content-head">
              <h2 className="settings-title">{label}</h2>
              <button className="icon-btn" onClick={onClose} title="关闭（Esc）">
                <X size={16} />
              </button>
            </header>

            {error && <div className="settings-body"><div className="notice notice-error">{error}</div></div>}

            {section === 'llm' ? (
              <ModelSettings />
            ) : current ? (
              <div className="settings-body">
                <p className="settings-desc">{current.title}</p>
                <div className="settings-fields">
                  {config ? (
                    <SettingsFields
                      fields={current.fields}
                      config={config}
                      baseline={baseline}
                      onChange={setConfig}
                    />
                  ) : (
                    <div className="settings-empty">加载中…</div>
                  )}
                </div>
              </div>
            ) : (
              <div className="settings-body">
                <div className="settings-empty">这一组没有可编辑字段。</div>
              </div>
            )}
          </section>
        </div>

        <footer className="settings-footer">
          <span className="footer-tip">
            {dirty ? '有未保存的修改' : savedTip || '所有修改会在保存后写入 config.yaml'}
          </span>
          <button className="btn btn-primary" onClick={save} disabled={!dirty || saving}>
            {saving ? <Loader2 size={14} className="spin" /> : <Save size={14} />}
            保存
          </button>
        </footer>
      </div>
    </div>
  )
}
