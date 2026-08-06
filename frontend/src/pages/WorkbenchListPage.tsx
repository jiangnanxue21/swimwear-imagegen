/**
 * 工作台商品列表(阶段 1:FE-101~104、FE-106)。
 *
 * 这一页要回答的唯一问题是「今天从哪件商品下手」。所以一行里挤了
 * 五个子状态、阻断数、待确认数和一个推荐动作 —— §3.6 的退出条件是
 * 运营看一件商品的中位判断时间 ≤ 10 秒,那就不能让他为了看卡点去点开详情。
 *
 * 状态、完成度、阻断数、下一步**全部来自后端**(§3.2.1),这一页不算任何一项。
 *
 * 阶段 3 在这一页加了多选(FE-301)。选择状态刻意**不跨屏保留** ——
 * 翻页、换排序、改任何一个筛选条件都清空。翻到第二页再点批量,运营以为
 * 在处理眼前这 20 件,而实际提交的是 40 件;「执行前显示可执行数量」那个
 * 确认框救不了这种误解,因为数字本身是对的,错的是他对"这些是谁"的理解。
 *
 * 这句话曾经只对翻页与换排序成立,六个筛选入口是漏的(BLOCK-09)。后来全部
 * 走 `changeFilter()` 一个口子;**而筛选搬进 URL 之后连那个口子也不够了** ——
 * 后退/前进会改筛选却不经过任何 JS。现在清空挂在 `filters.signature` 上:
 * 不管筛选是被谁改的,它都变。详见下面那条 effect。
 *
 * 阶段 0 收口(GAP-033):七个筛选项 + 排序 + 分页全部住在 URL 里,
 * 组件内不存第二份。地址可复制、刷新保留、后退保留 —— 这三条从前都不成立。
 */
import { useEffect, useState } from 'react'
import { useNavigate } from 'react-router-dom'
import {
  Button, Card, Empty, Input, Progress, Segmented, Select, Space, Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ArrowRightOutlined, ReloadOutlined, WarningOutlined } from '@ant-design/icons'
import { useQuery } from '@tanstack/react-query'
import ErrorNotice from '../components/ErrorNotice'
import { AudienceTag } from '../components/AudienceBadge'
import { AUDIENCES, AUDIENCE_LABEL, type Audience } from '../api/types'
import {
  enumParam, flagParam, intParam, oneOfParam, textParam, useUrlFilters,
} from '../hooks/useUrlFilters'
import {
  ACTION_TAB,
  FLOW_STEP_LABEL,
  FLOW_STEP_ORDER,
  NEXT_ACTION_LABEL,
  STEP_STATE_LABEL,
  STEP_TAB,
  detectFlowAnomaly,
  workbenchApi,
  type FlowStep,
  type NextActionCode,
  type StepState,
  type WorkbenchListItem,
} from '../api/workbench'
import { FlowStrip, FlowStripLegend, StepStateTag } from '../components/workbench/FlowBits'
import BatchActionBar from '../components/workbench/BatchActionBar'
import { brandVars, fontScale } from '../theme'
import { useServerSort } from '../hooks/useServerSort'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import PageHeader from '../components/PageHeader'

/** 列密度偏好。跟显示器走,不跟业务走 */
const DENSE_KEY = 'imagegen.workbench-dense'

