/** 特殊模式（场景触发范本）——2026-10-26 第五轮。
 *
 * 主人：「高级调参的场景触发范本的特殊模式是前端的哪个你没有说清楚，
 * 用户看的是前端不是文件内的代码，该解释很无力。」
 *
 * 所以这个组件的唯一使命是：**在一屏里说清它是什么、现在什么状态、怎么改**。
 * 不是把接口字段摆一遍（教训 #50），每个数字都要有锚。
 *
 * 三条设计约束（来自主人原话）：
 * 1. TTL 要能在这里改，**默认永久**；
 * 2. 「特殊模式」不是「亲密模式」——场景是中立可换的，组件文案里不出现任何
 *    具体内容名（教训 #52：内容名不进信息架构）；
 * 3. 一个场景配一个包，可以配多个——不是「只能有一个」。
 */
import { useCallback, useEffect, useState } from 'react'
import { api } from '../lib/api'
import type { ScenarioPack, ScenarioOption } from '../lib/api'
import { NumberField } from './SettingsFields'
import './SpecialMode.css'

const HOUR = 3600

export default function SpecialMode() {
  const [packs, setPacks] = useState<ScenarioPack[]>([])
  const [scenarios, setScenarios] = useState<ScenarioOption[]>([])
  const [editing, setEditing] = useState<number | null>(null)
  const [draft, setDraft] = useState<ScenarioPack | null>(null)
  const [busy, setBusy] = useState(false)
  const [notice, setNotice] = useState<{ kind: 'ok' | 'warn' | 'error'; text: string } | null>(null)

  const load = useCallback(() => {
    setNotice(null)
    void api
      .scenarioPacks()
      .then((d) => {
        setPacks(d.packs ?? [])
        setScenarios(d.available_scenarios ?? [])
      })
      .catch((e: Error) => setNotice({ kind: 'error', text: e.message }))
  }, [])

  useEffect(load, [load])

  const beginEdit = (i: number) => {
    setEditing(i)
    setDraft({ ...packs[i] })
  }

  const save = async () => {
    if (editing === null || !draft) return
    const next = packs.map((p, i) => (i === editing ? { ...p, ...draft } : p))
    setBusy(true)
    try {
      const r = await api.scenarioPacksSave(next)
      setPacks(r.packs ?? next)
      setEditing(null)
      setDraft(null)
      setNotice({ kind: 'ok', text: '已保存。**重启 bot 后生效**（她读的是启动时加载的配置）。' })
    } catch (e) {
      setNotice({ kind: 'error', text: (e as Error).message })
    } finally {
      setBusy(false)
    }
  }

  const scenarioDisplay = (name: string): string => {
    const hit = scenarios.find((s) => s.name === name)
    return hit ? hit.display : name
  }

  return (
    <section className="sm-block">
      <header className="sm-head">
        <div>
          <h2>特殊模式</h2>
          {/* 机制说明 = 解释（讲解模式可关）。数字「N 个」与卡片本身永远显示 */}
          <p className="explain-only">
            某个场景被激活时，把一批范本整篇注入她的上下文（不走检索、不参与背景预算淘汰）。
            <strong>平时一个字都不进</strong>——所以改这里不会影响日常对话。
          </p>
        </div>
        <span className="sm-count">{packs.length} 个</span>
      </header>

      <div className="sm-rules explain-only">
        <span>生效要同时满足：</span>
        <ol>
          <li>这个人/群<strong>挂了</strong>这个场景</li>
          <li>
            她判断<strong>该进入</strong>
            <em>（需要激活的包才要这步；不需要的挂上就生效）</em>
          </li>
          <li>在<strong>有效期</strong>内<em>（永久 = 一直有效，直到她退出或撤权）</em></li>
        </ol>
      </div>

      {notice ? <div className={'sm-notice ' + notice.kind}>{notice.text}</div> : null}

      {packs.length === 0 ? (
        <p className="sm-empty">还没有配置包。没有包 = 任何场景都不触发范本注入。</p>
      ) : null}

      {packs.map((pack, i) => (
        <article key={pack.id || i} className={'sm-card' + (editing === i ? ' is-editing' : '')}>
          {editing === i && draft ? (
            <div className="sm-form">
              <label>
                <span>用哪个场景触发</span>
                <select
                  value={draft.scenario}
                  onChange={(e) => setDraft({ ...draft, scenario: e.target.value })}
                >
                  {scenarios.map((s) => (
                    <option key={s.name} value={s.name}>
                      {s.display}（{s.name}）
                    </option>
                  ))}
                  {scenarios.every((s) => s.name !== draft.scenario) ? (
                    <option value={draft.scenario}>{draft.scenario}（磁盘上没有这个场景）</option>
                  ) : null}
                </select>
              </label>

              <label>
                <span>范本目录</span>
                <input
                  value={draft.dir}
                  onChange={(e) => setDraft({ ...draft, dir: e.target.value })}
                  placeholder="knowledge/你的范本目录"
                />
                <em>数字前缀目录 = 注入槽位；下划线前缀 = 归档，永不注入</em>
              </label>

              <label>
                <span>注入时的标签</span>
                <input
                  value={draft.label}
                  onChange={(e) => setDraft({ ...draft, label: e.target.value })}
                />
                <em>她看到的来源名。只是文案，不影响机制</em>
              </label>

              <label>
                <span>总量预算（字符）</span>
                <NumberField
                  kind="int"
                  value={draft.budget}
                  onCommit={(v) => setDraft({ ...draft, budget: Number(v) || 0 })}
                />
                <em>超了只告警，不截断——字数在蒸馏阶段控制</em>
              </label>

              <label className="sm-check">
                <input
                  type="checkbox"
                  checked={draft.require_marker}
                  onChange={(e) => setDraft({ ...draft, require_marker: e.target.checked })}
                />
                <span>
                  需要她主动「进入」这个模式才生效
                  <em>
                    勾上 = 挂载只是给了权限，还要她判断该进；不勾 = 挂上就生效（适合不需要
                    「进入」动作的场景，比如陪伴）
                  </em>
                </span>
              </label>

              <label className="sm-check">
                <input
                  type="checkbox"
                  checked={draft.allow_groups ?? false}
                  onChange={(e) => setDraft({ ...draft, allow_groups: e.target.checked })}
                />
                <span>
                  群聊也注入
                  <em>
                    默认关。群场景是群级设置：开了之后，群里挂了这个场景时会整篇注入，
                    对群里所有人都生效——所以要不要开由你判断。
                    群聊里不看激活状态，因为「谁激活了」在群里没有对应物
                  </em>
                </span>
              </label>

              <label className="sm-check">
                <input
                  type="checkbox"
                  checked={draft.ttl_seconds === 0}
                  onChange={(e) =>
                    setDraft({ ...draft, ttl_seconds: e.target.checked ? 0 : 6 * HOUR })
                  }
                />
                <span>
                  永久有效
                  <em>不勾就填小时数：激活后过多久自动退出</em>
                </span>
              </label>

              {draft.ttl_seconds > 0 ? (
                <label>
                  <span>有效期（小时）</span>
                  {/* 用共用的 NumberField：`type="number"` 的 DOM 存不下「6.」这种
                      中间态，会让 6.5 小时永远填不进去（见 SettingsFields 的注释）。 */}
                  <NumberField
                    kind="float"
                    value={draft.ttl_seconds / HOUR}
                    onCommit={(v) =>
                      setDraft({
                        ...draft,
                        ttl_seconds: Math.max(1800, Math.round(Number(v) * HOUR)),
                      })
                    }
                  />
                  <em>最少 0.5 小时（1800 秒），可以填 6.5 这种半小时</em>
                </label>
              ) : null}

              <div className="sm-actions">
                <button type="button" className="sm-btn primary" onClick={() => void save()} disabled={busy}>
                  保存
                </button>
                <button
                  type="button"
                  className="sm-btn"
                  onClick={() => {
                    setEditing(null)
                    setDraft(null)
                  }}
                  disabled={busy}
                >
                  取消
                </button>
              </div>
            </div>
          ) : (
            <>
              <div className="sm-card-head">
                <span className="sm-scenario">{scenarioDisplay(pack.scenario)}</span>
                <code>{pack.scenario}</code>
                <button type="button" className="sm-btn" onClick={() => beginEdit(i)}>
                  编辑
                </button>
              </div>

              <dl className="sm-facts">
                <div>
                  <dt>范本</dt>
                  <dd>
                    {pack.dir} · {pack.files ?? 0} 篇 · {(pack.chars ?? 0).toLocaleString()} 字
                    {pack.usage_pct ? <em>用掉预算 {pack.usage_pct}%</em> : null}
                  </dd>
                </div>
                <div>
                  <dt>激活</dt>
                  <dd>{pack.require_marker ? '需要她判断该进这个模式' : '挂载即生效（不需要激活）'}</dd>
                </div>
                <div>
                  <dt>有效期</dt>
                  <dd>{pack.ttl_seconds === 0 ? '永久' : `${pack.ttl_seconds / HOUR} 小时`}</dd>
                </div>
                <div>
                  <dt>群聊</dt>
                  <dd>
                    {pack.allow_groups
                      ? '也注入（挂上即生效，不看激活状态）'
                      : '不注入（只私聊）'}
                  </dd>
                </div>
                <div>
                  <dt>预算</dt>
                  <dd>{pack.budget.toLocaleString()} 字</dd>
                </div>
              </dl>

              {!pack.scenario_known ? (
                <p className="sm-warn">
                  磁盘上没有 name 为 <code>{pack.scenario}</code> 的场景——这个包<strong>永远不会被触发</strong>。
                  要么改场景名，要么在「人格规则 → 场景人格卡」新建。
                </p>
              ) : null}
              {!pack.dir_exists ? (
                <p className="sm-warn">范本目录不存在：{pack.dir}</p>
              ) : null}
              {pack.files === 0 && pack.dir_exists ? (
                <p className="sm-warn">目录在，但一个可注入的文件都没有（数字前缀目录里没有 .md/.txt）</p>
              ) : null}
              {(pack.warnings ?? []).map((w) => (
                <p className="sm-warn" key={w}>
                  {w}
                </p>
              ))}
            </>
          )}
        </article>
      ))}
    </section>
  )
}
