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
import { App, Button, Empty, Form, InputNumber, Modal, Select, Space, Table, Tag } from 'antd'

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
import { readError } from '../api/client'
import { useWriteError } from '../hooks/useWriteError'

interface Props {
  spuId: string
  /** 颜色 id → 显示名。**显示一律用名字,不用 id** */
  variantLabels?: Record<string, string>
}

const ANGLE_OPTIONS = (Object.keys(IMAGE_ANGLE_LABEL) as ImageAngle[]).map((value) => ({
  value,
  label: IMAGE_ANGLE_LABEL[value],
}))

export default function GenerationPlanPanel({ spuId, variantLabels = {} }: Props) {
  const { message } = App.useApp()
  const [plans, setPlans] = useState<GenerationPlan[]>([])
  const [loading, setLoading] = useState(false)
  const [creating, setCreating] = useState(false)
  const [effect, setEffect] = useState<PlanActivationEffect | null>(null)
  const [form] = Form.useForm()

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

  const columns = useMemo(
    () => [
      {
        title: '作用域',
        dataIndex: 'color_variant_id',
        render: (value: string | null) => scopeLabel(value),
      },
      { title: 'Provider', dataIndex: 'provider' },
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
    [scopeLabel],
  )

  async function openPreview(planId: string) {
    try {
      // 先看代价再启用(§7.5)。后端算,前端只展示 —— 这一步不改任何状态
      setEffect(await previewActivation(planId))
    } catch (error) {
      reportWriteError(error)
    }
  }

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
        provider: values.provider,
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
        <Form.Item name="provider" label="Provider" rules={[{ required: true }]}>
          <Select
            style={{ minWidth: 120 }}
            options={[
              { value: 'mock', label: 'mock' },
              { value: 'fashn', label: 'fashn' },
              { value: 'comfyui', label: 'comfyui' },
            ]}
          />
        </Form.Item>
        <Form.Item name="angles" label="角度" rules={[{ required: true }]}>
          <Select mode="multiple" style={{ minWidth: 220 }} options={ANGLE_OPTIONS} />
        </Form.Item>
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