/** 顶部四个计数。点一下就是一次筛选 —— 这是运营最常用的入口。 */
function SummaryTiles({
  summary,
  active,
  onPick,
}: {
  summary: { total: number; blocked: number; needs_confirm: number; exportable: number; done: number }
  active: 'blocked' | 'exportable' | null
  onPick: (which: 'blocked' | 'exportable' | null) => void
}) {
  const tiles: {
    key: 'total' | 'blocked' | 'needs_confirm' | 'exportable' | 'done'
    label: string
    hint: string
    color: string
    filter: 'blocked' | 'exportable' | null
  }[] = [
    { key: 'total', label: '筛选结果', hint: '当前筛选命中的商品数', color: brandVars.ink, filter: null },
    { key: 'blocked', label: '有阻断', hint: '存在阻断问题,走不到导出', color: brandVars.danger, filter: 'blocked' },
    { key: 'needs_confirm', label: '待确认', hint: '等人做决定:确认属性、批准、点导出', color: brandVars.sand, filter: null },
    { key: 'exportable', label: '可导出', hint: '草稿校验通过,点一下就能出文件', color: brandVars.marine, filter: 'exportable' },
    { key: 'done', label: '已完成', hint: '已导出且无过期', color: brandVars.success, filter: null },
  ]
  return (
    <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
      {tiles.map((t) => {
        const selected = t.filter !== null && active === t.filter
        return (
          <Tooltip key={t.key} title={t.hint}>
            <div
              role={t.filter ? 'button' : undefined}
              tabIndex={t.filter ? 0 : undefined}
              onClick={() => t.filter && onPick(selected ? null : t.filter)}
              // WAI-ARIA 的 button 模式要求 Enter 与 Space 都能激活。
              // 只认 Enter 的话,键盘用户按 Space 得到的是「页面往下滚了一屏」,
              // 而他以为自己点了这个筛选块
              onKeyDown={(e) => {
                if ((e.key === 'Enter' || e.key === ' ') && t.filter) {
                  e.preventDefault()
                  onPick(selected ? null : t.filter)
                }
              }}
              aria-pressed={selected}
              aria-label={`${t.label} ${summary[t.key]} 件 · ${t.hint}`}
              style={{
                border: `1px solid ${selected ? brandVars.marine : brandVars.line}`,
                background: selected ? brandVars.marineSoft : brandVars.surface,
                borderRadius: 4,
                padding: '6px 14px',
                minWidth: 96,
                cursor: t.filter ? 'pointer' : 'default',
              }}
            >
              <div style={{ fontSize: fontScale.meta, color: brandVars.slate }}>{t.label}</div>
              <div style={{ fontSize: fontScale.metric, fontWeight: 600, color: t.color, lineHeight: 1.2 }}>
                {summary[t.key]}
              </div>
            </div>
          </Tooltip>
        )
      })}
    </div>
  )
}

/**
 * 每页条数的档位。上限 100 **不是随便定的**:后端
 * `app/api/workbench.py` 的 `page_size: int = Query(default=20, ge=1, le=100)`
 * 就是 100,超了是 422。抄一份数字在这里有过期风险,所以
 * `test_a45_batch14_17_url_filters.py` 有一条跨语言守卫盯着
 * 「前端最大档位 ≤ 后端 le=」——加一档比后端还大时它会红。
 */
const PAGE_SIZES = [10, 20, 50, 100] as const

/**
 * 这一页能排的字段。三列 `sorter: true` 加一个默认值。
 *
 * 它必须是后端 `_LIST_SORT_KEYS` 的**子集**,守卫盯着这一条(取子集而不是
 * 相等:后端多出几个可排字段而前端没开列,那不是缺陷)。
 * 白名单在这里的作用是挡手敲 —— `?sort=drop_table` 退回默认排序,
 * 而不是发给后端让它 422。
 */
const LIST_SORT_KEYS = ['updated_at', 'sku', 'completion', 'blocking_count'] as const

