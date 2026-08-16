/**
 * 运行日志(docs/LOG-CONSOLE.md §5)。
 *
 * ## 与操作审计的分工,一句话说死
 *
 *     审计(/audit)    谁在什么时候改了什么 —— 合规,入库,长留
 *     运行(/ops/logs) 系统怎么跑的 —— 排障,环形,短留
 *
 * **不合并。** 合并的结果是运营在合规页里看见租约让位,谁都不舒服。
 *
 * ## 三个视角
 *
 *     流视角   紧凑时间线,新在上。例行事件折成一根计数条
 *     域筛选   左侧轨道,十五个域 + 实时计数
 *     链路模式 点任一 task_id / request_id,整页收束成这条链路的时间线,
 *              **旧在上**(链路要顺着读),按 round 分段
 *
 * 链路模式是这一页存在的理由:关联键(`request_id` 全局钉在 contextvar 里、
 * `task_id` 在结构化字段里出现 90 次)一直都在,缺的只是一个会用这些键的界面。
 *
 * ## 这一页不持有任何一张分类表
 *
 * 硬规则第 4 条。域下拉、事件标签、例行判定,全部来自 `/ops/logs/meta` 和每一条
 * 日志自带的字段。源码里出现一个事件码字面量就会让
 * `tests/component/ops-log-page.test.tsx` 变红 —— 理由写在 `api/ops.ts` 顶部。
 *
 * ## 折叠的是噪音,不是告警
 *
 * `routine` 由后端判,而且 **ERROR 永远不折叠**。这一条不能挪到前端:
 * "什么时候可以藏一条日志"是业务规则,而前端藏错了没有任何人会发现。
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Alert, Badge, Button, Card, Empty, Input, Segmented, Space, Switch, Table, Tabs, Tag, Tooltip,
  Typography,
} from 'antd'
import { ReloadOutlined, RollbackOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { LEVEL_TONE, opsApi, type LogEntry } from '../api/ops'
import { brandVars, fontScale, space } from '../theme'
import { formatDateTime } from '../utils/datetime'
import { enumParam, flagParam, intParam, oneOfParam, textParam, useUrlFilters } from '../hooks/useUrlFilters'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import PageHeader from '../components/PageHeader'
import BrandTag from '../components/BrandTag'
import ErrorNotice from '../components/ErrorNotice'

/** 跟随模式的节拍。项目里 `refetchInterval` 的既有手法 */
const FOLLOW_MS = 3000
const LIMITS = [100, 200, 500, 1000] as const
const limitParam = oneOfParam(200, LIMITS)

/** 链路模式的两种键。**取值是这两个词本身,不是事件码** */
const TRACE_KINDS = ['task', 'request'] as const

function shortId(value: string): string {
  return value.length > 10 ? `${value.slice(0, 8)}…` : value
}

/** 一行里显示哪些字段片。按"排障时先看什么"排,不按字母序 */
const CHIP_KEYS = ['round', 'attempt', 'http_status', 'provider', 'model', 'status', 'duration_ms']

