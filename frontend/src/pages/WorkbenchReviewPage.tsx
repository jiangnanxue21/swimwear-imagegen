/**
 * 审核中心(a47 §6)= 顶部一组计数入口 + 原有的逐件快审(OPS-REVIEW P3)。
 *
 * ## a47 只做入口,**不动任何一个审核模型**
 *
 * 收敛之前运营的「今日工作」里并列着两条审核入口:这一页(图片集与文案)
 * 和 `/reviews`(候选图评分不过关的落点)。它们审的不是同一个对象,
 * 所以合并数据模型是错的 —— 而并排摆着又让人每天要先想一下该点哪个。
 *
 * 折中是**一个入口、三条计数**:哪一类有几件在这里说清,点进去仍是各自
 * 现有页面。禁止新建统一审核表、拼统一 DTO、重写任一审核页、统一 mutation。
 *
 * **计数必须来自后端已有接口,没有的不显示**(硬规则 4)。三条的来源:
 *
 *     候选图审核   `GET /reviews?status=PENDING` 的 `total`
 *     图片集/文案  本页队列查询已经在拉的 `summary.by_next_action`
 *     属性冲突     同一份 summary 的 `RESOLVE_CONFLICT`
 *
 * 前两条是这一页本来就要发的请求,第三条搭同一次车 —— 没有为了三个数字
 * 新开接口。真统一队列的触发条件沿用上游:测试期记录交叉切换频次,数据说话。
 *
 * ## 为什么需要这一页
 *
 * 系统刻意**不提供批量批准**(见 `batch.BatchAction` 的注释):批准是人对内容
 * 负责的动作,批量批准等于把审核变成一次点击。这条不打算改。
 *
 * 但"不批量批准"不等于"每件都要点四下"。50 件停在待批准时,现在的路径是:
 * 列表页找到那件 -> 点进详情 -> 切到图片集/文案标签 -> 批准 -> 回列表页,
 * 四步里有三步是导航。这一页把导航压成 0:一屏一件,批完自动下一件,
 * 手不离键盘(J/K/A)。**审的东西一点没少** —— 图片集全图与文案全文都在同一屏,
 * 该看的仍然要看,省掉的只是找和跳。
 *
 * ## 队列在批的过程中不自动重排
 *
 * 批准一件图片集之后,它的"唯一下一步"会变成生成文案或批准文案 —— 也就是说
 * 如果每批一件就重新拉一次队列,手指下的第 3 件会变成另一件商品,
 * 而运营正准备对它按 A。所以本页的队列是**进入时的一次快照**,
 * 批过的就地划掉;要拿最新队列,点「重新载入队列」。
 *
 * ## 判定仍然全在后端
 *
 * 队列来自 `GET /workbench/products?next_action=APPROVE_*`(§3.2 的唯一下一步),
 * 不在前端筛"哪些看起来能批"。当前项的下一步如果已经不是批准动作了
 * (别人批过、或上游刚变过),批准按钮就不亮 —— 那是冲突,交给详情页,
 * 不在这一页猜。
 *
 * ## 退回(A10):和"跳过"是两件事
 *
 * 跳过(J)什么都不改:那一件仍然停在待批准,下一步仍然是"等待批准",
 * 于是它下一轮回到队列排在原处。**退回改变判定结果** —— 状态置 REJECTED,
 * 由退回原因推出唯一下一步,这件商品因此离开待批准队列,并且带着
 * "接下来该干什么"进入异常列表和工作台。
 *
 * 三样东西全部来自后端(`GET /workbench/reject-reasons`):可选原因、中文、
 * 以及每个原因退回之后去哪一步。前端抄一份的后果不是显示错字,是运营选了
 * 「商品结构改变」,界面说"接下来去重排图片",而系统实际把他送去改属性。
 *
 * 同样**不提供批量退回**:理由和不提供批量批准是同一条 —— 退回也是人对
 * 内容负责的动作,而且它比批准更需要理由。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Alert, App, Button, Card, Descriptions, Empty, Image, Input, Modal, Progress,
  Radio, Segmented, Skeleton, Space, Spin, Tag, Tooltip, Typography,
} from 'antd'
import {
  ArrowLeftOutlined, ArrowRightOutlined, CheckOutlined, ExportOutlined,
  ReloadOutlined, StopOutlined,
} from '@ant-design/icons'
import { useMutation, useQueries, useQuery, useQueryClient } from '@tanstack/react-query'
import { readError } from '../api/client'
import { useWriteError } from '../hooks/useWriteError'
import {
  COPY_STATUS_LABEL,
  NEXT_ACTION_LABEL,
  workbenchApi,
  type ListingCopy,
  type NextActionCode,
  type RejectReasonOption,
  type WorkbenchListItem,
} from '../api/workbench'
import { imageSetsApi } from '../api/imageSets'
import { reviewsApi } from '../api/reviews'
import { mediaApi } from '../api/media'
import { enumParam, useUrlFilters } from '../hooks/useUrlFilters'
import { brandVars, fontScale, imageTile } from '../theme'
import KeyboardHelp from '../components/KeyboardHelp'
import { MEDIA_ROLE_LABEL } from '../components/workbench/materialUtils'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { pushRecent, readPref, writePref } from '../utils/localPrefs'

/** 最近用过的退回原因(本机偏好)。存的是 code,中文与下一步永远现问后端 */
const RECENT_REASONS_KEY = 'review-recent-reject-reasons'
import BrandTag from '../components/BrandTag'
import { AudienceTag, ReviewFocusLine } from '../components/AudienceBadge'

/**
 * 审核中心的计数入口(a47 §6)。**只做入口,不动模型。**
 *
 * 三个数字全部来自后端已有接口(见文件头)。**读不到的不显示** ——
 * 这是硬规则 4 的直接后果:一个"0"和一个"没查到"在界面上长得一样,
 * 而运营会按前者的意思去理解(没活了),然后漏掉一整类待办。
 */
function ReviewCentreTiles({
  candidateCount,
  imageSetCount,
  copyCount,
  conflictCount,
  onPick,
}: {
  candidateCount: number | null
  imageSetCount: number | null
  copyCount: number | null
  conflictCount: number | null
  onPick: (filter: Filter) => void
}) {
  const navigate = useNavigate()
  const tiles: {
    key: string
    label: string
    hint: string
    count: number | null
    go: () => void
  }[] = [
    {
      key: 'candidate',
      label: '候选图审核',
      hint: '评分未自动通过、需要人工判断的单张候选图。',
      count: candidateCount,
      go: () => navigate('/reviews'),
    },
    {
      key: 'imageset',
      label: '图片集待批准',
      hint: '整套图片已完成编排，请确认这一版是否可以发布。',
      count: imageSetCount,
      go: () => onPick('APPROVE_IMAGE_SET'),
    },
    {
      key: 'copy',
      label: '文案待批准',
      hint: '标题、卖点和描述已生成，请审核后批准或退回。',
      count: copyCount,
      go: () => onPick('APPROVE_COPY'),
    },
    {
      key: 'conflict',
      label: '属性冲突',
      hint: '多个来源给出了不同属性值，请人工确认正确结果。',
      count: conflictCount,
      go: () => navigate('/workbench?next_action=RESOLVE_CONFLICT'),
    },
  ]

  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      {tiles.map((t) => {
        // 数字读不到就整块不显示 —— 见本组件文档
        if (t.count === null) return null
        return (
          <Tooltip key={t.key} title={t.hint}>
            <div
              role="button"
              tabIndex={0}
              onClick={t.go}
              onKeyDown={(e) => {
                // WAI-ARIA 的 button 模式要求 Enter 与 Space 都能激活
                if (e.key === 'Enter' || e.key === ' ') {
                  e.preventDefault()
                  t.go()
                }
              }}
              aria-label={`${t.label} ${t.count} 件 · ${t.hint}`}
              style={{
                border: `1px solid ${brandVars.line}`,
                background: brandVars.surface,
                borderRadius: 4,
                padding: '6px 14px',
                minWidth: 110,
                cursor: 'pointer',
              }}
            >
              <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>{t.label}</div>
              <div
                style={{
                  fontSize: fontScale.metric,
                  fontWeight: 600,
                  lineHeight: 1.2,
                  color: t.count ? brandVars.ink : brandVars.textFaint,
                }}
              >
                {t.count}
              </div>
            </div>
          </Tooltip>
        )
      })}
    </div>
  )
}

