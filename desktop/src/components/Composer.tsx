import { useEffect, useRef, useState } from 'react'
import { ArrowUp, Loader2, Sparkles, Trash2, AlertCircle } from 'lucide-react'
import { api } from '../lib/api'
import ModelSwitcher from './ModelSwitcher'
import './Composer.css'

type Msg = { role: 'user' | 'assistant'; content: string }

export default function Composer({ onOpenSettings }: { onOpenSettings: () => void }) {
  const [text, setText] = useState('')
  const [messages, setMessages] = useState<Msg[]>([])
  const [loading, setLoading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const taRef = useRef<HTMLTextAreaElement>(null)
  const logRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight
  }, [messages, loading])

  const send = async () => {
    const value = text.trim()
    if (!value || loading) return
    setText('')
    setMessages((m) => [...m, { role: 'user', content: value }])
    setLoading(true)
    setError(null)
    try {
      const res = await api.chat(value)
      setMessages((m) => [...m, { role: 'assistant', content: res.reply || '（空回复）' }])
    } catch (e) {
      setError((e as Error).message)
    } finally {
      setLoading(false)
    }
  }

  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      send()
    }
  }

  return (
    <div className="composer-wrap">
      {(messages.length > 0 || loading) && (
        <div className="chat-log" ref={logRef}>
          <div className="chat-log-head">
            <span className="chat-log-title">
              <Sparkles size={13} />
              模型试话（不经过 QQ）
            </span>
            <button
              className="icon-btn"
              title="清空"
              onClick={() => {
                setMessages([])
                setError(null)
              }}
            >
              <Trash2 size={13} />
            </button>
          </div>
          {messages.map((m, i) => (
            <div key={i} className={'bubble ' + (m.role === 'user' ? 'is-user' : 'is-bot')}>
              {m.content}
            </div>
          ))}
          {loading && (
            <div className="bubble is-bot is-pending">
              <Loader2 size={13} className="spin" /> 正在生成…
            </div>
          )}
        </div>
      )}

      {error && (
        <div className="composer-error">
          <AlertCircle size={13} />
          {error}
        </div>
      )}

      <div className="composer">
        <textarea
          ref={taRef}
          rows={1}
          value={text}
          placeholder="问糖糖点什么…（Enter 发送，Shift+Enter 换行）"
          onChange={(e) => setText(e.target.value)}
          onKeyDown={onKeyDown}
        />
        <div className="composer-bar">
          <ModelSwitcher onOpenSettings={onOpenSettings} />
          <div className="composer-bar-spacer" />
          <button
            className="send-btn"
            onClick={send}
            disabled={!text.trim() || loading}
            title="发送（Enter）"
          >
            {loading ? <Loader2 size={15} className="spin" /> : <ArrowUp size={16} />}
          </button>
        </div>
      </div>
    </div>
  )
}