export default function OpsLogPage() {
  useDocumentTitle('运行日志')

  const filters = useUrlFilters({
    domain: textParam(),
    event: textParam(),
    level: textParam(),
    q: textParam(),
    trace_kind: enumParam<string>(TRACE_KINDS),
    trace_id: textParam(),
    // 默认**开着**折叠,所以 URL 上出现的是把它关掉的那一次
    fold: flagParam(true),
    follow: flagParam(true),
    limit: limitParam,
    expanded: intParam(0, { min: 0 }),
  })
  const {
    domain, event, level, q, trace_kind: traceKind, trace_id: traceId,
    fold, follow, limit,
  } = filters.values

  const inTrace = Boolean(traceId)

  /** 搜索框里的字。与已生效的 `q` 分开(理由同走查 P0-3) */
  const [draft, setDraft] = useState(q)
  useEffect(() => setDraft(q), [q])

  const meta = useQuery({ queryKey: ['ops-meta'], queryFn: opsApi.meta })

  const query = useQuery({
    queryKey: ['ops-logs', { domain, event, level, q, traceKind, traceId, limit }],
    queryFn: () =>
      opsApi.logs({
        // 链路模式下**只按链路键筛**:进了链路就该看见这条链路的全部,
        // 带着域筛选进来会让人以为"这条链路只做了这些事"。
        domain: inTrace ? undefined : domain || undefined,
        event: inTrace ? undefined : event || undefined,
        level: inTrace ? undefined : level || undefined,
        q: inTrace ? undefined : q || undefined,
        task_id: traceKind === 'task' ? traceId : undefined,
        request_id: traceKind === 'request' ? traceId : undefined,
        limit,
      }),
    refetchInterval: follow ? FOLLOW_MS : false,
  })

  const ring = query.data?.ring
  const items = useMemo(() => {
    const rows = query.data?.items ?? []
    // 链路要顺着读:旧在上。流视角相反 —— 新的在上面才叫"盯着看"
    return inTrace ? [...rows].reverse() : rows
  }, [query.data, inTrace])

  /** 域计数。**只统计当前这一屏**,并且这件事要在界面上说出来 */
  const domainCounts = useMemo(() => {
    const counts = new Map<string, { total: number; warn: number; error: number }>()
    for (const row of query.data?.items ?? []) {
      const seen = counts.get(row.domain) ?? { total: 0, warn: 0, error: 0 }
      seen.total += 1
      if (row.level === 'WARNING') seen.warn += 1
      if (row.level === 'ERROR' || row.level === 'CRITICAL') seen.error += 1
      counts.set(row.domain, seen)
    }
    return counts
  }, [query.data])

  /**
   * 折叠:把连续的例行事件收成一根计数条。
   *
   * 链路模式里**全展** —— `attempt_started` 这类事件对单链路排障有用,
   * 它们在流视角里是噪音,在一条链路里是证据。
   */
  const rows = useMemo(() => {
    if (!fold || inTrace) return items.map((row) => ({ kind: 'row' as const, row }))
    const out: Array<
      | { kind: 'row'; row: LogEntry }
      | { kind: 'fold'; groups: Map<string, number>; total: number; warn: number; key: string }
    > = []
    let bucket: LogEntry[] = []
    const flush = () => {
      if (!bucket.length) return
      const groups = new Map<string, number>()
      let warn = 0
      for (const one of bucket) {
        const name = one.routine_group ?? '例行'
        groups.set(name, (groups.get(name) ?? 0) + 1)
        if (one.level === 'WARNING') warn += 1
      }
      out.push({
        kind: 'fold',
        groups,
        total: bucket.length,
        warn,
        key: `fold-${bucket[0].seq ?? bucket[0].ts}`,
      })
      bucket = []
    }
    for (const row of items) {
      if (row.routine) bucket.push(row)
      else {
        flush()
        out.push({ kind: 'row', row })
      }
    }
    flush()
    return out
  }, [items, fold, inTrace])

  /** 滚动即暂停跟随:正在读的行被顶走是这一页最讨厌的事 */
  const streamRef = useRef<HTMLDivElement>(null)
  useEffect(() => {
    const node = streamRef.current
    if (!node || !follow) return undefined
    const onScroll = () => {
      if (node.scrollTop > 40) filters.patch({ follow: false })
    }
    node.addEventListener('scroll', onScroll, { passive: true })
    return () => node.removeEventListener('scroll', onScroll)
  }, [follow, filters])

  const enterTrace = (kind: string, id: string) =>
    filters.patch({ trace_kind: kind, trace_id: id, follow: false })

  const windowNote = ring
    ? `环形窗口 ${ring.held ?? '?'} / ${ring.cap} 条${
        ring.dropped_since_boot ? ` · 本次启动以来掉了 ${ring.dropped_since_boot} 条` : ''
      }`
    : ''

  return (
    <Space direction="vertical" size={space.md} style={{ width: '100%' }}>
      <PageHeader
        title="运行日志"
        subtitle={
          <>
            系统怎么跑的 —— 排障视角,环形窗口,不入库。
            谁改了什么请查<a href="/audit">操作审计</a>。
          </>
        }
        extra={
          <Space>
            <Space size={6}>
              <Switch
                size="small"
                checked={follow}
                onChange={(value) => filters.patch({ follow: value })}
              />
              <Tooltip title="每 3 秒拉一次。向下滚动会自动暂停,免得正在读的行被顶走">
                <span style={{ fontSize: fontScale.body }}>{follow ? '跟随中' : '已暂停'}</span>
              </Tooltip>
            </Space>
            <Button
              icon={<ReloadOutlined />}
              onClick={() => query.refetch()}
              loading={query.isFetching}
            >
              刷新
            </Button>
          </Space>
        }
      />

      {ring?.unavailable_reason && (
        <Alert
          type="warning"
          showIcon
          message="环形缓冲读不到,下面这张列表是空的 —— 这不代表这段时间没有日志"
          description={
            <span>
              原因:<code>{ring.unavailable_reason}</code>。
              归档面没有受影响:stdout 与外部收集器那条链路照常。
              {!ring.enabled && ' 另外 OPS_LOG_RING_ENABLED 当前是关的。'}
            </span>
          }
        />
      )}

      {inTrace && (
        <Alert
          type="info"
          showIcon
          message={
            <Space>
              <span>链路 · {traceKind === 'task' ? '任务' : '请求'}</span>
              <code>{traceId}</code>
              <span style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
                按时间顺读,旧在上;例行事件全展
              </span>
            </Space>
          }
          action={
            <Button
              size="small"
              icon={<RollbackOutlined />}
              onClick={() => filters.patch({ trace_kind: undefined, trace_id: '', follow: true })}
            >
              退出链路
            </Button>
          }
        />
      )}

      <Card size="small" styles={{ body: { padding: space.md } }}>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="搜索 message 与字段"
            style={{ width: 240 }}
            value={draft}
            disabled={inTrace}
            onChange={(e) => setDraft(e.target.value)}
            onSearch={(value) => filters.patch({ q: value })}
          />
          <Segmented
            value={level || 'ALL'}
            disabled={inTrace}
            onChange={(value) => filters.patch({ level: value === 'ALL' ? '' : String(value) })}
            options={[
              { label: '全部级别', value: 'ALL' },
              ...(meta.data?.levels ?? []).map((one) => ({ label: one, value: one })),
            ]}
          />
          <Space size={6}>
            <Switch
              size="small"
              checked={fold}
              disabled={inTrace}
              onChange={(value) => filters.patch({ fold: value })}
            />
            <Tooltip title="例行事件(租约让位、幂等复用、调用生命周期)收成一根计数条。ERROR 永远不折叠 —— 那条判定在后端">
              <span style={{ fontSize: fontScale.body }}>折叠例行事件</span>
            </Tooltip>
          </Space>
          <Segmented
            value={limit}
            onChange={(value) => filters.patch({ limit: limitParam.narrow(Number(value)) })}
            options={LIMITS.map((one) => ({ label: `${one} 条`, value: one }))}
          />
          <span style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>{windowNote}</span>
        </Space>
      </Card>

      <div style={{ display: 'flex', gap: space.md, alignItems: 'flex-start' }}>
        <Card
          size="small"
          title={<span style={{ fontSize: fontScale.meta }}>领域(本屏计数)</span>}
          styles={{ body: { padding: space.xs } }}
          style={{ width: 190, flexShrink: 0 }}
        >
          {meta.isError ? (
            /* `/meta` 挂了不能只画一条空轨道 —— 那看起来像"这套系统只有零个领域"。
               分类表的唯一来源就是它,拿不到就得说出来(硬规则第 4 条的反面) */
            <ErrorNotice
              error={meta.error}
              title="拿不到领域清单"
              onRetry={() => meta.refetch()}
              retrying={meta.isFetching}
              type="warning"
            />
          ) : (
          <DomainRail
            domains={meta.data?.domains ?? []}
            counts={domainCounts}
            active={domain}
            disabled={inTrace}
            onPick={(key) => filters.patch({ domain: key, event: '' })}
          />
          )}
        </Card>

        <div ref={streamRef} style={{ flex: 1, minWidth: 0 }}>
          {query.isError && (
            /* 走 ErrorNotice 而不是把 error 拍平成一句话:管理员要拿得到
               请求编号与技术详情,而这一页的读者恰恰只有管理员 */
            <ErrorNotice
              error={query.error}
              title="拉不到运行日志"
              onRetry={() => query.refetch()}
              retrying={query.isFetching}
              style={{ marginBottom: space.sm }}
            />
          )}
          <Table<{ key: string }>
            rowKey="key"
            size="small"
            bordered
            showHeader={false}
            loading={query.isFetching && !query.data}
            pagination={false}
            dataSource={rows.map((one, index) =>
              one.kind === 'fold'
                ? { key: one.key, fold: one }
                : { key: one.row.seq ?? `${one.row.ts}-${index}`, row: one.row },
            )}
            columns={[
              {
                title: '事件',
                key: 'entry',
                render: (_, record) => {
                  const packed = record as { fold?: typeof rows[number]; row?: LogEntry }
                  if (packed.fold && packed.fold.kind === 'fold') {
                    return <FoldBar bar={packed.fold} />
                  }
                  return <LogRow row={packed.row as LogEntry} onTrace={enterTrace} />
                },
              },
            ]}
            expandable={{
              expandedRowRender: (record) => {
                const packed = record as { row?: LogEntry }
                return packed.row ? <RowDetail row={packed.row} /> : null
              },
              rowExpandable: (record) => Boolean((record as { row?: LogEntry }).row),
            }}
            locale={{
              emptyText: (
                <Empty
                  description={
                    ring?.unavailable_reason
                      ? '环形缓冲读不到 —— 见上方提示'
                      : '这个筛选组合下没有日志。环形只保留最近若干条,更早的记录看外部收集器'
                  }
                />
              ),
            }}
          />
          {query.data?.oldest_ts && (
            <div
              style={{
                color: brandVars.textMuted,
                fontSize: fontScale.meta,
                padding: `${space.sm}px 0`,
              }}
            >
              窗口里最早的一条是 {formatDateTime(query.data.oldest_ts)} —— 更早的不是没发生,
              是滚出窗口了。
            </div>
          )}
        </div>
      </div>
    </Space>
  )
}

