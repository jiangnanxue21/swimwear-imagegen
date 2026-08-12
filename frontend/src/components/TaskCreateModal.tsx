/**
 * 创建生成任务(a47 §7:入口去工程化)。
 *
 * ## 这个弹窗分两层,判据是「运营改得动它吗」
 *
 *     默认(所有人)   商品/颜色、模特、出图方案、角度、张数、预计付费调用次数
 *     高级(仅管理员) Provider、每轮候选数、最多轮次、基础 seed、提示词、
 *                     provider_params,以及绕过方案的 override_plan
 *
 * 改之前这一层不存在:运营建一次任务要面对 Provider 名、`base_seed`、
 * 提示词和一个叫 `provider_params` 的 JSON。这些不是"高级功能",
 * 它们是**运营没有依据去填的东西** —— 而填错了要花真钱才发现。
 *
 * ## 有方案时 Provider 与模特是**只读的解析结果**
 *
 * 这是让 a47 §5 那次后端改造在界面上说得通的那一半。后端已经改成
 * 「方案解析出来之后由后端决定最终参数」,那么界面上再摆一个可选的
 * Provider 下拉就是在撒谎:选了也不算数(冲突时按方案执行并留痕)。
 *
 * 解析结果由 `GET /generation-plans/effective` 给出,**前端不自己解析**
 * (硬规则 4)。两处各解析一遍的表现是界面显示 A 方案、任务按 B 方案跑。
 *
 * ## `override_plan` 为什么摆在界面上,而不是只留给 curl
 *
 * 管理员排障需要一条"就按我说的来"的路,而后端对它是 403 fail-closed。
 * 藏起来的代价是:排障的人会去改方案本身 —— 那会改掉幂等键、让这个 SPU
 * 下所有颜色的图片集判过期,为了跑一次调试付出一次全量重出图的代价。
 */
import { useEffect, useState } from 'react'
import {
  Alert, Collapse, Descriptions, Form, InputNumber, Modal, Select, Input, Space,
  Spin, Switch, Tag, Typography,
} from 'antd'
import { useQuery } from '@tanstack/react-query'
import {
  modelTemplatesApi, providersApi, type TaskCreatePayload,
} from '../api/generation'
import { productsApi } from '../api/products'
import { effectivePlanForProduct, IMAGE_ANGLE_LABEL } from '../api/generationPlans'
import {
  AUDIENCE_LABEL, MOCK_EVALUATOR_OUTCOMES, MOCK_OUTCOMES, MODE_LABEL,
  isProviderSelectable, providerOptionLabel, type Audience,
} from '../api/types'
import { useIdentity } from '../hooks/useIdentity'
import { brandVars, fontScale } from '../theme'

interface Props {
  open: boolean
  productId?: string
  initialValues?: Partial<TaskCreatePayload>
  title?: string
  confirmLoading?: boolean
  onCancel: () => void
  /**
   * 提交这次出图。**入参是 `TaskCreatePayload`,不是 `Record<string, unknown>`。**
   *
   * 2026-08-11:原来是后者,于是三个调用点各自把它强转回来 —— 两个写
   * `as never`(TS 对它一声不吭),第三个写 `as TaskCreatePayload`,
   * 而那一个**当场 TS2352**:`Record<string, unknown>` 缺 `product_id` 与
   * `mode`,两个类型不够重叠。也就是说 `npm run typecheck` 在这一行是红的,
   * CI 的 frontend job 到不了 lint 那一步。
   *
   * 强转本身就是那道缝:这个弹窗**确实**会补上 `product_id`(路由里那个)
   * 与 `mode`(默认 `virtual_try_on`),所以它产出的就是一个完整的
   * `TaskCreatePayload` —— 那件事应该由类型说出来,而不是靠三处
   * 各写一遍的 `as`。改成正着声明之后,三个 `as` 全部删掉。
   */
  onSubmit: (values: TaskCreatePayload) => void
}

