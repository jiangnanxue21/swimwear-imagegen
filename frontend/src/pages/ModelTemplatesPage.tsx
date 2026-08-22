import { useCallback, useEffect, useState } from 'react'
import {
  App, Button, Card, Checkbox, DatePicker, Divider, Empty, Form, Image, Input,
  Modal, Select, Space, Switch, Tag, Typography,
} from 'antd'
import { EditOutlined, PlusOutlined } from '@ant-design/icons'
import dayjs from 'dayjs'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { modelTemplatesApi } from '../api/generation'
import { useWriteError } from '../hooks/useWriteError'
import {
  AUDIENCE_LABEL,
  BODY_TYPES,
  BODY_TYPES_BY_AUDIENCE,
  BODY_TYPE_LABEL,
  MODEL_AUDIENCES,
  MODEL_LICENSE_STATUS_LABEL,
  POSES,
  POSE_LABEL,
  type Audience,
  type ModelTemplate,
} from '../api/types'

import { brandVars, fontScale } from '../theme'
import BrandTag from '../components/BrandTag'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

/**
 * §11 授权字段组。**创建与编辑共用这一份** —— 抄两份的后果是新增一个
 * 授权字段时只有一处跟上,而另一处的表单会静默地把它留成默认值。
 *
 * §11 要求每个模特模板记录十二项。它们不是"以后补":男装模特入库时
 * 如果这套字段还不在,第二批素材就会以第一批的方式进来,
 * 而第一批当时没人要求填。
 */
function LicenseFields() {
  return (
    <>
      <Divider orientation="left" style={{ margin: '4px 0 12px' }}>
        <span style={{ fontSize: fontScale.body }}>授权与合规(§11)</span>
      </Divider>

      {/*
        * §11.1 年龄确认。**这个控件之前根本不存在。**
        *
        * 提交时写了 `age_verified: values.age_verified ?? false`,而表单里
        * 没有任何 `name="age_verified"` 的 Form.Item —— `values.age_verified`
        * 恒为 undefined,于是这一行恒等于 false,**每一个通过界面建出来的
        * 模特都会永久挂着"年龄未确认"的橙色标记**。一个 100% 命中的警示
        * 等于没有警示:两周之后运营会自动忽略它,包括真正该看的那一次。
        *
        * 默认不勾是刻意的:false 的含义是"没人核实过",不是"未成年"。
        * 泳衣 + AI 生成人像是所有服饰品类里这一条最敏感的组合,
        * 默认勾上等于替不存在的核实签字(§0.3 第一条底线)。
        */}
      <Form.Item
        name="age_verified"
        valuePropName="checked"
        extra="不勾 = 没人核实过(不是「未成年」)。泳衣只使用已确认成年的模特,男装女装同一标准"
      >
        <Checkbox>已确认本人成年(§11.1)</Checkbox>
      </Form.Item>

      <Space.Compact block>
        <Form.Item name="source" label="素材来源" style={{ width: '50%', marginRight: 8 }}>
          <Input placeholder="签约拍摄 / 素材库采购 / 自有" />
        </Form.Item>
        <Form.Item
          name="license_status"
          label="授权状态"
          style={{ width: '50%' }}
          extra="未核实不阻断新任务,但会进待补清单"
        >
          <Select
            options={Object.entries(MODEL_LICENSE_STATUS_LABEL).map(([value, label]) => ({
              value,
              label,
            }))}
          />
        </Form.Item>
      </Space.Compact>

      <Form.Item name="commercial_scope" label="商业用途范围">
        <Input.TextArea rows={2} placeholder="电商详情页、社媒投放…" />
      </Form.Item>

      <Space.Compact block>
        <Form.Item name="allowed_platforms" label="可使用平台" style={{ width: '50%', marginRight: 8 }}>
          <Select mode="tags" placeholder="回车添加" />
        </Form.Item>
        <Form.Item name="allowed_regions" label="可使用国家/地区" style={{ width: '50%' }}>
          <Select mode="tags" placeholder="回车添加" />
        </Form.Item>
      </Space.Compact>

      <Space.Compact block>
        <Form.Item name="license_start_at" label="授权开始" style={{ width: '50%', marginRight: 8 }}>
          <DatePicker showTime style={{ width: '100%' }} />
        </Form.Item>
        <Form.Item
          name="license_expires_at"
          label="授权到期"
          style={{ width: '50%' }}
          extra="到期后禁止创建新任务"
        >
          <DatePicker showTime style={{ width: '100%' }} />
        </Form.Item>
      </Space.Compact>

      {/* 这两条只在「已授权」时参与判定(见后端 model_license.assert_usable)。
          默认 false 的含义是"没人核实过",对 UNVERIFIED 执行它会让存量模板
          全部停摆 —— 而那与迁移 0030 记的取向直接矛盾 */}
      <Form.Item name="allow_ai_dressing" valuePropName="checked" style={{ marginBottom: 4 }}>
        <Checkbox>允许 AI 换装（标记为“已授权”后，此限制将强制生效）</Checkbox>
      </Form.Item>
      <Form.Item name="allow_derivative_images" valuePropName="checked" style={{ marginBottom: 12 }}>
        <Checkbox>允许生成衍生图片</Checkbox>
      </Form.Item>

      <Form.Item
        name="prohibited_categories"
        label="禁止使用的品类"
        extra="按规则包的键填,例如 men_swimwear —— 和生成上架草稿时用的是同一套键"
      >
        <Select mode="tags" placeholder="回车添加" />
      </Form.Item>
    </>
  )
}

