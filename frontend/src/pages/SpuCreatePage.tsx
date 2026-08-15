/**
 * 三步建档(PRD v3.1 §6.1 步骤 1 / §13 阶段 1)。
 *
 * ## 这一页在此之前**不存在**
 *
 * `POST /api/spus`、`GET /api/spus/size-templates` 从阶段 1 起就齐了,
 * `sample-data/spus.json` 里三颜色九 SKU 的样例也在,而前端一个调用点都没有
 * —— 全仓只有 `batch.ts` 打过 `/workbench/spus`(只读聚合),
 * `WorkbenchSpuPage` 是查看页不是建档页。后端做完、门禁全绿、动线断在最后
 * 一跳,和 `facts_stale` / `variant_gate_roles` 是同一个形状。
 *
 * ## 第三步预览的 SKU 是**后端算的**,不是前端拼的
 *
 * 硬规则 4:前端不推测后端的东西。SKU 编码怎么拼(分隔符、颜色码位置、
 * 尺码归一)住在 `listings/sku_matrix`,前端照着拼一份的话,改一次拼法
 * 界面和库里就是两套编码 —— 而运营会照着界面上那个编码去平台后台搜,
 * 搜不到,然后怀疑是没建成功。
 *
 * **这一段原来的结论是"所以只预览数量"**,而那是当时唯一的选项:没有
 * 任何端点能在不写库的情况下告诉前端"这次会建出哪几行"。`POST /spus/preview`
 * 补上之后,顾虑本身就没了 —— 显示的那份 SKU 是后端 `expand()` 算出来的,
 * 与建档走同一个函数,前端仍然一个字符都没拼。
 *
 * ## 校验提前到"下一步",而**不是**在这里抄一份规则
 *
 * 编码字符集、重复颜色、行数上限、编码已存在这四类失败,在此之前全部
 * 只在第三步点「建档」时才报得出来 —— 而其中三类错在**第二步**的输入框上。
 * 那时页面上唯一的线索是一条会自己消失的 toast,以及一句
 * 「上面的提示里有具体原因」(见下面那条 Alert 的历史)。
 *
 * 解法不是把规则复制到前端(复制的下场是两个版本,先过期的那一个会让
 * 合法输入在界面上被拒,运营无从申诉),是**把同一份规则提前问一次**:
 * 每一步的"下一步"先打一次 `preview`,失败时按后端给的 `loc` 高亮到行。
 *
 * ## 这里没有视觉属性字段
 *
 * ## 这里没有视觉属性字段
 *
 * 阶段 1 的验收之一是「不填视觉属性即可建档」。后端的做法不是把那 8 个
 * 字段设成可选,而是让它们在这个接口上根本不存在 —— 可选字段会被前端填成
 * 空串,而空串和"还没识别"在下游是两件事:前者会被当成一个确定的事实。
 * 别在这一页加它们。
 *
 * ## 校验交给后端,不在这里抄一份
 *
 * 编码字符集、重复颜色、行数上限全部住在 `listings/sku_matrix`。抄一份到
 * 这里,它就有了两个版本,然后其中一个先过期 —— `schemas/spu.py` 顶部
 * 记着这类事故的原样。这一页只挡"必填项还空着"这种连请求都发不出去的情况。
 */
