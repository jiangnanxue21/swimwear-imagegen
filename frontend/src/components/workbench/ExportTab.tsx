/**
 * 导出标签(FE-235 + §5.2 步骤 13「旧导出记录仍可追溯」+ OPS-REVIEW P1 平台侧闭环)。
 *
 * ## 三道闸都在后端
 *
 * 草稿存在、状态可导出、指纹未过期 —— 三条都由后端在导出请求里现查,
 * 前端把这三条显示成可读的前置条件。
 *
 * ## 平台侧面板(P1)
 *
 * 导出文件下载之后,运营可以在这里录入平台侧状态与驳回原因。
 * 有未解决驳回时,商品在工作台以阻断问题出现,不再顶着「已完成」沉底。
 */
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, App, Button, Card, Checkbox, Empty, Input, Modal,
  Select, Space, Table, Tag, Tooltip, Typography,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import {
  CheckCircleOutlined, CloseCircleOutlined, CloudUploadOutlined, DownloadOutlined,
  FileExcelOutlined, WarningOutlined,
} from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  LOCATED_BY_LABEL,
  PLATFORM_STATUS_LABEL,
  REJECTION_REASON_LABEL,
  platformApi,
  isExportResultUnknown,
  readExportError,
  workbenchApi,
  type ExportHistoryEntry,
  type ListingDraft,
  type PlatformRejection,
} from '../../api/workbench'
import { AUDIT_ACTION_LABEL, saveBlob } from '../../api/batch'
import { isResultUnknown, readWriteError } from '../../api/client'
import ErrorNotice from '../ErrorNotice'
import { brandVars, fontScale } from '../../theme'
import { formatDateTime } from '../../utils/datetime'

/*
 * A45-#4:`saveBlob` 的**复制件已删**,统一用 `api/batch.ts` 那一份。
 *
 * 这里原来有一份逐字复制的实现。A-34 修 revoke 时机时我改的是**这一份** ——
 * 没发现正本在 `api/batch.ts`。于是修完之后两份都"对",但一份延后 60 秒、
 * 一份延后 0 毫秒,行为不同而没有任何东西会发现它们不一致。
 *
 * 比修之前更糟:修之前两份是"同样错",修之后是"不同程度地对"。
 * 同一个 helper 两份实现的漂移,正是本轮评审归纳的一整个残留带。
 *
 * `tests/pure/test_a45_batch8_fixes.py` 断言全仓只有一个 saveBlob 定义。
 */

/*
 * A-35:**文案只有一份,在 `api/batch.ts` 的 `AUDIT_ACTION_LABEL` 里。**
 *
 * 这里原来自带一张 `{ export: '导出', build: '生成草稿' }`,而共享那张写的是
 * 「导出上架文件」。同一个动作在导出页和审计页显示成两个词,谁也不会发现
 * 它们本该一致 —— 文案单源 + 契约测试是本仓的纪律,这里是唯一一处例外。
 *
 * 留在本地的只有颜色:它是这张表格的展示细节,审计页不需要。
 * `tests/pure/test_a44_batch5_fixes.py` 断言这里的每个键都在共享表里。
 */
const ACTION_COLOR: Record<string, string> = {
  export: 'blue',
  build: 'default',
}

// ---------------------------------------------------------------- 平台状态面板

