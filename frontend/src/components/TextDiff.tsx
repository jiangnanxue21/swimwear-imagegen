import { useMemo } from 'react'
import { Empty, Typography } from 'antd'

import { brandVars, fontScale } from '../theme'
import { type Op, diffLines } from '../utils/textDiff'

const { Text } = Typography

/**
 * 行级文本对比(FE-306 / FE-307)。
 *
 * **判定已经搬到 `src/utils/textDiff.ts`(a61)** —— 那里能被 `node --test` 真的跑,
 * 这里不能(剥类型不做 JSX 变换)。所以这个文件只剩渲染。
 *
 * ## 为什么自己写而不是装一个 diff 库
 *
 * 这一页要比的是两段提示词 —— 几十到几百行纯文本,没有语法高亮需求,没有
 * 折叠需求。为它引一个依赖,换来的是 lockfile 里多一行、构建里多一个包,
 * 以及一个只有这一处用得到的 API。LCS 的最长公共子序列在这个量级下是
 * 二十行代码。
 *
 * ## 变量名承认这里本来就该有对比
 *
 * `PromptsPage.tsx` 里那个状态从第一版起就叫 `diffOpen`,而 Modal 里只有一个
 * 只读 textarea,**没有任何对比**。名字已经先承认了这件事 —— 一个叫 diff 的
 * 东西不做 diff,读代码的人会以为自己看漏了。
 *
 * ## 相同的行也显示
 *
 * 只显示差异会让人失去位置感:提示词是一整段有顺序的指令,「第 12 行改了」
 * 这个信息只有在能看到第 11 行和第 13 行时才有用。所以不折叠,全量渲染 ——
 * 代价是长文本要滚很久,而这一页本来就是拿来细读的。
 */

const TONE: Record<Op, { bg: string; mark: string }> = {
  same: { bg: 'transparent', mark: ' ' },
  del: { bg: brandVars.dangerBg, mark: '−' },
  add: { bg: brandVars.marineSoft, mark: '+' },
}

export default function TextDiff({
  left,
  right,
  leftLabel,
  rightLabel,
}: {
  left: string
  right: string
  leftLabel: string
  rightLabel: string
}) {
  const rows = useMemo(() => diffLines(left, right), [left, right])
  const changed = rows.filter((r) => r.op !== 'same').length

  if (!changed) {
    return (
      <Empty
        description={`${leftLabel} 与 ${rightLabel} 逐行相同`}
        image={Empty.PRESENTED_IMAGE_SIMPLE}
      />
    )
  }

  return (
    <div>
      <Text type="secondary">
        {leftLabel}(−) 对 {rightLabel}(+),共 {changed} 行不同
      </Text>
      <div
        style={{
          marginTop: 8,
          maxHeight: '60vh',
          overflow: 'auto',
          border: `1px solid ${brandVars.line}`,
          borderRadius: 4,
          fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
          fontSize: fontScale.body,
        }}
      >
        {rows.map((row, index) => (
          <div
            key={`${row.op}-${index}`}
            style={{
              display: 'flex',
              background: TONE[row.op].bg,
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-word',
            }}
          >
            <span
              style={{
                flex: '0 0 5.5em',
                textAlign: 'right',
                paddingRight: 8,
                color: brandVars.slate,
                userSelect: 'none',
              }}
            >
              {row.leftNo ?? ''} {row.rightNo ?? ''}
            </span>
            <span style={{ flex: '0 0 1.2em', color: brandVars.slate, userSelect: 'none' }}>
              {TONE[row.op].mark}
            </span>
            <span style={{ flex: 1, paddingRight: 8 }}>{row.text || '\u00a0'}</span>
          </div>
        ))}
      </div>
    </div>
  )
}
