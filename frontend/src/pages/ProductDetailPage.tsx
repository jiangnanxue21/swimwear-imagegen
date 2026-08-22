import { useCallback, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  Alert, App, Button, Card, Descriptions, Empty, Image, Select, Skeleton,
  Space, Tag, Tooltip, Upload,
} from 'antd'
import {
  ArrowLeftOutlined, DownloadOutlined, ThunderboltOutlined, UploadOutlined, WarningOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { productsApi } from '../api/products'
import { readError } from '../api/client'
import { useWriteError } from '../hooks/useWriteError'
import { saveBlob } from '../api/batch'
import ErrorNotice from '../components/ErrorNotice'
import {
  ASSET_TYPE_LABEL, AUDIENCE_LABEL, COMPLEXITY_LABEL, GARMENT_TYPE_LABEL,
  PATTERN_TYPE_LABEL, STATUS_LABEL, type Asset, type AssetType, type Product,
} from '../api/types'
import { categoryLabel } from '../api/spus'
import ProductFormModal from '../components/ProductFormModal'
import TaskCreateModal from '../components/TaskCreateModal'
import { generationApi } from '../api/generation'
import { TASK_STATUS_LABEL, type Task } from '../api/types'
import { exportsApi } from '../api/exports'
import {
  OUTPUT_PURPOSE_LABEL, OUTPUT_PURPOSE_ORDER, type OutputPurpose,
} from '../api/types'
import { brandVars, fontScale } from '../theme'
import BrandTag from '../components/BrandTag'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(0)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/**
 * 一张源素材。
 *
 * `onExpired` 在图加载失败时被调用一次,由调用方重新取数拿新签的地址 ——
 * 源素材落在私有前缀下,它的 URL 是有 TTL 的签名地址
 * (`storage.SIGNED_URL_TTL_SECONDS`,30 分钟),而这一页不轮询。
 * 2026-08-11 评审点名的两处裂图之一,修法与素材库、图片集页同一套。
 *
 * 成品图那一块**不需要**这个:`outputs` 在 `storage.PUBLIC_PREFIXES` 里,
 * 它的地址不签名、不过期。两者混在一起处理会让人以为成品图也会裂。
 */
function AssetTile({ asset, onExpired }: { asset: Asset; onExpired?: () => void }) {
  return (
    <figure className="asset-tile" style={{ margin: 0 }}>
      <Image
        src={asset.url}
        alt={`${ASSET_TYPE_LABEL[asset.asset_type] ?? asset.asset_type} · ${asset.original_filename}`}
        preview={{ mask: '查看大图' }}
        onError={() => onExpired?.()}
      />
      <figcaption>
        <Space size={4} wrap style={{ marginBottom: 4 }}>
          <BrandTag tone="accent">{ASSET_TYPE_LABEL[asset.asset_type] ?? asset.asset_type}</BrandTag>
          {!asset.check_passed && (
            <Tooltip title={asset.check_message}>
              <Tag color="warning" icon={<WarningOutlined />}>需确认</Tag>
            </Tooltip>
          )}
        </Space>
        <div>{asset.width}×{asset.height} · {formatBytes(asset.file_size)}</div>
        <div className="mono" style={{ color: brandVars.textMuted }}>{asset.file_hash.slice(0, 12)}</div>
      </figcaption>
    </figure>
  )
}

/** 网站成品图(需求第十五章)。没出图时不显示空卡片,避免占掉半屏噪声。 */
function OutputSection({ productId }: { productId: string }) {
  const { message } = App.useApp()
  const query = useQuery({
    queryKey: ['product-export', productId],
    queryFn: () => exportsApi.product(productId),
    enabled: Boolean(productId),
  })

  const download = async (kind: 'csv' | 'json') => {
    try {
      const { blob, filename } =
        kind === 'csv'
          ? await exportsApi.csv(productId, query.data?.product?.sku)
          : await exportsApi.json(productId)
      saveBlob(blob, filename)
    } catch (err) {
      message.error(readError(err))
    }
  }

  const data = query.data
  if (query.isLoading) return <Skeleton active paragraph={{ rows: 2 }} />
  // 请求失败**不能**和「没出图」走同一条 return null:那样整张卡片会静默消失,
  // 而运营看到的是「这个商品没有成品图」—— 一个和事实相反的结论。
  // 没出图是业务状态,拉不到是故障,两者必须说不同的话。
  if (query.isError) {
    return (
      <Card size="small" title="网站成品图">
        <ErrorNotice
          error={query.error}
          title="成品图信息没有拉取成功"
          onRetry={() => void query.refetch()}
          retrying={query.isFetching}
        />
      </Card>
    )
  }
  if (!data || data.image_count === 0) return null

  return (
    <Card
      size="small"
      title={`网站成品图 · ${data.image_count} 张`}
      extra={
        <Space>
          {!data.complete && (
            <Tooltip title="五个用途没齐,网站某些位置会缺图">
              <Tag color="warning">未齐</Tag>
            </Tooltip>
          )}
          <Button
            size="small"
            icon={<DownloadOutlined />}
            onClick={() => void download('csv')}
          >
            导出 CSV
          </Button>
          <Button size="small" onClick={() => void download('json')}>
            JSON
          </Button>
        </Space>
      }
    >
      <div className="asset-grid">
        <Image.PreviewGroup>
          {OUTPUT_PURPOSE_ORDER.filter((p) => data.images[p]).map((purpose: OutputPurpose) => {
            const image = data.images[purpose]!
            return (
              <figure key={purpose} className="asset-tile" style={{ margin: 0 }}>
                <Image
                  src={image.url}
                  alt={`成品图 · ${OUTPUT_PURPOSE_LABEL[purpose].text}`}
                  preview={{ mask: '查看大图' }}
                />
                <figcaption>
                  <Tooltip title={OUTPUT_PURPOSE_LABEL[purpose].hint}>
                    <div style={{ fontWeight: 600 }}>{OUTPUT_PURPOSE_LABEL[purpose].text}</div>
                  </Tooltip>
                  <div className="mono" style={{ color: brandVars.textMuted }}>
                    {image.width}×{image.height} · {formatBytes(image.file_size)}
                  </div>
                  <a href={image.url} target="_blank" rel="noreferrer" style={{ fontSize: fontScale.meta }}>
                    打开原图
                  </a>
                </figcaption>
              </figure>
            )
          })}
        </Image.PreviewGroup>
      </div>
    </Card>
  )
}

export default function ProductDetailPage() {
  const { id = '' } = useParams()
  const navigate = useNavigate()
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [assetType, setAssetType] = useState<AssetType>('GARMENT_FRONT')
  const [editing, setEditing] = useState(false)
  const [creatingTask, setCreatingTask] = useState(false)

  const product = useQuery({
    queryKey: ['product', id],
    queryFn: () => productsApi.get(id),
    enabled: Boolean(id),
  })

  useDocumentTitle(product.data?.sku)

  const assets = useQuery({
    queryKey: ['assets', id],
    queryFn: () => productsApi.assets(id),
    enabled: Boolean(id),
  })

  /**
   * 这一页的三个写动作(BLOCK-05)。**建任务是付费的** ——
   * 后端按参数指纹幂等,所以重复提交多数会命中沿用;但"多数"不是"总是":
   * 改过任意一个参数(换 seed、换 Provider)就是一个新指纹,也就是一次
   * 真的付费调用。超时后提示"请重试"等于把这个判断交给运营去猜。
   */
  const refreshProduct = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['product', id] })
    queryClient.invalidateQueries({ queryKey: ['assets', id] })
    queryClient.invalidateQueries({ queryKey: ['tasks'] })
  }, [queryClient, id])

  const onWriteError = useWriteError(refreshProduct)

  const update = useMutation({
    mutationFn: (values: Partial<Product>) => productsApi.update(id, values),
    onSuccess: () => {
      message.success('已保存修改')
      setEditing(false)
      queryClient.invalidateQueries({ queryKey: ['product', id] })
    },
    onError: onWriteError,
  })

  const upload = useMutation({
    mutationFn: (file: File) => productsApi.uploadAsset(id, file, assetType),
    onSuccess: (result) => {
      if (result.deduplicated) {
        message.info('这张图已经在该商品下了,沿用已有素材')
      } else if (!result.asset.check_passed) {
        message.warning(result.asset.check_message ?? '素材已保存,但有需要确认的问题')
      } else {
        message.success('素材已上传')
      }
      queryClient.invalidateQueries({ queryKey: ['assets', id] })
      queryClient.invalidateQueries({ queryKey: ['product', id] })
    },
    onError: onWriteError,
  })

  const tasks = useQuery({
    queryKey: ['tasks', { product_id: id }],
    queryFn: () => generationApi.list({ product_id: id, page_size: 50 }),
    enabled: Boolean(id),
  })

  const createTask = useMutation({
    mutationFn: generationApi.create,
    onSuccess: (result) => {
      setCreatingTask(false)
      queryClient.invalidateQueries({ queryKey: ['tasks'] })
      message[result.deduplicated ? 'info' : 'success'](
        result.deduplicated ? '相同参数的任务已存在,已为你打开它' : '任务已提交,生成在后台进行',
      )
      navigate(`/tasks/${result.task.id}`)
    },
    onError: onWriteError,
  })

  if (product.isLoading) return <Skeleton active />
  if (product.isError) {
    return <Alert type="error" showIcon message="打不开这个商品" description={readError(product.error)} />
  }

  const p = product.data!
  const flagged = (assets.data ?? []).filter((a) => !a.check_passed)

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <Space>
        <Link to="/products">
          <Button size="small" icon={<ArrowLeftOutlined />}>返回商品列表</Button>
        </Link>
        <Tag color={STATUS_LABEL[p.status]?.color}>{STATUS_LABEL[p.status]?.text ?? p.status}</Tag>
        <Button
          size="small"
          type="primary"
          icon={<ThunderboltOutlined />}
          disabled={!assets.data?.length}
          onClick={() => setCreatingTask(true)}
        >
          创建生成任务
        </Button>
      </Space>

      <Card
        size="small"
        title={<span className="mono">{p.sku}</span>}
        extra={<Button size="small" onClick={() => setEditing(true)}>编辑属性</Button>}
      >
        <Descriptions size="small" column={2} bordered>
          <Descriptions.Item label="商品名称" span={2}>{p.name}</Descriptions.Item>
          <Descriptions.Item label="SPU">{p.spu}</Descriptions.Item>
          <Descriptions.Item label="品类">{categoryLabel(p.category)}</Descriptions.Item>
          <Descriptions.Item label="受众">{p.audience ? AUDIENCE_LABEL[p.audience] : '待确认'}</Descriptions.Item>
          <Descriptions.Item label="服装类型">
            {GARMENT_TYPE_LABEL[p.garment_type] ?? p.garment_type}
          </Descriptions.Item>
          <Descriptions.Item label="图案类型">
            {PATTERN_TYPE_LABEL[p.pattern_type] ?? p.pattern_type}
          </Descriptions.Item>
          <Descriptions.Item label="主颜色">{p.primary_color || '—'}</Descriptions.Item>
          <Descriptions.Item label="辅助颜色">
            {p.secondary_colors.length ? p.secondary_colors.join(' / ') : '—'}
          </Descriptions.Item>
          <Descriptions.Item label="肩带">{p.strap_type}</Descriptions.Item>
          <Descriptions.Item label="领口">{p.neckline_type}</Descriptions.Item>
          <Descriptions.Item label="背部样式">{p.back_style}</Descriptions.Item>
          <Descriptions.Item label="复杂度">
            {COMPLEXITY_LABEL[p.complexity] ?? p.complexity}
          </Descriptions.Item>
          <Descriptions.Item label="材质" span={2}>{p.material || '—'}</Descriptions.Item>
          {p.notes && <Descriptions.Item label="备注" span={2}>{p.notes}</Descriptions.Item>}
        </Descriptions>
      </Card>

      <Card
        size="small"
        title={`原始素材 · ${assets.data?.length ?? 0} 张`}
        extra={
          <Space>
            <Select<AssetType>
              size="small"
              value={assetType}
              style={{ width: 130 }}
              onChange={setAssetType}
              options={Object.entries(ASSET_TYPE_LABEL).map(([value, label]) => ({ value: value as AssetType, label }))}
            />
            <Upload
              accept="image/jpeg,image/png,image/webp"
              showUploadList={false}
              beforeUpload={(file) => {
                upload.mutate(file)
                return false
              }}
            >
              <Button size="small" type="primary" icon={<UploadOutlined />} loading={upload.isPending}>
                上传素材
              </Button>
            </Upload>
          </Space>
        }
      >
        {flagged.length > 0 && (
          <Alert
            type="warning"
            showIcon
            style={{ marginBottom: 12 }}
            message={`${flagged.length} 张素材有需要确认的问题`}
            description="这些图仍会入库,但可能影响生成效果。把鼠标移到「需确认」标签上可以看到具体原因。"
          />
        )}
        {assets.isLoading ? (
          <Skeleton active />
        ) : assets.isError ? (
          // 「拉不到」不能显示成「还没有素材」:运营会照着这句话去重传一遍
          // 已经存在的图,而真正该做的是重试或找管理员。
          <ErrorNotice
            error={assets.error}
            title="素材列表没有拉取成功"
            onRetry={() => void assets.refetch()}
            retrying={assets.isFetching}
          />
        ) : assets.data?.length ? (
          <div className="asset-grid">
            {assets.data.map((a) => (
              <AssetTile
                key={a.id}
                asset={a}
                // 一屏十几张图过期时只重取一次:`isFetching` 那道闸挡住其余的
                onExpired={() => {
                  if (!assets.isFetching) void assets.refetch()
                }}
              />
            ))}
          </div>
        ) : (
          <Empty description="还没有素材。先选好素材类型,再上传商品图。" />
        )}
      </Card>

      <OutputSection productId={id} />

      <Card size="small" title={`生成历史 · ${tasks.data?.total ?? 0} 个任务`}>
        {tasks.isLoading ? (
          // 原来连加载态都没有:请求在飞的时候页面就已经在说「还没有生成任务」,
          // 而下一秒又冒出一列任务。这一跳会被读成「刚才那次提交没生效」
          <Skeleton active paragraph={{ rows: 2 }} />
        ) : tasks.isError ? (
          <ErrorNotice
            error={tasks.error}
            title="生成历史没有拉取成功"
            onRetry={() => void tasks.refetch()}
            retrying={tasks.isFetching}
          />
        ) : tasks.data?.items.length ? (
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            {tasks.data.items.map((t: Task) => (
              <Space key={t.id} size={8}>
                <Link to={`/tasks/${t.id}`} className="mono">{t.id.slice(0, 8)}</Link>
                <Tag color={TASK_STATUS_LABEL[t.status]?.color}>
                  {TASK_STATUS_LABEL[t.status]?.text ?? t.status}
                </Tag>
                <span style={{ color: brandVars.slate }}>
                  {t.provider} · 第 {t.current_round}/{t.max_rounds} 轮 · 每轮 {t.candidate_count} 张
                </span>
              </Space>
            ))}
          </Space>
        ) : (
          <Empty
            image={Empty.PRESENTED_IMAGE_SIMPLE}
            description={assets.data?.length ? '还没有生成任务,点上方按钮创建一个。' : '先上传商品图,才能创建生成任务。'}
          />
        )}
      </Card>

      <TaskCreateModal
        open={creatingTask}
        productId={id}
        confirmLoading={createTask.isPending}
        onCancel={() => setCreatingTask(false)}
        onSubmit={(values) => createTask.mutate(values)}
      />

      <ProductFormModal
        open={editing}
        product={p}
        confirmLoading={update.isPending}
        onCancel={() => setEditing(false)}
        onSubmit={(values) => update.mutate(values)}
      />
    </Space>
  )
}
