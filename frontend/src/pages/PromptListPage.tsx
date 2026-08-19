import { useMemo } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Alert, Card, Space, Table, Tooltip, Typography } from 'antd'

import { promptsApi } from '../api/prompts'
import { isAuthError } from '../api/client'
import type { PromptSurface, PromptTier } from '../api/types'
import BrandTag from '../components/BrandTag'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { recentColumnTitle, recentLabel, unattributedNote } from '../utils/promptStats'
import { formatDateTime } from '../utils/datetime'

const { Text, Paragraph } = Typography

/**
 * 提示词列表(FE-301)。
 *
 * ## 这一页在补的是什么
 *
 * 仓库里有 8 处提示词,散在 6 个模块。在这一页之前**只有第 1 处能在界面上看到**,
 * 其余 7 处的存在只写在一份 PRD 的表格里 —— 表格不会在新增第 9 处时变红,
 * 也答不出"这个 key 的默认值还解析得出来吗"。a56 把那份盘点变成了机器可读的
 * 注册表(`app/prompts/registry.py`)并开了 `GET /prompts`,这一页是它的消费方。
 *
 * ## 不可编辑的项照样列出来,而且要说清为什么
 *
 * 8 处里只有评分两份的消费链路会读库。其余 6 处的正文由代码拼装或引用常量,
 * 库里存一版新的,链路**一个字都不会读** —— 给它们开编辑入口,得到的是
 * 「保存成功、毫无效果」,比不给入口更伤人,因为它连报错都没有。
 *
 * 但**不给入口不等于不列出来**:一个运营看不见的提示词,出问题时没有人会想到
 * 它的存在。所以这里全部列出,不可编辑的那些原样显示注册表里写的原因。
 *
 * ## 「近 N 天调用」这一列(a70 补齐,FE-301 至此完整)
 *
 * 这里原来写着「没有这一列,等统计接口」。a70 把它接上了,源是
 * `evaluation_attempts` —— 每一次评分请求成功与失败都留一行。
 *
 * **三件事由后端说,前端一个都不推测**(硬规则第 4 条):窗口天数走
 * `stats_window_days`(列头那句「近 N 天」的 N 不许写死)、失败率没有分母时
 * 后端给 `null` 而这里不 `?? 0`、统计起点走 `stats_since` ——
 * 少了最后一样,一个在迁移 0059 之前跑过 500 次的提示词会顶着「0 次」
 * 冒充没人用过,而那正是这一列要分开的两件事。
 *
 * 展示判定全在 `utils/promptStats.ts`(a61 那条「能不带 JSX 的部分就别带」)。
 */

const TIER_LABEL: Record<PromptTier, string> = {
  FREE: '全文可改',
  TEMPLATE: '模板(槽位固定)',
  MAPPING: '键值映射(只读)',
}

/** 分组顺序 = 可改的排前面。运营打开这一页是来改东西的,不是来读架构的。 */
const TIER_ORDER: PromptTier[] = ['FREE', 'TEMPLATE', 'MAPPING']

const TIER_NOTE: Record<PromptTier, string> = {
  FREE: '整段正文可以改。保存前会跑一遍体检,但不拦截。',
  TEMPLATE: '措辞可以改,必需槽位一个都不能少 —— 少一个,拼出来的提示词会缺一整块上下文。',
  MAPPING: '问题代码到修复动作的映射。改它等于改修复策略,风险高于改措辞,本轮只读。',
}

function reasonOf(surface: PromptSurface): string | null {
  // 注册表把原因写在 consumers 里(守卫强制它存在)。这里原样取那一行,
  // 不在前端重写一份说辞 —— 两份说辞迟早分叉,而分叉时读的人不知道信哪份。
  const hit = surface.consumers.find(
    (line) => line.includes('editable=False 的原因') || line.includes('ui_reachable=False 的原因'),
  )
  return hit ?? null
}

