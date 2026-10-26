import {
  useEffect,
  useMemo,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from 'react'
import {
  AlertTriangle,
  Ban,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Eraser,
  Loader2,
  Plus,
  Power,
  RefreshCw,
  Save,
  ShieldOff,
  Sparkles,
  Trash2,
  Undo2,
  Users,
  X,
} from 'lucide-react'
import {
  api,
  type GroupManageData,
  type GroupManageSavePayload,
  type GroupScenario,
} from '../lib/api'
import { DATA_CHANGED } from '../lib/events'
import ContextMenu, { type MenuItem, type MenuState } from '../components/ContextMenu'
import './Page.css'
import './GroupManage.css'

/* ═══════════════════════════════════════════════════════════════
   群管理（场景 + 黑名单 合并页）

   读走 bot（群消息量来自 chat_log）、写走 bridge（只碰 config.yaml），
   所以保存后**必须重启糖糖**才生效——页面按这个事实提示，不假装热更新。

   全部改动先落在本地 state，底部「保存」统一提交；右键菜单只改 state。
   提交时 `groups` 只发**被改动过的群**，避免把没碰过的群配置误覆盖。
   ═══════════════════════════════════════════════════════════════ */

type TargetMode = 'global' | 'group'

type TargetRow = {
  qq: string
  /** 昵称；数据库里查不到就是空串，界面回退成灰色「未知」 */
  name: string
  /** 全局目标存 display，群内定向存 name（后端历史包袱，见 api.ts） */
  scenario: string
}

type GroupRow = {
  id: string
  name: string
  memberCount: number
  messages: number
  lastActive: string
  /** 群默认场景 name；'' = 默认闲聊 */
  scenario: string
  targets: TargetRow[]
}

type EditState = {
  groups: GroupRow[]
  globalTargets: TargetRow[]
  /** blacklist.groups 原样保留，不在列表里的群号也不会丢 */
  blacklist: string[]
  /** 本期不编辑私聊黑名单，但保存时必须原样带回去 */
  privateUsers: string[]
}

type ConfirmState =
  | { kind: 'refresh' }
  | { kind: 'blacklist'; id: string; add: boolean }
  | { kind: 'clearGroup'; id: string }
  | null

/* ── 格式化 ──────────────────────────────────────────────── */

function parseTime(raw: string): number {
  const text = (raw || '').trim()
  if (!text) return 0
  const direct = Date.parse(text)
  if (Number.isFinite(direct)) return direct
  const alt = Date.parse(text.replace(' ', 'T'))
  return Number.isFinite(alt) ? alt : 0
}

/** 相对时间：刚刚 / N分钟前 / N小时前 / N天前；超过 30 天显示日期 */
function fmtRelative(raw: string): string {
  const ms = parseTime(raw)
  if (!ms) return '—'
  const diff = Date.now() - ms
  if (diff < 60_000) return '刚刚'
  const sec = diff / 1000
  if (sec < 3600) return `${Math.floor(sec / 60)}分钟前`
  if (sec < 86400) return `${Math.floor(sec / 3600)}小时前`
  if (sec < 86400 * 30) return `${Math.floor(sec / 86400)}天前`
  const d = new Date(ms)
  const mm = String(d.getMonth() + 1).padStart(2, '0')
  const dd = String(d.getDate()).padStart(2, '0')
  return `${d.getFullYear()}-${mm}-${dd}`
}

/** 消息量：1 万以上用「万」，26.9万条 */
function fmtMessages(n: number): string {
  if (!Number.isFinite(n) || n <= 0) return '0条'
  if (n < 10000) return `${Math.round(n)}条`
  return `${(n / 10000).toFixed(1).replace(/\.0$/, '')}万条`
}

function groupLabel(g: { id: string; name: string }): string {
  return g.name || `群 ${g.id}`
}

/* ── 场景名换算（群内用 name，全局用 display） ─────────────── */

function findScenario(
  scenarios: GroupScenario[],
  raw: string,
): GroupScenario | undefined {
  const value = (raw || '').trim()
  if (!value) return undefined
  return (
    scenarios.find((s) => s.name === value) ??
    scenarios.find((s) => s.display === value)
  )
}

function normGroupScenario(scenarios: GroupScenario[], raw: string): string {
  return findScenario(scenarios, raw)?.name ?? (raw || '')
}

function normGlobalScenario(scenarios: GroupScenario[], raw: string): string {
  return findScenario(scenarios, raw)?.display ?? (raw || '')
}

/* ── 数据 ⇄ 本地状态 ────────────────────────────────────── */

function toEditState(data: GroupManageData): EditState {
  const scenarios = data.scenarios
  return {
    groups: data.groups.map((g) => ({
      id: g.id,
      name: g.name,
      memberCount: g.member_count,
      messages: g.messages,
      lastActive: g.last_active,
      scenario: normGroupScenario(scenarios, g.scenario),
      targets: g.targets.map((t) => ({
        qq: t.qq,
        name: t.name,
        scenario: normGroupScenario(scenarios, t.scenario),
      })),
    })),
    globalTargets: data.global_targets.map((t) => ({
      qq: t.qq,
      name: t.name,
      scenario: normGlobalScenario(scenarios, t.scenario),
    })),
    blacklist: [...data.blacklist.groups],
    privateUsers: [...data.blacklist.private_users],
  }
}

function cloneEdit(state: EditState): EditState {
  return JSON.parse(JSON.stringify(state)) as EditState
}

/* ── 保存 payload ───────────────────────────────────────── */

/** 目标行 → QQ 号映射；空 QQ / 空场景（后端会静默丢掉）在这里先过滤掉 */
function targetMap(rows: TargetRow[]): Record<string, string> {
  const out: Record<string, string> = {}
  for (const row of rows) {
    const qq = row.qq.trim()
    const scenario = row.scenario.trim()
    if (qq && scenario) out[qq] = scenario
  }
  return out
}

/** 固定 key 顺序，比较/提交都稳定（否则键序不同会被误判成有改动） */
function stableTargets(rows: TargetRow[]): Record<string, string> {
  const map = targetMap(rows)
  const out: Record<string, string> = {}
  for (const qq of Object.keys(map).sort()) out[qq] = map[qq]
  return out
}

function buildPayload(edit: EditState, initial: EditState): GroupManageSavePayload {
  const initialGroups = new Map(initial.groups.map((g) => [g.id, g]))
  const initialBlack = new Set(initial.blacklist)
  const black = new Set(edit.blacklist)
  const groups: GroupManageSavePayload['groups'] = {}

  for (const g of edit.groups) {
    const before = initialGroups.get(g.id)
    const isBlack = black.has(g.id)
    /* 黑名单群不该还挂着场景：提交时置空（界面上仍保留显示，另给提醒） */
    const scenarioNow = isBlack ? '' : g.scenario
    const scenarioBefore = before ? before.scenario : ''
    const targetsNow = stableTargets(g.targets)
    const targetsBefore = stableTargets(before ? before.targets : [])
    const scenarioChanged = scenarioNow !== scenarioBefore
    const targetsChanged =
      JSON.stringify(targetsNow) !== JSON.stringify(targetsBefore)
    /* 黑名单进出也算这个群被改动：加入时把场景摘掉；移出时把界面上
       保留的场景恢复回去——否则「保留显示」就成了一句空话。 */
    const blacklistChanged = isBlack !== initialBlack.has(g.id)
    if (!scenarioChanged && !targetsChanged && !blacklistChanged) continue
    groups[g.id] = { scenario: scenarioNow, targets: targetsNow }
  }

  /* blacklist / global_targets 是**整块替换**语义（后端 dict/list 直接覆盖），
     所以必须全量发送——少发一个就等于把它删掉。 */
  return {
    groups,
    blacklist: {
      groups: edit.blacklist.filter((x) => x.trim() !== ''),
      private_users: [...edit.privateUsers],
    },
    global_targets: targetMap(edit.globalTargets),
  }
}

/* ═══════════════════════════════════════════════════════════ */

export default function GroupManage() {
  /** .page 自己就是滚动容器——保存/重启的提示在页面顶部，得把视野带回顶部 */
  const pageRef = useRef<HTMLDivElement>(null)
  const [scenarios, setScenarios] = useState<GroupScenario[]>([])
  const [edit, setEdit] = useState<EditState | null>(null)
  /** 最近一次拉取到的数据：撤销 / 有改动判断 / 差量提交的基准 */
  const [initial, setInitial] = useState<EditState | null>(null)

  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [menu, setMenu] = useState<MenuState>(null)
  const [confirm, setConfirm] = useState<ConfirmState>(null)

  const [saving, setSaving] = useState(false)
  const [saved, setSaved] = useState(false)
  const [restarting, setRestarting] = useState(false)
  const [restarted, setRestarted] = useState(false)

  /** 底部「场景目标」：全局（私聊） / 群内定向 */
  const [mode, setMode] = useState<TargetMode>('global')
  const [selectedGroupId, setSelectedGroupId] = useState('')

  const [addQq, setAddQq] = useState('')
  const [addScenario, setAddScenario] = useState('')
  const [addError, setAddError] = useState('')

  const [batchOpen, setBatchOpen] = useState(false)
  const [batchText, setBatchText] = useState('')
  const [batchIssues, setBatchIssues] = useState<string[]>([])
  const [batchTip, setBatchTip] = useState('')

  const load = () => {
    setLoading(true)
    setError(null)
    setConfirm(null)
    api
      .consoleGroups()
      .then((data) => {
        const next = toEditState(data)
        setScenarios(data.scenarios)
        setEdit(next)
        setInitial(cloneEdit(next))
        setSelectedGroupId((prev) =>
          next.groups.some((g) => g.id === prev) ? prev : (next.groups[0]?.id ?? ''),
        )
        setAddScenario(data.scenarios[0]?.display ?? '')
        setAddError('')
        setBatchTip('')
        setBatchIssues([])
      })
      .catch((e: Error) => setError(e.message))
      .finally(() => setLoading(false))
  }

  /* `dirty` 定义在 effect 之后，所以用 ref 把「当前是否有未保存改动」传进去
   * （effect 只挂一次，闭包会拿到旧的 dirty）。 */
  const dirtyRef = useRef(false)

  /* 在别处（「人格规则」）改了场景卡的名字/内容 → 这里的右键菜单必须跟着变。
   * 主人 2026-10-26：「改了场景名字，群列表右键显示的就应该是改后的场景名字，
   * 这种低级错误不能有。」——`scenarios` 是挂载时的快照，不重拉就一直是旧的。 */
  useEffect(() => {
    load()
    const onDataChanged = () => {
      // 本地有未保存改动时不自动重拉（会把用户正在改的东西冲掉）；
      // 只提示一句，让他自己决定先存还是先刷。
      if (dirtyRef.current) {
        setError('场景定义在别处改过了。先保存或放弃当前改动，再重新加载。')
        return
      }
      load()
    }
    window.addEventListener(DATA_CHANGED, onDataChanged)
    return () => window.removeEventListener(DATA_CHANGED, onDataChanged)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  const blackSet = useMemo(() => new Set(edit?.blacklist ?? []), [edit])

  const dirty = useMemo(() => {
    if (!edit || !initial) return false
    return JSON.stringify(edit) !== JSON.stringify(initial)
  }, [edit, initial])

  dirtyRef.current = dirty

  const selectedGroup = useMemo(
    () => edit?.groups.find((g) => g.id === selectedGroupId) ?? null,
    [edit, selectedGroupId],
  )

  const activeTargets: TargetRow[] =
    mode === 'global' ? (edit?.globalTargets ?? []) : (selectedGroup?.targets ?? [])

  const confirmGroup =
    confirm && confirm.kind !== 'refresh'
      ? (edit?.groups.find((g) => g.id === confirm.id) ?? null)
      : null

  const confirmText = (() => {
    if (!confirm) return ''
    if (confirm.kind === 'refresh') return '刷新会丢弃当前还没保存的改动，确定要刷新吗？'
    const label = confirmGroup ? groupLabel(confirmGroup) : `群 ${confirm.id}`
    if (confirm.kind === 'blacklist') {
      return confirm.add
        ? `把「${label}」加入黑名单？加入后糖糖不再回应这个群的消息，保存时会清空该群默认场景。`
        : `把「${label}」移出黑名单？保存后糖糖会重新回应这个群。`
    }
    return `清除「${label}」的群配置？会清空群默认场景和群内定向目标（黑名单状态不变）。`
  })()

  const confirmAction = (() => {
    if (!confirm) return '确认'
    if (confirm.kind === 'refresh') return '确认刷新'
    if (confirm.kind === 'blacklist') return confirm.add ? '确认加入' : '确认移出'
    return '清除配置'
  })()

  const scenarioDisplay = (name: string): string => {
    if (!name) return '默认闲聊'
    return scenarios.find((s) => s.name === name)?.display ?? name
  }

  const patchGroup = (id: string, fn: (g: GroupRow) => GroupRow) => {
    setEdit((prev) => {
      if (!prev) return prev
      return { ...prev, groups: prev.groups.map((g) => (g.id === id ? fn(g) : g)) }
    })
  }

  const firstScenarioValue = (target: TargetMode): string => {
    const first = scenarios[0]
    if (!first) return ''
    return target === 'global' ? first.display : first.name
  }

  const switchMode = (next: TargetMode) => {
    if (next === mode) return
    setMode(next)
    setAddError('')
    setBatchTip('')
    setBatchIssues([])
    setAddScenario(firstScenarioValue(next))
  }

  /* ── 右键菜单 ───────────────────────────────────────── */

  const openGroupMenu = (e: ReactMouseEvent, g: GroupRow) => {
    e.preventDefault()
    const isBlack = blackSet.has(g.id)
    const items: MenuItem[] = [
      {
        key: 'scenario-head',
        label: '设置场景',
        icon: Sparkles,
        disabled: true,
        onSelect: () => undefined,
      },
      {
        key: 'scenario-none',
        label: `${g.scenario === '' ? '✓ ' : ''}默认闲聊`,
        onSelect: () => patchGroup(g.id, (x) => ({ ...x, scenario: '' })),
      },
    ]

    for (const s of scenarios) {
      items.push({
        key: `scenario:${s.name}`,
        label: `${g.scenario === s.name ? '✓ ' : ''}${s.display}`,
        onSelect: () => patchGroup(g.id, (x) => ({ ...x, scenario: s.name })),
      })
    }

    items.push({
      key: 'blacklist',
      label: isBlack ? '移出黑名单' : '加入黑名单',
      icon: isBlack ? ShieldOff : Ban,
      danger: !isBlack,
      separator: true,
      onSelect: () => setConfirm({ kind: 'blacklist', id: g.id, add: !isBlack }),
    })

    items.push({
      key: 'clear-group',
      label: '清除该群配置',
      icon: Eraser,
      danger: true,
      separator: true,
      disabled: g.scenario === '' && g.targets.length === 0,
      onSelect: () => setConfirm({ kind: 'clearGroup', id: g.id }),
    })

    setMenu({ x: e.clientX, y: e.clientY, items })
  }

  const applyConfirm = () => {
    if (!confirm) return
    if (confirm.kind === 'refresh') {
      setConfirm(null)
      load()
      return
    }
    if (confirm.kind === 'blacklist') {
      const { id, add } = confirm
      setEdit((prev) => {
        if (!prev) return prev
        const has = prev.blacklist.includes(id)
        if (add && !has) return { ...prev, blacklist: [...prev.blacklist, id] }
        if (!add && has) {
          return { ...prev, blacklist: prev.blacklist.filter((x) => x !== id) }
        }
        return prev
      })
      setConfirm(null)
      return
    }
    patchGroup(confirm.id, (g) => ({ ...g, scenario: '', targets: [] }))
    setConfirm(null)
  }

  /* ── 保存 / 撤销 / 重启 ─────────────────────────────── */

  const scrollToTop = () => {
    pageRef.current?.scrollTo({ top: 0, behavior: 'smooth' })
  }

  const save = async () => {
    if (!edit || !initial || saving) return
    setSaving(true)
    setError(null)
    try {
      await api.groupManageSave(buildPayload(edit, initial))
      setInitial(cloneEdit(edit))
      setSaved(true)
      setRestarted(false)
      scrollToTop()
    } catch (e) {
      setError(`保存失败：${(e as Error).message}`)
      scrollToTop()
    } finally {
      setSaving(false)
    }
  }

  const undoAll = () => {
    if (!initial) return
    setEdit(cloneEdit(initial))
    setConfirm(null)
    setAddError('')
    setBatchIssues([])
    setBatchTip('')
  }

  const restartBot = async () => {
    if (restarting) return
    setRestarting(true)
    setError(null)
    try {
      await api.botStop()
      await new Promise<void>((r) => setTimeout(r, 3000))
      await api.botStart()
      setRestarted(true)
    } catch (e) {
      setError(`重启失败：${(e as Error).message}`)
      scrollToTop()
    } finally {
      setRestarting(false)
    }
  }

  /* ── 目标行编辑 ─────────────────────────────────────── */

  const addTarget = () => {
    if (!edit) return
    const qq = addQq.trim()
    if (!qq) {
      setAddError('请输入 QQ 号')
      return
    }
    if (!/^\d+$/.test(qq)) {
      setAddError('QQ 号只能是数字')
      return
    }
    if (!addScenario) {
      setAddError('糖糖还没加载到可用场景，确认她已经启动')
      return
    }
    const list = mode === 'global' ? edit.globalTargets : (selectedGroup?.targets ?? [])
    if (list.some((t) => t.qq === qq)) {
      setAddError(`QQ ${qq} 已经在列表里了，直接改那一行就好`)
      return
    }
    const row: TargetRow = { qq, name: '', scenario: addScenario }
    if (mode === 'global') {
      setEdit((prev) =>
        prev ? { ...prev, globalTargets: [...prev.globalTargets, row] } : prev,
      )
    } else if (selectedGroupId) {
      patchGroup(selectedGroupId, (g) => ({ ...g, targets: [...g.targets, row] }))
    } else {
      setAddError('先选择一个群')
      return
    }
    setAddQq('')
    setAddError('')
  }

  const changeTargetScenario = (index: number, value: string) => {
    if (mode === 'global') {
      setEdit((prev) =>
        prev
          ? {
              ...prev,
              globalTargets: prev.globalTargets.map((t, i) =>
                i === index ? { ...t, scenario: value } : t,
              ),
            }
          : prev,
      )
      return
    }
    if (!selectedGroupId) return
    patchGroup(selectedGroupId, (g) => ({
      ...g,
      targets: g.targets.map((t, i) => (i === index ? { ...t, scenario: value } : t)),
    }))
  }

  const removeTarget = (index: number) => {
    if (mode === 'global') {
      setEdit((prev) =>
        prev
          ? { ...prev, globalTargets: prev.globalTargets.filter((_, i) => i !== index) }
          : prev,
      )
      return
    }
    if (!selectedGroupId) return
    patchGroup(selectedGroupId, (g) => ({
      ...g,
      targets: g.targets.filter((_, i) => i !== index),
    }))
  }

  /** 场景下拉：全局用 display 作值，群内用 name 作值；清单外的值保留成一项 */
  const scenarioOptions = (current: string, target: TargetMode) => {
    const known =
      target === 'global' ? scenarios.map((s) => s.display) : scenarios.map((s) => s.name)
    return (
      <>
        {scenarios.length === 0 && <option value="">还没有可用场景</option>}
        {scenarios.map((s) => (
          <option key={s.name} value={target === 'global' ? s.display : s.name}>
            {s.display}
          </option>
        ))}
        {current && !known.includes(current) && (
          <option value={current}>{current}（未在场景清单里）</option>
        )}
      </>
    )
  }

  /* ── 批量粘贴 ───────────────────────────────────────── */

  const parseBatch = () => {
    if (!edit) return
    if ((mode === 'group' && !selectedGroup) || (mode === 'group' && !selectedGroupId)) {
      setBatchTip('先选择一个群再合并')
      return
    }
    const list = mode === 'global' ? edit.globalTargets : (selectedGroup?.targets ?? [])
    const issues: string[] = []
    const parsed: TargetRow[] = []

    batchText.split(/\r?\n/).forEach((line, idx) => {
      const text = line.trim()
      if (!text) return
      const match = /^(\d+)\s+(.+)$/.exec(text)
      if (!match) {
        const first = text.split(/\s+/)[0] ?? ''
        issues.push(
          /^\d+$/.test(first)
            ? `第 ${idx + 1} 行：缺少场景名（格式：QQ号 场景名）`
            : `第 ${idx + 1} 行：开头不是 QQ 号`,
        )
        return
      }
      const qq = match[1]
      const token = match[2].trim()
      const scenario = findScenario(scenarios, token)
      if (!scenario) {
        issues.push(`第 ${idx + 1} 行：场景「${token}」不存在`)
        return
      }
      parsed.push({
        qq,
        name: '',
        scenario: mode === 'global' ? scenario.display : scenario.name,
      })
    })

    if (parsed.length === 0) {
      setBatchIssues(issues)
      setBatchTip(issues.length ? '没有可合并的行，先把下面的问题改掉' : '还没有粘贴内容')
      return
    }

    const mergedMap = new Map(list.map((t) => [t.qq, t]))
    let added = 0
    let updated = 0
    for (const row of parsed) {
      const old = mergedMap.get(row.qq)
      if (old) {
        updated += 1
        mergedMap.set(row.qq, { ...old, scenario: row.scenario })
      } else {
        added += 1
        mergedMap.set(row.qq, row)
      }
    }
    const merged = [...mergedMap.values()]
    if (mode === 'global') {
      setEdit((prev) => (prev ? { ...prev, globalTargets: merged } : prev))
    } else if (selectedGroupId) {
      patchGroup(selectedGroupId, (g) => ({ ...g, targets: merged }))
    }

    setBatchIssues(issues)
    setBatchTip(
      `已合并 ${parsed.length} 条：新增 ${added}，覆盖 ${updated}` +
        (issues.length ? `；有 ${issues.length} 行没识别` : ''),
    )
    if (issues.length === 0) setBatchText('')
  }

  const refresh = () => {
    if (loading || saving) return
    if (dirty) {
      setConfirm({ kind: 'refresh' })
      return
    }
    load()
  }

  /* ── 渲染 ───────────────────────────────────────────── */

  return (
    <div className="page gm-page" ref={pageRef}>
      <div className="page-head">
        <div>
          <h1 className="page-title">群管理</h1>
          <p className="page-sub">
            糖糖加入的群会自动出现。右键群可以设置场景或加入黑名单。
          </p>
        </div>
        <div className="page-actions">
          <button
            className="btn btn-ghost"
            onClick={refresh}
            disabled={loading || saving}
            title="重新读取群列表"
          >
            <RefreshCw size={14} className={loading ? 'spin' : ''} />
            刷新
          </button>
        </div>
      </div>

      {saved && (
        <div className="notice gm-saved">
          <CheckCircle2 size={15} className="gm-saved-icon" />
          <span className="gm-saved-text">
            已保存到 config.yaml · 需要重启糖糖才生效
          </span>
          <button
            className="btn btn-sm btn-ghost gm-restart"
            onClick={() => void restartBot()}
            disabled={restarting || restarted}
          >
            {restarting ? (
              <Loader2 size={13} className="spin" />
            ) : restarted ? (
              <CheckCircle2 size={13} />
            ) : (
              <Power size={13} />
            )}
            {restarting ? '重启中…' : restarted ? '糖糖已重启' : '立即重启'}
          </button>
        </div>
      )}

      {error && (
        <div className="notice notice-error">
          <AlertTriangle size={15} />
          <span className="gm-error-text">{error}</span>
          {!edit ? (
            <button className="btn btn-sm btn-ghost gm-retry" onClick={load} disabled={loading}>
              {loading ? (
                <Loader2 size={13} className="spin" />
              ) : (
                <RefreshCw size={13} />
              )}
              重试
            </button>
          ) : (
            <button
              className="icon-btn gm-error-close"
              title="关闭"
              onClick={() => setError(null)}
            >
              <X size={14} />
            </button>
          )}
          {!edit && (
            <div className="notice-hint">
              需要糖糖本体在运行：群消息量只有她能读到。
            </div>
          )}
        </div>
      )}

      {confirm && (
        <div className="notice gm-confirm">
          <AlertTriangle size={15} className="gm-confirm-icon" />
          <span className="gm-confirm-text">{confirmText}</span>
          <div className="gm-confirm-actions">
            <button className="btn btn-sm btn-ghost" onClick={() => setConfirm(null)}>
              取消
            </button>
            <button
              className={
                'btn btn-sm ' + (confirm.kind === 'refresh' ? 'btn-primary' : 'gm-btn-danger')
              }
              onClick={applyConfirm}
            >
              {confirmAction}
            </button>
          </div>
        </div>
      )}

      {loading && !edit ? (
        <div className="panel">
          <div className="gm-loading">
            <Loader2 size={14} className="spin" />
            正在读取…
          </div>
          {[0, 1, 2, 3].map((i) => (
            <div className="gm-skeleton-row" key={i} />
          ))}
        </div>
      ) : edit ? (
        <>
          <div className="section-head">
            <span className="section-title">群列表</span>
            <span className="gm-count">共 {edit.groups.length} 个群</span>
          </div>

          {edit.groups.length === 0 ? (
            <div className="panel">
              <div className="gm-empty">
                <Users size={22} />
                <span>还没有发现任何群</span>
                <span className="gm-empty-hint">
                  糖糖在某个群里收到消息后，这里会自动出现。
                </span>
              </div>
            </div>
          ) : (
            <div className="panel gm-list-panel">
              {edit.groups.map((g) => {
                const isBlack = blackSet.has(g.id)
                const isConfirming =
                  confirm !== null && confirm.kind !== 'refresh' && confirm.id === g.id
                return (
                  <div
                    key={g.id}
                    className={'gm-group' + (isConfirming ? ' is-confirming' : '')}
                    onContextMenu={(e) => openGroupMenu(e, g)}
                    title="右键设置场景 / 黑名单"
                  >
                    <div className="gm-group-main">
                      <div className="gm-group-title">
                        <span className="gm-group-name">{groupLabel(g)}</span>
                        {isBlack && <span className="gm-badge is-danger">已屏蔽</span>}
                        <span
                          className={'gm-badge ' + (g.scenario ? 'is-scenario' : 'is-muted')}
                          title={
                            isBlack
                              ? '该群在黑名单中，保存时会清空这个场景'
                              : g.scenario
                                ? `场景：${scenarioDisplay(g.scenario)}`
                                : '没有单独设置场景，按默认闲聊处理'
                          }
                        >
                          {g.scenario ? scenarioDisplay(g.scenario) : '默认闲聊'}
                        </span>
                        {g.targets.length > 0 && (
                          <span className="gm-badge is-muted">
                            定向 {g.targets.length}
                          </span>
                        )}
                      </div>
                      <div className="gm-group-meta">
                        群号 {g.id} · 成员 {g.memberCount} · {fmtMessages(g.messages)} ·
                        最后活跃 {fmtRelative(g.lastActive)}
                      </div>
                      {isBlack && g.scenario !== '' && (
                        <div className="gm-group-warn">
                          该群在黑名单中，保存时会清空群场景
                        </div>
                      )}
                    </div>
                    <span className="gm-group-hint">右键操作</span>
                  </div>
                )
              })}
            </div>
          )}

          <div className="gm-targets">
            <div className="section-head">
              <span className="section-title">场景目标</span>
            </div>
            <p className="gm-desc explain-only">
              给单个 QQ 号指定场景，优先级高于群默认。全局那组用于私聊。
            </p>

            <div className="gm-tabs">
              <button
                className={'gm-tab' + (mode === 'global' ? ' is-active' : '')}
                onClick={() => switchMode('global')}
              >
                全局（私聊）
                <span className="gm-tab-count">{edit.globalTargets.length}</span>
              </button>
              <button
                className={'gm-tab' + (mode === 'group' ? ' is-active' : '')}
                onClick={() => switchMode('group')}
              >
                群内定向
                <span className="gm-tab-count">{selectedGroup?.targets.length ?? 0}</span>
              </button>
            </div>

            {mode === 'group' &&
              (edit.groups.length === 0 ? (
                <div className="gm-inline-warn">还没有发现任何群，无法设置群内定向。</div>
              ) : (
                <div className="gm-group-picker">
                  <label htmlFor="gm-group-picker">选择群</label>
                  <select
                    id="gm-group-picker"
                    value={selectedGroupId}
                    onChange={(e) => setSelectedGroupId(e.target.value)}
                  >
                    {edit.groups.map((g) => (
                      <option key={g.id} value={g.id}>
                        {groupLabel(g)}
                        {blackSet.has(g.id) ? '（黑名单）' : ''}
                      </option>
                    ))}
                  </select>
                  {selectedGroup && (
                    <span className="gm-picker-hint">
                      群默认场景：
                      {selectedGroup.scenario
                        ? scenarioDisplay(selectedGroup.scenario)
                        : '默认闲聊'}
                    </span>
                  )}
                  {selectedGroup && blackSet.has(selectedGroup.id) && (
                    <span className="gm-inline-warn">该群在黑名单中，群内定向不会生效</span>
                  )}
                </div>
              ))}

            <div className="panel gm-target-panel">
              {activeTargets.length === 0 ? (
                <div className="gm-target-empty">
                  {mode === 'global'
                    ? '还没有全局（私聊）目标'
                    : selectedGroup
                      ? '这个群还没有定向目标'
                      : '先选择一个群'}
                </div>
              ) : (
                <div className="gm-target-list">
                  {activeTargets.map((t, index) => (
                    <div className="gm-target-row" key={t.qq}>
                      <span className="gm-qq" title={t.qq}>
                        {t.qq}
                      </span>
                      <span
                        className={'gm-target-name' + (t.name ? '' : ' is-unknown')}
                        title={t.name || '数据库里还没有这个人的昵称'}
                      >
                        {t.name || '未知'}
                      </span>
                      <select
                        value={t.scenario}
                        onChange={(e) => changeTargetScenario(index, e.target.value)}
                        aria-label={`QQ ${t.qq} 的场景`}
                      >
                        {scenarioOptions(t.scenario, mode)}
                      </select>
                      <button
                        className="icon-btn gm-target-del"
                        title="删除这个目标"
                        onClick={() => removeTarget(index)}
                      >
                        <Trash2 size={14} />
                      </button>
                    </div>
                  ))}
                </div>
              )}

              <div className="gm-add-row">
                <input
                  value={addQq}
                  inputMode="numeric"
                  placeholder="QQ 号"
                  aria-label="要添加的 QQ 号"
                  onChange={(e) => {
                    setAddQq(e.target.value.replace(/\D/g, ''))
                    setAddError('')
                  }}
                  onKeyDown={(e) => {
                    if (e.key === 'Enter') addTarget()
                  }}
                />
                <select
                  value={addScenario}
                  onChange={(e) => {
                    setAddScenario(e.target.value)
                    setAddError('')
                  }}
                  disabled={scenarios.length === 0}
                  aria-label="要给这个 QQ 号指定的场景"
                >
                  {scenarioOptions(addScenario, mode)}
                </select>
                <button
                  className="btn btn-sm btn-ghost"
                  onClick={addTarget}
                  disabled={
                    scenarios.length === 0 || (mode === 'group' && !selectedGroup)
                  }
                >
                  <Plus size={13} />
                  添加
                </button>
              </div>
              {addError && (
                <div className="gm-target-error">
                  <AlertTriangle size={13} />
                  {addError}
                </div>
              )}
            </div>

            <button
              className={'gm-batch-toggle' + (batchOpen ? ' is-open' : '')}
              onClick={() => setBatchOpen((v) => !v)}
            >
              {batchOpen ? <ChevronUp size={13} /> : <ChevronDown size={13} />}
              批量粘贴
            </button>

            {batchOpen && (
              <div className="panel gm-batch">
                <p className="gm-batch-hint">
                  一行一个「QQ号 场景名」，用空格分隔；场景名写 name 或显示名都可以。
                </p>
                <textarea
                  value={batchText}
                  onChange={(e) => setBatchText(e.target.value)}
                  rows={5}
                  spellCheck={false}
                  placeholder={'123456789 亲密模式\n987654321 心理陪伴'}
                />
                <div className="gm-batch-actions">
                  <button
                    className="btn btn-sm btn-primary"
                    onClick={parseBatch}
                    disabled={mode === 'group' && !selectedGroup}
                  >
                    解析并合并
                  </button>
                  <button
                    className="btn btn-sm btn-ghost"
                    onClick={() => {
                      setBatchText('')
                      setBatchIssues([])
                      setBatchTip('')
                    }}
                  >
                    清空
                  </button>
                  {batchTip && <span className="gm-batch-tip">{batchTip}</span>}
                </div>
                {batchIssues.length > 0 && (
                  <ul className="gm-issues">
                    {batchIssues.map((issue, i) => (
                      <li key={`${i}-${issue}`}>
                        <AlertTriangle size={12} />
                        <span>{issue}</span>
                      </li>
                    ))}
                  </ul>
                )}
              </div>
            )}
          </div>

          <div className="gm-spacer" />

          <div className="gm-footer">
            <span className="gm-footer-info">
              共 {edit.groups.length} 个群 · {scenarios.length} 个场景 · 黑名单{' '}
              {edit.blacklist.length} 个群
            </span>
            <div className="gm-footer-actions">
              <button
                className="btn btn-ghost"
                onClick={undoAll}
                disabled={!dirty || saving}
              >
                <Undo2 size={14} />
                撤销所有改动
              </button>
              {dirty && (
                <span className="gm-unsaved">
                  <span className="gm-dirty-dot" />
                  未保存
                </span>
              )}
              <button
                className="btn btn-primary"
                onClick={() => void save()}
                disabled={!dirty || saving}
              >
                {saving ? (
                  <Loader2 size={14} className="spin" />
                ) : (
                  <Save size={14} />
                )}
                {saving ? '保存中…' : '保存'}
              </button>
            </div>
          </div>
        </>
      ) : null}

      <ContextMenu state={menu} onClose={() => setMenu(null)} />
    </div>
  )
}
