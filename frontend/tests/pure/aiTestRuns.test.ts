/**
 * 留档展示判定的行为测试(a63)。
 *
 * a61 建好了这条通路,a62 一条新用例都没加 —— 因为新逻辑全内联在 JSX 里。
 * 这一批是把那条约定真的执行一次。
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import {
  durationLabel,
  engineLabel,
  outcomeLabel,
  runsQueryForVersion,
  subjectLabel,
  versionLabel,
  versionPathFor,
} from '../../src/utils/aiTestRuns.ts'

const base = { prompt_key: 'k', prompt_version: null, prompt_template_version: null }

test('评分留档显示序号', () => {
  assert.equal(versionLabel({ ...base, prompt_template_version: 3 }), 'v3')
})

test('文案留档显示内容派生值', () => {
  assert.equal(versionLabel({ ...base, prompt_version: 'gpt-4o:a1b2c3' }), 'gpt-4o:a1b2c3')
})

test('两列都空时返回 null,由调用方画「—」', () => {
  assert.equal(versionLabel(base), null)
})

test('序号是 0 时仍然显示 —— 0 是合法版本号吗?这里按"有值就显示"处理', () => {
  // `!= null` 而不是 falsy 判:落库的序号从 1 起,但判定层不该替数据层
  // 假设取值范围。真出现 0 时显示 v0 比显示「—」诚实
  assert.equal(versionLabel({ ...base, prompt_template_version: 0 }), 'v0')
})

test('内置默认生效时不给互链入口', () => {
  // 没有版本行,查不出东西 —— 一个点进去永远是空的入口,
  // 会被读成「这一版没跑过测试」
  assert.equal(runsQueryForVersion('k', null), null)
  assert.equal(runsQueryForVersion('k', undefined), null)
})

test('非正整数的版本一律不给入口', () => {
  assert.equal(runsQueryForVersion('k', 0), null)
  assert.equal(runsQueryForVersion('k', -1), null)
  assert.equal(runsQueryForVersion('k', 1.5), null)
})

test('空 key 不给入口', () => {
  assert.equal(runsQueryForVersion('', 3), null)
})

test('正常情况给出后端认得的两个参数', () => {
  assert.deepEqual(runsQueryForVersion('vision_system_prompt', 3), {
    prompt_key: 'vision_system_prompt',
    prompt_template_version: 3,
  })
})

test('失败的留档带出错误码,而不是只说「未成功」', () => {
  assert.deepEqual(outcomeLabel({ success: false, error_code: 'PROVIDER_TIMEOUT' }), {
    tone: 'warning',
    text: '未成功',
    detail: 'PROVIDER_TIMEOUT',
  })
})

test('成功的留档不画错误码位', () => {
  assert.equal(outcomeLabel({ success: true, error_code: null }).detail, null)
})

test('生成器与模型都空时给一个破折号,不给空串', () => {
  assert.equal(engineLabel({ generator: null, model_name: null }), '—')
  assert.equal(engineLabel({ generator: 'llm', model_name: null }), 'llm')
  assert.equal(engineLabel({ generator: 'llm', model_name: 'gpt-4o' }), 'llm · gpt-4o')
})

test('耗时 0 ms 不被 falsy 判成「没有」', () => {
  // Mock 评分器与缓存命中都可能是 0 —— 显示「—」会让人以为这次没跑
  assert.equal(durationLabel(0), '0 ms')
  assert.equal(durationLabel(null), '—')
  assert.equal(durationLabel(undefined), '—')
})

test('对象列:候选图与商品用中文分,不甩后端枚举', () => {
  assert.equal(
    subjectLabel({ subject_type: 'generation_candidate', subject_id: 'abcd1234-ef56-7890' }),
    '候选图 abcd1234',
  )
  assert.equal(
    subjectLabel({ subject_type: 'product', subject_id: 'ffff0000-1111-2222' }),
    '商品 ffff0000',
  )
})

test('对象被清理掉之后只剩类型,不画一个空 id', () => {
  // `subject_id` 刻意不加外键 —— 候选图随任务清理走,而"那次测试跑过"不该跟着消失
  assert.equal(subjectLabel({ subject_type: 'product', subject_id: null }), '商品')
})

// ==================================================== FE-314 反向互链(a70)

test('评分留档给得出版本页路径', () => {
  assert.equal(
    versionPathFor({ ...base, prompt_key: 'vision_system_prompt', prompt_template_version: 3 }),
    '/prompts/vision_system_prompt/versions/3',
  )
})

test('文案留档不画链接 —— 内容哈希没有对应的版本页', () => {
  // 拿哈希去拼版本路由会得到一个必然 404 的链接。a63 的判据在反向同样成立:
  // 一个点进去永远是空的链接会被读成「这一版不见了」
  assert.equal(
    versionPathFor({
      prompt_key: 'copy_llm_system_prompt',
      prompt_version: 'gpt-4o:a1b2c3',
      prompt_template_version: null,
    }),
    null,
  )
})

test('没有 key 时不画链接', () => {
  assert.equal(versionPathFor({ ...base, prompt_key: null, prompt_template_version: 3 }), null)
})

test('序号小于 1 时不画链接 —— 版本号从 1 起,0 是判不出来的值', () => {
  assert.equal(versionPathFor({ ...base, prompt_template_version: 0 }), null)
  assert.equal(versionPathFor({ ...base, prompt_template_version: -1 }), null)
})

test('非整数序号不画链接', () => {
  assert.equal(versionPathFor({ ...base, prompt_template_version: 1.5 }), null)
})
