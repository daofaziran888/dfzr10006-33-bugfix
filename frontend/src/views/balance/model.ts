/** 负载均衡策略的共享类型与「服务列表 ↔ 权重配置」口径。
 *
 * 列表页与故障转移详情页都从这里取服务权重，避免两个页面各算一套
 * 造成同一策略显示两份不一致的服务权重。
 */
export type ServiceWeight = {
  服务: string
  权重: number | null
}

export type TrafficAllocation = {
  服务: string
  权重: number
  流量占比: number
}

export type ConfigError = {
  code: string
  message: string
  service?: string
}

export type FailoverReceipt = {
  id: string
  序号: number
  版本: number
  策略编号: string
  策略名称: string
  触发动作: string
  操作人: string
  生成时间: string
  服务列表: string[]
  权重配置: Record<string, number>
  流量分配: TrafficAllocation[]
  结论: string
}

export type Conclusion = {
  ok: boolean
  来源: string
  策略编号: string
  服务列表: string[]
  权重配置: Record<string, number>
  流量分配: TrafficAllocation[]
  结论: string
}

export type BalanceRow = {
  id: number
  status: string
  策略编号: string
  策略名称: string
  均衡算法: string
  策略负责人: string
  服务列表: string
  权重配置: string
  故障转移: string
  健康检查: string
  策略状态: string
  version: number
  updated_at?: string | null
  services: string[]
  weights: Record<string, number>
  service_weights: ServiceWeight[]
  traffic_allocation: TrafficAllocation[]
  config_ok: boolean
  config_errors: ConfigError[]
  failover: FailoverReceipt | null
  failover_count: number
  failover_history?: FailoverReceipt[]
  pending: boolean
  abnormal: boolean
}

export type FailoverDetail = BalanceRow & {
  policy: {
    id: number
    策略编号: string
    策略名称: string
    策略负责人: string
    status: string
    version: number
  }
  conclusion: Conclusion | null
  conclusion_errors: ConfigError[]
  receipt: FailoverReceipt | null
  receipt_matches_current: boolean
  failover_history: FailoverReceipt[]
  failover_count: number
}

export type PreflightResult = {
  id: number
  策略编号: string
  策略负责人: string
  ok: boolean
  services: string[]
  weights: Record<string, number>
  service_weights: ServiceWeight[]
  traffic_allocation: TrafficAllocation[]
  errors: ConfigError[]
}

/** 服务权重只认后端返回的 service_weights，缺字段时才退回 services/weights 重建。 */
export function serviceWeights(row: {
  service_weights?: ServiceWeight[]
  services?: string[]
  weights?: Record<string, number>
}): ServiceWeight[] {
  if (Array.isArray(row.service_weights) && row.service_weights.length > 0) {
    return row.service_weights
  }
  const services = Array.isArray(row.services) ? row.services : []
  const weights = row.weights ?? {}
  const pairs = services.map((service) => ({
    服务: service,
    权重: Object.prototype.hasOwnProperty.call(weights, service) ? weights[service] : null,
  }))
  for (const service of Object.keys(weights)) {
    if (!services.includes(service)) {
      pairs.push({ 服务: service, 权重: weights[service] })
    }
  }
  return pairs
}

/** 一一对应校验：服务列表与权重配置必须同名、同数量、同顺序覆盖。 */
export function validatePairs(pairs: ServiceWeight[]): string[] {
  const problems: string[] = []
  if (pairs.length === 0) {
    problems.push('服务列表为空，至少需要登记一个服务')
    return problems
  }
  const seen = new Set<string>()
  for (const pair of pairs) {
    if (!pair.服务) {
      problems.push('服务列表存在空服务名')
      continue
    }
    if (seen.has(pair.服务)) {
      problems.push(`服务列表存在重复服务「${pair.服务}」`)
    }
    seen.add(pair.服务)
    if (pair.权重 === null || pair.权重 === undefined) {
      problems.push(`服务「${pair.服务}」缺少权重配置`)
    } else if (pair.权重 < 0) {
      problems.push(`服务「${pair.服务}」的权重不能为负数`)
    }
  }
  const total = pairs.reduce((sum, pair) => sum + (pair.权重 ?? 0), 0)
  if (!problems.length && total <= 0) {
    problems.push('权重配置总和必须大于 0')
  }
  return problems
}

export function formatPairs(pairs: ServiceWeight[]): string {
  return pairs
    .map((pair) => `${pair.服务}:${pair.权重 ?? '缺失'}`)
    .join('、')
}

export function formatShare(value: number): string {
  return `${value.toFixed(2)}%`
}
