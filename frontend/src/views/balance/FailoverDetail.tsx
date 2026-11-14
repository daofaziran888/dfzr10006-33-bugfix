import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'

import { fetchJson } from '@/api/client'

import {
  type FailoverDetail,
  formatPairs,
  formatShare,
  serviceWeights,
} from './model'

export default function BalanceFailoverView() {
  const { entryId } = useParams()
  const [detail, setDetail] = useState<FailoverDetail | null>(null)
  const [errorMessage, setErrorMessage] = useState('')

  const reload = useCallback(async () => {
    if (!entryId) {
      setErrorMessage('缺少策略编号')
      return
    }
    try {
      const payload = await fetchJson<FailoverDetail>(`/api/balance/${entryId}/failover`)
      setDetail(payload)
      setErrorMessage('')
    } catch (error) {
      setDetail(null)
      setErrorMessage(error instanceof Error ? error.message : '故障转移详情读取失败')
    }
  }, [entryId])

  useEffect(() => {
    void reload()
  }, [reload])

  if (errorMessage) {
    return (
      <section className="page" data-module="balance-failover">
        <header className="page-head">
          <div>
            <h2>故障转移详情</h2>
            <p className="page-desc">故障转移结论只引用当前有效服务列表。</p>
          </div>
          <Link className="btn" to="/balance">返回策略列表</Link>
        </header>
        <p className="error-text">{errorMessage}</p>
      </section>
    )
  }

  if (!detail) {
    return (
      <section className="page" data-module="balance-failover">
        <p className="muted-text">正在读取故障转移详情…</p>
      </section>
    )
  }

  const pairs = serviceWeights(detail)
  const conclusion = detail.conclusion
  const receipt = detail.receipt

  return (
    <section className="page" data-module="balance-failover">
      <header className="page-head">
        <div>
          <h2>故障转移详情 · {detail.策略编号}</h2>
          <p className="page-desc">
            结论按当前有效服务列表生成，历史回执单独保留；同一策略的重复切换不会追加结论。
          </p>
        </div>
        <div className="page-actions">
          <button className="btn" type="button" onClick={() => void reload()}>刷新</button>
          <Link className="btn ghost" to="/balance">返回策略列表</Link>
        </div>
      </header>

      <div className="stat-row">
        <article className="stat-card">
          <span className="stat-label">策略状态</span>
          <strong className="stat-value">{detail.policy.status}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">策略负责人</span>
          <strong className="stat-value small">{detail.policy.策略负责人}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">有效版本</span>
          <strong className="stat-value">v{detail.policy.version}</strong>
        </article>
        <article className="stat-card">
          <span className="stat-label">故障转移回执</span>
          <strong className="stat-value">{detail.failover_count}</strong>
        </article>
      </div>

      <section className="detail-block">
        <h3>当前有效服务权重</h3>
        <table className="data-table compact">
          <thead>
            <tr><th>服务</th><th>权重</th><th>流量占比</th></tr>
          </thead>
          <tbody>
            {pairs.map((pair, index) => (
              <tr key={`${pair.服务}-${index}`}>
                <td>{pair.服务}</td>
                <td>{pair.权重 ?? '缺失'}</td>
                <td>{conclusion?.流量分配[index] ? formatShare(conclusion.流量分配[index].流量占比) : '—'}</td>
              </tr>
            ))}
            {pairs.length === 0 && (
              <tr><td className="empty-state" colSpan={3}>暂无服务权重配置</td></tr>
            )}
          </tbody>
        </table>
        <p className="detail-note">
          服务列表与权重配置：{formatPairs(pairs)}
          {detail.config_ok
            ? <span className="badge ok">一一对应</span>
            : <span className="badge danger">配置不一致</span>}
        </p>
      </section>

      <section className="detail-block">
        <h3>故障转移结论（当前有效服务列表）</h3>
        {conclusion ? (
          <>
            <p className="conclusion-text">{conclusion.结论}</p>
            <p className="detail-note">
              来源：{conclusion.来源} · 服务列表：{conclusion.服务列表.join('、')}
            </p>
          </>
        ) : (
          <div>
            <p className="muted-text">当前服务列表与权重配置无法一一对应，暂不能给出故障转移结论：</p>
            <ul className="error-list">
              {detail.conclusion_errors.map((item, index) => (
                <li key={`${item.code}-${index}`}>{item.message}</li>
              ))}
            </ul>
          </div>
        )}
      </section>

      <section className="detail-block">
        <h3>最近一次切换回执</h3>
        {receipt ? (
          <>
            {!detail.receipt_matches_current && (
              <p className="action-feedback warning">
                最近回执引用的服务列表已与当前有效服务列表不同，以上结论以当前有效服务列表为准。
              </p>
            )}
            <table className="data-table compact">
              <tbody>
                <tr><th>回执编号</th><td>{receipt.id}</td><th>版本</th><td>v{receipt.版本}</td></tr>
                <tr><th>操作人</th><td>{receipt.操作人}</td><th>生成时间</th><td>{receipt.生成时间}</td></tr>
                <tr><th>服务列表</th><td colSpan={3}>{receipt.服务列表.join('、')}</td></tr>
                <tr><th>权重配置</th><td colSpan={3}>{formatPairs(serviceWeights({ services: receipt.服务列表, weights: receipt.权重配置 }))}</td></tr>
              </tbody>
            </table>
            <p className="detail-note">{receipt.结论}</p>
          </>
        ) : (
          <p className="muted-text">该策略尚未执行过切换，暂无故障转移回执。</p>
        )}
      </section>

      <section className="detail-block">
        <h3>故障转移历史（共 {detail.failover_count} 份）</h3>
        {detail.failover_history.length > 0 ? (
          <table className="data-table">
            <thead>
              <tr><th>回执编号</th><th>版本</th><th>操作人</th><th>生成时间</th><th>服务列表</th><th>结论</th></tr>
            </thead>
            <tbody>
              {detail.failover_history.map((item) => (
                <tr key={item.id}>
                  <td>{item.id}</td>
                  <td>v{item.版本}</td>
                  <td>{item.操作人}</td>
                  <td>{item.生成时间}</td>
                  <td>{item.服务列表.join('、')}</td>
                  <td>{item.结论}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p className="muted-text">暂无故障转移历史。</p>
        )}
      </section>
    </section>
  )
}
