/**
 * 文案标签(FE-221~225)。
 *
 * ## 重生不覆盖未选字段(FE-222 的验收结果)
 *
 * 单字段重生把字段名传给 `only_fields`,后端只覆盖被点名的字段,其余保留上一版
 * (合并规则在 `copy_generator.merge_fields`,claims 跟着标题走)。所以这里
 * **不能**在本地把整份文案回传 —— 那等于人工编辑,会把没选的字段一起写掉。
 *
 * ## 校验结果来自后端
 *
 * 字数、禁词、无依据声明都在 `violations` 里,前端只做分级展示,不自己数字数:
 * 标题上限与 `copy_rules.title_max` 同源,前端再抄一份就会在规则改动后骗人。
 *
 * ## 语言只有 en
 *
 * 首期锁定「一个渠道、一个站点、一个类目、一个模板」,渠道/站点/语言是常量,
 * 所以这里不给选择器 —— 给一个只有一个选项的下拉是假装有能力。
 */
import { useCallback, useEffect, useState } from 'react'
import {
  Alert, App, Button, Card, Descriptions, Empty, Input, Popover, Space, Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ReloadOutlined, ThunderboltOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useWriteError } from '../../hooks/useWriteError'
import { attributesApi, type AttributeValue } from '../../api/attributes'
import {
  COPY_STATUS_LABEL,
  workbenchApi,
  type CopyClaim,
  type CopyViolation,
  type FlowStepResult,
  type ListingCopy,
} from '../../api/workbench'
import { IssueList, RejectNotice } from './FlowBits'
import UnsavedGuard from '../UnsavedGuard'
import { brandVars, fontScale } from '../../theme'
import { formatDateTime } from '../../utils/datetime'

const LOCATION_LABEL: Record<string, string> = {
  title: '标题',
  bullet_points: '卖点',
  description: '描述',
  keywords: '关键词',
}

function ViolationList({ violations }: { violations: CopyViolation[] }) {
  const errors = violations.filter((v) => v.level === 'error')
  const warnings = violations.filter((v) => v.level !== 'error')
  return (
    <Space direction="vertical" size={6} style={{ width: '100%' }}>
      {errors.length > 0 && (
        <Alert
          type="error"
          showIcon
          message={`${errors.length} 处硬失败,修完才能批准`}
          description={
            <ul style={{ paddingLeft: 18, margin: 0 }}>
              {errors.map((v, i) => (
                <li key={i}>
                  {LOCATION_LABEL[v.location ?? ''] ?? v.location ?? '整篇'}:{v.message}
                  <span className="mono" style={{ color: brandVars.textMuted, marginLeft: 6 }}>
                    {v.code}
                  </span>
                </li>
              ))}
            </ul>
          }
        />
      )}
      {warnings.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`${warnings.length} 处提醒(不阻断批准)`}
          description={
            <ul style={{ paddingLeft: 18, margin: 0 }}>
              {warnings.map((v, i) => (
                <li key={i}>
                  {v.message}
                  <span className="mono" style={{ color: brandVars.textMuted, marginLeft: 6 }}>
                    {v.code}
                  </span>
                </li>
              ))}
            </ul>
          }
        />
      )}
    </Space>
  )
}

/**
 * 卖点声明溯源(FE-224)。
 *
 * 按 §4.7 方案 B 降级:点一条 claim 显示它对应的**已确认属性**(值、来源、
 * 置信度),证据图仍在属性标签页看。做到这一步就足以回答「这句话凭什么这么说」;
 * 完整的证据浏览器留在 Backlog B1。
 */
