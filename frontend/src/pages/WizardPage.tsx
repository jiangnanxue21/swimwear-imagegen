/**
 * 一体化向导(PRD §13 阶段 6 批次 6-4:AC-01 / AC-05 / AC-16)。
 *
 * ## 它和详情页的八个标签是什么关系
 *
 * **同一批面板,换一种编排。** 每一步的操作面板就是详情页那几个 Tab 组件
 * (`MaterialTab` / `AttributeTab` / …),不另写一套 —— 另写一套的表现是
 * 同一件事在两个页面上有两种做法,而运营会在其中一个页面上找不到某个按钮。
 *
 * 差别在于**编排**:详情页把八个标签平铺,谁先谁后由人自己想;向导按
 * `STEP_ORDER` 排成一条路,并把"这一步现在能不能做、不能是谁挡着"
 * 显式说出来(AC-01)。
 *
 * ## AC-16 刷新恢复:URL 是唯一真相
 *
 * 当前步与当前颜色都住在 URL 里(`?step=IMAGE_SET&color=BLU`),走
 * `useUrlFilters` —— 与 GAP-033 把筛选搬进 URL 是同一条路,理由也同一条:
 * 组件内存一份就有了第二处真相,而刷新、后退、把链接发给同事三件事里
 * 至少有两件会跟它对不上。
 *
 * 没有 `step` 参数时落在后端算出来的 `current_step` 上。**不是落在第一步**:
 * 一件做到第五步的商品每次打开都从建档看起,运营要点四次才回到他昨天的位置。
 *
 * ## AC-16 的后半句:不因刷新重复提交
 *
 * 这一页**在挂载时不发起任何写请求**。所有写动作都由运营点出来,
 * 而且每一个都落在既有的幂等入口上(文案生成的幂等单元、识别 run 的
 * 幂等键、发布的提交键)。刷新只是重新 GET 一次判定。
 * 这条由 `tests/component/wizard.test.tsx` 的「挂载不发 POST」钉着 ——
 * 一个写在 `useEffect` 里的"自动继续"会让刷新变成一次重复付费调用。
 */
import { useMemo } from 'react'
import { Link, useParams } from 'react-router-dom'
import { Alert, Button, Card, Col, Empty, Row, Select, Skeleton, Space, Tag } from 'antd'
import { ArrowLeftOutlined, ReloadOutlined } from '@ant-design/icons'
import { useQuery, useQueryClient } from '@tanstack/react-query'

import ErrorNotice from '../components/ErrorNotice'
import GenerationPlanPanel from '../components/GenerationPlanPanel'
import MaterialTab from '../components/workbench/MaterialTab'
import AttributeTab from '../components/workbench/AttributeTab'
import ImageSetTab from '../components/workbench/ImageSetTab'
import CopyTab from '../components/workbench/CopyTab'
import DraftTab from '../components/workbench/DraftTab'
import ExportTab from '../components/workbench/ExportTab'
import WizardRail from '../components/wizard/WizardRail'
import WizardCost from '../components/wizard/WizardCost'
import ImpactPreview from '../components/wizard/ImpactPreview'
import { IssueList } from '../components/workbench/FlowBits'
import {
  FLOW_STEP_ORDER,
  workbenchApi,
  type FlowStep,
  type FlowStepResult,
  type ProductFlowDetail,
} from '../api/workbench'
import type { Audience } from '../api/types'
import { enumParam, textParam, useUrlFilters } from '../hooks/useUrlFilters'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { brandVars, fontScale } from '../theme'

/** SETUP 步的面板。**唯一一个把人送出向导的步骤,理由写在下面。** */
function SetupPanel({
  spuId,
  step,
}: {
  spuId: string | null
  step: FlowStepResult | undefined
}) {
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      {/*
        AC-01 说"跳详情页只允许发生在高级异常上"。归属没挂正是那一类:
        它只出现在存量行与导入行上,而修它要人**决定**这个 SKU 属于哪个颜色
        —— 系统按 `spu` 字符串码反查是 §3.39 明令禁掉的形状(那个码可能
        已经因改名而断开,反查出来的是另一个款)。所以这一步给的是一条
        带理由的链接,不是一个替他猜的按钮。
      */}
      <IssueList issues={step?.issues ?? []} empty="归属已挂好,这一步没有待办。" />
      {spuId ? (
        <Link to={`/spus/${spuId}`}>
          <Button size="small">打开 SPU 页核对颜色与 SKU</Button>
        </Link>
      ) : (
        <Alert
          type="warning"
          showIcon
          message="这一行还没有 SPU 归属"
          description="在「商品」页把它挂到一个 SPU 与颜色上,后面每一步的作用域都依赖它。"
        />
      )}
    </Space>
  )
}

