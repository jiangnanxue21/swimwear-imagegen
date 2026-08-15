/**
 * SPU 详情 —— **生成方案面板的宿主页**(阶段 4 交付第一项的 UI 那一瓣)。
 *
 * ## 这一页存在的唯一理由
 *
 * `GenerationPlanPanel` 与 `api/generationPlans.ts` 从 A45-batch14-20 起就写完了,
 * 之后**连着四批全树零 import**。历次交接把原因记成"缺一行 import",
 * 14-23 逐条核路由时核出那句话是错的:面板要 `spuId`(UUID),而当时
 * 全前端没有任何一条路径拿得到 SPU 主键 —— 唯一在出参里给 `spu_id` 的
 * schema 是方案接口自己,**要拿主键得先有方案,要列方案得先有主键**。
 *
 * 所以缺的不是一行 import,是一个**知道 SPU 主键的页面**。就是这一页。
 * 决策见 `docs/DECISIONS.md` §3.48。
 *
 * ## 为什么不挂在 `/workbench-spus`(SPU 聚合)上
 *
 * 那一页按 `products.spu` 字符串码分组,一行可能对应"没有主键的老商品"。
 * 把方案面板直接铺在那张表的展开行里,等于让一个**只在部分行上成立**的
 * 功能长在每一行下面 —— 而不成立的那些行没有任何视觉差别。
 * 现在的分法是:聚合页负责"哪个 SPU 该处理",详情页负责"对这个 SPU 做什么",
 * 而聚合页只在后端真给了主键时才给出通往这里的链接。
 *
 * ## 这一页不自己判断任何状态
 *
 * 颜色叫什么走 `colorVariantLabel`(与后端 `_material_facts` 拼
 * `variant_labels` 的顺序逐字相同);哪一份方案对哪个颜色生效由后端
 * `resolve_plan` 说了算,面板只展示。硬规则 4。
 *
 * ## 这一页也是那四个「写完没人调」的方法的落点
 *
 * `spusApi.update` / `addColorVariant` / `updateColorVariant` /
 * `disable` / `restore` —— 后端端点齐了、前端客户端也写了,而**全树零
 * 调用点**。和 `GenerationPlanPanel`(见上面那段)、`facts_stale`、
 * `variant_gate_roles` 是同一个形状:做完、门禁全绿、动线断在最后一跳。
 *
 * 挂在这一页而不是 SPU 聚合页,理由与方案面板逐字相同:那一页按
 * `products.spu` 字符串分组,一行可能对应"没有主键的老商品",
 * 而写操作**必须**有主键。聚合页只在后端真给了主键时给出通往这里的链接。
 *
 * ## 乐观锁:`expected_version` 从出参的 `row_version` 带上
 *
 * 后端 `update_spu` / `update_color_variant` 都要它,版本对不上抛
 * `ConcurrentTransition`(409)。这一页把那句话翻成一颗**刷新**按钮 ——
 * 那类失败的正确下一步是"看一眼别人改成了什么",不是"再点一次保存"。
 *
 * 停用与恢复**不要**版本号,与 `archive_product` 同一条:它们是带闸的
 * 状态迁移,要版本号的话一次双击的第二下会撞 409「已被其他人更新」,
 * 而那句话在双击这个语境下是假的。
 */
