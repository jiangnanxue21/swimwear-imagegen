/**
 * 素材标签(FE-101 的详情面 + §3.2.2 第 1 条「补充素材」的落点)。
 *
 * 隔离态素材**显示但不可用**:藏起来的话,运营看到「1 条素材被隔离」却
 * 在页面上找不到是哪一条,只能去素材库翻。放行也在这里做 —— 那是
 * 「处理隔离素材」这个推荐动作唯一能被执行完的地方。
 */
import { useCallback, useState } from 'react'
import {
  App, Button, Card, Empty, Image, Input, Modal, Select, Skeleton, Space, Tag, Tooltip, Upload,
} from 'antd'
import { UploadOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { mediaApi, type MediaAsset, type MediaRole } from '../../api/media'
import { productsApi } from '../../api/products'
import { useWriteError } from '../../hooks/useWriteError'
import ErrorNotice from '../ErrorNotice'
import { ASSET_TYPE_LABEL, type AssetType } from '../../api/types'
import { IssueList } from './FlowBits'
import type { FlowStepResult } from '../../api/workbench'
import { brandVars } from '../../theme'

/** 与后端 MediaRole 同集合。缺一个角色这里就少一个选项,不影响判定,只影响能不能改 */
export const MEDIA_ROLE_LABEL: Record<MediaRole, string> = {
  PRODUCT_FRONT: '正面图',
  PRODUCT_BACK: '背面图',
  MODEL_FRONT: '模特正面',
  MODEL_BACK: '模特背面',
  DETAIL: '细节图',
  SIZE_CHART: '尺码表',
  FLAT_LAY: '平铺图',
  PACKAGING: '包装图',
  OTHER: '其它',
}

const STATUS_TAG: Record<string, { text: string; color: string }> = {
  PENDING: { text: '待检查', color: 'processing' },
  READY: { text: '可用', color: 'success' },
  QUARANTINED: { text: '已隔离', color: 'error' },
  FAILED: { text: '失败', color: 'error' },
  DELETED: { text: '已删除', color: 'default' },
}

function AssetCard({
  asset,
  onQuarantine,
  onRelease,
  onRole,
  busy,
}: {
  asset: MediaAsset
  onQuarantine: (asset: MediaAsset) => void
  onRelease: (asset: MediaAsset) => void
  onRole: (asset: MediaAsset, role: MediaRole) => void
  busy: boolean
}) {
  const status = STATUS_TAG[asset.status] ?? { text: asset.status, color: 'default' }
  const quarantined = asset.status === 'QUARANTINED'
  return (
    <figure
      className="asset-tile"
      style={{ margin: 0, opacity: quarantined ? 0.6 : 1, borderColor: quarantined ? brandVars.danger : undefined }}
    >
      <Image src={asset.url} alt="" height={140} style={{ objectFit: 'contain' }} />
      <figcaption>
        <Space size={4} wrap style={{ marginBottom: 4 }}>
          <Tag color={status.color}>{status.text}</Tag>
          {asset.role_source === 'MODEL' && (
            <Tooltip title="角色是模型猜的,没经人确认前不能占主图位。确认后就改成人工指定">
              <Tag color="warning">角色待复核</Tag>
            </Tooltip>
          )}
        </Space>
        <Select<MediaRole>
          size="small"
          style={{ width: '100%', marginBottom: 4 }}
          value={asset.role ?? undefined}
          placeholder="未指定角色"
          disabled={busy}
          onChange={(role) => onRole(asset, role)}
          options={Object.entries(MEDIA_ROLE_LABEL).map(([value, label]) => ({
            value: value as MediaRole,
            label,
          }))}
        />
        <div>
          {asset.width}×{asset.height}
        </div>
        {quarantined ? (
          <>
            <div style={{ color: brandVars.danger }}>{asset.quarantine_reason ?? '未写原因'}</div>
            <Button size="small" type="link" style={{ padding: 0 }} disabled={busy} onClick={() => onRelease(asset)}>
              放行
            </Button>
          </>
        ) : (
          <Button size="small" type="link" danger style={{ padding: 0 }} disabled={busy} onClick={() => onQuarantine(asset)}>
            隔离
          </Button>
        )}
      </figcaption>
    </figure>
  )
}

export default function MaterialTab({
  productId,
  step,
}: {
  productId: string
  step: FlowStepResult | undefined
}) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [assetType, setAssetType] = useState<AssetType>('GARMENT_FRONT')
  const [quarantining, setQuarantining] = useState<MediaAsset | null>(null)
  const [reason, setReason] = useState('')

  const assets = useQuery({
    queryKey: ['workbench-media', productId],
    queryFn: () => mediaApi.list({ product_id: productId, page_size: 200 }),
    enabled: Boolean(productId),
  })

  /** 素材状态一变,流程判定跟着变(§4.5:素材被替换或隔离 -> 图片集与草稿过期) */
  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['workbench-media', productId] })
    queryClient.invalidateQueries({ queryKey: ['workbench-flow', productId] })
    queryClient.invalidateQueries({ queryKey: ['workbench'] })
  }, [queryClient, productId])

  /**
   * 四个动作都是写请求(BLOCK-05)。上传尤其不能说"请重试" ——
   * 后端按内容指纹去重,重传同一张会走沿用分支(不会产生两份文件),
   * 但**换了一张图重传**就会真的多一张素材,而运营在超时之后并不知道
   * 上一次到底进没进去。先刷新素材列表,它是这件事唯一的答案。
   */
  const onWriteError = useWriteError(refresh)

  const setRole = useMutation({
    mutationFn: ({ id, role }: { id: string; role: MediaRole }) => mediaApi.setRole(id, role),
    onSuccess: () => {
      message.success('已改角色')
      refresh()
    },
    onError: onWriteError,
  })

  const quarantine = useMutation({
    mutationFn: ({ id, why }: { id: string; why: string }) => mediaApi.quarantine(id, why),
    onSuccess: () => {
      message.success('已隔离。引用它的已批准图片集会被降级为待复核')
      setQuarantining(null)
      setReason('')
      refresh()
    },
    onError: onWriteError,
  })

  const release = useMutation({
    mutationFn: (id: string) => mediaApi.release(id),
    onSuccess: () => {
      message.success('已放行')
      refresh()
    },
    onError: onWriteError,
  })

  const upload = useMutation({
    mutationFn: (file: File) => productsApi.uploadAsset(productId, file, assetType),
    onSuccess: (result) => {
      if (result.deduplicated) message.info('这张图已经在该商品下了,沿用已有素材')
      else message.success('素材已上传')
      refresh()
    },
    onError: onWriteError,
  })

  const busy = setRole.isPending || quarantine.isPending || release.isPending

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      {step && step.issues.length > 0 && (
        <Card size="small" title="这一步的问题">
          <IssueList issues={step.issues} />
        </Card>
      )}

      <Card
        size="small"
        title={`素材 · ${assets.data?.total ?? 0} 条`}
        extra={
          <Space>
            <Select<AssetType>
              size="small"
              value={assetType}
              style={{ width: 130 }}
              onChange={setAssetType}
              options={Object.entries(ASSET_TYPE_LABEL).map(([value, label]) => ({
                value: value as AssetType,
                label,
              }))}
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
        {assets.isLoading ? (
          <Skeleton active />
        ) : assets.isError ? (
          <ErrorNotice
            title="拉不到素材"
            error={assets.error}
            onRetry={() => assets.refetch()}
          />
        ) : assets.data?.items.length ? (
          <div className="asset-grid">
            <Image.PreviewGroup>
              {assets.data.items.map((a) => (
                <AssetCard
                  key={a.id}
                  asset={a}
                  busy={busy}
                  onRole={(asset, role) => setRole.mutate({ id: asset.id, role })}
                  onQuarantine={(asset) => setQuarantining(asset)}
                  onRelease={(asset) => release.mutate(asset.id)}
                />
              ))}
            </Image.PreviewGroup>
          </div>
        ) : (
          <Empty description="还没有素材。选好类型后上传商品图,至少要有一张正面图才能往下走。" />
        )}
      </Card>

      <Modal
        open={Boolean(quarantining)}
        title="隔离这条素材"
        okText="隔离"
        okButtonProps={{ danger: true, disabled: !reason.trim() }}
        confirmLoading={quarantine.isPending}
        onCancel={() => {
          setQuarantining(null)
          setReason('')
        }}
        onOk={() => quarantining && quarantine.mutate({ id: quarantining.id, why: reason.trim() })}
      >
        <p style={{ color: brandVars.slate }}>
          原因会留在素材上,也会出现在图片集的降级提示里。写清楚是为了下一个人不用重新判断一遍。
        </p>
        <Input.TextArea
          rows={3}
          value={reason}
          placeholder="例如:模特手挡住了肩带,看不出款式"
          onChange={(e) => setReason(e.target.value)}
        />
      </Modal>
    </Space>
  )
}
