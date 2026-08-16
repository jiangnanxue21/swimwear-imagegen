/**
 * §9.4 受众与品类确认 —— **同屏确认,一次完成**。
 *
 * ## 为什么这个组件必须存在
 *
 * 上一轮的审视第 7 条说得很准:`FlowHeader` 会把没受众的商品标成橙色
 * "待确认",但流程里没有对应的动作码,首页也没有这一档 —— 那个橙色标记
 * **是个死胡同**:它告诉你有问题,界面上没有任何地方能解决它。
 *
 * batch10 补了动作码 `CONFIRM_AUDIENCE`,却把它指向属性标签页 ——
 * 而属性页当时没有受众控件,`ProductFormModal` 只挂在 `/products` 那两页上。
 * **那只是把死胡同挪了个位置。** 这个组件是真正的出口。
 *
 * ## 为什么受众和品类不拆两步
 *
 * §9.4 原话:它们通常一起对或一起错,拆开会让运营连点两次"正确",
 * 第二次就不看了。所以这里一屏两项、一次提交。
 *
 * ## 为什么没有"系统识别"的建议值
 *
 * §9.4 描述的交互是「系统识别:受众 男装 / 品类 沙滩裤 [识别正确,继续]
 * [修改]」。**本系统目前不识别受众** —— 属性识别器的字段集本身就按受众
 * 条件化(§18.3),受众未定时它给不出一个可信的建议。给一个猜出来的
 * 默认值正是 §9.1 明令禁止的("不允许按文件名或类目猜一个默认值填进去,
 * 那会让 §4.2 的保护形同虚设")。
 *
 * 所以这里走 §9.4 的**第二种形态**:置信度低时不给建议,直接问
 * 「无法确定这件商品的受众。请选择:[女装] [男装] [中性]」。
 */
import { App, Alert, Button, Card, Select, Space, Typography } from 'antd'
import { useState } from 'react'
import { useMutation, useQueryClient } from '@tanstack/react-query'
import { productsApi } from '../../api/products'
import { useWriteError } from '../../hooks/useWriteError'
import {
  AUDIENCES,
  AUDIENCE_LABEL,
  GARMENT_TYPES,
  GARMENT_TYPES_BY_AUDIENCE,
  garmentTypeOptions,
  type Audience,
} from '../../api/types'
import { brandVars, fontScale } from '../../theme'

