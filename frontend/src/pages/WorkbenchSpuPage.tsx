/**
 * SPU-SKU 聚合(FE-306)。
 *
 * 验收结果:**公共属性与变体属性不混淆**。界面上这句话有三块地方:
 *
 *     变体维度   颜色 / 尺码,只在展开的 SKU 行上出现,不显示成 SPU 的属性
 *     公共属性   全体 SKU 取值一致的字段,一行一个,挂在 SPU 上
 *     不一致     本该一致却不一致的字段,**红底并点名哪几个 SKU 持哪个值**
 *
 * 第三块是这一页真正的价值。属性识别是逐图跑的,同一款泳衣的 4 个 SKU 各有各的图,
 * 于是常常识别出 3 种面料。如果只是"取第一个 SKU 的值挂到 SPU 上",
 * 导出的 SPU 级字段就是随机的,而没有人会发现。
 *
 * SPU 完成度取各 SKU 的**最小值**(后端算的)。上架文件是整个 SPU 一起导的,
 * 平均值会给出"4 个 SKU 三个做完了 = 75%"这种读数,而这个 SPU 一件也上不了架。
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, Button, Card, Empty, Input, Progress, Space, Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined, WarningOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import ErrorNotice from '../components/ErrorNotice'
import { batchApi, type SpuGroup, type SpuSkuRow } from '../api/batch'
import { brandVars, fontScale } from '../theme'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

export default function WorkbenchSpuPage() {
  useDocumentTitle('SPU 聚合')
  const navigate = useNavigate()
  const [search, setSearch] = useState('')
  /** 输入框里的字。与已生效的 `search` 分开:敲字不该每个字符打一次接口,
      但两者必须能被同一个动作一起清掉(走查 P0-3:非受控输入框会说谎) */
  const [draft, setDraft] = useState('')
  const [page, setPage] = useState(1)
  const [pageSize, setPageSize] = useState(50)

  /** 换搜索词必须回到第 1 页。留在第 3 页的话,新词只有 12 个 SPU 时
      页面是空的,而空态写着"还没有商品" —— 又一个不报错的错答案 */
  const applySearch = (value: string) => {
    setSearch(value)
    setPage(1)
  }

  const query = useQuery({
    queryKey: ['spus', search, page, pageSize],
    queryFn: () =>
      batchApi.spus({ search: search || undefined, page, page_size: pageSize }),
  })

  const columns: ColumnsType<SpuGroup> = [
    {
      title: 'SPU',
      key: 'spu',
      width: 220,
      render: (_, row) => (
        <div>
          <span className="mono">{row.spu}</span>
          <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>{row.name}</div>
        </div>
      ),
    },
    {
      title: 'SKU 数',
      dataIndex: 'sku_count',
      key: 'sku_count',
      width: 80,
      align: 'center',
      sorter: (a, b) => a.sku_count - b.sku_count,
    },
    {
      title: (
        <Tooltip title="排序只作用于当前页:翻页在服务端做,浏览器手上只有这一页">
          <span>完成度 *</span>
        </Tooltip>
      ),
      key: 'completion',
      width: 130,
      // A-04:这里原来写的是"这一页一次拉全(pagination={false}),客户端排序
      // 对的是全量" —— 而请求只有 100 条,注释在撒谎。翻页改到服务端之后
      // 这句话更不成立了,所以列头上打了 * 并给出提示:**排的是当前页**。
      //
      // 真要"全量最低的那几个",得让排序键下推到 SQL(后端加 sort 参数)。
      // 那是另一件事,不在这一批里 —— 但界面不能继续声称它已经做到了。
      defaultSortOrder: 'ascend',
      sorter: (a, b) => a.completion - b.completion,
      render: (_, row) => (
        <Tooltip title="取各 SKU 的最小值:上架文件是整个 SPU 一起导的,一件没做完这个 SPU 就导不了">
          <Progress
            percent={row.completion}
            size="small"
            strokeColor={row.blocking_count ? brandVars.danger : brandVars.marine}
          />
        </Tooltip>
      ),
    },
    {
      title: '变体维度',
      key: 'variants',
      width: 220,
      render: (_, row) => (
        <Space size={4} wrap>
          {Object.entries(row.variants).map(([dim, values]) => (
            <Tag key={dim}>
              {dim}:{values.join(' / ')}
            </Tag>
          ))}
          {!Object.keys(row.variants).length && <span style={{ color: brandVars.textFaint }}>—</span>}
        </Space>
      ),
    },
    {
      title: '公共属性',
      key: 'common',
      render: (_, row) => (
        <Space size={4} wrap>
          {row.common.map((c) => (
            <Tag key={c.field_name} color="default">
              {c.field_name}={c.value}
            </Tag>
          ))}
          {!row.common.length && <span style={{ color: brandVars.textFaint }}>还没有已确认的公共属性</span>}
        </Space>
      ),
    },
    {
      title: '不一致',
      key: 'inconsistent',
      width: 260,
      render: (_, row) =>
        row.inconsistent.length ? (
          <Space direction="vertical" size={2}>
            {row.inconsistent.map((item) => (
              <Tooltip
                key={item.field_name}
                title="同一 SPU 下这个字段本该一致。属性识别是逐图跑的,各 SKU 的图不同就可能得出不同结论——需要人工统一"
              >
                <Tag color="error" icon={<WarningOutlined />}>
                  {item.field_name}:
                  {item.values
                    .map((v) => `${v.value}(${v.skus.length} 件)`)
                    .join(' / ')}
                </Tag>
              </Tooltip>
            ))}
          </Space>
        ) : (
          <Tag color="success">一致</Tag>
        ),
    },
    {
      title: '阻断',
      key: 'blocking',
      width: 70,
      align: 'center',
      sorter: (a, b) => a.blocking_count - b.blocking_count,
      render: (_, row) => (
        <Tag color={row.blocking_count ? 'error' : 'default'}>{row.blocking_count}</Tag>
      ),
    },
  ]

  const skuColumns: ColumnsType<SpuSkuRow> = [
    {
      title: 'SKU',
      key: 'sku',
      width: 220,
      render: (_, row) => (
        <a className="mono" onClick={() => navigate(`/workbench/${row.product_id}`)}>
          {row.sku}
        </a>
      ),
    },
    {
      title: '颜色',
      key: 'color',
      width: 110,
      render: (_, row) => row.color || <span style={{ color: brandVars.textFaint }}>—</span>,
    },
    {
      title: '尺码',
      key: 'size',
      width: 90,
      render: (_, row) => row.size || <span style={{ color: brandVars.textFaint }}>—</span>,
    },
    {
      title: '完成度',
      key: 'completion',
      width: 120,
      render: (_, row) => <Progress percent={row.completion} size="small" />,
    },
    {
      title: '阻断',
      key: 'blocking',
      width: 70,
      align: 'center',
      render: (_, row) => (
        <Tag color={row.blocking_count ? 'error' : 'default'}>{row.blocking_count}</Tag>
      ),
    },
    // 走查 P0-1:这张表 1400px,需要 1630px 视口 —— 1512 的 MacBook Pro
    // 都还缺 118px,而缺掉的是「下一步」
    { title: '下一步', dataIndex: 'next_action_label', key: 'next', width: 180, fixed: 'right' },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader title="SPU 聚合" subtitle="按款式看:同一 SPU 下各 SKU 的进度与属性是否一致" />

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="搜索 SPU / 名称"
            style={{ width: 240 }}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onSearch={applySearch}
          />
          <Button
            icon={<ReloadOutlined />}
            onClick={() => query.refetch()}
            loading={query.isFetching}
          >
            刷新
          </Button>
          {query.data && query.data.inconsistent_spus > 0 && (
            <Tooltip title="全量口径,不随翻页变化">
              <Tag color="error">
                {query.data.inconsistent_spus} 个 SPU 有属性不一致
              </Tag>
            </Tooltip>
          )}
        </Space>
      </Card>

      {query.isError && (
        <ErrorNotice
          title="拉不到 SPU 聚合"
          error={query.error}
          onRetry={() => query.refetch()}
        />
      )}

      <Alert
        type="info"
        showIcon
        message="图片集、文案、上架草稿按 SPU 共享"
        description="同一 SPU 下的多个 SKU 用同一份图片集与文案,所以批量动作对同一 SPU 只执行一次;属性识别按 SKU 独立进行,因为素材挂在 SKU 上。一致性判定只看已确认的属性值。"
      />

      <Table<SpuGroup>
        rowKey="spu"
        size="small"
        bordered
        scroll={{ x: 1400 }}
        columns={columns}
        dataSource={query.isError ? [] : query.data?.items ?? []}
        loading={query.isFetching}
        // A-01:原来是 `pagination={false}` 配后端硬编码 `limit: 100`,
        // 于是第 101 个 SPU 起在界面上不存在,而页面显得完全正常。
        // `total` 用服务端的全量数 —— 用 `items.length` 的话页码条只会有一页
        pagination={{
          current: query.data?.page ?? page,
          pageSize: query.data?.page_size ?? pageSize,
          total: query.data?.total ?? 0,
          showSizeChanger: true,
          pageSizeOptions: [20, 50, 100, 200],
          showTotal: (total) => `共 ${total} 个 SPU`,
          onChange: (nextPage, nextSize) => {
            setPage(nextPage)
            setPageSize(nextSize)
          },
        }}
        expandable={{
          expandedRowRender: (row) => (
            <Table<SpuSkuRow>
              rowKey="product_id"
              size="small"
              bordered
              columns={skuColumns}
              dataSource={row.skus}
              pagination={false}
            />
          ),
        }}
        locale={{
          emptyText: (
            <Empty
              description={
                search ? `没有匹配「${search}」的 SPU` : '还没有商品。先在导入页导入一批。'
              }
            />
          ),
        }}
      />
    </Space>
  )
}
