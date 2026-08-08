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
import { colorVariantLabel, type ColorVariant } from '../../api/spus'
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

/** 「通用图」这一组的键。空串不做键 —— 它和"没选"在 `Select` 里长得一样 */
const GENERIC = '__generic__'

/**
 * 素材按颜色分组。
 *
 * ## 读 `color_variant_id`,不读 `variant_hint`
 *
 * 第四次拒绝同一件事(前三次在 14-9 / 14-10 与 `run_state.scope_of`)。
 * hint 是模型猜的;拿它分组的表现是界面按 A 色显示的一张图,在完整度门禁
 * 和作用域指纹眼里属于共享作用域 —— 同一张图三处两个答案,而都不报错。
 *
 * ## 归属到已删除/未声明颜色的图不许消失
 *
 * 分组的键取「素材实际带的归属」与「SPU 声明的颜色」的并集,和后端
 * `_material_facts` 那一处同向。只按声明清单分组的话,一张挂在已下架颜色
 * 上的存量图会从页面上凭空消失 —— 而那种行恰恰需要有人处理。
 */
export function groupByColour(
  assets: MediaAsset[],
  variants: ColorVariant[],
): { key: string; label: string; assets: MediaAsset[] }[] {
  const label = new Map(variants.map((v) => [v.id, colorVariantLabel(v)]))
  const buckets = new Map<string, MediaAsset[]>()
  for (const v of variants) buckets.set(v.id, [])
  for (const a of assets) {
    const key = a.color_variant_id ?? GENERIC
    if (!buckets.has(key)) buckets.set(key, [])
    buckets.get(key)!.push(a)
  }
  const colours = [...buckets.entries()]
    .filter(([key]) => key !== GENERIC)
    .map(([key, rows]) => ({ key, label: label.get(key) ?? key, assets: rows }))
  const generic = buckets.get(GENERIC) ?? []
  // 通用图排最后:它不属于任何颜色,排在前面会让人以为它是第一个颜色的图
  return generic.length || colours.length
    ? [...colours, { key: GENERIC, label: '通用图(不属于某个颜色)', assets: generic }]
    : []
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
  /**
   * 这次上传归属到哪个颜色。`null` = 通用图。
   *
   * **默认 `null` 而不是"第一个颜色"。** 预选一个的表现是运营上传一批
   * 通用图,它们全部静默挂到黑色上 —— 于是黑色的完整度门禁被一批不是它的
   * 图满足,而其余颜色照旧缺图。这个方向和后端 `Form(None)` 一致:
   * 不选就是通用图,与本参数存在之前逐字相同。
   */
  const [uploadColour, setUploadColour] = useState<string | null>(null)

  /**
   * 能选的颜色。**取数与上传接口的白名单同源**(后端 `colours_for`)。
   *
   * 老建档路径的商品拿到空表(它没有 SPU),那时整块颜色 UI 不出现 ——
   * 而不是显示一个空下拉框:空下拉框看起来像"颜色还没加载出来"。
   */
  const colours = useQuery({
    queryKey: ['product-colours', productId],
    queryFn: () => productsApi.colorVariants(productId),
    enabled: Boolean(productId),
  })
  const variants = colours.data ?? []

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
    /**
     * 颜色跟着这次上传一起发(§4.8)。
     *
     * 在此之前这里只发 `file` + `asset_type`,于是界面传出来的素材
     * `color_variant_id` **恒为 null** —— §5.3 的颜色作用域指纹只有共享
     * 那一档能触发,AC-21(传 A 色图不 stale B 色事实)在界面上没有对象可验。
     */
    mutationFn: (file: File) =>
      productsApi.uploadAsset(productId, file, assetType, uploadColour),
    onSuccess: (result) => {
      if (result.deduplicated) message.info('这张图已经在该商品下了,沿用已有素材')
      else message.success('素材已上传')
      // 颜色清单不用刷,但门禁要:补一张 A 色正面图会解开 A 色那条阻断
      refresh()
    },
    onError: onWriteError,
  })

  const groups = groupByColour(assets.data?.items ?? [], variants)

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
            {/*
              * 颜色选择器只在**这个商品真的有颜色可选**时出现。
              *
              * 老建档路径的商品(没有 SPU)拿到空表,那时整块不渲染 ——
              * 显示一个空下拉框的话它看起来像"还没加载出来",运营会等;
              * 而它其实永远不会有内容。后端那一侧同向:`colours_for`
              * 对这种商品返回空表而不是 409。
              */}
            {/*
              * 颜色清单拉不到时**必须说出来**(FE-GLOBAL-03)。
              *
              * 空表在这里是一句业务结论:「这件商品没有颜色可选」(老建档
              * 路径的商品)。而它和「这条查询挂了」在界面上长得一模一样,
              * 两者的下一步却相反:前者是照旧传通用图,后者是重拉/找管理员。
              *
              * 静默吞掉的后果比少一个下拉框重:运营会把一批本该归属到颜色的
              * 图当成通用图传上去,而通用图不进任何颜色作用域 —— 那几个颜色
              * 的完整度门禁一条都解不开,且没有任何提示指向"颜色没加载出来"。
              */}
            {colours.isError && (
              <Tooltip title="颜色清单没拉到,现在传的图只能是通用图。点一下重试">
                <Button size="small" danger onClick={() => colours.refetch()}>
                  颜色没拉到
                </Button>
              </Tooltip>
            )}
            {variants.length > 0 && (
              <Tooltip title="这张图是哪个颜色的样品。不选 = 通用图,只进 SPU 作用域,证明不了某个颜色有正面照">
                <Select<string>
                  size="small"
                  value={uploadColour ?? GENERIC}
                  style={{ width: 150 }}
                  onChange={(value) => setUploadColour(value === GENERIC ? null : value)}
                  placeholder="颜色"
                  options={[
                    { value: GENERIC, label: '通用图(不选颜色)' },
                    ...variants.map((v) => ({
                      value: v.id,
                      label: colorVariantLabel(v),
                    })),
                  ]}
                />
              </Tooltip>
            )}
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
          <Image.PreviewGroup>
            {/*
              * 有颜色就按颜色分组,没有就是一张平铺的网格(与分组之前逐字相同)。
              *
              * 分组不只是排版:「哪个颜色还差哪张图」是 §6.2 颜色作用域门禁
              * 唯一能被人看见的地方。一张平铺的网格里,一件三色商品的
              * 二十张图看不出 B 色一张正面照都没有 —— 而那正是门禁在拦的东西。
              */}
            {groups.length > 1 ? (
              groups.map((g) => (
                <section key={g.key} style={{ marginBottom: 12 }}>
                  <div style={{ marginBottom: 6 }}>
                    <Space size={6}>
                      <b>{g.label}</b>
                      <Tag>{g.assets.length} 张</Tag>
                      {/*
                        * 缺角色提示直接读**后端已经算好的那条 issue**(硬规则 4)。
                        * 在前端重算一遍 `missing_role_groups` 的话,
                        * 门禁说「缺正面图或平铺图」、这里说「缺正面图」,
                        * 而运营为了消掉后一句会去把平铺图改标成正面图。
                        */}
                      {(step?.issues ?? [])
                        .filter(
                          (i) =>
                            i.code === 'MATERIAL_VARIANT_MISSING_REQUIRED_ROLE' &&
                            i.ref === g.key,
                        )
                        .map((i) => (
                          <Tag key={i.message} color="error">
                            {i.message}
                          </Tag>
                        ))}
                    </Space>
                  </div>
                  {g.assets.length ? (
                    <div className="asset-grid">
                      {g.assets.map((a) => (
                        <AssetCard
                          key={a.id}
                          asset={a}
                          busy={busy}
                          onRole={(asset, role) => setRole.mutate({ id: asset.id, role })}
                          onQuarantine={(asset) => setQuarantining(asset)}
                          onRelease={(asset) => release.mutate(asset.id)}
                        />
                      ))}
                    </div>
                  ) : (
                    <Empty
                      image={Empty.PRESENTED_IMAGE_SIMPLE}
                      description={`${g.label}还没有任何素材`}
                    />
                  )}
                </section>
              ))
            ) : (
              <div className="asset-grid">
                {(assets.data?.items ?? []).map((a) => (
                  <AssetCard
                    key={a.id}
                    asset={a}
                    busy={busy}
                    onRole={(asset, role) => setRole.mutate({ id: asset.id, role })}
                    onQuarantine={(asset) => setQuarantining(asset)}
                    onRelease={(asset) => release.mutate(asset.id)}
                  />
                ))}
              </div>
            )}
          </Image.PreviewGroup>
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
