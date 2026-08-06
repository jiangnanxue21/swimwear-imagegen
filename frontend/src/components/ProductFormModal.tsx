import { App, Form, Input, Modal, Select, Space } from 'antd'
import { useEffect } from 'react'
import {
  AUDIENCES, AUDIENCE_LABEL, COMPLEXITIES, GARMENT_TYPES,
  GARMENT_TYPES_BY_AUDIENCE, MAX_SECONDARY_COLORS, MAX_SECONDARY_COLOR_LENGTH,
  PATTERN_TYPES, PRODUCT_FIELD_LIMITS, type Audience, type Product,
} from '../api/types'

interface Props {
  open: boolean
  /** 传入商品表示编辑;为空表示新增。SPU/SKU 编辑时锁定,避免破坏已生成资产的对应关系。 */
  product?: Product | null
  confirmLoading?: boolean
  onCancel: () => void
  onSubmit: (values: Partial<Product>) => void
}

export default function ProductFormModal({
  open, product, confirmLoading, onCancel, onSubmit,
}: Props) {
  const [form] = Form.useForm()
  const { modal } = App.useApp()
  const editing = Boolean(product)
  //: 编辑前的受众。§20.5 的二次确认只在"已确认的受众被改动"时触发 ——
  //: 从"待确认"填上一个值不是改写,那是本来就该做的那一步(§8.2)
  const originalAudience = product?.audience ?? null

  /**
   * §20.5 最后一条:**修改已确认受众要二次确认,并提示会作废已生成的图片。**
   *
   * 那道确认的位置在界面层 —— 后端 `schemas/product.py` 的注释写得很明白,
   * 它只保证值本身合法。之前界面层根本没有受众输入项,于是这道确认
   * 不存在;而 `product_service` 会把受众改动**传播到同 SPU 的所有 SKU**
   * 并记 `audience_propagated_to` 审计,运营却只看到自己改了一件商品。
   */
  const submit = (values: Partial<Product>) => {
    const next = (values.audience ?? null) as Audience | null
    /*
     * 清空受众必须以 **null** 上线,不能停在 undefined。
     *
     * antd 的 allowClear 清空后 values.audience 是 undefined,而
     * JSON.stringify 会把 undefined 的键整个丢掉 —— PATCH 里于是没有
     * audience,后端按「未传不改」处理。结果是:运营在"作废已生成图片、
     * 同步同 SPU"的危险弹窗上点了确认,接口返回 200,toast 说已保存,
     * 而受众一个字没变。后端对显式 null 的语义本来就是"清空"
     * (schemas/product.py 的 PATCH 注释),这里把意图翻译到位即可。
     */
    /*
     * garment_type 与 audience 是同一个坑的两半:换受众时上面的 onChange
     * 把越界品类清成 undefined,JSON.stringify 会把这个键整个丢掉,
     * 后端按「未传不改」处理 —— 界面显示品类已清空,库里仍是旧品类,
     * 于是 MEN + BIKINI_SET 落库。列是 NOT NULL,清空语义 = 写回 OTHER。
     * (品类下拉没有 allowClear,undefined 只会由换受众联动产生。)
     * 分两步写:第一步是 batch10 钉住的 audience null 归一形状,原样保留。
     */
    const normalized: Partial<Product> = { ...values, audience: next }
    const payload: Partial<Product> = {
      ...normalized,
      garment_type: values.garment_type ?? 'OTHER',
    }
    if (editing && originalAudience && next !== originalAudience) {
      modal.confirm({
        title: '确认修改受众?',
        okText: '确认修改',
        cancelText: '取消',
        okButtonProps: { danger: true },
        content: (
          <div>
            <p>
              受众将从「{AUDIENCE_LABEL[originalAudience]}」改为「
              {next ? AUDIENCE_LABEL[next] : '待确认'}」。
            </p>
            <p>受众决定模特候选集、检查项与导出模板,所以这次改动会:</p>
            <ul style={{ paddingLeft: 18, marginBottom: 8 }}>
              <li>作废已生成的图片 —— 它们用的是原受众的模特</li>
              <li>让已有的上架草稿过期,需要按新受众的规则包重新生成</li>
              <li>同步到同 SPU 的其它颜色款与尺码(后端按 SPU 传播并留审计)</li>
            </ul>
          </div>
        ),
        onOk: () => onSubmit(payload),
      })
      return
    }
    onSubmit(payload)
  }

  useEffect(() => {
    if (open) {
      form.resetFields()
      if (product) form.setFieldsValue(product)
    }
  }, [open, product, form])

  return (
    <Modal
      open={open}
      title={editing ? `编辑 ${product?.sku}` : '新增商品'}
      okText={editing ? '保存修改' : '创建商品'}
      cancelText="取消"
      confirmLoading={confirmLoading}
      onCancel={onCancel}
      width={620}
      destroyOnClose
      onOk={() => form.validateFields().then(submit)}
    >
      <Form form={form} layout="vertical" size="small" style={{ marginTop: 12 }}>
        <Space.Compact block>
          <Form.Item
            name="spu" label="SPU"
            rules={[
              { required: true, whitespace: true, message: '填写 SPU' },
              { max: PRODUCT_FIELD_LIMITS.spu, message: `SPU 最长 ${PRODUCT_FIELD_LIMITS.spu} 字符` },
            ]}
            style={{ width: '50%', marginRight: 8 }}
          >
            <Input disabled={editing} placeholder="SPU-SW-001" />
          </Form.Item>
          <Form.Item
            name="sku" label="SKU"
            rules={[
              { required: true, whitespace: true, message: '填写 SKU' },
              { max: PRODUCT_FIELD_LIMITS.sku, message: `SKU 最长 ${PRODUCT_FIELD_LIMITS.sku} 字符` },
            ]}
            style={{ width: '50%' }}
          >
            <Input disabled={editing} placeholder="SW-001-BLK-S" />
          </Form.Item>
        </Space.Compact>

        <Form.Item
          name="name"
          label="商品名称"
          rules={[
            { required: true, whitespace: true, message: '填写商品名称' },
            { max: PRODUCT_FIELD_LIMITS.name, message: `名称最长 ${PRODUCT_FIELD_LIMITS.name} 字符` },
          ]}
        >
          <Input placeholder="经典三角比基尼套装" />
        </Form.Item>

        {/*
          * 受众(§2.3 / §3.4)。**允许留空** —— 留空的商品进"待确认受众"
          * 队列(§9.1),不允许按类目猜一个默认值填进去,那会让 §4.2
          * 「AI 不得静默覆盖受众」的保护形同虚设。
          *
          * §21.3:文字标签,不用性别符号或颜色编码 —— 符号跨站点含义
          * 不稳定,而这个系统的输出最终面向多个国家。
          */}
        <Form.Item
          name="audience"
          label="受众"
          extra="可以留空;留空的商品会排进「确认受众与品类」。受众决定模特候选集、检查项与导出模板"
        >
          <Select
            allowClear
            placeholder="待确认"
            onChange={(v?: Audience) => {
              // 换受众时清掉不属于新受众的品类 —— 与 AudienceConfirmCard
              // 同一条规则(§10.4 体型联动同理)。只收窄 options 而留着旧值,
              // 等于把「男装商品带着 BIKINI_SET」从下拉里挪进了提交载荷:
              // 后端按枚举全集收(取值本身不分受众),这里是唯一的拦截点
              const gt = form.getFieldValue('garment_type') as string | undefined
              if (v && gt && !GARMENT_TYPES_BY_AUDIENCE[v].includes(gt)) {
                form.setFieldValue('garment_type', undefined)
              }
            }}
            options={AUDIENCES.map((v) => ({ value: v, label: AUDIENCE_LABEL[v] }))}
          />
        </Form.Item>

        <Space.Compact block>
          {/* 服装类型按受众收窄(§2.2)。与体型词表(§10.4)同一个道理:
              给一件男装商品选到 BIKINI_SET 不该是一个可以做到的操作。
              受众未确认时退回全集 —— 那时"该出哪一组"这个问题还没有答案 */}
          <Form.Item noStyle shouldUpdate={(prev, next) => prev.audience !== next.audience}>
            {({ getFieldValue }) => {
              const picked = getFieldValue('audience') as Audience | undefined
              const options = picked ? GARMENT_TYPES_BY_AUDIENCE[picked] : GARMENT_TYPES
              return (
                <Form.Item
                  name="garment_type"
                  label="服装类型"
                  style={{ width: '50%', marginRight: 8 }}
                  extra={picked ? `已按${AUDIENCE_LABEL[picked]}收窄` : undefined}
                >
                  <Select
                    placeholder="选择类型"
                    options={options.map((v) => ({ value: v, label: v }))}
                  />
                </Form.Item>
              )
            }}
          </Form.Item>
          <Form.Item name="pattern_type" label="图案类型" style={{ width: '50%' }}>
            <Select
              placeholder="选择图案"
              options={PATTERN_TYPES.map((v) => ({ value: v, label: v }))}
            />
          </Form.Item>
        </Space.Compact>

        <Space.Compact block>
          <Form.Item
            name="primary_color" label="主颜色" style={{ width: '33%', marginRight: 8 }}
            rules={[{ max: PRODUCT_FIELD_LIMITS.primary_color, message: `最长 ${PRODUCT_FIELD_LIMITS.primary_color} 字符` }]}
          >
            <Input placeholder="black" />
          </Form.Item>
          <Form.Item
            name="secondary_colors" label="辅助颜色" style={{ width: '34%', marginRight: 8 }}
            rules={[{
              validator: (_, value: string[] | undefined) => {
                if (!value?.length) return Promise.resolve()
                if (value.length > MAX_SECONDARY_COLORS) {
                  return Promise.reject(new Error(`辅助颜色最多 ${MAX_SECONDARY_COLORS} 个`))
                }
                const long = value.find((v) => v.trim().length > MAX_SECONDARY_COLOR_LENGTH)
                if (long) return Promise.reject(new Error(`「${long.slice(0, 12)}…」超过 ${MAX_SECONDARY_COLOR_LENGTH} 字符`))
                return Promise.resolve()
              },
            }]}
          >
            <Select mode="tags" placeholder="回车添加" tokenSeparators={[',', '|']} />
          </Form.Item>
          <Form.Item name="complexity" label="复杂度" style={{ width: '33%' }}>
            <Select options={COMPLEXITIES.map((v) => ({ value: v, label: v }))} />
          </Form.Item>
        </Space.Compact>

        <Form.Item
          name="material" label="材质"
          rules={[{ max: PRODUCT_FIELD_LIMITS.material, message: `材质最长 ${PRODUCT_FIELD_LIMITS.material} 字符` }]}
        >
          <Input placeholder="80%锦纶 20%氨纶" />
        </Form.Item>

        <Form.Item
          name="notes"
          label="备注"
          extra="写下这个款式在生成时容易出问题的地方,后续评分校准会用到"
        >
          <Input.TextArea rows={2} placeholder="条纹易扭曲,重点观察图案保真度" />
        </Form.Item>
      </Form>
    </Modal>
  )
}