function DomainRail({
  domains,
  counts,
  active,
  disabled,
  onPick,
}: {
  domains: Array<{ key: string; label: string }>
  counts: Map<string, { total: number; warn: number; error: number }>
  active: string
  disabled: boolean
  onPick: (key: string) => void
}) {
  return (
    <Space direction="vertical" size={0} style={{ width: '100%' }}>
      <Button
        type={active ? 'text' : 'link'}
        block
        style={{ textAlign: 'left', justifyContent: 'flex-start' }}
        disabled={disabled}
        onClick={() => onPick('')}
      >
        全部领域
      </Button>
      {domains.map((one) => {
        const seen = counts.get(one.key)
        return (
          <Button
            key={one.key}
            type={active === one.key ? 'text' : 'link'}
            block
            disabled={disabled}
            style={{
              textAlign: 'left',
              justifyContent: 'space-between',
              display: 'flex',
              background: active === one.key ? brandVars.marineSoft : undefined,
            }}
            onClick={() => onPick(one.key)}
          >
            <span>{one.label}</span>
            <span style={{ fontSize: fontScale.meta, color: brandVars.textMuted }}>
              {seen?.error ? <Badge status="error" /> : seen?.warn ? <Badge status="warning" /> : null}
              {seen?.total ?? 0}
            </span>
          </Button>
        )
      })}
    </Space>
  )
}

