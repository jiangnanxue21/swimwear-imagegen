/**
 * AI 测试留档的展示判定(FE-312 / FE-314)。
 *
 * ## 为什么这些逻辑不留在 JSX 里
 *
 * a61 建了一条真的会执行的前端测试路径,并留下一条约定:**能不带 JSX 的部分
 * 就别带** —— 那一部分从此可验证,另一部分不是。而 a62 写历史表时把所有列渲染
 * 逻辑内联进了 `<Table columns={...}>`,一行都测不了。刚修好的路,下一轮就绕过去了。
 *
 * 这个文件是把那条约定真的执行一次:判定进 `.ts`,`<Table>` 只负责摆。
 *
 * ## 版本口径(a63 订正)
 *
 * `ai_test_runs` 有两列版本,语义**不同且不许互串**:
 *
 *     prompt_template_version  int   `prompt_templates.version` 自增序号。评分链路落它
 *     prompt_version           str   内容派生值。文案链路落它(`模型:哈希`)
 *
 * a57 曾把评分的序号塞进 `prompt_version`,于是那一列混装两种形状,而
 * `prompt_template_version` 恒为 NULL。**FE-314 的互链定不下口径,根因就在这里。**
 * 订正之后,提示词详情页(管的是评分那两份,版本是自增序号)按
 * `prompt_key + prompt_template_version` 查 —— 那是唯一对得上的一对。
 */

export interface RunVersionRef {
  prompt_key: string | null
  prompt_version: string | null
  prompt_template_version: number | null
}

/** 一条留档在界面上该显示的版本文本。两列都空时返回 null,让调用方画「—」。 */
export function versionLabel(run: RunVersionRef): string | null {
  if (run.prompt_template_version != null) return `v${run.prompt_template_version}`
  if (run.prompt_version) return run.prompt_version
  return null
}

/**
 * 提示词详情页跳到「这一版跑过的测试」用的查询参数(FE-314)。
 *
 * **只对有序号的那一版成立。** 内置默认生效时没有版本行,查不出东西来 ——
 * 返回 null 让调用方不画这个入口,而不是画一个必然空的链接。一个点进去
 * 永远是空的入口,比没有入口更糟:它会被读成「这一版没跑过测试」。
 */
export function runsQueryForVersion(
  promptKey: string,
  version: number | null | undefined,
): { prompt_key: string; prompt_template_version: number } | null {
  if (version == null || !Number.isInteger(version) || version < 1) return null
  if (!promptKey) return null
  return { prompt_key: promptKey, prompt_template_version: version }
}

/** 结果一列的文案。失败也留档 ——「跑了但没成」和「没跑过」必须分得开。 */
export function outcomeLabel(run: { success: boolean; error_code: string | null }): {
  tone: 'success' | 'warning'
  text: string
  detail: string | null
} {
  if (run.success) return { tone: 'success', text: '成功', detail: null }
  return { tone: 'warning', text: '未成功', detail: run.error_code }
}

/** 生成器与模型合成一格。两个都空时给「—」,不给一个空串。 */
export function engineLabel(run: {
  generator: string | null
  model_name: string | null
}): string {
  return [run.generator, run.model_name].filter(Boolean).join(' · ') || '—'
}

/** 耗时。`0` 是一个合法的值,不能被 falsy 判成「没有」。 */
export function durationLabel(ms: number | null | undefined): string {
  return ms == null ? '—' : `${ms} ms`
}

/**
 * 「对象」一列(FE-312)。
 *
 * uuid 全长在表格里没有信息量,截前八位;类型用中文说清是候选图还是商品 ——
 * `generation_candidate` 原样摆出来是把后端枚举直接甩给运营(a52 收敛过的东西)。
 *
 * 对象**可能已经被清理掉**(候选图随任务清理走,而 `subject_id` 刻意不加外键),
 * 所以这里只画标识不画链接:一个点进去 404 的链接比没有链接更糟。
 */
export function subjectLabel(run: {
  subject_type: string
  subject_id: string | null
}): string {
  const kind = run.subject_type === 'product' ? '商品' : '候选图'
  if (!run.subject_id) return kind
  return `${kind} ${run.subject_id.slice(0, 8)}`
}
