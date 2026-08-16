/**
 * 属性标签(FE-201~205)。
 *
 * ## 冲突不得静默覆盖(§5.3 指标:0 次)
 *
 * 冲突项在表里**没有默认选中值**,「确认」按钮对冲突项不可用,必须先从
 * 并列的候选里点一个,或手填一个第三值。少了这一条,运营一路点「全部确认」
 * 就会把冲突静默地按某个顺序解决掉,而验收要的正是「人工选择后保留操作记录」。
 *
 * ## 两个置信度分开显示
 *
 * `system_confidence` 是唯一参与阈值判定的量,`null` 表示**没校准过**,
 * 不是「置信度为零」。这两种情况运营要做的事不同(灌校准数据 vs 重拍图),
 * 所以未校准显示「未校准」而不是 0%。
 */
import { useCallback, useEffect, useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Descriptions, Empty, Input, InputNumber, Modal, Popover,
  Progress, Select, Skeleton, Space, Switch, Table, Tag, Tooltip,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { ThunderboltOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  ATTRIBUTE_SOURCE_LABEL,
  ATTRIBUTE_STATUS_LABEL,
  MISSING_REASON_ACTION,
  RUN_STATUS_LABEL,
  RUN_STATUS_NOTICE,
  attributeLabel,
  attributesApi,
  factsStale,
  type AttributeFieldSpec,
  type AttributeValue,
  type Evidence,
} from '../../api/attributes'
import { useWriteError } from '../../hooks/useWriteError'
import ErrorNotice from '../ErrorNotice'
import { IssueList } from './FlowBits'
import type { FlowStepResult, WorkbenchProduct } from '../../api/workbench'
import AudienceConfirmCard from './AudienceConfirmCard'
import UnsavedGuard from '../UnsavedGuard'
import { brandVars, fontScale } from '../../theme'
import { formatDateTime } from '../../utils/datetime'

function displayValue(row: AttributeValue): string {
  if (row.normalized_value) return row.normalized_value
  const raw = row.value_json
  if (raw === null || raw === undefined) return ''
  if (Array.isArray(raw)) return raw.join(' / ')
  if (typeof raw === 'object') return JSON.stringify(raw)
  return String(raw)
}

/**
 * 后端没给字段类型时的退路(旧后端)。**只有这一处** ——
 * 散在控件里就变成了"前端自己推断类型",那是硬规则第 4 条禁止的。
 *
 * 退到"不带约束的单值文本":它对存量文本字段是对的,对列表字段会退化成
 * 只读(见 `editorKind`),而只读的代价是改不了,远小于把数组存成字符串。
 */
const FALLBACK_SPEC: AttributeFieldSpec = {
  value_type: 'TEXT',
  multi_value: false,
  options: [],
}

function specOf(row: AttributeValue): AttributeFieldSpec {
  return row.spec ?? FALLBACK_SPEC
}

/**
 * 这一行用什么控件编辑(BLOCK-11)。
 *
 * ## 修的是什么
 *
 * 原来所有字段共用一个纯文本 `Input`,`edits` 是 `Record<string, string>`,
 * 于是提交回去的**一律是字符串**。`secondary_colors` 是 TEXT_LIST,
 * 界面上显示成 `RED / BLUE`,确认之后库里那一列就变成了字符串
 * `"RED / BLUE"` —— 下游按数组消费的地方(渠道层的颜色映射)会开始
 * 逐字符遍历它。这个损坏是静默的:接口 200,界面显示得也对。
 *
 * ## `readonly` 这一支为什么存在
 *
 * 值是一个对象(不是数组也不是标量)时,任何一种输入框都装不下它。
 * 原来的做法是 `JSON.stringify` 显示、原样当字符串提交回去 —— 那是
 * 把"编辑器不支持"变成"数据被改坏"。宁可改不了:改不了是看得见的。
 */
type EditorKind = 'enum' | 'enum_list' | 'text_list' | 'number' | 'bool' | 'text' | 'readonly'

function editorKind(row: AttributeValue): EditorKind {
  const spec = specOf(row)
  const raw = row.value_json
  // 结构化到编辑器装不下:只读展示,不假装能改
  if (raw !== null && raw !== undefined && typeof raw === 'object' && !Array.isArray(raw)) {
    return 'readonly'
  }
  // 后端没给类型、而值是个数组:这正是 FALLBACK_SPEC 会猜错的那一格。
  // 猜"文本"会把数组拍平成字符串,所以宁可只读
  if (!row.spec && Array.isArray(raw)) return 'readonly'
  if (spec.multi_value) return spec.options.length ? 'enum_list' : 'text_list'
  if (spec.options.length) return 'enum'
  if (spec.value_type === 'NUMBER') return 'number'
  if (spec.value_type === 'BOOL') return 'bool'
  return 'text'
}

/** 库里的值 -> 控件认识的值。**类型不在这一步丢失。** */
function toEditorValue(row: AttributeValue): unknown {
  const raw = row.value_json
  if (specOf(row).multi_value) {
    if (Array.isArray(raw)) return raw.map(String)
    if (raw === null || raw === undefined || raw === '') return []
    // 存量单值数据(字段类型改过)。装进数组而不是丢掉
    return [String(raw)]
  }
  if (raw === null || raw === undefined) return row.normalized_value ?? ''
  if (typeof raw === 'boolean' || typeof raw === 'number') return raw
  return String(raw)
}

/**
 * 这个字段能不能记成"未知",以及记成什么。
 *
 * 枚举里有 `UNKNOWN` 取值的按枚举给;自由文本按字符串给(它一直是这么用的,
 * 且能原样round-trip)。**列表字段给不出来** —— 数组里放一个 `"UNKNOWN"`
 * 是往颜色词表里加了一个叫 UNKNOWN 的颜色,而空数组的意思是"没有",
 * 不是"不知道"。两者在这里合并的话,下游分不出来,而分不出来的那一刻
 * 已经在渠道映射里了。
 */
function unknownValueFor(
  row: AttributeValue,
): { allowed: true; value: unknown } | { allowed: false; why: string } {
  const spec = specOf(row)
  if (editorKind(row) === 'readonly') {
    return { allowed: false, why: '这个值是结构化的,当前编辑器不改它' }
  }
  if (spec.multi_value) {
    return {
      allowed: false,
      why: '多值字段没有"未知"这个取值。确认为「没有」请清空标签再点确认 —— "没有"和"不知道"是两个结论',
    }
  }
  if (spec.options.length && !spec.options.includes('UNKNOWN')) {
    return { allowed: false, why: '这个字段的取值表里没有「未知」这一档,提交会被后端拒绝' }
  }
  return { allowed: true, value: 'UNKNOWN' }
}

/** 空值判定。多值字段的空数组是**有意义的**("确认过:没有辅助色") */
function isBlank(value: unknown): boolean {
  if (value === null || value === undefined) return true
  if (Array.isArray(value)) return false
  if (typeof value === 'string') return value.trim() === ''
  return false
}

/** 置信度因子分解。回答「0.71 是因为只有一张图,还是因为图太糊」 */
function ConfidenceCell({ row }: { row: AttributeValue }) {
  const system = row.system_confidence
  const breakdown = row.confidence_breakdown
  const body = (
    <div style={{ maxWidth: 300 }}>
      <div style={{ marginBottom: 6 }}>
        模型自评:{row.model_confidence === null ? '—' : `${Math.round(row.model_confidence * 100)}%`}
        <div style={{ color: brandVars.slate, fontSize: fontScale.body }}>只用来展示,不参与阈值判定</div>
      </div>
      {breakdown && Object.keys(breakdown).length > 0 ? (
        <Descriptions size="small" column={1} bordered items={
          Object.entries(breakdown).map(([key, value]) => ({
            key,
            label: key,
            children: typeof value === 'number' ? value.toFixed(3) : String(value),
          }))
        } />
      ) : (
        <div style={{ color: brandVars.slate }}>没有因子分解</div>
      )}
    </div>
  )
  return (
    <Popover content={body} title="置信度构成" trigger="hover">
      {system === null ? (
        <Tag color="default">未校准</Tag>
      ) : (
        <Progress
          percent={Math.round(system * 100)}
          size="small"
          strokeColor={system >= 0.85 ? brandVars.success : system >= 0.6 ? brandVars.sand : brandVars.danger}
          style={{ minWidth: 90, marginBottom: 0 }}
        />
      )}
    </Popover>
  )
}

export default function AttributeTab({
  productId,
  flowProductId = productId,
  step,
  product,
}: {
  productId: string
  /** 颜色切换后操作的是目标 SKU,但向导查询仍挂在入口 SKU 上。 */
  flowProductId?: string
  step: FlowStepResult | undefined
  /** §9.4 的受众确认卡要用。`CONFIRM_AUDIENCE` 指向本标签页,
   *  而在这之前这一页没有任何受众控件 —— 那个动作码会落进死胡同 */
  product?: WorkbenchProduct
}) {
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  /** 逐字段的未保存改动。值是**已经带好类型的**,不是一律字符串(BLOCK-11) */
  const [edits, setEdits] = useState<Record<string, unknown>>({})
  const [evidenceOf, setEvidenceOf] = useState<AttributeValue | null>(null)
  /**
   * 最近一次识别的批次 id(A45-batch14-2 / F2)。
   *
   * 只在本次会话里记,**不做持久化也不回溯历史批次**:这里要回答的是
   * 「我刚点的这次识别,模型对哪些字段说了『没有值』」——那是一个跟着
   * 点击走的问题。回溯历史批次是证据浏览器的活,那件事仍然没做
   * (见 `EvidenceModal` 顶部注释),不在这一轮的范围里。
   */
  const [lastExtractionId, setLastExtractionId] = useState<string | null>(null)

  const values = useQuery({
    queryKey: ['attributes', productId],
    queryFn: () => attributesApi.list(productId),
    enabled: Boolean(productId),
  })

  /** 逐图证据。只为拿 `missing_reason` —— 有 id 才拉,没有就不发请求 */
  const lastExtraction = useQuery({
    queryKey: ['extraction', lastExtractionId],
    queryFn: () => attributesApi.extraction(lastExtractionId as string),
    enabled: Boolean(lastExtractionId),
    // 是否还会变化由后端 `can_cancel` 给,不在前端维护第二份终态表。
    refetchInterval: (query) => query.state.data?.can_cancel ? 2000 : false,
  })

  const refresh = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['attributes', productId] })
    // 属性一改,文案与草稿按 §4.5 立刻过期 —— 流程判定必须跟着重拉
    queryClient.invalidateQueries({ queryKey: ['workbench-flow', flowProductId] })
    queryClient.invalidateQueries({ queryKey: ['workbench'] })
  }, [queryClient, productId, flowProductId])

  /**
   * 属性识别与确认都是写请求(BLOCK-05)。
   *
   * 识别**是付费链路**:一次调用按图计费。超时后提示"请重试"等于让运营
   * 再买一次同一批图。确认虽然不花钱,但它写的是 MANUAL 值 ——
   * 从此不被任何自动流程覆盖,重复提交会连着产生两个版本。
   */
  const onWriteError = useWriteError(refresh)

  const extract = useMutation({
    mutationFn: () => attributesApi.extract(
      productId,
      undefined,
      lastExtraction.data?.failed_scopes.length
        ? lastExtraction.data.failed_scopes
        : undefined,
      product?.spu_id,
    ),
    onSuccess: (result) => {
      // 先记批次 id:下面三条分支都要它。**失败的那次也记** ——
      // 「全部失败」和「模型说这些字段看不清」是两回事,后者恰恰
      // 发生在识别成功的时候,而前者的批次里本来就没有证据可看
      setLastExtractionId(result.id ?? null)
      // A2:这次识别算哪一档**由后端说**(硬规则 4)。这里原来是三段
      // 三元判断,而它漏了「没跑完但一张都没失败」那一种,把跑了一半的
      // 识别显示成「识别完成」。判定现在在 `run_state.terminal_status_for`,
      // 前端只挑语气和下一句话
      const notice = RUN_STATUS_NOTICE[result.status] ?? RUN_STATUS_NOTICE.FAILED
      const counts = `${result.image_count} 张图,成功 ${result.succeeded_count} 条,失败 ${result.failed_count} 条`
      message[notice.tone](
        [`${RUN_STATUS_LABEL[result.status] ?? result.status}:${counts}`, notice.next]
          .filter(Boolean)
          .join('。'),
      )
      refresh()
    },
    onError: onWriteError,
  })

  const cancelExtraction = useMutation({
    mutationFn: () => attributesApi.cancel(lastExtractionId as string),
    onSuccess: (result) => {
      message.warning('已请求中止;正在执行的单张调用完成后会停下')
      queryClient.setQueryData(['extraction', result.id], result)
    },
    onError: onWriteError,
  })

  useEffect(() => {
    const run = lastExtraction.data
    if (run && !run.can_cancel) refresh()
  }, [lastExtraction.data, refresh])

  const confirm = useMutation({
    mutationFn: (items: { field_name: string; value: unknown }[]) =>
      attributesApi.confirm(productId, items),
    /**
     * 只清**这次提交的那几个字段**(BLOCK-10)。
     *
     * 原来是 `setEdits({})` —— 确认一项会把其余全部未保存编辑一并清空。
     * 而这一页的实际用法正是"先把七八个字段都填一遍,再逐项确认":
     * 点第一个「确认」的那一刻,另外六个字段的输入**当场消失**,
     * 界面上没有任何提示,运营多半会以为是自己没填。
     *
     * 用 `variables` 拿本次提交的字段名,而不是在闭包里捕获一份 ——
     * 闭包捕获的是**发起时**的 `edits`,而回调执行时用户可能又改了别的字段,
     * 那几笔会被这次成功回调连坐清掉,缺陷换个形状原样保留。
     */
    onSuccess: (written, submitted) => {
      message.success(`已确认 ${written.length} 项。已确认的属性从此不被自动流程覆盖`)
      const done = new Set(submitted.map((i) => i.field_name))
      setEdits((prev) =>
        Object.fromEntries(Object.entries(prev).filter(([field]) => !done.has(field))),
      )
      refresh()
    },
    onError: onWriteError,
  })

  /*
   * `?? []` **必须自己也是一个 memo**。
   *
   * 裸写的话每次 render 都是一个新数组,于是下面每一个吃 `[rows]` 的
   * `useMemo` 都恒定失效 —— 它们看起来在缓存,实际每帧全量重算。
   * 属性页最多 30 行,今天不疼;但 lint 会因此每加一个消费点就多报一条,
   * 而"忍受几条固定警告"的下一步一定是不再看警告。
   *
   * 本批加了第三个消费点(`stale`),把这条一起收掉。
   */
  const rows = useMemo(() => values.data ?? [], [values.data])
  /*
   * 字段名 -> 中文名。词表的唯一来源是后端注册表(每行的 `spec.label`),
   * 前端不另存一张表 —— 存一张的下场是它和注册表分叉,而分叉的表现是
   * 同一个字段在属性表里叫「主色」、在缺证据提示里叫 `primary_color`。
   *
   * 证据行(`Evidence`)上没有 `spec`,所以由这里按属性表建一次词表递下去。
   * 查不到就回落成字段名本身。
   */
  const labelOf = useCallback(
    (fieldName: string) => {
      const hit = rows.find((r) => r.field_name === fieldName)
      return hit ? attributeLabel(hit) : fieldName
    },
    [rows],
  )
  const conflicts = useMemo(() => rows.filter((r) => r.status === 'CONFLICT'), [rows])
  /**
   * 样品已变的事实(§5.3 / AC-21)。**判定整个在后端**(硬规则 4)——
   * 这里只挑出后端已经算好的那几行,不在前端比指纹。
   *
   * 只数**已确认**的:接口那一侧算的是 `settled_only=False`(它回答的是
   * "库里这一行的指纹对不对得上"),而这一屏要回答的是"该不该催人"。
   * 把 CANDIDATE 也算进来的话,界面会对同一个字段同时说两句话——
   * 「有建议值待确认」和「已确认的事实过期了」,而后者是假的:
   * 那条事实从来没有立起来过,谈不上过不过期。口径与后端
   * `stale_fields(settled_only=True)` 同向。
   */
  const stale = useMemo(
    () => rows.filter((r) => r.status === 'CONFIRMED' && factsStale(r)),
    [rows],
  )
  const pending = useMemo(() => rows.filter((r) => r.status === 'CANDIDATE'), [rows])

  /**
   * 一项的待提交值:改过就用改的,没改过用当前值。冲突项没有默认值。
   *
   * 返回 `undefined` = 这一项现在不能提交(冲突未裁决 / 结构装不下)。
   * 用 `undefined` 而不是 `null`:`null` 在这个系统里是一个**合法的属性值**
   * (还没识别出来),两者混用会让"不能提交"和"提交一个空值"分不开。
   */
  const valueToSubmit = (row: AttributeValue): unknown => {
    if (row.field_name in edits) return edits[row.field_name]
    if (row.status === 'CONFLICT') return undefined
    if (editorKind(row) === 'readonly') return undefined
    const current = toEditorValue(row)
    return isBlank(current) ? undefined : current
  }

  const setEdit = (field: string, value: unknown) =>
    setEdits((prev) => ({ ...prev, [field]: value }))

  /** 类型决定控件(BLOCK-11)。类型由后端给,不在这里推 */
  const ValueCell = ({ row }: { row: AttributeValue }) => {
    const spec = specOf(row)
    const kind = editorKind(row)
    const pending = valueToSubmit(row)
    const unresolved = row.status === 'CONFLICT' && !(row.field_name in edits)
    const status = unresolved ? ('error' as const) : undefined

    if (kind === 'readonly') {
      return (
        <Tooltip title="这个值是结构化的,当前编辑器装不下它。原样保留 —— 用文本框改会把结构拍平成字符串">
          <div>
            <Input size="small" readOnly value={displayValue(row)} />
            <span style={{ color: brandVars.textFaint, fontSize: fontScale.meta }}>只读</span>
          </div>
        </Tooltip>
      )
    }
    if (kind === 'enum') {
      return (
        <Select<string>
          size="small"
          allowClear
          showSearch
          status={status}
          style={{ width: '100%' }}
          placeholder={unresolved ? '冲突,请先裁决' : '未填'}
          value={(pending as string) || undefined}
          onChange={(v) => setEdit(row.field_name, v ?? '')}
          options={spec.options.map((o) => ({ value: o, label: o }))}
        />
      )
    }
    if (kind === 'enum_list' || kind === 'text_list') {
      return (
        <Select<string[]>
          size="small"
          // 自由文本列表(颜色词表)允许新建项;枚举列表只能从取值里选 ——
          // 后端会拒掉表外的值,能选出来才提交是更省一轮的做法
          mode={kind === 'text_list' ? 'tags' : 'multiple'}
          status={status}
          style={{ width: '100%' }}
          placeholder={unresolved ? '冲突,请先裁决' : '未填(留空 = 确认为无)'}
          value={(pending as string[]) ?? []}
          onChange={(v) => setEdit(row.field_name, v)}
          options={spec.options.map((o) => ({ value: o, label: o }))}
        />
      )
    }
    if (kind === 'number') {
      return (
        <InputNumber
          size="small"
          status={status}
          style={{ width: '100%' }}
          value={typeof pending === 'number' ? pending : null}
          onChange={(v) => setEdit(row.field_name, v)}
        />
      )
    }
    if (kind === 'bool') {
      return (
        <Switch
          size="small"
          checked={pending === true}
          onChange={(v) => setEdit(row.field_name, v)}
        />
      )
    }
    return (
      <Input
        size="small"
        status={status}
        value={typeof pending === 'string' ? pending : ''}
        placeholder={unresolved ? '冲突,请先裁决' : '未填'}
        onChange={(e) => setEdit(row.field_name, e.target.value)}
      />
    )
  }

  const columns: ColumnsType<AttributeValue> = [
    {
      // 中文名在主位,字段名压成第二行的小字。
      //
      // **两个都要**:运营读的是「主色」,而报障、搜后端日志、对 spec
      // 用的是 `primary_color` —— 只留中文那一半的话,他描述得出问题
      // 却指不出是哪个字段
      title: '属性',
      dataIndex: 'field_name',
      width: 170,
      render: (_: string, row) => (
        <Space direction="vertical" size={0}>
          <span>{attributeLabel(row)}</span>
          <span className="mono" style={{ color: brandVars.textMuted, fontSize: fontScale.meta }}>
            {row.field_name}
          </span>
        </Space>
      ),
    },
    {
      title: '当前值 / AI 建议',
      key: 'value',
      width: 240,
      render: (_, row) => <ValueCell row={row} />,
    },
    { title: '置信度', key: 'confidence', width: 130, render: (_, row) => <ConfidenceCell row={row} /> },
    {
      title: '来源',
      dataIndex: 'source',
      width: 90,
      render: (v: string) => <Tag>{ATTRIBUTE_SOURCE_LABEL[v] ?? v}</Tag>,
    },
    {
      title: '状态',
      key: 'status',
      width: 150,
      render: (_, row) => {
        const meta = ATTRIBUTE_STATUS_LABEL[row.status]
        // 「样品已变」和状态是**两件正交的事**,所以是第二个 Tag 而不是
        // 第五档状态:一条 CONFIRMED 的事实过期之后,它仍然是 CONFIRMED
        // (仍然不被自动流程覆盖、仍然会进刊登报文),变的只是"还证不证得明"。
        // 塞进 `ATTRIBUTE_STATUS_LABEL` 会让它和后端的 `AttributeStatus`
        // 分叉,而那张表是逐值比对钉着的。
        return (
          <Space size={4} wrap>
            <Tag color={meta?.color}>{meta?.text ?? row.status}</Tag>
            {row.status === 'CONFIRMED' && factsStale(row) && (
              <Tooltip title="确认之后样品图变过,系统证明不了这个值仍然成立。重新识别一次再复核。">
                <Tag color="warning">样品已变</Tag>
              </Tooltip>
            )}
          </Space>
        )
      },
    },
    {
      title: '证据',
      key: 'evidence',
      width: 90,
      render: (_, row) =>
        row.evidence_ids.length ? (
          // 开抽屉,不是导航 —— 按钮而不是无 href 的锚点(键盘可达)
          <Button type="link" size="small" style={{ padding: 0, height: 'auto' }} onClick={() => setEvidenceOf(row)}>
            {row.evidence_ids.length} 条
          </Button>
        ) : (
          <span style={{ color: brandVars.textFaint }}>—</span>
        ),
    },
    {
      title: '操作',
      key: 'ops',
      width: 150,
      render: (_, row) => {
        const value = valueToSubmit(row)
        const unknown = unknownValueFor(row)
        return (
          <Space size={4}>
            <Button
              size="small"
              type="link"
              style={{ padding: 0 }}
              disabled={value === undefined || confirm.isPending}
              onClick={() => confirm.mutate([{ field_name: row.field_name, value }])}
            >
              {row.status === 'CONFLICT' ? '裁决' : '确认'}
            </Button>
            {/*
              * 「未知」原来无条件提交字符串 `'UNKNOWN'`(BLOCK-11 的另一半)。
              * 对 `secondary_colors` 这类列表字段,那会往一个数组列里塞一个
              * 字符串;对没有 UNKNOWN 取值的枚举字段,它是一个后端会拒的值。
              *
              * 现在按字段类型决定这个按钮**在不在**:给不出一个诚实的
              * "未知"表示时就不给按钮,而不是提交一个看起来像那么回事的值。
              * 列表字段要表达"没有",清空标签再点确认 —— 那是"无",
              * 和"不知道"是两句话,系统不替他把两者合并。
              */}
            <Tooltip
              title={
                unknown.allowed
                  ? '记为未知。它是一个明确结论,不是"跳过"'
                  : unknown.why
              }
            >
              <Button
                size="small"
                type="link"
                style={{ padding: 0 }}
                disabled={!unknown.allowed || confirm.isPending}
                onClick={() => {
                  // 收窄不能靠 `disabled`:它是运行期的渲染属性,tsc 看不见,
                  // 于是 `unknown.value` 在 `allowed: false` 那一支上不存在。
                  // 写成守卫而不是 `!` 断言 —— 断言在这里等于宣称"按钮禁用
                  // 时一定点不到",而那是一句关于 antd 渲染的假设,不是关于
                  // 这段代码的事实。
                  if (!unknown.allowed) return
                  confirm.mutate([{ field_name: row.field_name, value: unknown.value }])
                }}
              >
                未知
              </Button>
            </Tooltip>
          </Space>
        )
      },
    },
  ]

  /** 批量确认:**只带待确认项**,冲突项一律排除 —— 见文件头注释 */
  const confirmAllPending = () => {
    const items = pending
      .map((row) => ({ field_name: row.field_name, value: valueToSubmit(row) }))
      // `undefined` = 这一项提交不了(冲突未裁决 / 结构装不下)。
      // 不能写成 `.filter((i) => i.value)` —— 那会连 `false`、`0` 和
      // 空数组一起滤掉,而那三个都是**合法的属性值**
      .filter((i) => i.value !== undefined)
    if (!items.length) {
      message.info('没有可批量确认的待确认项')
      return
    }
    confirm.mutate(items)
  }

  /** A11:逐字段编辑存在 `edits` 里,一个键就算有未保存内容 */
  const dirty = Object.keys(edits).length > 0

  return (
    <Space direction="vertical" size={10} style={{ width: '100%' }}>
      <UnsavedGuard dirty={dirty} what="属性" />

      {/*
        * §9.4 受众与品类确认。**排在属性之前**,理由是必填属性集本身
        * 按受众条件化(§18.3)—— 受众没定就去确认属性,确认的是一份
        * 可能是错的清单:男装商品会被问领型,女装商品会被问腰头。
        *
        * 这也是 `ACTION_TAB.CONFIRM_AUDIENCE -> 'attribute'` 的落点。
        * 少了它,那个动作码就是把死胡同挪了个位置。
        */}
      {product && (
        <AudienceConfirmCard
          productId={productId}
          audience={product.audience}
          garmentType={product.garment_type ?? undefined}
        />
      )}

      {step && step.issues.length > 0 && (
        <Card size="small" title="这一步的问题">
          <IssueList issues={step.issues} />
        </Card>
      )}

      {conflicts.length > 0 && (
        <Alert
          type="error"
          showIcon
          message={`${conflicts.length} 项属性有冲突结论,必须人工裁决`}
          description="不同图片对同一属性给出了不同结论。系统不会自动选一个 —— 请在下面逐项填入采信值再点「裁决」,选择会连同操作人一起留痕。"
        />
      )}

      {/*
        * 「这批事实所依据的样品已经变了」(§5.3 / AC-21)。
        *
        * ## 为什么它必须上屏
        *
        * 后端从 batch14-21 起就在逐行回 `facts_stale`,而前端对它**零引用** ——
        * 于是"样品换了、这条已确认的事实可能不再成立"这件事,运营在界面上
        * 完全看不见。它不是一个提示的缺失:已确认的属性**从此不被自动流程
        * 覆盖**,所以没有任何后续动作会自己把它纠回来,这条事实会一路带到
        * 刊登报文里去。
        *
        * ## 语气是 warning 不是 error
        *
        * 过期**不等于错**。指纹对不上只说明"证明不了它仍然成立",
        * 多数情况下重跑一次识别得到的是同一个值。写成 error 会让运营
        * 把一屏黄字读成一屏故障,而真正的故障(冲突)就在它上面一格。
        *
        * ## 下一步只给一个,而且是那个花钱的
        *
        * §9.5:错误必须包含行动。这里的行动是"重新识别",它在右上角。
        * 不给「忽略」按钮 —— 那个按钮的语义是把指纹改成当前值,
        * 也就是**在没有看过新样品的情况下宣布事实仍然成立**。
        */}
      {stale.length > 0 && (
        <Alert
          type="warning"
          showIcon
          message={`${stale.length} 项已确认的属性,依据的样品已经变了`}
          description={
            <>
              这些字段确认之后,它们所依据的那批样品图发生了变化(补图、换图或删图),
              系统<b>证明不了</b>它们仍然成立。已确认的属性不会被自动流程覆盖,
              所以不重新识别的话,这个值会一直带到刊登报文里。
              <br />
              下一步:点右上角「重新识别」,再逐项复核带
              <Tag color="warning" style={{ marginInline: 4 }}>样品已变</Tag>
              标记的行。
            </>
          }
        />
      )}

      {/*
        * 逐图证据拉不到时**必须说出来**(FE-GLOBAL-03)。
        *
        * 这里的空数据是一句业务结论:"模型这次没有对任何字段说'没有值'"。
        * 而它和"这条查询挂了"在界面上长得一模一样 —— 运营照着前者做的下一步
        * (直接人工填)和照着后者做的下一步(重拉/找管理员)是相反的。
        *
        * 这条分支是 `test_every_query_handle_is_checked_for_failure` 逼出来的:
        * 本轮 F2 修的正是"诊断没上屏",而第一版的这个查询自己就把失败
        * 渲染成了空 —— **同一个病灶在修它的那次改动里复发**,被既有门禁当场咬住。
        */}
      {lastExtraction.isError && (
        <ErrorNotice
          title="这次识别的逐图证据没拉到"
          error={lastExtraction.error}
          onRetry={() => lastExtraction.refetch()}
        />
      )}

      {/* 排在冲突之后、结果表之前:它解释的是表里那几行**为什么是空的** */}
      <MissingEvidenceNotice
        evidence={lastExtraction.data?.evidence ?? []}
        labelOf={labelOf}
      />

      {Boolean(lastExtraction.data?.failed_scopes.length) && (
        <Alert
          type="warning"
          showIcon
          message={`${lastExtraction.data?.failed_scopes.length} 个颜色本轮全部失败`}
          description="再次识别只会重跑这些颜色,不会为已经成功的颜色重复付费。"
        />
      )}

      <Card
        size="small"
        title="属性识别结果"
        extra={
          <Space>
            <Button
              size="small"
              icon={<ThunderboltOutlined />}
              loading={extract.isPending}
              disabled={Boolean(lastExtraction.data?.can_cancel)}
              onClick={() => extract.mutate()}
            >
              {rows.length ? '重新识别' : '启动属性识别'}
            </Button>
            {lastExtraction.data?.can_cancel && (
              <Button
                size="small"
                danger
                loading={cancelExtraction.isPending}
                onClick={() => cancelExtraction.mutate()}
              >
                中止本次识别
              </Button>
            )}
            <Tooltip title="只提交状态为「待确认」的项,冲突项需要逐条裁决">
              <Button
                size="small"
                type="primary"
                disabled={!pending.length || confirm.isPending}
                onClick={confirmAllPending}
              >
                确认全部待确认({pending.length})
              </Button>
            </Tooltip>
          </Space>
        }
      >
        {values.isLoading ? (
          <Skeleton active />
        ) : values.isError ? (
          <ErrorNotice
            title="拉不到属性"
            error={values.error}
            onRetry={() => values.refetch()}
          />
        ) : rows.length ? (
          <Table<AttributeValue>
            rowKey="field_name"
            size="small"
            bordered
            pagination={false}
            columns={columns}
            dataSource={rows}
            rowClassName={(row) => (row.status === 'CONFLICT' ? 'attr-conflict-row' : '')}
          />
        ) : (
          <Empty description="还没跑过属性识别。点右上角「启动属性识别」,识别完成后逐项确认。" />
        )}
      </Card>

      <EvidenceModal
        row={evidenceOf}
        onClose={() => setEvidenceOf(null)}
      />
    </Space>
  )
}

