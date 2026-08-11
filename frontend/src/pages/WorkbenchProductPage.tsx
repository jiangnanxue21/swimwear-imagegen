/**
 * 工作台商品详情(FE-105:八个标签页 + 顶部固定进度区)。
 *
 * ## 八个标签与 §1.4 的八个流程环节不是一一对应的
 *
 * 任务书 FE-105 的说明里要求开工前确认这件事,并在不对应时列出标签清单。
 * 这里的清单是:
 *
 *     总览      五个子状态与全部问题的落点(徽标点过来的地方)
 *     素材      §1.4 环节 1-2:素材入库与检查
 *     属性      环节 3:属性识别与人工确认
 *     生成任务  环节 4:AI 出图。**只读观察窗**,建任务与审图仍在原页面
 *     图片集    环节 5:编排与批准
 *     文案      环节 6:生成、校验与批准
 *     草稿      环节 7:字段映射与校验
 *     导出      环节 8:出文件与留痕
 *
 * 差异只在第一个:总览不是一个流程环节,而是「问题都在哪」这个问题的答案。
 * 没有它的话,列表页徽标点击就只能落到某个具体步骤,而阻断问题常常横跨两步。
 *
 * ## tab 存 URL
 *
 * §3.5 步骤 5 的验收是「刷新商品详情页,当前步骤、完成度和问题状态不丢失」。
 * tab 放在查询参数里而不是 useState,刷新、后退、把链接发给同事都能停在原处。
 */
import { useMemo, type ReactNode } from 'react'
import { Link, useNavigate, useParams, useSearchParams } from 'react-router-dom'
import { Badge, Button, Skeleton, Space, Tabs } from 'antd'
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import ErrorNotice from '../components/ErrorNotice'
import {
  workbenchApi,
  type FlowStep,
  type FlowStepResult,
  type WorkbenchTab,
} from '../api/workbench'
import FlowHeader from '../components/workbench/FlowHeader'
import OverviewTab from '../components/workbench/OverviewTab'
import MaterialTab from '../components/workbench/MaterialTab'
import AttributeTab from '../components/workbench/AttributeTab'
import TasksTab from '../components/workbench/TasksTab'
import ImageSetTab from '../components/workbench/ImageSetTab'
import CopyTab from '../components/workbench/CopyTab'
import DraftTab from '../components/workbench/DraftTab'
import ExportTab from '../components/workbench/ExportTab'
import { brandVars } from '../theme'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

/**
 * 八个标签。`step` 是走查 P1-3 加的:标签要挂问题徽标,就得知道自己对应哪一步。
 *
 * 四个标签没有 `step`,因为它们不是流程步骤:
 *   总览      「问题都在哪」的答案,徽标会和它自己汇总的数字重复
 *   生成任务   出图不产生阻断:上架不要求有 AI 图,拍摄的正面图同样走得完
 *   导出       闸门清单在页内逐项可见,不进 flow.steps
 */
const TABS: { key: WorkbenchTab; label: string; step?: FlowStep }[] = [
  { key: 'overview', label: '总览' },
  { key: 'material', label: '素材', step: 'MATERIAL' },
  { key: 'attribute', label: '属性', step: 'ATTRIBUTE' },
  { key: 'tasks', label: '生成任务' },
  { key: 'imageset', label: '图片集', step: 'IMAGE_SET' },
  { key: 'copy', label: '文案', step: 'COPY' },
  { key: 'draft', label: '草稿', step: 'DRAFT' },
  { key: 'export', label: '导出' },
]

const TAB_KEYS = new Set<string>(TABS.map((t) => t.key))

