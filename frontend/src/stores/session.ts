/** 当前操作人：与页面顶部的「当前值班」保持一致，动作请求统一带上它。 */
import { useSyncExternalStore } from 'react'

const STORAGE_KEY = 'bus.operator'
const DEFAULT_OPERATOR = '值班管理员'

const listeners = new Set<() => void>()
let current: string | null = null

function readInitial(): string {
  if (current !== null) {
    return current
  }
  try {
    current = window.localStorage.getItem(STORAGE_KEY) || DEFAULT_OPERATOR
  } catch {
    current = DEFAULT_OPERATOR
  }
  return current
}

export function getOperator(): string {
  return readInitial()
}

export function setOperator(name: string): void {
  const next = name.trim()
  if (!next) {
    return
  }
  current = next
  try {
    window.localStorage.setItem(STORAGE_KEY, next)
  } catch {
    // 隐私模式下写不进去也不影响本次会话
  }
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener)
  return () => listeners.delete(listener)
}

export function useOperator(): string {
  return useSyncExternalStore(subscribe, getOperator, () => DEFAULT_OPERATOR)
}

export const fallbackOperator = DEFAULT_OPERATOR
