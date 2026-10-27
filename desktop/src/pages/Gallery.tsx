import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Images,
  Search,
  X,
  RefreshCw,
  Check,
  Tag,
  Trash2,
  ChevronLeft,
  ChevronRight,
  Loader2,
  CheckSquare,
  Square as SquareIcon,
  AlertCircle,
  Sparkles,
  Eye,
  Copy,
  FolderOpen,
  Tags,
  Square,
  Settings,
  FlaskConical,
} from 'lucide-react'
import {
  api,
  getBridge,
  galleryImageUrl,
  type BridgeInfo,
  type GalleryItem,
  type GalleryResponse,
  type GalleryTagStatus,
} from '../lib/api'
import ContextMenu, { type MenuState } from '../components/ContextMenu'
import ModelPicker from '../components/ModelPicker'
import './Page.css'
import './Gallery.css'

/** 一级分段：贴图组（默认/丛雨/米雪儿 三套合并）/ CG */
const TOP_SOURCES = [
  { key: 'sticker', label: '贴图', all: 'sticker_all' },
  { key: 'cg', label: 'CG', all: 'cg' },
] as const

type TopSource = (typeof TOP_SOURCES)[number]['key']

/** 选中项用「库:文件名」做 key —— 三套贴图合并展示时不能只靠文件名，
 *  万一两库有同名文件，选中状态会串。 */
const itemKey = (it: { source: string; name: string }) => `${it.source}:${it.name}`

const SORTS = [
  { key: 'mtime_desc', label: '最新优先' },
  { key: 'mtime_asc', label: '最早优先' },
  { key: 'size_desc', label: '体积从大到小' },
  { key: 'name', label: '按文件名' },
  { key: 'tagged_first', label: '已标注优先' },
]

const PAGE_SIZE = 48

