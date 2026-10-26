/**
 * 资产编辑器（2026-10-26 第 49 轮）——「参数完全暴露，自由度拉满」的入口。
 *
 * ## 为什么是「统一文件树 + 文本编辑器」而不是五个定制表单
 *
 * 主人要的是**自由度**：人格卡、协议条文、场景规则、恋爱军师、亲密范本——
 * 五类资产全是文本。给每类做一个「结构化表单」看着高级，实际是**限制**：
 * 表单里没列的字段就改不了，而场景 YAML 的结构本来就会演化。
 *
 * 所以这里只有一种交互：**左边选文件，右边改文本，保存**。
 * 结构化提示放在别处（`apply_hint` 告诉你改完怎么生效，`desc` 告诉你这是什么）。
 *
 * ## 判据不在这里（教训 #38）
 *
 * 「这个路径能不能改」「YAML 合法吗」「备份有没有」全在后端
 * `tools/asset_edit.py`（26 条闸门盯着）。本页**不做任何路径判断**，
 * 错误原样显示——前端再判断一次就有两个实现点，迟早漂移。
 *
 * ## 四条界面纪律
 *
 * 1. **改完要重启就明说**：`apply_hint` 常驻顶栏，不藏在 tooltip 里。
 * 2. **保存后告诉你备份在哪**：不然「我改坏了想回去」就没有抓手。
 * 3. **未保存的改动要看得见**：标题旁一个点 + 离开文件时确认。
 * 4. **空/失败都有话说**：不出现「一片空白不知道发生了什么」。
 */
import { Fragment, useCallback, useEffect, useMemo, useRef, useState } from 'react'
import type { ReactNode } from 'react'
import {
  AlertTriangle,
  Check,
  ChevronRight,
  FileText,
  Folder,
  History,
  LoaderCircle,
  RotateCcw,
  Save,
  Undo2,
  WrapText,
} from 'lucide-react'
import { api } from '../lib/api'
import { notifyDataChanged } from '../lib/events'
import type {
  AssetList,
  AssetContent,
  AssetSpec,
  AssetFile,
  AssetDir,
  KnowledgeStatus,
  PromptGroups,
} from '../lib/api'
import { fmtTokens } from '../lib/format'
import './Assets.css'

type Notice = { kind: 'ok' | 'warn' | 'error'; text: string } | null

/** 极简 Markdown → HTML（只支持协议文本里真实出现的几种写法）。
 *
 * 为什么自己写二十行而不是装库：协议文本的结构只有标题/列表/加粗/行内代码四种，
 * 引一个 markdown 库会带进一堆用不上的解析（表格、脚注、嵌入 HTML），
 * 而**嵌入 HTML 恰恰是这里不想要的**——协议文本里如果有 `<` 应该原样显示。
 *
 * 先 escape 再替换标记：顺序反了就等于把内容当 HTML 执行。
 */
function renderMarkdown(src: string): string {
  const esc = (t: string) => t.replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
  const inline = (t: string) =>
    esc(t).replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>').replace(/`([^`]+)`/g, '<code>$1</code>')
  const out: string[] = []
  let inList = false
  const closeList = () => {
    if (inList) {
      out.push('</ul>')
      inList = false
    }
  }
  for (const raw of src.split('\n')) {
    const t = raw.trim()
    if (!t) {
      closeList()
      continue
    }
    if (t.startsWith('### ')) {
      closeList()
      out.push(`<h4>${inline(t.slice(4))}</h4>`)
    } else if (t.startsWith('## ')) {
      closeList()
      out.push(`<h3>${inline(t.slice(3))}</h3>`)
    } else if (t.startsWith('# ')) {
      closeList()
      out.push(`<h2>${inline(t.slice(2))}</h2>`)
    } else if (/^[·\-*]\s/.test(t) || /^\d+\.\s/.test(t)) {
      if (!inList) {
        out.push('<ul>')
        inList = true
      }
      out.push(`<li>${inline(t.replace(/^[·\-*]\s/, '').replace(/^\d+\.\s/, ''))}</li>`)
    } else {
      closeList()
      out.push(`<p>${inline(t)}</p>`)
    }
  }
  closeList()
  return out.join('')
}

