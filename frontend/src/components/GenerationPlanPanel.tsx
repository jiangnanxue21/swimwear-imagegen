/**
 * 生成方案面板(阶段 4,PRD v3.1 §6.4 / §7.5)。
 *
 * 向导第四步「按颜色选择模特和生成方案」的落点。三件事:
 *
 *   列出   SPU 默认 + 各颜色覆盖 + 已归档(归档也要显示 —— 界面得能回答
 *          「这批图是按哪份方案出的」,而那一份多半已经被顶下去了)
 *   新建   落 DRAFT。**不提供「直接改已启用的那份」**,理由见 api/generationPlans.ts
 *   启用   点之前先显示"这会让哪些颜色的图片集过期"(§7.5)
 *
 * ## 状态一律来自后端
 *
 * 「哪一份对这个颜色生效」由后端解析(`resolve_plan`),前端不自己按
 * `color_variant_id` 挑一份 —— 那条规则(颜色覆盖优先、回落 SPU 默认、
 * 只看 ACTIVE)在后端是可穷举的判定,在这里重写一遍就变成第二个答案。
 * 硬规则第 4 条。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import {
  App, AutoComplete, Button, Empty, Form, InputNumber, Modal, Select, Space, Table, Tag,
} from 'antd'

import {
  IMAGE_ANGLE_LABEL,
  PLAN_STATUS_LABEL,
  activateGenerationPlan,
  createGenerationPlan,
  listGenerationPlans,
  previewActivation,
  type GenerationPlan,
  type ImageAngle,
  type PlanActivationEffect,
} from '../api/generationPlans'
import { modelTemplatesApi, providersApi } from '../api/generation'
import {
  AUDIENCE_LABEL,
  isProviderSelectable,
  providerOptionLabel,
  type Audience,
} from '../api/types'
import { readError } from '../api/client'
import { useWriteError } from '../hooks/useWriteError'

interface Props {
  spuId: string
  /** 颜色 id → 显示名。**显示一律用名字,不用 id** */
  variantLabels?: Record<string, string>
  /** 向导当前颜色。SPU 详情页不传,表单仍默认 SPU 级。 */
  initialColorVariantId?: string | null
  /**
   * 这件商品的受众。**只用来收窄模特候选集(§10.5),不用来做别的判断。**
   *
   * `null` = 待确认。那一档候选集不收窄,并在表单上如实说一句 ——
   * 与 `TaskCreateModal` 同一个处理:不收窄和"收窄之后正好是全部"
   * 在界面上长得一样,而前者选出来的模特可能是错的。
   *
   * 可选:SPU 详情页是 SPU 作用域,那里一个 SPU 下可能有多行 SKU,
   * 没有唯一的商品受众可传。不传就是不收窄,并且界面会说出来。
   */
  productAudience?: Audience | null
}

const ANGLE_OPTIONS = (Object.keys(IMAGE_ANGLE_LABEL) as ImageAngle[]).map((value) => ({
  value,
  label: IMAGE_ANGLE_LABEL[value],
}))

/**
 * 稳定键由后端 `generation_plan.SCENE_PROMPT_PROFILES` 展开成完整提示词。
 * AutoComplete 仍允许自由输入：业务场景不该被四个预设封死；自由文本在后端
 * 原样进入 prompt，不会被猜成某个相近场景。
 */
const SCENE_OPTIONS = [
  { value: 'STUDIO_CLEAN', label: '干净影棚（默认）' },
  { value: 'BEACH_DAYLIGHT', label: '自然日光海滩' },
  { value: 'POOLSIDE_DAYLIGHT', label: '自然日光池畔' },
  { value: 'LIFESTYLE_OUTDOOR', label: '户外生活方式' },
]

const POSE_OPTIONS = [
  { value: 'NATURAL_STANDING', label: '自然站立（默认）' },
  { value: 'RELAXED_WALKING', label: '自然行走' },
  { value: 'CATALOG_SIDE', label: '商品目录侧身' },
]

