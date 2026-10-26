// config 的 path 读写工具（schema 驱动的设置表单共用）。
// 只做不可变更新：任何一次编辑都返回新对象，方便 React 判断 dirty。

export function getAt(obj: unknown, path: string): unknown {
  return path
    .split('.')
    .reduce<unknown>((node, key) => {
      if (node && typeof node === 'object' && key in (node as object)) {
        return (node as Record<string, unknown>)[key]
      }
      return undefined
    }, obj)
}

export function setAt<T>(obj: T, path: string, value: unknown): T {
  const keys = path.split('.')
  const clone = (node: unknown): unknown =>
    Array.isArray(node) ? [...node] : { ...(node as Record<string, unknown>) }

  const root = clone(obj ?? {})
  let cursor = root as Record<string, unknown>
  for (let i = 0; i < keys.length - 1; i++) {
    const key = keys[i]
    const next = cursor[key]
    cursor[key] = next && typeof next === 'object' ? clone(next) : {}
    cursor = cursor[key] as Record<string, unknown>
  }
  cursor[keys[keys.length - 1]] = value
  return root as T
}

/** 按字段类型把 UI 字符串转回配置里的真实类型。 */
export function coerce(type: string, raw: string): unknown {
  if (type === 'int') {
    const n = parseInt(raw, 10)
    return Number.isNaN(n) ? 0 : n
  }
  if (type === 'float') {
    const n = parseFloat(raw)
    return Number.isNaN(n) ? 0 : n
  }
  return raw
}