function FoldBar({
  bar,
}: {
  bar: { groups: Map<string, number>; total: number; warn: number }
}) {
  const parts = [...bar.groups.entries()].map(([name, count]) => `${name} ×${count}`).join(' · ')
  return (
    <span style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
      例行 ×{bar.total} · {parts}
      {/* 折进去的里面有 WARN 就单独点名 —— 折叠是降噪,不是掩埋 */}
      {bar.warn > 0 && <Tag color="warning" style={{ marginLeft: space.sm }}>含 WARN {bar.warn}</Tag>}
    </span>
  )
}

function LogRow({
  row,
  onTrace,
}: {
  row: LogEntry
  onTrace: (kind: string, id: string) => void
}) {
  const taskId = row.fields.task_id ? String(row.fields.task_id) : ''
  const chips = CHIP_KEYS.filter((key) => row.fields[key] !== undefined && row.fields[key] !== null)
  return (
    <Space size={space.sm} wrap style={{ width: '100%' }}>
      <span className="mono" style={{ fontSize: fontScale.meta, color: brandVars.textMuted }}>
        {formatDateTime(row.ts)}
      </span>
      <Tag color={LEVEL_TONE[row.level ?? 'INFO']}>{row.level}</Tag>
      {/* 域一律同一个底色:十五种颜色没人记得住,域靠文字区分。
          走 BrandTag 而不是 antd 预设色 —— 后者不受 theme.ts 控制 */}
      <BrandTag tone="marine">{row.domain_label}</BrandTag>
      {/* 没迁移的调用点没有中文标签 —— 此时显示 message 原文,不显示"未分类" */}
      {row.event_label && <strong style={{ fontSize: fontScale.body }}>{row.event_label}</strong>}
      <span style={{ fontSize: fontScale.body }}>{row.message}</span>
      {taskId && (
        <Button size="small" type="link" onClick={() => onTrace('task', taskId)}>
          task {shortId(taskId)}
        </Button>
      )}
      {row.request_id && row.request_id !== '-' && (
        <Button size="small" type="link" onClick={() => onTrace('request', row.request_id as string)}>
          {row.request_id}
        </Button>
      )}
      {chips.map((key) => (
        <Tag key={key} style={{ fontSize: fontScale.meta }}>
          {key}={String(row.fields[key])}
        </Tag>
      ))}
    </Space>
  )
}