function ClaimTable({ claims, attributes }: { claims: CopyClaim[]; attributes: AttributeValue[] }) {
  const byField = new Map(attributes.map((a) => [a.field_name, a]))
  const columns: ColumnsType<CopyClaim> = [
    {
      title: '声明位置',
      dataIndex: 'location',
      width: 100,
      render: (v: string) => <Tag>{LOCATION_LABEL[v] ?? v}</Tag>,
    },
    { title: '文本片段', dataIndex: 'text_span', width: 180 },
    {
      title: '依据属性',
      key: 'field',
      render: (_, row) => {
        const attr = byField.get(row.field_name)
        const body = attr ? (
          <Descriptions
            size="small"
            column={1}
            bordered
            style={{ maxWidth: 320 }}
            items={[
              { key: 'v', label: '采信值', children: attr.normalized_value ?? String(attr.value_json ?? '—') },
              { key: 's', label: '状态', children: attr.status },
              {
                key: 'c',
                label: '系统置信度',
                children:
                  attr.system_confidence === null
                    ? '未校准'
                    : `${Math.round(attr.system_confidence * 100)}%`,
              },
              { key: 'by', label: '确认人', children: attr.confirmed_by ?? '—' },
              {
                key: 'e',
                label: '证据',
                children: `${attr.evidence_ids.length} 条(在属性标签页查看)`,
              },
            ]}
          />
        ) : (
          <span>事实源里找不到这个字段 —— 这条 claim 应当已被 CLAIM_NOT_IN_FACTS 拦下</span>
        )
        return (
          <Popover content={body} title={row.field_name} trigger="click">
            <a>
              <span className="mono">{row.field_name}</span> = {row.value}
            </a>
          </Popover>
        )
      },
    },
  ]
  if (!claims.length) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="这一版没有 claims。颜色与品类没有任何 claim 会被判为硬失败。"
      />
    )
  }
  return (
    <Table<CopyClaim>
      rowKey={(row) => `${row.location}-${row.field_name}-${row.text_span}`}
      size="small"
      bordered
      pagination={false}
      columns={columns}
      dataSource={claims}
    />
  )
}

