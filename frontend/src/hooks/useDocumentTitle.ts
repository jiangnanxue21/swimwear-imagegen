/**
 * 浏览器标签页标题(走查 P1-5)。
 *
 * ## 要修的那次体验
 *
 * 走查时全仓 **0 处** `document.title`,`index.html` 里写死一句
 * 「商品展示图生产台」。而运营的常态是同时开三四个标签 —— 一个逐件快审、
 * 一个盯批次进度、一个查商品详情 —— 标签栏上四个一模一样的标题,
 * 每次切换都要点进去才知道是哪个。
 *
 * ## 为什么把计数也写进去
 *
 * 逐件快审传的是 `逐件快审 (12)`。运营切到别的标签处理完一件事再切回来时,
 * 不用等页面渲染就知道还剩多少 —— 标签栏本身成了一个进度指示器。
 * 这是标签标题唯一比页面内标题多出来的能力,不用白不用。
 *
 * ## 卸载时要还原
 *
 * 不还原的话,从「商品详情 SW-001」返回工作台时,标签上还挂着 SW-001 ——
 * 而 react-router 换页不重新加载文档,没人会去改它。
 */
import { useEffect } from 'react'

export const BASE_TITLE = '服装商品运营平台'

/**
 * @param title 页面名。传 `undefined` 表示还在加载,这时不改标题 ——
 *              先闪一下「undefined · 服装商品运营平台」再跳成正确值,
 *              比晚 200ms 显示更糟
 */
export function useDocumentTitle(title?: string | null): void {
  useEffect(() => {
    document.title = title ? `${title} · ${BASE_TITLE}` : BASE_TITLE
    return () => {
      document.title = BASE_TITLE
    }
  }, [title])
}
