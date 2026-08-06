import { useCallback, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  App, Button, Card, Empty, Input, Select, Space, Table, Tag, Upload,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ImportOutlined, PlusOutlined, ReloadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { productsApi } from '../api/products'
import { readError } from '../api/client'
import { useWriteError } from '../hooks/useWriteError'
import {
  AUDIENCE_LABEL, GARMENT_TYPES, STATUS_LABEL, type Product, type ProductStatus,
} from '../api/types'
import ProductFormModal from '../components/ProductFormModal'
import { AudienceTag } from '../components/AudienceBadge'
import { useServerSort } from '../hooks/useServerSort'
import { brandVars } from '../theme'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

export default function ProductListPage() {
  useDocumentTitle('商品')
  const navigate = useNavigate()
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()

  const [search, setSearch] = useState('')
  /** 输入框里的字。与已生效的 `search` 分开:敲字不该每个字符打一次接口,
      但两者必须能被同一个动作一起清掉(走查 P0-3:非受控输入框会说谎) */
  const [draft, setDraft] = useState('')
  const [status, setStatus] = useState<string | undefined>()
  const [garmentType, setGarmentType] = useState<string | undefined>()
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(20)
  const [formOpen, setFormOpen] = useState(false)

  // 服务端排序(不是客户端的 sorter 函数):这张表一页只有 20 行,
  // 在前端排等于对这 20 行排,而运营以为排的是全部命中
  const sort = useServerSort({ sort: 'created_at', order: 'desc' }, () => setPage(1))

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

  /**
   * 建商品与导入 CSV 都是写请求(BLOCK-05)。
   *
   * 导入尤其要紧:一次导入会建几十上百行。超时后重来一次,已经建成的那些
   * 会撞 SKU 唯一约束,于是一次**部分成功**的导入被报成一屏错误,
   * 而运营分不出"哪些是这次撞的、哪些是本来就有的"。先刷新列表看总数。
   */
  const refreshProducts = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['products'] })
  }, [queryClient])

  const onWriteError = useWriteError(refreshProducts)

  const create = useMutation({
    mutationFn: productsApi.create,
    onSuccess: (product) => {
      message.success(`已创建 ${product.sku}`)
      setFormOpen(false)
      queryClient.invalidateQueries({ queryKey: ['products'] })
    },
    onError: onWriteError,
  })

  const importCsv = useMutation({
    mutationFn: productsApi.importCsv,
    onSuccess: (result) => {
      queryClient.invalidateQueries({ queryKey: ['products'] })
      if (result.failed > 0) {
        modal.warning({
          title: `导入完成,${result.failed} 行未通过`,
          width: 560,
          content: (
            <div style={{ maxHeight: 320, overflow: 'auto' }}>
              <p>
                新增 {result.created} 个,已存在跳过 {result.skipped_existing} 个。
              </p>
              <ul style={{ paddingLeft: 18 }}>
                {result.errors.map((e, i) => (
                  <li key={i}>
                    第 {e.row_number} 行 · {e.field}:{e.message}
                  </li>
                ))}
              </ul>
            </div>
          ),
        })
      } else {
        message.success(
          `新增 ${result.created} 个,已存在跳过 ${result.skipped_existing} 个`,
        )
      }
    },
    onError: onWriteError,
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
      <PageHeader title="商品" subtitle="商品主数据:新建、批量导入、改基础信息" />

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="搜索 SKU / SPU / 名称"
            style={{ width: 260 }}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onSearch={(v) => {
              setSearch(v)
              setPage(1)
            }}
          />
          <Select
            allowClear
            placeholder="状态"
            style={{ width: 140 }}
            value={status}
            onChange={(v) => {
              setStatus(v)
              setPage(1)
            }}
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
            onChange={(v) => {
              setGarmentType(v)
              setPage(1)
            }}
            options={GARMENT_TYPES.map((v) => ({ value: v, label: v }))}
          />
          <Button icon={<ReloadOutlined />} onClick={() => query.refetch()}>
            刷新
          </Button>
          <Upload
            accept=".csv"
            showUploadList={false}
            beforeUpload={(file) => {
              importCsv.mutate(file)
              return false
            }}
          >
            <Button icon={<ImportOutlined />} loading={importCsv.isPending}>
              导入 CSV
            </Button>
          </Upload>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setFormOpen(true)}>
            新增商品
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
                  : '还没有商品。导入 sample-data/products.csv 就能看到 10 个示例款式。'
              }
            />
          ),
        }}
        pagination={{
          current: page,
          pageSize,
          total: query.data?.total ?? 0,
          showSizeChanger: true,
          showTotal: (t) => `共 ${t} 个商品`,
          onChange: (p, ps) => {
            setPage(p)
            setPageSize(ps)
          },
        }}
      />

      <ProductFormModal
        open={formOpen}
        confirmLoading={create.isPending}
        onCancel={() => setFormOpen(false)}
        onSubmit={(values) => create.mutate(values)}
      />
    </Space>
  )
}
