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

export default function Gallery() {
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

  /* ── 图库打标（2026-10-26 第 63 轮）──
   *
   * 原来这件事靠双击 `打标色图.bat`：模型名、`--resume`、venv 路径全写死在 bat 里，
   * 换个模型要改文件。现在配置在「设置 → 图库打标」，这里只留一个按钮。
   *
   * 打标是长任务（本地模型约 1.5 秒一张，222 张就是 5 分钟），所以：
   * 点了立刻返回 + 轮询进度，**不能**同步等——否则界面假死几十秒没人知道还在跑。 */
  const [tagStatus, setTagStatus] = useState<GalleryTagStatus | null>(null)
  const [tagBusy, setTagBusy] = useState(false)

  const refreshTag = useCallback(async () => {
    try {
      setTagStatus(await api.galleryTagStatus())
    } catch {
      /* 打标是可选功能：拉不到就不显示按钮，不能把图库页搞挂 */
      setTagStatus(null)
    }
  }, [])

  useEffect(() => {
    void refreshTag()
  }, [refreshTag])

  // 只在跑的时候轮询；停下就停，别让一个隐藏的定时器永远转
  const tagRunning = Boolean(tagStatus?.progress.running)
  useEffect(() => {
    if (!tagRunning) return
    const t = setInterval(() => void refreshTag(), 2000)
    return () => clearInterval(t)
  }, [tagRunning, refreshTag])

  const startTag = useCallback(async () => {
    if (!tagStatus) return
    if (tagStatus.provider_ready) {
      setError(tagStatus.provider_ready)
      return
    }
    const pending = tagStatus.scan.pending
    // 全都打过标签时，点一下要明确问一句：增量跑不出东西，很容易被当成「坏了」
    if (pending === 0) {
      if (!window.confirm(`这个图库 ${tagStatus.scan.files} 张全都已有标签。\n\n要全部重新打一遍吗？（会覆盖现有标签）`))
        return
    }
    setTagBusy(true)
    setError(null)
    try {
      const r = await api.galleryTagRun(pending === 0)
      if (!r.ok) setError(r.error || '启动失败')
      else if (!r.started) setError(r.message || '没有需要打标的图')
      await refreshTag()
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setTagBusy(false)
    }
  }, [tagStatus, refreshTag])

  const stopTag = useCallback(async () => {
    try {
      await api.galleryTagCancel()
      await refreshTag()
    } catch (e) {
      setError((e as Error).message)
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
        source: source === 'sticker' ? lib || 'sticker_all' : 'cg',
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
  }, [bridge, source, lib, q, tag, state, sort, page])

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
          {/* 打标开关（2026-10-26）：替代双击 打标色图.bat。
              只放一个按钮——方式/模型/端点/改名全在「设置 → 图库打标」，
              这里再摆一遍配置就是两个真相。 */}
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
            ) : (
              <button
                className="btn btn-ghost"
                onClick={() => void startTag()}
                disabled={tagBusy || Boolean(tagStatus.provider_ready)}
                title={
                  tagStatus.provider_ready ||
                  `给还没有标签的 ${tagStatus.scan.pending} 张打标` +
                    `（${tagStatus.config.provider === 'api' ? '云端 API' : '本机 Ollama'} · ` +
                    `${tagStatus.config.provider === 'api' ? tagStatus.config.api_model : tagStatus.config.ollama_model}）`
                }
              >
                <Tags size={14} />
                打标{tagStatus.scan.pending > 0 ? ` ${tagStatus.scan.pending}` : ''}
              </button>
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
