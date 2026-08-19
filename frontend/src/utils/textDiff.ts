/**
 * 行级文本对比的**纯逻辑**(FE-306 / FE-307)。
 *
 * ## 为什么单独一个 .ts 而不是留在 TextDiff.tsx 里
 *
 * a61 起前端有了一条真的会执行的测试路径:Node 22 原生剥类型 + `node --test`,
 * 零依赖、不需要 node_modules。**但它只剥类型,不做 JSX 变换** —— 留在 `.tsx`
 * 里的函数一行都跑不了,而这是本仓前端第一次能验行为而不是验源码形状。
 *
 * 所以判定与渲染分家:判定进 `.ts`(能跑),渲染留在 `.tsx`(仍然只有源码守卫)。
 * 这条分界线值得记住 —— **新写前端逻辑时,能不带 JSX 的部分就别带**,
 * 那一部分从此是可验证的,另一部分不是。
 */

export type Op = 'same' | 'add' | 'del'

export interface Row {
  op: Op
  text: string
  leftNo: number | null
  rightNo: number | null
}

/** 最长公共子序列。两段几百行的文本,O(n·m) 在这个量级下完全够用。 */
function lcsTable(a: string[], b: string[]): number[][] {
  const table: number[][] = Array.from({ length: a.length + 1 }, () =>
    new Array(b.length + 1).fill(0),
  )
  for (let i = a.length - 1; i >= 0; i -= 1) {
    for (let j = b.length - 1; j >= 0; j -= 1) {
      table[i][j] =
        a[i] === b[j] ? table[i + 1][j + 1] + 1 : Math.max(table[i + 1][j], table[i][j + 1])
    }
  }
  return table
}

export function diffLines(left: string, right: string): Row[] {
  const a = left.split('\n')
  const b = right.split('\n')
  const table = lcsTable(a, b)
  const rows: Row[] = []
  let i = 0
  let j = 0
  while (i < a.length && j < b.length) {
    if (a[i] === b[j]) {
      rows.push({ op: 'same', text: a[i], leftNo: i + 1, rightNo: j + 1 })
      i += 1
      j += 1
    } else if (table[i + 1][j] >= table[i][j + 1]) {
      rows.push({ op: 'del', text: a[i], leftNo: i + 1, rightNo: null })
      i += 1
    } else {
      rows.push({ op: 'add', text: b[j], leftNo: null, rightNo: j + 1 })
      j += 1
    }
  }
  while (i < a.length) {
    rows.push({ op: 'del', text: a[i], leftNo: i + 1, rightNo: null })
    i += 1
  }
  while (j < b.length) {
    rows.push({ op: 'add', text: b[j], leftNo: null, rightNo: j + 1 })
    j += 1
  }
  return rows
}

// 用调色板里真的有的 token。`successBg` 一度被写进这里而 `lightTokens` 里没有它 ——
// `brandVars` 是 `Object.fromEntries` 出来的 Record,取一个不存在的键会得到
// `undefined`,而 CSS 收到 undefined 只是不上色:**颜色错了不会有任何征兆**。
