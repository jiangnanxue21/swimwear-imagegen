/**
 * 付费调用花费(A23)。
 *
 * ## 这一页要回答三个问题,顺序就是运营关心的顺序
 *
 *     1. 这个月花了多少、还剩多少预算       —— 顶部预算卡
 *     2. 钱花在哪个 provider 上             —— 明细表
 *     3. 有没有花了钱但没算进来的           —— 未配价提示 + 非主币种提示
 *
 * 第 3 条最容易被略过,也最容易让人低估开销。它有**两个来源**,两个都会让
 * 「本月已花费」偏小,而且都不报错:
 *
 *     没配单价的调用记 0 元        一整个 provider 的开销被一条平坦的绿线盖住
 *     非主币种的调用不进这个数      后端 `spent` 只求和主币种(不折算是对的,
 *                                  汇率是又一个"看起来精确、实际是估的"数字),
 *                                  但"少了多少"必须说出来
 *
 * 所以它们不是脚注,是两条独立的 Alert。
 *
 * ## 「预算」不是「余额」
 *
 * 页脚那句 disclaimer 由后端返回、原样显示,不在前端改写 —— 它必须和产生
 * 数字的地方待在一起。我们能知道的是本系统发起的调用花了多少,不是厂商
 * 账户里还剩多少;共用同一把 Key 的其他系统、赠送额度、失败但仍计费的调用
 * 都不在其中。
 */