export default function TaskCreateModal({
  open, productId, initialValues, title = '创建生成任务', confirmLoading, onCancel, onSubmit,
}: Props) {
  const [form] = Form.useForm()
  const { isAdmin } = useIdentity()
  /*
   * 高级区展开与否是**每次打开都重置**的本地状态,不进 localStorage。
   *
   * 记住它会让"上次排障时展开过"变成"这次也展开着",而下一次多半是
   * 日常出图 —— 那正是这一层要收起来的场景。
   */
  const [advancedOpen, setAdvancedOpen] = useState(false)
  useEffect(() => {
    if (open) setAdvancedOpen(false)
  }, [open])

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

  /**
   * 这件商品出图会按哪份方案跑。**后端解析,前端只展示。**
   *
   * 没选商品时不查:`/generation-plans/effective` 要 `product_id`,
   * 而"还没选商品"不是一个可以查的状态。
   */
  const effectivePlan = useQuery({
    queryKey: ['effective-plan', effectiveProductId],
    queryFn: () => effectivePlanForProduct(effectiveProductId!),
    enabled: open && Boolean(effectiveProductId),
  })
  const plan = effectivePlan.data?.plan ?? null
  const overridePlan = Form.useWatch('override_plan', form) as boolean | undefined
  /** 方案说了算的那几个字段现在是不是只读。绕过时它们又归调用方 */
  const planGoverns = Boolean(plan) && !overridePlan

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
        mode: 'virtual_try_on',
        provider: 'mock',
        candidate_count: 4,
        max_rounds: 3,
        mock_outcome: 'success',
        mock_evaluator_outcome: 'auto',
        override_plan: false,
        ...initialValues,
        product_id: productId ?? initialValues?.product_id,
      })
    }
  }, [open, productId, initialValues, form])

  const usable = (providers.data ?? []).filter(isProviderSelectable)
  /*
   * 三个下拉的数据源任一失败,都不许静默变成空选项(FE-TASK-CREATE-04 /
   * 阶段 A 验收第 6 条)。空的 Provider 下拉读起来是"没有可用 Provider",
   * 空的模板下拉读起来是"还没建模板",而**建任务是付费动作** ——
   * 让人对着一份不完整的选项提交,比让他看见一句"没拉到"贵得多。
   *
   * a47 加进第五个:方案解析失败。它比另外四条更要紧 —— 另外四条是
   * "选项不全",这一条是**运营不知道这次会按什么参数出图**。
   */
  const optionsFailed =
    providers.isError || templates.isError || products.isError || product.isError
    || effectivePlan.isError
  const selectedProvider = Form.useWatch('provider', form)
  const maxRounds = (Form.useWatch('max_rounds', form) as number | undefined) ?? 3
  const candidateCount = (Form.useWatch('candidate_count', form) as number | undefined) ?? 4
  /**
   * 一轮几张:有方案时是后端算好的角度张数之和,没方案时是表单里那个数。
   * **两个数字都不是这里推出来的** —— 一个来自接口,一个来自用户自己填的框。
   */
  const perRound = planGoverns ? effectivePlan.data?.candidates_per_round ?? null : candidateCount

  return (
    <Modal
      open={open}
      title={title}
      okText="提交任务"
      cancelText="取消"
      confirmLoading={confirmLoading}
      onCancel={onCancel}
      width={640}
      destroyOnClose
      onOk={() =>
        form.validateFields().then((values) => {
          const {
            mock_outcome, mock_evaluator_outcome, override_plan, ...rest
          } = values
          // Mock 专用旋钮全部塞进 provider_params,真实 Provider 会忽略它们。
          const providerParams: Record<string, unknown> = {}
          if (mock_outcome) providerParams.mock_outcome = mock_outcome
          if (mock_evaluator_outcome && mock_evaluator_outcome !== 'auto') {
            providerParams.mock_evaluator = { outcome: mock_evaluator_outcome }
          }
          /*
           * 商品详情页会固定 `productId`,因此不渲染商品下拉。未渲染的
           * Form.Item 不属于 `validateFields()` 的返回值,即使上面调用过
           * `setFieldsValue({ product_id })` 也不能依赖它被带回来。
           * 在提交边界显式合并路由里的商品主键,保证两种入口请求形状一致。
           *
           * a47:`override_plan` 同理 —— 非管理员看不到那个开关,于是它
           * 不在返回值里。**这里显式写成 false 而不是让它缺席**:后端
           * 对缺席与 false 是同一个默认,但请求体里有它才说得清
           * "这一次没有绕过方案"是一个决定,不是一个遗漏。
           */
          // 显式收成 `TaskCreatePayload`:两个必填项在这里各有一个确定来源
          // (`product_id` 来自路由或下拉,`mode` 有 setFieldsValue 的默认值),
          // 所以类型上它们不该是可选的 —— 见 `Props.onSubmit` 那一段
          const payload: TaskCreatePayload = {
            ...rest,
            product_id: String(productId ?? rest.product_id),
            mode: String(rest.mode),
            override_plan: Boolean(override_plan),
            provider_params: providerParams,
          }
          onSubmit(payload)
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
              {effectivePlan.isError && (
                <div>
                  出图方案没解析出来 —— <b>不知道这次会按什么参数出图</b>。
                  有方案时 Provider、模特与角度由方案决定,现在这几项显示的不是最终值。
                </div>
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

        <Form.Item name="mode" label="生成模式">
          <Select
            options={Object.entries(MODE_LABEL).map(([value, label]) => ({ value, label }))}
          />
        </Form.Item>

        {/*
          出图方案(a47 §7)。**这一块是只读的解析结果,不是一个选择器。**

          放在模特之前:运营的问题顺序是"这次按什么出" -> "谁来穿",
          而方案恰恰同时回答了 Provider 与模特两件事。
        */}
        {effectiveProductId && (
          <Form.Item label="出图方案">
            {effectivePlan.isLoading ? (
              <Spin size="small" />
            ) : plan ? (
              <Descriptions
                size="small"
                bordered
                column={1}
                items={[
                  {
                    key: 'scope',
                    label: '作用域',
                    children:
                      effectivePlan.data?.scope === 'COLOR'
                        ? '这个颜色的专属方案'
                        : '款默认方案(这个颜色没有专属方案,回落到它)',
                  },
                  {
                    key: 'provider',
                    label: 'Provider',
                    children: <span className="mono">{plan.provider || '未指定'}</span>,
                  },
                  {
                    key: 'angles',
                    label: '角度与张数',
                    children: (
                      <Space size={4} wrap>
                        {plan.angles_json.map((a) => (
                          <Tag key={a.angle} style={{ marginInlineEnd: 0 }}>
                            {IMAGE_ANGLE_LABEL[a.angle] ?? a.angle} ×{a.count}
                          </Tag>
                        ))}
                        {plan.angles_json.length === 0 && '方案没有配置角度'}
                      </Space>
                    ),
                  },
                  {
                    key: 'sp',
                    label: '场景 / 姿势',
                    children: `${plan.scene || '未指定'} / ${plan.pose || '未指定'}`,
                  },
                ]}
              />
            ) : (
              <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
                这件商品没有生效的出图方案,本次按下面的参数出图。
                {/* 不拦。存量商品与单色 SPU 都没有方案,拦下来等于让它们集体无法出图
                    —— 与后端 `create_task` 那条"解析不到方案不拦"同一口径 */}
              </Typography.Text>
            )}
          </Form.Item>
        )}

        {/* 方案本身有问题时说出来。藏起来的话运营会以为"没有方案",然后去建第二份 */}
        {plan && (effectivePlan.data?.problems.length ?? 0) > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message="这份方案本身有问题"
            description={effectivePlan.data?.problems.map((p) => <div key={p}>{p}</div>)}
          />
        )}

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
          // 2026-08-11:留空**不再是一条能走的路**。后端原来会退回商品自带的
          // 「模特参考图」并跳过 §10.5 受众与 §11 授权/年龄检查,那条缝已经
          // 关掉了(`generation_service._assert_assets_are_usable`,按 PRD §6.4
          // 「自由上传模特图不得绕过 ModelTemplate 校验」)。
          // 这里的文案必须跟着改 —— 让人照着一句过期的提示留空,
          // 拿到的会是一个他无法从提示里预料到的报错。
          //
          // **编号只能待在注释里**:运营手里没有那份文档,ESLint 的
          // no-restricted-syntax 拦的就是把编号写进界面文案。
          extra={
            planGoverns && plan?.model_template_id
              ? '方案已经指定了模特 —— 这一项由方案决定,选了不算数'
              : productAudience
                ? `候选集已按商品受众(${AUDIENCE_LABEL[productAudience]})收窄:受众不匹配的模特不会出现在这里`
                : '虚拟试穿必须选一个模特模板。商品自带的「模特参考图」不能直接用 —— 那张图没有受众、年龄与授权记录,合规检查无从执行'
          }
        >
          <Select
            allowClear
            // 方案指定了模特就锁住:后端已经改成按方案执行,这里再让人选
            // 一个选了不算数的值,是在界面上撒谎
            disabled={planGoverns && Boolean(plan?.model_template_id)}
            placeholder={
              planGoverns && plan?.model_template_id
                ? '由方案指定'
                // 原来是"不指定(使用商品自带模特参考图)"—— 那条路已经关了,
                // 留着这句话等于在下拉框里推荐一个必定失败的选择
                : '选一个模特模板'
            }
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

        {/*
          预计付费调用次数(§7 运营视图的最后一项)。

          **两个因数都不是这里推出来的**:一轮几张来自后端(有方案时)
          或用户自己填的框(没方案时),轮次来自用户自己填的框。
          "最多"这个词不能省 —— 达标就停,多数任务跑不满轮次,
          写成确定值会让运营按一个偏高的数字去卡预算。
        */}
        <Form.Item label="预计付费调用次数">
          <Typography.Text style={{ fontSize: fontScale.body }}>
            {perRound === null ? (
              <span style={{ color: brandVars.slate }}>方案没配角度,算不出张数</span>
            ) : (
              <>
                每轮 <b>{perRound}</b> 张 × 最多 <b>{maxRounds}</b> 轮 ={' '}
                <b style={{ color: brandVars.danger }}>最多 {perRound * maxRounds} 次</b>
                <span style={{ color: brandVars.slate }}>
                  {' '}· 达标即停,多数任务跑不满轮次
                </span>
              </>
            )}
          </Typography.Text>
        </Form.Item>

        {/*
          高级选项(a47 §7)。**默认收起,而且只有管理员看得见。**

          `isAdmin` 只是可发现性:运营手改请求体照样能把 seed 发上来,
          后端也照收 —— 唯一在后端 fail-closed 的是 `override_plan`(403)。
          这一层挡的是"运营对着一个他没有依据去填的框做决定",不是攻击者。
        */}
        {isAdmin && (
          <Collapse
            size="small"
            activeKey={advancedOpen ? ['advanced'] : []}
            onChange={(keys) => setAdvancedOpen((keys as string[]).includes('advanced'))}
            items={[
              {
                key: 'advanced',
                label: '高级选项(工程参数 · 仅管理员)',
                children: (
                  <>
                    <Form.Item
                      name="override_plan"
                      label="绕过出图方案"
                      valuePropName="checked"
                      extra={
                        plan
                          ? '打开后完全按本弹窗的参数出图,并且这次任务不会记方案指纹 —— 排障用'
                          : '这件商品本来就没有方案,这个开关不改变任何事'
                      }
                    >
                      <Switch size="small" disabled={!plan} />
                    </Form.Item>

                    <Form.Item
                      name="provider"
                      label="Provider"
                      extra={
                        planGoverns
                          ? '方案指定了 Provider —— 这里选什么都会按方案执行,并在审计里留痕'
                          : usable.length < (providers.data?.length ?? 0)
                            ? '未配置或尚未实现的 Provider 不可选'
                            : undefined
                      }
                    >
                      <Select
                        disabled={planGoverns && Boolean(plan?.provider)}
                        loading={providers.isLoading}
                        options={(providers.data ?? []).map((p) => ({
                          value: p.name,
                          label: providerOptionLabel(p),
                          disabled: !isProviderSelectable(p),
                        }))}
                      />
                    </Form.Item>

                    <Space.Compact block>
                      <Form.Item
                        name="candidate_count"
                        label="每轮候选数"
                        style={{ width: '33%', marginRight: 8 }}
                        extra={planGoverns ? '由方案的角度张数决定' : undefined}
                      >
                        <InputNumber min={1} max={8} disabled={planGoverns} style={{ width: '100%' }} />
                      </Form.Item>
                      <Form.Item name="max_rounds" label="最多轮次" style={{ width: '33%', marginRight: 8 }}>
                        <InputNumber min={1} max={10} style={{ width: '100%' }} />
                      </Form.Item>
                      <Form.Item name="base_seed" label="基础 seed" style={{ width: '34%' }}>
                        <InputNumber min={0} placeholder="留空随机" style={{ width: '100%' }} />
                      </Form.Item>
                    </Space.Compact>

                    <Form.Item
                      name="prompt"
                      label="提示词"
                      extra={
                        planGoverns
                          ? '方案的场景、姿势与角度会接在这句话后面一起发给 Provider'
                          : undefined
                      }
                    >
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
                  </>
                ),
              },
            ]}
          />
        )}

        <Typography.Text
          type="secondary"
          style={{ fontSize: fontScale.body, display: 'block', marginTop: 12 }}
        >
          相同商品、素材与参数会命中幂等,不会重复建任务。
        </Typography.Text>
      </Form>
    </Modal>
  )
}
