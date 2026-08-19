/**
 * `diffLines` 的行为测试(a61)。
 *
 * ## 这是本仓前端第一批**真的会执行**的测试
 *
 * 在此之前,前端的防线全部是后端 `tests/pure/` 里读源码的守卫 —— 它们证明得了
 * 「这一行写了」,证明不了「这个函数算得对」。Vitest 要 node_modules,而这台机器
 * 六轮没装上,于是 a54 那句「下一步第一件事就是 `make fe-check`」在交接文档里
 * 重复了六遍,**而每一轮都在往那条没有行为验证的路上多写几个文件**。
 *
 * Node 22 原生剥类型 + `node:test` 把这件事的成本降到零:不装依赖、不转译、
 * 不引测试框架。它跑不了 `.tsx`(剥类型不做 JSX 变换),所以覆盖的是判定层 ——
 * 而判定层恰好是"算错了不会有任何征兆"的那一半。
 *
 * ## 它不能替代 Vitest
 *
 * 组件渲染、hooks、用户交互一条都覆盖不到。`make fe-check` 仍然是欠着的。
 * 这里做的是**把欠账从"全部"缩到"渲染那一半"**,不是把它还清。
 */
import test from 'node:test'
import assert from 'node:assert/strict'

import { diffLines } from '../../src/utils/textDiff.ts'

const ops = (l: string, r: string) => diffLines(l, r).map((row) => row.op).join('')
const texts = (l: string, r: string) => diffLines(l, r).map((row) => row.text)

test('两段相同的文本一行差异都没有', () => {
  assert.equal(ops('a\nb\nc', 'a\nb\nc'), 'samesamesame')
})

test('中间插入一行,只报那一行是新增', () => {
  assert.equal(ops('a\nc', 'a\nb\nc'), 'sameaddsame')
})

test('中间删掉一行,只报那一行是删除', () => {
  assert.equal(ops('a\nb\nc', 'a\nc'), 'samedelsame')
})

test('改一行 = 一删一增,而不是一条 change', () => {
  // 行级 diff 没有"修改"这个操作。界面上一红一蓝比一行黄更有信息量:
  // 运营要读的是改成了什么,不是"这里改过"
  assert.equal(ops('a\nX\nc', 'a\nY\nc'), 'samedeladdsame')
})

test('整段替换时不会把不相干的行硬凑成相同', () => {
  assert.equal(ops('a\nb', 'x\ny'), 'deldeladdadd')
})

test('左边为空 = 全是新增', () => {
  assert.equal(ops('', 'a\nb'), 'deladdadd')
  // 空串 split 出一个空行,所以左边有一条 del —— 这不是 bug,是"原来有一个空行"。
  // 把它特判掉会让"内置默认是空的"和"内置默认有一个空行"长得一样
})

test('行号左右各自计数,新增行没有左侧行号', () => {
  const rows = diffLines('a\nc', 'a\nb\nc')
  assert.deepEqual(
    rows.map((r) => [r.leftNo, r.rightNo]),
    [
      [1, 1],
      [null, 2],
      [2, 3],
    ],
  )
})

test('删除行没有右侧行号', () => {
  const rows = diffLines('a\nb\nc', 'a\nc')
  assert.deepEqual(rows[1].leftNo, 2)
  assert.equal(rows[1].rightNo, null)
})

test('保留原文,不做 trim', () => {
  // 提示词里的缩进是有意义的(模型会照着它排版),trim 掉之后 diff 会说"没变"
  assert.deepEqual(texts('  a', 'a'), ['  a', 'a'])
})

test('尾部多出的行全部计为新增', () => {
  assert.equal(ops('a', 'a\nb\nc'), 'sameaddadd')
})

test('最长公共子序列不是逐行对齐 —— 位移之后仍能对上', () => {
  // 逐行比的实现在这一条上会报"全都变了"。提示词最常见的改法正是
  // 在开头插一段,那种实现会把整份提示词标成全新
  assert.equal(ops('a\nb\nc', 'x\na\nb\nc'), 'addsamesamesame')
})

test('同一份输入重复调用结果恒等', () => {
  const once = JSON.stringify(diffLines('a\nb', 'a\nc'))
  for (let i = 0; i < 20; i += 1) {
    assert.equal(JSON.stringify(diffLines('a\nb', 'a\nc')), once)
  }
})