export default function CopyTab({
  productId,
  flowProductId = productId,
  copy,
  step,
}: {
  productId: string
  /** 向导跨色操作后刷新入口页,详情页不传时仍刷新自身。 */
  flowProductId?: string
  copy: ListingCopy | null
  step: FlowStepResult | undefined
}) {
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()

  const [title, setTitle] = useState('')
  const [bullets, setBullets] = useState('')
  const [description, setDescription] = useState('')
  const [keywords, setKeywords] = useState('')

  // 服务端版本变了就丢掉本地编辑 —— 否则重生完还显示旧文字,而用户以为重生没生效
  useEffect(() => {
    setTitle(copy?.title ?? '')
    setBullets((copy?.bullet_points ?? []).join('\n'))
    setDescription(copy?.description ?? '')
    setKeywords((copy?.keywords ?? []).join(', '))
  }, [copy?.id, copy?.version])

  const attributes = useQuery({
    queryKey: ['attributes', productId],
    queryFn: () => attributesApi.list(productId),
    enabled: Boolean(productId),
  })

  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['workbench-flow', flowProductId] })
    queryClient.invalidateQueries({ queryKey: ['workbench'] })
  }, [queryClient, flowProductId])

  /**
   * 文案三个动作全是写请求(BLOCK-05)。
   *
   * 生成走的是付费模型链路 —— 超时后提示"请重试"就是让运营再买一版。
   * 批准会改变别人看到的状态,重复批准在审计里留下两条签字。
   * 保存派生新版本,重复保存会多出一个没人要的 v(n+1)。
   * 三者结果未知时都先刷新:版本号自己会说话。
   */
  const onWriteError = useWriteError(refresh)

  const generate = useMutation({
    mutationFn: (onlyFields?: string[]) => workbenchApi.generateCopy(productId, onlyFields),
    onSuccess: (row, onlyFields) => {
      message.success(
        onlyFields?.length
          ? `已重新生成:${onlyFields.join('、')}(其余字段保留上一版)`
          : `已生成 v${row.version}`,
      )
      refresh()
    },
    onError: onWriteError,
  })

  const save = useMutation({
    mutationFn: () =>
      workbenchApi.saveCopy(productId, {
        title,
        bullet_points: bullets.split('\n').map((s) => s.trim()).filter(Boolean),
        description,
        keywords: keywords.split(',').map((s) => s.trim()).filter(Boolean),
      }),
    onSuccess: (row) => {
      message.success(`已保存为 v${row.version}`)
      refresh()
    },
    onError: onWriteError,
  })

  const approve = useMutation({
    mutationFn: () => workbenchApi.approveCopy(productId, copy?.id as string),
    onSuccess: () => {
      message.success('文案已批准')
      refresh()
    },
    onError: onWriteError,
  })

  if (!copy) {
    return (
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        {step && step.issues.length > 0 && (
          <Card size="small" title="这一步的问题">
            <IssueList issues={step.issues} />
          </Card>
        )}
        <Card size="small">
          <Empty description="还没有文案。生成前必须先确认属性 —— 文案里的每句事实都要有已确认属性背书。">
            <Button
              type="primary"
              icon={<ThunderboltOutlined />}
              loading={generate.isPending}
              onClick={() => confirmRegenerate(undefined, '整篇重生')}
            >
              生成文案
            </Button>
          </Empty>
        </Card>
      </Space>
    )
  }

  const status = COPY_STATUS_LABEL[copy.status]
  /**
   * A10:`REJECTED` 有两个来源 —— 规则硬失败(`save_copy` / `revalidate` 置的)
   * 与快审退回。判据是**有没有退回原因**,与后端 `flow._evaluate_copy` 同一条。
   *
   * 合成一个标签的后果是:被人退回的那一版在这里写着「校验失败」,而运营
   * 正是被送来改它的 —— 他会去翻禁词和字数,但问题是有人告诉他尺码写错了。
   */
  const quickRejected = copy.status === 'REJECTED' && Boolean(copy.reject_reason)
  const statusText = quickRejected ? '快审退回' : (status?.text ?? copy.status)
  const blocking = copy.violations.filter((v) => v.level === 'error')
  const approved = copy.status === 'APPROVED'
  const dirty =
    title !== (copy.title ?? '') ||
    bullets !== (copy.bullet_points ?? []).join('\n') ||
    description !== (copy.description ?? '') ||
    keywords !== (copy.keywords ?? []).join(', ')

  /**
   * A45-#32:**重生会销毁未保存的编辑,而它同时是一次付费调用。**
   *
   * 这一页已经知道自己 dirty(标题栏挂"有未保存的编辑",`UnsavedGuard` 拦导航),
   * 但两个重生按钮在 dirty 时照常可点;成功之后
   * `useEffect([copy?.id, copy?.version])` 会把本地编辑全部重置 —— 无确认、无提示。
   *
   * **同一份 dirty 状态,离开页面被拦、页面内销毁它不被拦。** 而销毁它的那个
   * 动作还要调一次付费模型。同页的"批准"在 dirty 时是禁用的,
   * `ImageSetTab` 对"已批准版保存"专门弹了 `modal.confirm` ——
   * 保护标准在同一个仓库里不该有三档。
   *
   * 不做成禁用:重生本来就是"我不满意,重来一遍"的动作,dirty 时想重来是合理的。
   * 要的是让他知道代价。
   */
  const confirmRegenerate = (fields: string[] | undefined, what: string) => {
    if (!dirty) {
      generate.mutate(fields)
      return
    }
    modal.confirm({
      title: `${what}会覆盖你未保存的编辑`,
      content:
        '重新生成之后,当前输入框里的改动会被新生成的内容替换,且无法撤销;这一步还会产生一次模型调用(计费)。',
      okText: '仍然重生',
      cancelText: '先保存',
      onOk: () => generate.mutate(fields),
    })
  }

  /** 单字段重生按钮。字段名与后端 `only_fields` 认的键一致 */
  const regen = (field: string, label: string) => (
    <Tooltip title={`只重新生成${label},其余字段保留上一版`}>
      <Button
        size="small"
        type="link"
        icon={<ReloadOutlined />}
        loading={generate.isPending}
        onClick={() => confirmRegenerate([field], `重新生成${label}`)}
      >
        重生
      </Button>
    </Tooltip>
  )

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <UnsavedGuard dirty={dirty} what="文案" />
      {step && step.issues.length > 0 && (
        <Card size="small" title="这一步的问题">
          <IssueList issues={step.issues} />
        </Card>
      )}

      <RejectNotice
        target="copy"
        reason={copy.reject_reason}
        note={copy.reject_note}
        at={copy.rejected_at}
      />

      <ViolationList violations={copy.violations} />

      <Card
        size="small"
        title={
          <Space size={8}>
            <span>文案</span>
            <Tag>{copy.locale}</Tag>
            <Tag>v{copy.version}</Tag>
            <Tag color={status?.color}>{statusText}</Tag>
            {dirty && <Tag color="warning">有未保存的编辑</Tag>}
          </Space>
        }
        extra={
          <Space>
            <Button
              size="small"
              icon={<ThunderboltOutlined />}
              loading={generate.isPending}
              onClick={() => confirmRegenerate(undefined, '整篇重生')}
            >
              整篇重生
            </Button>
            <Button
              size="small"
              type={dirty ? 'primary' : 'default'}
              disabled={!dirty}
              loading={save.isPending}
              onClick={() => save.mutate()}
            >
              保存编辑
            </Button>
            <Tooltip
              title={
                approved
                  ? '已经批准了。批准后再编辑会落一个新版本并回到待批准'
                  : dirty
                    ? '先保存编辑,校验会重跑'
                    : blocking.length
                      ? '有硬失败,不能批准'
                      : copy.status !== 'VALIDATED'
                        ? `只有校验通过(VALIDATED)的文案可以批准,当前 ${copy.status}`
                        : '保存并批准'
              }
            >
              <Button
                size="small"
                type="primary"
                disabled={approved || dirty || blocking.length > 0 || copy.status !== 'VALIDATED'}
                loading={approve.isPending}
                onClick={() => approve.mutate()}
              >
                批准文案
              </Button>
            </Tooltip>
          </Space>
        }
      >
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <div>
            <Space size={8} style={{ marginBottom: 4 }}>
              <span className="settings-label">标题</span>
              <span style={{ color: brandVars.slate, fontSize: fontScale.meta }}>{title.length} 字符</span>
              {regen('title', '标题')}
            </Space>
            <Input value={title} onChange={(e) => setTitle(e.target.value)} />
          </div>
          <div>
            <Space size={8} style={{ marginBottom: 4 }}>
              <span className="settings-label">卖点</span>
              <span style={{ color: brandVars.slate, fontSize: fontScale.meta }}>一行一条</span>
              {regen('bullet_points', '卖点')}
            </Space>
            <Input.TextArea rows={5} value={bullets} onChange={(e) => setBullets(e.target.value)} />
          </div>
          <div>
            <Space size={8} style={{ marginBottom: 4 }}>
              <span className="settings-label">描述</span>
              <span style={{ color: brandVars.slate, fontSize: fontScale.meta }}>{description.length} 字符</span>
              {regen('description', '描述')}
            </Space>
            <Input.TextArea rows={4} value={description} onChange={(e) => setDescription(e.target.value)} />
          </div>
          <div>
            <Space size={8} style={{ marginBottom: 4 }}>
              <span className="settings-label">关键词</span>
              <span style={{ color: brandVars.slate, fontSize: fontScale.meta }}>逗号分隔</span>
              {regen('keywords', '关键词')}
            </Space>
            <Input value={keywords} onChange={(e) => setKeywords(e.target.value)} />
          </div>
        </Space>
      </Card>

      <Card
        size="small"
        title={`卖点声明溯源 · ${copy.claims.length} 条`}
        extra={
          copy.model_name ? (
            <span style={{ color: brandVars.slate, fontSize: fontScale.meta }}>生成器:{copy.model_name}</span>
          ) : null
        }
      >
        {/*
          * 属性拉不到时不许把证据栏渲染成"无证据"(FE-COPY-04 / 阶段 A 验收第 6 条)。
          * 这张表是文案宣称与属性事实的对账表:一句"没有证据"会让运营把一条
          * 合规的宣称改掉,或者反过来放行一条真没有依据的。空证据必须是结论,
          * 不能是拉取失败的副产品。
          */}
        {attributes.isError ? (
          <Alert
            type="warning"
            showIcon
            message="拉不到属性,证据列暂时对不上"
            description="这不代表这些宣称没有依据。请刷新后再判断是否要改文案。"
          />
        ) : (
          <ClaimTable claims={copy.claims} attributes={attributes.data ?? []} />
        )}
      </Card>

      {approved && (
        <Alert
          type="success"
          showIcon
          message={`已批准${copy.approved_by ? ` · ${copy.approved_by}` : ''}${copy.approved_at ? ` · ${formatDateTime(copy.approved_at)}` : ''}`}
          description="改动已批准的文案会把它降回待批准,并让上架草稿过期 —— 草稿要重新生成一次。"
        />
      )}
    </Space>
  )
}
