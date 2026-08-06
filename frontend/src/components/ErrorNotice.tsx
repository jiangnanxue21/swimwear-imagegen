/**
 * A12:失败提示的统一出口。运营看一句话,管理员能展开技术详情。
 *
 * ## 为什么要有这个组件
 *
 * 判定那一份已经在 `describeError` 里了(client.ts),但**展示**如果还是各页
 * 各写一个 `<Alert description={readError(e)} />`,技术层就永远没有落点 ——
 * 上一版就是这样:错误码、HTTP 状态、请求编号都算得出来,一个都没显示。
 * 于是管理员排查时唯一的手段是打开浏览器控制台,而那正是 A12 想终结的动作。
 *
 * ## 分层的落地
 *
 *     运营        标题 + 一句话 + 请求编号(他要转述它)+ 重试按钮
 *     管理员      上面全部,再加一个折叠的「技术详情」
 *
 * 折叠**默认收起**。管理员多数时候也是在做运营的活,一进页面就迎面一张
 * 错误码表,和当初把 docker 命令写给运营是同一类错误 —— 只是换了个方向。
 *
 * ## 判定不在这里
 *
 * 这个文件不认识 401、不认识 5xx,也不拼任何文案:它只读 `describeError`
 * 的结果。想改"某类失败该说什么",改 client.ts 那一处;在这里加一个
 * `if (status === ...)` 就是把判定分成两份,而两份迟早会各说各话。
 *
 * ## `isAdmin` 是显示层判定
 *
 * 和 A8 菜单同一条口径(`useIdentity`):后端答得上来就听后端的,答不上来
 * 退回"本浏览器填了管理口令没"。可以被本地伪造 —— 但这里被伪造的后果只是
 * 多看见一个错误码和一句排查建议,里面**没有任何秘密**(口令、密钥、异常
 * 原文都不在错误体里)。所以它不需要是权限边界,也不该被做成权限边界。
 */
import type { CSSProperties } from 'react'
import { Alert, Button, Collapse, Descriptions, Space, Typography } from 'antd'
import { describeError, TECHNICAL_ELSEWHERE, type RequestKind } from '../api/client'
import { useIdentity } from '../hooks/useIdentity'
import { brandVars, fontScale } from '../theme'

interface Props {
  /** 原始错误对象。**不要**先 `readError` 再传进来,那样技术层就丢了 */
  error: unknown
  /** 标题:这次是什么没成。给运营用来判断"我刚才那一步成没成" */
  title?: string
  /** 给了就渲染重试按钮。不给就不渲染 —— 有些失败重试没有意义 */
  onRetry?: () => void
  retrying?: boolean
  type?: 'error' | 'warning'
  style?: CSSProperties
  /**
   * 这次失败来自读请求还是写请求(FE-GLOBAL-01)。
   *
   * 只影响"请求没到后端"那一支的话术:写请求超时时不能说"请重试",
   * 因为后端可能已经执行了。判定仍在 `describeError`,这里只是把
   * 调用点知道、而 `describeError` 不可能知道的那一位信息传下去。
   */
  kind?: RequestKind
}

export default function ErrorNotice({
  error,
  title = '这一步没有完成',
  onRetry,
  retrying,
  type = 'error',
  style,
  kind = 'read',
}: Props) {
  const { text, requestId, outcome, technical } = describeError(error, kind)
  const { isAdmin } = useIdentity()

  return (
    <Alert
      type={type}
      showIcon
      style={style}
      message={title}
      description={
        <Space direction="vertical" size={6} style={{ width: '100%' }}>
          <Typography.Text style={{ fontSize: fontScale.body }}>{text}</Typography.Text>

          {/* 请求编号对运营也可见:上一行可能刚要求他把它交给管理员。
              等宽字体是为了让人能照着念,或者一眼看出复制时漏了一位 */}
          {requestId && (
            <Typography.Text
              className="mono"
              copyable={{ text: requestId }}
              style={{ fontSize: fontScale.meta, color: brandVars.slate }}
            >
              请求编号 {requestId}
            </Typography.Text>
          )}

          {isAdmin && (
            <Collapse
              ghost
              size="small"
              items={[
                {
                  key: 'technical',
                  label: <span style={{ fontSize: fontScale.meta }}>技术详情(管理员)</span>,
                  children: (
                    <Descriptions
                      size="small"
                      column={1}
                      items={[
                        { key: 'code', label: '错误码', children: technical.code || '—' },
                        {
                          key: 'status',
                          label: 'HTTP',
                          children: technical.status === null ? '未到后端' : technical.status,
                        },
                        { key: 'rid', label: '请求编号', children: requestId || '—' },
                        {
                          key: 'outcome',
                          label: '业务状态',
                          children:
                            outcome === 'UNKNOWN'
                              ? '未知 —— 这一步可能已经生效'
                              : outcome === 'REJECTED'
                                ? '未发生(后端明确拒绝)'
                                : '未发生(后端明确失败)',
                        },
                        {
                          key: 'retriable',
                          label: '可否重试',
                          children: technical.retriable
                            ? '重试有意义'
                            : outcome === 'UNKNOWN'
                              ? '重试不安全,先核对当前状态'
                              : '重试不会改变结果',
                        },
                        ...(technical.fields.length
                          ? [
                              {
                                key: 'fields',
                                label: '字段',
                                children: technical.fields.join('; '),
                              },
                            ]
                          : []),
                        ...(technical.hint
                          ? [{ key: 'hint', label: '建议', children: technical.hint }]
                          : []),
                        { key: 'rest', label: '其余', children: TECHNICAL_ELSEWHERE },
                      ]}
                    />
                  ),
                },
              ]}
            />
          )}
        </Space>
      }
      action={
        // 结果未知时**不给重试按钮**。给了就等于在说"再点一次",
        // 而这一支的全部意义在于:那一次可能已经生效了
        onRetry && outcome !== 'UNKNOWN' ? (
          <Button size="small" loading={retrying} onClick={onRetry}>
            重试
          </Button>
        ) : undefined
      }
    />
  )
}
