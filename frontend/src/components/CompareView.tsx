/**
 * 原图 / 候选图 等尺寸对照(走查 P0-5)。
 *
 * ## 为什么要单独做一个视图
 *
 * 虚拟试穿最核心的判断是**「这还是同一件衣服吗」** —— 花型有没有走样、领口深浅
 * 变没变、拼色的位置对不对。原本审核页把商品原图和候选图放在两张分开的卡片里,
 * 中间隔着一整块评分区,等于要求运营**用记忆比对**:看完左边记住花型,滚到右边
 * 再回想。人对图案细节的短期记忆很差,这一步的判断准确率是被界面吃掉的。
 *
 * 所以给两种对照,解决的是两类问题:
 *
 *     并排    整体比:版型、姿态、颜色调子。两张图**同尺寸同底色**,
 *             不同尺寸的并排仍然需要脑补缩放
 *     叠加    局部比:花型位置、领口线、拼色边界。同一个画框里推滑块,
 *             差异会直接跳出来 —— 这是并排看不出来的那一类
 *
 * ## 尺寸必须相等,这不是排版偏好
 *
 * 两个画框都用 `--tile-review` 的高度和 `object-fit: contain`。原图和候选图的
 * 长宽比经常不一样(原图是平铺拍摄、候选图是模特图),`cover` 会各裁各的,
 * 于是「领口变了」可能只是两边裁掉的部分不同。contain + 等框 = 两边都是完整画面。
 *
 * ## 滑块而不是拖拽分界线
 *
 * 原生拖拽对键盘用户不可用,而 antd 的 Slider 自带方向键与焦点样式。
 * 一个用来看细节的控件把细节看不清的人挡在外面没有道理。
 */
import { useMemo, useState } from 'react'
import { Alert, Col, Empty, Row, Segmented, Select, Slider, Space, Typography } from 'antd'
import { brandVars, fontScale } from '../theme'

export interface ComparePicture {
  id: string
  url: string
  /** 画框下面那行小字,例如「正面图 1200×1600」 */
  label: string
}

interface Props {
  /** 可选的对照源,通常是商品原图。为空时组件自己说明为什么比不了 */
  originals: ComparePicture[]
  /** 当前选中的候选图。没有选中时不渲染对照 */
  candidate: ComparePicture | null
}

type Mode = 'side' | 'overlay'

export default function CompareView({ originals, candidate }: Props) {
  const [mode, setMode] = useState<Mode>('side')
  const [originalId, setOriginalId] = useState<string>('')
  const [split, setSplit] = useState(50)

  // 默认选第一张。原图列表在后端已经按 created_at 排过,第一张通常就是正面图
  const original = useMemo(
    () => originals.find((o) => o.id === originalId) ?? originals[0] ?? null,
    [originals, originalId],
  )

  if (!candidate) {
    return (
      <Empty
        image={Empty.PRESENTED_IMAGE_SIMPLE}
        description="先在上面选一张候选图,这里会和原图并排对照"
      />
    )
  }

  if (!original) {
    return (
      <Alert
        type="info"
        showIcon
        message="这件商品没有可对照的原图"
        description="对照视图要拿商品素材当基准。素材缺失本身也是阻断项,可以到素材标签页补。"
      />
    )
  }

  const controls = (
    <Space wrap size={8} style={{ marginBottom: 10 }}>
      <Segmented
        size="small"
        value={mode}
        onChange={(v) => setMode(v as Mode)}
        options={[
          { label: '并排', value: 'side' },
          { label: '叠加滑块', value: 'overlay' },
        ]}
      />
      {originals.length > 1 && (
        <Select
          size="small"
          style={{ minWidth: 200 }}
          value={original.id}
          onChange={setOriginalId}
          options={originals.map((o) => ({ value: o.id, label: o.label }))}
        />
      )}
      <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
        {mode === 'side'
          ? '整体比:版型、姿态、颜色调子'
          : '局部比:推滑块看花型位置、领口线、拼色边界'}
      </Typography.Text>
    </Space>
  )

  if (mode === 'side') {
    return (
      <div>
        {controls}
        <Row gutter={12}>
          <Col xs={24} md={12}>
            <div className="compare-pane">
              <img src={original.url} alt={`商品原图 · ${original.label}`} />
            </div>
            <div className="compare-caption">
              <span>商品原图</span>
              <span className="mono">{original.label}</span>
            </div>
          </Col>
          <Col xs={24} md={12}>
            <div className="compare-pane">
              <img src={candidate.url} alt={`候选图 · ${candidate.label}`} />
            </div>
            <div className="compare-caption">
              <span style={{ color: brandVars.accent, fontWeight: 600 }}>候选图</span>
              <span className="mono">{candidate.label}</span>
            </div>
          </Col>
        </Row>
      </div>
    )
  }

  return (
    <div>
      {controls}
      <div className="compare-pane">
        {/* 下层:原图。上层按百分比裁切,露出下层 —— 所以滑块推到 0 是纯原图、
            推到 100 是纯候选图 */}
        <img src={original.url} alt={`商品原图 · ${original.label}`} />
        <img
          src={candidate.url}
          alt={`候选图 · ${candidate.label}`}
          className="compare-overlay-top"
          style={{ clipPath: `inset(0 ${100 - split}% 0 0)` }}
        />
      </div>
      <Slider
        min={0}
        max={100}
        value={split}
        onChange={setSplit}
        tooltip={{ formatter: (v) => `候选图 ${v}%` }}
        marks={{ 0: '原图', 50: '', 100: '候选图' }}
        style={{ marginTop: 4 }}
      />
      <div className="compare-caption">
        <span className="mono">{original.label}</span>
        <span className="mono">{candidate.label}</span>
      </div>
    </div>
  )
}