import { useCallback, useMemo, useState } from 'react'
import {
  Alert, App, Button, Card, Descriptions, Empty, Form, Input, Select, Space,
  Statistic, Steps, Table, Tag,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import { DeleteOutlined, PlusOutlined } from '@ant-design/icons'
import { useMutation, useQuery } from '@tanstack/react-query'
import { useNavigate, useSearchParams } from 'react-router-dom'
import {
  SPU_AUDIENCE_LABEL,
  spusApi,
  type ColorVariantDraft,
  type PlannedSku,
  type SpuAudience,
  type SpuDetail,
  type SpuPreview,
  type SpuSku,
} from '../api/spus'
import { fieldProblems, readWriteError, type FieldProblem } from '../api/client'
import { noRefresh, useWriteError } from '../hooks/useWriteError'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import UnsavedGuard from '../components/UnsavedGuard'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { brandVars } from '../theme'

interface Basics {
  spu_code: string
  internal_name: string
  audience: SpuAudience | undefined
  base_category: string
  supplier_ref: string
}

const EMPTY_BASICS: Basics = {
  spu_code: '',
  internal_name: '',
  audience: undefined,
  base_category: 'swimwear',
  supplier_ref: '',
}

/** 一行空颜色。`variant_code` 是必填的那一个,名字可以后补 */
const emptyColour = (): ColorVariantDraft => ({
  variant_code: '',
  working_name: '',
  supplier_color_code: '',
})

export default function SpuCreatePage() {
  const { message } = App.useApp()
  const navigate = useNavigate()
  // 来路白名单(a47 §3.1)。理由与 `WorkbenchImportPage` 那一处同一条
  const [searchParams] = useSearchParams()
  const cameFromWorkbench = searchParams.get('from') === 'workbench'
  useDocumentTitle('新建商品款式')

  const [current, setCurrent] = useState(0)
  const [basics, setBasics] = useState<Basics>(EMPTY_BASICS)
  const [colours, setColours] = useState<ColorVariantDraft[]>([emptyColour()])
  const [sizeTemplate, setSizeTemplate] = useState<string | undefined>()
  const [created, setCreated] = useState<SpuDetail | null>(null)

  /**
   * 这次建档的 `Idempotency-Key`(PRD §9.1)。**按表单会话生成一次。**
   *
   * 在此之前这一页没有键,于是双击提交的第二次请求撞 `uq_spus_spu_code`,
   * 运营看到的是「SPU 编码 X 已存在」—— 在双击这个语境下那是一句假话。
   *
   * 用 `useState` 的惰性初始值而不是每次渲染现算:现算的话每次重渲染都换一把,
   * 键就退化成一个随机串,一次都不会命中。点「再建一个」时显式换新的 ——
   * 那确实是另一次请求。
   */
  const [requestKey, setRequestKey] = useState(() => crypto.randomUUID())

  /**
   * 尺码模板**从后端拿**(硬规则 4)。
   *
   * 内置一份的话,加一个模板要改两个仓库,而漏改的那一侧不报错 ——
   * 它只会少一个选项,而少的那个恰恰是新加的那个。
   */
  const templates = useQuery({
    queryKey: ['size-templates'],
    queryFn: () => spusApi.sizeTemplates(),
  })

  /**
   * 编码规则。**一个数字都不内置**(硬规则 4)。
   *
   * 拉不到**不致命**:提示文案退化成空,而判定本来就不在前端 ——
   * 那时的行为与这个端点存在之前逐字相同。但"不致命"不等于"不说" ——
   * FE-GLOBAL-03 那条(查询失败不许静默退化成无数据)在这里同样成立:
   * 运营看到一张没有任何编码提示的表单时,他不知道是这个系统本来就不提示,
   * 还是这一次没拉到。所以下面有一条明确写着"提示缺失、校验不受影响"的横幅。
   */
  const rules = useQuery({
    queryKey: ['spu-code-rules'],
    queryFn: () => spusApi.codeRules(),
    staleTime: Infinity,
  })

  /**
   * 试算结果与试算失败。
   *
   * `preview` 是**一次写请求形状的读请求**:POST 只因为一整张颜色表放不进
   * query string。所以它不进 `useWriteError`(那条会说"结果未知,先去核对",
   * 而试算什么都没改),用 `readWriteError` 取那句话就够。
   */
  const [preview, setPreview] = useState<SpuPreview | null>(null)
  /** 试算在飞。按钮转圈,防连点 —— 试算不写库,但两次结果谁后到不确定 */
  const [previewing, setPreviewing] = useState(false)
  /** 后端点名的那几行。key 是 `loc`,与 `spu_service._api_loc()` 逐字对应 */
  const [problems, setProblems] = useState<FieldProblem[]>([])
  /** 提交或试算失败时页面上那句**不会消失**的话(见下面 Alert 的历史) */
  const [failure, setFailure] = useState<string>('')

  /**
   * `loc` -> 第几行颜色。
   *
   * **不自己拼这个对应关系,只解析后端给的那一个。** 后端为它付过一次代价:
   * `spu_service._api_loc()` 专门把纯层的 `variant_codes[1]` 翻成
   * `color_variants[1].variant_code`,而那个函数的 docstring 里记着上一版
   * 翻错时的表现 ——「前端高亮不到任何一行」。翻它的唯一理由就是这里。
   */
  const rowProblems = useMemo(() => {
    const byRow = new Map<number, string>()
    for (const p of problems) {
      const m = /^color_variants\[(\d+)\]/.exec(p.loc)
      if (m) byRow.set(Number(m[1]), p.msg)
    }
    return byRow
  }, [problems])

  /** 不指向某一行的那些(`spu_code` / `size_template` / 整张表) */
  const generalProblems = useMemo(
    () => problems.filter((p) => !/^color_variants\[\d+\]/.test(p.loc)),
    [problems],
  )

  /**
   * 建档是写请求,而且**不是幂等的**:同一个 `spu_code` 提交两次,
   * 第二次会被后端的唯一约束挡下,但"结果未知"(超时、502)那一档不会 ——
   * 那时运营不知道上一次到底建没建。所以这里显式传 `noRefresh`:
   * 这一页没有可刷新的列表,唯一正确的下一步是去 SPU 聚合页看一眼,
   * 而那句话由 `readWriteError` 的"结果未知"文案负责说。
   */
  /**
   * 提交失败时把后端点名的行**记下来并跳回第二步**。
   *
   * 在此之前这条路径上的字段明细是被丢掉的:`useWriteError` 只弹一条
   * toast,而 `loc` 停在 `describeError` 里被拍成了字符串。于是运营在
   * 第三步看到一句红字,而错在第二步的某个输入框上,页面不告诉他是哪一个。
   */
  const onFields = useCallback((fields: FieldProblem[]) => {
    setProblems(fields)
    // 只在**确实指向某一行**时才跳。`spu_code` 出错时跳去第二步等于
    // 把人送到一个没有问题的页面上
    if (fields.some((f) => /^color_variants\[\d+\]/.test(f.loc))) setCurrent(1)
  }, [])

  const onWriteError = useWriteError(noRefresh, onFields)

  /**
   * 试算一次。**每一步的「下一步」都过它**,而不是等到提交。
   *
   * 返回是否通过 —— 调用方据此决定要不要往下走。失败时不抛:
   * 试算失败是一件**预期之内**的事(运营正在改),抛异常会让它
   * 走到 ErrorBoundary 那条路上去。
   */
  const runPreview = useCallback(
    async (withTemplate: boolean): Promise<boolean> => {
      setPreviewing(true)
      try {
        const result = await spusApi.preview({
          spu_code: basics.spu_code.trim(),
          color_variants: colours.map((c) => ({
            variant_code: c.variant_code.trim(),
            working_name: c.working_name.trim(),
            supplier_color_code: c.supplier_color_code?.trim() || null,
          })),
          size_template: withTemplate ? sizeTemplate : null,
        })
        setPreview(result)
        setProblems([])
        setFailure('')
        return true
      } catch (err) {
        setPreview(null)
        setProblems(fieldProblems(err))
        // 这句话留在页面上,不是 toast。理由见第三步那条 Alert 的注释
        setFailure(readWriteError(err))
        return false
      } finally {
        setPreviewing(false)
      }
    },
    [basics.spu_code, colours, sizeTemplate],
  )

  const create = useMutation({
    mutationFn: () =>
      spusApi.create(
        {
          spu_code: basics.spu_code.trim(),
          internal_name: basics.internal_name.trim(),
          audience: basics.audience!,
          base_category: basics.base_category.trim() || 'swimwear',
          supplier_ref: basics.supplier_ref.trim() || null,
          color_variants: colours.map((c) => ({
            variant_code: c.variant_code.trim(),
            working_name: c.working_name.trim(),
            supplier_color_code: c.supplier_color_code?.trim() || null,
          })),
          size_template: sizeTemplate!,
        },
        requestKey,
      ),
    onSuccess: (spu) => {
      setCreated(spu)
      message.success(`已建档:${spu.spu_code},${spu.skus.length} 个 SKU`)
    },
    onError: onWriteError,
  })

  const sizes = useMemo(
    () => templates.data?.find((t) => t.name === sizeTemplate)?.sizes ?? [],
    [templates.data, sizeTemplate],
  )

  /** 填了任何一个字段就算有未保存内容(A11)。建档没建成时离开会全部丢失 */
  const dirty =
    !created &&
    (basics.spu_code !== '' ||
      basics.internal_name !== '' ||
      basics.audience !== undefined ||
      colours.some((c) => c.variant_code !== '' || c.working_name !== ''))

  /**
   * 每一步"能不能往下走"。
   *
   * **只挡连请求都发不出去的情况**(必填项空着)。编码字符集、重复颜色、
   * 行数上限一律交给后端 —— 抄一份规则到这里的下场是两个版本,
   * 而先过期的那一个会让一个合法的输入在界面上被拒,运营无从申诉。
   */
  const basicsReady =
    basics.spu_code.trim() !== '' &&
    basics.internal_name.trim() !== '' &&
    basics.audience !== undefined
  const coloursReady =
    colours.length > 0 && colours.every((c) => c.variant_code.trim() !== '')
  const matrixReady = sizeTemplate !== undefined && sizes.length > 0

  /**
   * 失焦时做**与后端同一个归一化**:去首尾空白 + 转大写。
   *
   * 这不是把校验抄过来。`normalize_code()` 明确只做这两件事,而且刻意
   * **不做**"删掉非法字符"那种善后(悄悄把 `BLK-1` 变成 `BLK1` 之后,
   * 运营看到的 SKU 和他填的不一样,而他收不到任何提示)。这里跟着它走,
   * 一步不多。
   *
   * 要不要转大写由后端 `normalizes_to_uppercase` 说了算 —— 规则拉不到时
   * 只 trim,行为退回这个端点存在之前那一版
   */
  const normalizeCode = (value: string) => {
    const trimmed = value.trim()
    return rules.data?.normalizes_to_uppercase ? trimmed.toUpperCase() : trimmed
  }

  const colourColumns: ColumnsType<ColorVariantDraft & { index: number }> = [
    {
      title: '颜色编码',
      dataIndex: 'variant_code',
      width: 200,
      render: (_, row) => {
        const problem = rowProblems.get(row.index)
        return (
          <Form.Item
            noStyle={!problem}
            style={{ marginBottom: 0 }}
            validateStatus={problem ? 'error' : undefined}
            help={problem}
          >
            <Input
              value={row.variant_code}
              placeholder="例如 BLK"
              status={problem ? 'error' : undefined}
              maxLength={rules.data?.max_variant_code}
              onChange={(e) =>
                setColours((prev) =>
                  prev.map((c, i) =>
                    i === row.index ? { ...c, variant_code: e.target.value } : c,
                  ),
                )
              }
              onBlur={(e) =>
                setColours((prev) =>
                  prev.map((c, i) =>
                    i === row.index
                      ? { ...c, variant_code: normalizeCode(e.target.value) }
                      : c,
                  ),
                )
              }
            />
          </Form.Item>
        )
      },
    },
    {
      title: '工作名称',
      dataIndex: 'working_name',
      render: (_, row) => (
        <Input
          value={row.working_name}
          placeholder="供应商口中的那个名字,内部用"
          maxLength={128}
          onChange={(e) =>
            setColours((prev) =>
              prev.map((c, i) =>
                i === row.index ? { ...c, working_name: e.target.value } : c,
              ),
            )
          }
        />
      ),
    },
    {
      title: '供应商色号',
      dataIndex: 'supplier_color_code',
      width: 160,
      render: (_, row) => (
        <Input
          value={row.supplier_color_code ?? ''}
          placeholder="选填"
          maxLength={64}
          onChange={(e) =>
            setColours((prev) =>
              prev.map((c, i) =>
                i === row.index
                  ? { ...c, supplier_color_code: e.target.value }
                  : c,
              ),
            )
          }
        />
      ),
    },
    {
      title: '',
      key: 'ops',
      width: 60,
      render: (_, row) => (
        <Button
          type="text"
          danger
          icon={<DeleteOutlined />}
          // 最后一行不许删:一个 SPU 至少要有一个颜色(后端 min_length=1),
          // 删到零行的话"下一步"按钮会灰掉而没有任何提示说明为什么
          disabled={colours.length <= 1}
          onClick={() => {
            setColours((prev) => prev.filter((_, i) => i !== row.index))
            // **删一行会让后面所有行的下标左移一位**,而 `problems` 里的
            // `loc` 记的是删之前的下标 —— 不清掉的话红框会挂到隔壁那一行,
            // 而那一行是对的。清空比重算下标可靠:下一次「下一步」会重新试算
            setProblems([])
            setPreview(null)
          }}
        />
      ),
    },
  ]

  if (created) {
    return (
      <Space direction="vertical" size={12} style={{ width: '100%' }}>
        <PageHeader title="建档完成" subtitle={created.spu_code} />
        <Alert
          type="success"
          showIcon
          message={`${created.spu_code} 已建档,${created.color_variants.length} 个颜色 × ${created.skus.length / Math.max(created.color_variants.length, 1)} 个尺码 = ${created.skus.length} 个 SKU`}
          description="下一步:到素材页按颜色上传样品图。通用图只进 SPU 作用域,证明不了某个颜色有正面照。"
        />
        <Card size="small" title="生成的 SKU">
          {/*
            * 这一份**是后端回出来的真值**,不是前端拼的。
            * 编码怎么拼住在 `listings/sku_matrix`,前端照着抄一份的话,
            * 改一次拼法界面和库里就是两套编码 —— 而运营会拿界面上那个
            * 去平台后台搜,搜不到,然后怀疑没建成功。
            */}
          <Table<SpuSku>
            rowKey="id"
            size="small"
            bordered
            pagination={false}
            dataSource={created.skus}
            columns={[
              { title: 'SKU', dataIndex: 'sku', render: (v: string) => <span className="mono">{v}</span> },
              { title: '尺码', dataIndex: 'size', width: 100 },
              {
                title: '颜色',
                dataIndex: 'color_variant_id',
                width: 160,
                render: (id: string | null) => {
                  const v = created.color_variants.find((c) => c.id === id)
                  return v ? (v.working_name || v.variant_code) : '—'
                },
              },
            ]}
          />
        </Card>
        <Space wrap>
          {/*
            AC-01:建档完成之后的下一步是**进七步向导**,不是回到一张列表
            去找它。这一条是 2026-08-09 评审补的 —— 在此之前建档与向导之间
            断着一跳:运营建完款,得自己去商品列表里把刚建的 SKU 找出来,
            再点进向导。AC-01 的原话是"普通运营从建档到形成完整 Draft 的
            七步全程在向导内完成",而那一跳恰恰落在第 1 步与第 2 步之间。

            落到 `?step=MATERIAL`:建档这一步刚做完,停在它上面等于让运营
            自己再点一次"下一步"。向导的 `?step=` 是白名单 codec,认不出来
            会安全退回后端算出的 `current_step`。

            `skus[0]` 一定存在:`expand()` 至少展开一行,零行会在后端 422。
          */}
          {created.skus.length > 0 && (
            <Button
              type="primary"
              onClick={() =>
                navigate(`/wizard/${created.skus[0].id}?step=MATERIAL`)
              }
            >
              进入七步向导(下一步:上传样品)
            </Button>
          )}
          <Button onClick={() => navigate(`/spus/${created.id}`)}>
            去配置生成方案
          </Button>
          {/*
            a47 §3.1:工作台带 `?from=workbench` 过来时,建完款回工作台。
            「SPU 聚合页」那颗留着 —— 它本轮撤出菜单但路由保留,
            而从建档直接去看聚合仍然是一条说得通的动线。
          */}
          {cameFromWorkbench && (
            <Button onClick={() => navigate('/workbench')}>回商品工作台</Button>
          )}
          <Button onClick={() => navigate('/workbench-spus')}>去 SPU 聚合页</Button>
          <Button
            onClick={() => {
              setCreated(null)
              setBasics(EMPTY_BASICS)
              setColours([emptyColour()])
              setSizeTemplate(undefined)
              setCurrent(0)
              // 上一次的试算结果与红框一并清掉 —— 留着的话新表单第一眼
              // 就带着上一个款的 SKU 列表
              setPreview(null)
              setProblems([])
              setFailure('')
              // **换一把键。** 不换的话下一次建档会命中上一次的键,
              // 后端按"同键不同入参"答 409 —— 而运营看到的是"又失败了"
              setRequestKey(crypto.randomUUID())
            }}
          >
            再建一个
          </Button>
        </Space>
      </Space>
    )
  }

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <UnsavedGuard dirty={dirty} what="建档表单" />
      <PageHeader
        title="新建商品款式"
        subtitle="按三步建立 SPU、颜色和首批 SKU；视觉属性可以稍后识别补全"
      />

      <Steps
        current={current}
        items={[
          { title: '基本信息' },
          { title: '颜色' },
          { title: '尺码与确认' },
        ]}
      />

      {/*
        规则拉不到时**说一句**,而不是安静地少显示几行提示。
        两者在屏幕上长得一样,而区别是"这个系统不提示"还是"这一次没拉到" ——
        后者点一下重试就好了。校验不受影响:它在后端,走试算。
      */}
      {rules.isError && (
        <Alert
          type="warning"
          showIcon
          message="编码规则没拉到 —— 下面的字符集与长度提示会缺失"
          description="校验不受影响(它在后端,点「下一步」时会真的跑一遍)。只是这一次填的时候没有提示可看。"
          action={
            <Button size="small" onClick={() => rules.refetch()}>
              重试
            </Button>
          }
        />
      )}

      {current === 0 && (
        <Card size="small" title="第一步 · 这个款是什么">
          <Form layout="vertical">
            <Form.Item
              label="SPU 编码"
              required
              // 提示文案**全部来自后端**(硬规则 4)。规则拉不到时这里是
              // 空的,而不是一句可能已经过期的硬编码
              extra={
                rules.data
                  ? `只允许 ${rules.data.allowed_charset.replace(/(.{12})/g, '$1 ')}` +
                    `,最长 ${rules.data.max_spu_code} 个字符;` +
                    `只有这一段允许含 ${rules.data.separator}(它是 SKU 编码的分段分隔符)`
                  : undefined
              }
              validateStatus={preview?.code_taken ? 'warning' : undefined}
              // **占用是警告不是错误**:运营可能正打算换一个,也可能是
              // 想改这个已存在的款。这里只把事实说出来,不挡
              help={
                preview?.code_taken
                  ? `${preview.spu_code ?? ''} 已经建过了 —— 再提交会被拒。要改它请去 SPU 详情页`
                  : undefined
              }
            >
              <Input
                value={basics.spu_code}
                placeholder="例如 SW-001"
                maxLength={rules.data?.max_spu_code ?? 64}
                onChange={(e) => setBasics((b) => ({ ...b, spu_code: e.target.value }))}
                onBlur={(e) =>
                  setBasics((b) => ({ ...b, spu_code: normalizeCode(e.target.value) }))
                }
              />
            </Form.Item>
            <Form.Item label="内部名称" required>
              <Input
                value={basics.internal_name}
                placeholder="例如 三角比基尼套装"
                maxLength={255}
                onChange={(e) =>
                  setBasics((b) => ({ ...b, internal_name: e.target.value }))
                }
              />
            </Form.Item>
            <Form.Item
              label="受众"
              required
              // §4.2:SPU 层**不存在**"待确认受众"。选不出来说明这个款还不该建档
              extra="SPU 层没有「待确认」这一档。必填属性集按受众条件化,受众没定就往下走,确认的是一份可能是错的清单"
            >
              <Select<SpuAudience>
                value={basics.audience}
                style={{ maxWidth: 240 }}
                placeholder="选择受众"
                onChange={(v) => setBasics((b) => ({ ...b, audience: v }))}
                options={Object.entries(SPU_AUDIENCE_LABEL).map(([value, label]) => ({
                  value: value as SpuAudience,
                  label,
                }))}
              />
            </Form.Item>
            <Form.Item label="品类" extra="目前只有泳装有渠道字段 spec 与校准过的属性注册表">
              <Input
                value={basics.base_category}
                style={{ maxWidth: 240 }}
                maxLength={64}
                onChange={(e) =>
                  setBasics((b) => ({ ...b, base_category: e.target.value }))
                }
              />
            </Form.Item>
            <Form.Item label="供应商编号">
              <Input
                value={basics.supplier_ref}
                style={{ maxWidth: 320 }}
                placeholder="选填"
                maxLength={128}
                onChange={(e) =>
                  setBasics((b) => ({ ...b, supplier_ref: e.target.value }))
                }
              />
            </Form.Item>
          </Form>
          <Button
            type="primary"
            disabled={!basicsReady}
            loading={previewing}
            // 第一步就试算一次:此刻答得出的是"SPU 编码本身合不合法"与
            // "这个编码是不是已经被占了"。后者在此之前要等到提交才知道,
            // 而那时表单已经填完三步
            onClick={async () => {
              await runPreview(false)
              // **不拦。** 第一步的颜色表还是空的,试算必然报"至少要有一个
              // 颜色变体" —— 拿它挡住"下一步"的话,这一页永远走不到第二步
              setCurrent(1)
            }}
          >
            下一步
          </Button>
        </Card>
      )}

      {current === 1 && (
        <Card
          size="small"
          title="第二步 · 有哪几个颜色"
          extra={
            <Button
              size="small"
              icon={<PlusOutlined />}
              onClick={() => setColours((prev) => [...prev, emptyColour()])}
            >
              加一个颜色
            </Button>
          }
        >
          {/*
            * 「正式颜色名称」(`display_name`)**不在这张表里**。
            * 它是投影列,唯一写入点是属性服务在 VARIANT 层
            * `standard_color_name` 被确认时(§4.3)。给它一个输入框的话,
            * 建档时填进去的值会在第一次识别确认时被覆盖 —— 而运营不知道
            * 是自己填错了还是系统改了。
            */}
          <Alert
            type="info"
            showIcon
            style={{ marginBottom: 10 }}
            message="这里填的是「工作名称」——供应商口中的那个名字,内部用"
            description={
              <>
                正式颜色名称由属性识别确认后回填,建档阶段填不了,也不该填。
                颜色编码在 SPU 内唯一;跨 SPU 同名是常态(几乎每个款都有黑色)。
                {/*
                  编码规则**由后端下发**(硬规则 4)。原来这一句里没有规则,
                  于是运营要靠试错才知道颜色编码里不能有横线 —— 而那次试错
                  发生在第三步提交时
                */}
                {rules.data && (
                  <div style={{ marginTop: 4 }}>
                    颜色编码只允许 <span className="mono">{rules.data.allowed_charset}</span>
                    (会自动转大写),最长 {rules.data.max_variant_code} 个字符,
                    <b>不能含 {rules.data.separator}</b> —— 它是 SKU 编码的分段分隔符,
                    颜色里含它会让两个不同的商品拼出同一个 SKU。
                    一个 SPU 最多 {rules.data.max_variants_per_spu} 个颜色。
                  </div>
                )}
              </>
            }
          />

          {/*
            **这一块是后端点名的,不是前端判的。**

            上面表格里每一行的红框也来自同一份 `problems`,但那些红框在
            表格滚动出视野之后就看不见了,而且 `spu_code` / 整张表这一类
            问题根本不指向任何一行。所以这里再说一次,并且**不会自己消失** ——
            与一条 toast 的区别正在于此。
          */}
          {(rowProblems.size > 0 || generalProblems.length > 0) && (
            <Alert
              type="error"
              showIcon
              style={{ marginBottom: 10 }}
              message="这几处要先改掉"
              description={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {[...rowProblems.entries()].map(([index, msg]) => (
                    <li key={`row-${index}`}>第 {index + 1} 行颜色:{msg}</li>
                  ))}
                  {generalProblems.map((p) => (
                    <li key={p.loc}>{p.msg}</li>
                  ))}
                </ul>
              }
            />
          )}
          <Table<ColorVariantDraft & { index: number }>
            rowKey="index"
            size="small"
            bordered
            pagination={false}
            columns={colourColumns}
            dataSource={colours.map((c, index) => ({ ...c, index }))}
          />
          <Space style={{ marginTop: 10 }}>
            <Button onClick={() => setCurrent(0)}>上一步</Button>
            <Button
              type="primary"
              disabled={!coloursReady}
              loading={previewing}
              /*
                **这一颗是这次改动的重点。**

                在此之前它只看"每一行都填了字",于是编码字符集、重复颜色
                这两类错要等到第三步点「建档」才报 —— 而它们错在这一页的
                输入框上。现在先试算一次:后端用的是与建档**同一个**
                `normalize_variant_codes()`,所以这里过了那里就不会再因为
                同样的理由被拒。

                行数上限过不了这一关(它要两个数才算得出),那一条在第三步
                选完尺码模板之后的那次试算里报。
              */
              onClick={async () => {
                if (await runPreview(false)) setCurrent(2)
              }}
            >
              下一步
            </Button>
          </Space>
        </Card>
      )}

      {current === 2 && (
        <Card size="small" title="第三步 · 尺码与确认">
          {templates.isError ? (
            <ErrorNotice
              title="尺码模板没拉到"
              error={templates.error}
              onRetry={() => templates.refetch()}
            />
          ) : (
            <Form layout="vertical">
              <Form.Item label="尺码模板" required>
                <Select<string>
                  value={sizeTemplate}
                  style={{ maxWidth: 320 }}
                  loading={templates.isLoading}
                  placeholder="选择尺码段"
                  // 换模板要重算。不重算的话下面显示的还是上一个模板展开出来的
                  // 那份 SKU,而它看起来完全正常 —— 一个不报错的错答案
                  onChange={(value) => {
                    setSizeTemplate(value)
                    setPreview(null)
                  }}
                  options={(templates.data ?? []).map((t) => ({
                    value: t.name,
                    label: `${t.label}(${t.sizes.join(' / ')})`,
                  }))}
                />
              </Form.Item>
            </Form>
          )}

          <Descriptions size="small" bordered column={2} style={{ marginBottom: 10 }}>
            <Descriptions.Item label="SPU 编码">
              <span className="mono">{basics.spu_code}</span>
            </Descriptions.Item>
            <Descriptions.Item label="内部名称">{basics.internal_name}</Descriptions.Item>
            <Descriptions.Item label="受众">
              {basics.audience ? SPU_AUDIENCE_LABEL[basics.audience] : '—'}
            </Descriptions.Item>
            <Descriptions.Item label="品类">{basics.base_category}</Descriptions.Item>
            <Descriptions.Item label="颜色" span={2}>
              <Space size={4} wrap>
                {colours.map((c, i) => (
                  <Tag key={i}>{c.working_name || c.variant_code}</Tag>
                ))}
              </Space>
            </Descriptions.Item>
            <Descriptions.Item label="尺码" span={2}>
              {sizes.length ? (
                <Space size={4} wrap>
                  {sizes.map((s) => (
                    <Tag key={s}>{s}</Tag>
                  ))}
                </Space>
              ) : (
                <span style={{ color: brandVars.textFaint }}>先选尺码模板</span>
              )}
            </Descriptions.Item>
          </Descriptions>

          {/*
            * **这份 SKU 是后端算出来的。**
            *
            * 上一版这里只显示 `颜色数 × 尺码数`,注释论证的是"编码怎么拼
            * 住在 `listings/sku_matrix`,前端拼一份的话界面和库里就是两套"。
            * 那个论证是对的,而它当时的结论(所以只显示数量)是**当时唯一
            * 的选项**:没有任何端点能在不写库的前提下回答"这次会建出哪几行"。
            *
            * `POST /spus/preview` 补上之后,顾虑本身就没了 —— 这份列表走的
            * 是 `expand()`,与建档同一个函数,前端一个字符都没拼。
            *
            * 行数上限也在这一次试算里被判掉。它是四类失败里唯一必须等到
            * 选完模板才答得出的一条(要两个数才乘得出来)。
            */}
          {matrixReady ? (
            <Card
              size="small"
              type="inner"
              title="会生成这些 SKU"
              extra={
                <Button size="small" loading={previewing} onClick={() => runPreview(true)}>
                  重新试算
                </Button>
              }
            >
              <Space size={32} style={{ marginBottom: 10 }}>
                <Statistic title="颜色" value={colours.length} />
                <Statistic title="尺码" value={sizes.length} />
                <Statistic
                  title="SKU 合计"
                  // 试算回来之前不显示前端自己乘的那个数 —— 它和后端算的
                  // 通常一样,而"通常一样"正是最难发现的那类不一致
                  value={preview?.sku_count ?? '—'}
                  valueStyle={{ color: brandVars.slate }}
                />
              </Space>
              {preview && preview.skus.length > 0 ? (
                <Table<PlannedSku>
                  rowKey="sku"
                  size="small"
                  bordered
                  pagination={preview.skus.length > 12 ? { pageSize: 12 } : false}
                  dataSource={preview.skus}
                  columns={[
                    {
                      title: 'SKU',
                      dataIndex: 'sku',
                      render: (v: string) => <span className="mono">{v}</span>,
                    },
                    { title: '颜色', dataIndex: 'variant_code', width: 140 },
                    { title: '尺码', dataIndex: 'size', width: 100 },
                  ]}
                />
              ) : (
                <p style={{ margin: 0, color: brandVars.textFaint }}>
                  点「重新试算」看这次会建出哪几行 —— 编码由后端生成,这里不复制一份拼法。
                </p>
              )}
            </Card>
          ) : (
            <Empty image={Empty.PRESENTED_IMAGE_SIMPLE} description="选好尺码模板才能试算" />
          )}

          {/*
            **这条 Alert 原来在说一句它自己做不到的话。**

            原文是「上面的提示里有具体原因」—— 而"上面的提示"是一条
            `message.error` 的 toast,它会自己消失。运营读到这条 Alert 时,
            那句具体原因大概率已经不在屏幕上了;而真正的原因(哪一行的
            哪个编码)后端本来就点名了,只是被前端丢在了字符串拼接里。

            现在它自己带着原因,并且指得出该回哪一步。
          */}
          {failure && (
            <Alert
              style={{ marginTop: 10 }}
              type="error"
              showIcon
              message={create.isError ? '建档没成功' : '这次试算没通过'}
              description={
                <>
                  <div>{failure}</div>
                  {(rowProblems.size > 0 || generalProblems.length > 0) && (
                    <ul style={{ margin: '6px 0 0', paddingLeft: 18 }}>
                      {[...rowProblems.entries()].map(([index, msg]) => (
                        <li key={`row-${index}`}>第 {index + 1} 行颜色:{msg}</li>
                      ))}
                      {generalProblems.map((p) => (
                        <li key={p.loc}>{p.msg}</li>
                      ))}
                    </ul>
                  )}
                  <div style={{ marginTop: 6 }}>
                    改完再提交一次即可,不会产生半个 SPU —— 建档是一次事务。
                    {rowProblems.size > 0 && (
                      <Button
                        type="link"
                        size="small"
                        style={{ padding: '0 4px' }}
                        onClick={() => setCurrent(1)}
                      >
                        回第二步改颜色
                      </Button>
                    )}
                  </div>
                </>
              }
            />
          )}

          <Space style={{ marginTop: 10 }}>
            <Button onClick={() => setCurrent(1)}>上一步</Button>
            <Button
              type="primary"
              loading={create.isPending || previewing}
              disabled={!matrixReady || !basicsReady || !coloursReady}
              /*
                提交前**再试算一次**。看起来多余(第二步已经试过),但那一次
                不含尺码模板,而模板决定行数上限、也可能在这一步被换过。
                试算不写库、不计费,这一次的代价是一次读请求。

                试算不过就不提交 —— 提交只会拿到同一条错误,而中间要多等
                一次事务和一把幂等键
              */
              onClick={async () => {
                if (await runPreview(true)) create.mutate()
              }}
            >
              建档
            </Button>
          </Space>
        </Card>
      )}
    </Space>
  )
}