function fileSize(n: number): string {
  if (n >= 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`
  if (n >= 1024) return `${Math.round(n / 1024)} KB`
  return `${n} B`
}

/** 数字页码：首尾始终可见，当前页左右各留 1 个 */
function pageNumbers(current: number, total: number): (number | '…')[] {
  if (total <= 7) return Array.from({ length: total }, (_, i) => i + 1)
  const out: (number | '…')[] = []
  const push = (v: number | '…') => {
    if (out[out.length - 1] !== v) out.push(v)
  }
  push(1)
  for (let p = current - 1; p <= current + 1; p++) {
    if (p > 1 && p < total) {
      if (p === current - 1 && p > 2) push('…')
      push(p)
    }
  }
  if (current + 1 < total - 1) push('…')
  push(total)
  return out
}

export default function Gallery({
  onOpenSettings,
}: {
  /** 「去设置」用：跳到指定的设置分组（打标参数在 `image_tagging`） */
  onOpenSettings?: (section?: string) => void
} = {}) {
  const [bridge, setBridge] = useState<BridgeInfo | null>(null)
  const [source, setSource] = useState<TopSource>('sticker')
  /** 贴图组内的二级选择：'' = 三套合并，或具体库 key（sticker/murasame/michele） */
  const [lib, setLib] = useState('')
  const [searchText, setSearchText] = useState('')
  const [q, setQ] = useState('')
  const [tag, setTag] = useState('')
  const [state, setState] = useState<'' | 'tagged' | 'untagged'>('')
  const [sort, setSort] = useState('mtime_desc')
  const [page, setPage] = useState(1)

  const [data, setData] = useState<GalleryResponse | null>(null)
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [preview, setPreview] = useState<GalleryItem | null>(null)
  const [busy, setBusy] = useState(false)
  const [toast, setToast] = useState('')
  const [imgSize, setImgSize] = useState<{ w: number; h: number } | null>(null)
  /** 图片渲染后的实际宽度 —— 信息区用它做「跟随图片但有下限」 */
  const [imgBox, setImgBox] = useState<{ w: number; h: number } | null>(null)

  const [tagMenuOpen, setTagMenuOpen] = useState(false)
  const [tagDraft, setTagDraft] = useState('')
  const [confirmTrash, setConfirmTrash] = useState(false)
  const [menu, setMenu] = useState<MenuState>(null)
  const lastClicked = useRef<number | null>(null)

  useEffect(() => {
    getBridge().then(setBridge).catch((e: Error) => setError(e.message))
  }, [])

  /** 图库页当前选中的库 key —— **唯一一套「当前目录」语义**：
   *  列表加载和打标范围都用它，不另造第二个「当前目录」（两套必然打架）。 */
  const gallerySource = source === 'sticker' ? lib || 'sticker_all' : 'cg'

  /* ── 图库打标（2026-10-26 第 63 轮；2026-10-27 按图库页语义重排）──
   *
   * 主人反馈：「设置中的『图库打标』仅仅是调整背后参数、信息、模型的而非决定打标方式，
   * 打标方式、选哪些图打标需要在图库中具体设置并打标。」
   *
   * 所以这里管「打哪些图 / 怎么打」，设置页只管「用哪个模型、什么端点、key」。
   * 范围直接继承图库页当前的库（source + 二级 lib），**不另造一个当前目录**——
   * 两套「当前目录」概念必然打架。
   *
   * 打标是长任务（本地模型约 1.5 秒一张），所以：点了立刻返回 + 轮询进度，
   * 不能同步等——否则界面假死几十秒没人知道还在跑。 */
  const [tagStatus, setTagStatus] = useState<GalleryTagStatus | null>(null)
  const [tagBusy, setTagBusy] = useState(false)
  const [tagPanelOpen, setTagPanelOpen] = useState(false)
  const [tagScope, setTagScope] = useState<'pending' | 'all' | 'selected'>('pending')
  const [tagRename, setTagRename] = useState(true)
  const [tagOverwrite, setTagOverwrite] = useState(false)
  /** 打标相关的错误紧贴按钮显示——塞进页面顶部那个 error 用户根本看不见（原 bug） */
  const [tagError, setTagError] = useState('')
  const tagRenameTouched = useRef(false)

  /* ── 试打一张（2026-10-27）─────────────────────────────────────────
   * 配好 API Key / 本地模型之后**先验证通道**：只跑模型、不写 metadata.json。
   * 价值在「第一次批量跑之前」——否则 key 填错、模型名不对、显存不够，
   * 要等几十张失败才从日志里看出来（2026-10-26 的 6 条尾巴之一）。 */
  const [probeBusy, setProbeBusy] = useState(false)
  const [probeError, setProbeError] = useState('')
  const [probeResult, setProbeResult] = useState<
    Awaited<ReturnType<typeof api.galleryTagProbe>> | null
  >(null)

  /** 选模型面板开着吗（2026-10-27：方式和模型在同一份列表里选，不跳设置页） */
  const [pickerOpen, setPickerOpen] = useState(false)

  const refreshTag = useCallback(async () => {
    try {
      setTagStatus(await api.galleryTagStatus(gallerySource))
    } catch {
      /* 打标是可选功能：拉不到就不显示按钮，不能把图库页搞挂 */
      setTagStatus(null)
    }
  }, [gallerySource])

  useEffect(() => {
    void refreshTag()
  }, [refreshTag])

  // 切库时收面板并回到最安全的默认：面板里的数字是上一个库的，
  // 范围若留着「全部重打」，换个库一点就是几千张（比误点更糟的是误打）
  useEffect(() => {
    setTagPanelOpen(false)
    setTagError('')
    setTagScope('pending')
    setTagOverwrite(false)
  }, [gallerySource])

  // 只在跑的时候轮询；停下就停，别让一个隐藏的定时器永远转
  const tagRunning = Boolean(tagStatus?.progress.running)
  useEffect(() => {
    if (!tagRunning) return
    const t = setInterval(() => void refreshTag(), 2000)
    return () => clearInterval(t)
  }, [tagRunning, refreshTag])

  // 改名默认值跟配置走（老配置 rename=false 的人不该被这里默默打开），用户随时可改
  useEffect(() => {
    if (!tagStatus || tagRenameTouched.current) return
    setTagRename(tagStatus.config.rename !== false)
  }, [tagStatus])

  /** 当前库里被选中的文件名（合集时为空——写操作只允许单库） */
  const selectedNamesForTag = useMemo(() => {
    const out: string[] = []
    for (const k of selected) {
      const idx = k.indexOf(':')
      if (idx < 0) continue
      if (k.slice(0, idx) === gallerySource) out.push(k.slice(idx + 1))
    }
    return out
  }, [selected, gallerySource])

  const tagProvider = tagStatus?.config.provider ?? ''
  const tagOff = Boolean(tagStatus) && tagProvider === 'off'
  /** 为什么不能打：**分成两类**，出路不同（2026-10-27 分开渲染）
   *  · `provider_ready` = 配置问题（没选方式 / 模型名空 / key 没配）→ 去设置
   *  · `dir_ready`      = 范围问题（当前是合集，写操作只允许单库）→ 切库
   * 以前合成一个「打标不可用」，配好 key 的人会一直怀疑 key。 */
  const tagPending = tagStatus?.scan.pending ?? 0
  const tagFiles = tagStatus?.scan.files ?? 0
  /** 面板里显示的库名（用列表接口给的 label，避免在这里再抄一份中英对照） */
  const tagSourceLabel =
    (data?.sources ?? []).find((s) => s.key === gallerySource)?.label ??
    (gallerySource === 'sticker_all' ? '贴图（三个库合集）' : gallerySource)
  const tagModel =
    tagProvider === 'api' ? tagStatus?.config.api_model : tagStatus?.config.ollama_model
  /** 面板里「当前选中」的 key（与 /api/models/picker 的 current 同一套写法） */
  const tagConfig = (tagStatus?.config ?? {}) as Record<string, unknown>
  // 2026-10-28：只有一个 `model_ref` 决定用哪个模型（'' = 当前聊天模型、
  // `ollama:x` = 本机、`<id>` = 档位），所以 current 直接由它派生——
  // 不再按 provider 分支猜，那正是「界面显示 A、实际在用 B」的来源（教训 #51）。
  const tagRef = String(tagConfig.model_ref || '')
  const pickerCurrent = !tagRef
    ? 'main'
    : tagRef.startsWith('ollama:')
      ? tagRef
      : `ref:${tagRef}`

  const startTag = useCallback(async () => {
    if (!tagStatus) return
    if (tagStatus.provider_ready || tagStatus.dir_ready) {
      setTagError(tagStatus.provider_ready || tagStatus.dir_ready)
      return
    }
    const scope = tagScope
    const names = scope === 'selected' ? selectedNamesForTag : []
    if (scope === 'selected' && names.length === 0) {
      setTagError('没有选中这个图库里的图')
      return
    }
    setTagBusy(true)
    setTagError('')
    try {
      const r = await api.galleryTagRun({
        source: gallerySource,
        scope,
        names,
        rename: tagRename,
        overwrite: scope === 'pending' ? false : tagOverwrite,
      })
      if (!r.ok) setTagError(r.error || '启动失败')
      else if (!r.started) setTagError(r.message || '没有需要打标的图')
      else setTagPanelOpen(false)
      await refreshTag()
    } catch (e) {
      setTagError((e as Error).message)
    } finally {
      setTagBusy(false)
    }
  }, [tagStatus, tagScope, selectedNamesForTag, gallerySource, tagRename, tagOverwrite, refreshTag])

  /** 试打的那一张：优先当前勾选的第一张，否则列表第一张（不猜、不让他手动填名字） */
  const probeCandidate = selectedNamesForTag[0] ?? data?.items?.[0]?.name ?? ''

  const runProbe = useCallback(async () => {
    if (!probeCandidate) {
      setProbeError('这个图库没有可试的图')
      return
    }
    setProbeBusy(true)
    setProbeError('')
    setProbeResult(null)
    try {
      const r = await api.galleryTagProbe({ source: gallerySource, name: probeCandidate })
      if (!r.ok) setProbeError(r.error || '试打失败（先看设置里的打标方式与 Key）')
      else setProbeResult(r)
    } catch (e) {
      setProbeError((e as Error).message)
    } finally {
      setProbeBusy(false)
    }
  }, [probeCandidate, gallerySource])

  /**
   * 面板里直接换模型（2026-10-27）。
   *
   * 为什么不再「跳到设置页改」：主人反馈点完「改模型」回来显示的还是旧名字，
   * 而且「打标方式」和「模型」两个控件分开填，容易出现方式=api 但模型名还是
   * ollama 那个的自相矛盾组合。这里一次把方式和模型定下来，选完立刻重拉状态。
   */
  const pickModel = useCallback(
    async (item: { kind: 'main' | 'ref' | 'ollama'; value: string }) => {
      try {
        const r = await api.galleryTagModel({ kind: item.kind, value: item.value })
        setPickerOpen(false)
        // 先重拉状态（模型名/能不能跑都变了），再决定要不要报错
        await refreshTag()
        setTagError(r.provider_ready || '')
      } catch (e) {
        setTagError((e as Error).message)
      }
    },
    [refreshTag],
  )

  const stopTag = useCallback(async () => {
    try {
      await api.galleryTagCancel()
      await refreshTag()
    } catch (e) {
      setTagError((e as Error).message)
    }
  }, [refreshTag])

  useEffect(() => {
    const t = setTimeout(() => {
      setQ(searchText.trim())
      setPage(1)
    }, 260)
    return () => clearTimeout(t)
  }, [searchText])

  const load = useCallback(async () => {
    if (!bridge) return
    setLoading(true)
    try {
      const res = await api.gallery({
        source: gallerySource,
        q,
        tag,
        tag_field: source === 'cg' ? 'scene_tags' : 'emotions',
        state,
        sort,
        page,
        page_size: PAGE_SIZE,
      })
      setData(res)
      setError(null)
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }, [bridge, gallerySource, source, q, tag, state, sort, page])

  useEffect(() => {
    load()
  }, [load])

  // 跑完那一刻刷新一次列表：标签和文件名都变了，不刷还是旧的
  const wasRunning = useRef(false)
  useEffect(() => {
    if (wasRunning.current && !tagRunning) {
      void load()
      void refreshTag()
    }
    wasRunning.current = tagRunning
  }, [tagRunning, load, refreshTag])

  const flash = (msg: string) => {
    setToast(msg)
    setTimeout(() => setToast(''), 3200)
  }

  const resetFilters = () => {
    setSearchText('')
    setQ('')
    setTag('')
    setState('')
    setSort('mtime_desc')
    setPage(1)
    setSelected(new Set())
  }

  const items = data?.items ?? []
  const pageKeys = useMemo(() => items.map(itemKey), [items])
  const allOnPageSelected =
    pageKeys.length > 0 && pageKeys.every((k) => selected.has(k))

  /** 把选中的「库:文件名」按库分组 —— 写操作接口只接受单库 */
  const selectedBySource = useMemo(() => {
    const m = new Map<string, string[]>()
    for (const k of selected) {
      const idx = k.indexOf(':')
      if (idx < 0) continue
      const src = k.slice(0, idx)
      const name = k.slice(idx + 1)
      const arr = m.get(src)
      if (arr) arr.push(name)
      else m.set(src, [name])
    }
    return m
  }, [selected])

  const toggleSelect = (key: string, index: number, ev: React.MouseEvent) => {
    setSelected((prev) => {
      const next = new Set(prev)
      // Shift 连选：从上次点的那张连到这张
      if (ev.shiftKey && lastClicked.current !== null) {
        const [a, b] = [lastClicked.current, index].sort((x, y) => x - y)
        for (let i = a; i <= b; i++) next.add(pageKeys[i])
        return next
      }
      if (next.has(key)) next.delete(key)
      else next.add(key)
      lastClicked.current = index
      return next
    })
  }

  const selectAllOnPage = () => {
    setSelected((prev) => {
      const next = new Set(prev)
      if (allOnPageSelected) pageKeys.forEach((k) => next.delete(k))
      else pageKeys.forEach((k) => next.add(k))
      return next
    })
  }

  const applyTag = async (tagName: string) => {
    const name = tagName.trim()
    if (!name || selected.size === 0) return
    setBusy(true)
    try {
      let changed = 0
      for (const [src, names] of selectedBySource) {
        const res = await api.galleryTags({ source: src, names, add: [name] })
        changed += res.changed
      }
      flash(`已给 ${changed} 张加上「${name}」`)
      setTagDraft('')
      setTagMenuOpen(false)
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const doTrash = async () => {
    setBusy(true)
    try {
      let moved = 0
      let lastDir = ''
      for (const [src, names] of selectedBySource) {
        const res = await api.galleryTrash({ source: src, names })
        moved += res.moved.length
        lastDir = res.trash_dir
      }
      flash(`已移入回收站：${moved} 张（${lastDir || '—'}）`)
      setConfirmTrash(false)
      setSelected(new Set())
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  /** 单张回收（右键菜单）—— 回收站不是真删，所以不做二次确认 */
  const trashOne = async (it: GalleryItem) => {
    setBusy(true)
    try {
      const res = await api.galleryTrash({ source: it.source, names: [it.name] })
      flash(`已移入回收站：${res.trash_dir}`)
      setSelected((prev) => {
        const next = new Set(prev)
        next.delete(itemKey(it))
        return next
      })
      await load()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setBusy(false)
    }
  }

  const openCardMenu = (it: GalleryItem, e: React.MouseEvent) => {
    e.preventDefault()
    e.stopPropagation()
    const tags = it.source === 'cg' ? it.scene_tags : it.emotions
    setMenu({
      x: e.clientX,
      y: e.clientY,
      items: [
        { key: 'view', label: '查看大图', icon: Eye, onSelect: () => setPreview(it) },
        {
          key: 'copy-name',
          label: '复制文件名',
          icon: Copy,
          onSelect: () => navigator.clipboard?.writeText(it.name).catch(() => {}),
        },
        {
          key: 'copy-tags',
          label: '复制标签',
          icon: Copy,
          disabled: tags.length === 0,
          onSelect: () => navigator.clipboard?.writeText(tags.join(' ')).catch(() => {}),
        },
        {
          key: 'tag',
          label: '加标签…',
          icon: Tag,
          separator: true,
          onSelect: () => {
            setSelected((prev) => new Set(prev).add(itemKey(it)))
            setTagMenuOpen(true)
          },
        },
        {
          key: 'reveal',
          label: '在文件夹中显示',
          icon: FolderOpen,
          onSelect: () => {
            api
              .galleryReveal({ source: it.source, name: it.name })
              .catch((err: Error) => setError(err.message))
          },
        },
        {
          key: 'trash',
          label: '移入回收站',
          icon: Trash2,
          danger: true,
          separator: true,
          onSelect: () => void trashOne(it),
        },
      ],
    })
  }

  const currentTagList =
    source === 'cg' ? (data?.scene_tags ?? []) : (data?.emotions ?? [])
  const filterTags = currentTagList.slice(0, 18)
  /** 贴图组内的三套库（默认 / 丛雨 / 米雪儿），用于二级 chips */
  const stickerLibs = (data?.sources ?? []).filter((s) => s.key !== 'cg')
  const stickersTotal = stickerLibs.reduce((n, s) => n + s.count, 0)

  const previewIndex = preview
    ? items.findIndex((i) => itemKey(i) === itemKey(preview))
    : -1
  const stepPreview = (delta: number) => {
    if (previewIndex < 0) return
    const next = items[previewIndex + delta]
    if (next) setPreview(next)
  }

  // 切图时先清掉旧尺寸，避免信息区闪一下上一张的像素值
  useEffect(() => {
    setImgSize(null)
    setImgBox(null)
  }, [preview?.name])

  useEffect(() => {
    if (!preview) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') setPreview(null)
      if (e.key === 'ArrowLeft') stepPreview(-1)
      if (e.key === 'ArrowRight') stepPreview(1)
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  })

  return (
    <div className="page gallery-page">
      <div className="page-head">
        <div>
          <h1 className="page-title">图库</h1>
          <p className="page-sub">
            贴图 {data ? stickersTotal : '…'} 张（{(data?.sources ?? [])
              .filter((s) => s.key !== 'cg')
              .map((s) => `${s.label} ${s.count}`)
              .join(' · ')}）· CG {data?.sources?.find((s) => s.key === 'cg')?.count ?? '…'} 张
            —— 筛选、打标签、批量整理
          </p>
        </div>
        <div className="page-actions">
          {/* 打标（2026-10-26 第 63 轮；2026-10-27 状态化）：
              按钮**先看状态再决定可不可点**，不是一个能点的按钮、点了才在页面顶部报错
              （那就是主人说「按钮不能用」的直接原因：错在看不见的地方）。
              这里管「打哪些图 / 怎么打」，模型与参数仍在设置 → 图库打标。 */}
          {tagStatus ? (
            tagRunning ? (
              <button
                className="btn btn-ghost"
                onClick={() => void stopTag()}
                title="当前这张跑完就停（不打断正在推理的那一张）"
              >
                <Loader2 size={14} className="spin" />
                打标中 {tagStatus.progress.done}/{tagStatus.progress.total}
                <span className="tag-abort">停止</span>
              </button>
            ) : tagOff ? (
              <>
                <button
                  className="btn btn-ghost"
                  disabled
                  title="设置 → 图库打标 → 打标方式，选 ollama（本机）或 api（云端）"
                >
                  <Tags size={14} />
                  打标未启用
                </button>
                {onOpenSettings && (
                  <button
                    className="btn-link"
                    onClick={() => onOpenSettings('image_tagging')}
                    title="去设置里选打标方式（模型 / 端点 / key）"
                  >
                    <Settings size={13} />
                    去设置
                  </button>
                )}
              </>
            ) : tagStatus.provider_ready ? (
              <>
                <button className="btn btn-ghost" disabled title={tagStatus.provider_ready}>
                  <Tags size={14} />
                  打标不可用
                </button>
                {/* 原因必须显示在按钮**旁边**——塞进页面顶部的 error 等于没说 */}
                <span className="tag-why" title={tagStatus.provider_ready}>
                  {tagStatus.provider_ready}
                </span>
                {onOpenSettings && (
                  <button
                    className="btn-link"
                    onClick={() => onOpenSettings('image_tagging')}
                    title="去设置里补齐打标参数"
                  >
                    <Settings size={13} />
                    去设置
                  </button>
                )}
              </>
            ) : tagStatus.dir_ready ? (
              /* 2026-10-27：这里**不是配置问题**，是「当前选中的是合集」。
                 以前和上面的配置错误共用一个「打标不可用」，主人配好 key 后
                 来回刷新仍看到它，自然会怀疑 key。文案与出路要分开：
                 配置问题 → 去设置；范围问题 → 直接切到一个具体库。 */
              <>
                <button className="btn btn-ghost" disabled title={tagStatus.dir_ready}>
                  <Tags size={14} />
                  合集不能打标
                </button>
                <span className="tag-why" title={tagStatus.dir_ready}>
                  {tagStatus.dir_ready}
                </span>
                {source === 'sticker' && (
                  <span className="tag-why">
                    切到一个库就能打：
                    {(data?.sources ?? [])
                      .filter((s) => s.key !== 'cg')
                      .map((s) => (
                        <button
                          key={s.key}
                          className="btn-link"
                          onClick={() => {
                            setLib(s.key)
                            setPage(1)
                          }}
                          title={`切到「${s.label}」`}
                        >
                          {s.label}
                        </button>
                      ))}
                  </span>
                )}
              </>
            ) : (
              <div className="tag-pop-wrap">
                <button
                  className={'btn btn-ghost' + (tagPanelOpen ? ' is-active' : '')}
                  onClick={() => setTagPanelOpen((v) => !v)}
                  title={
                    tagPending === 0
                      ? `这个图库 ${tagFiles} 张全都已打标——要重打就在面板里选「全部重打」`
                      : `给 ${tagPending} 张还没打标的图打标（${tagSourceLabel}）`
                  }
                >
                  <Tags size={14} />
                  {tagPending === 0
                    ? `全部已打标（${tagFiles} 张）`
                    : `打标（${tagPending} 张待打）`}
                </button>

                {tagPanelOpen && (
                  <>
                    <div className="pop-mask" onClick={() => setTagPanelOpen(false)} />
                    <div className="tag-pop" role="dialog" aria-label="图库打标">
                      <div className="tag-pop-head">
                        <span className="tag-pop-title">打标 · {tagSourceLabel}</span>
                        <button
                          className="icon-btn"
                          onClick={() => setTagPanelOpen(false)}
                          title="关闭"
                        >
                          <X size={13} />
                        </button>
                      </div>
                      <div className="tag-pop-dir" title={tagStatus.dir ?? undefined}>
                        {tagStatus.dir ?? '多个库（合集不能直接打标）'}
                      </div>
                      <div className="tag-pop-model">
                        <span className="tag-pop-model-text">
                          用 {tagProvider === 'api' ? '云端' : '本机 Ollama'} ·{' '}
                          {tagModel || '未选模型'}
                        </span>
                        <button
                          className="btn-link"
                          onClick={() => setPickerOpen((v) => !v)}
                          title="在同一份列表里选云端档位或本机模型——方式和模型一次定下来"
                        >
                          改模型
                        </button>
                        {pickerOpen && (
                          <ModelPicker
                            current={pickerCurrent}
                            onPick={(item) => pickModel(item)}
                            onClose={() => setPickerOpen(false)}
                          />
                        )}
                      </div>

                      <div className="tag-pop-label">打哪些图</div>
                      <div className="tag-scopes">
                        <label className="tag-scope">
                          <input
                            type="radio"
                            checked={tagScope === 'pending'}
                            onChange={() => setTagScope('pending')}
                          />
                          只打未打标的（{tagPending} 张）
                        </label>
                        <label className="tag-scope">
                          <input
                            type="radio"
                            checked={tagScope === 'all'}
                            onChange={() => setTagScope('all')}
                          />
                          全部重打（{tagFiles} 张）
                        </label>
                        <label
                          className={
                            'tag-scope' + (selectedNamesForTag.length === 0 ? ' is-disabled' : '')
                          }
                          title={
                            selectedNamesForTag.length === 0
                              ? '先在网格里勾选这个图库的图（合集不能打标）'
                              : '只打当前勾选的图'
                          }
                        >
                          <input
                            type="radio"
                            disabled={selectedNamesForTag.length === 0}
                            checked={tagScope === 'selected'}
                            onChange={() => setTagScope('selected')}
                          />
                          只打选中的（{selectedNamesForTag.length} 张）
                        </label>
                      </div>

                      <div className="tag-pop-opts">
                        <label
                          className={
                            'tag-check' + (tagScope === 'pending' ? ' is-disabled' : '')
                          }
                          title={
                            tagScope === 'pending'
                              ? '「只打未打标的」本来就不会碰已有标签'
                              : '勾上＝已有标签的也重打一遍'
                          }
                        >
                          <input
                            type="checkbox"
                            disabled={tagScope === 'pending'}
                            checked={tagScope === 'pending' ? false : tagOverwrite}
                            onChange={(e) => setTagOverwrite(e.target.checked)}
                          />
                          覆盖已有标签
                        </label>
                        <label className="tag-check" title="按标签生成规范文件名（会改文件名）">
                          <input
                            type="checkbox"
                            checked={tagRename}
                            onChange={(e) => {
                              tagRenameTouched.current = true
                              setTagRename(e.target.checked)
                            }}
                          />
                          打完按标签改名
                        </label>
                      </div>

                      <div className="tag-pop-hint">
                        {tagScope === 'pending'
                          ? tagPending === 0
                            ? '这个图库没有待打的图；要重打请选「全部重打」'
                            : '只打 metadata 里还没有 persons 的图，已打标的不会被改动'
                          : tagOverwrite
                            ? '已有标签的会重打一遍（覆盖 persons / act / emotions 等）'
                            : '已有标签的会跳过'}
                        {tagRename
                          ? ' · 改名会改文件名，贴图库按文件名检索，请确认'
                          : ' · 不改文件名'}
                      </div>

                      {tagError && (
                        <div className="tag-pop-error">
                          <AlertCircle size={12} />
                          {tagError}
                        </div>
                      )}

                      {/* 试打一张：先验证通道再批量。不写 metadata、不改文件名 */}
                      <div className="tag-probe">
                        <button
                          className="btn btn-ghost btn-sm"
                          onClick={() => void runProbe()}
                          disabled={probeBusy || !probeCandidate}
                          title={
                            probeCandidate
                              ? `只打这一张：${probeCandidate}（不写标签、不改文件名）`
                              : '这个图库没有可试的图'
                          }
                        >
                          {probeBusy ? (
                            <Loader2 size={12} className="spin" />
                          ) : (
                            <FlaskConical size={12} />
                          )}
                          试打一张
                        </button>
                        <span className="tag-probe-why">
                          只跑模型验证通道，不写 metadata、不改文件名
                        </span>
                      </div>

                      {probeError && (
                        <div className="tag-pop-error">
                          <AlertCircle size={12} />
                          {probeError}
                        </div>
                      )}

                      {probeResult && (
                        <div className="tag-probe-out">
                          <div className="tag-probe-head">
                            <b>{probeResult.name || probeCandidate}</b>
                            <span>
                              {probeResult.provider} · {probeResult.model}
                            </span>
                          </div>
                          <div className="tag-probe-raw">
                            {probeResult.raw?.trim() || '（模型没吐内容）'}
                          </div>
                          <div className="tag-probe-tags">
                            {Object.entries(probeResult.tags || {})
                              .filter(([, v]) => v)
                              .map(([k, v]) => (
                                <span key={k} className="tag-probe-tag">
                                  {k}: {String(v)}
                                </span>
                              ))}
                          </div>
                          {probeResult.rename_to && (
                            <div className="tag-probe-note">
                              打标后会改名为：{probeResult.rename_to}
                            </div>
                          )}
                        </div>
                      )}

                      <div className="tag-pop-actions">
                        <button
                          className="btn btn-ghost btn-sm"
                          onClick={() => setTagPanelOpen(false)}
                        >
                          取消
                        </button>
                        <button
                          className="btn btn-primary btn-sm"
                          onClick={() => void startTag()}
                          disabled={
                            tagBusy ||
                            (tagScope === 'pending' && tagPending === 0) ||
                            (tagScope === 'selected' && selectedNamesForTag.length === 0)
                          }
                          title={
                            tagScope === 'pending' && tagPending === 0
                              ? '没有待打的图；换「全部重打」或勾选覆盖'
                              : '开始一轮打标（后台跑，可随时停止）'
                          }
                        >
                          {tagBusy ? (
                            <Loader2 size={13} className="spin" />
                          ) : (
                            <Tags size={13} />
                          )}
                          开始打标
                        </button>
                      </div>
                    </div>
                  </>
                )}
              </div>
            )
          ) : null}
          {selected.size > 0 && (
            <button className="btn btn-ghost" onClick={() => setSelected(new Set())}>
              <X size={14} />
              取消选择
            </button>
          )}
          <button className="btn btn-ghost" onClick={load} disabled={loading}>
            <RefreshCw size={14} className={loading ? 'spin' : ''} />
            刷新
          </button>
        </div>
      </div>

      {error && (
        <div className="notice notice-error">
          <AlertCircle size={14} />
          {error}
        </div>
      )}

      {/* 打标自己的错误（启动失败 / 停止失败）：面板关着时也要能看见 */}
      {tagError && !tagPanelOpen && (
        <div className="notice notice-error">
          <AlertCircle size={14} />
          打标：{tagError}
        </div>
      )}

      {/* 打标失败的单张原因：跑完也要留着——「6 张失败」不告诉他为什么等于没说 */}
      {tagStatus?.progress.error && !tagRunning ? (
        <div className="notice notice-error">
          <AlertCircle size={14} />
          打标最近一次失败：{tagStatus.progress.error}
        </div>
      ) : null}

      {/* ── 筛选条 ── */}
      <div className="filter-bar">
        <div className="seg">
          {TOP_SOURCES.map((s) => (
            <button
              key={s.key}
              className={'seg-item' + (source === s.key ? ' is-active' : '')}
              onClick={() => {
                setSource(s.key)
                setLib('')
                setTag('')
                setPage(1)
                setSelected(new Set())
              }}
            >
              {s.label}
            </button>
          ))}
        </div>

        <div className="gallery-search">
          <Search size={14} />
          <input
            value={searchText}
            placeholder="搜文件名、标签、描述…"
            onChange={(e) => setSearchText(e.target.value)}
          />
          {searchText && (
            <button className="icon-btn" onClick={() => setSearchText('')} title="清空">
              <X size={13} />
            </button>
          )}
        </div>

        <select
          className="mini-select"
          value={state}
          onChange={(e) => {
            setState(e.target.value as '' | 'tagged' | 'untagged')
            setPage(1)
          }}
          title="标注状态"
        >
          <option value="">全部标注状态</option>
          <option value="tagged">只看已标注</option>
          <option value="untagged">只看未标注</option>
        </select>

        <select
          className="mini-select"
          value={sort}
          onChange={(e) => {
            setSort(e.target.value)
            setPage(1)
          }}
          title="排序"
        >
          {SORTS.map((s) => (
            <option key={s.key} value={s.key}>
              {s.label}
            </option>
          ))}
        </select>
      </div>

      {/* ── 贴图分库（默认 / 丛雨 / 米雪儿） ── */}
      {source === 'sticker' && stickerLibs.length > 0 && (
        <div className="lib-bar">
          <span className="lib-label">贴图库</span>
          <button
            className={'lib-chip' + (lib === '' ? ' is-active' : '')}
            onClick={() => {
              setLib('')
              setTag('')
              setPage(1)
              setSelected(new Set())
            }}
          >
            全部
            <span className="tag-count">{stickersTotal}</span>
          </button>
          {stickerLibs.map((s) => (
            <button
              key={s.key}
              className={'lib-chip' + (lib === s.key ? ' is-active' : '')}
              onClick={() => {
                setLib(s.key)
                setTag('')
                setPage(1)
                setSelected(new Set())
              }}
            >
              {s.label}
              <span className="tag-count">{s.count}</span>
            </button>
          ))}
        </div>
      )}

      {/* ── 标签 chips ── */}
      {filterTags.length > 0 && (
        <div className="tag-bar">
          <button
            className={'tag-chip' + (tag === '' ? ' is-active' : '')}
            onClick={() => {
              setTag('')
              setPage(1)
            }}
          >
            全部
          </button>
          {filterTags.map((t) => (
            <button
              key={t.tag}
              className={'tag-chip' + (tag === t.tag ? ' is-active' : '')}
              onClick={() => {
                setTag(tag === t.tag ? '' : t.tag)
                setPage(1)
              }}
            >
              {t.tag}
              <span className="tag-count">{t.count}</span>
            </button>
          ))}
          {(tag || q || state) && (
            <button className="tag-chip is-reset" onClick={resetFilters}>
              <X size={12} />
              清空筛选
            </button>
          )}
        </div>
      )}

      {/* ── 选择条 ── */}
      <div className="select-bar">
        <button className="check-all" onClick={selectAllOnPage} disabled={!items.length}>
          {allOnPageSelected ? <CheckSquare size={15} /> : <SquareIcon size={15} />}
          全选本页
        </button>
        <span className="select-hint">
          {data
            ? `共 ${data.total} 张${q || tag || state ? '（已筛选）' : ''} · 第 ${data.page}/${data.pages} 页`
            : '加载中…'}
          {selected.size > 0 && ` · 已选 ${selected.size}`}
        </span>
        {loading && <Loader2 size={14} className="spin muted" />}
      </div>

      {/* ── 网格 ── */}
      {items.length === 0 && !loading ? (
        <div className="g-empty">
          <Images size={28} />
          <div>这个条件下没有图片</div>
          <button className="btn btn-ghost" onClick={resetFilters}>
            清空筛选
          </button>
        </div>
      ) : (
        <div className="gallery-grid">
          {items.map((it, i) => {
            const k = itemKey(it)
            const isSel = selected.has(k)
            const tags = it.source === 'cg' ? it.scene_tags : it.emotions
            return (
              <div
                key={k}
                className={'g-card' + (isSel ? ' is-selected' : '')}
                onClick={() => setPreview(it)}
                onContextMenu={(e) => openCardMenu(it, e)}
              >
                {bridge && (
                  <img
                    className="g-card-img"
                    src={galleryImageUrl(bridge, it.source, it.name, 'thumb', 360)}
                    alt={it.name}
                    loading="lazy"
                    draggable={false}
                  />
                )}
                <button
                  className={'g-card-check' + (isSel ? ' is-on' : '')}
                  title="选择（Shift 连选）"
                  onClick={(e) => {
                    e.stopPropagation()
                    toggleSelect(k, i, e)
                  }}
                >
                  {isSel && <Check size={12} strokeWidth={3} />}
                </button>
                {/* 三套贴图混在一起时，标一下这张属于哪一套 */}
                {source === 'sticker' && lib === '' && (
                  <span className="g-card-lib">
                    {(data?.sources ?? []).find((s) => s.key === it.source)?.label ?? it.source}
                  </span>
                )}
                <div className="g-card-foot">
                  <span className="g-card-tags">
                    {tags.length > 0 ? (
                      tags.slice(0, 2).map((t) => (
                        <span className="g-mini-tag" key={t}>
                          {t}
                        </span>
                      ))
                    ) : (
                      <span className="g-mini-tag is-faint">未标注</span>
                    )}
                    {tags.length > 2 && (
                      <span className="g-mini-more">+{tags.length - 2}</span>
                    )}
                  </span>
                  <span className="g-card-size">{fileSize(it.size)}</span>
                </div>
              </div>
            )
          })}
        </div>
      )}

      {/* ── 分页 ── */}
      {(data?.pages ?? 1) > 1 && (
        <div className="g-pager">
          <button
            className="pager-btn"
            disabled={page <= 1}
            onClick={() => setPage(page - 1)}
            title="上一页"
          >
            <ChevronLeft size={15} />
          </button>
          {pageNumbers(page, data?.pages ?? 1).map((p, idx) =>
            p === '…' ? (
              <span className="pager-gap" key={'gap' + idx}>
                …
              </span>
            ) : (
              <button
                key={p}
                className={'pager-btn' + (p === page ? ' is-active' : '')}
                onClick={() => setPage(p)}
              >
                {p}
              </button>
            ),
          )}
          <button
            className="pager-btn"
            disabled={page >= (data?.pages ?? 1)}
            onClick={() => setPage(page + 1)}
            title="下一页"
          >
            <ChevronRight size={15} />
          </button>
        </div>
      )}

      {/* ── 批量操作条 ── */}
      {selected.size > 0 && (
        <div className="bulk-bar">
          <span className="bulk-count">{selected.size}</span>
          <span className="bulk-label">张已选</span>

          <div className="bulk-actions">
            <div className="bulk-pop-wrap">
              <button
                className={'btn btn-ghost' + (tagMenuOpen ? ' is-active' : '')}
                onClick={() => {
                  setTagMenuOpen((v) => !v)
                  setConfirmTrash(false)
                }}
              >
                <Tag size={14} />
                打标签
              </button>
              {tagMenuOpen && (
                <>
                  <div className="pop-mask" onClick={() => setTagMenuOpen(false)} />
                  <div className="pop-panel">
                    <div className="pop-title">给选中的 {selected.size} 张加标签</div>
                    <div className="pop-tags">
                      {currentTagList.slice(0, 12).map((t) => (
                        <button
                          key={t.tag}
                          className="tag-chip"
                          onClick={() => applyTag(t.tag)}
                          disabled={busy}
                        >
                          {t.tag}
                        </button>
                      ))}
                    </div>
                    <div className="pop-input">
                      <Tag size={13} />
                      <input
                        autoFocus
                        value={tagDraft}
                        placeholder="输入新标签，回车应用"
                        onChange={(e) => setTagDraft(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === 'Enter') applyTag(tagDraft)
                          if (e.key === 'Escape') setTagMenuOpen(false)
                        }}
                      />
                      <button
                        className="btn btn-primary btn-sm"
                        onClick={() => applyTag(tagDraft)}
                        disabled={!tagDraft.trim() || busy}
                      >
                        应用
                      </button>
                    </div>
                  </div>
                </>
              )}
            </div>

            {confirmTrash ? (
              <span className="bulk-confirm">
                确认移入回收站？
                <button className="btn btn-danger btn-sm" onClick={doTrash} disabled={busy}>
                  {busy ? <Loader2 size={13} className="spin" /> : <Trash2 size={13} />}
                  确认
                </button>
                <button className="btn btn-ghost btn-sm" onClick={() => setConfirmTrash(false)}>
                  取消
                </button>
              </span>
            ) : (
              <button
                className="btn btn-ghost btn-danger"
                onClick={() => {
                  setConfirmTrash(true)
                  setTagMenuOpen(false)
                }}
              >
                <Trash2 size={14} />
                移入回收站
              </button>
            )}
          </div>
        </div>
      )}

      {toast && (
        <div className="g-toast">
          <Sparkles size={13} />
          {toast}
        </div>
      )}

      <ContextMenu state={menu} onClose={() => setMenu(null)} />

      {/* ── 大图预览 ──
          布局要点：卡片宽度固定、高度随图自适应，"信息区"永远在卡片底部占满整宽。
          之前把信息区绝对定位在图片下方，宽度跟着图片走 —— 小图会把文字挤成窄条。 */}
      {preview && bridge && (
        <div className="lightbox" onClick={() => setPreview(null)}>
          <div className="lb-wrap" onClick={(e) => e.stopPropagation()}>
            <div className="lb-stage">
              <div className="lb-figure">
                <img
                  key={preview.name}
                  className="lb-img"
                  src={galleryImageUrl(bridge, preview.source, preview.name, 'file')}
                  alt={preview.name}
                  onLoad={(e) => {
                    const el = e.currentTarget
                    const box = el.getBoundingClientRect()
                    setImgSize({ w: el.naturalWidth, h: el.naturalHeight })
                    setImgBox({ w: box.width, h: box.height })
                  }}
                />
                <button
                  className="lb-nav is-prev"
                  onClick={() => stepPreview(-1)}
                  disabled={previewIndex <= 0}
                  title="上一张（←）"
                >
                  <ChevronLeft size={20} />
                </button>
                <button
                  className="lb-nav is-next"
                  onClick={() => stepPreview(1)}
                  disabled={previewIndex < 0 || previewIndex >= items.length - 1}
                  title="下一张（→）"
                >
                  <ChevronRight size={20} />
                </button>
                <button
                  className="lb-close"
                  onClick={() => setPreview(null)}
                  title="关闭（Esc）"
                >
                  <X size={16} />
                </button>
              </div>
            </div>

            {/* 信息区：跟随图片宽度，但给个下限（小贴图不至于把文字挤成窄条） */}
            <div
              className="lb-info"
              style={{
                width: imgBox ? Math.min(Math.max(imgBox.w, 620), 1120) : '100%',
              }}
            >
              <div className="lb-info-head">
                <span className="lb-name" title={preview.name}>
                  {preview.name}
                </span>
                <span className="lb-index">
                  {previewIndex + 1} / {items.length}
                </span>
              </div>
              <div className="lb-meta">
                {fileSize(preview.size)}
                {imgSize ? ` · ${imgSize.w}×${imgSize.h}` : ''} · {preview.mtime}
                {source === 'sticker' && (
                  <>
                    {' · '}
                    {(data?.sources ?? []).find((s) => s.key === preview.source)?.label ??
                      preview.source}
                  </>
                )}
              </div>
              <div className="lb-tags">
                {(preview.source === 'cg' ? preview.scene_tags : preview.emotions).map((t) => (
                  <span className="g-mini-tag" key={t}>
                    {t}
                  </span>
                ))}
                {preview.source === 'cg' &&
                  preview.emotions.map((t) => (
                    <span className="g-mini-tag is-emotion" key={'e' + t}>
                      {t}
                    </span>
                  ))}
              </div>
              {preview.desc && <div className="lb-desc">{preview.desc}</div>}
            </div>
          </div>
        </div>
      )}
    </div>
  )
}