export default function AudienceConfirmCard({
  productId,
  audience,
  garmentType,
}: {
  productId: string
  /** null = 待确认。**不是 UNISEX** —— UNISEX 是一个明确的业务判断 */
  audience: Audience | null
  garmentType?: string
}) {
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const [picked, setPicked] = useState<Audience | undefined>(audience ?? undefined)
  const [pickedType, setPickedType] = useState<string | undefined>(garmentType)

  const refresh = () => {
    // 受众一改,必填属性集(§18.3)、规则包、检查项、模特候选集全都变了。
    // 这几个 key 少失效一个,界面上就会留着一份按旧受众算出来的结论
    queryClient.invalidateQueries({ queryKey: ['workbench-flow', productId] })
    queryClient.invalidateQueries({ queryKey: ['workbench'] })
    queryClient.invalidateQueries({ queryKey: ['attributes', productId] })
    queryClient.invalidateQueries({ queryKey: ['product', productId] })
    queryClient.invalidateQueries({ queryKey: ['products'] })
    queryClient.invalidateQueries({ queryKey: ['model-templates'] })
  }

  const onWriteError = useWriteError(refresh)

  const save = useMutation({
    mutationFn: (next: { audience: Audience; garment_type?: string }) =>
      productsApi.update(productId, next),
    onSuccess: () => {
      message.success('受众已确认')
      refresh()
    },
    onError: onWriteError,
  })

  const submit = () => {
    if (!picked) return
    /*
     * garment_type 必须**显式**出现在请求体里。
     *
     * 换受众时上面的 onChange 会把越界品类清成 undefined,而
     * JSON.stringify 会把 undefined 的键整个丢掉 —— PATCH 里于是没有
     * garment_type,后端按「未传不改」处理:界面显示品类已清空,
     * 数据库里仍是 MEN + BIKINI_SET。与 ProductFormModal 对 audience
     * 的 null 归一是同一个坑(A45 batch10 修了那半,漏了这半)。
     * 后端 garment_type 列 NOT NULL,清空语义 = 写回中性值 OTHER。
     */
    const payload = {
      audience: picked,
      garment_type: pickedType ?? 'OTHER',
    }
    // §20.5:修改**已确认**的受众要二次确认,并提示会作废已生成的图片。
    // 从"待确认"填上一个值不走这道确认 —— 那不是改写,是本来就该做的那一步
    if (audience && picked !== audience) {
      modal.confirm({
        title: '确认修改受众?',
        okText: '确认修改',
        cancelText: '取消',
        okButtonProps: { danger: true },
        content: (
          <div>
            <p>
              受众将从「{AUDIENCE_LABEL[audience]}」改为「{AUDIENCE_LABEL[picked]}」。
            </p>
            <p>这会作废已生成的图片(它们用的是原受众的模特),并让已有草稿过期。</p>
          </div>
        ),
        onOk: () => save.mutate(payload),
      })
      return
    }
    save.mutate(payload)
  }

  const typeOptions = picked ? GARMENT_TYPES_BY_AUDIENCE[picked] : GARMENT_TYPES

  return (
    <Card
      size="small"
      style={{
        marginBottom: 12,
        borderColor: audience ? brandVars.line : brandVars.sand,
      }}
      title={
        <Space size={8}>
          <span>受众与品类</span>
          {!audience && (
            <Typography.Text style={{ color: brandVars.sand, fontSize: fontScale.meta }}>
              待确认
            </Typography.Text>
          )}
        </Space>
      }
    >
      {!audience && (
        <Alert
          type="warning"
          showIcon
          style={{ marginBottom: 12 }}
          message="无法确定这件商品的受众,请选择"
          description={
            <>
              系统不替这件商品猜一个受众 —— 按类目或文件名猜出来的值会被当成
              人工确认过的事实(§9.1)。受众决定<b>模特候选集、必填属性、
              检查项与导出模板</b>,所以它排在选模特之前(§8.2)。
            </>
          }
        />
      )}

      {/* 一屏两项、一次提交(§9.4:不拆两步) */}
      <Space wrap align="end" size={12}>
        <div>
          <div style={{ fontSize: fontScale.meta, color: brandVars.slate, marginBottom: 4 }}>
            受众
          </div>
          <Select<Audience>
            style={{ width: 160 }}
            placeholder="请选择"
            value={picked}
            onChange={(v) => {
              setPicked(v)
              // 换受众时清掉不属于新受众的品类 —— 与 §10.4 体型联动同一个道理
              if (pickedType && !GARMENT_TYPES_BY_AUDIENCE[v].includes(pickedType)) {
                setPickedType(undefined)
              }
            }}
            options={AUDIENCES.map((v) => ({ value: v, label: AUDIENCE_LABEL[v] }))}
          />
        </div>
        <div>
          <div style={{ fontSize: fontScale.meta, color: brandVars.slate, marginBottom: 4 }}>
            品类
          </div>
          <Select
            style={{ width: 200 }}
            placeholder={picked ? '请选择' : '先选受众'}
            disabled={!picked}
            value={pickedType}
            onChange={setPickedType}
            options={garmentTypeOptions(typeOptions)}
          />
        </div>
        <Button
          type="primary"
          disabled={!picked || (picked === audience && pickedType === garmentType)}
          loading={save.isPending}
          onClick={submit}
        >
          {audience ? '保存修改' : '确认,继续'}
        </Button>
      </Space>
    </Card>
  )
}
