/**
 * 三步建档(PRD v3.1 §6.1 步骤 1 / §13 阶段 1)。
 *
 * ## 这一页在此之前**不存在**
 *
 * `POST /api/spus`、`GET /api/spus/size-templates` 从阶段 1 起就齐了,
 * `sample-data/spus.json` 里三颜色九 SKU 的样例也在,而前端一个调用点都没有
 * —— 全仓只有 `batch.ts` 打过 `/workbench/spus`(只读聚合),
 * `WorkbenchSpuPage` 是查看页不是建档页。后端做完、门禁全绿、动线断在最后
 * 一跳,和 `facts_stale` / `variant_gate_roles` 是同一个形状。
 *
 * ## 第三步预览的是**数量**,不是 SKU 编码
 *
 * 硬规则 4:前端不推测后端的东西。SKU 编码怎么拼(分隔符、颜色码位置、
 * 尺码归一)住在 `listings/sku_matrix`,前端照着拼一份的话,改一次拼法
 * 界面和库里就是两套编码 —— 而运营会照着界面上那个编码去平台后台搜,
 * 搜不到,然后怀疑是没建成功。
 *
 * 数量不一样:`颜色数 × 尺码数` 是运营自己就能验算的算术,而且它正是
 * 「三颜色九 SKU」那句验收要看的数。真正的 SKU 列表在建档**之后**
 * 由后端回出来,那份是真的。
 *
 * ## 这里没有视觉属性字段
 *
 * 阶段 1 的验收之一是「不填视觉属性即可建档」。后端的做法不是把那 8 个
 * 字段设成可选,而是让它们在这个接口上根本不存在 —— 可选字段会被前端填成
 * 空串,而空串和"还没识别"在下游是两件事:前者会被当成一个确定的事实。
 * 别在这一页加它们。
 *
 * ## 校验交给后端,不在这里抄一份
 *
 * 编码字符集、重复颜色、行数上限全部住在 `listings/sku_matrix`。抄一份到
 * 这里,它就有了两个版本,然后其中一个先过期 —— `schemas/spu.py` 顶部
 * 记着这类事故的原样。这一页只挡"必填项还空着"这种连请求都发不出去的情况。
 */
import { useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Descriptions, Empty, Form, Input, Select, Space,
  Statistic, Steps, Table, Tag,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate } from 'react-router-dom'
import {
  SPU_AUDIENCE_LABEL,
  spusApi,
  type ColorVariantDraft,
  type SpuAudience,
  type SpuDetail,
  type SpuSku,
} from '../api/spus'
import { noRefresh, useWriteError } from '../hooks/useWriteError'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import UnsavedGuard from '../components/UnsavedGuard'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { brandVars } from '../theme'

interface Basics {
  spu_code: string
  internal_name: string
  audience: SpuAudience | undefined
  base_category: string
  supplier_ref: string
}

const EMPTY_BASICS: Basics = {
  spu_code: '',
  internal_name: '',
  audience: undefined,
  base_category: 'swimwear',
  supplier_ref: '',
}

/** 一行空颜色。`variant_code` 是必填的那一个,名字可以后补 */
const emptyColour = (): ColorVariantDraft => ({
  variant_code: '',
  working_name: '',
  supplier_color_code: '',
})

