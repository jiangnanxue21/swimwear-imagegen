/**
 * 工作台的「按款」视图(a47 §8.2)。
 *
 * ## 它取代的是 `/workbench-spus`,不是新做一页
 *
 * 那一页本轮撤出运营菜单(路由保留)。它原来的价值是"按款看",而运营
 * 为了这一件事要离开工作台、丢掉七个筛选条件、再回来 —— 于是没人用。
 * 收进工作台之后,切一下就是同一组商品的另一种看法。
 *
 * ## 第一版只显示后端给得出的东西
 *
 * SPU 码、款名、SKU 数、颜色数、完成度、阻断数、卡点分布、可展开 SKU 明细。
 * **一个业务状态都不在这里算**(硬规则 4)。
 *
 * 尤其是:**没有款级「下一步」**。后端刻意不给,理由写在
 * `app/workbench/spu.py` 的 `SpuGroup.blocked_steps` 上 —— 款级"下一步"
 * 在这套状态机里没有定义,任何单选都是编的。这里显示的是真实分布:
 * "3 只卡在图片集、1 只卡在文案",让运营自己决定先动哪一只。
 *
 * ## 两个数字不要拿去对账
 *
 * 「阻断」数的是问题条数,「卡点分布」数的是 SKU 个数。同一只 SKU 在
 * 一步里可以有三条阻断,所以两者不相等 —— 这不是 bug。
 */
import { Progress, Space, Table, Tag, Tooltip, Empty } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useNavigate } from 'react-router-dom'
import type { SpuGroup, SpuSkuRow } from '../../api/batch'
import { FLOW_STEP_LABEL, type FlowStep } from '../../api/workbench'
import { brandVars, fontScale } from '../../theme'

/** 展开行:这一款下面的 SKU 明细。列比主表窄,因为它是"看一眼"而不是"在这里干活" */
function SkuDetail({ group }: { group: SpuGroup }) {
  const navigate = useNavigate()
  const columns: ColumnsType<SpuSkuRow> = [
    {
      title: 'SKU',
      dataIndex: 'sku',
      width: 200,
      render: (sku: string, row) => (
        <a className="mono" onClick={() => navigate(`/workbench/${row.product_id}`)}>
          {sku}
        </a>
      ),
    },
    {
      title: '颜色 / 尺码',
      key: 'variant',
      width: 140,
      render: (_, row) => (
        <span style={{ color: brandVars.slate }}>
          {[row.color, row.size].filter(Boolean).join(' / ') || '—'}
        </span>
      ),
    },
    {
      title: '完成度',
      dataIndex: 'completion',
      width: 120,
      render: (value: number, row) => (
        <Progress
          percent={value}
          size="small"
          strokeColor={row.blocking_count ? brandVars.danger : brandVars.marine}
        />
      ),
    },
    {
      title: '阻断',
      dataIndex: 'blocking_count',
      width: 70,
      align: 'center',
      render: (value: number) => (
        <Tag color={value ? 'error' : 'default'} style={{ marginInlineEnd: 0 }}>
          {value}
        </Tag>
      ),
    },
    {
      /* SKU 级的「下一步」有真实定义(后端 `flow._decide_next` 算的),
         所以这一层给它。款级那一层不给 —— 见文件头 */
      title: '下一步',
      dataIndex: 'next_action_label',
      render: (label: string) => label || <span style={{ color: brandVars.textFaint }}>—</span>,
    },
  ]

  return (
    <Table<SpuSkuRow>
      rowKey="product_id"
      size="small"
      bordered
      pagination={false}
      columns={columns}
      dataSource={group.skus}
    />
  )
}

export default function SpuGroupTable({
  groups,
  loading,
  total,
  page,
  pageSize,
  onPageChange,
}: {
  groups: SpuGroup[]
  loading: boolean
  total: number
  page: number
  pageSize: number
  onPageChange: (page: number, pageSize: number) => void
}) {
  const columns: ColumnsType<SpuGroup> = [
    {
      title: '款',
      key: 'spu',
      width: 240,
      fixed: 'left',
      render: (_, row) => (
        <div style={{ minWidth: 0 }}>
          <span className="mono">{row.spu}</span>
          <div style={{ color: brandVars.slate, fontSize: fontScale.meta }}>
            {row.name || '未命名'}
          </div>
        </div>
      ),
    },
    {
      title: 'SKU 数',
      dataIndex: 'sku_count',
      width: 80,
      align: 'center',
    },
    {
      title: '颜色数',
      key: 'colors',
      width: 80,
      align: 'center',
      // 变体维度由后端按标签给出(`VARIANT_FIELD_LABELS`),这里只读第一维
      render: (_, row) => (row.variants['颜色'] ?? []).length || '—',
    },
    {
      title: '完成度',
      key: 'completion',
      width: 130,
      render: (_, row) => (
        <Tooltip title="取旗下 SKU 的最小值,不是平均值 —— 上架文件是整个款一起导的,所以看最慢那只">
          <Progress
            percent={row.completion}
            size="small"
            strokeColor={row.blocking_count ? brandVars.danger : brandVars.marine}
          />
        </Tooltip>
      ),
    },
    {
      title: '阻断',
      key: 'blocking',
      width: 80,
      align: 'center',
      render: (_, row) => (
        <Tooltip title="旗下 SKU 阻断问题的条数之和。它和右边的卡点分布不是同一个口径:那边数的是 SKU 个数">
          <Tag color={row.blocking_count ? 'error' : 'default'} style={{ marginInlineEnd: 0 }}>
            {row.blocking_count}
          </Tag>
        </Tooltip>
      ),
    },
    {
      title: '卡点分布',
      key: 'blocked_steps',
      render: (_, row) => {
        const entries = Object.entries(row.blocked_steps)
        if (entries.length === 0) {
          return <span style={{ color: brandVars.textFaint }}>没有卡住的 SKU</span>
        }
        return (
          <Space size={4} wrap>
            {entries.map(([step, count]) => (
              <Tooltip key={step} title={`${count} 只 SKU 卡在这一步`}>
                <Tag color="error" style={{ marginInlineEnd: 0 }}>
                  {FLOW_STEP_LABEL[step as FlowStep] ?? step} {count}
                </Tag>
              </Tooltip>
            ))}
          </Space>
        )
      },
    },
  ]

  return (
    <Table<SpuGroup>
      rowKey="spu"
      size="small"
      bordered
      scroll={{ x: 900 }}
      columns={columns}
      dataSource={groups}
      loading={loading}
      expandable={{
        expandedRowRender: (row) => <SkuDetail group={row} />,
        rowExpandable: (row) => row.skus.length > 0,
      }}
      locale={{
        emptyText: <Empty description="没有款命中当前筛选。点「清除筛选」看全部。" />,
      }}
      pagination={{
        current: page,
        pageSize,
        total,
        showSizeChanger: true,
        showTotal: (t) => `共 ${t} 个款命中`,
        onChange: onPageChange,
      }}
    />
  )
}