/**
 * 行展开的三个页签。
 *
 * 「原始日志行」**对每一条事件都有**。这条是态度问题:分类法是索引,不是转述。
 * 控制台把事件码和中文标签摆在前面是为了让人快速定位;一旦定位到了,
 * 原文必须零成本可得,否则这套分类就变成了一层遮挡。
 */
function RowDetail({ row }: { row: LogEntry }) {
  const callId = row.fields.llm_call_id ? String(row.fields.llm_call_id) : ''
  return (
    <Tabs
      size="small"
      items={[
        {
          key: 'fields',
          label: '字段',
          children: (
            <Table
              size="small"
              rowKey="k"
              pagination={false}
              showHeader={false}
              dataSource={[
                { k: 'event', v: row.event ?? '(未迁移,按 logger 前缀归域)' },
                { k: 'logger', v: row.logger ?? '' },
                ...Object.entries(row.fields).map(([k, v]) => ({ k, v: JSON.stringify(v) })),
              ]}
              columns={[
                { dataIndex: 'k', width: 180, render: (v) => <code>{String(v)}</code> },
                { dataIndex: 'v', render: (v) => <span className="mono">{String(v)}</span> },
              ]}
            />
          ),
        },
        {
          key: 'raw',
          label: '原始日志行',
          children: (
            <div>
              <Typography.Paragraph
                type="secondary"
                style={{ fontSize: fontScale.meta, marginBottom: space.xs }}
              >
                这是采集链路里逐字的一行,控制台不改写它。
              </Typography.Paragraph>
              <Typography.Paragraph copyable={{ text: row.raw }}>
                <pre style={{ margin: 0, fontSize: fontScale.meta, whiteSpace: 'pre-wrap' }}>
                  {row.raw}
                </pre>
              </Typography.Paragraph>
            </div>
          ),
        },
        ...(callId ? [{ key: 'payload', label: '模型载荷', children: <PayloadPanel callId={callId} /> }] : []),
      ]}
    />
  )
}

