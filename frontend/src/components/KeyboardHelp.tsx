/**
 * 快捷键速查(走查 P1-6)。
 *
 * ## 要修的那件事:说明常驻,占的是图的位置
 *
 * 走查前,逐件快审把 J/K/A/R/Enter 的说明连同「为什么不提供批量批准」
 * 一起写成页顶一段四行小字,**常驻**。那段话第一天很有价值,第二天起就是噪音 ——
 * 而它占着约 80px,把这一页真正的主角(300px 判定档候选图)每次都往下推。
 * 一个以「一屏一件」为设计目标的页面,不该有 80px 的常驻说明。
 *
 * 现在:常驻只留一行按键本身,规则说明收进这个弹窗,`?` 唤起。
 *
 * ## 为什么规则说明也放进来
 *
 * 「系统不提供批量批准」「跳过和退回的区别」是**需要时才查**的东西,
 * 不是每天都要读的东西。但它们又不能删 —— 新人第二周想不起 R 和 J 的区别时,
 * 得有个地方能查到,否则他会去猜,而猜错的代价是把该退回的件跳过了。
 */
import { Modal, Space, Table, Typography } from 'antd'
import { brandVars, fontScale } from '../theme'

export interface ShortcutRow {
  keys: string[]
  action: string
  /** 这个键**改变什么**。说不清就别加这个键 */
  effect: string
}

/** 逐件快审的按键表。这一份就是页面上那一行小字的完整版 */
export const REVIEW_SHORTCUTS: ShortcutRow[] = [
  { keys: ['J'], action: '下一件', effect: '什么都不改。这一件仍然停在待批准,下一轮回到队列排在原处' },
  { keys: ['K'], action: '上一件', effect: '什么都不改' },
  { keys: ['A'], action: '批准', effect: '批准当前的图片集或文案,这一件离开队列' },
  { keys: ['R'], action: '退回', effect: '打开退回弹窗。退回会改变判定结果,并按原因给出明确的下一步' },
  { keys: ['Enter'], action: '打开完整详情页', effect: '离开快审,到八标签详情页' },
  { keys: ['?'], action: '打开这张表', effect: '' },
]

function Keycap({ children }: { children: string }) {
  return (
    <kbd
      style={{
        display: 'inline-block',
        minWidth: 22,
        padding: '1px 6px',
        textAlign: 'center',
        fontSize: fontScale.meta,
        fontFamily: 'ui-monospace, Menlo, Consolas, monospace',
        border: `1px solid ${brandVars.lineStrong}`,
        borderBottomWidth: 2,
        borderRadius: 3,
        background: brandVars.surfaceAlt,
        color: brandVars.ink,
      }}
    >
      {children}
    </kbd>
  )
}

export default function KeyboardHelp({
  open,
  onClose,
  rows = REVIEW_SHORTCUTS,
}: {
  open: boolean
  onClose: () => void
  rows?: ShortcutRow[]
}) {
  return (
    <Modal
      open={open}
      onCancel={onClose}
      onOk={onClose}
      okText="知道了"
      cancelButtonProps={{ style: { display: 'none' } }}
      title="快捷键与规则"
      width={620}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Table<ShortcutRow>
          rowKey={(r) => r.keys.join('+')}
          size="small"
          bordered
          pagination={false}
          dataSource={rows}
          columns={[
            {
              title: '按键',
              key: 'keys',
              width: 90,
              render: (_, r) => (
                <Space size={4}>
                  {r.keys.map((k) => (
                    <Keycap key={k}>{k}</Keycap>
                  ))}
                </Space>
              ),
            },
            { title: '动作', dataIndex: 'action', width: 130 },
            {
              title: '改变什么',
              dataIndex: 'effect',
              render: (v: string) => (
                <span style={{ fontSize: fontScale.body, color: brandVars.slate }}>{v || '—'}</span>
              ),
            },
          ]}
        />

        <div>
          <div className="section-label">两条规则</div>
          <Typography.Paragraph style={{ fontSize: fontScale.body, marginBottom: 6 }}>
            <b>不提供批量批准,也不提供批量退回。</b>
            批准和退回都是人对内容负责的动作,一次点击处理 50 件等于没审。
            这一页省掉的只是「找」和「跳」,该看的仍然要看。
          </Typography.Paragraph>
          <Typography.Paragraph style={{ fontSize: fontScale.body, marginBottom: 0 }}>
            <b>跳过(J)和退回(R)是两件事。</b>
            跳过什么都不改;退回会把这一件移出待批准队列,并按你选的原因
            给出明确的下一步,带进异常列表。选错原因就是选错下一步。
          </Typography.Paragraph>
        </div>

        <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
          焦点在输入框里时快捷键不生效;弹窗打开时这一页交出键盘。
        </Typography.Text>
      </Space>
    </Modal>
  )
}
