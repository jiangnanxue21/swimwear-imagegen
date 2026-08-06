import { useEffect, useState } from 'react'
import { Alert, Input, Modal, Space, Typography } from 'antd'
import { RECONCILIATION_NOTE_MAX } from '../api/generation'
import { fontScale } from '../theme'

/**
 * 「提交结果未知」的人工对账出口(BLOCK-06 的前端半边)。
 *
 * ## 为什么是一个受控 Modal,不是 `modal.confirm`
 *
 * 这个弹窗里有一个**必填输入**,而 `modal.confirm` 的 content 是渲染一次的
 * 静态节点:要让「确定」按钮跟着输入内容变灰变亮,只能去调实例的 `update()`,
 * 或者在 `onOk` 里返回一个被拒的 Promise 把弹窗留住 —— 后者的行为取决于
 * antd 内部实现,而本仓库刚在上一批里因为赌它踩过一次(见 a28 前端交付说明 §6.4)。
 * 受控 Modal 没有这两个问题,代价只是多几行 state。
 *
 * ## 为什么这个输入必须是必填
 *
 * 后端 `services/generation_service.retry_task()` 对 `force=true` 校验
 * `reconciliation_note` 非空,空的话 422。所以「不填也能点」在这里不是宽松,
 * 是把一个必然失败的按钮摆出来 —— 而一个永远失败的按钮比没有按钮更糟:
 * 它让人以为这条路走得通,只是今天坏了。
 *
 * 更根本的理由在后端那条注释里:这是全系统唯一一个由人**主动承担重复计费
 * 风险**的动作。出了重复计费之后,审计里如果只有一条和自动重试长得一模一样的
 * "retry",就没人答得上「谁依据什么放的行」。这个输入框就是那个「依据」。
 */
export default function ForceRetryModal({
  open,
  provider,
  externalTaskId,
  submitting,
  onCancel,
  onConfirm,
}: {
  open: boolean
  /** 去哪个 Provider 后台核对。运营要拿它决定开哪个网页 */
  provider: string
  /** 核对依据。为空本身就是一条信息 —— 说明请求可能根本没发出去 */
  externalTaskId: string | null
  submitting: boolean
  onCancel: () => void
  /** 只在 `note` 非空时被调用;`note` 已 trim */
  onConfirm: (note: string) => void
}) {
  const [note, setNote] = useState('')

  // 每次打开都清空:上一条任务的对账结论套用到下一条,等于伪造依据。
  // 用 `open` 而不是 `destroyOnClose` 来清 —— 后者只在卸载时生效,
  // 而关闭动画期间组件还在,期间重新打开就会看到上一次的内容
  useEffect(() => {
    if (open) setNote('')
  }, [open])

  const trimmed = note.trim()

  return (
    <Modal
      open={open}
      width={560}
      title="这条任务的提交结果未知,确认要强制重试吗?"
      okText="我已核对,强制重试"
      cancelText="先去核对"
      okButtonProps={{ danger: true, disabled: !trimmed }}
      confirmLoading={submitting}
      // 提交中不许从遮罩或 ESC 关掉:关掉了请求还在飞,而运营会以为自己取消了
      maskClosable={!submitting}
      keyboard={!submitting}
      onCancel={onCancel}
      onOk={() => onConfirm(trimmed)}
    >
      <Space direction="vertical" size={10} style={{ width: '100%' }}>
        <Typography.Text style={{ fontSize: fontScale.body }}>
          提交阶段网络超时,系统无法确认 Provider 是否已经受理这次生成。
          <strong>直接重试可能再买一次。</strong>
        </Typography.Text>

        <Typography.Text style={{ fontSize: fontScale.body }}>
          请先到 <strong>{provider}</strong> 后台按下面的外部任务 ID 核对,
          确认没有产生结果后再继续:
        </Typography.Text>

        {externalTaskId ? (
          <Typography.Text className="mono" copyable={{ text: externalTaskId }}>
            {externalTaskId}
          </Typography.Text>
        ) : (
          <Alert
            type="info"
            showIcon
            message="没有外部任务 ID"
            description="说明请求可能根本没发出去。仍然建议到 Provider 后台按 SKU 与时间确认一次 —— 没有 ID 不等于没有提交。"
          />
        )}

        <div>
          <Typography.Text strong style={{ fontSize: fontScale.body }}>
            对账结论(必填)
          </Typography.Text>
          <Typography.Paragraph type="secondary" style={{ fontSize: fontScale.meta, marginBottom: 6 }}>
            写清楚你在 Provider 后台看到了什么。这句话会进审计,是日后追查重复计费时
            唯一能回答「谁依据什么放的行」的记录。
          </Typography.Paragraph>
          <Input.TextArea
            value={note}
            onChange={(e) => setNote(e.target.value)}
            maxLength={RECONCILIATION_NOTE_MAX}
            showCount
            rows={3}
            autoFocus
            placeholder="例:后台任务列表中查无此 ID,当日该 SKU 无任何扣费记录"
          />
        </div>
      </Space>
    </Modal>
  )
}