import { Alert, Button, Card, Progress, Skeleton, Space, Statistic, Table, Tooltip, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { BUDGET_LABEL, usageApi, type ProviderSpend } from '../api/usage'
import { readError } from '../api/client'
import BrandTag from '../components/BrandTag'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { brandVars, fontScale, space } from '../theme'

export default function SpendPage() {
  useDocumentTitle('付费调用花费')
  const { data, isLoading, isError, error, refetch } = useQuery({
    queryKey: ['usage', 'spend'],
    queryFn: usageApi.spend,
    refetchInterval: 5 * 60_000,
  })

  const columns = [
    {
      title: '服务商',
      dataIndex: 'provider',
      key: 'provider',
      render: (v: string) => <strong>{v}</strong>,
    },
    { title: '本月花费', dataIndex: 'cost_display', key: 'cost' },
    { title: '调用次数', dataIndex: 'calls', key: 'calls' },
    { title: '计费单位', dataIndex: 'billable_units', key: 'units' },
    {
      title: '未配价',
      dataIndex: 'unpriced_calls',
      key: 'unpriced',
      // 0 不显示标签 —— 一列整齐的绿色「0」会稀释掉真正非零的那几行
      render: (v: number) =>
        v > 0 ? (
          <BrandTag tone="warning">{v} 次未计入</BrandTag>
        ) : (
          <span style={{ opacity: 0.4 }}>—</span>
        ),
    },
  ]

  const budget = data?.budget
  const percent = budget ? Math.min(Math.round(budget.used_ratio * 100), 999) : 0

  /*
   * 「本月已花费」只是主币种那一部分。这里挑出**其余币种**,金额字符串
   * 一律用后端的 `cost_display` —— 前端不自己格式化货币。
   *
   * 兜底 `?? []`:老后端没有这个字段,此时不提示(和以前一样),
   * 而不是渲染一个 undefined。
   *
   * 过滤条件是「有钱**或者**有调用」,不是只看金额:一个只有未配价调用的
   * 币种金额正好是 0,只看金额会把它整行丢掉,而它的花费不是 0 是未知。
   * 顶层那条 `unpriced_calls` 提示确实会亮,但它不说是哪个币种 ——
   * 运营看到「有 3 次调用没算进钱里」,却找不到那 3 次在哪。
   * `?? 0` 兜老后端:没有 calls 字段时退回只看金额,也就是以前的行为。
   */
  const otherCurrencies = (data?.by_currency ?? []).filter(
    (c) => !c.is_main && (c.cost_micros > 0 || (c.calls ?? 0) > 0),
  )
  /** 其中金额为 0、只有未配价调用的那些。它们要用另一句话说。 */
  const unpricedOnlyCurrencies = otherCurrencies.filter((c) => c.cost_micros <= 0)
  const pricedOtherCurrencies = otherCurrencies.filter((c) => c.cost_micros > 0)

  return (
    <Space direction="vertical" size={16} style={{ width: '100%', maxWidth: 960 }}>
      <PageHeader
        title="付费调用花费"
        subtitle="本月在图片生成与视觉/文本模型上的开销,以及相对预算的位置"
      />

      {isError && (
        <Alert
          type="error"
          showIcon
          message="拿不到花费数据"
          description={readError(error)}
          action={
            // 纯动作,没有目标地址 —— 用按钮而不是无 href 的锚点:
            // 后者不进 Tab 序列,键盘用户到不了这颗"重试"
            <Button type="link" size="small" onClick={() => void refetch()}>
              重试
            </Button>
          }
        />
      )}

      {isLoading && <Skeleton active paragraph={{ rows: 4 }} />}

      {data && (
        <>
          <Card size="small">
            <Space size={48} wrap>
              <Statistic
                title={
                  otherCurrencies.length ? (
                    <Tooltip title={`不含 ${otherCurrencies.map((c) => c.currency).join('、')} 的调用`}>
                      {`${data.period.month} 已花费(仅 ${data.currency})`}
                    </Tooltip>
                  ) : (
                    `${data.period.month} 已花费`
                  )
                }
                value={data.spent_display}
              />
              <Statistic
                title="本月预算"
                value={budget && budget.budget_micros > 0 ? budget.budget_display : '未设置'}
              />
              <Statistic
                title="按当前速度外推整月"
                value={budget?.projected_month_display ?? '—'}
              />
            </Space>

            {budget && budget.budget_micros > 0 ? (
              <div style={{ marginTop: space.lg }}>
                <Progress
                  percent={percent}
                  status={
                    budget.level === 'EXCEEDED' || budget.level === 'CRITICAL'
                      ? 'exception'
                      : 'normal'
                  }
                />
                <Typography.Text type="secondary" style={{ fontSize: fontScale.meta }}>
                  {BUDGET_LABEL[budget.level]}
                  {budget.projected_exhaustion_day !== null &&
                    ` · 按目前速度约在本月 ${budget.projected_exhaustion_day} 号用完`}
                  {budget.projected_exhaustion_day === null &&
                    budget.projected_month_micros !== null &&
                    ' · 按目前速度本月花不完'}
                </Typography.Text>
              </div>
            ) : (
              <Alert
                style={{ marginTop: space.lg }}
                type="info"
                showIcon
                message="还没有设置月度预算"
                description="设置后这一页才能判断快不快花完。改 .env 里的 SPEND_MONTHLY_BUDGET_MICROS(微单位,1 货币单位 = 1000000)。"
              />
            )}
          </Card>

          {data.price_book_error && (
            // 排在「未计价调用」之前:价目表坏掉时那一条必然也会亮,
            // 而它只是这一条的**后果**。先说原因,否则运营会照着后一条
            // 去补单价 —— 而单价本来就在那里,是解析失败了
            <Alert
              type="error"
              showIcon
              message="价目表解析失败,本页金额全部不可信"
              description={`所有调用都被记成「未计价」,因此「本月花费」显示的 0 不代表没花钱。请检查 .env 的 PROVIDER_PRICE_BOOK。原因:${data.price_book_error}`}
            />
          )}

          {data.unpriced_calls > 0 && (
            <Alert
              type="warning"
              showIcon
              message={`有 ${data.unpriced_calls} 次调用没有计入金额`}
              description="这些服务商还没配单价,它们的开销不是 0,是未知。在 .env 的 PROVIDER_PRICE_BOOK 里补上单价,这一页才是完整的。"
            />
          )}

          {otherCurrencies.length > 0 && (
            /*
              * 不折算、不求和,只是把每一种原样列出来 —— 后端 `_daily` 的
              * 注释里写着为什么:不同币种的 micros 相加得到的数没有单位。
              * 这里同理,把两个数并排放着,比给出一个"约合"要诚实。
              *
              * 预算判定也只对主币种做,所以这句话还回答了另一个问题:
              * 进度条为什么没把这笔钱算进去。
              */
            <Alert
              type="warning"
              showIcon
              message={[
                pricedOtherCurrencies.length
                  ? `另有 ${pricedOtherCurrencies.map((c) => c.cost_display).join('、')} 没有计入上面的「已花费」`
                  : '',
                /*
                 * 金额是 0 的那些**单独说一句**。混进上面那串里会渲染成
                 * 「另有 0.00 BRL 没有计入已花费」—— 读起来像"这个币种没花钱",
                 * 而真相是"花了多少不知道"。这两句话会导向完全不同的下一步。
                 */
                unpricedOnlyCurrencies.length
                  ? `${unpricedOnlyCurrencies
                      .map((c) => `${c.currency}(${c.unpriced_calls} 次)`)
                      .join('、')} 有调用但还没配单价,金额未知`
                  : '',
              ]
                .filter(Boolean)
                .join(';')}
              description={`「已花费」与预算进度条只统计主币种 ${data.currency}。不同币种不做汇率折算——折算出来的数看着精确,实际是估的,而这一页是用来提前发现异常的,不是用来对账的。各币种的明细见下表。`}
            />
          )}

          <Card size="small" title="按服务商">
            <Table<ProviderSpend>
              size="small"
              rowKey={(r) => `${r.provider}-${r.currency}`}
              columns={columns}
              dataSource={data.by_provider}
              pagination={false}
              locale={{ emptyText: '本月还没有付费调用' }}
            />
          </Card>

          <Typography.Paragraph
            type="secondary"
            style={{ fontSize: fontScale.meta, color: brandVars.textMuted }}
          >
            {data.disclaimer}
            {' '}价目表版本 {data.pricing_version}。
          </Typography.Paragraph>
        </>
      )}
    </Space>
  )
}
