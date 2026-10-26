import { useCallback, useEffect, useState } from 'react'
import { Loader2, Save } from 'lucide-react'
import { api, type SecretsResponse } from '../../lib/api'

/* ═══════════════════════════════════════════════════════════════
   连接密钥（2026-10-26）

   糖糖要连两样东西：LLM（大脑）和 SnowLuma（QQ 通道）。这两把钥匙原先
   只有旧 Qt 控制台的「密钥管理」能填；Qt 移入 运维/ 后桌面端成为主入口，
   不补这一节，新用户装完就卡在「自检说缺 Token、但没地方填」。

   边界（刻意窄）：只允许写 LLM API Key（变量名跟着 provider 走，由后端
   `agent/secret_refs.PROVIDER_KEY_ENV` 决定）+ SNOWLUMA_TOKEN。白名单外
   一律拒绝——密钥写入是安全敏感面，宁少不多。

   讲解模式：机制说明（为什么写 .env）标 `explain-only`；
   输入框、状态、错误、操作步骤**永不隐藏**。
   ═══════════════════════════════════════════════════════════════ */

export default function SecretsSettings() {
  const [data, setData] = useState<SecretsResponse | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [saving, setSaving] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [tip, setTip] = useState('')

  const load = useCallback(() => {
    api
      .secrets()
      .then((d) => {
        setData(d)
        setError(null)
      })
      .catch((e: Error) => setError(e.message))
  }, [])

  useEffect(() => {
    load()
  }, [load])

  const save = async (name: string) => {
    const value = (drafts[name] ?? '').trim()
    if (!value) {
      setError('请先填写这个值（留空不会清空已保存的密钥）')
      return
    }
    setSaving(name)
    setError(null)
    setTip('')
    try {
      const res = await api.saveSecret(name, value)
      setDrafts((d) => ({ ...d, [name]: '' }))
      setTip(`${name} 已保存${res.backup ? `（备份 ${res.backup}）` : ''}——重启糖糖后生效`)
      load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setSaving('')
    }
  }

  return (
    <div className="settings-body">
      <p className="settings-desc explain-only">
        这两个值写进项目根目录的 <code>.env</code>，config.yaml 里只保留
        <code> ${'{VAR}'} </code>引用——密钥写进 config.yaml 会随 git 泄漏。
      </p>

      {error && <div className="notice notice-error">{error}</div>}
      {tip && <div className="notice notice-ok">{tip}</div>}

      {!data ? (
        <div className="settings-empty">加载中…</div>
      ) : (
        <div className="secrets-list">
          {data.entries.map((e) => (
            <div className="secrets-row" key={e.name}>
              <div className="secrets-row-main">
                <div className="secrets-row-title">{e.label}</div>
                <div className="secrets-row-sub">
                  {e.configured ? `已配置 · ${e.masked}` : '未配置'}
                  <span className="muted"> · {e.name}</span>
                </div>
              </div>
              <div className="secrets-row-edit">
                <input
                  type="password"
                  placeholder="粘贴新值（不回显）"
                  autoComplete="off"
                  value={drafts[e.name] ?? ''}
                  onChange={(ev) =>
                    setDrafts((d) => ({ ...d, [e.name]: ev.target.value }))
                  }
                  onKeyDown={(ev) => {
                    if (ev.key === 'Enter') save(e.name)
                  }}
                />
                <button
                  className="btn btn-primary"
                  onClick={() => save(e.name)}
                  disabled={saving === e.name}
                >
                  {saving === e.name ? (
                    <Loader2 size={14} className="spin" />
                  ) : (
                    <Save size={14} />
                  )}
                  保存
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <p className="settings-desc">
        保存后先重启糖糖（或重启桌面端桥）。SnowLuma Token 必须和 SnowLuma
        网页面板里「授权 Token」那一栏完全一致；两边都空也算正常（不鉴权）。
      </p>
    </div>
  )
}
