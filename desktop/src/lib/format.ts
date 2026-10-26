/**
 * 纯格式化工具（2026-10-25 第 17 轮建，第 45 轮扩充）。
 *
 * ## 为什么集中在这里
 *
 * 第 17 轮把 `formatMs` 从 `TurnReplay.tsx` 提出来时就吃过一次亏：
 * 同一个耗时在两处各写一份，必然漂移（同一回合一处显示 `3.1s`、一处 `3100ms`）。
 *
 * 第 45 轮做「糖糖状态」时，会客厅右栏和 Dashboard 要显示**同一批字段**
 * （运行时长 / 亲密度 / 能量档位 / 体验文案），如果各自再写一份，
 * 就会出现「右栏写『低』、概览写『偏低』」——用户会以为是两个不同的东西。
 * 所以这些**显示口径**统一在这里，两处都 import。
 *
 * ## 边界
 *
 * 这里只做「把后端字段变成人能读的字」，**不做任何业务判断**
 * （什么算健康、什么算异常是后端的事）。所有函数失败一律降级成 `—`，
 * 不抛错——状态面板不该因为一个字段怪就整块塌。
 */

/** 88ms / 3.1s —— 小于 1 秒用毫秒，正好是工具耗时的心智单位 */
export function formatMs(ms: number): string {
  if (!Number.isFinite(ms) || ms < 0) return '—'
  if (ms < 1000) return `${Math.round(ms)}ms`
  const s = ms / 1000
  return Number.isInteger(s) ? `${s}s` : `${s.toFixed(1)}s`
}

/** 运行时长：`2 小时 13 分` / `47 分钟` / `刚刚启动` */
export function fmtUptime(sec: number): string {
  if (!Number.isFinite(sec) || sec <= 0) return '刚刚启动'
  const s = Math.floor(sec)
  const h = Math.floor(s / 3600)
  const m = Math.floor((s % 3600) / 60)
  if (h > 0) return `${h} 小时 ${m} 分`
  if (m > 0) return `${m} 分钟`
  return `${s} 秒`
}

/** 亲密度：后端可能给 0~1 也可能给 0~100，显示口径统一成 0~100 整数 */
export function fmtCloseness(v: number): string {
  if (!Number.isFinite(v)) return '—'
  return Math.abs(v) <= 1.001 ? String(Math.round(v * 100)) : String(Math.round(v))
}

/**
 * `self.energy` 是**档位字符串**（'low' / 'normal' / 'high'），不是数值。
 * 只做中文映射，**不要拿它画条形图**（没有量程，画出来是假刻度）。
 */
export function energyLabel(raw: string): string {
  const k = (raw || '').trim().toLowerCase()
  if (k === 'low') return '偏低'
  if (k === 'normal' || k === 'mid' || k === 'medium') return '正常'
  if (k === 'high') return '充沛'
  return raw || '—'
}

/** 字符串时间（'2026-10-14 13:58'）省掉年份 —— 侧栏只有 320px 宽 */
export function shortStamp(raw: string): string {
  if (!raw) return ''
  const m = raw.match(/^\d{4}-(\d{2}-\d{2})[ T]?(\d{2}:\d{2})?/)
  if (m) return m[2] ? `${m[1]} ${m[2]}` : m[1]
  return raw
}

/** 体验记录可能已经带昵称，避免「@某人：@某人 说…」这种重复 */
export function expText(x: { summary: string; nickname: string }): string {
  if (!x.nickname) return x.summary
  return x.summary.includes(x.nickname) ? x.summary : `${x.nickname}：${x.summary}`
}

/**
 * `self.mood_tint` 是英文枚举（`agent/self_state.py`：warm / neutral / tired /
 * restless / bright），直接显示 `tired` 对一个不写代码的运营者等于没解释。
 * 值域以外原样返回——后端加了新枚举时界面显示原文，不会变成空白。
 */
export function moodTintLabel(raw: string): string {
  const k = (raw || '').trim().toLowerCase()
  const map: Record<string, string> = {
    warm: '温暖',
    neutral: '平和',
    tired: '疲惫',
    restless: '躁动',
    bright: '明朗',
  }
  return map[k] ?? (raw || '—')
}

/**
 * 0~100 的三档口语化（**只是显示分档，不是业务判据**）。
 *
 * 后端给的是裸数值，没有任何「多少算低」的定义；这里按三等分给一个
 * 人能读的词，让「精力 31」不至于悬空。真要定业务阈值，那是后端的事
 * ——不要在前端偷偷改口径（教训 #48：判据只许有一个实现点）。
 */
export function tier100(
  v: number | null | undefined,
  labels: { low: string; mid: string; high: string },
): { text: string; tone: 'low' | 'mid' | 'high' } {
  if (v == null || !Number.isFinite(v)) return { text: '—', tone: 'mid' }
  if (v < 35) return { text: labels.low, tone: 'low' }
  if (v < 65) return { text: labels.mid, tone: 'mid' }
  return { text: labels.high, tone: 'high' }
}

/**
 * 把 0~1（或 0~100）的标量夹到百分比宽度。满值由调用方给定——
 * **不猜尺度**，免得 0~100 里的「1 分」被当成 100% 满格。
 */
export function pctOf(value: number, full: number): number {
  if (!Number.isFinite(value) || !Number.isFinite(full) || full <= 0) return 0
  return Math.max(0, Math.min(1, value / full)) * 100
}

/**
 * token / 字符数（第 47 轮账本用）。
 *
 * 一万以下原样显示——`5954` 是这个项目里最常被核对的数字（工具定义的固定开销），
 * 缩成 `6.0k` 会让人没法跟日志里的原值对上。一万以上才缩写，因为那时精度
 * 已经不重要，宽度更紧。
 */
export function fmtTokens(n: number): string {
  if (!Number.isFinite(n) || n < 0) return '—'
  if (n < 10000) return String(Math.round(n))
  return `${(n / 1000).toFixed(1)}k`
}
