import { useEffect } from 'react'
import { Alert, Form, InputNumber, Modal, Select, Input, Space, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { modelTemplatesApi, providersApi } from '../api/generation'
import { productsApi } from '../api/products'
import {
  AUDIENCE_LABEL, MOCK_EVALUATOR_OUTCOMES, MOCK_OUTCOMES, MODE_LABEL,
  isProviderSelectable, providerOptionLabel, type Audience,
} from '../api/types'
import { fontScale } from '../theme'

interface Props {
  open: boolean
  productId?: string
  confirmLoading?: boolean
  onCancel: () => void
  onSubmit: (values: Record<string, unknown>) => void
}

export default function TaskCreateModal({
  open, productId, confirmLoading, onCancel, onSubmit,
}: Props) {
  const [form] = Form.useForm()

  const providers = useQuery({ queryKey: ['providers'], queryFn: providersApi.list, enabled: open })
  const products = useQuery({
    queryKey: ['products', 'ready'],
    queryFn: () => productsApi.list({ page_size: 200 }),
    enabled: open && !productId,
  })

  /*
   * 商品受众要在**选模特之前**知道(§10.5)。两条取数路径:
   *
   *   带 productId 进来   直接查这一件(详情页/工作台的入口)
   *   走商品下拉         从已经拉回来的列表里取当前选中那一件
   *
   * 上一版两条都没有:`Product` 类型里没有 audience,`TaskCreateModal`
   * 也从不查商品 —— 于是 `forProductAudience` 这个参数是**死代码**,
   * §10.5 的硬约束筛选一次都没被调用过。
   */
  const pickedProductId = Form.useWatch('product_id', form) as string | undefined
  const effectiveProductId = productId ?? pickedProductId

  const product = useQuery({
    queryKey: ['product', effectiveProductId],
    queryFn: () => productsApi.get(effectiveProductId!),
    enabled: open && Boolean(productId),
  })

  const productAudience: Audience | null =
    (productId
      ? product.data?.audience
      : products.data?.items.find((p) => p.id === pickedProductId)?.audience) ?? null

  /*
   * **query key 带上受众。** 少了它,换一个商品后模特列表不会重查 ——
   * 运营会对着一份上一件商品的候选集做选择,而那正是 §10.5 要防的事。
   */
  const templates = useQuery({
    queryKey: ['model-templates', 'enabled', productAudience ?? 'UNCONFIRMED'],
    queryFn: () =>
      modelTemplatesApi.list(true, {
        forProductAudience: productAudience ?? undefined,
      }),
    enabled: open,
  })

  useEffect(() => {
    if (open) {
      form.resetFields()
      form.setFieldsValue({
        product_id: productId,
        mode: 'virtual_try_on',
        provider: 'mock',
        candidate_count: 4,
        max_rounds: 3,
        mock_outcome: 'success',
        mock_evaluator_outcome: 'auto',
      })
    }
  }, [open, productId, form])

  const usable = (providers.data ?? []).filter(isProviderSelectable)
  /*
   * 三个下拉的数据源任一失败,都不许静默变成空选项(FE-TASK-CREATE-04 /
   * 阶段 A 验收第 6 条)。空的 Provider 下拉读起来是"没有可用 Provider",
   * 空的模板下拉读起来是"还没建模板",而**建任务是付费动作** ——
   * 让人对着一份不完整的选项提交,比让他看见一句"没拉到"贵得多。
   */
  const optionsFailed =
    providers.isError || templates.isError || products.isError || product.isError
  const selectedProvider = Form.useWatch('provider', form)

  return (
    <Modal
      open={open}
      title="创建生成任务"
      okText="提交任务"
      cancelText="取消"
      confirmLoading={confirmLoading}
      onCancel={onCancel}
      width={600}
      destroyOnClose
      onOk={() =>
        form.validateFields().then((values) => {
          const { mock_outcome, mock_evaluator_outcome, ...rest } = values
          // Mock 专用旋钮全部塞进 provider_params,真实 Provider 会忽略它们。
          const providerParams: Record<string, unknown> = {}
          if (mock_outcome) providerParams.mock_outcome = mock_outcome
          if (mock_evaluator_outcome && mock_evaluator_outcome !== 'auto') {
            providerParams.mock_evaluator = { outcome: mock_evaluator_outcome }
          }
          onSubmit({ ...rest, provider_params: providerParams })
        })
      }
    >
      <Alert
        type="info"
        showIcon
        style={{ marginBottom: 16 }}
        message="任务提交后立即返回,生成在后台进行"
        description="后台会自动完成生成、评分、A/B/C/D 分档;低分候选自动淘汰重生,轮次耗尽仍不达标才转人工审核。"
      />

      {optionsFailed && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 16 }}
          message="下拉选项没有取全,先不要提交"
          description={
            <>
              {providers.isError && <div>Provider 列表没拉到 —— 选项里缺哪几家现在不知道。</div>}
              {templates.isError && <div>模特模板没拉到 —— 不代表没有模板。</div>}
              {products.isError && <div>商品列表没拉到。</div>}
              {/*
                * 商品没拉到 = **不知道这件商品的受众**,于是模特候选集
                * 没有按 §10.5 收窄。这一条比其它三条更要紧:另外三条是
                * "选项不全",这一条是"选项可能是错的那一组" ——
                * 一个男泳裤生成到女模特身上,出来的图在纯技术指标上往往
                * 是好的,评分器不检查穿的人是谁,人工也容易放过。
                */}
              {product.isError && (
                <div>商品资料没拉到 —— 受众因此未知,下面的模特候选集没有按受众筛选。</div>
              )}
              <div>建任务会调用付费接口,请刷新后再提交。</div>
            </>
          }
        />
      )}

      <Form form={form} layout="vertical" size="small">
        {!productId && (
          <Form.Item name="product_id" label="商品" rules={[{ required: true, message: '选择商品' }]}>
            <Select
              showSearch
              placeholder="按 SKU 或名称搜索"
              loading={products.isLoading}
              optionFilterProp="label"
              options={(products.data?.items ?? []).map((p) => ({
                value: p.id,
                label: `${p.sku} · ${p.name}`,
              }))}
            />
          </Form.Item>
        )}

        <Space.Compact block>
          <Form.Item name="mode" label="生成模式" style={{ width: '50%', marginRight: 8 }}>
            <Select
              options={Object.entries(MODE_LABEL).map(([value, label]) => ({ value, label }))}
            />
          </Form.Item>
          <Form.Item
            name="provider"
            label="Provider"
            style={{ width: '50%' }}
            extra={usable.length < (providers.data?.length ?? 0)
              ? '未配置或尚未实现的 Provider 不可选' : undefined}
          >
            <Select
              loading={providers.isLoading}
              options={(providers.data ?? []).map((p) => ({
                value: p.name,
                label: providerOptionLabel(p),
                disabled: !isProviderSelectable(p),
              }))}
            />
          </Form.Item>
        </Space.Compact>

        {/*
          * 受众待确认时先说出来。候选集这时**没有被过滤**(后端对 None
          * 不过滤,理由是不替商品猜一个受众),所以下拉里两种受众都在 ——
          * 运营需要知道自己正在一份未收窄的列表上做选择。
          */}
        {!productAudience && effectiveProductId && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="这件商品的受众还没确认"
            description="模特候选集因此没有按受众收窄。先去商品资料确认受众,选出来的模特才是对的。"
          />
        )}

        <Form.Item
          name="model_template_id"
          label="模特模板"
          // 留空这条路径绕过 §11 的授权/年龄检查(`media_assets` 上没有那几列,
          // 见 docs/STATUS.md 的已知缺口)。**编号只能待在注释里** ——
          // 运营手里没有那份文档,ESLint 的 no-restricted-syntax 拦的就是它。
          // 下面那句话必须把"跳过了什么"说成人话,而不是甩一个编号。
          extra={
            productAudience
              ? `候选集已按商品受众(${AUDIENCE_LABEL[productAudience]})收窄:受众不匹配的模特不会出现在这里`
              : '留空则使用商品自带的模特参考图 —— 那张图没有授权与年龄记录,不走授权检查'
          }
        >
          <Select
            allowClear
            placeholder="不指定(使用商品自带模特参考图)"
            loading={templates.isLoading}
            notFoundContent={
              productAudience
                ? `没有受众为「${AUDIENCE_LABEL[productAudience]}」的启用模特`
                : undefined
            }
            options={(templates.data ?? []).map((t) => ({
              value: t.id,
              // §10.3:模特卡片必须展示受众 —— 而这里恰恰是**真正做选择**
              // 的那一处。看不到受众,§10.5 的硬约束就只剩后端一道
              label: `${t.name} · ${AUDIENCE_LABEL[t.audience] ?? t.audience} · ${t.pose}`,
            }))}
          />
        </Form.Item>

        <Space.Compact block>
          <Form.Item name="candidate_count" label="每轮候选数" style={{ width: '33%', marginRight: 8 }}>
            <InputNumber min={1} max={8} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="max_rounds" label="最多轮次" style={{ width: '33%', marginRight: 8 }}>
            <InputNumber min={1} max={10} style={{ width: '100%' }} />
          </Form.Item>
          <Form.Item name="base_seed" label="基础 seed" style={{ width: '34%' }}>
            <InputNumber min={0} placeholder="留空随机" style={{ width: '100%' }} />
          </Form.Item>
        </Space.Compact>

        <Form.Item name="prompt" label="提示词">
          <Input.TextArea rows={2} placeholder="影棚灯光,白色背景,正面全身" />
        </Form.Item>

        {selectedProvider === 'mock' && (
          <Space.Compact block>
            <Form.Item
              name="mock_outcome"
              label="模拟生成结果"
              style={{ width: '50%', marginRight: 8 }}
              extra="演练 Provider 失败分支"
            >
              <Select options={MOCK_OUTCOMES} />
            </Form.Item>
            <Form.Item
              name="mock_evaluator_outcome"
              label="模拟评分结果"
              style={{ width: '50%' }}
              extra="演练分档、硬错误与多轮重生"
            >
              <Select options={MOCK_EVALUATOR_OUTCOMES} />
            </Form.Item>
          </Space.Compact>
        )}

        <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
          相同商品、素材与参数会命中幂等,不会重复建任务。
        </Typography.Text>
      </Form>
    </Modal>
  )
}