export default function WorkbenchProductPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const [params, setParams] = useSearchParams()

  // 不认识的 tab 值一律退回总览,不白屏
  const raw = params.get('tab') ?? ''
  const tab: WorkbenchTab = TAB_KEYS.has(raw) ? (raw as WorkbenchTab) : 'overview'

  const query = useQuery({
    queryKey: ['workbench-flow', id],
    queryFn: () => workbenchApi.flow(id),
    enabled: Boolean(id),
  })

  const goTab = (next: WorkbenchTab) => {
    const merged = new URLSearchParams(params)
    merged.set('tab', next)
    setParams(merged, { replace: false })
  }

  // 走查 P1-5:带上 SKU。同时开几个详情标签时,标签栏上四个
  // 「商品展示图生产台」是分不出来的
  useDocumentTitle(query.data?.product.sku)

  const stepOf = useMemo(() => {
    const map = new Map<FlowStep, FlowStepResult>()
    for (const s of query.data?.flow.steps ?? []) map.set(s.step, s)
    return map
  }, [query.data])

  if (query.isLoading) return <Skeleton active paragraph={{ rows: 8 }} />

  if (query.isError) {
    return (
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Link to="/workbench">
          <Button size="small" icon={<ArrowLeftOutlined />}>
            返回工作台
          </Button>
        </Link>
        <ErrorNotice
          title="打不开这件商品"
          error={query.error}
          onRetry={() => query.refetch()}
        />
      </Space>
    )
  }

  const data = query.data!
  const { flow, product } = data

  const content: Record<WorkbenchTab, ReactNode> = {
    overview: <OverviewTab flow={flow} colors={query.data?.colors ?? null} onJump={goTab} />,
    material: <MaterialTab productId={id} step={stepOf.get('MATERIAL')} />,
    attribute: (
      <AttributeTab productId={id} step={stepOf.get('ATTRIBUTE')} product={product} />
    ),
    tasks: <TasksTab productId={id} />,
    imageset: (
      <ImageSetTab
        productId={id}
        spu={product.spu}
        imageSetId={data.image_set?.id ?? null}
        step={stepOf.get('IMAGE_SET')}
      />
    ),
    copy: <CopyTab productId={id} copy={data.copy} step={stepOf.get('COPY')} />,
    draft: (
      <DraftTab productId={id} draft={data.draft} step={stepOf.get('DRAFT')} onJump={goTab} />
    ),
    export: <ExportTab productId={id} draft={data.draft} />,
  }

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space>
        <Link to="/workbench">
          <Button size="small" icon={<ArrowLeftOutlined />}>
            返回工作台
          </Button>
        </Link>
        <Button
          size="small"
          icon={<ReloadOutlined />}
          loading={query.isFetching}
          onClick={() => {
            queryClient.invalidateQueries({ queryKey: ['workbench-flow', id] })
          }}
        >
          刷新状态
        </Button>
        {/*
          进向导的入口(阶段 6 / AC-01)。
          **没有做成侧栏菜单项**:向导是"这一件商品"的视图,菜单项没有
          商品 id 可带,点进去只能是一个选商品的空页 —— 那等于在主流程
          中间插一屏。入口放在商品身上,和「商品原始信息」并列。
        */}
        <Button size="small" onClick={() => navigate(`/wizard/${id}`)}>
          用向导做
        </Button>
        <Button size="small" onClick={() => navigate(`/products/${id}`)}>
          商品原始信息
        </Button>
      </Space>

      <FlowHeader
        product={product}
        flow={flow}
        thumbnail={data.thumbnail_url}
        onJump={goTab}
        actionPending={query.isFetching}
      />

      <Tabs
        activeKey={tab}
        onChange={(key) => goTab(key as WorkbenchTab)}
        items={TABS.map((t) => {
          /**
           * 走查 P1-3:标签徽标。
           *
           * 列表页做到了「不点开就能看出卡在哪」(五个状态列 + 阻断徽标),
           * 而走查前**一点进详情这个能力就消失了**:八个标签一样宽、一样黑、
           * 一样没信息。要找那三条阻断问题在哪个标签,只能靠总览页转一手,
           * 或者逐个点过去。上游做到的事情被下游一页抵消掉。
           *
           * 数字仍然全部来自后端 `flow.steps[].issues`,前端只做**过滤和计数**,
           * 不判定级别、不推算状态 —— §3.2.1 的纪律在这里同样成立。
           *
           * 阻断优先于待确认:两者都有时显示阻断数并标红。理由是阻断决定
           * 「能不能导出」,待确认只决定「要不要点一下」,同屏竞争时前者更急。
           */
          const step = t.step ? stepOf.get(t.step) : undefined
          const blocking = step?.issues.filter((i) => i.level === 'BLOCKING').length ?? 0
          const pending = step?.issues.filter((i) => i.level === 'NEEDS_CONFIRM').length ?? 0
          const count = blocking || pending
          return {
            key: t.key,
            label: count ? (
              <Badge
                count={count}
                color={blocking ? brandVars.danger : brandVars.sand}
                offset={[8, -2]}
                size="small"
                title={
                  blocking
                    ? `${blocking} 条阻断问题,不解决走不到导出`
                    : `${pending} 条等你确认`
                }
              >
                <span>{t.label}</span>
              </Badge>
            ) : (
              t.label
            ),
            // 只渲染当前标签:八个标签各自都有请求,全挂上去等于一进详情就打八次接口
            children: tab === t.key ? content[t.key] : null,
          }
        })}
      />
    </Space>
  )
}
