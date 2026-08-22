import { useCallback, useState } from 'react'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert, Button, Card, Empty, Image, Input, Modal, Pagination, Select,
  App, Space, Skeleton, Tag, Tooltip, Typography,
} from 'antd'
import { readError } from '../api/client'
import { useWriteError } from '../hooks/useWriteError'
import {
  mediaApi,
  MEDIA_STATUS_LABEL,
  type MediaAsset,
  type MediaQuery,
  type MediaRole,
} from '../api/media'
import { useIdentity } from '../hooks/useIdentity'
import { brandVars, fontScale, gridMin, imageTile } from '../theme'
import BrandTag from '../components/BrandTag'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'

/**
 * 素材库(M1)。
 *
 * 这一页存在的理由不是「多一个能看图的地方」,而是:统一素材层上线之后,
 * **来源与角色是两个独立维度**,需要一个地方能同时按两者筛。以前商品图、
 * 生成候选、成品图分散在三个页面,「这个 SPU 到底有哪些图可以用来上架」
 * 这个问题在界面上根本问不出来。
 */

const SOURCE_LABEL: Record<string, string> = {
  MANUAL_UPLOAD: '人工上传',
  IMPORTED_URL: '外链导入',
  SUPPLIER_FEED: '供应商推送',
  AI_GENERATED: 'AI 生成',
  PLATFORM_SYNC: '平台回抓',
}

const ROLE_LABEL: Record<string, string> = {
  PRODUCT_FRONT: '商品正面',
  PRODUCT_BACK: '商品背面',
  MODEL_FRONT: '模特正面',
  MODEL_BACK: '模特背面',
  DETAIL: '细节图',
  SIZE_CHART: '尺码表',
  FLAT_LAY: '平铺图',
  PACKAGING: '包装图',
  OTHER: '其它',
}

/**
 * 排序选项。值是后端 `media/service.SORTABLE` 的字段名 + 方向。
 *
 * 分辨率与体积在这里不是凑数的:素材库最常见的两个体力活是「找出分辨率不够的
 * 那几张」和「找出体积异常大的那几张」,以前只能翻页用眼睛比 —— 而列表是服务端
 * 分页的,眼睛比的还只是当前这 24 张。
 */
const SORT_OPTIONS = [
  { value: 'created_at:desc', label: '最新入库' },
  { value: 'created_at:asc', label: '最早入库' },
  { value: 'width:asc', label: '宽度从小到大' },
  { value: 'height:asc', label: '高度从小到大' },
  { value: 'bytes:desc', label: '体积从大到小' },
  { value: 'spu:asc', label: 'SPU 升序' },
]

/** 角色来源徽标。模型猜的角色不能当主图,这个信息必须在列表里看得见。 */
function RoleSourceTag({ asset }: { asset: MediaAsset }) {
  if (asset.role_source === 'HUMAN') return <BrandTag tone="accent">人工</BrandTag>
  if (asset.role_source === 'RULE') return <Tag>规则</Tag>
  if (asset.role_source === 'MODEL') {
    const confident = (asset.role_confidence ?? 0) >= 0.9
    return (
      <Tooltip title={confident ? '把握足够,可用于主图位' : '把握不足 0.9,不能当主图'}>
        <BrandTag tone={confident ? 'accent' : 'warning'}>
          模型 {asset.role_confidence?.toFixed(2) ?? '—'}
        </BrandTag>
      </Tooltip>
    )
  }
  return <BrandTag tone="warning">未定角色</BrandTag>
}