export default function GenerationPlanPanel({
  spuId,
  variantLabels = {},
  productAudience = null,
  initialColorVariantId,
}: Props) {
  const { message } = App.useApp()
  const [plans, setPlans] = useState<GenerationPlan[]>([])
  const [loading, setLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [effect, setEffect] = useState<PlanActivationEffect | null>(null)
  const [form] = Form.useForm()

  useEffect(() => {
    if (initialColorVariantId !== undefined) {
      form.setFieldValue('color_variant_id', initialColorVariantId)
    }
  }, [form, initialColorVariantId])

  /*
   * 模特候选集。**query key 带上受众** —— 与 `TaskCreateModal` 同一条:
   * 少了它,换一件商品之后列表不会重查,运营会对着上一件商品的候选集
   * 做选择,而那正是 §10.5 要防的事。
   */
  const templates = useQuery({
    queryKey: ['model-templates', 'enabled', productAudience ?? 'UNCONFIRMED'],
    queryFn: () =>
      modelTemplatesApi.list(true, {
        forProductAudience: productAudience ?? undefined,
      }),
  })

  /**
   * Provider 下拉的数据源(2026-08-11 评审)。
   *
   * 这里原来是三个硬编码字面量 `mock / fashn / comfyui`,而后端
   * `registry.IMPLEMENTED_PROVIDERS` 只有 `{mock, fashn}` 并且会明确拒绝
   * comfyui。于是这条动线是通的:选 comfyui -> 存草稿 -> 启用 -> 一切正常 ->
   * 三天后创建生成任务 -> `CONFIG_INVALID`,而报错指向的是任务不是方案。
   * FASHN 填了 Key 也是同一条(`configured` 为假时同样进不了任务)。
   *
   * `/api/providers` 一直就在报 `implemented` / `configured`,只是这张表单
   * 没读它。判据用共用的 `isProviderSelectable`,和 `TaskCreateModal` 同一份 ——
   * 两处各写一遍的表现是"任务弹窗里选不到的那家,方案里能选"。
   *
   * 后端从这一轮起也拦(`generation_plan_service._assert_plan_usable` 递
   * `registry.selectable_names()`)。这里是可发现性,那里是边界 —— 两处都要有。
   */
  /** 当前勾了哪些角度。每个角度的"几张"输入框跟着它渲染 */
  const selectedAngles = Form.useWatch('angles', form) as ImageAngle[] | undefined

  const providers = useQuery({ queryKey: ['providers'], queryFn: providersApi.list })
  const providerOptions = (providers.data ?? []).map((p) => ({
    value: p.name,
    label: providerOptionLabel(p),
    // 不可选的**仍然列出来但禁用**,并把原因写在标签上。整个隐藏的话,
    // "为什么没有 comfyui" 这个问题在界面上无法回答,而运营会去怀疑自己
    disabled: !isProviderSelectable(p),
  }))

  /**
   * 重读方案列表。
   *
   * ## 为什么这里用 `readError` 而不是 `reportWriteError`
   *
   * 原来这一处走的是 `reportWriteError`,而 `useWriteError` 在 `UNKNOWN`
   * (`client.ts`:没拿到响应 —— 超时或断网)时会回调 `onUnknown` 去重读一次。
   * 本函数**正是**那个 `onUnknown`,断网时就成了自己调自己的无限重读。
   *
   * 而且这是一次**读**:读失败不存在"可能已经发生"的问题,
   * `UNKNOWN` 那套话术("先刷新、先核对")对它没有意义。
   *
   * 顺带说明原来那行为什么没炸成这样:`useWriteError()` 当时一个参数都没传,
   * `onUnknown` 是 `undefined`,于是断网时报的是
   * `onUnknown is not a function` —— 类型错误 TS2554 底下压着的是这个。
   */
  const reload = useCallback(async () => {
    setLoading(true)
    try {
      setPlans(await listGenerationPlans(spuId))
    } catch (error) {
      message.error(readError(error))
    } finally {
      setLoading(false)
    }
  }, [spuId, message])

  // 写失败且结果未知时重读一次:方案可能已经建了/已经启用了,
  // 下一个动作是"看一眼现在是什么状态",不是"再点一次"
  const reportWriteError = useWriteError(reload)

  useEffect(() => {
    void reload()
  }, [reload])

  const scopeLabel = useCallback(
    (variantId: string | null) =>
      variantId ? (variantLabels[variantId] ?? variantId) : 'SPU 默认',
    [variantLabels],
  )

  /**
   * 先看代价再启用(§7.5)。后端算,前端只展示 —— 这一步不改任何状态。
   *
   * `useCallback` 不是为了省一次渲染,是为了让它**能进** `columns` 的依赖数组:
   * 原来它是个每次渲染都新建的函数声明,写进依赖等于让 memo 失效,不写进去
   * 则 `exhaustive-deps` 一直警告 —— 于是它长期停在"有一条没人读的警告"上。
   * 依赖升成 error 之前必须把这种two-way dead end 拆掉,而不是加一行 disable。
   */
  const openPreview = useCallback(
    async (planId: string) => {
      try {
        setEffect(await previewActivation(planId))
      } catch (error) {
        reportWriteError(error)
      }
    },
    [reportWriteError],
  )

  const columns = useMemo(
    () => [
      {
        title: '作用域',
        dataIndex: 'color_variant_id',
        render: (value: string | null) => scopeLabel(value),
      },
      { title: '出图服务商', dataIndex: 'provider' },
      {
        title: '场景 / 姿势',
        key: 'scene-pose',
        render: (_: unknown, row: GenerationPlan) =>
          `${row.scene || '未指定'} / ${row.pose || '未指定'}`,
      },
      {
        title: '角度',
        dataIndex: 'angles_json',
        render: (angles: GenerationPlan['angles_json']) =>
          angles.length === 0 ? (
            <Tag color="warning">未配置</Tag>
          ) : (
            <Space size={4} wrap>
              {angles.map((a) => (
                <Tag key={a.angle}>
                  {IMAGE_ANGLE_LABEL[a.angle]} ×{a.count}
                </Tag>
              ))}
            </Space>
          ),
      },
      {
        title: '预算上限',
        dataIndex: 'budget_cap',
        // 这里的措辞是「预算」不是「余额」—— 它是本系统的台账,
        // 不是厂商账户余额(services/spend.py 顶部)
        render: (value: string | null) => value ?? '不限',
      },
      {
        title: '状态',
        dataIndex: 'status',
        render: (value: GenerationPlan['status']) => (
          <Tag color={PLAN_STATUS_LABEL[value].color}>{PLAN_STATUS_LABEL[value].text}</Tag>
        ),
      },
      {
        title: '指纹',
        dataIndex: 'plan_fingerprint',
        render: (value: string) => <code>{value.slice(0, 8) || '—'}</code>,
      },
      {
        title: '操作',
        key: 'actions',
        render: (_: unknown, row: GenerationPlan) =>
          row.status === 'DRAFT' ? (
            <Button size="small" onClick={() => void openPreview(row.id)}>
              启用
            </Button>
          ) : null,
      },
    ],
    [scopeLabel, openPreview],
  )


  async function confirmActivate() {
    if (!effect) return
    try {
      const result = await activateGenerationPlan(effect.plan.id)
      setEffect(null)
      await reload()
      const staled = result.stale_color_variant_ids.length
      message.success(
        staled === 0
          ? '方案已启用'
          : `方案已启用;${staled} 个颜色的图片集已过期,需要重新生成`,
      )
    } catch (error) {
      reportWriteError(error)
    }
  }

  async function submitCreate() {
    const values = await form.validateFields()
    setCreating(true)
    try {
      await createGenerationPlan({
        spu_id: spuId,
        color_variant_id: values.color_variant_id ?? null,
        // **这一行以前不在。** 后端 `save_plan` 一直收它、一直跑
        // `assert_usable` 校验,而这张表单从不填 —— 于是向导里造出来的
        // 方案 `model_template_id` 恒为 null,`service._plan_facts` 的
        // `has_model` 恒为假,PRD §14.1 定义的方案步完成条件
        //(「有一份生效方案,**且方案里选了模特**」)在向导内**永远达不到**:
        // 第四步卡在 NEEDS_CONFIRM,七步走不完,完成度上限 96%。
        //
        // 判定层的用例看不见这件事:`test_a45_batch29_wizard._flow()` 的
        // 夹具写着 `has_model=True` —— 一个前端一天都造不出来的状态。
        model_template_id: values.model_template_id ?? null,
        provider: values.provider,
        scene: values.scene,
        pose: values.pose,
        angles: (values.angles ?? []).map((angle: ImageAngle) => ({
          angle,
          count: values[`count_${angle}`] ?? 1,
        })),
        budget_cap: values.budget_cap != null ? String(values.budget_cap) : null,
      })
      form.resetFields()
      await reload()
      message.success('方案已存为草稿,确认无误后点「启用」')
    } catch (error) {
      reportWriteError(error)
    } finally {
      setCreating(false)
    }
  }

  return (
    <>
      <Table
        rowKey="id"
        size="small"
        loading={loading}
        dataSource={plans}
        columns={columns}
        pagination={false}
        locale={{
          emptyText: (
            <Empty description="还没有生成方案。没有方案也能出图,但角度不会被验收" />
          ),
        }}
      />

      <Form form={form} layout="inline" style={{ marginTop: 16 }}>
        <Form.Item name="color_variant_id" label="作用域">
          <Select
            allowClear
            style={{ minWidth: 160 }}
            placeholder="SPU 默认"
            options={Object.entries(variantLabels).map(([value, label]) => ({ value, label }))}
          />
        </Form.Item>
        <Form.Item
          name="model_template_id"
          label="模特"
          // **不设 required。** 没有模特的方案是合法的 DRAFT(后端不拒),
          // 只是方案步不会判 DONE。设成必填的话,"先把角度存下来、模特
          // 明天再定"这条真实动线就没了 —— 而那不是 §6.4 的意思。
          //
          // 差的那一步由判定层说:方案缺模特 -> NEEDS_CONFIRM +
          // 「方案里还没有选模特」,向导上那条提示就是它。
          extra={
            templates.isError
              ? // **拉失败与"一个都没有"必须分开说。** 空下拉框在界面上
                // 是一句业务结论(「没有可用模特」),运营照着它做的下一步
                // 是去新建一个模特;而照着"没拉到"做的下一步是重试。
                // 两者相反 —— a28 的 FE-GLOBAL-03 说的就是这一档。
                `模特列表没有拉到(${readError(templates.error)}),下面是空的不代表没有模特`
              : productAudience
                ? `候选模特已按商品受众(${AUDIENCE_LABEL[productAudience]})筛过`
                : '商品受众未确认,候选模特没有按受众筛过 —— 选出来的可能不匹配'
          }
        >
          <Select
            allowClear
            style={{ minWidth: 220 }}
            placeholder="未选(方案步会停在待确认)"
            loading={templates.isLoading}
            status={templates.isError ? 'error' : undefined}
            notFoundContent={
              templates.isError
                ? '没拉到,不是没有'
                : productAudience
                  ? `没有受众为「${AUDIENCE_LABEL[productAudience]}」的启用模特`
                  : undefined
            }
            options={(templates.data ?? []).map((t) => ({
              value: t.id,
              // §10.3:真正做选择的这一处必须显示受众。看不到它,
              // §10.5 那条硬约束就只剩后端一道
              label: `${t.name} · ${AUDIENCE_LABEL[t.audience] ?? t.audience} · ${t.pose}`,
            }))}
          />
        </Form.Item>
        <Form.Item
          name="provider"
          label="出图服务商"
          rules={[{ required: true }]}
          extra={
            providers.isError
              ? // 拉失败与"一个都不可用"必须分开说 —— 同上面模特那一处
                `服务商列表没拉到(${readError(providers.error)}),下面是空的不代表没有可用的`
              : undefined
          }
        >
          <Select
            style={{ minWidth: 160 }}
            loading={providers.isLoading}
            status={providers.isError ? 'error' : undefined}
            notFoundContent={providers.isError ? '没拉到,不是没有' : undefined}
            options={providerOptions}
          />
        </Form.Item>
        <Form.Item
          name="scene"
          label="场景"
          initialValue="STUDIO_CLEAN"
          extra="选择预设会展开成场景专用提示词；也可以输入自定义场景"
        >
          <AutoComplete
            style={{ minWidth: 190 }}
            options={SCENE_OPTIONS}
            placeholder="选择预设或输入自定义场景"
            filterOption={(input, option) =>
              String(option?.label ?? option?.value ?? '')
                .toLowerCase()
                .includes(input.toLowerCase())
            }
          />
        </Form.Item>
        <Form.Item
          name="pose"
          label="姿势"
          initialValue="NATURAL_STANDING"
          extra="用于补充模特动作约束，不替代模特模板本身"
        >
          <AutoComplete
            style={{ minWidth: 180 }}
            options={POSE_OPTIONS}
            placeholder="选择预设或输入自定义姿势"
            filterOption={(input, option) =>
              String(option?.label ?? option?.value ?? '')
                .toLowerCase()
                .includes(input.toLowerCase())
            }
          />
        </Form.Item>
        <Form.Item name="angles" label="角度" rules={[{ required: true }]}>
          <Select mode="multiple" style={{ minWidth: 220 }} options={ANGLE_OPTIONS} />
        </Form.Item>
        {/*
          每个角度出几张。**这些输入框以前不存在**(2026-08-11 评审):
          `submitCreate` 里写着 `values[`count_${angle}`] ?? 1`,而表单里
          没有任何 `name={"count_" + angle}` 的 Form.Item —— 于是每个角度
          恒为 1 张,「FRONT×2」这件事在界面上**表达不出来**,
          而方案表格里那一列 `×{a.count}` 永远显示 ×1。

          只给选中的角度渲染:没选的角度不该占一格,也不该带一个会被
          `submitCreate` 读到的值。
        */}
        {(selectedAngles ?? []).map((angle) => (
          <Form.Item
            key={angle}
            name={`count_${angle}`}
            label={`${IMAGE_ANGLE_LABEL[angle]} 张数`}
            initialValue={1}
          >
            {/* 上限跟着后端 `MAX_PLAN_CANDIDATES`(= `MAX_CANDIDATE_COUNT`)。
                超了后端会以 `PLAN_TOO_MANY_CANDIDATES` 拒,这里只是先拦一下 ——
                真判据在后端,不在这个 max */}
            <InputNumber min={1} max={8} style={{ width: 88 }} />
          </Form.Item>
        ))}
        <Form.Item name="budget_cap" label="预算上限">
          <InputNumber min={0} placeholder="不限" />
        </Form.Item>
        <Form.Item>
          <Button type="primary" loading={creating} onClick={() => void submitCreate()}>
            存为草稿
          </Button>
        </Form.Item>
      </Form>

      <Modal
        open={effect !== null}
        title="启用这份方案"
        onCancel={() => setEffect(null)}
        onOk={() => void confirmActivate()}
        okText="确认启用"
      >
        {effect && effect.stale_color_variant_ids.length > 0 ? (
          <>
            <p>启用之后,下列颜色的图片集会过期,需要重新生成:</p>
            <Space wrap>
              {effect.stale_color_variant_ids.map((id) => (
                <Tag key={id} color="warning">
                  {scopeLabel(id)}
                </Tag>
              ))}
            </Space>
            <p style={{ marginTop: 12 }}>重新生成会产生真实的付费调用。</p>
          </>
        ) : (
          <p>启用之后没有颜色的图片集会因此过期。</p>
        )}
      </Modal>
    </>
  )
}