import { useCallback, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import {
  Alert, App, Button, Card, Descriptions, Empty, Form, Input, Select,
  Skeleton, Space, Table, Tag,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'

import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import GenerationPlanPanel from '../components/GenerationPlanPanel'
import {
  SPU_AUDIENCE_LABEL,
  colorVariantLabel,
  spusApi,
  type ColorVariant,
  type SpuSku,
} from '../api/spus'
import { useWriteError } from '../hooks/useWriteError'
import { brandVars, fontScale } from '../theme'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

/**
 * 可售状态的界面文案(§4.3)。
 *
 * `PAUSED` 与 `DISABLED` 的区别是**打算**不是当下 —— 枚举注释里写着
 * 「合成一个取值的话,停售一次就再也提示不出来了」。所以这四档一个都不合并,
 * 括号里那半句是让运营选得对的关键。
 */
const SELLABLE_LABEL: Record<string, string> = {
  PLANNED: '计划中(颜色建了,样品还没到)',
  ACTIVE: '在售',
  PAUSED: '暂停(先不上,过阵子再说)',
  DISABLED: '停做(这个颜色不做了)',
}

export default function SpuDetailPage() {
  const { spuId = '' } = useParams<{ spuId: string }>()
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const query = useQuery({
    queryKey: ['spu', spuId],
    queryFn: () => spusApi.get(spuId),
    enabled: Boolean(spuId),
  })
  const spu = query.data
  useDocumentTitle(spu ? `SPU ${spu.spu_code}` : 'SPU 详情')

  const [editing, setEditing] = useState(false)
  const [draft, setDraft] = useState({ internal_name: '', supplier_ref: '', notes: '' })
  /** 正在编辑哪一行颜色。`null` = 没有。一次只编一行 —— 全表可编辑的话,
      一次误触会把三行一起写出去,而那三行的版本号各自独立 */
  const [editingColour, setEditingColour] = useState<string | null>(null)
  const [colourDraft, setColourDraft] = useState({
    working_name: '',
    supplier_color_code: '',
    sellable_status: '',
  })
  const [addingColour, setAddingColour] = useState(false)
  const [newColour, setNewColour] = useState({ variant_code: '', working_name: '' })

  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['spu', spuId] })
  }, [queryClient, spuId])

  const onWriteError = useWriteError(refresh)

  const saveBasics = useMutation({
    mutationFn: () =>
      spusApi.update(spuId, {
        // **版本号从出参带上。** 不带的话后端 422;带错的话后端 409,
        // 而 409 那句话("已被其他人更新,请刷新后重试")正是运营该看到的
        expected_version: spu!.row_version,
        internal_name: draft.internal_name.trim(),
        supplier_ref: draft.supplier_ref.trim() || null,
        notes: draft.notes.trim() || null,
      }),
    onSuccess: () => {
      message.success('已保存')
      setEditing(false)
      refresh()
    },
    onError: onWriteError,
  })

  const saveColour = useMutation({
    mutationFn: (row: ColorVariant) =>
      spusApi.updateColorVariant(spuId, row.id, {
        expected_version: row.row_version,
        working_name: colourDraft.working_name.trim(),
        supplier_color_code: colourDraft.supplier_color_code.trim() || null,
        sellable_status: colourDraft.sellable_status,
      }),
    onSuccess: () => {
      message.success('颜色已保存')
      setEditingColour(null)
      refresh()
    },
    onError: onWriteError,
  })

  /**
   * 加一个颜色。**后端会连带铺开这个颜色的整组 SKU**(按既有尺码模板),
   * 而且是一个事务 —— 所以这颗按钮的后果比它看起来大,提示里要说出来。
   *
   * 不传 `size_template`:后端从现有 SKU 推,推不出唯一值时报错并要求显式传。
   * 前端猜一个的话,猜错的表现是这个颜色的尺码和别的颜色不一样,而没有
   * 任何地方会报错。
   */
  const addColour = useMutation({
    mutationFn: () =>
      spusApi.addColorVariant(spuId, {
        variant_code: newColour.variant_code.trim(),
        working_name: newColour.working_name.trim(),
      }),
    onSuccess: (row) => {
      message.success(`已加颜色 ${row.variant_code},并按既有尺码模板铺开了 SKU`)
      setAddingColour(false)
      setNewColour({ variant_code: '', working_name: '' })
      refresh()
    },
    onError: onWriteError,
  })

  const disable = useMutation({
    mutationFn: (reason: string) => spusApi.disable(spuId, reason),
    onSuccess: () => {
      message.success('已停用。它不再接受新的颜色与 SKU;已经在做的那些行不受影响')
      refresh()
    },
    onError: onWriteError,
  })

  const restore = useMutation({
    mutationFn: () => spusApi.restore(spuId),
    onSuccess: () => {
      message.success('已恢复。它回到「建档中」')
      refresh()
    },
    onError: onWriteError,
  })

  /**
   * 停用前问一次理由。
   *
   * 与工作台归档那一处同一条:理由必填,且**空理由不许提交** ——
   * 后端会 422,而那时对话框已经关了,运营会以为停用成功了。
   */
  const confirmDisable = () => {
    if (!spu) return
    let reason = ''
    modal.confirm({
      title: `停用 ${spu.spu_code}?`,
      width: 520,
      content: (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <div>
            停用之后这个款从建档列表里消失,<b>不再接受新的颜色与 SKU</b>。
            颜色、SKU、素材、属性、审计一样不少 —— 勾选「显示已停用」找得回来。
          </div>
          <div style={{ color: brandVars.textFaint }}>
            它<b>不会</b>连带归档底下那 {spu.sku_count} 行 SKU:归档一行有它
            自己的闸和自己的理由,一个按钮背后迁移十几行的话,那十几条审计
            记录的理由会全是同一句。
          </div>
          <Input.TextArea
            rows={2}
            placeholder="停用理由(必填),例如:款式下架 / 重复建档 / 供应商撤款"
            onChange={(e) => {
              reason = e.target.value
            }}
          />
        </Space>
      ),
      okText: '停用',
      okButtonProps: { danger: true },
      cancelText: '取消',
      onOk: () => {
        if (!reason.trim()) {
          message.error('要填停用理由 —— 三个月后复盘时,只有它答得出这个款为什么不做了')
          return Promise.reject(new Error('reason required'))
        }
        return disable.mutateAsync(reason.trim())
      },
    })
  }

  /**
   * 颜色 id -> 显示名。面板拿它把作用域显示成名字而不是 UUID。
   *
   * 空对象也照样传:面板的"作用域"下拉会因此只剩「SPU 默认」一项,
   * 那是对的 —— 一个还没有颜色的 SPU 本来就配不出颜色覆盖方案。
   */
  const variantLabels: Record<string, string> = Object.fromEntries(
    (spu?.color_variants ?? []).map((v) => [v.id, colorVariantLabel(v)]),
  )

  const disabled = spu?.status === 'DISABLED'

  const colorColumns: ColumnsType<ColorVariant> = [
    {
      // 工作名称是**可编辑**的那一个(供应商口中的那个名字,内部用)。
      // 正式名称在下一列,那一列不可编辑 —— 它是投影列
      title: '工作名称',
      key: 'label',
      render: (_, row) =>
        editingColour === row.id ? (
          <Input
            value={colourDraft.working_name}
            maxLength={128}
            placeholder="供应商口中的那个名字"
            onChange={(e) =>
              setColourDraft((d) => ({ ...d, working_name: e.target.value }))
            }
          />
        ) : (
          colorVariantLabel(row)
        ),
    },
    {
      // **编码不可编辑,这是刻意的。** 它是 SPU 内的稳定标识,而且已经
      // 拼进了这个颜色底下每一行 SKU 的编码里(`SW-001-BLK-S`)。改它
      // 要么让界面和库里的 SKU 对不上,要么要重命名一整组已经挂着素材、
      // 生成任务和图片集的行 —— 那不是"编辑",是重建
      title: '编码',
      dataIndex: 'variant_code',
      key: 'code',
      width: 120,
      render: (value: string) => <span className="mono">{value}</span>,
    },
    {
      // display_name 是投影列:唯一写入点是属性服务在 VARIANT 层确认
      // `standard_color_name` 的那一刻。建档后它是空的,而空在这里有含义 ——
      // 它就是"这个颜色的正式名称还没被确认过",所以照实显示,不回落
      title: '正式名称',
      dataIndex: 'display_name',
      key: 'display_name',
      width: 160,
      render: (value: string) =>
        value || <span style={{ color: brandVars.textFaint }}>待属性确认</span>,
    },
    {
      title: '供应商色号',
      dataIndex: 'supplier_color_code',
      key: 'supplier_color_code',
      width: 160,
      render: (value: string | null, row) =>
        editingColour === row.id ? (
          <Input
            value={colourDraft.supplier_color_code}
            maxLength={64}
            placeholder="选填"
            onChange={(e) =>
              setColourDraft((d) => ({ ...d, supplier_color_code: e.target.value }))
            }
          />
        ) : (
          value || <span style={{ color: brandVars.textFaint }}>—</span>
        ),
    },
    {
      title: '可售状态',
      dataIndex: 'sellable_status',
      key: 'sellable_status',
      width: 200,
      render: (value: string, row) =>
        editingColour === row.id ? (
          <Select<string>
            value={colourDraft.sellable_status}
            style={{ width: '100%' }}
            onChange={(v) => setColourDraft((d) => ({ ...d, sellable_status: v }))}
            options={Object.entries(SELLABLE_LABEL).map(([v, label]) => ({
              value: v,
              label,
            }))}
          />
        ) : (
          <Tag>{SELLABLE_LABEL[value] ?? value}</Tag>
        ),
    },
    {
      title: '',
      key: 'ops',
      width: 130,
      render: (_, row) =>
        editingColour === row.id ? (
          <Space size={4}>
            <Button
              size="small"
              type="primary"
              loading={saveColour.isPending}
              onClick={() => saveColour.mutate(row)}
            >
              保存
            </Button>
            <Button size="small" onClick={() => setEditingColour(null)}>
              取消
            </Button>
          </Space>
        ) : (
          <Button
            size="small"
            type="link"
            // 停用的款上不给编辑入口。后端不会拒绝改颜色元数据(那不是
            // "长出新东西"),但一个已经说了不做的款上摆着编辑按钮,
            // 会让人以为它还在动线里
            disabled={disabled || editingColour !== null}
            onClick={() => {
              setEditingColour(row.id)
              setColourDraft({
                working_name: row.working_name,
                supplier_color_code: row.supplier_color_code ?? '',
                sellable_status: row.sellable_status,
              })
            }}
          >
            编辑
          </Button>
        ),
    },
  ]

  const skuColumns: ColumnsType<SpuSku> = [
    {
      title: 'SKU',
      key: 'sku',
      render: (_, row) => (
        <Link className="mono" to={`/workbench/${row.id}`}>
          {row.sku}
        </Link>
      ),
    },
    {
      title: '颜色',
      key: 'color',
      width: 140,
      render: (_, row) => {
        const label = row.color_variant_id ? variantLabels[row.color_variant_id] : undefined
        return label ?? <span style={{ color: brandVars.textFaint }}>—</span>
      },
    },
    {
      title: '尺码',
      dataIndex: 'size',
      key: 'size',
      width: 90,
      render: (value: string | null) =>
        value || <span style={{ color: brandVars.textFaint }}>—</span>,
    },
    {
      title: '条码',
      dataIndex: 'barcode',
      key: 'barcode',
      width: 160,
      render: (value: string | null) =>
        value || <span style={{ color: brandVars.textFaint }}>—</span>,
    },
    {
      title: '价格',
      dataIndex: 'price',
      key: 'price',
      width: 110,
      render: (value: string | null) =>
        value || <span style={{ color: brandVars.textFaint }}>—</span>,
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader
        title={spu ? `SPU ${spu.spu_code}` : 'SPU 详情'}
        subtitle="这个款的颜色、SKU 与生成方案"
        extra={
          spu ? (
            <Space>
              {disabled ? (
                <Button loading={restore.isPending} onClick={() => restore.mutate()}>
                  恢复到建档动线
                </Button>
              ) : (
                /*
                  按钮叫「停用」不叫「删除」。这不是措辞洁癖:它做的确实
                  不是删除 —— 颜色、SKU、素材、属性、审计一行都不动,只是
                  这个款不再接受新东西、并从建档列表里消失。写成「删除」
                  的话,第一个点它的人会以为库里那些行没了
                */
                <Button danger loading={disable.isPending} onClick={confirmDisable}>
                  停用这个款
                </Button>
              )}
            </Space>
          ) : undefined
        }
      />

      {query.isError && (
        <ErrorNotice
          title="拉不到这个 SPU"
          error={query.error}
          onRetry={() => query.refetch()}
        />
      )}

      {query.isLoading && <Skeleton active paragraph={{ rows: 4 }} />}

      {spu && (
        <>
          {disabled && (
            <Alert
              type="warning"
              showIcon
              message="这个款已停用"
              description="它不再接受新的颜色与 SKU,也不出现在建档列表里。底下已经在做的那些 SKU 行不受影响 —— 归档一行是它自己的动作。要继续做这个款,先点右上角「恢复」。"
            />
          )}

          <Card
            size="small"
            title="基本信息"
            extra={
              editing ? (
                <Space size={4}>
                  <Button
                    size="small"
                    type="primary"
                    loading={saveBasics.isPending}
                    disabled={!draft.internal_name.trim()}
                    onClick={() => saveBasics.mutate()}
                  >
                    保存
                  </Button>
                  <Button size="small" onClick={() => setEditing(false)}>
                    取消
                  </Button>
                </Space>
              ) : (
                <Button
                  size="small"
                  type="link"
                  disabled={disabled}
                  onClick={() => {
                    // 从**当前出参**填草稿,不是从一个空表单开始。
                    // 空表单 + PATCH 语义(不传=不改)看起来也能用,但保存
                    // 那一刻运营看到的是空的、库里是旧的,而两者不一致时
                    // 他不知道自己刚才改了什么
                    setDraft({
                      internal_name: spu.internal_name,
                      supplier_ref: spu.supplier_ref ?? '',
                      notes: spu.notes ?? '',
                    })
                    setEditing(true)
                  }}
                >
                  编辑
                </Button>
              )
            }
          >
            {editing ? (
              <Form layout="vertical">
                <Form.Item label="内部名称" required>
                  <Input
                    value={draft.internal_name}
                    maxLength={255}
                    onChange={(e) =>
                      setDraft((d) => ({ ...d, internal_name: e.target.value }))
                    }
                  />
                </Form.Item>
                <Form.Item label="供应商编号">
                  <Input
                    value={draft.supplier_ref}
                    maxLength={128}
                    placeholder="选填"
                    onChange={(e) =>
                      setDraft((d) => ({ ...d, supplier_ref: e.target.value }))
                    }
                  />
                </Form.Item>
                <Form.Item
                  label="备注"
                  // SPU 编码、受众、品类**不在这张表单里**。它们是身份:
                  // 编码拼进了每一行 SKU;受众决定模特、槽位表、检查项、
                  // 尺码表和平台类目(§4.2 把它列进禁改清单)。后端
                  // `SpuPatch` 也只收这三个字段 —— 两边是同一份判断
                  extra="SPU 编码、受众、品类改不了 —— 它们是身份,不是元数据。受众填错要重建这个款"
                >
                  <Input.TextArea
                    rows={2}
                    value={draft.notes}
                    onChange={(e) => setDraft((d) => ({ ...d, notes: e.target.value }))}
                  />
                </Form.Item>
              </Form>
            ) : (
              <Descriptions size="small" column={3}>
                <Descriptions.Item label="内部名称">{spu.internal_name}</Descriptions.Item>
                <Descriptions.Item label="受众">
                  {/* 受众在 SPU 层必填且没有"待确认"(§4.2),所以这里不会有空态 */}
                  <Tag>{SPU_AUDIENCE_LABEL[spu.audience] ?? spu.audience}</Tag>
                </Descriptions.Item>
                <Descriptions.Item label="品类">{spu.base_category}</Descriptions.Item>
                <Descriptions.Item label="状态">{spu.status}</Descriptions.Item>
                <Descriptions.Item label="SKU 数">{spu.sku_count}</Descriptions.Item>
                <Descriptions.Item label="供应商编号">
                  {spu.supplier_ref || '—'}
                </Descriptions.Item>
                <Descriptions.Item label="备注" span={3}>
                  {spu.notes || <span style={{ color: brandVars.textFaint }}>—</span>}
                </Descriptions.Item>
              </Descriptions>
            )}
          </Card>

          <Card
            size="small"
            title={`颜色(${spu.color_variants.length})`}
            extra={
              <Button
                size="small"
                icon={<PlusOutlined />}
                disabled={disabled || addingColour}
                onClick={() => setAddingColour(true)}
              >
                加一个颜色
              </Button>
            }
          >
            {addingColour && (
              <Space.Compact style={{ width: '100%', marginBottom: 10 }}>
                <Input
                  style={{ width: 200 }}
                  value={newColour.variant_code}
                  placeholder="颜色编码,例如 NVY"
                  maxLength={16}
                  // 与建档页同一个归一化(去空白 + 转大写),而且**只有**
                  // 这两件事 —— 后端 `normalize_code` 刻意不删非法字符
                  onBlur={(e) =>
                    setNewColour((c) => ({
                      ...c,
                      variant_code: e.target.value.trim().toUpperCase(),
                    }))
                  }
                  onChange={(e) =>
                    setNewColour((c) => ({ ...c, variant_code: e.target.value }))
                  }
                />
                <Input
                  value={newColour.working_name}
                  placeholder="工作名称(选填)"
                  maxLength={128}
                  onChange={(e) =>
                    setNewColour((c) => ({ ...c, working_name: e.target.value }))
                  }
                />
                <Button
                  type="primary"
                  loading={addColour.isPending}
                  disabled={!newColour.variant_code.trim()}
                  onClick={() => addColour.mutate()}
                >
                  加上
                </Button>
                <Button onClick={() => setAddingColour(false)}>取消</Button>
              </Space.Compact>
            )}
            {addingColour && (
              <Alert
                type="info"
                showIcon
                style={{ marginBottom: 10 }}
                message="加一个颜色会连带铺开这个颜色的整组 SKU"
                description="尺码模板由后端从现有 SKU 推出来;推不出唯一值时它会报错要求显式指定,而不是猜一个。整件事是一个事务,不会留下有颜色没 SKU 的半成品。"
              />
            )}
            <Table<ColorVariant>
              rowKey="id"
              size="small"
              columns={colorColumns}
              dataSource={spu.color_variants}
              pagination={false}
              locale={{ emptyText: <Empty description="这个 SPU 还没有颜色" /> }}
            />
          </Card>

          <Card
            size="small"
            title="生成方案"
            extra={
              <span style={{ fontSize: fontScale.meta, color: brandVars.slate }}>
                先存草稿,启用前会显示这会让哪些颜色的图片集过期
              </span>
            }
          >
            {/* 面板只要主键、颜色显示名与受众。它自己拉方案列表、自己处理失败 ——
                这一页不替它再判一次"哪一份生效",那条规则在后端。

                `productAudience` 是补的(2026-08-11 评审):不传它,面板拉
                模特候选集时**不带 `for_product_audience`**,于是 §10.5 的
                候选收窄在这个入口上完全不生效 —— 女装 SPU 的下拉里会出现
                男模特,而运营选中它、存下来、启用,一路都不会有人说什么。
                后端从这一轮起会拦(`_assert_model_template_usable`),
                但拦在保存那一刻仍然是"让他先选错再报错";候选集收窄才是
                让他选不到。两处都要有。

                SPU 的 `audience` 非空(建档第一步就要选),所以这里没有
                "待确认"那一档 —— 面板的 `null` 分支服务的是别的入口。 */}
            <GenerationPlanPanel
              spuId={spu.id}
              variantLabels={variantLabels}
              productAudience={spu.audience}
            />
          </Card>

          <Card size="small" title={`SKU(${spu.skus.length})`}>
            <Table<SpuSku>
              rowKey="id"
              size="small"
              columns={skuColumns}
              dataSource={spu.skus}
              pagination={false}
              locale={{ emptyText: <Empty description="这个 SPU 还没有 SKU" /> }}
            />
          </Card>
        </>
      )}
    </Space>
  )
}
