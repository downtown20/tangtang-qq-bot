import { useEffect, useRef, useState } from 'react'
import { RefreshCw, Trash2, Pause, Play, Search, ScrollText } from 'lucide-react'
import { subscribeLogs } from '../lib/api'
import './Page.css'
import './Logs.css'

const MAX_LINES = 3000

export default function Logs() {
  const [lines, setLines] = useState<string[]>([])
  const [paused, setPaused] = useState(false)
  const [filter, setFilter] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [connected, setConnected] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)
  const pausedRef = useRef(paused)
  pausedRef.current = paused

  useEffect(() => {
    let dispose: (() => void) | null = null
    let stopped = false

    subscribeLogs(
      (line) => {
        if (pausedRef.current) return
        setLines((prev) => {
          const next = [...prev, line]
          return next.length > MAX_LINES ? next.slice(next.length - MAX_LINES) : next
        })
      },
      (e) => setError(e.message),
    )
      .then((off) => {
        if (stopped) {
          off()
          return
        }
        dispose = off
        setConnected(true)
      })
      .catch((e: Error) => setError(e.message))

    return () => {
      stopped = true
      dispose?.()
    }
  }, [])

  useEffect(() => {
    if (boxRef.current && !paused) {
      boxRef.current.scrollTop = boxRef.current.scrollHeight
    }
  }, [lines, paused])

  const visible = filter
    ? lines.filter((l) => l.toLowerCase().includes(filter.toLowerCase()))
    : lines

  return (
    <div className="page page-logs">
      <div className="page-head">
        <div>
          <h1 className="page-title">日志</h1>
          <p className="page-sub">
            糖糖进程的实时输出（SSE）· {connected ? '已连接' : '连接中…'}
          </p>
        </div>
        <div className="page-actions">
          <div className="log-search">
            <Search size={14} />
            <input
              value={filter}
              onChange={(e) => setFilter(e.target.value)}
              placeholder="过滤关键词"
            />
          </div>
          <button className="btn btn-ghost" onClick={() => setPaused((p) => !p)}>
            {paused ? <Play size={14} /> : <Pause size={14} />}
            {paused ? '继续' : '暂停'}
          </button>
          <button className="btn btn-ghost" onClick={() => setLines([])}>
            <Trash2 size={14} />
            清空
          </button>
        </div>
      </div>

      {error && <div className="notice notice-error">{error}</div>}

      <div className="log-box" ref={boxRef}>
        {visible.length === 0 ? (
          <div className="log-empty">
            <ScrollText size={18} />
            <span>还没有日志。在「概览」里启动糖糖后，这里会实时滚动输出。</span>
          </div>
        ) : (
          visible.map((l, i) => (
            <div className="log-line" key={i}>
              {l}
            </div>
          ))
        )}
      </div>

      <div className="log-foot">
        {visible.length} 行{filter ? `（已过滤，共 ${lines.length}）` : ''}
        {paused && ' · 已暂停'}
      </div>
    </div>
  )
}
