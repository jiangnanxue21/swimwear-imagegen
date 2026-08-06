/**
 * 渲染异常兜底。
 *
 * ## 为什么需要
 *
 * 走查发现全站 0 处 ErrorBoundary:任何一个组件抛错,React 会把整棵树卸载,
 * 运营看到的是**纯白页面**。他没有控制台、不知道该找谁、也说不清刚才在干什么,
 * 于是只能刷新或者放弃 —— 而刷新之后如果是数据本身触发的异常,还会白第二次。
 *
 * 这一层不修 bug,它只做三件事:
 *
 *     1. 保住外框     页面级边界挂在 `<Outlet />` 外面,侧栏和顶栏不跟着白,
 *                     运营还能自己走去别的页面
 *     2. 给个编号     `E-<时间戳36进制>`。运营截图发过来的时候,这个编号能对上
 *                     浏览器控制台里的那条 console.error
 *     3. 能一键带走   把编号、路径、错误名、消息、组件栈拼成一段文本,
 *                     点一下进剪贴板 —— 比让运营描述「就是白了」有用得多
 *
 * ## 刻意不做的
 *
 * 不上报到任何后端接口。Gate A 期间没有前端日志通道,凭空加一条会引入一个
 * 「上报本身失败了怎么办」的新分支。编号 + 复制按钮已经能把信息传出去。
 */
import { Component, type ErrorInfo, type ReactNode } from 'react'
import { Button, Result, Space, Typography } from 'antd'
import { CopyOutlined, HomeOutlined, ReloadOutlined } from '@ant-design/icons'
import { brandVars, fontScale } from '../theme'

interface Props {
  children: ReactNode
  /**
   * 变了就重置错误状态。页面级边界传 `location.pathname`——
   * 否则一个页面崩过之后,运营点侧栏走到别的页面,看到的还是那张兜底页。
   */
  resetKey?: string
  /** 兜底页上多说一句,比如「这一页出错了」和「整个界面出错了」不是一回事 */
  scope?: string
}

interface State {
  error: Error | null
  componentStack: string
  errorId: string
  resetKey?: string
}

function makeErrorId() {
  return `E-${Date.now().toString(36).toUpperCase()}`
}

export default class ErrorBoundary extends Component<Props, State> {
  state: State = { error: null, componentStack: '', errorId: '', resetKey: undefined }

  static getDerivedStateFromError(error: Error): Partial<State> {
    return { error, errorId: makeErrorId() }
  }

  /** resetKey 变了就清空。放在 getDerivedStateFromProps 里,避免多一次渲染 */
  static getDerivedStateFromProps(props: Props, state: State): Partial<State> | null {
    if (state.resetKey === props.resetKey) return null
    if (!state.error) return { resetKey: props.resetKey }
    return { error: null, componentStack: '', errorId: '', resetKey: props.resetKey }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    this.setState({ componentStack: info.componentStack ?? '' })
    // 编号一起打出去:运营报的编号要能和控制台里的这条对上
    console.error(`[${this.state.errorId || makeErrorId()}] 渲染异常`, error, info.componentStack)
  }

  private report() {
    const { error, componentStack, errorId } = this.state
    return [
      `编号: ${errorId}`,
      `页面: ${window.location.pathname}${window.location.search}`,
      `时间: ${new Date().toISOString()}`,
      `错误: ${error?.name}: ${error?.message}`,
      '',
      (error?.stack ?? '').trim(),
      '',
      '组件栈:',
      componentStack.trim(),
    ].join('\n')
  }

  private copy = async () => {
    const text = this.report()
    try {
      await navigator.clipboard.writeText(text)
    } catch {
      // 非 https / 老浏览器下 clipboard 不可用。退回选中文本让运营自己 Ctrl+C
      window.prompt('复制下面这段发给技术:', text)
    }
  }

  render() {
    const { error, errorId } = this.state
    if (!error) return this.props.children

    return (
      <Result
        status="error"
        title={`${this.props.scope ?? '这一页'}出错了`}
        subTitle={
          <Space direction="vertical" size={4}>
            <span>
              不是你操作错了。把下面这个编号发给技术就能定位到具体是哪一步。
            </span>
            <Typography.Text className="mono" strong style={{ color: brandVars.dangerDeep }}>
              {errorId}
            </Typography.Text>
          </Space>
        }
        extra={
          <Space wrap>
            <Button type="primary" icon={<ReloadOutlined />} onClick={() => window.location.reload()}>
              重新加载
            </Button>
            <Button icon={<HomeOutlined />} onClick={() => { window.location.href = '/today' }}>
              回工作首页
            </Button>
            <Button icon={<CopyOutlined />} onClick={this.copy}>
              复制错误详情
            </Button>
          </Space>
        }
      >
        {/* 错误消息本身对运营没用,但对着屏幕念给技术听是有用的,所以留着但压小 */}
        <Typography.Paragraph
          className="mono"
          style={{ color: brandVars.slate, fontSize: fontScale.body, marginBottom: 0 }}
        >
          {error.name}: {error.message}
        </Typography.Paragraph>
      </Result>
    )
  }
}