/** 方案步的面板:直接把 SPU 详情页那块生成方案面板搬进来(不另写一套)。 */
function PlanPanel({
  spuId,
  variantLabels,
  productAudience,
  selectedVariantId,
}: {
  spuId: string | null
  variantLabels: Record<string, string>
  /** 受众下传只为一件事:按 §10.5 收窄模特候选集。`null` = 待确认 */
  productAudience: Audience | null
  selectedVariantId: string | null
}) {
  if (!spuId) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="没有 SPU 归属,方案按 SPU 与颜色存,现在配不了。先做完建档那一步。"
      />
    )
  }
  return (
    <GenerationPlanPanel
      spuId={spuId}
      variantLabels={variantLabels}
      productAudience={productAudience}
      initialColorVariantId={selectedVariantId}
    />
  )
}

function StepPanel({
  step,
  data,
  stepOf,
}: {
  step: FlowStep
  data: ProductFlowDetail
  stepOf: Map<FlowStep, FlowStepResult>
}) {
  const baseId = data.product.id
  const selectedVariantId = data.color_selection?.variant_id ?? null
  // 没有颜色轴的旧商品继续用路由商品;有颜色轴却没有目标 SKU 时必须停住。
  // 借原颜色的 id 顶上去会让界面显示 BLU、实际写 RED。
  const targetId = data.color_selection?.product_id ?? (data.colors ? null : baseId)
  const needsTarget = (
    <Empty
      image={Empty.PRESENTED_IMAGE_SIMPLE}
      description="这个颜色还没有 SKU,不能执行该操作。先回建档补齐颜色与 SKU。"
    />
  )
  const variantLabels = Object.fromEntries(
    (data.colors?.colors ?? []).map((c) => [c.variant_id, c.variant_code]),
  )

  switch (step) {
    case 'SETUP':
      return <SetupPanel spuId={data.product.spu_id} step={stepOf.get('SETUP')} />
    case 'MATERIAL':
      return targetId ? (
        <MaterialTab
          productId={targetId}
          flowProductId={baseId}
          step={stepOf.get('MATERIAL')}
          focusColorVariantId={selectedVariantId}
        />
      ) : needsTarget
    case 'ATTRIBUTE':
      return targetId ? (
        <AttributeTab
          productId={targetId}
          flowProductId={baseId}
          step={stepOf.get('ATTRIBUTE')}
          product={data.product}
        />
      ) : needsTarget
    case 'PLAN':
      return (
        <PlanPanel
          spuId={data.product.spu_id}
          variantLabels={variantLabels}
          productAudience={data.product.audience}
          selectedVariantId={selectedVariantId}
        />
      )
    case 'IMAGE_SET':
      return targetId ? (
        <ImageSetTab
          productId={targetId}
          flowProductId={baseId}
          spu={data.product.spu}
          imageSetId={data.image_set?.id ?? null}
          step={stepOf.get('IMAGE_SET')}
        />
      ) : needsTarget
    case 'COPY':
      return targetId ? (
        <CopyTab
          productId={targetId}
          flowProductId={baseId}
          copy={data.copy}
          step={stepOf.get('COPY')}
        />
      ) : needsTarget
    case 'DRAFT':
      // 草稿与导出在同一步下:PRD §16 的第七行是"形成完整 Draft",
      // 而导出是它的出口。拆成两步的话向导会有八步,与七步口径分叉
      return (
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <DraftTab
            productId={baseId}
            draft={data.draft}
            step={stepOf.get('DRAFT')}
            onJump={() => {}}
          />
          <ExportTab productId={baseId} draft={data.draft} />
        </Space>
      )
  }
}

