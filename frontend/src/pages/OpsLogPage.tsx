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
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert, Badge, Button, Card, Empty, Input, Segmented, Select, Space, Switch, Table, Tabs, Tag,
  Tooltip, Typography,
} from 'antd'
import { DownloadOutlined, ReloadOutlined, RollbackOutlined } from '@ant-design/icons'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { LEVEL_TONE, opsApi, type LlmPayload, type LogEntry } from '../api/ops'
import { brandVars, fontScale, space } from '../theme'
import { formatDateTime } from '../utils/datetime'
import { enumParam, flagParam, oneOfParam, textParam, useUrlFilters } from '../hooks/useUrlFilters'
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

/** 行展开的三个页签。**是页签的名字,不是事件码** */
const ROW_TABS = ['fields', 'raw', 'payload'] as const

/**
 * 超过这么多字符的文本默认收起,留一个「展开全文(N 字符)」。
 *
 * 系统提示词本身就有几千字,整段摊开会把响应挤出屏幕;而**不能直接截掉** ——
 * 排格式问题要看的恰恰是「输出要求」那一段,它通常在末尾。
 */
const FOLD_TEXT_OVER = 1200

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
    /*
     * 展开的是哪一行(存 seq)、停在哪个页签。
     *
     * 上一版这里声明了一个 `expanded` 却**全页零引用** —— 刷新一次就回到
     * 收起状态,而 GAP-033 的教训正是「刷新不许丢筛选」。展开一条日志之后
     * 把链接发给同事,是这一页最常见的动作之一。
     */
    expanded: textParam(),
    tab: enumParam<string>(ROW_TABS, 'fields'),
  })
  const {
    domain, event, level, q, trace_kind: traceKind, trace_id: traceId,
    fold, follow, limit, expanded, tab,
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

  /**
   * 域计数。**服务端按全窗算,不受当前选中的域影响。**
   *
   * 上一版是在前端按已经筛过的那一屏算的,于是点进一个域之后其余十四格
   * 全变 0 —— 恰好在最需要「别处还有没有事」的时候把这个信息拿掉了。
   */
  const domainCounts = query.data?.domain_counts ?? {}

  /**
   * 已经被点开的那几根计数条。
   *
   * **折叠是降噪,不是掩埋** —— 计数条必须点得开。上一版它是个纯展示的
   * `<span>`,而设计 §3.5 写的是「点开全展」。换一次筛选就忘掉,
   * 免得一根属于上一屏的条一直摊在那里。
   */
  const [opened, setOpened] = useState<Set<string>>(new Set())
  useEffect(() => setOpened(new Set()), [domain, event, level, q, traceId])

  /**
   * 折叠:把连续的例行事件收成一根计数条。
   *
   * 链路模式里**全展** —— `attempt_started` 这类事件对单链路排障有用,
   * 它们在流视角里是噪音,在一条链路里是证据。
   */
  const rows = useMemo<Row[]>(() => {
    /*
     * 链路模式:按 `round` 分段,段头取该轮里程碑那条的摘要(§5.2)。
     *
     * 哪条事件算里程碑由后端给的 `round_summary` 决定 —— 这一页不认事件码。
     * 没有 round 的事件(领取、审批、产出)归段外,单独一条分段线,
     * 否则它们会安静地并进上一轮,让人以为那些动作发生在那一轮里。
     */
    if (inTrace) {
      const out: Row[] = []
      let current: string | null = null
      items.forEach((row, index) => {
        const round = row.fields.round === undefined || row.fields.round === null
          ? ''
          : String(row.fields.round)
        if (round !== current) {
          current = round
          const summary = items.find(
            (one) => one.round_summary && String(one.fields.round ?? '') === round,
          )
          out.push({ kind: 'round', round, summary: summary ?? null, key: `round-${index}` })
        }
        out.push({ kind: 'row', row })
      })
      return out
    }
    if (!fold) return items.map((row): Row => ({ kind: 'row', row }))
    const out: Row[] = []
    let bucket: LogEntry[] = []
    const flush = () => {
      if (!bucket.length) return
      const key = `fold-${bucket[0].seq ?? bucket[0].ts}`
      if (opened.has(key)) {
        // 点开之后就是普通行,连折叠条一起消失 —— 留着一根「已展开」的空条
        // 只会让人再点一次
        for (const one of bucket) out.push({ kind: 'row', row: one })
        bucket = []
        return
      }
      const groups = new Map<string, number>()
      let warn = 0
      for (const one of bucket) {
        const name = one.routine_group ?? '例行'
        groups.set(name, (groups.get(name) ?? 0) + 1)
        if (one.level === 'WARNING') warn += 1
      }
      out.push({ kind: 'fold', groups, total: bucket.length, warn, key })
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
  }, [items, fold, inTrace, opened])

  /*
   * 滚动即暂停跟随:正在读的行被顶走是这一页最讨厌的事。
   *
   * **监听的是 window,不是那个 div。** 上一版把监听挂在流列表的容器上,
   * 而那个 div 既没有 `overflow` 也没有高度 —— 它根本不是滚动容器,
   * `scrollTop` 恒为 0,于是这个功能一次都没有触发过。整页在滚的是 window。
   */
  useEffect(() => {
    if (!follow) return undefined
    const onScroll = () => {
      if (window.scrollY > 40) filters.patch({ follow: false })
    }
    window.addEventListener('scroll', onScroll, { passive: true })
    return () => window.removeEventListener('scroll', onScroll)
  }, [follow, filters])

  /** 展开某一行的某个页签。`call` 芯片直接落在载荷页签上 */
  const openRow = useCallback(
    (row: LogEntry, which: string) =>
      filters.patch({ expanded: row.seq ?? row.ts ?? '', tab: which }),
    [filters],
  )

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
            谁改了什么请查<Link to="/audit">操作审计</Link>。
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
          {/* 级别是**最低级别**,不是精确值:选 WARNING 的人要找的是问题,
              而 ERROR 是更严重的问题。上一版是精确匹配,筛 WARNING 会把
              ERROR 挡掉 —— 「告警在噪音里挣扎」那个病换了个地方复发 */}
          <Tooltip title="按最低级别筛:选 WARNING 会同时看到 ERROR 与 CRITICAL">
            <Segmented
              value={level || 'ALL'}
              disabled={inTrace}
              onChange={(value) => filters.patch({ level: value === 'ALL' ? '' : String(value) })}
              options={[
                { label: '全部级别', value: 'ALL' },
                ...(meta.data?.levels ?? []).map((one) => ({ label: `≥ ${one}`, value: one })),
              ]}
            />
          </Tooltip>
          {/* 事件精筛。取值全部来自 `/meta`,选中一个域时只列这个域的事件 ——
              上一版后端、类型、URL 三处都支持 `event`,唯独没有任何界面
              能设它,只能手改地址栏 */}
          <Select
            allowClear
            showSearch
            optionFilterProp="label"
            placeholder="按事件精筛"
            style={{ width: 220 }}
            disabled={inTrace}
            value={event || undefined}
            onChange={(value) => filters.patch({ event: value ?? '' })}
            options={(meta.data?.events ?? [])
              .filter((one) => !domain || one.domain === domain)
              .map((one) => ({ label: one.label, value: one.key }))}
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
            note="计数按整个环形窗口算,不受选中的域影响"
          />
          )}
        </Card>

        <div style={{ flex: 1, minWidth: 0 }}>
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
              one.kind === 'row'
                ? { key: one.row.seq ?? `${one.row.ts}-${index}`, row: one.row }
                : { key: one.key, block: one },
            )}
            columns={[
              {
                title: '事件',
                key: 'entry',
                render: (_, record) => {
                  const packed = record as { block?: Row; row?: LogEntry }
                  if (packed.block?.kind === 'fold') {
                    return (
                      <FoldBar
                        bar={packed.block}
                        onOpen={() =>
                          setOpened((was) => new Set(was).add((packed.block as { key: string }).key))
                        }
                      />
                    )
                  }
                  if (packed.block?.kind === 'round') return <RoundBar bar={packed.block} />
                  return (
                    <LogRow
                      row={packed.row as LogEntry}
                      onTrace={enterTrace}
                      onOpen={openRow}
                    />
                  )
                },
              },
            ]}
            expandable={{
              // 展开态进 URL:刷新、分享链接都保得住(GAP-033 的教训)
              expandedRowKeys: expanded ? [expanded] : [],
              onExpand: (open, record) =>
                filters.patch({ expanded: open ? String((record as { key: string }).key) : '' }),
              expandedRowRender: (record) => {
                const packed = record as { row?: LogEntry }
                return packed.row ? (
                  <RowDetail
                    row={packed.row}
                    tab={tab}
                    onTab={(which) => filters.patch({ tab: which })}
                  />
                ) : null
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

type Row =
  | { kind: 'row'; row: LogEntry }
  | { kind: 'fold'; groups: Map<string, number>; total: number; warn: number; key: string }
  | { kind: 'round'; round: string; summary: LogEntry | null; key: string }

function DomainRail({
  domains,
  counts,
  active,
  disabled,
  note,
  onPick,
}: {
  domains: Array<{ key: string; label: string }>
  counts: Record<string, { total: number; warn: number; error: number }>
  active: string
  disabled: boolean
  note: string
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
        const seen = counts[one.key]
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
      <div style={{ padding: space.xs, color: brandVars.textMuted, fontSize: fontScale.meta }}>
        {note}
      </div>
    </Space>
  )
}

/**
 * 链路里的一条轮次分段线(§5.2 的签名交互)。
 *
 * 段头文字取该轮里程碑那条事件的 message。**哪条事件算里程碑由后端给的
 * `round_summary` 决定** —— 这一页不认事件码(硬规则第 4 条)。
 * 没有 round 的事件归段外,也给一条线:并进上一轮会让人以为那些动作
 * 发生在那一轮里。
 */
function RoundBar({ bar }: { bar: { round: string; summary: LogEntry | null } }) {
  return (
    <Space size={space.sm} style={{ width: '100%', color: brandVars.sand }}>
      <span style={{ fontSize: fontScale.meta, whiteSpace: 'nowrap' }}>
        {bar.round ? `── 第 ${bar.round} 轮` : '── 不属于任何一轮'}
      </span>
      {bar.summary && (
        <span style={{ fontSize: fontScale.meta, color: brandVars.textMuted }}>
          {bar.summary.message}
        </span>
      )}
      <span
        aria-hidden
        style={{ display: 'inline-block', width: 120, borderTop: `1px solid ${brandVars.sand}` }}
      />
    </Space>
  )
}

/** 折叠计数条。**点得开** —— 折叠是降噪,不是掩埋 */
function FoldBar({
  bar,
  onOpen,
}: {
  bar: { groups: Map<string, number>; total: number; warn: number }
  onOpen: () => void
}) {
  const parts = [...bar.groups.entries()].map(([name, count]) => `${name} ×${count}`).join(' · ')
  return (
    <Space size={space.sm}>
      <Button type="link" size="small" style={{ padding: 0, height: 'auto' }} onClick={onOpen}>
        <span style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
          例行 ×{bar.total} · {parts}
        </span>
      </Button>
      <span style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>点开全展</span>
      {/* 折进去的里面有 WARN 就单独点名 —— 折叠是降噪,不是掩埋 */}
      {bar.warn > 0 && <Tag color="warning">含 WARN {bar.warn}</Tag>}
    </Space>
  )
}

function LogRow({
  row,
  onTrace,
  onOpen,
}: {
  row: LogEntry
  onTrace: (kind: string, id: string) => void
  onOpen: (row: LogEntry, tab: string) => void
}) {
  const taskId = row.fields.task_id ? String(row.fields.task_id) : ''
  const callId = row.fields.llm_call_id ? String(row.fields.llm_call_id) : ''
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
      {/* `call` 芯片直接打开载荷页签 —— 与 task 芯片点进链路是同一个手势(§6.5)。
          上一版没有它:要看这次调用发了什么,得先展开行、再切页签,
          而载荷正是这一页最贵的那块内容 */}
      {callId && (
        <Button size="small" type="link" onClick={() => onOpen(row, 'payload')}>
          call {shortId(callId)}
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
 * 长文本块:默认收起,留一个「展开全文(N 字符)」,带复制。
 *
 * **截断必须显形。** 直接截掉会让人以为看到的是全文,而排格式问题要看的
 * 「输出要求」那一段通常在末尾。
 */
function LongText({ text, rows = 12 }: { text: string; rows?: number }) {
  const [open, setOpen] = useState(text.length <= FOLD_TEXT_OVER)
  const shown = open ? text : text.slice(0, FOLD_TEXT_OVER)
  return (
    <div>
      <pre
        style={{
          margin: 0,
          fontSize: fontScale.meta,
          whiteSpace: 'pre-wrap',
          wordBreak: 'break-all',
          maxHeight: open ? rows * 22 : undefined,
          overflow: 'auto',
        }}
      >
        {shown}
      </pre>
      <Space size={space.sm}>
        {!open && (
          <Button size="small" type="link" onClick={() => setOpen(true)}>
            展开全文({text.length} 字符)
          </Button>
        )}
        <Typography.Text copyable={{ text }} style={{ fontSize: fontScale.meta }} />
      </Space>
    </div>
  )
}

function pretty(value: unknown): string {
  if (typeof value === 'string') return value
  try {
    return JSON.stringify(value, null, 2)
  } catch {
    return String(value)
  }
}

/**
 * 模型把结构化结果塞在一个字符串字段里返回是常态,转义后的原文没法读。
 * 找到最长的那个能解析成 JSON 的字符串叶子,单独给一块。
 *
 * **它是补充,不是替代** —— 原文那一块必须同时在场(§6.5)。
 */
function parsedOutput(body: unknown): string | null {
  let best: string | null = null
  const walk = (node: unknown) => {
    if (typeof node === 'string') {
      if (node.length > 20 && (!best || node.length > best.length)) {
        try {
          const seen = JSON.parse(node)
          if (seen && typeof seen === 'object') best = node
        } catch {
          /* 不是 JSON 就算了 —— 这一块是锦上添花,不该为它报错 */
        }
      }
      return
    }
    if (Array.isArray(node)) node.forEach(walk)
    else if (node && typeof node === 'object') Object.values(node).forEach(walk)
  }
  walk(body)
  return best ? JSON.stringify(JSON.parse(best), null, 2) : null
}

/**
 * 行展开的三个页签。
 *
 * 「原始日志行」**对每一条事件都有**。这条是态度问题:分类法是索引,不是转述。
 * 控制台把事件码和中文标签摆在前面是为了让人快速定位;一旦定位到了,
 * 原文必须零成本可得,否则这套分类就变成了一层遮挡。
 */
function RowDetail({
  row,
  tab,
  onTab,
}: {
  row: LogEntry
  tab: string
  onTab: (tab: string) => void
}) {
  const callId = row.fields.llm_call_id ? String(row.fields.llm_call_id) : ''
  // 停在载荷页签、但这一条没有 call id 时退回字段页 —— 空页签比没有页签更难解释
  const active = tab === 'payload' && !callId ? 'fields' : tab
  return (
    <Tabs
      size="small"
      activeKey={active}
      onChange={onTab}
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

/** 一次调用整包下载成 JSON,方便贴进 issue 或发给厂商(§6.5) */
function download(data: LlmPayload) {
  const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' })
  const url = URL.createObjectURL(blob)
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.download = `llm-call-${data.llm_call_id}.json`
  anchor.click()
  URL.revokeObjectURL(url)
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
  const headers = Object.entries(data.request?.headers ?? {})

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
        <Button size="small" icon={<DownloadOutlined />} onClick={() => download(data)}>
          下载整次调用
        </Button>
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
        <Space direction="vertical" size={space.xs} style={{ width: '100%' }}>
          {data.request?.endpoint && (
            <span className="mono" style={{ fontSize: fontScale.meta }}>
              {data.request.endpoint}
            </span>
          )}
          {/* 图片画成 chip:tag、MIME、base64 字符数、摘要。上一版把 images
              取回来了却从来没画过,于是"这一次到底发了哪几张图"答不出来 */}
          {(data.request?.images ?? []).length > 0 && (
            <Space wrap size={space.xs}>
              {(data.request?.images ?? []).map((one, index) => (
                <Tag key={index} className="mono" style={{ fontSize: fontScale.meta }}>
                  {String(one.tag ?? '图')} · {String(one.mime_type ?? '?')} ·{' '}
                  {String(one.base64_chars ?? '?')} 字符 · {String(one.sha256_16 ?? '?')}
                </Tag>
              ))}
            </Space>
          )}
          {headers.length > 0 && (
            <span style={{ fontSize: fontScale.meta, color: brandVars.textMuted }}>
              {headers.map(([k, v]) => `${k}: ${String(v)}`).join('  ·  ')}
            </span>
          )}
          <LongText text={pretty(data.request?.body ?? {})} />
        </Space>
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
              <Space direction="vertical" size={space.xs} style={{ width: '100%' }}>
                <Typography.Paragraph type="secondary" style={{ fontSize: fontScale.meta, margin: 0 }}>
                  content-type: {one.content_type ?? '未知'} · 耗时:
                  {one.duration_ms === null || one.duration_ms === undefined
                    ? '未记录'
                    : `${one.duration_ms} ms`}{' '}
                  · 上游请求号:{one.upstream_request_id ?? '无'}
                </Typography.Paragraph>
                {/* 非 JSON 响应原文照显:上游返回网关 HTML 错误页时,
                    摘要字段全是空,只有这一块答得出发生了什么 */}
                <LongText text={pretty(one.body)} />
                {parsedOutput(one.body) && (
                  <Card size="small" type="inner" title="output_text(解析后)">
                    {/* 解析块是补充不是替代 —— 上面那块原文必须同时在场 */}
                    <LongText text={parsedOutput(one.body) as string} />
                  </Card>
                )}
              </Space>
            ),
          }))}
        />
      </Card>
    </Space>
  )
}