/** 这一页只处理这两个动作。其余动作不是"审",而是"做"。 */
const REVIEW_ACTIONS: NextActionCode[] = ['APPROVE_IMAGE_SET', 'APPROVE_COPY']

/**
 * 一次载入多少件。**抽成常量是因为界面要把这个数说出来**(见 `notLoaded`)——
 * 写死在请求里的话,调大它就会留下一句说错数的提示,而那句提示恰恰是
 * 为了消掉"两个数互相矛盾"才加的。
 *
 * 上限 100 与后端 `api/workbench.py` 的 `le=100` 对齐,不能再大。
 */
const QUEUE_PAGE_SIZE = 100

type Filter = 'ALL' | 'APPROVE_IMAGE_SET' | 'APPROVE_COPY'

/** 本轮处理过的一件。存 SKU 而不只是 id —— 理由见 `processed` 的注释 */
interface ProcessedItem {
  id: string
  sku: string
  action: 'approve' | 'reject'
  /**
   * 处理掉的是**哪一步**(FE-QUICK-REVIEW-03)。
   *
   * 少了这一维,屏蔽就是按商品 id 做的:批准了图片集之后,同一件商品的
   * 文案会紧接着变成待批准,而它在本次会话里永远不会再出现 —— 队列快照
   * 一旦重新拉取,它会回到数据里,然后被 `doneKeys` 按 id 滤掉。
   * 运营看到的是"文案队列少了一件",而少的正是他刚推进到那一步的那一件。
   *
   * **A45-#26:触发器订正。** 这段论证原来写的是"因窗口聚焦重新拉取
   * (TanStack Query 默认行为)",而 `main.tsx` 里全局配了
   * `refetchOnWindowFocus: false` —— 那个触发器**不存在**。
   *
   * 机制本身没有失效:队列还会因为决策后的 invalidate、手动刷新、
   * 以及切换筛选而重新拉取,按步屏蔽在这些路径上同样必要。改的是理由,
   * 不是结论 —— 而理由写错的代价是下一个人照着它去验证,验不出来,
   * 然后把整段机制当成多余的删掉。
   */
  step: NextActionCode
}

/** 屏蔽键:同一件商品的不同步骤是两件待办,不能互相顶掉 */
function doneKey(productId: string, step: NextActionCode): string {
  return `${productId}:${step}`
}

/** URL 上认这三个值(A9 首页的「待审核图片」「待审核文案」两张卡片) */
const FILTERS: Filter[] = ['ALL', 'APPROVE_IMAGE_SET', 'APPROVE_COPY']

// ---------------------------------------------------------------- 图片集一屏

function ImageSetPane({ productId, imageSetId }: { productId: string; imageSetId: string }) {
  const detail = useQuery({
    queryKey: ['image-set', imageSetId],
    queryFn: () => imageSetsApi.get(imageSetId),
  })
  const assets = useQuery({
    queryKey: ['workbench-media', productId],
    queryFn: () => mediaApi.list({ product_id: productId, page_size: 200 }),
  })

  const urlById = useMemo(() => {
    const map = new Map<string, string>()
    for (const asset of assets.data?.items ?? []) map.set(asset.id, asset.url)
    return map
  }, [assets.data])
  /*
   * 素材拉不到时,下面每一张图都会拿不到 URL 而渲染成裂图 —— 那读起来是
   * "这个 SPU 的图坏了",于是运营会去退回它。而真相只是这一次 GET 没成。
   * 快审页是**一屏一个决定**的地方,不能让它在信息不全时看起来完整。
   */
  const assetsFailed = assets.isError

  // 骨架按判定档占位,不用 Spin —— Spin 高度为 0,图回来时整块往下弹一次
  if (detail.isLoading) {
    return (
      <div className="asset-grid asset-grid--review">
        {[0, 1, 2].map((i) => (
          <Skeleton.Image key={i} active style={{ width: '100%', height: imageTile.review }} />
        ))}
      </div>
    )
  }
  if (detail.isError) return <Alert type="error" showIcon message={readError(detail.error)} />
  const set = detail.data
  if (!set) return null

  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Space size={8}>
        <Tag>v{set.version}</Tag>
        <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
          共 {set.items.length} 项 · 主图 {set.items.filter((i) => i.is_primary).length} 张
        </Typography.Text>
      </Space>
      {/* 校验问题原样列出。批准按钮的可用性由后端的下一步决定,这里只负责让人看见 */}
      {set.violations.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`图片集有 ${set.violations.length} 条校验问题`}
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {set.violations.map((v, i) => (
                <li key={i} style={{ fontSize: fontScale.body }}>{v.message}</li>
              ))}
            </ul>
          }
        />
      )}
      {/* 走查 P0-1/P0-2:这一屏原本是 132×160 的 `cover` 裸 <img>。
          两个问题叠在一起:`cover` 把画面裁掉,而运营在这一页唯一能做的动作就是
          盯着这张图按 A —— 裁掉的部分等于没审;裸 <img> 又没有 antd Image 的灯箱,
          想看细节只能跳去详情页,而这一页的全部意义就是不跳。
          现在:contain + 判定档尺寸 + PreviewGroup(点开可以在几张之间左右翻)。 */}
      {assetsFailed && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 8 }}
          message="拉不到素材,下面的图可能显示不全"
          description="这不是这个 SPU 的图有问题。在图补齐之前不要按批准或退回 —— 你现在看到的不是完整的一组。"
        />
      )}
      <div className="asset-grid asset-grid--review">
        <Image.PreviewGroup>
          {set.items.map((item, i) => {
            const url = urlById.get(item.media_asset_id)
            return (
              <figure
                key={item.id}
                className="asset-tile"
                style={{
                  margin: 0,
                  borderColor: item.is_primary ? brandVars.accent : brandVars.line,
                  borderWidth: item.is_primary ? 2 : 1,
                  opacity: item.enabled ? 1 : 0.45,
                }}
              >
                {url ? (
                  <Image
                    src={url}
                    alt={`图片集第 ${i + 1} 张 · ${
                      MEDIA_ROLE_LABEL[item.role] ?? item.role
                    }${item.is_primary ? ' · 主图' : ''}`}
                    preview={{ mask: '看大图' }}
                  />
                ) : (
                  <div
                    style={{
                      height: imageTile.review,
                      background: brandVars.imageBg,
                      display: 'grid',
                      placeItems: 'center',
                      color: brandVars.textMuted,
                      fontSize: fontScale.body,
                    }}
                  >
                    素材缺失
                  </div>
                )}
                <figcaption>
                  {item.is_primary && <BrandTag tone="accent" style={{ marginInlineEnd: 4 }}>主图</BrandTag>}
                  {MEDIA_ROLE_LABEL[item.role] ?? item.role}
                  {!item.enabled && <Tag style={{ marginInlineStart: 4 }}>已停用</Tag>}
                </figcaption>
              </figure>
            )
          })}
        </Image.PreviewGroup>
      </div>
    </Space>
  )
}

