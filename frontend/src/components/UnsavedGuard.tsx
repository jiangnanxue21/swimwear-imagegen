/**
 * 未保存内容离开保护(A11)。
 *
 * ## 在此之前会发生什么
 *
 * 六个编辑面(属性、图片集、文案、草稿、设置、提示词)都已经有 dirty 状态,
 * 但它只用来**显示一个「未保存」标签**。运营改完十二个属性、顺手点了侧栏的
 * 「今日工作」—— 那十二个改动就没了,没有确认框,没有提示,页面平静地切走。
 * 全仓库搜不到一处 `beforeunload`,也搜不到 blocker。
 *
 * ## 四条离开路径,两种机制
 *
 *     点菜单 / 切标签 / 切商品     react-router 的 `useBlocker`(标签值在 URL 上,
 *                                  所以切标签也是一次导航,同一个机制盖住)
 *     刷新 / 关掉浏览器            `beforeunload`。浏览器只给一个自己的原生弹窗,
 *                                  文案改不了 —— 这是浏览器的限制,不是没写
 *
 * `useBlocker` 只在数据路由下可用,这也是 App.tsx 换成 `createBrowserRouter`
 * 的唯一原因(见那边的注释)。
 *
 * ## 用法就一行
 *
 *     <UnsavedGuard dirty={dirty} what="文案" />
 *
 * 刻意做成组件而不是纯 hook:确认框要自己渲染。做成 hook 的话每个调用点都得再写
 * 一遍 Modal,六处抄六遍,而抄漏的那一处正好是运营用得最多的那一页。
 *
 * ## 不做的两件事
 *
 * 不自动保存。这六个面里有四个的保存动作会**产生新版本**(图片集 derive、文案落
 * 新版本、提示词进版本历史),自动保存等于替运营决定"这就是终稿了"。
 * 也不做"暂存草稿再恢复":那需要一处本地暂存区和一套冲突规则,是另一件事。
 */
import { useEffect } from 'react'
import { useBlocker } from 'react-router-dom'
import { Modal } from 'antd'
import { ExclamationCircleOutlined } from '@ant-design/icons'
import { brandVars } from '../theme'

interface Props {
  /** 有没有未保存的改动。false 时这个组件完全不生效 */
  dirty: boolean
  /** 提示里那个名字:"文案""属性"。写具体点,运营才知道要丢的是什么 */
  what: string
}

export default function UnsavedGuard({ dirty, what }: Props) {
  // 站内导航:菜单、标签、商品切换。`useBlocker` 收一个判定函数,
  // 返回 true 就把这次导航挂起,交给下面的确认框决定放不放行
  const blocker = useBlocker(
    ({ currentLocation, nextLocation }) =>
      dirty && currentLocation.pathname + currentLocation.search !== nextLocation.pathname + nextLocation.search,
  )

  // 刷新与关闭标签页。浏览器不接受自定义文案,`preventDefault` 是唯一能做的事
  useEffect(() => {
    if (!dirty) return
    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault()
      // 老浏览器要靠返回值才弹窗;新标准只看 preventDefault
      event.returnValue = ''
    }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [dirty])

  // dirty 在挂起期间被清掉了(比如用户回头点了保存):直接放行,
  // 否则那次导航会永远卡在挂起态,而界面上已经没有任何未保存内容了
  useEffect(() => {
    if (blocker.state === 'blocked' && !dirty) blocker.proceed()
  }, [blocker, dirty])

  return (
    <Modal
      open={blocker.state === 'blocked'}
      title={
        <>
          <ExclamationCircleOutlined style={{ color: brandVars.warning, marginInlineEnd: 8 }} />
          {what}还有未保存的改动
        </>
      }
      okText="放弃改动并离开"
      okButtonProps={{ danger: true }}
      cancelText="留在本页"
      onOk={() => blocker.proceed?.()}
      onCancel={() => blocker.reset?.()}
    >
      现在离开,这些改动不会保留,也无法恢复。留下来的话,页面上那个「保存」按钮还在原处。
    </Modal>
  )
}
