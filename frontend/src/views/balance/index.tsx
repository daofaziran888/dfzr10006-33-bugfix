import { useCallback, useEffect, useMemo, useState } from 'react'
import { Link } from 'react-router-dom'

import { postAction, request } from '@/api/client'
import { useOperator } from '@/stores/session'

import {
  type BalanceRow,
  type PreflightResult,
  formatPairs,
  formatShare,
  serviceWeights,
  validatePairs,
} from './model'

const ENDPOINT = '/api/balance'
const columns = [
  "策略编号",
  "策略名称",
  "均衡算法",
  "服务列表",
  "权重配置",
  "故障转移",
  "健康检查",
  "策略状态"
]
const actions = [
  "启用策略",
  "切换策略",
  "关闭策略"
]
const statuses = [
  "草稿",
  "生效中",
  "已切换",
  "已关闭"
]

type ActionFeedback = {
  tone: 'success' | 'error' | 'warning'
  text: string
}

export default function BalanceView() {
  const operator = useOperator()
  const [rows, setRows] = useState<BalanceRow[]>([])
  const [total, setTotal] = useState(0)
  const [filters, setFilters] = useState<Record<string, string>>({})
  const [feedback, setFeedback] = useState<ActionFeedback | null>(null)
  const [preflight, setPreflight] = useState<PreflightResult | null>(null)
  const [pending, setPending] = useState<string>('')
  const [currentPage, setCurrentPage] = useState(1)
  const pageSize = 20

  const filterFields = useMemo(() => columns.slice(0, 3), [])

  const reload = useCallback(async () => {
    const query = new URLSearchParams({
      ...filters,
      page: String(currentPage),
      size: String(pageSize),
    }).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('均衡策略列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setFeedback({
        tone: 'error',
        text: error instanceof Error ? error.message : '负载均衡策略列表读取失败',
      })
    }
  }, [filters, currentPage])

  useEffect(() => {
    void reload()
  }, [reload])

  async function runPreflight(row: BalanceRow) {
    setPending(`preflight-${row.id}`)
    setFeedback(null)
    try {
      const response = await request(`${ENDPOINT}/${row.id}/preflight`)
      const payload = await response.json()
      if (!response.ok) {
        throw new Error(payload?.detail ?? '切换前预检读取失败')
      }
      setPreflight(payload as PreflightResult)
    } catch (error) {
      setFeedback({
        tone: 'error',
        text: error instanceof Error ? error.message : '切换前预检读取失败',
      })
    } finally {
      setPending('')
    }
  }

  async function runAction(action: string, row: BalanceRow) {
    setPending(`${action}-${row.id}`)
    setFeedback(null)
    try {
      const isOwner = row.策略负责人 === operator
      if (!isOwner) {
        setFeedback({
          tone: 'error',
          text: `切换已拦截：仅策略负责人「${row.策略负责人}」可执行「${action}」，当前操作人「${operator}」只有只读权限`,
        })
        return
      }
      if (action === '切换策略') {
        const pairs = serviceWeights(row)
        const problems = validatePairs(pairs)
        if (problems.length > 0) {
          setFeedback({ tone: 'error', text: `切换已拦截：${problems.join('；')}，服务列表与流量分配保持原值` })
          return
        }
        // 切换前先跑服务端预检：两侧校验口径一致，配置不一致时不发起切换。
        const preflightResponse = await request(`${ENDPOINT}/${row.id}/preflight`)
        const preflightPayload = await preflightResponse.json() as PreflightResult
        if (!preflightResponse.ok) {
          throw new Error((preflightPayload as unknown as { detail?: string })?.detail ?? '切换前预检读取失败')
        }
        if (!preflightPayload.ok) {
          const reasons = (preflightPayload.errors ?? []).map((item) => item.message).join('；')
          setFeedback({
            tone: 'error',
            text: `切换已拦截：${reasons || '切换前预检未通过'}，服务列表与流量分配保持原值`,
          })
          setPreflight(preflightPayload)
          return
        }
        setPreflight(preflightPayload)
      }
      const result = await postAction(`${ENDPOINT}/${row.id}/actions`, {
        action,
        operator,
      })
      if (!result.ok) {
        const details = (result.errors ?? []).map((item) => item.message)
        const text = details.length > 0
          ? `${result.message}（${details.join('；')}）`
          : result.message
        setFeedback({ tone: 'warning', text: result.rolled_back ? `${text}，流量分配已回滚` : text })
        return
      }
      setFeedback({
        tone: 'success',
        text: result.code === 'already_switched'
          ? result.message
          : `${result.message}（当前操作人 ${operator}，策略负责人 ${row.策略负责人}）`,
      })
      await reload()
    } catch (error) {
      setFeedback({
        tone: 'error',
        text: error instanceof Error ? error.message : '负载均衡策略操作失败',
      })
    } finally {
      setPending('')
    }
  }

  const stats = useMemo(() => {
    return [
      { label: '均衡策略总数', value: total },
      { label: '生效中策略', value: rows.filter((row) => row.status === '生效中').length },
      { label: '已切换策略', value: rows.filter((row) => row.status === '已切换').length },
    ]
  }, [rows, total])

  return (
    <section className="page" data-module="balance">
      <header className="page-head">
        <div>
          <h2>负载均衡策略管理</h2>
          <p className="page-desc">
            维护负载均衡的算法选择、服务列表与故障转移策略。只有策略负责人可以切换策略，
            其他人员只读；切换前会校验服务列表与权重配置一一对应。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setFeedback({ tone: 'warning', text: '均衡策略登记入口尚未接入审批流' })}>
            登记均衡策略
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出负载均衡策略清单
          </button>
        </div>
      </header>

      <div className="stat-row">
        {stats.map((item) => (
          <article key={item.label} className="stat-card">
            <span className="stat-label">{item.label}</span>
            <strong className="stat-value">{item.value}</strong>
          </article>
        ))}
      </div>

      <form className="filter-bar" onSubmit={(event) => { event.preventDefault(); void reload() }}>
        {filterFields.map((field) => (
          <label key={field} className="filter-item">
            <span>{field}</span>
            <input
              value={filters[field] ?? ''}
              placeholder={`按${field}检索`}
              onChange={(event) => setFilters((prev) => ({ ...prev, [field]: event.target.value }))}
            />
          </label>
        ))}
        <button className="btn" type="submit">查询</button>
        <button className="btn ghost" type="button" onClick={() => { setFilters({}); setCurrentPage(1) }}>重置条件</button>
      </form>

      {feedback && (
        <p className={`action-feedback ${feedback.tone}`} role="status">{feedback.text}</p>
      )}

      {preflight && (
        <div className="preflight-card">
          <header>
            <strong>切换前预检 · {preflight.策略编号}</strong>
            <button className="link" type="button" onClick={() => setPreflight(null)}>关闭</button>
          </header>
          <p className={preflight.ok ? 'ok-text' : 'error-text'}>
            {preflight.ok ? '服务列表与权重配置一一对应，可以切换。' : preflight.errors.map((item) => item.message).join('；')}
          </p>
          <table className="data-table compact">
            <thead>
              <tr><th>服务</th><th>权重</th><th>流量占比</th></tr>
            </thead>
            <tbody>
              {preflight.service_weights.map((pair, index) => (
                <tr key={`${pair.服务}-${index}`}>
                  <td>{pair.服务}</td>
                  <td>{pair.权重 ?? '缺失'}</td>
                  <td>{preflight.traffic_allocation[index] ? formatShare(preflight.traffic_allocation[index].流量占比) : '—'}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>策略负责人</th>
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => {
            const isOwner = row.策略负责人 === operator
            const pairs = serviceWeights(row)
            return (
              <tr key={String(row.id)}>
                {columns.map((column) => {
                  const value = (row as Record<string, unknown>)[column]
                  if (column === '服务列表') {
                    return <td key={column}>{row.services.length > 0 ? row.services.join('、') : (row.服务列表 || '—')}</td>                  }
                  if (column === '权重配置') {
                    return (
                      <td key={column}>
                        <span>{formatPairs(pairs)}</span>
                        {!row.config_ok && <span className="badge danger">配置不一致</span>}
                        {row.config_ok && <span className="badge ok">一一对应</span>}
                      </td>
                    )
                  }
                  if (column === '故障转移') {
                    return (
                      <td key={column}>
                        <span title={row.故障转移}>{row.故障转移 || '未生成'}</span>
                        {row.failover_count > 0 && <span className="badge neutral">{row.failover_count} 份回执</span>}
                      </td>
                    )
                  }
                  return <td key={column}>{value ? String(value) : '—'}</td>
                })}
                <td>
                  {row.策略负责人}
                  {isOwner
                    ? <span className="badge ok">负责人</span>
                    : <span className="badge neutral">只读</span>}
                </td>
                <td className="row-actions">
                  {actions.map((action) => (
                    <button
                      key={action}
                      className="link"
                      type="button"
                      disabled={pending === `${action}-${row.id}`}
                      title={isOwner ? undefined : `仅策略负责人「${row.策略负责人}」可执行，当前操作人只读`}
                      onClick={() => void runAction(action, row)}
                    >
                      {action}
                    </button>
                  ))}
                  <button
                    className="link"
                    type="button"
                    onClick={() => void runPreflight(row)}
                  >
                    切换预检
                  </button>
                  <Link className="link" to={`/balance/${row.id}/failover`}>故障转移</Link>
                </td>
              </tr>
            )
          })}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 2}>暂无负载均衡策略数据，可先登记均衡策略</td>
            </tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条负载均衡策略记录
          {total > pageSize && (
            <>
              <button className="link page-step" type="button" disabled={currentPage <= 1} onClick={() => setCurrentPage((page) => page - 1)}>上一页</button>
              <span>第 {currentPage} / {Math.max(1, Math.ceil(total / pageSize))} 页</span>
              <button className="link page-step" type="button" disabled={currentPage >= Math.ceil(total / pageSize)} onClick={() => setCurrentPage((page) => page + 1)}>下一页</button>
            </>
          )}
        </span>
        {!rows.some((row) => row.策略负责人 === operator) && (
          <span className="muted-text">当前操作人「{operator}」不是本页任一策略的负责人，仅可查看。</span>
        )}
      </footer>
    </section>
  )
}