/**
 * 「模型说这些字段没有值」(§4.5,A45-batch14-2 / F2)。
 *
 * 后端从 batch14 起就在落 `missing_reason`,一路铺到了接口出参 —— 但界面上
 * 一个字都没有。运营看到的是那几个字段**空着**,和"还没识别到"长得一模一样,
 * 于是唯一能想到的动作是再点一次识别;而三个原因里只有第一个重跑有意义,
 * 第三个(材质克重一类)重跑一万次也不会有值。
 *
 * 按原因分组而不是按字段列:动作是跟着原因走的,同一个动作说一遍就够。
 * 不做成 error —— 它不是缺陷,是模型如实报告的一次"我不知道",
 * 而"如实说不知道"正是 §4.5 想要的行为;报成红色会训练运营忽略它。
 */
function MissingEvidenceNotice({
  evidence,
  labelOf,
}: {
  evidence: Evidence[]
  /** 字段名 -> 中文名。证据行上没有 `spec`,所以词表由属性表那边递进来 */
  labelOf: (fieldName: string) => string
}) {
  const byReason = useMemo(() => {
    const groups = new Map<string, Set<string>>()
    for (const item of evidence) {
      if (!item.missing_reason) continue
      const fields = groups.get(item.missing_reason) ?? new Set<string>()
      // 用 Set:同一个字段在多张图上都"看不清"是常态,列九遍没有信息量
      fields.add(item.field_name)
      groups.set(item.missing_reason, fields)
    }
    return [...groups.entries()].map(([reason, fields]) => ({
      reason,
      // 按**字段名**排序再翻译:排中文名的话,同一组字段在两次识别之间
      // 可能换一次序,看起来像有东西变了
      fields: [...fields].sort().map(labelOf),
    }))
  }, [evidence, labelOf])

  if (!byReason.length) return null

  return (
    <Alert
      type="warning"
      showIcon
      message="模型对这些字段明确报告了「没有值」——不是漏识别"
      description={
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          {byReason.map(({ reason, fields }) => {
            // 认不出来的原因原样显示。后端可能加了第四个取值而前端还没跟上,
            // 那时候显示编码比显示"未知原因"有用 —— 至少能搜得到
            const known = MISSING_REASON_ACTION[reason]
            return (
              <span key={reason}>
                <b>{known ? known.label : reason}</b>:{fields.join('、')}。
                {known ? known.action : '取值前端还不认识,请把这个编码提供给管理员'}
              </span>
            )
          })}
          <span style={{ color: brandVars.slate }}>
            这几项不会自动变成值,也不会参与合并投票 —— 要么按上面的动作补素材,要么在下表里人工填。
          </span>
        </Space>
      }
    />
  )
}