function PlatformPanel({ productId, draft }: { productId: string; draft: ListingDraft }) {
  const { message } = App.useApp()
  const qc = useQueryClient()
  const [showRejectModal, setShowRejectModal] = useState(false)
  const [reasonCode, setReasonCode] = useState<string>('IMAGE_ISSUE')
  const [platformNote, setPlatformNote] = useState('')
  // A4:关闭驳回不再是一次点击直达,要过确认框
  const [resolveTarget, setResolveTarget] = useState<PlatformRejection | null>(null)
  const [resolveNote, setResolveNote] = useState('')
  const [resolveConfirmed, setResolveConfirmed] = useState(false)

  const rejections = useQuery({
    queryKey: ['platform-rejections', productId],
    queryFn: () => platformApi.listRejections(productId),
  })

  /**
   * 平台侧写请求失败的统一收尾(FE-GLOBAL-01)。
   *
   * 这三个动作都是**人工声明**:后端没有外部平台的回执可以对账,库里记的
   * 就是运营说的那句话。所以超时后尤其不能说"失败了,请重试" ——
   * 重试可能录进第二条同样的驳回,而台账上没有任何东西能看出这是同一条。
   */
  const onPlatformWriteError = (err: unknown) => {
    message.error(readWriteError(err))
    if (isResultUnknown(err)) {
      qc.invalidateQueries({ queryKey: ['platform-rejections', productId] })
    }
  }

  const setStatus = useMutation({
    mutationFn: (status: string) => platformApi.setStatus(productId, status),
    onSuccess: () => {
      message.success('平台状态已更新')
      qc.invalidateQueries({ queryKey: ['platform-rejections', productId] })
      qc.invalidateQueries({ queryKey: ['workbench-flow', productId] })
      qc.invalidateQueries({ queryKey: ['workbench'] })
    },
    onError: onPlatformWriteError,
  })

  const recordRejection = useMutation({
    mutationFn: () =>
      platformApi.recordRejection(productId, {
        reason_code: reasonCode,
        platform_note: platformNote || null,
      }),
    onSuccess: () => {
      message.success('驳回已记录;商品进入阻断列表')
      setShowRejectModal(false)
      setPlatformNote('')
      qc.invalidateQueries({ queryKey: ['platform-rejections', productId] })
      qc.invalidateQueries({ queryKey: ['workbench-flow', productId] })
      qc.invalidateQueries({ queryKey: ['workbench'] })
    },
    onError: onPlatformWriteError,
  })

  const resolveRejection = useMutation({
    mutationFn: (vars: { rejectionId: string; note: string | null; confirmed: boolean }) =>
      platformApi.resolveRejection(productId, vars.rejectionId, {
        note: vars.note,
        confirmed: vars.confirmed,
      }),
    onSuccess: () => {
      message.success('驳回已标记解决')
      setResolveTarget(null)
      setResolveNote('')
      setResolveConfirmed(false)
      qc.invalidateQueries({ queryKey: ['platform-rejections', productId] })
      qc.invalidateQueries({ queryKey: ['workbench-flow', productId] })
      qc.invalidateQueries({ queryKey: ['workbench'] })
    },
    onError: onPlatformWriteError,
  })

  const data = rejections.data

  /*
   * FE-EXPORT-02(BLOCK-13):驳回查询失败时**不许按"无驳回"计算**。
   *
   * 上一版是 `data?.platform_status ?? 'NONE'` + `data?.open_count ?? 0`。
   * 接口挂掉时这两个默认值一路推出 `canSubmit = true` —— 于是页面上
   * "标记为已上传平台"是亮的,而库里可能正躺着三条未解决的驳回。
   * 运营点下去,商品在看板上变成"已提交",驳回被这个状态盖住。
   *
   * 页面下方原本就有一条错误提示,但它在按钮**下面**,而且不影响按钮 ——
   * 提示和可执行动作说了两件相反的事,以按钮为准。
   *
   * 现在:读不到就一个状态动作都不给,并把原因摆在动作区上方。
   */
  const unknownPlatformState = rejections.isError || (rejections.isLoading && !data)
  const platformStatus = data?.platform_status ?? 'NONE'
  const openCount = data?.open_count ?? 0
  const statusMeta = PLATFORM_STATUS_LABEL[platformStatus] ?? { text: platformStatus, color: 'default' }

  /**
   * FE-EXPORT-04:平台状态、记录驳回、解决驳回三者互斥。
   *
   * 三个动作改的是同一件商品的同一份平台台账,而且互相依赖:
   * "解决驳回"的闸门条件里包含平台状态,"标记已上传"的前置条件里包含
   * 未解决驳回数。并发发出时,后端各自校验各自的前置 —— 两个都能过,
   * 而合起来是一个矛盾的状态。
   */
  const platformBusy =
    setStatus.isPending || recordRejection.isPending || resolveRejection.isPending

  /*
   * 允许的手工转移(前端镜像 platform.manual_targets 的逻辑;后端仍是守门人)。
   *
   * **"该不该出现这个动作"和"现在能不能点"必须分开算。**
   *
   * 这几个按钮是条件渲染的(`{canSubmit && <Button/>}`),把 busy 折进条件里
   * 会让按钮在点下去的那一刻**整个消失** —— `loading` spinner 永远不显示,
   * 整行动作区先变空再变回来。那比不加互斥还糟。
   *
   * 所以:出现与否只看平台状态(下面三个),能不能点看 `platformLocked`(disabled)。
   */
  const showSubmit = platformStatus === 'NONE' && openCount === 0
  const showLive   = platformStatus === 'SUBMITTED' && openCount === 0
  const showRevert = platformStatus === 'LIVE' && openCount === 0
  const showReRecord = Boolean(draft.exported_at)

  /**
   * 现在不能动平台状态。两种原因:
   *
   *   读不到台账     这几个判断全部建立在"我知道现在是什么状态"之上,
   *                  而那正是拿不到的东西(FE-EXPORT-02)
   *   已有动作在飞    三个动作改同一份台账且互相依赖(FE-EXPORT-04)
   */
  const platformLocked = unknownPlatformState || platformBusy

  const rejectionColumns: ColumnsType<PlatformRejection> = [
    {
      title: '状态',
      key: 'status',
      width: 90,
      render: (_, row) => (
        <Tag color={row.status === 'OPEN' ? 'error' : 'default'}>
          {row.status_label}
        </Tag>
      ),
    },
    {
      title: '原因',
      key: 'reason',
      render: (_, row) => (
        <div>
          <div>{row.reason_label}</div>
          {row.platform_note && (
            <div style={{ fontSize: fontScale.body, color: brandVars.slate }}>平台原话:{row.platform_note}</div>
          )}
        </div>
      ),
    },
    {
      title: '定位',
      key: 'located',
      width: 160,
      render: (_, row) => (
        <Tooltip title={LOCATED_BY_LABEL[row.located_by] ?? row.located_by}>
          <div style={{ fontSize: fontScale.meta }}>
            {row.export_fingerprint && (
              <span className="mono" style={{ color: brandVars.slate }}>
                {row.export_fingerprint.slice(0, 12)}
              </span>
            )}
            {row.component_snapshot?.image_set && (
              <div>图片集 v{row.component_snapshot.image_set.version}</div>
            )}
          </div>
        </Tooltip>
      ),
    },
    {
      title: '记录时间',
      dataIndex: 'created_at',
      width: 160,
      render: (v: string | null) => formatDateTime(v),
    },
    {
      // A4 验收:"页面明确显示还缺少哪一步"。放在表里而不是只放确认框里 ——
      // 运营扫一眼列表就知道哪几条是可以直接关的
      title: '关闭闸门',
      key: 'gate',
      width: 190,
      render: (_, row) => {
        if (row.status !== 'OPEN') {
          return row.resolved_note ? (
            <Tooltip title={row.resolved_note}>
              <span style={{ fontSize: fontScale.meta, color: brandVars.slate }}>
                {row.resolved_note.slice(0, 18)}
                {row.resolved_note.length > 18 ? '…' : ''}
              </span>
            </Tooltip>
          ) : (
            <span style={{ fontSize: fontScale.meta, color: brandVars.slate }}>—</span>
          )
        }
        const gate = row.resolve_gate
        if (!gate) return <span style={{ fontSize: fontScale.meta, color: brandVars.slate }}>—</span>
        if (gate.satisfied) {
          return (
            <Tag color="success" icon={<CheckCircleOutlined />}>
              已重导且内容有变化
            </Tag>
          )
        }
        return (
          <Tooltip title={gate.missing_advice.filter(Boolean).join(';')}>
            <div>
              {gate.missing_labels.map((label) => (
                <Tag key={label} color="warning" style={{ marginBottom: 2 }}>
                  {label}
                </Tag>
              ))}
            </div>
          </Tooltip>
        )
      },
    },
    {
      title: '',
      key: 'ops',
      width: 80,
      render: (_, row) =>
        row.status === 'OPEN' ? (
          <Button
            size="small"
            type="link"
            icon={<CheckCircleOutlined />}
            // 和上面那排动作同一把锁:三个动作改的是同一份台账
            disabled={platformLocked}
            onClick={() => {
              setResolveTarget(row)
              setResolveNote('')
              setResolveConfirmed(false)
            }}
          >
            解决
          </Button>
        ) : (
          <Tag color="default">已解决</Tag>
        ),
    },
  ]

  // A4:闸门没过时,勾选与说明至少要有一个,否则后端 409。
  // 前端把这条判断显式写出来(禁用确认按钮),而不是让运营点了才被拒
  const resolveGate = resolveTarget?.resolve_gate ?? null
  const gateSatisfied = resolveGate?.satisfied ?? true
  const canConfirmResolve = gateSatisfied || resolveConfirmed || resolveNote.trim().length > 0

  return (
    <Card
      size="small"
      title={
        <Space>
          <span>平台侧状态</span>
          {/* 未知就说未知。显示成"未提交"是在替后端编一个事实 */}
          {unknownPlatformState ? (
            <Tag color="default">状态未知</Tag>
          ) : (
            <Tag color={statusMeta.color}>{statusMeta.text}</Tag>
          )}
          {!unknownPlatformState && openCount > 0 && (
            <Tag color="error" icon={<WarningOutlined />}>
              驳回 {openCount} 条未解决
            </Tag>
          )}
        </Space>
      }
    >
      {rejections.isError && (
        <ErrorNotice
          title="读不到平台驳回台账"
          error={rejections.error}
          onRetry={() => rejections.refetch()}
          retrying={rejections.isFetching}
          style={{ marginBottom: 12 }}
        />
      )}

      {unknownPlatformState && (
        <Alert
          type="warning"
          showIcon
          message="平台状态动作已暂时禁用"
          description="拿不到当前台账时无法判断有没有未解决的驳回。此时标记「已上传平台」会把驳回盖住 —— 先重试上面的读取。"
          style={{ marginBottom: 12 }}
        />
      )}

      {!unknownPlatformState && openCount > 0 && (
        <Alert
          type="error"
          showIcon
          message={`有 ${openCount} 条平台驳回未解决`}
          description="这件商品在工作台以阻断问题出现,修复并重导后标记解决即可。"
          style={{ marginBottom: 12 }}
        />
      )}

      {/* 状态未知时整排动作都不渲染:那时候连"该出现哪一个"都答不上来 */}
      {!unknownPlatformState && (
        <Space wrap>
          {showSubmit && (
            <Button
              size="small"
              disabled={platformLocked}
              onClick={() => setStatus.mutate('SUBMITTED')}
              loading={setStatus.isPending}
            >
              标记为已上传平台
            </Button>
          )}
          {showLive && (
            <Button
              size="small"
              type="primary"
              icon={<CheckCircleOutlined />}
              disabled={platformLocked}
              onClick={() => setStatus.mutate('LIVE')}
              loading={setStatus.isPending}
            >
              平台审核通过 · 上架
            </Button>
          )}
          {showRevert && (
            <Button
              size="small"
              disabled={platformLocked}
              onClick={() => setStatus.mutate('SUBMITTED')}
              loading={setStatus.isPending}
            >
              整改后重新上传
            </Button>
          )}
          {showReRecord && (
            <Button
              size="small"
              danger
              icon={<CloseCircleOutlined />}
              disabled={platformLocked}
              onClick={() => setShowRejectModal(true)}
            >
              记录平台驳回
            </Button>
          )}
        </Space>
      )}



      {!unknownPlatformState && (data?.items.length ?? 0) > 0 && (
        <div style={{ marginTop: 12 }}>
          <Table<PlatformRejection>
            rowKey="id"
            size="small"
            bordered
            columns={rejectionColumns}
            dataSource={data?.items ?? []}
            pagination={false}
            loading={rejections.isFetching}
          />
        </div>
      )}

      {/* A4:关闭驳回的最小闸门。强提醒 + 二次确认,不硬阻断 ——
          平台侧全手工录入,系统判定不了"已重新上传",硬拦只会逼出
          "先随便导一次骗过闸门"的动作 */}
      <Modal
        open={resolveTarget !== null}
        title="关闭这条平台驳回"
        okText={gateSatisfied ? '确认关闭' : '仍然关闭'}
        okButtonProps={{
          disabled: !canConfirmResolve || platformBusy,
          danger: !gateSatisfied,
        }}
        cancelText="取消"
        onCancel={() => setResolveTarget(null)}
        onOk={() =>
          resolveTarget &&
          resolveRejection.mutate({
            rejectionId: resolveTarget.id,
            note: resolveNote.trim() || null,
            confirmed: resolveConfirmed,
          })
        }
        confirmLoading={resolveRejection.isPending}
        destroyOnHidden
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          {resolveTarget && (
            <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
              {resolveTarget.reason_label}
              {resolveTarget.platform_note ? ` · ${resolveTarget.platform_note}` : ''}
            </Typography.Text>
          )}

          {gateSatisfied ? (
            <Alert
              type="success"
              showIcon
              message="驳回之后已经重新导出,且内容指纹有变化"
              description="关闭后平台状态会退回「已上传平台」,记得在平台侧确认新版本已提交。"
            />
          ) : (
            <Alert
              type="warning"
              showIcon
              message="系统没有看到这条驳回被修过的证据"
              description={
                <div>
                  <ul style={{ margin: '4px 0 4px 18px', padding: 0 }}>
                    {(resolveGate?.missing_labels ?? []).map((label, i) => (
                      <li key={label}>
                        {label}
                        {resolveGate?.missing_advice?.[i]
                          ? ` —— ${resolveGate.missing_advice[i]}`
                          : ''}
                      </li>
                    ))}
                  </ul>
                  关闭之后这条记录仍会留在台账里,并标注是人工确认关闭的。
                </div>
              }
            />
          )}

          {!gateSatisfied && (
            <Checkbox
              checked={resolveConfirmed}
              onChange={(e) => setResolveConfirmed(e.target.checked)}
            >
              我已修改内容并在平台侧重新上传
            </Checkbox>
          )}

          <div>
            <div style={{ marginBottom: 4, fontWeight: 500 }}>
              关闭说明{gateSatisfied ? '(选填)' : resolveConfirmed ? '(选填)' : '(必填)'}
            </div>
            <Input.TextArea
              value={resolveNote}
              onChange={(e) => setResolveNote(e.target.value)}
              rows={3}
              placeholder={
                gateSatisfied
                  ? '改了什么,可留一句备忘'
                  : '说明为什么现在可以关闭这条驳回'
              }
            />
          </div>

          {!canConfirmResolve && (
            <Typography.Text type="danger" style={{ fontSize: fontScale.body }}>
              勾选上面那一项,或填写关闭说明,才能关闭这条驳回。
            </Typography.Text>
          )}
        </Space>
      </Modal>

      <Modal
        open={showRejectModal}
        title="记录平台驳回"
        okText="确认记录"
        cancelText="取消"
        onCancel={() => setShowRejectModal(false)}
        onOk={() => recordRejection.mutate()}
        okButtonProps={{ disabled: platformBusy }}
        confirmLoading={recordRejection.isPending}
        destroyOnHidden
      >
        <Space direction="vertical" style={{ width: '100%' }} size={12}>
          <div>
            <div style={{ marginBottom: 4, fontWeight: 500 }}>驳回原因</div>
            <Select
              style={{ width: '100%' }}
              value={reasonCode}
              onChange={setReasonCode}
              options={Object.entries(REJECTION_REASON_LABEL).map(([value, label]) => ({
                value,
                label,
              }))}
            />
          </div>
          <div>
            <div style={{ marginBottom: 4, fontWeight: 500 }}>平台原话(选填)</div>
            {/* antd v5 没有 Typography.Text.TextArea,这里原本写成那样,
                弹窗一打开就是 Element type is invalid —— 录不进驳回,
                P1 的整条回流路径都走不通。正确的是 Input.TextArea */}
            <Input.TextArea
              value={platformNote}
              onChange={(e) => setPlatformNote(e.target.value)}
              rows={3}
              placeholder="把平台驳回通知原文贴在这里"
            />
          </div>
          <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
            系统会自动定位这次驳回对应的是哪一版图片集与文案,
            方便修复后与驳回内容比对。
          </Typography.Text>
        </Space>
      </Modal>
    </Card>
  )
}