export default function WizardPage() {
  const { id = '' } = useParams()
  const queryClient = useQueryClient()

  // AC-16:当前步与当前颜色住在 URL 里。`step` 用白名单 codec ——
  // 手敲一个不存在的步骤应该落回当前步,不是白屏
  const { values, patch } = useUrlFilters({
    step: enumParam<FlowStep>(FLOW_STEP_ORDER),
    color: textParam(),
  })

  const query = useQuery({
    queryKey: ['workbench-flow', id, values.color || 'DEFAULT'],
    queryFn: () => workbenchApi.flow(id, values.color || undefined),
    enabled: Boolean(id),
  })

  useDocumentTitle(query.data?.product.sku)

  const stepOf = useMemo(() => {
    const map = new Map<FlowStep, FlowStepResult>()
    for (const s of query.data?.flow.steps ?? []) map.set(s.step, s)
    return map
  }, [query.data])

  if (query.isLoading) return <Skeleton active paragraph={{ rows: 10 }} />

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
  const wizard = data.wizard

  // URL 上的那一步优先(刷新恢复);没有就落在后端算出来的当前步。
  // **不落第一步** —— 一件做到第五步的商品每次打开都从建档看起,
  // 运营要点四次才回到昨天的位置
  const active: FlowStep = values.step ?? wizard.current_step
  const activeStep = wizard.steps.find((s) => s.step === active)
  // 手敲一个关着的步骤:如实说它关着,而不是偷偷跳走。
  // 偷偷跳走的表现是地址栏和页面对不上,而运营把这个链接发给了同事
  const openable = activeStep?.is_open ?? false
  const focusColor = data.color_selection?.variant_code ?? wizard.focus_color
  const colorOptions = (data.colors?.colors ?? []).map((row) => ({
    value: row.variant_code,
    label: row.is_active ? row.variant_code : `${row.variant_code}（非在售）`,
    disabled: !row.is_active,
  }))

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space wrap>
        <Link to="/workbench">
          <Button size="small" icon={<ArrowLeftOutlined />}>
            返回工作台
          </Button>
        </Link>
        <Link to={`/workbench/${id}`}>
          <Button size="small">切换到标签页视图</Button>
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
        <span className="mono" style={{ color: brandVars.slate }}>
          {data.product.sku} · 完成度 {data.flow.completion}%
        </span>
        {colorOptions.length > 0 && (
          <Select<string>
            size="small"
            style={{ minWidth: 150 }}
            data-testid="wizard-color-select"
            aria-label="当前操作颜色"
            value={focusColor ?? undefined}
            placeholder="选择操作颜色"
            options={colorOptions}
            onChange={(color) => patch({ color })}
          />
        )}
        {focusColor && (
          <Tag color="processing" data-testid="wizard-focus-color">
            当前操作:{focusColor}
          </Tag>
        )}
      </Space>

      {/* 阻塞清单。级别由后端定,前端只显示 —— 这里不做二次分级 */}
      {wizard.blocking.length > 0 && (
        <Alert
          type="error"
          showIcon
          data-testid="wizard-blocking"
          message={`${wizard.blocking.length} 条阻断,不解决走不到导出`}
          description={
            <Space direction="vertical" size={2}>
              {wizard.blocking.map((b) => (
                <span key={`${b.code}-${b.message}`}>{b.message}</span>
              ))}
            </Space>
          }
        />
      )}

      <Row gutter={12}>
        <Col xs={24} lg={7}>
          <Card size="small" title="七步">
            <WizardRail
              steps={wizard.steps}
              activeStep={active}
              onPick={(s) => patch({ step: s.step })}
            />
          </Card>
        </Col>
        <Col xs={24} lg={17}>
          <Space direction="vertical" size={10} style={{ width: '100%' }}>
            <Card
              size="small"
              title={
                <Space size={8}>
                  <span>{activeStep?.label ?? active}</span>
                  <span
                    style={{
                      color: brandVars.slate,
                      fontWeight: 400,
                      fontSize: fontScale.meta,
                    }}
                  >
                    {activeStep?.intro}
                  </span>
                </Space>
              }
            >
              {openable ? (
                <StepPanel step={active} data={data} stepOf={stepOf} />
              ) : (
                /* 关着的步骤不渲染面板。渲染出来会让运营在一个做不了的
                   步骤上填半天,然后被服务端 409 —— 而 AC-05 那道闸
                   本来就会拒,这里只是别让他白干 */
                <Alert
                  type="info"
                  showIcon
                  data-testid="wizard-gate-reason"
                  message="这一步现在做不了"
                  description={activeStep?.gate_reason || '上游还没就绪'}
                />
              )}
            </Card>

            <WizardCost cost={wizard.cost} />
            <ImpactPreview productId={id} />
          </Space>
        </Col>
      </Row>
    </Space>
  )
}
