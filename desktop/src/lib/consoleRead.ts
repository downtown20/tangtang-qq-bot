/**
 * 会客厅未读位置（2026-10-24 侧栏重构时抽出）。
 *
 * 为什么单独一个文件：会话列表从「会客厅页内左栏」搬到了「应用侧栏」，
 * 未读逻辑（key → 已读到的 last_id）现在**两个地方都要用**——
 * Sidebar 负责显示蓝点，将来别的地方也可能要标记已读。
 * 抽出来比在两个组件里各写一份可靠（复制粘贴必然漂移）。
 */

export type ReadMap = Record<string, number>

const READ_KEY = 'tangtang.console.read'

export function loadRead(): ReadMap {
  try {
    const raw = localStorage.getItem(READ_KEY)
    const parsed = raw ? JSON.parse(raw) : null
    return parsed && typeof parsed === 'object' ? (parsed as ReadMap) : {}
  } catch {
    return {}
  }
}

export function saveRead(m: ReadMap) {
  try {
    localStorage.setItem(READ_KEY, JSON.stringify(m))
  } catch {
    /* 隐私模式忽略 */
  }
}

/** 该会话是否有未读：非本地会话且 last_id 超过已读位置 */
export function hasUnread(
  kind: string,
  key: string,
  lastId: number,
  read: ReadMap,
): boolean {
  if (kind === 'desktop') return false
  return lastId > (read[key] || 0)
}