// ---------------------------------------------------------------- 主组件

export default function ExportTab({
  productId,
  draft,
}: {
  productId: string
  draft: ListingDraft | null
}) {
  const { message } = App.useApp()
  const navigate = useNavigate()

  const history = useQuery({
    queryKey: ['workbench-exports', productId],
    queryFn: () => workbenchApi.exportHistory(productId),
    enabled: Boolean(productId),
  })

  /**
   * 导出是**写**请求(BLOCK-06)。
   *
   * 后端的顺序是:生成文件 -> `export_count + 1` -> 写审计 -> commit ->
   * 才把文件流回浏览器。浏览器超时或连接中断落在最后一步时,库里那三件事
   * 已经做完了 —— 而页面此时说"失败,请重试"是**错的**:再点一次会产生
   * 第二条导出审计、第二次计数、第二个文件名和导出时间,平台驳回追溯时
   * 会看到两次逻辑上相同的导出。
   *
   * 结果未知时唯一正确的第一动作是**刷新导出历史**:那条记录在不在,
   * 本身就是答案。所以这里不复用 `useWriteError` —— 那个 hook 只发一条
   * message,而这里还要把"先看一眼历史"这件事做出来并说出来。
   */
  const download = useMutation({
    mutationFn: (fmt: 'xlsx' | 'csv') => workbenchApi.exportFile(productId, fmt),
    onSuccess: (file) => {
      saveBlob(file.blob, file.filename)
      message.success(`已导出 ${file.filename}`)
      history.refetch()
    },
    onError: async (err) => {
      const text = await readExportError(err)
      if (isExportResultUnknown(err)) {
        // 先刷,再说话:提示弹出来的时候历史多半已经回来了
        await history.refetch()
        message.warning({
          content: `${text} 已为你刷新下方导出历史 —— 如果新记录已经出现,不要再导一次。`,
          duration: 8,
        })
        return
      }
      message.error(text)
    },
  })

  const stale = draft?.stale?.stale ?? draft?.status === 'STALE'
  const errors = draft?.validation_errors.length ?? 0
  const gates: { ok: boolean; label: string; detail: string }[] = [
    {
      ok: Boolean(draft),
      label: '草稿已生成',
      detail: draft ? '存在当前草稿' : '还没有草稿,先去草稿标签页生成',
    },
    {
      ok: Boolean(draft) && errors === 0,
      label: '字段校验通过',
      detail: errors ? `${errors} 个字段不合格` : '必填、长度、枚举、图片数量都过了',
    },
    {
      ok: Boolean(draft) && !stale,
      label: '未过期',
      detail: stale ? '上游已变化,必须重新生成草稿' : '草稿反映的是当前的上游数据',
    },
  ]
  const canExport = gates.every((g) => g.ok)

  const columns: ColumnsType<ExportHistoryEntry> = [
    {
      title: '动作',
      dataIndex: 'action',
      width: 100,
      render: (v: string) => (
        <Tag color={ACTION_COLOR[v]}>{AUDIT_ACTION_LABEL[v] ?? v}</Tag>
      ),
    },
    // A45-#33:时间显示的唯一入口是 `utils/datetime.ts`(那个模块文档明写的)。
    // 原来这里把后端 ISO 串原样打到界面,而同一页第 236 行的"最近一次导出"
    // 走的是 formatDateTime —— 同一件事两种时间并排出现,一个 UTC 原文一个本地化
    { title: '时间', dataIndex: 'at', width: 200, render: (v: string | null) => formatDateTime(v) },
    { title: '操作人', dataIndex: 'actor', width: 140, render: (v: string | null) => v ?? '—' },
    {
      title: '指纹 / 格式',
      key: 'payload',
      render: (_, row) => (
        <Space size={8}>
          {typeof row.payload.format === 'string' && <Tag>{row.payload.format}</Tag>}
          {typeof row.payload.fingerprint === 'string' && (
            <Tooltip title="这次导出对应的上游数据指纹。改了颜色再导,这一列就会不同">
              <span className="mono" style={{ color: brandVars.slate }}>
                {row.payload.fingerprint}
              </span>
            </Tooltip>
          )}
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <Card size="small" title="导出前置条件">
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          {gates.map((gate) => (
            <div key={gate.label} style={{ display: 'flex', gap: 8, alignItems: 'baseline' }}>
              <Tag color={gate.ok ? 'success' : 'error'} style={{ marginInlineEnd: 0 }}>
                {gate.ok ? '通过' : '未通过'}
              </Tag>
              <span style={{ minWidth: 90 }}>{gate.label}</span>
              <span style={{ color: brandVars.slate }}>{gate.detail}</span>
            </div>
          ))}
        </Space>
        <Space style={{ marginTop: 12 }}>
          <Button
            type="primary"
            icon={<FileExcelOutlined />}
            disabled={!canExport}
            loading={download.isPending}
            onClick={() => download.mutate('xlsx')}
          >
            导出 Excel
          </Button>
          <Tooltip title="CSV 是给导出一致率比对脚本用的,不作为交付给平台的文件">
            <Button
              icon={<DownloadOutlined />}
              disabled={!canExport}
              loading={download.isPending}
              onClick={() => download.mutate('csv')}
            >
              导出 CSV(比对用)
            </Button>
          </Tooltip>
          {/*
            B-02:发布链路的入口。**这是流水线终点原来断掉的那一截。**

            上架文件导出之后,后端还有一整条 `/publish/*`(提交 / 刷新平台状态 /
            下架 / 清理清单),而在这个按钮之前前端没有任何一处通向它 ——
            运营的动线到"下载 zip"为止。

            跳过去而不是就地做:提交要选店铺、要填测试批次标签、要先干跑看报文,
            那是一整块交互;更要紧的是提交完之后要能**看到它后来怎么样了**,
            而那需要清单页。带 `?product_id=` 过去直接打开提交弹窗。

            不设 `disabled`:能不能提交由后端 `publish_view` 判定并在那一页
            以 `blocking_reasons` 说明原因。在这里自己判一遍等于把那条规则
            复制到第二个地方,而两份判定迟早会给出不同答案。
          */}
          <Tooltip title="到发布页提交上架、跟踪平台状态。能不能提交由发布页按后端判定给出">
            <Button
              icon={<CloudUploadOutlined />}
              onClick={() => navigate(`/publish?product_id=${productId}`)}
            >
              发布到平台
            </Button>
          </Tooltip>
        </Space>
        {/*
          * 受众闸口的警告档(§17 兼容缝)。**不是阻断** —— 存量商品
          * 「受众未确认 + 无前缀规则包」这一组合是允许导出的,矩阵为它
          * 留了唯一一条缝。但导出的人要知道自己导的是哪一版规则包:
          * 确认受众的那一刻,这份草稿就要按新规则包重新生成。
          *
          * 这条警告后端一直在算,只是之前没有出口 —— `export_gate`
          * 只读了 problems,warnings 算完就被丢掉了。
          */}
        {draft?.audience_warnings?.length ? (
          <Alert
            type="warning"
            showIcon
            style={{ marginTop: 12 }}
            message="这份草稿用的是受众前时代的规则包"
            description={
              <>
                {draft.audience_warnings.map((w) => (
                  <div key={w}>{w}</div>
                ))}
                <div style={{ marginTop: 4 }}>
                  现在导出不会被拦,但确认受众之后要重新生成草稿,
                  届时会按该受众的规则包重新校验。
                </div>
              </>
            }
          />
        ) : null}
        {draft && draft.export_count > 0 && (
          <div style={{ color: brandVars.slate, fontSize: fontScale.meta, marginTop: 8 }}>
            已导出 {draft.export_count} 次,最近一次 {formatDateTime(draft.exported_at)}
            {draft.exported_by ? ` · ${draft.exported_by}` : ''}
          </div>
        )}
      </Card>

      {/* P1:平台侧面板 —— 只在已导出过时展示 */}
      {draft && <PlatformPanel productId={productId} draft={draft} />}

      <Card size="small" title="导出历史">
        {history.isError ? (
          <ErrorNotice
            title="拉不到导出历史"
            error={history.error}
            onRetry={() => history.refetch()}
          />
        ) : (
          <Table<ExportHistoryEntry>
            rowKey={(row) => `${row.action}-${row.at}`}
            size="small"
            bordered
            pagination={false}
            loading={history.isFetching}
            columns={columns}
            dataSource={history.data ?? []}
            locale={{
              emptyText: (
                <Empty
                  image={Empty.PRESENTED_IMAGE_SIMPLE}
                  description="还没有导出记录。导出后这里会留下操作人、时间和指纹。"
                />
              ),
            }}
          />
        )}
      </Card>
    </Space>
  )
}
