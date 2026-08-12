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
 */
import { Link, useParams } from 'react-router-dom'
import { Card, Descriptions, Empty, Skeleton, Space, Table, Tag } from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { useQuery } from '@tanstack/react-query'

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
import { brandVars, fontScale } from '../theme'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

export default function SpuDetailPage() {
  const { spuId = '' } = useParams<{ spuId: string }>()
  const query = useQuery({
    queryKey: ['spu', spuId],
    queryFn: () => spusApi.get(spuId),
    enabled: Boolean(spuId),
  })
  const spu = query.data
  useDocumentTitle(spu ? `SPU ${spu.spu_code}` : 'SPU 详情')

  /**
   * 颜色 id -> 显示名。面板拿它把作用域显示成名字而不是 UUID。
   *
   * 空对象也照样传:面板的"作用域"下拉会因此只剩「SPU 默认」一项,
   * 那是对的 —— 一个还没有颜色的 SPU 本来就配不出颜色覆盖方案。
   */
  const variantLabels: Record<string, string> = Object.fromEntries(
    (spu?.color_variants ?? []).map((v) => [v.id, colorVariantLabel(v)]),
  )

  const colorColumns: ColumnsType<ColorVariant> = [
    { title: '颜色', key: 'label', render: (_, row) => colorVariantLabel(row) },
    { title: '编码', dataIndex: 'variant_code', key: 'code', width: 120 },
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
      width: 140,
      render: (value: string | null) =>
        value || <span style={{ color: brandVars.textFaint }}>—</span>,
    },
    { title: '状态', dataIndex: 'sellable_status', key: 'sellable_status', width: 110 },
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
          <Card size="small">
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
            </Descriptions>
          </Card>

          <Card size="small" title={`颜色(${spu.color_variants.length})`}>
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
