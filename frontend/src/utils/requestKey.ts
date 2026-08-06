/**
 * 「这一次提交」的标识符,发在 `Idempotency-Key` 头里(A45-batch12-3 / EX-06)。
 *
 * ## 它必须由客户端生成,而且必须**每次点击一把**
 *
 * 后端在 `batch_jobs.request_key` 上挂了唯一约束,同键同入参返回原批次、
 * 同键不同入参报 409。这条约束是**全局**的,不按操作人分。
 *
 * 所以键**不能**按请求内容算(动作 + 商品 + 标签之类)。那样算的话:
 *
 *     两个运营先后选中同一批商品做同一个动作
 *     -> 算出同一把键
 *     -> 第二个人静默拿到第一个人的批次,而且什么都不会执行
 *
 * 界面上他会看到一个批次号、一份进度,一切正常 —— 只是那不是他建的,
 * 而他要做的那件事从来没发生过。这正是 EX-06 反对的那种静默。
 *
 * 一次点击一把随机键就没有这个问题:两个人点两次就是两把键、两个批次,
 * 而同一个人的双击、以及第一次响应在回程丢失后的重发,共用同一把。
 *
 * ## 为什么有 fallback
 *
 * `crypto.randomUUID()` 只在安全上下文(https 或 localhost)里存在。
 * 局域网 IP 上跑 dev server 时它是 undefined —— 那时候整条提交会因为
 * 一个 TypeError 挂掉,而这个头本来只是锦上添花。宁可退化成一把
 * 随机性差一点的键:它仍然满足"每次点击不同",而这就是全部要求。
 */
export function newRequestKey(): string {
  const c = globalThis.crypto
  if (c && typeof c.randomUUID === 'function') return c.randomUUID()
  if (c && typeof c.getRandomValues === 'function') {
    const bytes = new Uint8Array(16)
    c.getRandomValues(bytes)
    return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('')
  }
  // 最后一档。Math.random 不是密码学随机,但这把键不需要不可预测 ——
  // 它只需要不撞上同一个浏览器里的上一次点击
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 12)}`
}