export default function WorkbenchListPage() {
  const navigate = useNavigate()

  /**
   * 筛选条件住在 URL 里(GAP-033)。这一页有七项,全在这张表里 ——
   * **加筛选项 = 往这张表加一行**,而不是再开一个 useState。
   *
   * A9:首页卡片带着 `?next_action=` 过来。参数名和从前一字不差,
   * 所以旧链接、旧书签照旧能用;区别只是这一页不再把它擦掉了。
   */
  const filters = useUrlFilters({
    search: textParam(),
    step: enumParam<FlowStep>(FLOW_STEP_ORDER),
    state: enumParam<StepState>(Object.keys(STEP_STATE_LABEL)),
    next_action: enumParam<NextActionCode>(Object.keys(NEXT_ACTION_LABEL)),
    /**
     * 受众筛选。**它是批量导出闸口的配套动作,不是一个方便功能。**
     *
     * 批次内商品分属不同规则包时,后端拒绝合并导出并让运营"按受众分成
     * 两个批次分别导出" —— 而在这个筛选之前,界面上**没有任何手段**能按
     * 受众把批次拆开。§9.5 要求"错误必须包含行动";那条错误给了指令
     * 却没给行动,运营只能一件件点开看受众。
     */
    audience: enumParam<Audience | 'UNCONFIRMED'>([...AUDIENCES, 'UNCONFIRMED']),
    only_blocked: flagParam(),
    page: intParam(1, { min: 1 }),
    page_size: oneOfParam(20, PAGE_SIZES),
    sort: enumParam<string>(LIST_SORT_KEYS),
    order: enumParam<'asc' | 'desc'>(['asc', 'desc']),
  })
  const {
    search, step, state, next_action: nextAction, audience, only_blocked: onlyBlocked,
    page, page_size: pageSize,
  } = filters.values

  /** 输入框里的字。与已生效的 `search` 分开 —— 敲字不该每个字符打一次接口
      (也不该每个字符进一格后退栈)。生效的那一份在 URL 上,所以刷新、后退
      都要把框里的字同步回来,见下面那条 effect */
  const [draft, setDraft] = useState(search)
  const [selected, setSelected] = useState<string[]>([])

  /**
   * 筛选变了就清掉勾选(BLOCK-09 / FE-WORKBENCH-LIST)。
   *
   * ## 为什么必须清
   *
   * antd 的 `rowSelection` 是**受控**的:`selectedRowKeys` 里那些不在当前
   * `dataSource` 里的 key 不会被自动剔除,它们照样计入 `selected.length`,
   * 照样被批量动作条提交。于是这条动线是通的:勾 20 件 -> 换成「只看有阻断」
   * (剩 3 件)-> 动作条说「已选 20 件」-> 点批量识别 -> 提交的是那 20 件,
   * 其中 17 件根本不在他眼前。
   *
   * 「执行前显示可执行数量」那个确认框救不了这种误解,因为**数字本身是对的** ——
   * 错的是运营对"这 20 件是谁"的理解。
   *
   * ## 为什么从 `changeFilter()` 挪到了这里(GAP-033)
   *
   * 上一版把清空写在 `changeFilter()` 里,六个筛选入口都走那一个口子。
   * **筛选搬进 URL 之后那个口子不再是唯一入口**:浏览器后退、前进、
   * 直接编辑地址栏都会改筛选,而它们一行 JS 都不经过。勾 20 件、按一次后退,
   * 动作条照样说「已选 20 件」—— BLOCK-09 换了一扇门原样回来。
   *
   * 挂在 `filters.signature` 上就没有那扇门:不管筛选是被谁改的,它都变。
   * 首次挂载也会跑一次,那时 `selected` 本来就是空的。
   */
  useEffect(() => {
    setSelected([])
  }, [filters.signature])

  /**
   * 搜索框的字跟着 URL 走。
   *
   * 生效的搜索词在 URL 上,框里的字是本地的 —— 两份就要同步:刷新、后退、
   * 或者点「清除筛选」之后,不同步的表现是**框里留着字、列表却是全量**,
   * 而运营看到的是"我明明筛着呢"。
   */
  useEffect(() => {
    setDraft(search)
  }, [search])

  /**
   * 列密度(走查 P0-1)。展开时五个步骤列各占 96px,表格最小 1280px;
   * 而外框固定吃掉 230px(侧栏 190 + 内容区 padding 40),于是 1366 的屏幕
   * 缺 144px、1440 缺 70px —— 缺掉的正是最后一列那颗主按钮。
   *
   * 默认值按视口给,而不是一律紧凑:大屏用户本来就装得下五列,
   * 而五列的信息量确实更高,没理由替他收起来。
   *
   * 选择记进 localStorage:这是个人偏好(跟显示器走),不是业务状态,
   * 不该每次进页面重新决定一次。
   */
  const [dense, setDense] = useState<boolean>(() => {
    try {
      const saved = window.localStorage.getItem(DENSE_KEY)
      if (saved === '1') return true
      if (saved === '0') return false
    } catch {
      /* 隐私模式:当作没设置过,走视口判断 */
    }
    // 1510 = 表格 1280 + 外框 230。低于它展开就一定会横滚
    return window.innerWidth < 1510
  })

  /**
   * 二次走查 A-3:跟随窗口变化。
   *
   * 改动前 `window.innerWidth` 只在挂载时读一次。而接外接显示器、拔线去开会、
   * 把浏览器拖成半屏是每天都发生的事 —— 14 寸上进来默认紧凑,插上 27 寸仍然紧凑
   * (白白浪费一半屏宽);反过来在大屏打开、拔线去会议室,主按钮又被挤出去了,
   * 正是 P0-1 要修的那个问题换了个触发路径。
   *
   * **手动设置过就不再自动跟随**:那是用户的明确表态,不该被窗口尺寸推翻。
   * 用 matchMedia 而不是 resize:后者拖窗口时每帧触发,而这里只关心
   * 一个阈值有没有被跨过。
   */
  useEffect(() => {
    /**
     * 判据必须**每次触发时现读**,不能只在挂载时读一次。
     *
     * 上一版是在 effect 顶部 `if (localStorage.getItem(DENSE_KEY) !== null) return`,
     * 依赖数组是空的 —— 于是"挂载时还没设置过"的那一次注册的监听器会一直活着,
     * 而 `toggleDense()` 写完 localStorage 不会让这个 effect 重跑。结果是:
     * 进页面(没设置过,注册监听)→ 手动切成展开 → 拖一次窗口跨过 1510px
     * → 被改回紧凑。**正是上面那句「不该被窗口尺寸推翻」要防的事**。
     *
     * 放进 `sync` 里之后,判据和它要保护的那个动作在同一时刻求值,
     * 中间没有可以过期的窗口。
     */
    const manuallySet = () => {
      try {
        return window.localStorage.getItem(DENSE_KEY) !== null
      } catch {
        /* 隐私模式:读不到就当没设置过,继续跟随 */
        return false
      }
    }
    if (manuallySet()) return
    // 1510 = 表格 1280 + 外框 230
    const mq = window.matchMedia('(max-width: 1509px)')
    const sync = (e: MediaQueryListEvent | MediaQueryList) => {
      if (manuallySet()) return
      setDense(e.matches)
    }
    sync(mq)
    mq.addEventListener('change', sync)
    return () => mq.removeEventListener('change', sync)
  }, [])

  const toggleDense = (next: boolean) => {
    setDense(next)
    try {
      window.localStorage.setItem(DENSE_KEY, next ? '1' : '0')
    } catch {
      /* 存不下不影响本次会话 */
    }
  }

  // 从前这里有一条 effect,专门接「已经挂载着这一页时又从首页点了另一张卡片」
  // (react-router 不重新挂载组件,靠初值接不到)。URL 是唯一真相之后它没有了
  // 存在理由:参数变了 -> `useSearchParams` 变了 -> `values` 变了,
  // 组件里没有任何一份需要被"同步"的副本。

  // 服务端排序。这一页的排序在后端对**全量筛选结果**做完才切页,所以
  // 「完成度」「阻断数」这两个 flow_rules 现算出来的维度也能排 —— 它们不是数据库列。
  //
  // 换排序回第一页,并且清空选择:选择状态本来就不跨页保留(见文件头注释),
  // 换了排序之后「眼前这 20 件」已经不是刚才那 20 件了,留着更危险。
  // 换排序回第一页。**一次 patch 写完三个参数**:分开写会进三格后退栈,
  // 而运营按一次后退看到的是"排序变了但页码没回去"这种没人产生过的中间态。
  // 清空勾选不写在这里 —— 它挂在 signature 上(见上面那条 effect)。
  const sort = useServerSort({ sort: 'updated_at', order: 'desc' }, undefined, {
    value: {
      sort: filters.values.sort ?? 'updated_at',
      order: filters.values.order ?? 'desc',
    },
    set: (next) => filters.patch({ sort: next.sort, order: next.order, page: 1 }),
  })

  const query = useQuery({
    queryKey: [
      'workbench',
      { search, step, state, nextAction, audience, onlyBlocked, page, pageSize, ...sort.params },
    ],
    queryFn: () =>
      workbenchApi.list({
        search: search || undefined,
        step,
        state,
        next_action: nextAction,
        audience,
        only_blocked: onlyBlocked || undefined,
        ...sort.params,
        page,
        page_size: pageSize,
      }),
  })

  // 「清除筛选」= 把这一页声明过的参数从 URL 上全删掉。URL 上别的参数
  // (将来可能有的 `?tab=`)不动 —— 这一页没资格替别人做决定。
  // 框里的字由上面那条同步 effect 跟着清,不在这里各写一遍。
  const reset = () => filters.reset()

  useDocumentTitle(
    query.data ? `运营工作台 (${query.data.total})` : '运营工作台',
  )

  const open = (item: WorkbenchListItem, tab?: string) => {
    const suffix = tab ? `?tab=${tab}` : ''
    navigate(`/workbench/${item.product.id}${suffix}`)
  }

  const columns: ColumnsType<WorkbenchListItem> = [
    {
      // 排的是 SKU(列里同时显示 SPU 与名称,但排序键必须只有一个,
      // 而运营记的是 SKU —— 它也是这张表的唯一键)
      title: '商品',
      key: 'sku',
      width: 260,
      fixed: 'left',
      sorter: true,
      sortOrder: sort.orderFor('sku'),
      render: (_, row) => (
        <Space size={8} align="start">
          {row.thumbnail_url ? (
            <img
              src={row.thumbnail_url}
              alt=""
              style={{ width: 44, height: 44, objectFit: 'contain', background: brandVars.imageBg, borderRadius: 3 }}
            />
          ) : (
            // FE-101:无主图显示占位和警告。缺正面图本身就是阻断项,这里只做视觉提示
            <Tooltip title="没有可用主图。素材列会显示具体缺什么">
              <div
                style={{
                  width: 44, height: 44, background: brandVars.dangerBg, borderRadius: 3,
                  display: 'grid', placeItems: 'center', color: brandVars.danger,
                }}
              >
                <WarningOutlined />
              </div>
            </Tooltip>
          )}
          <div style={{ minWidth: 0 }}>
            <a className="mono" onClick={() => open(row)}>
              {row.product.sku}
            </a>
            <div style={{ color: brandVars.slate, fontSize: fontScale.meta }}>
              {row.product.spu} <AudienceTag product={row.product} />
            </div>
            <div style={{ color: brandVars.slate, fontSize: fontScale.meta, maxWidth: 190, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
              {row.product.name}
            </div>
          </div>
        </Space>
      ),
    },
    {
      title: '完成度',
      key: 'completion',
      width: 110,
      // 上一轮这里挂的是客户端比较函数,而这张表是服务端分页的 ——
      // 它排的是当前这 20 行。现在交给后端,排的是全部命中
      sorter: true,
      sortOrder: sort.orderFor('completion'),
      render: (_, row) => (
        <Tooltip title="五步等权各 20%,每步按状态计分:完成 100%、待确认/待批准 60%、进行中 30%、未开始/阻断/过期 0%。过期算 0 分 —— 这样过期件不会顶着一个高完成度沉到列表底下">
          <Progress
            percent={row.flow.completion}
            size="small"
            strokeColor={row.flow.blocking_count ? brandVars.danger : brandVars.marine}
          />
        </Tooltip>
      ),
    },
    // 五个子状态(FE-102)。两种形态,由 `dense` 切换 —— 走查 P0-1:
    // 展开形态 5×96 = 480px,把最后一列「唯一下一步」挤出了 1366/1440 的屏幕,
    // 而那颗按钮是这一整页存在的理由
    ...(dense
      ? [
          {
            title: '流程',
            key: 'flow',
            width: 96,
            align: 'center' as const,
            render: (_: unknown, row: WorkbenchListItem) => (
              <FlowStrip steps={row.flow.steps} onJump={(s) => open(row, STEP_TAB[s])} />
            ),
          },
        ]
      : FLOW_STEP_ORDER.map<ColumnsType<WorkbenchListItem>[number]>((s) => ({
          title: FLOW_STEP_LABEL[s],
          key: s,
          width: 96,
          align: 'center',
          render: (_: unknown, row: WorkbenchListItem) => {
            const found = row.flow.steps.find((x) => x.step === s)
            if (!found) return <span style={{ color: brandVars.textFaint }}>—</span>
            return <StepStateTag step={found.step} state={found.state} summary={found.summary} />
          },
        }))),
    {
      title: '阻断 / 待确认',
      key: 'issues',
      width: 120,
      align: 'center',
      sorter: true,
      sortOrder: sort.orderFor('blocking_count'),
      render: (_, row) => (
        <Space size={6}>
          {/* §3.3.2:徽标点击直接跳到详情对应标签页并定位首个该级别问题 */}
          <Tooltip title={row.flow.blocking_count ? '点击查看第一条阻断问题' : '没有阻断问题'}>
            <Tag
              color={row.flow.blocking_count ? 'error' : 'default'}
              style={{ cursor: row.flow.blocking_count ? 'pointer' : 'default', marginInlineEnd: 0 }}
              onClick={() => row.flow.blocking_count && open(row, 'overview')}
            >
              {row.flow.blocking_count}
            </Tag>
          </Tooltip>
          <Tag
            color={row.flow.pending_count ? 'warning' : 'default'}
            style={{ cursor: row.flow.pending_count ? 'pointer' : 'default', marginInlineEnd: 0 }}
            onClick={() => row.flow.pending_count && open(row, 'overview')}
          >
            {row.flow.pending_count}
          </Tag>
        </Space>
      ),
    },
    {
      title: '唯一下一步',
      key: 'next',
      width: 220,
      // 走查 P0-1:这一列是整页的核心 CTA,不能跟着横向滚动跑掉。
      // `dense` 之后表格通常已经装得下,但 100% 缩放的 1280 屏、
      // 或者用户把浏览器开成半屏时仍然会滚 —— 钉住的成本是零
      fixed: 'right',
      render: (_, row) => {
        // §3.2.3:状态组合非法时不展示任何动作,只报异常
        const anomaly = detectFlowAnomaly(row.flow)
        if (anomaly) {
          return (
            <Tooltip title={`${anomaly.reason} · ${anomaly.detail}`}>
              <Tag color="error" icon={<WarningOutlined />}>
                状态异常
              </Tag>
            </Tooltip>
          )
        }
        const action = row.flow.next_action
        if (action.code === 'DONE') {
          return <Tag color="success">已完成</Tag>
        }
        return (
          <Tooltip title={action.reason}>
            <Button
              size="small"
              type="primary"
              icon={<ArrowRightOutlined />}
              onClick={() => open(row, ACTION_TAB[action.code])}
            >
              {action.label || NEXT_ACTION_LABEL[action.code]}
            </Button>
          </Tooltip>
        )
      },
    },
  ]

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader
        title="运营工作台"
        subtitle="这一页回答一个问题:今天从哪件商品下手"
        extra={
          <>
            <Tooltip
              title={
                dense
                  ? '展开五个步骤列。屏幕不够宽时表格会横向滚动'
                  : '把五个步骤压成一格色条,表格窄 384px —— 小屏上主按钮才不会被挤出去'
              }
            >
              <Segmented
                size="small"
                value={dense ? 'dense' : 'wide'}
                onChange={(v) => toggleDense(v === 'dense')}
                options={[
                  { label: '紧凑', value: 'dense' },
                  { label: '展开', value: 'wide' },
                ]}
              />
            </Tooltip>
            <Button
              size="small"
              icon={<ReloadOutlined />}
              onClick={() => query.refetch()}
              loading={query.isFetching}
            >
              刷新
            </Button>
          </>
        }
      />

      {query.data && (
        <SummaryTiles
          summary={query.data.summary}
          active={onlyBlocked ? 'blocked' : nextAction === 'EXPORT' ? 'exportable' : null}
          onPick={(which) =>
            filters.patch({
              only_blocked: which === 'blocked',
              next_action: which === 'exportable' ? 'EXPORT' : undefined,
              page: 1,
            })
          }
        />
      )}

      {selected.length > 0 && (
        <BatchActionBar
          selected={selected}
          onClear={() => setSelected([])}
          onDone={() => query.refetch()}
        />
      )}

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space wrap>
          <Input.Search
            allowClear
            placeholder="搜索 SKU / SPU / 名称"
            style={{ width: 240 }}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onSearch={(v) => filters.patch({ search: v, page: 1 })}
          />
          <Select<FlowStep>
            allowClear
            placeholder="流程步骤"
            style={{ width: 130 }}
            value={step}
            onChange={(v) => filters.patch({ step: v, page: 1 })}
            options={FLOW_STEP_ORDER.map((s) => ({ value: s, label: FLOW_STEP_LABEL[s] }))}
          />
          <Tooltip title="不选步骤时,任一步处于该状态就命中;选了步骤则只看那一步">
            <Select<StepState>
              allowClear
              placeholder="状态"
              style={{ width: 130 }}
              value={state}
              onChange={(v) => filters.patch({ state: v, page: 1 })}
              options={Object.entries(STEP_STATE_LABEL).map(([value, meta]) => ({
                value: value as StepState,
                label: meta.text,
              }))}
            />
          </Tooltip>
          <Select<NextActionCode>
            allowClear
            placeholder="下一步动作"
            style={{ width: 170 }}
            value={nextAction}
            onChange={(v) => filters.patch({ next_action: v, page: 1 })}
            options={Object.entries(NEXT_ACTION_LABEL).map(([value, label]) => ({
              value: value as NextActionCode,
              label,
            }))}
          />
          {/* 受众筛选:批量导出要求"按受众分成两个批次"时,这是执行那句话的地方 */}
          <Tooltip title="批量导出要求同一批次只含一个规则包。按受众筛完再全选,就是一个可导出的批次">
            <Select<Audience | 'UNCONFIRMED'>
              allowClear
              placeholder="受众"
              style={{ width: 120 }}
              value={audience}
              onChange={(v) => filters.patch({ audience: v, page: 1 })}
              options={[
                ...AUDIENCES.map((v) => ({ value: v as Audience, label: AUDIENCE_LABEL[v] })),
                { value: 'UNCONFIRMED' as const, label: '待确认' },
              ]}
            />
          </Tooltip>
          <Button
            type={onlyBlocked ? 'primary' : 'default'}
            onClick={() => filters.patch({ only_blocked: !onlyBlocked, page: 1 })}
          >
            只看有阻断
          </Button>
          <Button onClick={reset}>清除筛选</Button>
        </Space>
      </Card>

      {/* 二次走查 A-2:紧凑模式的图例。不给的话那五个色块在界面上
          没有任何地方解释「左起第一格是哪一步」「哪个色是哪个状态」 */}
      {dense && <FlowStripLegend />}

      {/* §3.5 步骤 7:后端断开时要有失败状态与重试入口,不白屏、不显示空列表 */}
      {query.isError && (
        <ErrorNotice
          title="拉不到商品流程状态"
          error={query.error}
          onRetry={() => query.refetch()}
        />
      )}

      {/* 排序走服务端(useServerSort)。列上是 `sorter: true` 而不是比较函数 ——
          传函数就是客户端排序,排的只有当前这 20 行,而运营以为排的是全部命中。
          后端 `_LIST_SORT_KEYS` 在**全量筛选结果**上排完才切页。 */}
      <Table<WorkbenchListItem>
        rowKey={(row) => row.product.id}
        size="small"
        bordered
        // 跟着列密度走。紧凑时 = 勾选 48 + 商品 260 + 完成度 110 + 流程 96
        // + 阻断/待确认 120 + 下一步 220 ≈ 854;展开时五步列多吃 384px
        scroll={{ x: dense ? 900 : 1280 }}
        /* 二次走查 A-4:表头跟着吸顶。上一轮只让批量动作条吸顶,修了一半 ——
           滚到第 15 行时按钮在、列名却跑了,运营勾选时不知道自己在看哪一列。
           offsetHeader 必须给:动作条出现时会占住顶部 44px,不避开的话
           表头会被它盖住,那比现在更糟 */
        sticky={{ offsetHeader: selected.length > 0 ? 44 : 0 }}
        columns={columns}
        dataSource={query.isError ? [] : query.data?.items ?? []}
        loading={query.isFetching}
        onRow={(row) => ({ onDoubleClick: () => open(row) })}
        rowSelection={{
          selectedRowKeys: selected,
          onChange: (keys) => setSelected(keys.map(String)),
          preserveSelectedRowKeys: false,
        }}
        locale={{
          emptyText: query.isError ? (
            <Empty description="加载失败,点上方「重试」再试一次。" />
          ) : (
            <Empty
              description={
                search || step || state || nextAction || onlyBlocked
                  ? '没有商品命中当前筛选。点「清除筛选」看全部。'
                  : '还没有商品。先在「商品」页导入 sample-data/products.csv。'
              }
            />
          ),
        }}
        onChange={sort.onTableChange}
        pagination={{
          current: page,
          pageSize,
          total: query.data?.total ?? 0,
          showSizeChanger: true,
          // 档位必须和 `PAGE_SIZES` 同源:codec 只认这几档,
          // 让 antd 自己列一份的话,选了它不认的档会被静默退回 20
          pageSizeOptions: PAGE_SIZES.map(String),
          showTotal: (t) => `共 ${t} 件命中`,
          // 勾选由 signature 那条 effect 清,这里不再各写一遍
          onChange: (p, ps) => filters.patch({ page: p, page_size: ps }),
        }}
      />
    </Space>
  )
}
