/** 统一请求封装：拼后端地址、抛网络错误、给页脚留一句可读的说明。 */
import { getOperator } from '@/stores/session'

const API_BASE = import.meta.env.VITE_API_BASE ?? ''

export function request(path: string, init?: RequestInit): Promise<Response> {
  const url = path.startsWith('http') ? path : `${API_BASE}${path}`
  return fetch(url, {
    headers: {
      'Content-Type': 'application/json',
      // 后端按 X-Operator 判定操作人；只读接口忽略这个头。
      'X-Operator': getOperator(),
    },
    ...init,
  }).catch((error: unknown) => {
    const detail = error instanceof Error ? error.message : '请求未送达'
    throw new Error(`接口请求失败：${detail}`)
  })
}

export async function fetchJson<T>(path: string): Promise<T> {
  const response = await request(path)
  if (!response.ok) {
    throw new Error(`接口返回 ${response.status}，数据未更新`)
  }
  return (await response.json()) as T
}

export type ActionResponse = {
  ok: boolean
  message: string
  code?: string | null
  changed?: boolean | null
  entry?: Record<string, unknown> | null
  errors?: { code: string; message: string; service?: string }[]
  rolled_back?: boolean
  [key: string]: unknown
}

/** 动作接口不能只看 HTTP 状态：业务拒绝（越权、校验失败）都以可读消息返回。 */
export async function postAction<T extends ActionResponse = ActionResponse>(
  path: string,
  body: Record<string, unknown>,
): Promise<T> {
  const response = await request(path, { method: 'POST', body: JSON.stringify(body) })
  let payload: ActionResponse | null = null
  try {
    payload = (await response.json()) as ActionResponse
  } catch {
    payload = null
  }
  if (payload && typeof payload.ok === 'boolean') {
    return payload as T
  }
  if (response.status === 403) {
    throw new Error('当前操作人不是策略负责人，只有只读权限，切换已被拒绝')
  }
  throw new Error(
    (payload as { message?: string } | null)?.message ??
      `接口返回 ${response.status}，动作未生效`,
  )
}