/**
 * 模型载荷:左请求右响应,多次尝试用页签切。
 *
 * 「重试后成功了但结果不一样」「第一次是 429 第二次是 200」这类问题,
 * 只有把两次尝试摆在同一个切换器里才看得出来 —— 这是这一屏真正的价值。
 */
function PayloadPanel({ callId }: { callId: string }) {
  const query = useQuery({
    queryKey: ['ops-llm', callId],
    queryFn: () => opsApi.llmPayload(callId),
    retry: false,
  })

  if (query.isLoading) return <span>正在取载荷…</span>
  if (query.isError) {
    // 「没开捕获」和「已过期」是两种情况,后端把话说清楚了 —— 原样透出,
    // 不要在这里改写成一句更短的。它们的下一步完全相反。
    return <ErrorNotice error={query.error} title="取不到这次调用的载荷" type="warning" />
  }
  const data = query.data
  if (!data) return null

  return (
    <Space direction="vertical" size={space.sm} style={{ width: '100%' }}>
      <Space wrap>
        <Tag>{data.provider}</Tag>
        <Tag>{data.model}</Tag>
        {data.request && (
          <Tooltip title="你看到的是脱敏视图,这个哈希对应的是真正发出去的那串原始字节">
            <Tag className="mono">
              sha256_16 {data.request.sha256_16} · {data.request.body_bytes} 字节
            </Tag>
          </Tooltip>
        )}
      </Space>
      {data.truncated.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`这些字段被截断了,你看到的不是全文:${data.truncated.join('、')}`}
        />
      )}
      <Typography.Paragraph type="secondary" style={{ fontSize: fontScale.meta, margin: 0 }}>
        图片正文永不留存 —— 只留 MIME、base64 字符数与摘要,足够核对到底发的是哪一份数据。
      </Typography.Paragraph>
      <Card size="small" title="请求">
        <pre style={{ margin: 0, fontSize: fontScale.meta, whiteSpace: 'pre-wrap', maxHeight: 320, overflow: 'auto' }}>
          {JSON.stringify(data.request?.body ?? {}, null, 2)}
        </pre>
      </Card>
      <Card size="small" title={`响应(${data.attempts.length} 次尝试)`}>
        <Tabs
          size="small"
          items={data.attempts.map((one) => ({
            key: String(one.attempt),
            label: (
              <Space size={4}>
                <Badge status={one.http_status && one.http_status < 400 ? 'success' : 'error'} />
                第 {one.attempt} 次 · {one.http_status}
              </Space>
            ),
            children: (
              <div>
                <Typography.Paragraph type="secondary" style={{ fontSize: fontScale.meta }}>
                  content-type: {one.content_type ?? '未知'} · 上游请求号:
                  {one.upstream_request_id ?? '无'}
                </Typography.Paragraph>
                {/* 非 JSON 响应原文照显:上游返回网关 HTML 错误页时,
                    摘要字段全是空,只有这一块答得出发生了什么 */}
                <pre style={{ margin: 0, fontSize: fontScale.meta, whiteSpace: 'pre-wrap', maxHeight: 320, overflow: 'auto' }}>
                  {typeof one.body === 'string' ? one.body : JSON.stringify(one.body, null, 2)}
                </pre>
              </div>
            ),
          }))}
        />
      </Card>
    </Space>
  )
}
