import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Card, Empty, Input, Select, Space, Table, Tag,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ImportOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import { productsApi } from '../api/products'
import { readError } from '../api/client'
import {
  AUDIENCE_LABEL, GARMENT_TYPES, STATUS_LABEL, type Product, type ProductStatus,
} from '../api/types'
import { AudienceTag } from '../components/AudienceBadge'
import { useServerSort } from '../hooks/useServerSort'
import {
  enumParam, intParam, oneOfParam, textParam, useUrlFilters,
} from '../hooks/useUrlFilters'
import { brandVars } from '../theme'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

export default function ProductListPage() {
  useDocumentTitle('商品与 SKU')
  const navigate = useNavigate()

  /**
   * 筛选条件住在 URL 里(GAP-033)。这一页有 4 项,全在这张表里 ——
   * **加筛选项 = 往这张表加一行**,而不是再开一个 useState。
   *
   * A45 那一轮的两页(ProductListPage / ReviewQueuePage)是最后两块没搬的;
   * 它们当时被 STATUS 列为"已知限制",理由是排序一起搬要做 `useServerSort`
   * 的 store 接进 useUrlFilters。store 已在 §3.74 a49 那一轮写好,这一轮把
   * 那条缝接上。
   */
  const PAGE_SIZES = [10, 20, 50, 100] as const
const pageSizeParam = oneOfParam(20, PAGE_SIZES)

const filters = useUrlFilters({
    search: textParam(),
    status: enumParam<string>(Object.keys(STATUS_LABEL)),
    garment_type: enumParam<string>(GARMENT_TYPES as readonly string[]),
    page: intParam(1, { min: 1 }),
    page_size: pageSizeParam,
    sort: enumParam<string>(['created_at', 'sku', 'name', 'status']),
    order: enumParam<'asc' | 'desc'>(['asc', 'desc']),
  })
  const {
    search, status, garment_type: garmentType, page, page_size: pageSize,
  } = filters.values

  /**
   * 输入框里的字。**和已生效的 `search` 分开** —— 敲字不该每个字符打一次接口,
   * 但两者必须能被同一个动作一起清掉(走查 P0-3:非受控输入框会说谎)。
   *
   * URL 化之后这一对同步依然必要,而且多了一条:URL 变了(后退 / 前进 / 改地址栏)
   * 框里要跟着回来,否则框里留着字、列表却是另一组条件。
   */
  const [searchDraft, setSearchDraft] = useState(search)

  // 服务端排序(不是客户端的 sorter 函数):这张表一页只有 20 行,
  // 在前端排等于对这 20 行排,而运营以为排的是全部命中
  // **排序也住 URL 里** —— 只搬一半的代价在 useServerSort 顶部那段写明。
  const sort = useServerSort(
    { sort: 'created_at', order: 'desc' },
    undefined,
    {
      value: {
        sort: filters.values.sort ?? 'created_at',
        order: filters.values.order ?? 'desc',
      },
      set: (next) => filters.patch({ sort: next.sort, order: next.order, page: 1 }),
    },
  )

  const query = useQuery({
    queryKey: ['products', { search, status, garmentType, page, pageSize, ...sort.params }],
    queryFn: () =>
      productsApi.list({
        search: search || undefined,
        status,
        garment_type: garmentType,
        ...sort.params,
        page,
        page_size: pageSize,
      }),
  })

  const columns: ColumnsType<Product> = [
    {
      title: 'SKU',
      dataIndex: 'sku',
      key: 'sku',
      width: 160,
      sorter: true,
      sortOrder: sort.orderFor('sku'),
      render: (v: string, row) => (
        <a className="mono" onClick={() => navigate(`/products/${row.id}`)}>
          {v}
        </a>
      ),
    },
    {
      title: '商品名称',
      dataIndex: 'name',
      key: 'name',
      ellipsis: true,
      sorter: true,
      sortOrder: sort.orderFor('name'),
    },
    {
      // §13.2 之外的一处:受众在**商品列表**上也要看得见 —— 运营在这里
      // 决定先处理哪一批,而男装与女装的检查项完全不同(§14.1 / §14.2)
      title: '受众',
      dataIndex: 'audience',
      width: 90,
      render: (_: unknown, row: Product) => (
        <AudienceTag
          product={{
            audience: row.audience,
            // `/products` 这条路径不下发 audience_label(那是工作台出参的字段),
            // 所以这里补一次中文。**只在这一处补**:AudienceTag 自己不翻译,
            // 否则受众到中文的映射就有两份了
            audience_label: row.audience ? AUDIENCE_LABEL[row.audience] : '待确认',
          }}
        />
      ),
    },
    { title: '类型', dataIndex: 'garment_type', width: 120 },
    { title: '图案', dataIndex: 'pattern_type', width: 110 },
    {
      title: '颜色',
      dataIndex: 'primary_color',
      width: 150,
      render: (primary: string, row) => (
        <Space size={4} wrap>
          <Tag>{primary || '—'}</Tag>
          {row.secondary_colors.map((c) => (
            <Tag key={c} color="default">{c}</Tag>
          ))}
        </Space>
      ),
    },
    {
      title: '素材',
      dataIndex: 'asset_count',
      width: 80,
      align: 'center',
      render: (n: number) => (n > 0 ? n : <span style={{ color: brandVars.textFaint }}>0</span>),
    },
    {
      title: '状态',
      dataIndex: 'status',
      key: 'status',
      width: 110,
      sorter: true,
      sortOrder: sort.orderFor('status'),
      render: (s: ProductStatus) => (
        <Tag color={STATUS_LABEL[s]?.color}>{STATUS_LABEL[s]?.text ?? s}</Tag>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader
        title="商品与 SKU"
        subtitle="查看和维护已建档的 SKU；新款建档与批量导入从右侧入口开始"
      />

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="搜索 SKU / SPU / 名称"
            style={{ width: 260 }}
            value={searchDraft}
            onChange={(e) => setSearchDraft(e.target.value)}
            onSearch={(v) => filters.patch({ search: v, page: 1 })}
          />
          <Select
            allowClear
            placeholder="状态"
            style={{ width: 140 }}
            value={status}
            onChange={(v) => filters.patch({ status: v, page: 1 })}
            options={Object.entries(STATUS_LABEL).map(([value, meta]) => ({
              value,
              label: meta.text,
            }))}
          />
          <Select
            allowClear
            placeholder="服装类型"
            style={{ width: 150 }}
            value={garmentType}
            onChange={(v) => filters.patch({ garment_type: v, page: 1 })}
            options={GARMENT_TYPES.map((v) => ({ value: v, label: v }))}
          />
          <Button icon={<ReloadOutlined />} onClick={() => query.refetch()}>
            刷新
          </Button>
          <Button icon={<ImportOutlined />} onClick={() => navigate('/workbench-import')}>
            批量导入 SKU
          </Button>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => navigate('/spus/new')}>
            新建商品款式
          </Button>
        </Space>
      </Card>

      <Table<Product>
        rowKey="id"
        size="small"
        bordered
        columns={columns}
        dataSource={query.data?.items ?? []}
        loading={query.isFetching}
        onRow={(row) => ({ onDoubleClick: () => navigate(`/products/${row.id}`) })}
        onChange={sort.onTableChange}
        locale={{
          emptyText: (
            <Empty
              description={
                query.isError
                  ? readError(query.error)
                  : '还没有 SKU。请先新建商品款式，或向已有款式批量导入 SKU。'
              }
            />
          ),
        }}
        pagination={{
          current: page,
          pageSize,
          total: query.data?.total ?? 0,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 个 SKU`,
          onChange: (p, ps) => filters.patch({ page: p, page_size: pageSizeParam.narrow(ps) }),
        }}
      />

    </Space>
  )
}
