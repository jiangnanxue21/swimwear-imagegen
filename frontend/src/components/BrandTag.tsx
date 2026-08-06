/**
 * 品牌色标签(走查 M-1)。
 *
 * ## 为什么需要它
 *
 * `theme.ts` 整篇在讲「消除色值漂移」,并且把 165 处硬编码 hex 收进了令牌。
 * 但漏掉一整类:**antd 的调色板预设色不受任何令牌控制。**
 *
 *     <Tag color="error|warning|success|default">     受控 —— 走 theme.token.colorXxx
 *     <Tag color="blue|green|orange|gold|volcano">    不受控 —— antd 自己那套调色板
 *
 * 走查时后一类有 13 处。最刺眼的一处在图片集图块上:
 *
 *     borderColor: brandVars.accent    // #3F6C8F 沉静蓝
 *     <Tag color="blue">主图</Tag>      // antd 亮蓝,就贴在上面
 *
 * 同一个「主图」概念,边框一个蓝、标签另一个蓝,而且是紧挨着的两个蓝 ——
 * 正是 theme.ts 那段注释想根除的那种漂移。
 *
 * ## 为什么不直接 `<Tag color={brandVars.marine}>`
 *
 * 因为那会换掉标签的**形态**,不只是颜色。antd 的 Tag 认到预设名时渲染「浅底 +
 * 彩字 + 彩边」,认到任意色值时渲染「实底 + 白字」。表格里每格塞一个实底色块,
 * 密度一上来整页会花掉 —— 而这套界面的第一条设计原则是「主色让位给图片」。
 *
 * 所以这里手工拼出浅色形态:用 `color-mix()` 从同一个 CSS 变量派生底色与边色。
 * 派生而不是再定义一套浅色令牌,是为了不制造第二处事实来源 ——
 * 暗色模式切换时 `--brand-*` 一变,这里三个值一起跟着变,不需要任何额外代码。
 */
import { Tag } from 'antd'
import type { ReactNode } from 'react'
import { brandVars, type BrandToken } from '../theme'

interface Props {
  /** 用哪个品牌令牌。底色与边色由它派生 */
  tone: BrandToken
  children: ReactNode
  /** 透传给 antd Tag */
  style?: React.CSSProperties
  title?: string
  onClick?: () => void
}

export default function BrandTag({ tone, children, style, title, onClick }: Props) {
  const c = brandVars[tone]
  return (
    <Tag
      title={title}
      onClick={onClick}
      style={{
        color: c,
        // 10% / 35% 是对着 antd 预设浅色标签调的,两个模式下都试得住:
        // 底色够浅不抢图片,边色够实能在 surfaceAlt 的表头底上分出来
        background: `color-mix(in srgb, ${c} 10%, transparent)`,
        borderColor: `color-mix(in srgb, ${c} 35%, transparent)`,
        marginInlineEnd: 0,
        cursor: onClick ? 'pointer' : undefined,
        ...style,
      }}
    >
      {children}
    </Tag>
  )
}