export default function ModelTemplatesPage() {
  useDocumentTitle('模特模板')
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [open, setOpen] = useState(false)
  const [file, setFile] = useState<File | null>(null)
  const [form] = Form.useForm()
  //: 正在编辑的模板。null = 编辑弹窗关着
  const [editing, setEditing] = useState<ModelTemplate | null>(null)
  const [editForm] = Form.useForm()

  const query = useQuery({ queryKey: ['model-templates'], queryFn: () => modelTemplatesApi.list() })

  /** 建模板会上传一张图并落一行;开关会改变别人建任务时能选到什么(BLOCK-05) */
  const refreshTemplates = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['model-templates'] })
  }, [queryClient])

  const onWriteError = useWriteError(refreshTemplates)

  /** 打开编辑弹窗时把当前值灌进表单。日期列是 ISO 字符串,DatePicker 要 dayjs */
  useEffect(() => {
    if (!editing) return
    editForm.setFieldsValue({
      name: editing.name,
      pose: editing.pose,
      background: editing.background,
      tags: editing.tags ?? [],
      notes: editing.notes ?? '',
      source: editing.source ?? '',
      license_status: editing.license_status ?? 'UNVERIFIED',
      commercial_scope: editing.commercial_scope ?? '',
      allowed_platforms: editing.allowed_platforms ?? [],
      allowed_regions: editing.allowed_regions ?? [],
      license_start_at: editing.license_start_at ? dayjs(editing.license_start_at) : null,
      license_expires_at: editing.license_expires_at ? dayjs(editing.license_expires_at) : null,
      allow_ai_dressing: editing.allow_ai_dressing ?? false,
      allow_derivative_images: editing.allow_derivative_images ?? false,
      age_verified: editing.age_verified ?? false,
      prohibited_categories: editing.prohibited_categories ?? [],
    })
  }, [editing, editForm])

  const create = useMutation({
    //: 与 `modelTemplatesApi.create` 的参数类型共用同一个形状。batch10 给
    //: 提交载荷加了三个布尔授权字段,这里若仍声明 Record<string, string>,
    //: `tsc` 会在 mutate 调用点报 TS2322(boolean 不可赋给 string)
    mutationFn: (values: Record<string, string | boolean>) => {
      if (!file) throw new Error('请先选择模特图')
      return modelTemplatesApi.create(file, values)
    },
    onSuccess: () => {
      message.success('模特模板已添加')
      setOpen(false)
      setFile(null)
      form.resetFields()
      queryClient.invalidateQueries({ queryKey: ['model-templates'] })
    },
    onError: onWriteError,
  })

  /**
   * 改授权字段(§11)。**PATCH 语义:只发改过的键。**
   *
   * 整份表单原样回发的话,一次改备注会把 `license_expires_at` 之类
   * 没碰过的字段也写一遍 —— 值相同时后端不记审计,但 dayjs 对象与库里
   * 的字符串来回转换总有偏差,那种"我没改它却变了"最难查。
   */
  const update = useMutation({
    mutationFn: ({ id, patch }: { id: string; patch: Record<string, unknown> }) =>
      modelTemplatesApi.update(id, patch),
    onSuccess: () => {
      message.success('授权信息已保存')
      setEditing(null)
      queryClient.invalidateQueries({ queryKey: ['model-templates'] })
    },
    onError: onWriteError,
  })

  const toggle = useMutation({
    mutationFn: ({ id, enabled }: { id: string; enabled: boolean }) =>
      modelTemplatesApi.setEnabled(id, enabled),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['model-templates'] }),
    onError: onWriteError,
  })

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader title="模特模板" subtitle="上传与启停生成时可选的模特" />

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space>
          <Button type="primary" icon={<PlusOutlined />} onClick={() => setOpen(true)}>
            添加模特
          </Button>
          <span style={{ color: brandVars.slate }}>停用的模板不会出现在创建任务的下拉里。</span>
        </Space>
      </Card>

      <Card size="small">
        {/*
          * 拉不到模板**不等于**一个模板都没建(FE-TEMPLATE-01 / 阶段 A 验收第 6 条)。
          * 空态那句话会让运营去新建一个已经存在的模板,而模板是带图上传的 ——
          * 建重了之后创建任务的下拉里会出现两个同名项,谁也说不清该选哪个。
          */}
        {query.isError ? (
          <ErrorNotice
            title="拉不到模特模板"
            error={query.error}
            onRetry={() => query.refetch()}
            retrying={query.isFetching}
          />
        ) : query.data?.length ? (
          <div className="asset-grid">
            {query.data.map((t: ModelTemplate) => (
              <figure key={t.id} className="asset-tile" style={{ margin: 0 }}>
                {/* 走查 P0-6:原本是 cover,而模特模板之间的差异恰恰在**体型与姿态** ——
                    裁掉头和脚之后几个模板长得一样,选哪个都行等于没得选 */}
                <Image
                  src={t.url}
                  alt={`模特模板 ${t.name} · ${POSE_LABEL[t.pose] ?? t.pose} · ${BODY_TYPE_LABEL[t.body_type] ?? t.body_type}`}
                  preview={{ mask: '查看大图' }}
                />
                <figcaption>
                  <div style={{ fontWeight: 600, marginBottom: 4 }}>{t.name}</div>
                  <Space size={4} wrap style={{ marginBottom: 6 }}>
                    {/* §10.3:模特卡片必须展示受众。它不是可以折叠进"更多信息"
                        的字段 —— 选模特时看不到受众,§10.5 的硬约束就只剩
                        后端一道。§21.3:文字标签,不用性别符号或颜色编码 */}
                    <Tag>{AUDIENCE_LABEL[t.audience as Audience] ?? t.audience}</Tag>
                    <Tag>{POSE_LABEL[t.pose] ?? t.pose}</Tag>
                    <Tag>{BODY_TYPE_LABEL[t.body_type] ?? t.body_type}</Tag>
                    {t.license_status && t.license_status !== 'LICENSED' ? (
                      <BrandTag tone={t.license_status === 'UNVERIFIED' ? 'warning' : 'danger'}>
                        {MODEL_LICENSE_STATUS_LABEL[t.license_status] ?? t.license_status}
                      </BrandTag>
                    ) : null}
                    {t.age_verified === false ? <BrandTag tone="warning">年龄未确认</BrandTag> : null}
                    {t.tags.map((tag) => <BrandTag key={tag} tone="accent">{tag}</BrandTag>)}
                  </Space>
                  <Space size={8}>
                    <span>
                      <Switch
                        size="small"
                        checked={t.enabled}
                        loading={toggle.isPending}
                        onChange={(enabled) => toggle.mutate({ id: t.id, enabled })}
                      />
                      <span style={{ marginLeft: 6 }}>{t.enabled ? '启用' : '停用'}</span>
                    </span>
                    {/* §11 的授权字段在这之前没有任何写入入口 */}
                    <Button
                      size="small"
                      type="link"
                      icon={<EditOutlined />}
                      style={{ padding: 0 }}
                      onClick={() => setEditing(t)}
                    >
                      授权
                    </Button>
                  </Space>
                </figcaption>
              </figure>
            ))}
          </div>
        ) : (
          <Empty description="还没有模特模板。添加几个不同姿势和体型的,生成时可以按款式挑选。" />
        )}
      </Card>

      <Modal
        open={open}
        title="添加模特模板"
        okText="添加"
        cancelText="取消"
        confirmLoading={create.isPending}
        onCancel={() => setOpen(false)}
        onOk={() =>
          form.validateFields().then((values) =>
            create.mutate({
              name: values.name,
              // 受众无默认值:后端也拒绝缺省(§10.5)。表单里它是 required,
              // 所以这里不写 `?? 'WOMEN'` —— 那个兜底正是要防的东西
              audience: values.audience,
              pose: values.pose ?? 'UNKNOWN',
              body_type: values.body_type ?? 'UNKNOWN',
              background: values.background ?? 'studio',
              tags: JSON.stringify(values.tags ?? []),
              notes: values.notes ?? '',
              // ---- §11 授权字段组 ----
              // 这些之前一个都不发,于是 license_status 永远停在 UNVERIFIED,
              // 而 assert_usable 对非 LICENSED 提前 return —— 授权闸口
              // 在产品里永远不会触发任何一次阻断
              age_verified: values.age_verified ?? false,
              source: values.source ?? '',
              license_status: values.license_status ?? 'UNVERIFIED',
              commercial_scope: values.commercial_scope ?? '',
              allowed_platforms: JSON.stringify(values.allowed_platforms ?? []),
              allowed_regions: JSON.stringify(values.allowed_regions ?? []),
              license_start_at: values.license_start_at
                ? values.license_start_at.toISOString() : '',
              license_expires_at: values.license_expires_at
                ? values.license_expires_at.toISOString() : '',
              allow_ai_dressing: values.allow_ai_dressing ?? false,
              allow_derivative_images: values.allow_derivative_images ?? false,
              prohibited_categories: JSON.stringify(values.prohibited_categories ?? []),
            }),
          )
        }
      >
        <Form form={form} layout="vertical" size="small" style={{ marginTop: 12 }}>
          <Form.Item label="模特图" required>
            <input
              type="file"
              accept="image/jpeg,image/png,image/webp"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
            />
          </Form.Item>
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '给这个模特起个名字' }]}>
            <Input placeholder="亚洲 · 长发 · 正面站姿" />
          </Form.Item>
          <Form.Item
            name="audience"
            label="受众"
            rules={[{ required: true, message: '模特必须标注受众' }]}
            extra="选定后体型选项会跟着变;中性是商品的取值,不是模特的"
          >
            <Select
              options={MODEL_AUDIENCES.map((v) => ({
                value: v,
                label: AUDIENCE_LABEL[v as Audience],
              }))}
              onChange={() => form.setFieldValue('body_type', 'UNKNOWN')}
            />
          </Form.Item>
          <Space.Compact block>
            <Form.Item name="pose" label="姿势" style={{ width: '50%', marginRight: 8 }}>
              <Select options={POSES.map((v) => ({ value: v, label: POSE_LABEL[v] }))} />
            </Form.Item>
            {/* 体型选项按受众过滤(§10.4)。选了男装还能选到 CURVY 的话,
                那个操作就"可以做到"了 —— 后端会拒,但错误说在提交之后 */}
            <Form.Item noStyle shouldUpdate={(prev, next) => prev.audience !== next.audience}>
              {({ getFieldValue }) => {
                const picked = getFieldValue('audience') as 'WOMEN' | 'MEN' | undefined
                const options = picked ? BODY_TYPES_BY_AUDIENCE[picked] : BODY_TYPES
                return (
                  <Form.Item name="body_type" label="体型" style={{ width: '50%' }}>
                    <Select
                      disabled={!picked}
                      placeholder={picked ? undefined : '先选受众'}
                      options={options.map((v) => ({ value: v, label: BODY_TYPE_LABEL[v] ?? v }))}
                    />
                  </Form.Item>
                )
              }}
            </Form.Item>
          </Space.Compact>
          {/* 背景词会拼进出图提示词,所以填英文 —— 与商品的颜色字段同一个道理 */}
          <Form.Item name="background" label="背景" extra="填英文,会直接进出图提示词">
            <Input placeholder="例如 studio" />
          </Form.Item>
          <Form.Item name="tags" label="标签">
            <Select mode="tags" placeholder="回车添加" />
          </Form.Item>
          <Form.Item name="notes" label="备注">
            <Input.TextArea rows={2} placeholder="停用原因、拍摄批次…" />
          </Form.Item>

          <LicenseFields />
        </Form>
      </Modal>

      {/*
        * 编辑弹窗。**它存在的唯一理由是让 §11 的授权字段可写。**
        *
        * 受众、体型与模特图不在这里:受众是模特本人的属性(改它等于换一个人,
        * 而历史图片仍然溯源到这一行),体型跟着受众走(§10.4),
        * 图是这条记录的身份。要改这三样就新建一个模板。
        */}
      <Modal
        open={editing !== null}
        title={editing ? `编辑授权 · ${editing.name}` : ''}
        okText="保存"
        cancelText="取消"
        width={620}
        confirmLoading={update.isPending}
        onCancel={() => setEditing(null)}
        destroyOnClose
        onOk={() =>
          editForm.validateFields().then((values) => {
            if (!editing) return
            /*
             * **真的只发改过的键。** 上面 update mutation 的注释一直这么承诺,
             * 实现却把整份表单快照原样回发 —— 于是 A 只改备注保存时,会把
             * B 刚撤销的 license_status、日期、范围整组覆盖回 A 打开弹窗时
             * 的旧值,而 A 对此毫不知情。后端没有版本号/CAS,前端不发的键
             * 就是唯一的保护。逐键与打开弹窗时的快照(editing)比对:
             * 日期按时间点比(dayjs 往返会改字符串形状),数组按内容比,
             * 空串与 null 视为同一种"空"。
             */
            const next: Record<string, unknown> = {
              name: values.name,
              pose: values.pose,
              background: values.background,
              tags: values.tags ?? [],
              notes: values.notes ?? '',
              source: values.source ?? '',
              license_status: values.license_status,
              commercial_scope: values.commercial_scope ?? null,
              allowed_platforms: values.allowed_platforms ?? [],
              allowed_regions: values.allowed_regions ?? [],
              license_start_at: values.license_start_at
                ? values.license_start_at.toISOString() : null,
              license_expires_at: values.license_expires_at
                ? values.license_expires_at.toISOString() : null,
              allow_ai_dressing: values.allow_ai_dressing ?? false,
              allow_derivative_images: values.allow_derivative_images ?? false,
              age_verified: values.age_verified ?? false,
              prohibited_categories: values.prohibited_categories ?? [],
            }
            const unchanged = (key: string, before: unknown, after: unknown): boolean => {
              if (key === 'license_start_at' || key === 'license_expires_at') {
                const ta = before ? dayjs(before as string).valueOf() : null
                const tb = after ? dayjs(after as string).valueOf() : null
                return ta === tb
              }
              if (Array.isArray(before) || Array.isArray(after)) {
                return JSON.stringify(before ?? []) === JSON.stringify(after ?? [])
              }
              if ((before ?? '') === '' && (after ?? '') === '') return true
              return (before ?? null) === (after ?? null)
            }
            const patch = Object.fromEntries(
              Object.entries(next).filter(
                ([key, value]) =>
                  !unchanged(key, (editing as unknown as Record<string, unknown>)[key], value),
              ),
            )
            if (Object.keys(patch).length === 0) {
              // 后端对空 PATCH 会 422(「没有要修改的字段」);没改就直接收起
              setEditing(null)
              return
            }
            update.mutate({ id: editing.id, patch })
          })
        }
      >
        <Typography.Paragraph type="secondary" style={{ fontSize: fontScale.meta }}>
          授权状态改为“已授权”后，授权范围内的限制（是否允许 AI 换装、
          禁止使用的品类）将开始强制执行；在此之前，这些内容仅作记录。
        </Typography.Paragraph>
        <Form form={editForm} layout="vertical" size="small">
          <Form.Item name="name" label="名称" rules={[{ required: true, message: '名称不能为空' }]}>
            <Input />
          </Form.Item>
          <Space.Compact block>
            <Form.Item name="pose" label="姿势" style={{ width: '50%', marginRight: 8 }}>
              <Select options={POSES.map((v) => ({ value: v, label: POSE_LABEL[v] }))} />
            </Form.Item>
            <Form.Item name="background" label="背景" style={{ width: '50%' }}>
              <Input />
            </Form.Item>
          </Space.Compact>
          <Form.Item name="tags" label="标签">
            <Select mode="tags" placeholder="回车添加" />
          </Form.Item>
          <Form.Item name="notes" label="备注">
            <Input.TextArea rows={2} />
          </Form.Item>

          <LicenseFields />
        </Form>
      </Modal>
    </Space>
  )
}