export default function MediaLibraryPage() {
  useDocumentTitle('素材库')
  const { message } = App.useApp()
  const qc = useQueryClient()
  const [query, setQuery] = useState<MediaQuery>({
    page: 1,
    page_size: 24,
    sort: 'created_at',
    order: 'desc',
  })
  const [quarantining, setQuarantining] = useState<MediaAsset | null>(null)
  const [reason, setReason] = useState('')

  const list = useQuery({
    queryKey: ['media', query],
    queryFn: () => mediaApi.list(query),
  })

  // 迁移状态单独拉:它回答的是「回填跑完了没有、这一周干净不干净」，
  // 和素材列表不是同一件事,也不该因为筛选条件变化就重新查
  const migration = useQuery({
    queryKey: ['media-migration'],
    queryFn: mediaApi.migrationStatus,
  })

  const invalidate = useCallback(() => {
    qc.invalidateQueries({ queryKey: ['media'] })
  }, [qc])

  /**
   * 角色 / 隔离 / 放行都是写请求(BLOCK-05)。
   *
   * 隔离尤其不能说"失败了,请重试":隔离会把一张素材从所有可选清单里摘掉,
   * 而放行是另一个人的动作。超时后重来一次没有直接损害,但运营会**以为
   * 隔离没生效**,于是跑去找那张图,而它其实已经不在了 —— 先刷新,
   * 列表自己会告诉他现在是什么状态。
   */
  const onWriteError = useWriteError(invalidate)

  const setRole = useMutation({
    mutationFn: ({ id, role }: { id: string; role: MediaRole }) => mediaApi.setRole(id, role),
    onSuccess: () => {
      message.success('角色已更新')
      invalidate()
    },
    onError: onWriteError,
  })

  const quarantine = useMutation({
    mutationFn: ({ id, why }: { id: string; why: string }) => mediaApi.quarantine(id, why),
    onSuccess: () => {
      message.success('已隔离')
      setQuarantining(null)
      setReason('')
      invalidate()
    },
    onError: onWriteError,
  })

  const release = useMutation({
    mutationFn: (id: string) => mediaApi.release(id),
    onSuccess: () => {
      message.success('已放行')
      invalidate()
    },
    onError: onWriteError,
  })

  const cov = migration.data?.coverage
  /*
   * 拉不到对账结果**不等于零处不一致**(FE-MEDIA-02)。
   *
   * 这个数决定的是"能不能切读路径"这个运维判断。`?? 0` 会把一次接口失败
   * 变成一句"双读一致,可以切" —— 而切错的代价是整站读到旧事实源。
   * 所以失败时不给数,给一条说明。
   */
  const migrationUnknown = migration.isError
  const mismatchTotal = migration.data?.mismatches?.total ?? 0

  // A12:下面两条迁移横幅只对管理员显示。
  //
  // 它们回答的是「双读回填跑完没有、这周能不能切读路径」—— 一个运维判断,
  // 而运营在这一页要做的事(找图、改角色、隔离)不受它影响。摆给他看的代价
  // 很具体:一条写着要去后端敲 `python -m ...`,另一条点名一张数据库表,
  // 两条都是 A12 验收原文点名不许出现在运营页面上的东西,而且他看懂了也做不了。
  //
  // 判据与菜单同一份(`useIdentity`),不新造第二种"谁是管理员"。
  const { isAdmin } = useIdentity()
  const assetsPending = cov ? cov.product_assets - cov.product_assets_shadowed : 0
  const candidatesPending = cov ? cov.candidates_with_image - cov.candidates_linked : 0

  return (
    <Space direction="vertical" size={16} style={{ width: '100%' }}>
      <PageHeader title="素材库" subtitle="管理原始素材的角色、复核状态和隔离情况" />

      {/* 迁移期状态。切换读路径之前这两个数字必须都是 0 —— 放在页面顶部
          而不是藏在运维脚本里,是因为「一周无 mismatch」是个需要有人天天看的判据。
          A12 之后只对管理员显示:天天看它的人是管理员,不是运营 */}
      {isAdmin && cov && (assetsPending > 0 || candidatesPending > 0) && (
        <Alert
          type="warning"
          showIcon
          message="历史素材尚未回填完成"
          description={
            <>
              商品素材 {cov.product_assets_shadowed}/{cov.product_assets}，
              生成候选 {cov.candidates_linked}/{cov.candidates_with_image}。
              请在后端执行 <span className="mono">python -m app.scripts.backfill_media_assets --apply</span>。
              回填完成前,素材库看到的不是全部图片。
            </>
          }
        />
      )}
      {isAdmin && mismatchTotal > 0 && (
        <Alert
          type="error"
          showIcon
          message={`双读对账发现 ${mismatchTotal} 处不一致（最近 ${migration.data?.window_days} 天）`}
          description="新旧两边的素材数据对不上。切换读路径的前提是一周无新增不一致,现在还不能切。详见 media_migration_mismatches 表。"
        />
      )}
      {isAdmin && migrationUnknown && (
        <ErrorNotice
          type="warning"
          title="拉不到迁移对账结果"
          error={migration.error}
          onRetry={() => migration.refetch()}
          retrying={migration.isFetching}
        />
      )}

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space wrap>
          <Input
            allowClear
            placeholder="按 SPU 筛选"
            style={{ width: 180 }}
            onChange={(e) => setQuery((q) => ({ ...q, spu: e.target.value || undefined, page: 1 }))}
          />
          <Select
            allowClear
            placeholder="来源"
            style={{ width: 140 }}
            options={Object.entries(SOURCE_LABEL).map(([value, label]) => ({ value, label }))}
            onChange={(v) => setQuery((q) => ({ ...q, source: v, page: 1 }))}
          />
          <Select
            allowClear
            placeholder="角色"
            style={{ width: 140 }}
            options={Object.entries(ROLE_LABEL).map(([value, label]) => ({ value, label }))}
            onChange={(v) => setQuery((q) => ({ ...q, role: v, page: 1 }))}
          />
          <Select
            allowClear
            placeholder="状态"
            style={{ width: 140 }}
            options={Object.entries(MEDIA_STATUS_LABEL).map(([value, meta]) => ({
              value,
              label: meta.text,
            }))}
            onChange={(v) => setQuery((q) => ({ ...q, status: v, page: 1 }))}
          />
          {/* 排序做成下拉而不是表头箭头:这一页是图片栅格,没有表头可点。
              选项是「字段 + 方向」的组合而不是两个控件 —— 两个控件意味着运营
              要点两次才能换一种看法,而这里只有五种有意义的看法 */}
          <Select
            style={{ width: 190 }}
            value={`${query.sort ?? 'created_at'}:${query.order ?? 'desc'}`}
            options={SORT_OPTIONS}
            onChange={(v) => {
              const [sort, order] = v.split(':')
              // 换排序回第一页:不回的话,运营在第 3 页换了排序,
              // 屏幕上会出现一批他从没见过的图,看起来像筛选被清空了
              setQuery((q) => ({ ...q, sort, order: order as 'asc' | 'desc', page: 1 }))
            }}
          />
        </Space>
      </Card>

      {list.isLoading && <Skeleton active />}
      {list.isError && <Alert type="error" showIcon message={readError(list.error)} />}
      {list.data && list.data.items.length === 0 && <Empty description="没有符合条件的素材" />}

      <div
        style={{
          display: 'grid',
          gridTemplateColumns: `repeat(auto-fill, minmax(${gridMin.gallery}px, 1fr))`,
          gap: 12,
        }}
      >
        {list.data?.items.map((asset) => (
          <Card
            key={asset.id}
            size="small"
            cover={
              <div style={{ height: imageTile.gallery, overflow: 'hidden', background: brandVars.imageBg }}>
                <Image
                  src={asset.url}
                  alt={`素材 ${asset.role ?? '未定角色'} · ${asset.width}×${asset.height}`}
                  height={imageTile.gallery}
                  width="100%"
                  style={{ objectFit: 'contain' }}
                  preview={{ mask: '查看大图' }}
                  /*
                   * 签名地址会过期(`storage.SIGNED_URL_TTL_SECONDS`,30 分钟)。
                   *
                   * 2026-08-11 评审:这里原来只挂一个空占位,注释写着"过期后重新
                   * 拉列表就能拿到新地址,所以不做自动重试" —— 前半句对,后半句
                   * 是把"有办法恢复"当成了"会自动恢复"。这一页**不轮询**,
                   * 而审图是最典型的挂机场景:泡杯咖啡回来,整屏缩略图一起变成
                   * 空白占位,而现象酷似"图全坏了"。运营不会想到按 F5,他会
                   * 去查素材是不是被隔离了。
                   *
                   * 修法与图片集页(A45-#28)完全一致:加载失败时重新取一次列表,
                   * 那会带回新签的地址。`refetch` 而不是 `invalidate` —— 这一次
                   * 失败要的是立刻重签,不是把缓存标脏等下一个订阅者。
                   * `list.isFetching` 那道闸挡住"一屏 20 张图各触发一次 refetch";
                   * 重签之后仍然失败的话,占位符照常出现,不会陷进重试循环。
                   */
                  onError={() => {
                    if (!list.isFetching) void list.refetch()
                  }}
                  fallback="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg'/%3E"
                />
              </div>
            }
          >
            <Space direction="vertical" size={4} style={{ width: '100%' }}>
              <Space wrap size={4}>
                <Tag>{SOURCE_LABEL[asset.source] ?? asset.source}</Tag>
                <Tag color={MEDIA_STATUS_LABEL[asset.status]?.color}>
                  {MEDIA_STATUS_LABEL[asset.status]?.text ?? asset.status}
                </Tag>
              </Space>
              <Space wrap size={4}>
                <RoleSourceTag asset={asset} />
                {asset.legacy_kind && (
                  <Tooltip title="从旧表迁移过来的。旧枚举到角色的映射有损,建议复核">
                    <BrandTag tone="sand">待复核</BrandTag>
                  </Tooltip>
                )}
              </Space>
              <Select
                size="small"
                style={{ width: '100%' }}
                value={asset.role ?? undefined}
                placeholder="指定角色"
                options={Object.entries(ROLE_LABEL).map(([value, label]) => ({ value, label }))}
                onChange={(role) => setRole.mutate({ id: asset.id, role })}
              />
              <Typography.Text type="secondary" style={{ fontSize: fontScale.meta }}>
                {asset.width}×{asset.height} · {(asset.bytes / 1024).toFixed(0)} KB
              </Typography.Text>
              {asset.quarantine_reason && (
                <Typography.Text type="warning" style={{ fontSize: fontScale.meta }}>
                  {asset.quarantine_reason}
                </Typography.Text>
              )}
              {asset.status === 'QUARANTINED' ? (
                <Button size="small" onClick={() => release.mutate(asset.id)}>
                  放行
                </Button>
              ) : (
                <Button size="small" danger onClick={() => setQuarantining(asset)}>
                  隔离
                </Button>
              )}
            </Space>
          </Card>
        ))}
      </div>

      {list.data && list.data.total > 0 && (
        <Pagination
          current={list.data.page}
          pageSize={list.data.page_size}
          total={list.data.total}
          showSizeChanger={false}
          onChange={(page) => setQuery((q) => ({ ...q, page }))}
        />
      )}

      <Modal
        open={quarantining !== null}
        title="隔离素材"
        okText="隔离"
        cancelText="取消"
        confirmLoading={quarantine.isPending}
        onCancel={() => {
          setQuarantining(null)
          setReason('')
        }}
        onOk={() => {
          if (!quarantining || !reason.trim()) {
            message.warning('请填写隔离原因')
            return
          }
          quarantine.mutate({ id: quarantining.id, why: reason.trim() })
        }}
      >
        <Space direction="vertical" style={{ width: '100%' }}>
          <Typography.Text type="secondary">
            隔离不删除素材,只是让它不参与上架,随时可以放行。
            请写清原因——放行的人需要知道当初为什么隔离。
          </Typography.Text>
          <Input.TextArea
            rows={3}
            value={reason}
            placeholder="例如:图中有供应商水印 / 露出超过平台尺度"
            onChange={(e) => setReason(e.target.value)}
          />
        </Space>
      </Modal>
    </Space>
  )
}