export default function Assets() {
  const [list, setList] = useState<AssetList | null>(null)
  const [listError, setListError] = useState('')
  const [selected, setSelected] = useState('')
  const [asset, setAsset] = useState<AssetContent | null>(null)
  const [draft, setDraft] = useState('')
  const [loading, setLoading] = useState(false)
  const [saving, setSaving] = useState(false)
  const [notice, setNotice] = useState<Notice>(null)
  const [showBackups, setShowBackups] = useState(false)
  /** 结构管理（B5）：正在哪一类下新建什么。base = 建在哪个目录里。 */
  const [creating, setCreating] = useState<{
    specKey: string
    kind: 'file' | 'dir'
    base: string
    exts: string[]
    hint: string
  } | null>(null)
  const [newName, setNewName] = useState('')
  const [busy, setBusy] = useState('')
  const taRef = useRef<HTMLTextAreaElement>(null)
  const gutterRef = useRef<HTMLPreElement>(null)

  const dirty = !!asset && draft !== asset.content

  const loadList = useCallback(async () => {
    try {
      setList(await api.assets())
      setListError('')
    } catch (e) {
      setListError((e as Error).message)
    }
  }, [])

  useEffect(() => {
    void loadList()
  }, [loadList])

  /** 打开一个文件。有未保存改动时先问一句（浏览器 confirm 够用，不引组件）。 */
  const openFile = useCallback(
    async (path: string) => {
      if (dirty && !window.confirm('当前文件有未保存的修改，确定要切走吗？')) return
      setSelected(path)
      setLoading(true)
      setNotice(null)
      setShowBackups(false)
      try {
        const a = await api.assetRead(path)
        setAsset(a)
        setDraft(a.content)
      } catch (e) {
        setAsset(null)
        setNotice({ kind: 'error', text: (e as Error).message })
      } finally {
        setLoading(false)
      }
    },
    [dirty],
  )

  const save = useCallback(async () => {
    if (!asset || !dirty || saving) return
    setSaving(true)
    setNotice(null)
    try {
      const r = await api.assetWrite(asset.path, draft)
      setAsset({ ...asset, content: draft, size: r.size })
      // 备份列表要跟着刷新：保存前那份「改前版本」就是刚生成的。
      // 只补 backups 字段（不重设 content）——保存是异步的，这期间用户可能又敲了字。
      void api
        .assetRead(asset.path)
        .then((a) => setAsset((prev) => (prev ? { ...prev, backups: a.backups } : prev)))
        .catch(() => undefined)
      const parts = ['已保存']
      if (r.backup) parts.push(`改前版本备份在 _backups/${r.backup}`)
      parts.push(r.apply_hint)
      // 场景卡改了 display 名 → 挂载点已跟着改。**必须说出来**：
      // 不说的话，用户无法判断「改完名字场景还生不生效」，
      // 而这一步是静默失效的高发区（scenario.py 用 display 名匹配）。
      const renamed = r.renamed_mounts ?? []
      if (renamed.length) {
        parts.push(`已把 ${renamed.length} 个挂载点改到新名字（${renamed.join('、')}）`)
      }
      setNotice({
        kind: r.warnings.length ? 'warn' : 'ok',
        text: [parts.join(' · '), ...r.warnings].join('；'),
      })
      void loadList()
      notifyDataChanged()
    } catch (e) {
      setNotice({ kind: 'error', text: (e as Error).message })
    } finally {
      setSaving(false)
    }
  }, [asset, dirty, draft, saving, loadList])

  // Ctrl/⌘+S 保存：编辑器里这是肌肉记忆，不接反而别扭
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 's') {
        e.preventDefault()
        void save()
      }
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [save])

  const doRestore = useCallback(
    async (backupPath: string) => {
      if (!asset) return
      if (!window.confirm('用这个备份覆盖当前内容？（当前版本也会先备份一次）')) return
      setShowBackups(false)
      try {
        await api.assetRestore(asset.path, backupPath)
        const a = await api.assetRead(asset.path)
        setAsset(a)
        setDraft(a.content)
        setNotice({ kind: 'ok', text: `已从备份恢复。${a.apply_hint}` })
      } catch (e) {
        setNotice({ kind: 'error', text: (e as Error).message })
      }
    },
    [asset],
  )

  const doReset = useCallback(async () => {
    if (!asset) return
    if (!window.confirm('删除这个覆盖文件、回落到代码内置默认值？')) return
    try {
      const r = await api.assetReset(asset.path)
      setNotice({ kind: 'ok', text: r.apply_hint })
      void loadList()
      notifyDataChanged()
      if (r.deleted) {
        setAsset(null)
        setDraft('')
        setSelected('')
      }
    } catch (e) {
      setNotice({ kind: 'error', text: (e as Error).message })
    }
  }, [asset, loadList])

  const lines = useMemo(() => draft.split('\n').length, [draft])

  useEffect(() => {
    // 只在进页面时取一次：它是「文件 + 索引库」的快照，不跟编辑实时变
    // （改完要重载才生效，提示语在状态条里说）
    api.knowledgeStatus().then(setKStatus).catch(() => setKStatus(null))
  }, [])

  /** 协议条文：覆盖状态 + 一句话作用 + 所属分组（都来自后端，前端不判）
   *
   * `known` = 文件头的 `group` 在组表里存在吗；`stale` = 文件头还残留着
   * 已失效的 `group_title`。两个都是**要说出来**的静默失效（教训 #53 一族）。 */
  const promptMeta = useMemo(() => {
    const m = new Map<
      string,
      {
        active: boolean
        desc: string
        group: string
        title: string
        known: boolean
        stale: boolean
        origin: string
      }
    >()
    list?.prompt_overrides?.forEach((o) =>
      m.set(o.name, {
        active: o.active,
        desc: o.desc,
        group: o.group,
        title: o.group_title,
        known: o.group_known,
        stale: o.stale_group_title,
        origin: o.origin,
      }),
    )
    return m
  }, [list])

  /** 协议分组表（顺序 + 标题 + 每组条数），唯一真相是 `prompts/_groups.yaml`。
   *
   * 2026-10-26 之前组标题写在**每条协议的文件头**里：同组 6 条就有 6 份副本，
   * 改哪份生效取决于遍历顺序（改了不生效），两条撞名还会被合并成一组。
   * 现在组标题只有一份，界面双击组头改的就是它。 */
  const [promptGroups, setPromptGroups] = useState<PromptGroups | null>(null)
  const refreshGroups = useCallback(() => {
    api
      .promptGroups()
      .then(setPromptGroups)
      .catch(() => setPromptGroups(null))
  }, [])
  useEffect(() => {
    // 跟着 `list` 一起刷：新建/删除协议后组头的条目数要变（list 变了就重拉一次）
    refreshGroups()
  }, [refreshGroups, list])

  /** 编辑器永远自动换行，另给一个「预览」。
   *
   * 主人 2026-10-26：「换行不应该单独加一个按钮，它默认就应该加一个预览」。
   * 对——不换行本来就是缺陷，不该做成选项。预览看的是**渲染后的结构**
   * （标题/列表/加粗），那才是编辑协议时真正需要判断的东西。
   * 代价：行号栏去掉（行号是逻辑行，折行后视觉行对不上，显示错位数字更糟）。 */
  const [preview, setPreview] = useState(false)

  /** 正在改名的文件路径 + 输入框里的新名字（行内输入，不用 window.prompt）。
   *
   * `kind: 'group'` 时 `path` 存的是**组 key**（组表里的一行），不是文件路径。 */
  const [renaming, setRenaming] = useState<
    { path: string; stem: string; kind: 'file' | 'dir' | 'group' } | null
  >(null)
  const [renameDraft, setRenameDraft] = useState('')

  /** 知识库两条通道的现状（检索 56/56、直投 9 篇…），进页面取一次。 */
  const [kStatus, setKStatus] = useState<KnowledgeStatus | null>(null)

  /** 「检索命中」这类默认收起。
   *
   * 主人 2026-10-26 的原话：「恋爱军师是以蒸馏的方式进入上下文的，
   * 没必要把相关文档放进去」。她说得对——那 44 篇原文她基本不读
   * （`protocols.py:1372-1378` 的注释写着 search_knowledge 曾 8 天零调用），
   * 摊在界面上只会让人误以为「改这里就能改变她的话术」。
   * 收起不等于藏起来：需要改原文时点开就是。 */
  const [openSpecs, setOpenSpecs] = useState<Record<string, boolean>>({})

  /** 某个目录下的**直接**子文件（更深层由它自己的目录行负责）。
   *
   * 为什么必须按目录分组而不是平铺：恋爱军师 44 篇平铺时，「知识」和「话术」
   * 两个分类就只剩两个空目录行——用户看不出哪篇属于哪类，
   * 「分类目录」这个功能等于白做。 */
  const filesIn = (spec: AssetSpec, dirPath: string) =>
    spec.files.filter((f) => {
      const i = f.path.lastIndexOf('/')
      // 单文件资产（role_card.md）路径里没有目录：lastIndexOf 返回 -1，
      // `slice(0, -1)` 会切掉最后一个字符，于是它永远匹配不上任何目录 → 整行消失。
      // 2026-10-26 验收时被主人发现「人格卡下面什么都没有」。
      if (i < 0) return dirPath === spec.root
      return f.path.slice(0, i) === dirPath
    })

  const renderFileRow = (spec: AssetSpec, f: AssetFile, indent: number) => {
    const meta = spec.key === 'prompt' ? promptMeta.get(f.name.replace(/\.md$/, '')) : undefined
    return (
    <li key={f.path} className="assets-filerow" style={{ paddingLeft: 6 + indent * 12 }}>
      <button
        type="button"
        className={
          'assets-file' + (f.path === selected ? ' is-active' : '') + (meta?.desc ? ' has-desc' : '')
        }
        onClick={() => void openFile(f.path)}
        onDoubleClick={(e) => {
          // 双击文件名 = 改名（主人 2026-10-26：「改名不需要点击按钮，
          // 只需双击该章节或文档即可」）。双击是「对它做点什么」的直觉动作，
          // 按钮多占一行宽度还多一步。
          e.preventDefault()
          beginRename(f)
        }}
        title={`${f.path}\n${f.mtime} · ${fmtTokens(f.size)} 字节\n双击改名`
          + (meta?.desc ? `\n${meta.desc}` : '')}
      >
        <span className="assets-fline">
          <FileText size={11} />
          <span className="assets-fname">{f.name}</span>
          {meta?.origin ? (
            <em className="assets-kind origin" title="这条协议是从知识语料蒸馏进来的">
              {meta.origin}
            </em>
          ) : null}
          {meta ? (
            <em
              className={'assets-kind ' + (meta.active ? 'slot' : 'archive')}
              title={meta.active ? '正在用 prompts/ 里的这份文件' : '已回落到代码内置默认值'}
            >
              {meta.active ? '覆盖中' : '内置默认'}
            </em>
          ) : null}
        </span>
        {meta?.desc ? (
          <span className="assets-fdesc explain-only">{meta.desc}</span>
        ) : null}
      </button>
      {/* 改名：只换文件名，内容一个字不动。场景卡的 name/display 在文件内容里，
          所以改名不影响挂载——这句话按钮的 title 里就写着，免得用户不敢点。 */}
      {renaming?.path === f.path ? (
        <input
          className="assets-rename-input"
          autoFocus
          value={renameDraft}
          onChange={(e) => setRenameDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void commitRename()
            if (e.key === 'Escape') setRenaming(null)
          }}
          onBlur={() => setRenaming(null)}
          aria-label="新的文件名"
          placeholder="新名字（不含扩展名）"
        />
      ) : null}
      {spec.can_delete ? (
        <button
          type="button"
          className="assets-mini danger"
          title="删除这一篇（会先备份，可从备份恢复）"
          disabled={busy === f.path}
          onClick={() => void doDeleteFile(f.path)}
        >
          ×
        </button>
      ) : null}
    </li>
    )
  }

  /** 协议条文按「她什么时候用」分组渲染。
   *
   * 顺序**必须**跟随后端 `list_overrides()`（那是唯一的排序点）：
   * 这里若再按文件名排一次，分组标题的顺序就和条目顺序对不上了。
   *
   * 分组边界按 `group` **key** 切，不按标题文字——按文字切时，两个不同组
   * 只要撞了同一个显示名就会被渲染成一整组（2026-10-26 主人的「两个组合并」），
   * 而同一个组若被后端排到不相邻的位置又会被切成两段。 */
  const renderPromptTree = (spec: AssetSpec): ReactNode[] => {
    const order = new Map<string, number>()
    list?.prompt_overrides?.forEach((o, i) => order.set(o.name, i))
    const files = [...spec.files].sort(
      (a, b) =>
        (order.get(a.name.replace(/\.md$/, '')) ?? 999) -
        (order.get(b.name.replace(/\.md$/, '')) ?? 999),
    )
    const groupInfo = new Map((promptGroups?.groups ?? []).map((g) => [g.key, g]))
    const out: ReactNode[] = []
    let cur: string | null = null
    for (const f of files) {
      const meta = promptMeta.get(f.name.replace(/\.md$/, ''))
      const key = meta?.group || ''
      if (key !== cur) {
        cur = key
        const g = groupInfo.get(key)
        const title = meta?.title || '未分组'
        out.push(
          <li
            key={`grp-${key || 'unknown'}`}
            className={'assets-grouprow' + (meta && !meta.known ? ' is-warn' : '')}
          >
            {renaming?.kind === 'group' && renaming.path === key ? (
              <input
                className="assets-rename-input"
                autoFocus
                value={renameDraft}
                onChange={(e) => setRenameDraft(e.target.value)}
                onBlur={() => void commitRename()}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void commitRename()
                  if (e.key === 'Escape') setRenaming(null)
                }}
              />
            ) : (
              <span
                className="assets-groupname"
                onDoubleClick={() => beginRenameGroup(key, title)}
                title={
                  (g?.desc ? g.desc + '\n\n' : '') +
                  (meta && !meta.known
                    ? `文件头写的 group: ${key} 不在分组表里——双击改不了不存在的组，` +
                      `请在 prompts/_groups.yaml 里加一条，或把这条协议的 group 改成已有的 key`
                    : `双击改这一组的标题（写在 prompts/_groups.yaml 里）` +
                      (g ? `\n这一组现有 ${g.files} 条协议` : ''))
                }
              >
                {title}
                {meta && !meta.known ? (
                  <em className="assets-kind warn" title="这个 group key 不在分组表里">
                    分组不存在
                  </em>
                ) : g && g.files > 0 ? (
                  <em className="assets-groupcount">{g.files}</em>
                ) : null}
              </span>
            )}
          </li>,
        )
      }
      out.push(renderFileRow(spec, f, 1))
    }
    return out
  }

  /** 场景人格卡：显示 emoji + 名字 + 说明，而不是 yaml 文件名。
   *
   * 她在群里挂的是「🧠 心理陪伴」，文件却叫 `psychology.yaml`——
   * 只给文件名，用户根本不知道自己在改哪张卡（教训 #48：界面撒谎）。 */
  /** 当前打开的是哪张场景卡（顶栏显示卡片名而不是文件名）。 */
  const activeCard = useMemo(() => {
    for (const g of list?.groups ?? []) {
      for (const sp of g.items) {
        for (const c of sp.cards) {
          if (c.path === selected) return c
        }
      }
    }
    return null
  }, [list, selected])

  const renderCards = (spec: AssetSpec) => (
    <div className="assets-cards">
      {spec.cards.map((c) => (
        <button
          key={c.path}
          type="button"
          className={'assets-card' + (c.path === selected ? ' is-active' : '')}
          onClick={() => void openFile(c.path)}
          title={`${c.path}\n${c.mtime} · ${fmtTokens(c.size)} 字节`}
        >
          <span className="assets-card-head">
            <span className="assets-card-title">{c.display}</span>
            <em className={'assets-kind ' + (c.type === 'replace' ? 'archive' : 'category')}>
              {c.type === 'replace' ? '替换' : '叠加'}
            </em>
          </span>
          <span className="assets-card-desc">{c.description || '（这张卡没写说明）'}</span>
          {c.warning ? <span className="assets-card-warn">{c.warning}</span> : null}
        </button>
      ))}
    </div>
  )

  /** 身份卡：显示从首行读到的名字，而不是 role_card_murasame.md 这种文件名。
   *
   * 三张卡是**互斥**的（同一时刻只有一张生效，`/角色` 切换时连带换声线与表情包），
   * 所以按卡片列——文件列表会让人以为它们可以并列叠加。 */
  const renderRoleCards = (spec: AssetSpec) => (
    <div className="assets-cards">
      {spec.files.map((f) => (
        <button
          key={f.path}
          type="button"
          className={'assets-card' + (f.path === selected ? ' is-active' : '')}
          onClick={() => void openFile(f.path)}
          title={`${f.path}\n${f.mtime} · ${fmtTokens(f.size)} 字节`}
        >
          <span className="assets-card-head">
            <span className="assets-card-title">{f.label || f.name}</span>
            {f.is_default ? (
              <em className="assets-kind slot" title="默认身份：其它卡都没加载时用它">
                默认
              </em>
            ) : null}
          </span>
          <span className="assets-card-desc">
            {f.name} · {fmtTokens(f.size)} 字节
          </span>
        </button>
      ))}
    </div>
  )

  /** 注入方式的一句话标签——**这是主人最需要知道的**：
   *  「我改了这份东西，她什么时候用得上」。
   *
   *  `scenario` 型曾经被我写成「每轮都在」，主人 2026-10-26 纠正：
   *  「这会在我的『亲密模式』下才触发」。真实条件见 handler.py:5502-5509。 */
  const injectionLabel = (spec: AssetSpec): string => {
    switch (spec.injection) {
      case 'forced':
        return '每轮都在'
      case 'scenario':
        return '模式激活时'
      case 'retrieval':
        return '查了才有'
      default:
        return ''
    }
  }

  const renderDirRow = (spec: AssetSpec, d: AssetDir) => (
    <li key={d.path} className="assets-dirrow" style={{ paddingLeft: 6 + d.depth * 12 }}>
      <Folder size={11} className="assets-folder" />
      {renaming?.kind === 'dir' && renaming.path === d.path ? (
        <input
          className="assets-rename-input"
          autoFocus
          value={renameDraft}
          onChange={(e) => setRenameDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === 'Enter') void commitRename()
            if (e.key === 'Escape') setRenaming(null)
          }}
          onBlur={() => setRenaming(null)}
          aria-label="新的槽位名（数字前缀保留）"
          placeholder="只改「-」后面这段"
        />
      ) : (
        <span
          className="assets-dirname is-renameable"
          title="双击改目录名（数字前缀保留）"
          onDoubleClick={(e) => {
            e.preventDefault()
            beginRenameDir(d)
          }}
        >
          {d.name}
        </span>
      )}
      <em className={`assets-kind ${d.kind}`}>
        {d.kind === 'slot' ? '进注入' : d.kind === 'archive' ? '归档·不进' : '分类'}
      </em>
      {spec.can_create_file ? (
        <button
          type="button"
          className="assets-mini"
          title={`在 ${d.name} 里新建一篇`}
          onClick={() => beginCreate(spec.key, 'file', d.path, spec.exts, '')}
        >
          ＋
        </button>
      ) : null}
      {d.empty ? (
        <button
          type="button"
          className="assets-mini danger"
          title="删除这个空目录"
          disabled={busy === d.path}
          onClick={() => void doDeleteDir(d.path)}
        >
          ×
        </button>
      ) : null}
    </li>
  )

  // ── 结构管理（B5）──────────────────────────────────────────
  // 判据全在后端：名字规则、父目录存在、是否覆盖，一律由 `asset_edit` 判断。
  // 前端只负责「把错误原样显示」——这里再写一遍规则就是第二个判定点（教训 #38）。

  const beginCreate = (
    specKey: string,
    kind: 'file' | 'dir',
    base: string,
    exts: string[],
    hint: string,
  ) => {
    setCreating({ specKey, kind, base, exts, hint })
    setNewName('')
    setNotice(null)
  }

  const submitCreate = useCallback(async () => {
    if (!creating) return
    let name = newName.trim()
    if (!name) return
    if (creating.kind === 'file' && !/\.[a-z0-9]+$/i.test(name)) {
      // 不写后缀 100% 会被后端拒（bad_ext），不如在这里补上最常见的那一个
      name += creating.exts[0] || '.md'
    }
    const path = `${creating.base}/${name}`
    setBusy('create')
    try {
      if (creating.kind === 'dir') {
        const r = await api.assetMkdir(path)
        setNotice({
          kind: 'ok',
          text: [`已新建目录「${r.name}」`, r.in_injection ? '它进常驻注入' : '它不进常驻注入',
                 ...r.warnings].join(' · '),
        })
      } else {
        const r = await api.assetCreate(path, '')
        setNotice({
          kind: r.warnings.length ? 'warn' : 'ok',
          text: [`已新建「${path.split('/').pop()}」`, r.apply_hint, ...r.warnings].join(' · '),
        })
        setCreating(null)
        setNewName('')
        void loadList()
        notifyDataChanged()
        await openFile(path)
        return
      }
      setCreating(null)
      setNewName('')
      void loadList()
      notifyDataChanged()
    } catch (e) {
      setNotice({ kind: 'error', text: (e as Error).message })
    } finally {
      setBusy('')
    }
  }, [creating, newName, loadList, openFile])

  /** 进入改名态（双击文件名触发）。只改文件名——场景卡的 name/display 在内容里，
   * 所以挂载与触发都不受影响。 */
  const beginRename = useCallback((f: AssetFile) => {
    setRenameDraft(f.name.replace(/\.[^.]+$/, ''))
    setRenaming({ path: f.path, stem: f.name, kind: 'file' })
  }, [])

  /** 目录改名（双击目录名触发）。数字前缀保留，输入框只预填 `-` 后面那段——
   *  让用户改「核心」而不是「00-核心」，前缀是槽位序号，不该由他记。 */
  const beginRenameDir = useCallback((d: AssetDir) => {
    const m = /^(\d+)-(.*)$/.exec(d.name)
    setRenameDraft(m ? m[2] : d.name)
    setRenaming({ path: d.path, stem: d.name, kind: 'dir' })
  }, [])

  /** 改组标题（双击组头触发）。`key` 为空是「未分组」——那不是一个组，没有名字可改。 */
  const beginRenameGroup = useCallback((key: string, title: string) => {
    if (!key) return
    setRenameDraft(title)
    setRenaming({ path: key, stem: title, kind: 'group' })
  }, [])

  /** 提交改名。只改文件名——场景卡的 name/display 在内容里，所以挂载不受影响。 */
  const commitRename = useCallback(async () => {
    if (!renaming) return
    const name = renameDraft.trim()
    setRenaming(null)
    if (!name) return
    // 组标题：只动 `prompts/_groups.yaml` 里那一块，不碰任何协议文件
    if (renaming.kind === 'group') {
      try {
        const r = await api.promptGroupRename(renaming.path, name)
        setNotice({ kind: 'ok', text: `组标题已改为「${r.title}」 · ${r.apply_hint}` })
        refreshGroups()
        notifyDataChanged()
      } catch (e) {
        setNotice({ kind: 'error', text: (e as Error).message })
      }
      return
    }
    try {
      const r =
        renaming.kind === 'dir'
          ? await api.assetRenameDir(renaming.path, name)
          : await api.assetRename(renaming.path, name)
      setNotice({
        kind: 'ok',
        text: [r.new_path === r.path ? '名字没变' : `已改名为 ${r.new_path.split('/').pop()}`,
               r.note, '改前版本已备份'].filter(Boolean).join(' · '),
      })
      if (selected === renaming.path) setSelected(r.new_path)
      void loadList()
      notifyDataChanged()
    } catch (e) {
      setNotice({ kind: 'error', text: (e as Error).message })
    }
  }, [renaming, renameDraft, selected, refreshGroups])

  const doDeleteFile = useCallback(
    async (path: string) => {
      if (!window.confirm(`删除「${path.split('/').pop()}」？\n\n会先备份到 _backups，可以从备份恢复。`))
        return
      setBusy(path)
      try {
        const r = await api.assetDelete(path)
        setNotice({
          kind: 'ok',
          text: `已删除 · 备份在 _backups/${r.backup} · ${r.apply_hint}`,
        })
        if (selected === path) {
          setSelected('')
          setAsset(null)
          setDraft('')
        }
        void loadList()
        notifyDataChanged()
      } catch (e) {
        setNotice({ kind: 'error', text: (e as Error).message })
      } finally {
        setBusy('')
      }
    },
    [loadList, selected],
  )

  const doDeleteDir = useCallback(
    async (path: string) => {
      if (!window.confirm(`删除空目录「${path.split('/').pop()}」？\n\n目录里有内容时会被拒绝。`))
        return
      setBusy(path)
      try {
        const r = await api.assetRmdir(path)
        setNotice({ kind: 'ok', text: `已删除空目录 · ${r.apply_hint}` })
        void loadList()
        notifyDataChanged()
      } catch (e) {
        setNotice({ kind: 'error', text: (e as Error).message })
      } finally {
        setBusy('')
      }
    },
    [loadList],
  )

  return (
    <div className="assets-page">
      <aside className="assets-tree">
        <header className="assets-tree-head">
          <h1>人格与规则</h1>
          <p>
            她的身份、行为协议、场景剧本、知识文档——都能在这里改。
            <b>每次保存前自动备份</b>。
          </p>
        </header>

        {listError ? (
          <div className="assets-err">
            <AlertTriangle size={13} />
            <span>{listError}</span>
          </div>
        ) : null}

        {!list && !listError ? (
          <div className="assets-loading">
            <LoaderCircle size={14} className="assets-spin" />
            正在读取资产清单…
          </div>
        ) : null}

        {list?.groups.map((g) => (
          <section className="assets-group" key={g.group}>
            <h2 className="assets-group-title">{g.group}</h2>
            {g.group_desc ? (
              <p className="assets-group-desc explain-only">{g.group_desc}</p>
            ) : null}
            {g.items.map((spec) => (
              <div className="assets-spec" key={spec.key}>
                <div className="assets-spec-head" title={spec.desc}>
                  <ChevronRight size={11} className="assets-caret" />
                  <span>{spec.label}</span>
                  <em>{spec.files.length}</em>
                  {injectionLabel(spec) ? (
                    <em className={'assets-kind inj ' + spec.injection}>
                      {injectionLabel(spec)}
                    </em>
                  ) : null}
                  <div className="assets-spec-tools">
                    {spec.can_create_file ? (
                      <button
                        type="button"
                        className="assets-mini"
                        onClick={() => beginCreate(spec.key, 'file', spec.root, spec.exts, '')}
                        title="在根目录新建一篇"
                      >
                        ＋篇
                      </button>
                    ) : null}
                    {spec.can_mkdir ? (
                      <button
                        type="button"
                        className="assets-mini"
                        onClick={() =>
                          beginCreate(spec.key, 'dir', spec.root, spec.exts, spec.dir_rule)
                        }
                        title={spec.dir_rule}
                      >
                        ＋目录
                      </button>
                    ) : null}
                  </div>
                </div>

                {creating?.specKey === spec.key ? (
                  <div className="assets-newline">
                    <div className="assets-newline-hint">
                      {creating.kind === 'dir'
                        ? creating.hint
                        : `新建篇目 · 位置 ${creating.base}`}
                    </div>
                    <div className="assets-newline-row">
                      <input
                        autoFocus
                        value={newName}
                        placeholder={
                          creating.kind === 'dir'
                            ? '例如 30-新槽 或 _草稿'
                            : `篇名（不写后缀自动补 ${spec.exts[0] || '.md'}）`
                        }
                        onChange={(e) => setNewName(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') void submitCreate()
                          if (e.key === 'Escape') setCreating(null)
                        }}
                      />
                      <button
                        type="button"
                        className="assets-mini ok"
                        disabled={busy === 'create' || !newName.trim()}
                        onClick={() => void submitCreate()}
                      >
                        建
                      </button>
                      <button
                        type="button"
                        className="assets-mini"
                        onClick={() => setCreating(null)}
                      >
                        取消
                      </button>
                    </div>
                  </div>
                ) : null}

                {spec.key === 'knowledge_retrieval' && kStatus ? (
                  <div className={'assets-idx ' + (kStatus.needs_sync ? 'warn' : 'ok')}>
                    {kStatus.needs_sync
                      ? `索引缺 ${kStatus.retrieval.missing.length} 篇，这些目前查不到。${kStatus.sync_hint}`
                      : `索引齐全：可检索 ${kStatus.retrieval.disk} 篇 = 已索引 ${kStatus.retrieval.indexed} 篇`}
                    <br />
                    {/* 受保护目录为什么不在索引里 = 解释（讲解模式可关）；上面的数字是状态 */}
                    <span className="explain-only">
                      {kStatus.protected_dirs.length} 个受保护目录按设计不进检索索引——
                      它们走「场景触发」通道，只在对应模式激活时注入
                    </span>
                  </div>
                ) : null}

                {spec.key === 'knowledge_scenario' && kStatus ? (
                  <div className="assets-idx ok">
                    这 {kStatus.scenario_channel.files} 篇分{' '}
                    {kStatus.scenario_channel.slots.length} 个槽位目录
                    {/* 机制说明 = 解释（讲解模式可关）。**不列具体槽位名**：
                        那是使用者的内容设定，列在文案里就变成「架构认得内容名」（教训 #52） */}
                    <span className="explain-only">
                      ：数字前缀 = 槽位（整块注入），下划线前缀 = 归档（不注入）。
                      {kStatus.scenario_channel.trigger}——平时一个字都不进上下文，
                      所以改这里不会影响日常对话，只影响进入该模式之后。
                    </span>
                  </div>
                ) : null}
                {spec.files.length === 0 && spec.dirs.length === 0 && spec.cards.length === 0 ? (
                  <div className="assets-spec-empty">
                    {spec.exists ? '这个目录是空的' : '还没有这个目录（新建文件后出现）'}
                  </div>
                ) : (
                  spec.injection === 'retrieval' && !openSpecs[spec.key] ? (
                    <button
                      type="button"
                      className="assets-folded"
                      onClick={() => setOpenSpecs((o) => ({ ...o, [spec.key]: true }))}
                      title="这些原文她只有主动检索时才可能读到，平时改这里不会改变她的话术"
                    >
                      共 {spec.files.length} 篇原文 · 她基本不读，只供检索深挖 —— 点开查看
                    </button>
                  ) : spec.key === 'roles' ? (
                    renderRoleCards(spec)
                  ) : spec.cards.length ? (
                    renderCards(spec)
                  ) : (
                  <ul className="assets-tree-list">
                    {spec.key === 'prompt'
                      ? renderPromptTree(spec)
                      : filesIn(spec, spec.root).map((f) => renderFileRow(spec, f, 0))}
                    {spec.dirs.map((d) => (
                      <Fragment key={d.path}>
                        {renderDirRow(spec, d)}
                        {filesIn(spec, d.path).map((f) =>
                          renderFileRow(spec, f, d.depth + 1),
                        )}
                      </Fragment>
                    ))}
                  </ul>
                  )
                )}
              </div>
            ))}
          </section>
        ))}
      </aside>

      <main className="assets-main">
        {!selected ? (
          <div className="assets-hint">
            <FileText size={22} />
            <p>左边选一个文件开始编辑。</p>
            <p className="assets-hint-sub">
              保存前会自动备份到 <code>_backups/编辑_&lt;时间&gt;/</code>，
              改坏了可以「从备份恢复」。
            </p>
          </div>
        ) : loading ? (
          <div className="assets-loading">
            <LoaderCircle size={14} className="assets-spin" />
            正在读取…
          </div>
        ) : asset ? (
          <>
            <header className="assets-bar">
              <div className="assets-bar-main">
                <div className="assets-name">
                  {activeCard ? activeCard.display : asset.path.split('/').pop()}
                  {dirty ? <span className="assets-dot" title="有未保存的修改" /> : null}
                </div>
                <div className="assets-meta">
                  {activeCard ? `\n${activeCard.description}` : null}
                  {asset.path} · {asset.mtime} · {fmtTokens(asset.size)} 字节 · {lines} 行
                </div>
              </div>
              <div className="assets-actions">
                <button
                  type="button"
                  className={'assets-btn' + (preview ? ' is-on' : '')}
                  onClick={() => setPreview((v) => !v)}
                  title={preview ? '当前是预览（渲染后的样子，不能编辑）' : '看渲染后的结构：标题、列表、加粗'}
                >
                  <WrapText size={13} />
                  预览
                </button>
                <button
                  type="button"
                  className="assets-btn"
                  onClick={() => setDraft(asset.content)}
                  disabled={!dirty}
                  title="丢弃未保存的修改"
                >
                  <Undo2 size={13} />
                  撤销
                </button>
                <div className="assets-restore">
                  <button
                    type="button"
                    className="assets-btn"
                    onClick={() => setShowBackups((v) => !v)}
                    disabled={!asset.backups.length}
                    title={
                      asset.backups.length
                        ? `${asset.backups.length} 份备份`
                        : '还没有备份（第一次保存后才有）'
                    }
                  >
                    <History size={13} />
                    备份{asset.backups.length ? ` ${asset.backups.length}` : ''}
                  </button>
                  {showBackups && asset.backups.length ? (
                    <div className="assets-backups">
                      {asset.backups.map((b) => (
                        <button
                          key={b.path}
                          type="button"
                          onClick={() => void doRestore(b.path)}
                          title={b.path}
                        >
                          <span>{b.dir.replace('编辑_', '')}</span>
                          <em>{fmtTokens(b.size)} B</em>
                        </button>
                      ))}
                    </div>
                  ) : null}
                </div>
                {asset.path.startsWith('prompts/') ? (
                  <button
                    type="button"
                    className="assets-btn"
                    onClick={() => void doReset()}
                    title="删除这个覆盖文件，回落到代码内置默认值"
                  >
                    <RotateCcw size={13} />
                    恢复默认
                  </button>
                ) : null}
                <button
                  type="button"
                  className="assets-btn is-primary"
                  onClick={() => void save()}
                  disabled={!dirty || saving}
                >
                  {saving ? (
                    <LoaderCircle size={13} className="assets-spin" />
                  ) : (
                    <Save size={13} />
                  )}
                  保存
                </button>
              </div>
            </header>

            <div className="assets-apply" title="改动不会自动生效——这里有说明">
              <AlertTriangle size={12} />
              <span>{asset.apply_hint}</span>
            </div>

            {notice ? (
              <div className={'assets-notice is-' + notice.kind}>
                {notice.kind === 'ok' ? <Check size={13} /> : <AlertTriangle size={13} />}
                <span>{notice.text}</span>
              </div>
            ) : null}

            {preview ? (
              <div
                className="assets-preview"
                dangerouslySetInnerHTML={{ __html: renderMarkdown(draft) }}
              />
            ) : (
              <div className="assets-editor">
                <textarea
                  ref={taRef}
                  className="assets-textarea"
                  value={draft}
                  spellCheck={false}
                  onChange={(e) => setDraft(e.target.value)}
                />
              </div>
            )}
          </>
        ) : (
          <div className="assets-hint">
            <AlertTriangle size={22} />
            <p>这个文件读不出来。</p>
          </div>
        )}
      </main>
    </div>
  )
}
