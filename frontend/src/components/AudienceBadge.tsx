/**
 * 受众标签与重点检查项。**审阅相关的每一块屏幕都要用它**(PRD v2 §13.2 / §19)。
 *
 * ## 为什么抽成组件
 *
 * 上一版这两段代码写在 `FlowHeader` 里,注释也明确引了 §13.2 —— 但
 * `FlowHeader` **只被 `WorkbenchProductPage` 一个页面使用**。真正做审阅
 * 判断的那几块屏幕(逐件快审、单图评分详情、审核队列)受众出现次数是 0。
 *
 * 跨语言契约测试当时断言的是"至少一个组件读了 `product.review_focus`",
 * `FlowHeader` 满足了它,所以测试是绿的。那道网挡得住"完全没接线",
 * 挡不住"接在了不是审阅页的地方" —— 于是「男装该看腰头和裤长」这件事,
 * 在实际审图的那块屏幕上不会显示。
 *
 * §13.4 第 5 条(模特受众与商品受众一致,且呈现为成年人)是运营必须
 * 逐张执行的判断。**审阅时不知道这是男装还是女装,那一条就无法执行。**
 * 它排在八条判断标准的中间而不是最后,因为它一旦不成立,
 * 后面几条判断都没有意义。
 */
import { Space, Tag, Tooltip, Typography } from 'antd'
import { brandVars, fontScale } from '../theme'
import BrandTag from './BrandTag'

/** 这个组件只要求两个字段,所以不绑 `WorkbenchProduct` —— 评分详情页
 *  手上的对象形状不同,但一样有受众。 */
export interface AudienceBearing {
  audience: string | null
  audience_label?: string
  review_focus?: string[]
}

/**
 * 受众标签。
 *
 * §21.3:**文字标签,不用性别符号或颜色编码。** 符号跨站点含义不稳定,
 * 粉/蓝在部分市场是负面信号,而这个系统的输出最终面向多个国家。
 * 所以这里只有"待确认"那一档带颜色 —— 它标的不是受众,是"这个字段还没填"。
 *
 * 待确认用醒目色是刻意的:那不是一个可以放着不管的状态。§8.2 把它排在
 * 待选模特之前 —— 受众定了,模特候选集才是对的。
 */
export function AudienceTag({ product }: { product: AudienceBearing }) {
  const label = product.audience_label || (product.audience ?? '待确认')
  return (
    <Tooltip
      title={
        product.audience
          ? '商品受众。它决定模特候选集、检查项与导出模板'
          : '受众未确认。它决定这一屏该重点看什么 —— 先去商品资料确认'
      }
    >
      {product.audience ? (
        <Tag>{label}</Tag>
      ) : (
        <BrandTag tone="warning">{label}</BrandTag>
      )}
    </Tooltip>
  )
}

/**
 * 重点检查项(§19)。**从规则包下发,前端不维护一份受众到检查项的映射** ——
 * 维护两份的后果是规则包改了检查项、界面还在提示旧的那几项,而且不会报错。
 *
 * 读不出来(受众待确认 / spec 缺失)时后端给空数组:那时这一行不出现,
 * 而不是显示一份猜出来的清单。
 */
export function ReviewFocusLine({
  product,
  compact = false,
}: {
  product: AudienceBearing
  compact?: boolean
}) {
  if (!product.review_focus?.length) {
    // 受众待确认时说一句为什么没有清单 —— 空白读起来像"没有要检查的",
    // 而实际含义是"还不知道该检查什么"
    if (!product.audience) {
      return (
        <Typography.Text
          type="secondary"
          style={{ fontSize: compact ? fontScale.meta : fontScale.body }}
        >
          受众未确认,暂无重点检查清单
        </Typography.Text>
      )
    }
    return null
  }
  return (
    <Tooltip title="按商品受众下发的重点检查区域,来自该受众的规则包">
      <div
        style={{
          color: brandVars.textMuted,
          fontSize: compact ? fontScale.meta : fontScale.body,
        }}
      >
        重点检查:{product.review_focus.join(' · ')}
      </div>
    </Tooltip>
  )
}

/**
 * 两者合成一块。审阅页左栏用这个 —— §13.2 要求受众**常驻显示在左栏
 * 商品信息里**,它不是一个可以折叠进"更多信息"的字段。
 */
export default function AudienceBadge({
  product,
  compact = false,
}: {
  product: AudienceBearing
  compact?: boolean
}) {
  return (
    <Space direction="vertical" size={2} style={{ width: '100%' }}>
      <AudienceTag product={product} />
      <ReviewFocusLine product={product} compact={compact} />
    </Space>
  )
}
