import { useCallback, useEffect, useMemo, useState } from 'react'

import { request } from '@/api/client'

const ENDPOINT = '/api/cache'
const columns = [
  "策略编号",
  "关联服务",
  "缓存键模板",
  "过期秒数",
  "淘汰算法",
  "预热策略",
  "命中率阈值",
  "最大条目"
]
const actions = [
  "提交校验",
  "启用缓存",
  "停用缓存"
]
const stats = [
  {
    "label": "缓存策略总数",
    "value": 0
  },
  {
    "label": "已启用策略",
    "value": 0
  },
  {
    "label": "待校验策略",
    "value": 0
  }
]

type Row = Record<string, string | number | null>
type AnyRow = Record<string, any>

interface ChainResult {
  查询编号: string
  缓存键: string
  命中标识: string
  查询结果版本: string
  转换模板版本: string
  数据范围: string
  优先级说明: string
}

const hitText = (value: unknown) => `${(Number(value ?? 0) * 100).toFixed(1)}%`

export default function CacheView() {
  const [rows, setRows] = useState<Row[]>([])
  const [total, setTotal] = useState(0)
  const [errorMessage, setErrorMessage] = useState('')
  const [filters, setFilters] = useState<Record<string, string>>({})
  const filterFields = useMemo(() => columns.slice(0, 3), [])

  // 版本感知缓存：契约、条目、失效记录、数据集与执行结果
  const [contracts, setContracts] = useState<AnyRow[]>([])
  const [entries, setEntries] = useState<AnyRow[]>([])
  const [invalidations, setInvalidations] = useState<AnyRow[]>([])
  const [dataset, setDataset] = useState<AnyRow[]>([])
  const [datasetSummary, setDatasetSummary] = useState<AnyRow>({})
  const [queryOptions, setQueryOptions] = useState<AnyRow[]>([])
  const [selectedQueries, setSelectedQueries] = useState<string[]>(['QUER-ORDER'])
  const [simulateFailure, setSimulateFailure] = useState(false)
  const [chainResults, setChainResults] = useState<ChainResult[]>([])
  const [chainMessage, setChainMessage] = useState('')
  const [publishForm, setPublishForm] = useState({
    策略编号: 'CACH-0002',
    查询结果版本: 'qv2',
    转换模板版本: 'tv2',
    变更说明: '转换模板增加金额字段 amount',
    结果内容: '{"order_id": "1001", "status": "已发货", "amount": 236.5}',
  })

  const reload = useCallback(async () => {
    setErrorMessage('')
    const query = new URLSearchParams(filters).toString()
    try {
      const response = await request(`${ENDPOINT}?${query}`)
      if (!response.ok) {
        throw new Error('缓存策略列表读取失败')
      }
      const payload = await response.json()
      setRows(payload.items ?? [])
      setTotal(payload.total ?? 0)
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '结果缓存列表读取失败')
    }
  }, [filters])

  const reloadVersion = useCallback(async () => {
    try {
      const [contractRes, entryRes, invRes, datasetRes, queryRes] = await Promise.all([
        request(`${ENDPOINT}/version/contracts`),
        request(`${ENDPOINT}/version/entries`),
        request(`${ENDPOINT}/version/invalidations`),
        request(`${ENDPOINT}/version/dataset`),
        request(`${ENDPOINT}/version/queries`),
      ])
      const [contractData, entryData, invData, datasetData, queryData] = await Promise.all([
        contractRes.json(), entryRes.json(), invRes.json(), datasetRes.json(), queryRes.json(),
      ])
      setContracts(contractData.items ?? [])
      setEntries(entryData.items ?? [])
      setInvalidations(invData.items ?? [])
      setDataset(datasetData.items ?? [])
      setDatasetSummary(datasetData['汇总'] ?? {})
      setQueryOptions(queryData.items ?? [])
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '版本缓存数据读取失败')
    }
  }, [])

  useEffect(() => {
    void reload()
  }, [reload])

  useEffect(() => {
    void reloadVersion()
  }, [reloadVersion])

  async function runAction(action: string, row: Row) {
    setErrorMessage('')
    try {
      const response = await request(`${ENDPOINT}/${row.id}/actions`, {
        method: 'POST',
        body: JSON.stringify({ action }),
      })
      if (!response.ok) {
        throw new Error('结果缓存动作未生效，请稍后重试')
      }
      await reload()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '结果缓存操作失败')
    }
  }

  function toggleQuery(qid: string) {
    setSelectedQueries((prev) =>
      prev.includes(qid) ? prev.filter((item) => item !== qid) : [...prev, qid],
    )
  }

  async function runChain(kind: 'execute' | 'warmup') {
    setErrorMessage('')
    setChainMessage('')
    try {
      const response = await request(`${ENDPOINT}/version/${kind}`, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            查询编号列表: selectedQueries,
            批次说明: kind === 'warmup' ? '缓存预热' : '手动执行',
            模拟写入失败: simulateFailure,
          },
        }),
      })
      const payload = await response.json()
      setChainMessage(payload.message ?? '')
      if (payload.ok && payload.entry) {
        setChainResults(payload.entry['结果列表'] ?? [])
      } else {
        setChainResults([])
        if (!payload.ok) setErrorMessage(payload.message ?? '数据链执行失败')
      }
      await reloadVersion()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '数据链执行失败')
    }
  }

  async function runPublish() {
    setErrorMessage('')
    setChainMessage('')
    let content: AnyRow | undefined
    if (publishForm.结果内容.trim()) {
      try {
        content = JSON.parse(publishForm.结果内容)
      } catch {
        setErrorMessage('新查询结果内容不是合法 JSON，未提交')
        return
      }
    }
    try {
      const response = await request(`${ENDPOINT}/version/publish`, {
        method: 'POST',
        body: JSON.stringify({
          values: {
            策略编号: publishForm.策略编号,
            查询结果版本: publishForm.查询结果版本,
            转换模板版本: publishForm.转换模板版本,
            变更说明: publishForm.变更说明,
            新查询结果: content ? { 结果内容: content, 结果字段: Object.keys(content) } : undefined,
          },
        }),
      })
      const payload = await response.json()
      setChainMessage(payload.message ?? '')
      if (!payload.ok) setErrorMessage(payload.message ?? '版本发布失败')
      await reloadVersion()
    } catch (error) {
      setErrorMessage(error instanceof Error ? error.message : '版本发布失败')
    }
  }

  return (
    <section className="page" data-module="cache">
      <header className="page-head">
        <div>
          <h2>结果缓存管理</h2>
          <p className="page-desc">维护结果缓存策略的输入、执行约束与结果记录，为结果缓存提供可验证的数据工程流程。</p>
        </div>
        <div className="page-actions">
          <button className="btn primary" type="button" onClick={() => setErrorMessage('缓存策略登记入口尚未接入审批流')}>
            登记缓存策略
          </button>
          <button className="btn" type="button" onClick={() => window.open(`${ENDPOINT}/export`, '_blank')}>
            导出结果缓存清单
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
        <button className="btn ghost" type="button" onClick={() => setFilters({})}>重置条件</button>
      </form>

      <table className="data-table">
        <thead>
          <tr>
            {columns.map((column) => <th key={column}>{column}</th>)}
            <th>可执行动作</th>
          </tr>
        </thead>
        <tbody>
          {rows.map((row) => (
            <tr key={String(row.id)}>
              {columns.map((column) => <td key={column}>{row[column] ?? '—'}</td>)}
              <td className="row-actions">
                {actions.map((action) => (
                  <button key={action} className="link" type="button" onClick={() => void runAction(action, row)}>
                    {action}
                  </button>
                ))}
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td className="empty-state" colSpan={columns.length + 1}>暂无结果缓存数据，可先登记缓存策略</td>
            </tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">缓存策略契约（查询入口读取当前有效缓存版本与命中率）</h3>
      <div className="stat-row">
        {contracts.map((contract) => (
          <article key={String(contract['策略编号'])} className="stat-card">
            <span className="stat-label">
              {contract['策略编号']} · {contract['关联查询']}
            </span>
            <strong className="stat-value">{hitText(contract['命中率'])}</strong>
            <span className="stat-label">
              当前版本 {contract['当前查询结果版本']}+{contract['当前转换模板版本']} ·
              命中 {contract['累计命中']} / 未命中 {contract['累计未命中']} ·
              失效 {contract['失效次数']} 次
            </span>
          </article>
        ))}
        {contracts.length === 0 && (
          <article className="stat-card"><span className="stat-label">暂无缓存策略契约</span></article>
        )}
      </div>

      <h3 className="section-title">版本化数据链（整组执行，逐条返回；事务失败整体回滚）</h3>
      <div className="filter-bar">
        <span className="filter-item"><span>批量多选查询</span></span>
        {queryOptions.map((option) => (
          <label key={String(option['查询编号'])} className="check-item">
            <input
              type="checkbox"
              checked={selectedQueries.includes(String(option['查询编号']))}
              onChange={() => toggleQuery(String(option['查询编号']))}
            />
            {option['查询编号']} {option['查询名称']}
          </label>
        ))}
        <label className="check-item">
          <input
            type="checkbox"
            checked={simulateFailure}
            onChange={(event) => setSimulateFailure(event.target.checked)}
          />
          模拟写入失败（验证整体回滚）
        </label>
        <button className="btn primary" type="button" onClick={() => void runChain('execute')}>执行数据链</button>
        <button className="btn" type="button" onClick={() => void runChain('warmup')}>缓存预热</button>
      </div>
      {chainMessage && <p className="chain-message">{chainMessage}</p>}
      {chainResults.length > 0 && (
        <table className="data-table">
          <thead>
            <tr>
              <th>查询编号</th><th>缓存键</th><th>命中标识</th>
              <th>查询结果版本</th><th>转换模板版本</th><th>数据范围</th><th>优先级说明</th>
            </tr>
          </thead>
          <tbody>
            {chainResults.map((item, index) => (
              <tr key={`${item.缓存键}-${index}`}>
                <td>{item.查询编号}</td>
                <td>{item.缓存键}</td>
                <td>{item.命中标识}</td>
                <td>{item.查询结果版本}</td>
                <td>{item.转换模板版本}</td>
                <td>{item.数据范围}</td>
                <td>{item.优先级说明}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}

      <h3 className="section-title">发布新版本（旧缓存明确失效并写入缓存策略契约）</h3>
      <form
        className="filter-bar"
        onSubmit={(event) => { event.preventDefault(); void runPublish() }}
      >
        <label className="filter-item">
          <span>策略编号</span>
          <select
            value={publishForm.策略编号}
            onChange={(event) => setPublishForm((prev) => ({ ...prev, 策略编号: event.target.value }))}
          >
            {contracts.map((contract) => (
              <option key={String(contract['策略编号'])} value={String(contract['策略编号'])}>
                {contract['策略编号']}
              </option>
            ))}
          </select>
        </label>
        <label className="filter-item">
          <span>查询结果版本</span>
          <input
            value={publishForm.查询结果版本}
            onChange={(event) => setPublishForm((prev) => ({ ...prev, 查询结果版本: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>转换模板版本</span>
          <input
            value={publishForm.转换模板版本}
            onChange={(event) => setPublishForm((prev) => ({ ...prev, 转换模板版本: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>变更说明</span>
          <input
            value={publishForm.变更说明}
            onChange={(event) => setPublishForm((prev) => ({ ...prev, 变更说明: event.target.value }))}
          />
        </label>
        <label className="filter-item">
          <span>新查询结果内容（JSON，可空）</span>
          <textarea
            className="json-input"
            value={publishForm.结果内容}
            onChange={(event) => setPublishForm((prev) => ({ ...prev, 结果内容: event.target.value }))}
          />
        </label>
        <button className="btn primary" type="submit">发布新版本并失效旧缓存</button>
      </form>

      <h3 className="section-title">缓存条目（缓存键 = 缓存键模板 + 查询结果版本 + 转换模板版本）</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>缓存键</th><th>策略编号</th><th>查询编号</th>
            <th>查询结果版本</th><th>转换模板版本</th><th>状态</th><th>写入时间</th><th>失效时间</th>
          </tr>
        </thead>
        <tbody>
          {entries.map((entry) => (
            <tr key={String(entry['id'])}>
              <td>{entry['缓存键']}</td>
              <td>{entry['策略编号']}</td>
              <td>{entry['查询编号']}</td>
              <td>{entry['查询结果版本']}</td>
              <td>{entry['转换模板版本']}</td>
              <td>{entry['状态']}</td>
              <td>{entry['写入时间']}</td>
              <td>{entry['失效时间'] ?? '—'}</td>
            </tr>
          ))}
          {entries.length === 0 && (
            <tr><td className="empty-state" colSpan={8}>暂无缓存条目，可先执行数据链或缓存预热</td></tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">主动失效记录</h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>失效编号</th><th>缓存键</th><th>旧查询结果版本</th><th>旧转换模板版本</th>
            <th>新查询结果版本</th><th>新转换模板版本</th><th>失效原因</th><th>失效时间</th>
          </tr>
        </thead>
        <tbody>
          {invalidations.map((item) => (
            <tr key={String(item['id'])}>
              <td>{item['失效编号']}</td>
              <td>{item['缓存键']}</td>
              <td>{item['旧查询结果版本']}</td>
              <td>{item['旧转换模板版本']}</td>
              <td>{item['新查询结果版本']}</td>
              <td>{item['新转换模板版本']}</td>
              <td>{item['失效原因']}</td>
              <td>{item['失效时间']}</td>
            </tr>
          ))}
          {invalidations.length === 0 && (
            <tr><td className="empty-state" colSpan={8}>暂无失效记录，版本发布后旧缓存会在此留痕</td></tr>
          )}
        </tbody>
      </table>

      <h3 className="section-title">
        查询结果数据集（复核：命中率 {hitText(datasetSummary['命中率'])} ·
        命中 {String(datasetSummary['命中'] ?? 0)} · 未命中 {String(datasetSummary['未命中'] ?? 0)}）
      </h3>
      <table className="data-table">
        <thead>
          <tr>
            <th>批次编号</th><th>批次说明</th><th>查询编号</th><th>缓存键</th>
            <th>命中标识</th><th>查询结果版本</th><th>转换模板版本</th><th>数据范围</th><th>产生时间</th>
          </tr>
        </thead>
        <tbody>
          {dataset.map((item) => (
            <tr key={String(item['id'])}>
              <td>{item['批次编号']}</td>
              <td>{item['批次说明']}</td>
              <td>{item['查询编号']}</td>
              <td>{item['缓存键']}</td>
              <td>{item['命中标识']}</td>
              <td>{item['查询结果版本']}</td>
              <td>{item['转换模板版本']}</td>
              <td>{item['数据范围']}</td>
              <td>{item['产生时间']}</td>
            </tr>
          ))}
          {dataset.length === 0 && (
            <tr><td className="empty-state" colSpan={9}>暂无数据集记录，执行数据链后可在此复核命中率</td></tr>
          )}
        </tbody>
      </table>

      <footer className="page-foot">
        <span>共 {total} 条结果缓存记录</span>
        {errorMessage && <span className="error-text">{errorMessage}</span>}
      </footer>
    </section>
  )
}