/**
 * 证据查看(FE-203)。
 *
 * 方案 B 的降级做法:显示模型对该字段的原始结论与来源素材 id,不做证据浏览器。
 * 归一化失败(`normalized_value` 为 null)的条目**照样显示** —— 那是模型
 * 自造了一个枚举值被拦下来,是提示词要改的信号,藏起来等于把信号丢掉。
 *
 * 这里按属性行的 `evidence_ids` 反查:证据挂在识别批次上,而一个属性的
 * 证据可能来自多个批次,所以先拉批次再筛字段,而不是假设只有最近一次。
 */
function EvidenceModal({ row, onClose }: { row: AttributeValue | null; onClose: () => void }) {
  return (
    <Modal
      open={Boolean(row)}
      title={row ? `证据 · ${attributeLabel(row)}` : '证据'}
      footer={null}
      width={640}
      onCancel={onClose}
    >
      {row && (
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Descriptions
            size="small"
            column={2}
            bordered
            items={[
              { key: 'value', label: '采信值', children: displayValue(row) || '—' },
              {
                key: 'source',
                label: '来源',
                children: ATTRIBUTE_SOURCE_LABEL[row.source] ?? row.source,
              },
              {
                key: 'system',
                label: '系统置信度',
                children:
                  row.system_confidence === null
                    ? '未校准(不是 0)'
                    : `${Math.round(row.system_confidence * 100)}%`,
              },
              {
                key: 'confirmed',
                label: '确认人',
                children: row.confirmed_by ? `${row.confirmed_by} · ${formatDateTime(row.confirmed_at)}` : '—',
              },
            ]}
          />
          <div className="section-label" style={{ marginBottom: 0 }}>
            证据条目 · {row.evidence_ids.length}
          </div>
          {row.evidence_ids.length ? (
            <ul style={{ paddingLeft: 18, margin: 0 }}>
              {row.evidence_ids.map((id) => (
                <li key={id} className="mono" style={{ fontSize: fontScale.meta }}>
                  {id}
                </li>
              ))}
            </ul>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="这一项没有模型证据(人工填的值)" />
          )}
          <Alert
            type="info"
            showIcon
            message="逐图证据与模型原文在识别批次详情里"
            description="这里给出证据条目与采信值。逐图查看每条证据来自哪张素材的功能还没做,需要核对到图这一级时,请到「素材」标签页对照。"
          />
        </Space>
      )}
    </Modal>
  )
}
