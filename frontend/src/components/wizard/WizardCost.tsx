/**
 * 向导上的费用预估卡(阶段 6 批次 6-5 / AC-15 的「费用预估」)。
 *
 * ## 三件事这一段绝不做
 *
 * 1. **不把"未配价"显示成 ¥0。** 后端给的是 `cost_display: null`,
 *    这里显示「未配价」。显示 0 的表现是运营看着"预计 ¥0"点下去,
 *    然后花了真钱 —— 一句有具体数值的假话。
 * 2. **不自己求和。** 合计按币种由后端给(micros 是某个币种的百万分之一,
 *    不同币种相加得到的数没有单位,但它长得像钱)。
 * 3. **不隐藏"算不出来的那部分"。** `is_complete` 为假时,总额旁边必须
 *    有一句话说清它不全 —— 一个总是偏低的预算数字比没有数字更危险。
 */
import { Alert, Card, Empty, Space, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { CostEstimate, CostLine } from '../../api/workbench'
import { brandVars, fontScale } from '../../theme'

const COLUMNS: ColumnsType<CostLine> = [
  { title: '动作', dataIndex: 'label', width: 110 },
  { title: '服务商', dataIndex: 'provider', width: 110, className: 'mono' },
  { title: '次数', dataIndex: 'units', width: 70 },
  {
    title: '预计金额',
    dataIndex: 'cost_display',
    width: 120,
    render: (value: string | null) =>
      value ?? (
        // 「未配价」而不是 ¥0:NULL 是"不知道",0 是"免费"
        <Tag color="default">未配价</Tag>
      ),
  },
  {
    title: '依据',
    dataIndex: 'basis',
    render: (basis: string) => (
      <span style={{ color: brandVars.slate, fontSize: fontScale.meta }}>{basis}</span>
    ),
  },
]

export default function WizardCost({ cost }: { cost: CostEstimate | null }) {
  // `null` = 没算(没有颜色维的存量行),不是"不要钱"。
  // 显示一个"合计 ¥0"的空卡就是把"没算"写成了"免费"
  if (!cost) {
    return (
      <Card size="small" title="费用预估">
        <Empty
          image={Empty.PRESENTED_IMAGE_SIMPLE}
          description="这件商品还没有 SPU 归属,算不出颜色维,因此也算不出预估。"
        />
      </Card>
    )
  }

  const totals = cost.totals.map((t) => t.cost_display).join(' + ')

  return (
    <Card
      size="small"
      title={
        <Space size={8}>
          <span>费用预估</span>
          {/* 它会和 /spend 的真实台账出现在同一个系统里,必须自报家门 */}
          <Tag color="default">预估,非台账</Tag>
        </Space>
      }
      extra={
        <strong data-testid="wizard-cost-total">
          {totals || '—'}
        </strong>
      }
    >
      <Space direction="vertical" size={8} style={{ width: '100%' }}>
        {!cost.is_complete && (
          <Alert
            type="warning"
            showIcon
            message="这个数不全"
            data-testid="wizard-cost-incomplete"
            description={
              <Space direction="vertical" size={2}>
                {cost.unpriced_operations.length > 0 && (
                  <span>
                    {cost.unpriced_operations.map((o) => o.label).join('、')}
                    :价目表里没有单价,金额未知(不是不要钱)。
                  </span>
                )}
                {cost.unknown.map((u) => (
                  <span key={u.operation}>
                    {u.label}:{u.reason}
                    {u.refs.length > 0 && `(${u.refs.join('、')})`}
                  </span>
                ))}
              </Space>
            }
          />
        )}

        {cost.lines.length > 0 ? (
          <Table<CostLine>
            rowKey={(row) => `${row.operation}-${row.basis}`}
            size="small"
            bordered
            pagination={false}
            columns={COLUMNS}
            dataSource={cost.lines}
          />
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description="按当前上游,接下来没有会计费的调用。"
          />
        )}

        {/* 「这一项为什么不在清单里」由后端说。少了它,一行的消失
            与"我们忘了算它"长得一模一样 */}
        {cost.notes.map((note) => (
          <div key={note} style={{ color: brandVars.slate, fontSize: fontScale.meta }}>
            · {note}
          </div>
        ))}

        <div style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
          {cost.disclaimer}
        </div>
      </Space>
    </Card>
  )
}
