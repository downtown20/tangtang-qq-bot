import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from './api'

/** 后台兜底轮询间隔：只是「别人在别处启停了」的兜底，不是主路径 */
const POLL_MS = 15000
/** 起停之后等状态真的翻转的上限。本地 `python main.py` 一般 3~8 秒 */
const FLIP_TIMEOUT_MS = 12000
const FLIP_STEP_MS = 500

export type BotControl = {
  running: boolean
  pid: number | null
  busy: boolean
  error: string | null
  /** 状态没在期望时间内翻转时的提示。**不是错误**：进程可能还在起 */
  pending: string | null
  refresh: () => Promise<boolean>
  toggle: () => Promise<void>
}

/**
 * 糖糖本体的启停 —— 概览页和左上角共用这一个实现（2026-10-27）。
 *
 * ## 为什么要抽出来
 *
 * 两个入口各自写一套，必然漂移。这次的实际症状是「左上角点了像没反应」：
 * 它调完 API 只做了一个**不 await** 的 `refreshStatus()`，而 `python main.py`
 * 起来要好几秒 —— UI 立刻回到「启动糖糖」，看着就是没生效。
 *
 * ## 真正的修法
 *
 * 不是改文案，是**等状态真的翻转再解除 busy**：每 500ms 读一次 `/api/status`，
 * 最多等 12 秒。超时也**不算失败**（进程可能还在起），给一句
 * 「已发出启动命令，但还没看到进程」让人去翻日志 —— 比假装成功诚实得多。
 */
export function useBotControl(): BotControl {
  const [running, setRunning] = useState(false)
  const [pid, setPid] = useState<number | null>(null)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [pending, setPending] = useState<string | null>(null)

  // 组件卸载后别再 setState（轮询还在飞的时候切页会报警告）
  const alive = useRef(true)
  useEffect(() => {
    alive.current = true
    return () => {
      alive.current = false
    }
  }, [])

  const refresh = useCallback(async () => {
    try {
      const s = await api.status()
      const now = Boolean(s.bot.running)
      if (alive.current) {
        setRunning(now)
        setPid(s.bot.pid ?? null)
      }
      return now
    } catch {
      // 桥没起时不要清状态：那是「看不到」，不是「没在跑」
      return false
    }
  }, [])

  useEffect(() => {
    void refresh()
    const t = setInterval(() => void refresh(), POLL_MS)
    return () => clearInterval(t)
  }, [refresh])

  const toggle = useCallback(async () => {
    setBusy(true)
    setError(null)
    setPending(null)
    try {
      const wasRunning = await refresh()
      if (wasRunning) await api.botStop()
      else await api.botStart()

      const want = !wasRunning
      let flipped = false
      for (let waited = 0; waited < FLIP_TIMEOUT_MS; waited += FLIP_STEP_MS) {
        await new Promise((r) => setTimeout(r, FLIP_STEP_MS))
        if (!alive.current) return
        if ((await refresh()) === want) {
          flipped = true
          break
        }
      }
      if (!flipped) {
        setPending(
          wasRunning
            ? '已发出停止命令，但进程还在 —— 再等等，或去看日志'
            : '已发出启动命令，但 12 秒内没看到进程 —— 去看日志里报了什么',
        )
      }
    } catch (e) {
      setError((e as Error).message)
    } finally {
      if (alive.current) setBusy(false)
    }
  }, [refresh])

  return { running, pid, busy, error, pending, refresh, toggle }
}