// ---------------------------------------------------------------- 文案一屏

function CopyPane({ copy }: { copy: ListingCopy }) {
  const status = COPY_STATUS_LABEL[copy.status] ?? { text: copy.status, color: 'default' }
  return (
    <Space direction="vertical" size={8} style={{ width: '100%' }}>
      <Space size={8}>
        <Tag color={status.color}>{status.text}</Tag>
        <Tag>v{copy.version}</Tag>
        <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
          {copy.locale} · {copy.model_name ?? '人工编辑'}
        </Typography.Text>
      </Space>
      {copy.violations.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`文案有 ${copy.violations.length} 条校验问题`}
          description={
            <ul style={{ margin: 0, paddingLeft: 18 }}>
              {copy.violations.map((v, i) => (
                <li key={i} style={{ fontSize: fontScale.body }}>{v.message}</li>
              ))}
            </ul>
          }
        />
      )}
      {/* 全文一屏,不折叠:折起来的部分等于没审 */}
      <Descriptions size="small" column={1} bordered>
        <Descriptions.Item label="标题">{copy.title ?? '—'}</Descriptions.Item>
        <Descriptions.Item label="卖点">
          <ul style={{ margin: 0, paddingLeft: 18 }}>
            {copy.bullet_points.map((b, i) => (
              <li key={i}>{b}</li>
            ))}
          </ul>
        </Descriptions.Item>
        <Descriptions.Item label="描述">
          {/* 走查 M-5:描述是这一屏唯一需要**逐字读**的东西,给它一个阅读宽度。
              不限宽的话在宽屏上会拉成一行 1500px 的长句,而中文一行超过 45 字
              左右眼睛就会跳行 —— 审文案时跳行等于漏审 */}
          <Typography.Paragraph
            style={{ marginBottom: 0, whiteSpace: 'pre-wrap', maxWidth: '46em' }}
          >
            {copy.description ?? '—'}
          </Typography.Paragraph>
        </Descriptions.Item>
        <Descriptions.Item label="关键词">{copy.keywords.join(' / ') || '—'}</Descriptions.Item>
        <Descriptions.Item label="声明用语">
          {copy.claims.length
            ? copy.claims.map((c, i) => (
                <Tag key={i} title={`${c.field_name} · ${c.location}`}>
                  {c.text_span}
                </Tag>
              ))
            : '无'}
        </Descriptions.Item>
      </Descriptions>
    </Space>
  )
}

// ---------------------------------------------------------------- 退回弹窗(A10)

/**
 * 选原因、可选填说明,确认后退回。
 *
 * 每个原因后面直接显示"退回后:去改属性"这类后果 —— 运营在选的那一刻就该
 * 知道会发生什么。选错原因就是选错下一步,这一点不该等到列表页才发现。
 */
function RejectModal({
  open, target, sku, pending, onCancel, onSubmit,
}: {
  open: boolean
  target: 'image_set' | 'copy'
  sku: string
  pending: boolean
  onCancel: () => void
  onSubmit: (reason: string, note: string) => void
}) {
  const [reason, setReason] = useState<string>('')
  const [note, setNote] = useState('')

  const reasons = useQuery({
    queryKey: ['reject-reasons', target],
    queryFn: () => workbenchApi.rejectReasons(target),
    // 九个原因不会在一次会话里变。不设的话每开一次弹窗闪一下加载态
    staleTime: 60 * 60 * 1000,
    enabled: open,
  })

  // 换一件商品(或换了退回对象)时清空,否则上一次选的原因会跟着带过来
  useEffect(() => {
    if (!open) return
    setReason('')
    setNote('')
  }, [open, target, sku])

  const options: RejectReasonOption[] = reasons.data ?? []
  const picked = options.find((o) => o.code === reason)
  const noteMissing = Boolean(picked?.note_required) && !note.trim()

  /**
   * 最近用过的原因。**这一行是快捷入口,不是排序。**
   *
   * 九个原因里高频的通常只有两三个,而 R 键的价值全在节奏上 —— 每次从九行里
   * 用眼睛找同一行,退回就比批准慢一截。
   *
   * 刻意**不重排下面那个单选列表**:顺序由后端给,而运营按位置形成肌肉记忆
   * (首页那七张卡片不许因为归零就消失,是同一条理由)。会动的列表还有一个
   * 更贵的后果:上一次点第 3 行,这一次第 3 行换了内容,而退回选错原因
   * 等于把商品送去错的下一步。所以高频项另起一行摆在上面,原列表一动不动。
   *
   * 只存 code:中文、下一步、是否必填说明全部来自后端那份清单
   * (`GET /workbench/reject-reasons`),本机存下来的一份会在原因改名那天骗人。
   * 后端撤掉某个原因时,它自然从这一行消失 —— 下面这个 filter 就是那道闸。
   */
  const recentCodes = readPref<string[]>(RECENT_REASONS_KEY, [])
  const recent = recentCodes
    .map((code) => options.find((o) => o.code === code))
    .filter((o): o is RejectReasonOption => Boolean(o))

  return (
    <Modal
      open={open}
      title={`退回 ${sku}`}
      okText="确认退回"
      cancelText="取消"
      okButtonProps={{ danger: true, disabled: !picked || noteMissing, loading: pending }}
      onOk={() => {
        if (!picked) return
        // 记在提交那一刻,不是选中那一刻:选了又改的原因不该被记成"用过"
        writePref(RECENT_REASONS_KEY, pushRecent(recentCodes, picked.code))
        onSubmit(picked.code, note.trim())
      }}
      onCancel={onCancel}
      destroyOnClose
      width={520}
    >
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
          退回不是跳过:这一件会离开待批准队列,并按下面选的原因给出明确的下一步。
        </Typography.Text>

        {reasons.isLoading && <Spin />}
        {reasons.isError && (
          <Alert type="error" showIcon message={readError(reasons.error)} />
        )}

        {recent.length > 0 && (
          <Space size={[6, 6]} wrap>
            <Typography.Text type="secondary" style={{ fontSize: fontScale.meta }}>
              最近用过:
            </Typography.Text>
            {recent.map((option) => (
              <Tooltip key={option.code} title={`退回后:${option.next_action_label}`}>
                <Tag.CheckableTag
                  checked={reason === option.code}
                  onChange={() => setReason(option.code)}
                >
                  {option.label}
                </Tag.CheckableTag>
              </Tooltip>
            ))}
          </Space>
        )}

        <Radio.Group
          value={reason}
          onChange={(e) => setReason(e.target.value)}
          style={{ width: '100%' }}
        >
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            {options.map((option) => (
              <Radio key={option.code} value={option.code}>
                {option.label}
                <Typography.Text type="secondary" style={{ fontSize: fontScale.meta, marginLeft: 8 }}>
                  退回后:{option.next_action_label}
                </Typography.Text>
              </Radio>
            ))}
          </Space>
        </Radio.Group>

        <Input.TextArea
          rows={3}
          value={note}
          onChange={(e) => setNote(e.target.value)}
          maxLength={500}
          showCount
          placeholder={
            picked?.note_required
              ? '必填:具体是什么问题(选了「其他」,系统不猜)'
              : '可选:补充说明。下一个接手的人会看到这句'
          }
        />
        {noteMissing && (
          <Alert
            type="warning"
            showIcon
            message="选了「其他」必须写明具体是什么问题"
            description="不写的话这条记录对下一个接手的人等于没有信息,事后也统计不出退回集中在哪一类。"
          />
        )}
      </Space>
    </Modal>
  )
}