export default function SpuCreatePage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  useDocumentTitle('新建商品款式')

  const [current, setCurrent] = useState(0)
  const [basics, setBasics] = useState<Basics>(EMPTY_BASICS)
  const [colours, setColours] = useState<ColorVariantDraft[]>([emptyColour()])
  const [sizeTemplate, setSizeTemplate] = useState<string | undefined>()
  const [created, setCreated] = useState<SpuDetail | null>(null)

  /**
   * 这次建档的 `Idempotency-Key`(PRD §9.1)。**按表单会话生成一次。**
   *
   * 在此之前这一页没有键,于是双击提交的第二次请求撞 `uq_spus_spu_code`,
   * 运营看到的是「SPU 编码 X 已存在」—— 在双击这个语境下那是一句假话。
   *
   * 用 `useState` 的惰性初始值而不是每次渲染现算:现算的话每次重渲染都换一把,
   * 键就退化成一个随机串,一次都不会命中。点「再建一个」时显式换新的 ——
   * 那确实是另一次请求。
   */
  const [requestKey, setRequestKey] = useState(() => crypto.randomUUID())

  /**
   * 尺码模板**从后端拿**(硬规则 4)。
   *
   * 内置一份的话,加一个模板要改两个仓库,而漏改的那一侧不报错 ——
   * 它只会少一个选项,而少的那个恰恰是新加的那个。
   */
  const templates = useQuery({
    queryKey: ['size-templates'],
    queryFn: () => spusApi.sizeTemplates(),
  })

  /**
   * 建档是写请求,而且**不是幂等的**:同一个 `spu_code` 提交两次,
   * 第二次会被后端的唯一约束挡下,但"结果未知"(超时、502)那一档不会 ——
   * 那时运营不知道上一次到底建没建。所以这里显式传 `noRefresh`:
   * 这一页没有可刷新的列表,唯一正确的下一步是去 SPU 聚合页看一眼,
   * 而那句话由 `readWriteError` 的"结果未知"文案负责说。
   */
  const onWriteError = useWriteError(noRefresh)

  const create = useMutation({
    mutationFn: () =>
      spusApi.create(
        {
          spu_code: basics.spu_code.trim(),
          internal_name: basics.internal_name.trim(),
          audience: basics.audience!,
          base_category: basics.base_category.trim() || 'swimwear',
          supplier_ref: basics.supplier_ref.trim() || null,
          color_variants: colours.map((c) => ({
            variant_code: c.variant_code.trim(),
            working_name: c.working_name.trim(),
            supplier_color_code: c.supplier_color_code?.trim() || null,
          })),
          size_template: sizeTemplate!,
        },
        requestKey,
      ),
    onSuccess: (spu) => {
      setCreated(spu)
      message.success(`已建档:${spu.spu_code},${spu.skus.length} 个 SKU`)
    },
    onError: onWriteError,
  })

  const sizes = useMemo(
    () => templates.data?.find((t) => t.name === sizeTemplate)?.sizes ?? [],
    [templates.data, sizeTemplate],
  )

  /** 填了任何一个字段就算有未保存内容(A11)。建档没建成时离开会全部丢失 */
  const dirty =
    !created &&
    (basics.spu_code !== '' ||
      basics.internal_name !== '' ||
      basics.audience !== undefined ||
      colours.some((c) => c.variant_code !== '' || c.working_name !== ''))

  /**
   * 每一步"能不能往下走"。
   *
   * **只挡连请求都发不出去的情况**(必填项空着)。编码字符集、重复颜色、
   * 行数上限一律交给后端 —— 抄一份规则到这里的下场是两个版本,
   * 而先过期的那一个会让一个合法的输入在界面上被拒,运营无从申诉。
   */
  const basicsReady =
    basics.spu_code.trim() !== '' &&
    basics.internal_name.trim() !== '' &&
    basics.audience !== undefined
  const coloursReady =
    colours.length > 0 && colours.every((c) => c.variant_code.trim() !== '')
  const matrixReady = sizeTemplate !== undefined && sizes.length > 0

  const colourColumns: ColumnsType<ColorVariantDraft & { index: number }> = [
    {
      title: '颜色编码',
      dataIndex: 'variant_code',
      width: 180,
      render: (_, row) => (
        <Input
          value={row.variant_code}
          placeholder="例如 BLK"
          maxLength={16}
          onChange={(e) =>
            setColours((prev) =>
              prev.map((c, i) =>
                i === row.index ? { ...c, variant_code: e.target.value } : c,
              ),
            )
          }
        />
      ),
    },
    {
      title: '工作名称',
      dataIndex: 'working_name',
      render: (_, row) => (
        <Input
          value={row.working_name}
          placeholder="供应商口中的那个名字,内部用"
          maxLength={128}
          onChange={(e) =>
            setColours((prev) =>
              prev.map((c, i) =>
                i === row.index ? { ...c, working_name: e.target.value } : c,
              ),
            )
          }
        />
      ),
    },
    {
      title: '供应商色号',
      dataIndex: 'supplier_color_code',
      width: 160,
      render: (_, row) => (
        <Input
          value={row.supplier_color_code ?? ''}
          placeholder="选填"
          maxLength={64}
          onChange={(e) =>
            setColours((prev) =>
              prev.map((c, i) =>
                i === row.index
                  ? { ...c, supplier_color_code: e.target.value }
                  : c,
              ),
            )
          }
        />
      ),
    },
    {
      title: '',
      key: 'ops',
      width: 60,
      render: (_, row) => (
        <Button
          type="text"
          danger
          icon={<DeleteOutlined />}
          // 最后一行不许删:一个 SPU 至少要有一个颜色(后端 min_length=1),
          // 删到零行的话"下一步"按钮会灰掉而没有任何提示说明为什么
          disabled={colours.length <= 1}
          onClick={() =>
            setColours((prev) => prev.filter((_, i) => i !== row.index))
          }
        />
      ),
    },
  ]

  if (created) {
    return (
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <PageHeader title="建档完成" subtitle={created.spu_code} />
        <Alert
          type="success"
          showIcon
          message={`${created.spu_code} 已建档,${created.color_variants.length} 个颜色 × ${created.skus.length / Math.max(created.color_variants.length, 1)} 个尺码 = ${created.skus.length} 个 SKU`}
          description="下一步:到素材页按颜色上传样品图。通用图只进 SPU 作用域,证明不了某个颜色有正面照。"
        />
        <Card size="small" title="生成的 SKU">
          {/*
            * 这一份**是后端回出来的真值**,不是前端拼的。
            * 编码怎么拼住在 `listings/sku_matrix`,前端照着抄一份的话,
            * 改一次拼法界面和库里就是两套编码 —— 而运营会拿界面上那个
            * 去平台后台搜,搜不到,然后怀疑没建成功。
            */}
          <Table<SpuSku>
            rowKey="id"
            size="small"
            bordered
            pagination={false}
            dataSource={created.skus}
            columns={[
              { title: 'SKU', dataIndex: 'sku', render: (v: string) => <span className="mono">{v}</span> },
              { title: '尺码', dataIndex: 'size', width: 100 },
              {
                title: '颜色',
                dataIndex: 'color_variant_id',
                width: 160,
                render: (id: string | null) => {
                  const v = created.color_variants.find((c) => c.id === id)
                  return v ? (v.working_name || v.variant_code) : '—'
                },
              },
            ]}
          />
        </Card>
        <Space wrap>
          {/*
            AC-01:建档完成之后的下一步是**进七步向导**,不是回到一张列表
            去找它。这一条是 2026-08-09 评审补的 —— 在此之前建档与向导之间
            断着一跳:运营建完款,得自己去商品列表里把刚建的 SKU 找出来,
            再点进向导。AC-01 的原话是"普通运营从建档到形成完整 Draft 的
            七步全程在向导内完成",而那一跳恰恰落在第 1 步与第 2 步之间。

            落到 `?step=MATERIAL`:建档这一步刚做完,停在它上面等于让运营
            自己再点一次"下一步"。向导的 `?step=` 是白名单 codec,认不出来
            会安全退回后端算出的 `current_step`。

            `skus[0]` 一定存在:`expand()` 至少展开一行,零行会在后端 422。
          */}
          {created.skus.length > 0 && (
            <Button
              type="primary"
              onClick={() =>
                navigate(`/wizard/${created.skus[0].id}?step=MATERIAL`)
              }
            >
              进入七步向导(下一步:上传样品)
            </Button>
          )}
          <Button onClick={() => navigate(`/spus/${created.id}`)}>
            去配置生成方案
          </Button>
          <Button onClick={() => navigate('/workbench-spus')}>去 SPU 聚合页</Button>
          <Button
            onClick={() => {
              setCreated(null)
              setBasics(EMPTY_BASICS)
              setColours([emptyColour()])
              setSizeTemplate(undefined)
              setCurrent(0)
              // **换一把键。** 不换的话下一次建档会命中上一次的键,
              // 后端按"同键不同入参"答 409 —— 而运营看到的是"又失败了"
              setRequestKey(crypto.randomUUID())
            }}
          >
            再建一个
          </Button>
        </Space>
      </Space>
    )
  }

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <UnsavedGuard dirty={dirty} what="建档表单" />
      <PageHeader
        title="新建商品款式"
        subtitle="按三步建立 SPU、颜色和首批 SKU；视觉属性可以稍后识别补全"
      />

      <Steps
        current={current}
        items={[
          { title: '基本信息' },
          { title: '颜色' },
          { title: '尺码与确认' },
        ]}
      />

      {current === 0 && (
        <Card size="small" title="第一步 · 这个款是什么">
          <Form layout="vertical">
            <Form.Item label="SPU 编码" required>
              <Input
                value={basics.spu_code}
                placeholder="例如 SW-001"
                maxLength={64}
                onChange={(e) => setBasics((b) => ({ ...b, spu_code: e.target.value }))}
              />
            </Form.Item>
            <Form.Item label="内部名称" required>
              <Input
                value={basics.internal_name}
                placeholder="例如 三角比基尼套装"
                maxLength={255}
                onChange={(e) =>
                  setBasics((b) => ({ ...b, internal_name: e.target.value }))
                }
              />
            </Form.Item>
            <Form.Item
              label="受众"
              required
              // §4.2:SPU 层**不存在**"待确认受众"。选不出来说明这个款还不该建档
              extra="SPU 层没有「待确认」这一档。必填属性集按受众条件化,受众没定就往下走,确认的是一份可能是错的清单"
            >
              <Select<SpuAudience>
                value={basics.audience}
                style={{ maxWidth: 240 }}
                placeholder="选择受众"
                onChange={(v) => setBasics((b) => ({ ...b, audience: v }))}
                options={Object.entries(SPU_AUDIENCE_LABEL).map(([value, label]) => ({
                  value: value as SpuAudience,
                  label,
                }))}
              />
            </Form.Item>
            <Form.Item label="品类" extra="目前只有泳装有渠道字段 spec 与校准过的属性注册表">
              <Input
                value={basics.base_category}
                style={{ maxWidth: 240 }}
                maxLength={64}
                onChange={(e) =>
                  setBasics((b) => ({ ...b, base_category: e.target.value }))
                }
              />
            </Form.Item>
            <Form.Item label="供应商编号">
              <Input
                value={basics.supplier_ref}
                style={{ maxWidth: 320 }}
                placeholder="选填"
                maxLength={128}
                onChange={(e) =>
                  setBasics((b) => ({ ...b, supplier_ref: e.target.value }))
                }
              />
            </Form.Item>
          </Form>
          <Button type="primary" disabled={!basicsReady} onClick={() => setCurrent(1)}>
            下一步
          </Button>
        </Card>
      )}

      {current === 1 && (
        <Card
          size="small"
          title="第二步 · 有哪几个颜色"
          extra={
            <Button
              size="small"
              icon={<PlusOutlined />}
              onClick={() => setColours((prev) => [...prev, emptyColour()])}
            >
              加一个颜色
            </Button>
          }
        >
          {/*
            * 「正式颜色名称」(`display_name`)**不在这张表里**。
            * 它是投影列,唯一写入点是属性服务在 VARIANT 层
            * `standard_color_name` 被确认时(§4.3)。给它一个输入框的话,
            * 建档时填进去的值会在第一次识别确认时被覆盖 —— 而运营不知道
            * 是自己填错了还是系统改了。
            */}
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 10 }}
            message="这里填的是「工作名称」——供应商口中的那个名字,内部用"
            description="正式颜色名称由属性识别确认后回填,建档阶段填不了,也不该填。颜色编码在 SPU 内唯一;跨 SPU 同名是常态(几乎每个款都有黑色)。"
          />
          <Table<ColorVariantDraft & { index: number }>
            rowKey="index"
            size="small"
            bordered
            pagination={false}
            columns={colourColumns}
            dataSource={colours.map((c, index) => ({ ...c, index }))}
          />
          <Space style={{ marginTop: 10 }}>
            <Button onClick={() => setCurrent(0)}>上一步</Button>
            <Button type="primary" disabled={!coloursReady} onClick={() => setCurrent(2)}>
              下一步
            </Button>
          </Space>
        </Card>
      )}

      {current === 2 && (
        <Card size="small" title="第三步 · 尺码与确认">
          {templates.isError ? (
            <ErrorNotice
              title="尺码模板没拉到"
              error={templates.error}
              onRetry={() => templates.refetch()}
            />
          ) : (
            <Form layout="vertical">
              <Form.Item label="尺码模板" required>
                <Select<string>
                  value={sizeTemplate}
                  style={{ maxWidth: 320 }}
                  loading={templates.isLoading}
                  placeholder="选择尺码段"
                  onChange={setSizeTemplate}
                  options={(templates.data ?? []).map((t) => ({
                    value: t.name,
                    label: `${t.label}(${t.sizes.join(' / ')})`,
                  }))}
                />
              </Form.Item>
            </Form>
          )}

          <Descriptions size="small" bordered column={2} style={{ marginBottom: 10 }}>
            <Descriptions.Item label="SPU 编码">
              <span className="mono">{basics.spu_code}</span>
            </Descriptions.Item>
            <Descriptions.Item label="内部名称">{basics.internal_name}</Descriptions.Item>
            <Descriptions.Item label="受众">
              {basics.audience ? SPU_AUDIENCE_LABEL[basics.audience] : '—'}
            </Descriptions.Item>
            <Descriptions.Item label="品类">{basics.base_category}</Descriptions.Item>
            <Descriptions.Item label="颜色" span={2}>
              <Space size={4} wrap>
                {colours.map((c, i) => (
                  <Tag key={i}>{c.working_name || c.variant_code}</Tag>
                ))}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="尺码" span={2}>
              {sizes.length ? (
                <Space size={4} wrap>
                  {sizes.map((s) => (
                    <Tag key={s}>{s}</Tag>
                  ))}
                </Space>
              ) : (
                <span style={{ color: brandVars.textFaint }}>先选尺码模板</span>
              )}
            </Descriptions.Item>
          </Descriptions>

          {/*
            * **预览的是数量,不是编码。**
            *
            * SKU 编码怎么拼住在 `listings/sku_matrix`,前端拼一份的话,
            * 改一次拼法界面和库里就是两套 —— 而运营会拿界面上那个去平台
            * 后台搜,搜不到,然后怀疑没建成功。
            *
            * 数量不一样:`颜色 × 尺码` 是运营自己能验算的算术,而且它正是
            * 「三颜色九 SKU」那句验收要看的数。真的 SKU 列表建完由后端回。
            */}
          {matrixReady ? (
            <Card size="small" type="inner" title="会生成多少行">
              <Space size={32}>
                <Statistic title="颜色" value={colours.length} />
                <Statistic title="尺码" value={sizes.length} />
                <Statistic
                  title="SKU 合计"
                  value={colours.length * sizes.length}
                  valueStyle={{ color: brandVars.slate }}
                />
              </Space>
              <p style={{ marginTop: 8, marginBottom: 0, color: brandVars.textFaint }}>
                具体的 SKU 编码由后端生成 —— 拼法住在一处,这里不复制一份。
                建完立刻能看到真正的那一份。
              </p>
            </Card>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选好尺码模板才能算行数" />
          )}

          {create.isError && (
            <Alert
              style={{ marginTop: 10 }}
              type="error"
              showIcon
              message="建档没成功"
              description="编码字符集、重复颜色、行数上限这些规则由后端校验,上面的提示里有具体原因。改完再提交一次即可,不会产生半个 SPU —— 建档是一次事务。"
            />
          )}

          <Space style={{ marginTop: 10 }}>
            <Button onClick={() => setCurrent(1)}>上一步</Button>
            <Button
              type="primary"
              loading={create.isPending}
              disabled={!matrixReady || !basicsReady || !coloursReady}
              onClick={() => create.mutate()}
            >
              建档
            </Button>
          </Space>
        </Card>
      )}
    </Space>
  )
}
