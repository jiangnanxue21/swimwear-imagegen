/**
 * 写请求失败的统一收尾(FE-GLOBAL-01 的接线层)。
 *
 * ## 为什么是一个 hook 而不是继续手写
 *
 * `describeError` 已经把判定收敛成一份,但**接线**在 a30 那一轮只做了一半:
 * 六个文件在用 `readWriteError`,而批准、属性确认、文案生成、建任务这几处
 * 仍在用 `readError`。这个分布最糟的地方不是覆盖率低,是它**恰好反了** ——
 * 防住的是最便宜的动作(翻页、导出重下),没防的是最贵的那几个
 * (重复计费的模型调用、重复的审核签字)。
 *
 * 运营会从"这个系统会告诉我结果未知"里形成预期,然后在批准和建任务时踩空。
 * 全站统一用 `readError` 至少预期是一致的;修一半比不修更危险。
 *
 * 所以把这件事做成一个**必须显式接的东西**:一个 hook,调用点可 grep,
 * 而不是靠每个页面各自记得写两行。
 *
 * **注意:今天没有契约测试数这些调用点。** 本注释原来写着「可被契约测试
 * 数出来」—— 那是一句愿望,不是事实(`useWriteError` / `noRefresh` 在
 * `backend/tests/` 与 `frontend/tests/` 里 0 命中)。要把它变成事实,
 * 加一条测试数 `useWriteError(` 的出现次数,并和 `readError(` 的 Alert
 * 用法数一起做成只许减不许增的棘轮 —— 这正是 `docs/STATUS.md` 里
 * A12 那一行欠着的东西。
 *
 * ## 为什么强制要求 `onUnknown`
 *
 * 参数不是可选的。结果未知时唯一正确的第一动作是"看一眼现在是什么状态",
 * 而"看一眼"在每个页面是不同的 query key —— 这里给不出默认值。
 * 做成可选参数的话,漏传就退化成"只换了句话术",而话术恰恰是最不重要的一半:
 * 真正的区别在于**下一个按钮是什么**(见 `client.ts` 的 `unknownText`)。
 *
 * 页面确实没有可刷新的东西时(比如纯导航动作),显式传 `noRefresh` ——
 * 它是一个需要写出来的决定,不是一次遗忘。
 */
import { useCallback } from 'react'
import { App } from 'antd'
import { fieldProblems, isResultUnknown, readWriteError } from '../api/client'
import type { FieldProblem } from '../api/client'

/**
 * 显式声明"这一处没有可刷新的状态"。
 *
 * 写出来是为了让它在评审时可见:漏传和"确实不需要"在代码里必须长得不一样。
 */
export const noRefresh = (): void => {}

export function useWriteError(
  onUnknown: () => void,
  /**
   * 字段级校验明细的落点。**这一个是可选的,和 `onUnknown` 不同。**
   *
   * 两者可选性相反是有理由的,不是不一致:
   *
   *     onUnknown   每个写请求都可能"结果未知",而那时唯一正确的下一步
   *                 ("看一眼现在是什么状态")每一页都不同 —— 给不出默认值,
   *                 所以强制传,漏传要变成一次显式的 `noRefresh`
   *     onFields    只有**带表单**的写请求才有字段可高亮。多数调用点
   *                 (批准一版图、建一个任务、归档一件商品)根本没有输入框,
   *                 强制它们传一个空函数只会得到 20 处噪音
   *
   * 传了它的页面负责把 `loc` 映射到自己的输入控件。`loc` 的取值由后端
   * 决定(`spu_service._api_loc()` 那张表),前端不许自己拼一份对应关系 ——
   * 拼了就是第二个版本,而先过期的那一份会安静地高亮不到任何一行。
   *
   * **它不替代那条 message。** 明细高亮之外仍然要有一句话,否则
   * 折叠在第二步的那一行红框在第三步是看不见的。
   */
  onFields?: (fields: FieldProblem[]) => void,
): (err: unknown) => void {
  const { message } = App.useApp()
  return useCallback(
    (err: unknown) => {
      message.error(readWriteError(err))
      // 结果未知时顺手刷新:多数情况下刷新本身就给出了答案
      // (任务已经在跑、批次已经建了、这一版已经批过了)
      if (isResultUnknown(err)) onUnknown()
      // 无条件调,哪怕是空数组 —— 页面据此**清掉**上一次的高亮。
      // 只在有明细时才调的话,一次成功之后的第二次失败(这次没有字段错)
      // 会留着上一轮的红框,而那些行现在是对的
      if (onFields) onFields(fieldProblems(err))
    },
    [message, onUnknown, onFields],
  )
}