// ---------------------------------------------------------------- 主体

export default function WorkbenchReviewPage() {
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const { message, notification } = App.useApp()
  /**
   * A9:首页的两张审核卡片直接带着要审的类别过来,省掉进页面再点一次分段器。
   * GAP-033 之后它住在 URL 里 —— 刷新、后退都还在,地址也贴得出去。
   *
   * **游标(`index`)刻意不进 URL。** 它是"队列里的第几张",而队列是活的:
   * 每次重新取数、每批准一件都会重排。把 `?index=7` 贴给同事,他打开看到的
   * 是另一件商品 —— 一个**看起来精确、实际上指错**的地址比不带游标更贵。
   * 筛选是条件(在两个人那里意思一样),游标是位置(不是)。
   */
  const urlFilters = useUrlFilters({ filter: enumParam<Filter>(FILTERS, 'ALL') })
  const { filter } = urlFilters.values
  const [index, setIndex] = useState(0)
  /**
   * 本次已处理的商品(批准或退回)。队列不重排,处理过的就地划掉。
   *
   * ## 二次走查 B-1:这里原本只存 id
   *
   * 只存 id 的后果不是少个字段,是**批错了查不回来**:那一件立刻从队列消失,
   * 唯一的反馈是 3 秒后自动消失的轻提示,而后端没有任何撤销接口。
   * 三秒之后运营连「我刚批的是哪一件」都答不出。
   *
   * 现在连 SKU 和动作一起存,页面上常驻列出来 —— 至少「刚才那一件是谁」
   * 永远可查,点一下能进详情页看它现在是什么状态。
   */
  const [processed, setProcessed] = useState<ProcessedItem[]>([])
  /** 屏蔽的是"这件商品的这一步",不是"这件商品"。理由见 `ProcessedItem.step` */
  const doneKeys = useMemo(
    () => new Set(processed.map((x) => doneKey(x.id, x.step))),
    [processed],
  )
  /** 退回弹窗开着时,J/K/A 全部让位 —— 否则在弹窗里按 A 会把这一件批了 */
  const [rejecting, setRejecting] = useState(false)
  /** 快捷键速查(走查 P1-6)。和退回弹窗一样要接管键盘 */
  const [helpOpen, setHelpOpen] = useState(false)

  // 换筛选就回到队列开头。**挂在 signature 上而不是写在分段器的 onChange 里**:
  // 后退/前进也会换筛选,而它们不经过分段器 —— 那时游标还停在上一条队列的
  // 第 7 位,而新队列可能只有 3 件(`current` 会取到最后一件,运营以为
  // 自己在审第 7 件)。理由与工作台列表清勾选那条相同,见 `useUrlFilters`
  useEffect(() => {
    setIndex(0)
  }, [urlFilters.signature])

  const wanted = filter === 'ALL' ? REVIEW_ACTIONS : [filter]

  // 一个动作一条请求:后端的 next_action 是单值筛选,拼成 OR 需要改接口,
  // 而两条并发请求在 50 件量级下比改接口便宜
  const queues = useQueries({
    queries: wanted.map((action) => ({
      queryKey: ['review-queue', action],
      queryFn: () =>
        workbenchApi.list({ next_action: action, page_size: QUEUE_PAGE_SIZE }),
    })),
  })

  const loading = queues.some((q) => q.isLoading)
  const failed = queues.find((q) => q.isError)

  /**
   * 这一轮**没载进来**的件数。0 表示队列就是全部。
   *
   * ## 为什么必须说出来
   *
   * 队列按每个动作各拉 `QUEUE_PAGE_SIZE` 条,而同一屏顶部的计数瓦片显示的是
   * 后端在**全量筛选结果**上算的真实 total。待批图片集有 260 件时,原来的表现是:
   *
   *     瓦片        260
   *     进度条      0 / 100        ← 两个数在同一屏上互相矛盾
   *     清完 100 件 「队列已空」    ← 其余 160 件在他手动点「重新载入队列」前不存在
   *
   * 批准是本系统定义的**不可撤销**动作,一个让人以为"审完了"的完成态,
   * 风险等级与数据缺陷同级 —— 他会带着这个结论去汇报。
   *
   * 这一页的注释把每个取舍都写了(快照不重排、只预取 1 件、100 件量级下
   * 并发请求比改接口便宜),唯独没写"超过 100 会怎样" —— 说明它是遗漏,
   * 不是取舍。所以补的是**如实说出来**,不是偷偷改成自动续拉:
   * 后者会让"快照不重排"那条设计当场失效(见 `doneKeys` 那一段)。
   */
  const notLoaded = queues.reduce((sum, q) => {
    const data = q.data
    if (!data) return sum
    return sum + Math.max(data.total - data.items.length, 0)
  }, 0)

  /*
   * 候选图审核的件数(a47 §6)。`page_size: 1` 只为了拿 `total` ——
   * 后端在**全量筛选结果**上算它,不是当页条数。
   *
   * 这是本页为计数条新增的唯一一次请求;另外两个数搭队列查询的车。
   */
  const candidateQueue = useQuery({
    queryKey: ['reviews', 'PENDING', 'count'],
    queryFn: () => reviewsApi.list({ status: 'PENDING', page_size: 1 }),
  })

  /*
   * 图片集/文案/属性冲突三个数都住在同一份 `summary.by_next_action` 里。
   *
   * **取哪一条 query 的 summary 有讲究**:`queues` 是按当前筛选发的,
   * 筛成「只看文案」时里面就没有图片集那一条。所以计数条单独发一次
   * 不带 next_action 的查询 —— `page_size: 1` 同理,要的只是 summary。
   * 少了这一步,计数条会跟着筛选变,而它恰恰是用来**切换**筛选的。
   */
  const summaryQuery = useQuery({
    queryKey: ['workbench', 'review-centre-summary'],
    queryFn: () => workbenchApi.list({ page_size: 1 }),
  })
  /*
   * 失败与 0 必须分得开:`isError` 时给 null,那几块整个不显示。
   *
   * `?? null` 单靠 `data` 为空也能得到 null,但那是**碰巧**对的 ——
   * `test_frontend_contract.py` 的「每个 query 句柄都要被问过 isError」
   * 盯的正是这种写法:它今天对,而下一个人给这几块加一个"读不到就显示 0"
   * 的兜底时不会有任何地方变红。
   */
  const byAction = summaryQuery.isError
    ? null
    : summaryQuery.data?.summary.by_next_action ?? null

  /**
   * 「这几条队列查询的数据换过没有」压成一个字符串。
   *
   * 这一行原来是内联在依赖数组里的 `queues.map(...).join(',')`,而
   * `exhaustive-deps` 对复杂表达式一律报警(它静态分析不了)——
   * **本仓当初把整条规则从 error 降成 warn,就是为了迁就这一处写法。**
   * 一条规则的强度被一个表达式的位置决定,那不划算:代价是全仓 16 个
   * 手写依赖数组从此无人看管,而那类缺陷("偶尔不刷新、状态错乱")
   * 恰恰是 UAT 里最难复现的。
   *
   * 提出来命名之后,规则看得懂它,升级就不再欠着这一处。
   */
  const queuesFreshness = queues.map((q) => q.dataUpdatedAt).join(',')

  /*
   * `queues` 本身不进依赖:`useQueries` 每次渲染都返回**新数组**,
   * 写进去等于让这个 memo 恒失效 —— 而它排的是整条审核队列的序,
   * 每次渲染重排会让"快照不重排"那条设计当场失效(见下面 `doneKeys`)。
   * 真正会变的那部分已经由 `queuesFreshness` 表达了。
   */
  const queue: WorkbenchListItem[] = useMemo(() => {
    const rows = queues.flatMap((q) => q.data?.items ?? [])
    // 按 SPU 再按 SKU 排序:同 SPU 的图片集/文案是共享对象,连着审能少看几遍同一组图
    return rows
      .filter((row) => !doneKeys.has(doneKey(row.product.id, row.flow.next_action.code)))
      .sort((a, b) =>
        a.product.spu === b.product.spu
          ? a.product.sku.localeCompare(b.product.sku)
          : a.product.spu.localeCompare(b.product.spu),
      )
    /*
     * 这条豁免消不掉,原因写在上面两段:`queues` 进依赖会让 memo 恒失效,
     * 而它排的是整条审核队列的序 —— 每次渲染重排会让「快照不重排」当场失效。
     * 真正会变的那部分已经由 `queuesFreshness` 表达了。
     *
     * **加新依赖的时候要回到这里。** disable 盖住的是整个依赖数组,不只是
     * `queues` 这一项:将来这个 memo 里读了第四个变量而忘了手动补进数组,
     * 规则本该提醒的那一次会被这条 disable 一起吞掉,表现是排序用着旧值,
     * 而界面上看不出任何异常。
     */
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queuesFreshness, doneKeys])

  const current = queue[Math.min(index, Math.max(queue.length - 1, 0))]

  // 队列长度变化后兜一次下标。放在 effect 里而不是渲染中改 state:
  // 渲染中 setState 会多跑一轮,而这件事不着急
  useEffect(() => {
    setIndex((i) => Math.min(i, Math.max(queue.length - 1, 0)))
  }, [queue.length])

  const detail = useQuery({
    queryKey: ['workbench-flow', current?.product.id],
    queryFn: () => workbenchApi.flow(current!.product.id),
    enabled: Boolean(current),
  })

  /**
   * 预取下一件(走查 P0-2)。
   *
   * ## 修的是什么
   *
   * 这一页是全站唯一按**吞吐**设计的页面(键盘流、进度条、批完自动下一件),
   * 设计目标写在文件头:把导航压成 0。但走查发现翻页链路是三跳串行:
   *
   *     按 J -> setIndex -> flow 请求才发出 -> 拿到 image_set.id
   *          -> imageSets.get + mediaApi.list 再发 -> 拿到 URL
   *          -> 浏览器这才开始下载 3~6 张 300px 判定档图
   *
   * 每按一次停 1~3 秒,50 件就是 1~2 分钟纯等待。而**队列快照早就在手里** ——
   * `queue[index + 1].product.id` 是已知的,完全可以提前问。
   *
   * ## 为什么只预取 1 件
   *
   * 2 件以上会在 100 件的队列上白拉一堆素材列表(每件 `page_size: 200`)。
   * 运营的手速再快也快不过一次预取,深度 1 就够把等待抹平。
   *
   * ## 为什么用 prefetchQuery 而不是多开一个 useQuery
   *
   * `prefetchQuery` 只写缓存、不订阅,所以下一件的数据回来时**不会触发本页重渲染** ——
   * 而重渲染会让运营正在看的那张图闪一下。等真的按了 J,`useQuery` 拿到的是
   * 已经在缓存里的数据,直接命中。
   */
  useEffect(() => {
    const next = queue[index + 1]
    if (!next) return
    let cancelled = false
    /** 预热用的 Image 对象。留着引用是为了卸载时能掐断没下完的请求 */
    const warmed: HTMLImageElement[] = []

    // flow 只写缓存不订阅:它回来时**不该触发本页重渲染** ——
    // 重渲染会让运营正在看的那张图闪一下。
    // 拿回值是为了接着预取图片集:`image_set.id` 只能从 flow 里得到,
    // 不接这一跳的话,按 J 之后 ImageSetPane 仍然要自己等一个来回
    queryClient
      .fetchQuery({
        queryKey: ['workbench-flow', next.product.id],
        queryFn: () => workbenchApi.flow(next.product.id),
        staleTime: 30_000,
      })
      .then((data) => {
        if (cancelled || !data.image_set) return
        queryClient.prefetchQuery({
          queryKey: ['image-set', data.image_set.id],
          queryFn: () => imageSetsApi.get(data.image_set!.id),
          staleTime: 30_000,
        })
      })
      .catch(() => {
        /* 同下:预取失败不影响任何事 */
      })

    // 素材列表要拿回值(URL 在里面),所以用 fetchQuery。它同样不订阅
    queryClient
      .fetchQuery({
        queryKey: ['workbench-media', next.product.id],
        queryFn: () => mediaApi.list({ product_id: next.product.id, page_size: 200 }),
        staleTime: 30_000,
      })
      .then((media) => {
        if (cancelled) return
        // 光有 URL 浏览器不会去下图,而这一页真正慢的就是图:
        // 300px 判定档,一件 3~6 张。new Image() 把它们塞进 HTTP 缓存,
        // 按 J 之后 <img> 直接命中。取前 8 张够覆盖一个图片集
        for (const asset of media.items.slice(0, 8)) {
          const img = new window.Image()
          img.src = asset.url
          warmed.push(img)
        }
      })
      .catch(() => {
        /* 预取失败不影响任何事:按 J 之后正常请求会照跑一遍 */
      })

    return () => {
      cancelled = true
      // 手速快过网速时(连按 J),掐掉上一件的预热,别和当前这件抢带宽
      for (const img of warmed) img.src = ''
    }
  }, [index, queue, queryClient])

  const action = detail.data?.flow.next_action.code
  const isImageSet = action === 'APPROVE_IMAGE_SET'
  const isCopy = action === 'APPROVE_COPY'
  /** 队列快照与当前状态对不上:别人批过、上游刚变过。交给详情页,不在这里猜 */
  const conflicted = Boolean(detail.data) && !isImageSet && !isCopy

  /**
   * 批准 / 退回失败时的收尾(BLOCK-05)。
   *
   * 这一页原来用 `readError`,于是超时后运营读到的是"请重试" —— 而这一页
   * 的重试就是再按一次 A,也就是**再签一次同一件**。批准不可撤销,审计里
   * 会留下两条,而"到底哪一次生效了"事后查不清。
   *
   * 结果未知时重新拉队列:这一步会把真实状态取回来。已经批掉的那一件
   * 下一步不再是批准动作,它自己就从队列里消失了 —— 这比任何提示都直接。
   */
  const onWriteError = useWriteError(
    useCallback(() => {
      for (const queued of REVIEW_ACTIONS) {
        queryClient.invalidateQueries({ queryKey: ['review-queue', queued] })
      }
      queryClient.invalidateQueries({ queryKey: ['workbench-flow'] })
    }, [queryClient]),
  )

  /** 批过一件:就地划掉,同一个下标自然指向下一件(队列缩短一位)。 */
  const advance = useCallback((item: ProcessedItem, remaining: number) => {
    setProcessed((prev) => [...prev, item])
    // 批掉的是最后一件时下标要夹回来,否则「下一件」按钮的禁用状态会错一位
    setIndex((i) => Math.min(i, Math.max(remaining - 1, 0)))
  }, [])

  const approve = useMutation({
    /**
     * 返回值就是"这次批的是谁、批的是哪一步"(FE-QUICK-REVIEW-06)。
     *
     * `onSuccess` 原来读的是 `current!.product` —— 那是**回调执行那一刻**的
     * 当前项,不是请求发出时的那一件。两者在正常情况下相同,而不同的那几种
     * 情况恰好是这一页的日常:按 A 之后立刻按 J 翻到下一件、队列因窗口聚焦
     * 重新拉取、批掉最后一件后下标被夹回来。于是常驻回执上写着 B 的 SKU,
     * 而实际批准的是 A —— 一个不可逆动作,回执指错了人。
     *
     * `current!` 的那个非空断言也一并消失:队列空掉时它会在运行期抛。
     */
    mutationFn: async () => {
      const data = detail.data
      if (!data) throw new Error('还没读到当前商品')
      const decided = {
        id: data.product.id,
        sku: data.product.sku,
        step: data.flow.next_action.code,
      }
      if (data.flow.next_action.code === 'APPROVE_IMAGE_SET') {
        if (!data.image_set) throw new Error('这件商品没有图片集')
        await imageSetsApi.approve(data.image_set.id)
        return decided
      }
      if (data.flow.next_action.code === 'APPROVE_COPY') {
        if (!data.copy) throw new Error('这件商品没有文案')
        await workbenchApi.approveCopy(data.product.id, data.copy.id)
        return decided
      }
      throw new Error('当前下一步不是批准动作')
    },
    onSuccess: ({ id, sku, step }) => {
      /**
       * 二次走查 B-1:这里原本是 `message.success`,3 秒后自己消失。
       *
       * 批准是**不可逆**的动作(图片集批准后不能原地改,重排要 derive 新版本),
       * 而后端没有撤销接口。一个不可逆动作的回执不该 3 秒就收走 ——
       * 这条判断这个仓库自己做过:批量结果当初正是因为
       * 「一闪就没,等于把待办说了一遍然后收走」才从 message 改成 notification。
       *
       * 顺带说明后果并给回跳:按错了至少能立刻打开那一件看看。
       */
      notification.success({
        key: `approved-${id}`,
        message: `已批准 ${sku}`,
        duration: null,
        description: (
          <Space direction="vertical" size={6} style={{ width: '100%' }}>
            <Typography.Text style={{ fontSize: fontScale.body }}>
              批准后不可原地修改;要改内容需要重排或重新生成,会产生新版本。
            </Typography.Text>
            <Button
              size="small"
              onClick={() => {
                notification.destroy(`approved-${id}`)
                navigate(`/workbench/${id}`)
              }}
            >
              打开这一件
            </Button>
          </Space>
        ),
      })
      advance({ id, sku, action: 'approve', step }, queue.length - 1)
      // 列表页 / 异常列表的徽标要跟着变,但**不重新拉本页队列**
      queryClient.invalidateQueries({ queryKey: ['workbench'] })
      queryClient.invalidateQueries({ queryKey: ['workbench-exceptions'] })
      /*
       * A45-#25:**预取播下去的两个键,决策之后必须一起失效。**
       *
       * 这一页用 `staleTime: 30_000` 预取了 `['workbench-flow', id]` 与
       * `['image-set', setId]`(为了按 J 之后不等来回)。而批准/退回改变的
       * 正是 flow 的 next_action 与图片集状态 —— 那两个键原来不在失效列表里。
       *
       * 后果:批完图片集 → 重载队列 → 同一件商品因文案回到队列时,30 秒内
       * detail 命中的是**旧 flow**(next_action 仍是"批图"),`canApprove` 为真,
       * 按 A 会对一个已批准的集再调一次 approve。后端 409 兜得住,
       * 但这是一个可稳定复现的误导态 —— 而"能稳定复现的误导"就是缺陷。
       */
      queryClient.invalidateQueries({ queryKey: ['workbench-flow', id] })
      queryClient.invalidateQueries({ queryKey: ['image-set'] })
    },
    onError: onWriteError,
  })

  const reject = useMutation({
    /** 同 approve:退回的对象在**请求发出时**确定,不在回调里重新读一次 */
    mutationFn: async (input: { reason: string; note: string }) => {
      const data = detail.data
      if (!data) throw new Error('还没读到当前商品')
      const payload = { reason: input.reason, note: input.note || null }
      const decided = {
        id: data.product.id,
        sku: data.product.sku,
        step: data.flow.next_action.code,
      }
      if (data.flow.next_action.code === 'APPROVE_IMAGE_SET') {
        if (!data.image_set) throw new Error('这件商品没有图片集')
        await imageSetsApi.reject(data.image_set.id, payload)
        return decided
      }
      if (data.flow.next_action.code === 'APPROVE_COPY') {
        if (!data.copy) throw new Error('这件商品没有文案')
        await workbenchApi.rejectCopy(data.product.id, data.copy.id, payload)
        return decided
      }
      throw new Error('当前下一步不是批准动作,没有可退回的对象')
    },
    onSuccess: ({ id, sku, step }) => {
      // 不说"已退回"就完事:退回的意义在于它产出了下一步,而那句话在列表页。
      // 这里只确认动作发生了,具体去哪一步由后端判定,不在前端复述一遍。
      // 退回**是可逆的**(它只是把件送回上游某一步),所以不像批准那样用常驻回执
      message.success(`已退回 ${sku},下一步已更新`)
      setRejecting(false)
      advance({ id, sku, action: 'reject', step }, queue.length - 1)
      queryClient.invalidateQueries({ queryKey: ['workbench'] })
      queryClient.invalidateQueries({ queryKey: ['workbench-exceptions'] })
      /*
       * A45-#25:**预取播下去的两个键,决策之后必须一起失效。**
       *
       * 这一页用 `staleTime: 30_000` 预取了 `['workbench-flow', id]` 与
       * `['image-set', setId]`(为了按 J 之后不等来回)。而批准/退回改变的
       * 正是 flow 的 next_action 与图片集状态 —— 那两个键原来不在失效列表里。
       *
       * 后果:批完图片集 → 重载队列 → 同一件商品因文案回到队列时,30 秒内
       * detail 命中的是**旧 flow**(next_action 仍是"批图"),`canApprove` 为真,
       * 按 A 会对一个已批准的集再调一次 approve。后端 409 兜得住,
       * 但这是一个可稳定复现的误导态 —— 而"能稳定复现的误导"就是缺陷。
       */
      queryClient.invalidateQueries({ queryKey: ['workbench-flow', id] })
      queryClient.invalidateQueries({ queryKey: ['image-set'] })
    },
    onError: onWriteError,
  })

  const reload = () => {
    setProcessed([])
    setIndex(0)
    for (const action of REVIEW_ACTIONS) {
      queryClient.invalidateQueries({ queryKey: ['review-queue', action] })
    }
  }

  /**
   * 审核决策锁(BLOCK-08 / FE-QUICK-REVIEW-04)。
   *
   * 原来 `canApprove` 只排除 `approve.isPending`、`canReject` 只排除
   * `reject.isPending` —— 于是「批准」在飞的时候「退回」是亮的。两个请求
   * 打到同一个图片集上,后端会按先到的那个改状态、拒掉后到的那个,
   * 而运营看到的是一条成功提示加一条失败提示,分不出哪个生效了。
   *
   * 更要紧的是这两件事的后果相反:批准让它往下走,退回把它送回上游。
   * 猜错的代价不是多点一次,是这件商品去了错的地方。
   */
  const decisionBusy = approve.isPending || reject.isPending

  const canApprove = Boolean(current) && !conflicted && !decisionBusy && !detail.isFetching
  /** 退回的前提和批准一样:队列快照对得上当前状态。冲突态一律交给详情页 */
  const canReject = Boolean(current) && !conflicted && !decisionBusy && !detail.isFetching
  /** 退的是图片集还是文案,由后端的下一步决定 —— 不由这一页猜 */
  const rejectTarget: 'image_set' | 'copy' = isCopy ? 'copy' : 'image_set'

  // 键盘流。焦点在输入框里时不接管按键 —— 否则在搜索框里打字会触发批准
  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      const target = event.target as HTMLElement | null
      const tag = target?.tagName?.toLowerCase()
      if (tag === 'input' || tag === 'textarea' || target?.isContentEditable) return
      if (event.metaKey || event.ctrlKey || event.altKey) return
      // 弹窗开着时这一页交出键盘:Esc 关弹窗由 antd 自己管,J/K/A 让位。
      // 不让位的话运营在弹窗里选原因时按到 A,批准的是他正准备退回的那一件
      if (rejecting) return
      // `?` 在任何时候都能开速查(除了输入框里),这是它作为帮助键的意义。
      // Shift+/ 是它在多数键盘布局上的实际按法
      if (event.key === '?' || (event.shiftKey && event.key === '/')) {
        setHelpOpen(true)
        event.preventDefault()
        return
      }
      // 速查开着时交出其余按键:在帮助页上按 A 不该把背后那件批了
      if (helpOpen) return
      /**
       * 请求在飞的时候连翻件也不许(FE-QUICK-REVIEW-05)。
       *
       * 按钮那一层的锁挡不住键盘:`canApprove` 只管 A,J/K 原来完全不设防。
       * 而运营的实际手法就是 A、J、A、J 连着按 —— 网络稍慢一点,
       * 第二个 A 落在**已经翻过去的下一件**上,他以为自己批的是刚看完那件。
       *
       * 翻件本身没有副作用,禁它是因为它会**改变 A 和 R 的作用对象**。
       * 这一步已经很快了(乐观情况几十毫秒),挡住的只是真正慢的那几次。
       */
      if (decisionBusy) {
        event.preventDefault()
        return
      }
      const key = event.key.toLowerCase()
      if (key === 'j') setIndex((i) => Math.min(i + 1, Math.max(queue.length - 1, 0)))
      else if (key === 'k') setIndex((i) => Math.max(i - 1, 0))
      else if (key === 'a') {
        if (canApprove) approve.mutate()
      } else if (key === 'r') {
        if (canReject) setRejecting(true)
      } else if (key === 'enter' && current) {
        navigate(`/workbench/${current.product.id}`)
      } else return
      event.preventDefault()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [
    queue.length, canApprove, canReject, decisionBusy, rejecting, helpOpen,
    current, approve, navigate,
  ])

  const total = queue.length + processed.length

  // 走查 P1-5:剩余量写进标签页标题。运营切去别的标签处理完一件事再切回来时,
  // 不用等页面渲染就知道还剩多少 —— 标签栏本身成了进度指示器
  useDocumentTitle(queue.length ? `审核中心 (${queue.length})` : '审核中心')

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      {/* a47 §6:计数入口。点进去仍是各自现有页面,底层一个模型都没动 */}
      <ReviewCentreTiles
        candidateCount={candidateQueue.isError ? null : candidateQueue.data?.total ?? null}
        imageSetCount={byAction?.APPROVE_IMAGE_SET ?? null}
        copyCount={byAction?.APPROVE_COPY ?? null}
        conflictCount={byAction?.RESOLVE_CONFLICT ?? null}
        onPick={(next) => urlFilters.patch({ filter: next })}
      />

      <Card
        size="small"
        title="逐件快审"
        extra={
          <Space>
            <Segmented
              size="small"
              value={filter}
              onChange={(value) => urlFilters.patch({ filter: value as Filter })}
              options={[
                { label: '全部待批准', value: 'ALL' },
                { label: '图片集', value: 'APPROVE_IMAGE_SET' },
                { label: '文案', value: 'APPROVE_COPY' },
              ]}
            />
            <Button size="small" icon={<ReloadOutlined />} onClick={reload}>
              重新载入队列
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          {/* 走查 P1-6:这里原本是四行常驻说明(按键 + 「不提供批量批准」+
              「跳过与退回的区别」)。那段话第一天很有价值,第二天起就是噪音 ——
              而它占着约 80px,把这一页真正的主角(300px 判定档候选图)
              每次都往下推。一个以「一屏一件」为设计目标的页面,
              不该有 80px 的常驻说明。

              常驻只留按键本身,规则说明进 `?` 速查 —— 那些是需要时才查的东西。 */}
          <Space size={10} wrap style={{ fontSize: fontScale.body }}>
            <Typography.Text type="secondary">
              每次处理一件，提交后自动进入下一件 · <kbd>J</kbd>/<kbd>K</kbd> 切换 ·{' '}
              <kbd>A</kbd> 批准 · <kbd>R</kbd> 退回
            </Typography.Text>
            <Typography.Link onClick={() => setHelpOpen(true)}>快捷键与规则 (?)</Typography.Link>
          </Space>
          {total > 0 && (
            <Progress
              percent={total ? Math.round((processed.length / total) * 100) : 0}
              format={() => `${processed.length} / ${total}`}
              size="small"
            />
          )}
          {/* 进度条的分母是**这一批**,不是全部待批。两者不等时必须常驻说出来,
              否则它和顶部瓦片那个数在同一屏上互相矛盾,而运营没有办法分辨
              哪个是真的 */}
          {notLoaded > 0 && (
            <Typography.Text type="warning" style={{ fontSize: fontScale.meta }}>
              队列一次只载入 {QUEUE_PAGE_SIZE} 件,另有 {notLoaded} 件还没进来。
              清完这一批后点「重新载入队列」继续。
            </Typography.Text>
          )}
          {/* 二次走查 B-1:本轮已处理名单。
              进度条只给分子(「3 / 20」),而运营想复查的恰恰是**名单** ——
              「刚才那三件是哪三件」。批准不可撤销,这份名单是按错之后
              唯一还能找回那一件的地方。点一下进详情页看它现在什么状态。 */}
          {processed.length > 0 && (
            <Space size={[6, 6]} wrap style={{ fontSize: fontScale.meta }}>
              <Typography.Text type="secondary" style={{ fontSize: fontScale.meta }}>
                本轮已处理:
              </Typography.Text>
              {processed.map((item) => (
                <Tooltip
                  key={item.id}
                  title={`${item.action === 'approve' ? '已批准' : '已退回'} · 点击打开详情页`}
                >
                  <Tag
                    style={{ cursor: 'pointer', marginInlineEnd: 0 }}
                    color={item.action === 'approve' ? undefined : brandVars.warning}
                    onClick={() => navigate(`/workbench/${item.id}`)}
                  >
                    {item.sku}
                  </Tag>
                </Tooltip>
              ))}
            </Space>
          )}
        </Space>
      </Card>

      {failed && <Alert type="error" showIcon message={readError(failed.error)} />}
      {loading && <Spin />}

      {!loading && !current && (
        <Card size="small">
          {/*
            完成态的判据是「队列空**且** notLoaded 为 0」,不只是队列空。
            少了后半句,清完第一批 100 件的人读到的是「队列已空」——
            而后面还排着 160 件。这句话会被他当成"今天审完了"。
          */}
          <Empty
            description={
              notLoaded > 0
                ? `这一批 ${processed.length} 件处理完了,还有 ${notLoaded} 件没载入 —— 点上面的「重新载入队列」继续`
                : processed.length
                  ? `这一轮处理了 ${processed.length} 件,队列已空`
                  : '当前没有待批准的商品'
            }
          />
        </Card>
      )}

      {current && (
        <Card
          size="small"
          title={
            <Space size={8} wrap>
              <Typography.Text strong>{current.product.sku}</Typography.Text>
              <Tag>{current.product.spu}</Tag>
              {/* §13.2:受众常驻。这里是**真正做审阅判断**的那一屏 ——
                  §13.4 第 5 条(模特受众与商品受众一致)必须逐张执行,
                  而不知道这是男装还是女装就执行不了 */}
              <AudienceTag product={current.product} />
              <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
                {current.product.name}
              </Typography.Text>
              <Tag color="processing">
                {NEXT_ACTION_LABEL[detail.data?.flow.next_action.code ?? 'DONE']}
              </Tag>
              <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
                第 {Math.min(index + 1, queue.length)} / {queue.length} 件
              </Typography.Text>
            </Space>
          }
          extra={
            <Space>
              {/* 翻件在请求飞行期间同样禁用 —— 和键盘那条路是同一个理由:
                  它会改变批准/退回的作用对象(FE-QUICK-REVIEW-05)。
                  两条路给不同的答案的话,运营会以为鼠标比键盘"更管用" */}
              <Button
                size="small"
                icon={<ArrowLeftOutlined />}
                disabled={index === 0 || decisionBusy}
                onClick={() => setIndex((i) => Math.max(i - 1, 0))}
              >
                上一件 (K)
              </Button>
              <Button
                size="small"
                icon={<ArrowRightOutlined />}
                disabled={index >= queue.length - 1 || decisionBusy}
                onClick={() => setIndex((i) => Math.min(i + 1, queue.length - 1))}
              >
                下一件 (J)
              </Button>
              <Button
                size="small"
                icon={<ExportOutlined />}
                onClick={() => navigate(`/workbench/${current.product.id}`)}
              >
                完整详情页 (Enter)
              </Button>
              <Button
                danger
                size="small"
                icon={<StopOutlined />}
                disabled={!canReject}
                loading={reject.isPending}
                onClick={() => setRejecting(true)}
              >
                退回 (R)
              </Button>
              <Button
                type="primary"
                size="small"
                icon={<CheckOutlined />}
                disabled={!canApprove}
                loading={approve.isPending}
                onClick={() => approve.mutate()}
              >
                批准 (A)
              </Button>
            </Space>
          }
        >
          {/* 走查 P0-2:这里原本是裸 `<Spin />`(全站唯一一处,其余 10 页都用
              Skeleton)。它的高度是 0,于是每翻一件都要经历「塌陷 -> 转圈 -> 图撑开」
              三段跳,运营的视线得跟着重新找一次落点。骨架按判定档尺寸占好位,
              图回来时原地替换,不发生任何位移 */}
          {/*
            * §13.3 防止盲点确认:标出需要重点检查的区域(按受众,见 §14)。
            * 放在图上方而不是折叠区里 —— 它要在人看图**之前**被读到。
            * 男装最容易被生成改掉的是腰头和裤长,而这两项在女装那张表里
            * 一项都没有,所以这一行不是装饰
            */}
          <div style={{ marginBottom: 8 }}>
            <ReviewFocusLine product={current.product} />
          </div>

          {detail.isLoading && (
            <div className="asset-grid asset-grid--review">
              {[0, 1, 2].map((i) => (
                <Skeleton.Image key={i} active style={{ width: '100%', height: imageTile.review }} />
              ))}
            </div>
          )}
          {detail.isError && (
            <Alert type="error" showIcon message={readError(detail.error)} />
          )}
          {conflicted && detail.data && (
            <Alert
              type="warning"
              showIcon
              style={{ marginBottom: 8 }}
              message="这件商品的下一步已经不是批准了"
              description={
                <>
                  现在的下一步是「{detail.data.flow.next_action.label}」:
                  {detail.data.flow.next_action.reason}
                  <br />
                  可能是别人刚批过,也可能上游变了。这一页不猜 ——
                  <Button
                    type="link"
                    size="small"
                    onClick={() => navigate(`/workbench/${current.product.id}`)}
                  >
                    到详情页处理
                  </Button>
                  ,或按 J 跳过它。
                </>
              }
            />
          )}
          {detail.data && isImageSet && detail.data.image_set && (
            <ImageSetPane
              productId={current.product.id}
              imageSetId={detail.data.image_set.id}
            />
          )}
          {detail.data && isCopy && detail.data.copy && (
            <CopyPane copy={detail.data.copy} />
          )}
        </Card>
      )}

      <KeyboardHelp open={helpOpen} onClose={() => setHelpOpen(false)} />

      {current && (
        <RejectModal
          open={rejecting}
          target={rejectTarget}
          sku={current.product.sku}
          pending={reject.isPending}
          onCancel={() => setRejecting(false)}
          onSubmit={(reason, note) => reject.mutate({ reason, note })}
        />
      )}
    </Space>
  )
}