export default function PromptListPage() {
  useDocumentTitle('提示词')
  const query = useQuery({
    queryKey: ['prompt-surfaces'],
    queryFn: () => promptsApi.list(),
    retry: (count, err) => !isAuthError(err) && count < 2,
  })
  const needsLogin = query.isError && isAuthError(query.error)

  // 统计起点先转成人读得懂的形式再交给判定层 —— 那一层不该知道时区与格式。
  // 后端给 null(还没有任何带归属的留痕)时原样传 null,不要变成 "—":
  // 判定层拿 null 与拿一个占位串会说出两句不同的话
  const statsSince = query.data?.stats_since
    ? formatDateTime(query.data.stats_since)
    : null
  const unattributed = unattributedNote(query.data?.stats_unattributed)

  const grouped = useMemo(() => {
    const all = query.data?.surfaces ?? []
    return TIER_ORDER.map((tier) => ({
      tier,
      rows: all.filter((s) => s.tier === tier),
    })).filter((g) => g.rows.length > 0)
  }, [query.data])

  const columns = [
    {
      title: '提示词',
      dataIndex: 'label',
      render: (label: string, row: PromptSurface) =>
        row.editable && row.ui_reachable ? (
          <Link to={`/prompts/${row.key}`}>{label}</Link>
        ) : (
          <Text>{label}</Text>
        ),
    },
    {
      title: '当前生效',
      width: 160,
      render: (_: unknown, row: PromptSurface) => {
        if (!row.editable) return <Text type="secondary">由代码拼装</Text>
        return row.active_version == null ? (
          <BrandTag tone="sand">内置默认</BrandTag>
        ) : (
          <BrandTag tone="accent">第 {row.active_version} 版</BrandTag>
        )
      },
    },
    {
      title: recentColumnTitle(query.data?.stats_window_days),
      width: 210,
      render: (_: unknown, row: PromptSurface) => {
        // `stats` 只有 editable 的项带 —— 其余几处的链路不读库,调用不经过
        // 评分留痕。`recentLabel` 对 undefined 给「不统计」而不是「0 次」
        const display = recentLabel(row.stats, statsSince)
        const body = (
          <Text type={display.tone === 'muted' ? 'secondary' : undefined}>{display.text}</Text>
        )
        return display.hint ? <Tooltip title={display.hint}>{body}</Tooltip> : body
      },
    },
    {
      title: '必需槽位',
      dataIndex: 'required_slots',
      width: 260,
      render: (slots: string[]) =>
        slots.length ? (
          <Space size={4} wrap>
            {slots.map((slot) => (
              <BrandTag key={slot} tone="slate">
                {slot}
              </BrandTag>
            ))}
          </Space>
        ) : (
          <Text type="secondary">—</Text>
        ),
    },
    {
      title: '状态',
      width: 300,
      render: (_: unknown, row: PromptSurface) => {
        if (row.editable && row.ui_reachable) return <Text type="secondary">可在此编辑</Text>
        const reason = reasonOf(row)
        return (
          <Text type="secondary">
            {reason ?? '注册表里没写原因 —— 这本身是个缺陷,请报给管理员'}
          </Text>
        )
      },
    },
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <PageHeader
        title="提示词"
        subtitle="全仓 8 处提示词的盘点。能改的进详情页改,不能改的写明为什么"
      />

      {needsLogin && (
        <Alert
          type="warning"
          showIcon
          message="需要管理员身份才能查看提示词"
          description="请登录管理员账号后重试。"
        />
      )}
      {query.isError && !needsLogin && (
        <ErrorNotice
          title="拉不到提示词清单"
          error={query.error}
          onRetry={() => query.refetch()}
          retrying={query.isFetching}
        />
      )}

      <Alert
        type="info"
        showIcon
        message="「不可编辑」的意思是改了不会生效,不是不重要"
        description={
          <Paragraph style={{ marginBottom: 0 }}>
            这 8 处里只有评分用的两份会在运行时读数据库。其余几处的正文由代码拼装,
            存一版新的进库,链路<Text strong>一个字都不会读</Text> —— 所以这里不给编辑入口,
            而不是给一个保存成功却毫无效果的按钮。要改它们,改代码。
          </Paragraph>
        }
      />

      {unattributed && (
        <Alert type="info" showIcon message="部分调用归不到具体提示词上" description={unattributed} />
      )}

      {grouped.map(({ tier, rows }) => (
        <Card key={tier} title={TIER_LABEL[tier]} loading={query.isLoading}>
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Text type="secondary">{TIER_NOTE[tier]}</Text>
            <Table
              rowKey="key"
              size="small"
              pagination={false}
              columns={columns}
              dataSource={rows}
            />
          </Space>
        </Card>
      ))}
    </Space>
  )
}
