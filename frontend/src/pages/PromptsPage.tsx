import { useCallback, useEffect, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Alert,
  Button,
  Card,
  Input,
  Modal,
  Space,
  Table,
  Tag,
  Typography,
  App,
  Collapse,
} from 'antd'

import { promptsApi } from '../api/prompts'
import { isAuthError, readError } from '../api/client'
import { useWriteError } from '../hooks/useWriteError'
import type { PromptVersion } from '../api/types'
import UnsavedGuard from '../components/UnsavedGuard'
import { formatDateTime } from '../utils/datetime'
import BrandTag from '../components/BrandTag'
import ErrorNotice from '../components/ErrorNotice'
import { runsQueryForVersion } from '../utils/aiTestRuns'
import TextDiff from '../components/TextDiff'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { fontScale } from '../theme'
import PageHeader from '../components/PageHeader'

const { Paragraph, Text } = Typography

/**
 * 单个提示词的编辑页(`/prompts/:key`)。
 *
 * 全文开放编辑,不做内容拦截 —— 这是明确选择过的。但保存前会跑一遍体检,
 * 把结果显示出来:提示词和 JSON Schema 是同一份约束的两种表达,
 * 手滑删掉一个约束不会当场报错,只会让模型开始间歇性输出解析失败的结果,
 * 而界面上只显示「这张图评分失败」。查这种问题很费劲,提前一行提示成本几乎为零。
 *
 * ## 从「写死女装」改成「按 key」(FE-302)
 *
 * 这一页原来把 `vision_system_prompt` 写死在四个地方(读、体检、保存、切版本)。
 * 后果不是不方便,是**男装那份提示词后端全通、前端一个入口都没有** —— 注册表里
 * 那行 `ui_reachable=False` 就是为这个状态留的记录,而它的解锁条件写着「FE-301
 * 落地」。key 来自路由之后,两份提示词走的是同一段代码,不存在"男装那条路径没人走过"。
 *
 * **`label` 从注册表拿,不在前端写一份键到中文名的映射。** 写一份的话,后端加
 * 第 9 处提示词时前端会显示一个原始 key,而那正是 a52 收敛过的东西
 * (「后端枚举不许直接摆在界面上」)。
 */
export default function PromptsPage() {
  const { key: routeKey } = useParams<{ key: string }>()
  const promptKey = routeKey ?? ''
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()

  // 列表接口顺带回答了"这个 key 叫什么中文名、能不能改"。它与详情共用
  // react-query 缓存,从列表点进来时不会多发一次请求
  const surfaces = useQuery({
    queryKey: ['prompt-surfaces'],
    queryFn: () => promptsApi.list(),
    retry: (count, err) => !isAuthError(err) && count < 2,
  })
  const surface = surfaces.data?.surfaces.find((item) => item.key === promptKey)
  useDocumentTitle(surface ? `提示词 · ${surface.label}` : '提示词')
  const [draft, setDraft] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [diffOpen, setDiffOpen] = useState(false)

  const query = useQuery({
    queryKey: ['prompts', promptKey],
    queryFn: () => promptsApi.read(promptKey),
    retry: (count, err) => !isAuthError(err) && count < 2,
  })
  const needsToken = query.isError && isAuthError(query.error)

  // 服务端内容变了(保存成功、回滚)就丢掉本地草稿,否则会拿旧文本盖掉刚回滚的版本
  useEffect(() => {
    setDraft(null)
  }, [query.data?.version, query.data?.is_default])

  const content = draft ?? query.data?.content ?? ''
  const dirty = draft !== null && draft !== query.data?.content

  /**
   * 体检的防抖输入(BLOCK-14 前端半边)。
   *
   * `queryKey` 原来直接是 `content` —— 每敲一个字符就是一个新 key,
   * 也就是**一次带管理口令的 POST**。写一段两千字的提示词等于两千次请求,
   * 而它们还会互相取消、让警告区来回闪。这是一个纯诊断功能,
   * 不值得这个代价。
   *
   * 400ms 是"停下来想一下"的量级:连续打字时一次都不发,手一停才发一次。
   */
  const [checked, setChecked] = useState('')
  useEffect(() => {
    const timer = window.setTimeout(() => setChecked(content), 400)
    return () => window.clearTimeout(timer)
  }, [content])

  const preview = useQuery({
    queryKey: ['prompt-preview', checked],
    queryFn: () => promptsApi.preview(promptKey, checked),
    // 空内容也要送体检(FE-308)。上一版 `&& checked.length > 0` 是个 fail-open:
    // 清空文本框 -> 不发请求 -> preview.data 是 undefined -> `?? []` -> 界面上
    // 什么都不显示,保存按钮照常可点 —— 而 lint 对空串的第一条返回值恰恰是
    // 「提示词是空的」。与本文件 124 行起那段注释批判的是同一个 bug:
    // isError 那一支修好了,enabled 这一支漏了。
    enabled: dirty,
  })

  const invalidate = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['prompts', promptKey] })
  }, [promptKey, queryClient])

  /**
   * 三个动作都会改变**在线上生效的评分口径**(BLOCK-05)。
   *
   * 保存超时后再点一次会落第二个版本;切版本和恢复默认超时后再点一次,
   * 生效的是哪一版取决于两个请求到达后端的顺序。而"现在生效的是哪一版"
   * 决定了每一张图怎么被打分 —— 先刷新,版本表自己会说。
   */
  const onWriteError = useWriteError(invalidate)

  /**
   * 服务端**此刻**的生效版本,原样回传给写端点(FE-309)。
   *
   * `query.data.version` 为 null 表示内置默认生效中 —— 这一支必须能表达出来,
   * 用 0 之类的哨兵值会让「回到默认」和「第 0 版」长得一样。
   */
  const seenVersion = query.data ? query.data.version : undefined
  const [conflict, setConflict] = useState<unknown>(null)
  const [rollbackNote, setRollbackNote] = useState('')

  /** 409 单独接住:它不是"失败",是"有人在你之前动过"。 */
  const onConflictOr = useCallback(
    (fallback: (err: unknown) => void) => (err: unknown) => {
      const detail = (err as { response?: { status?: number; data?: unknown } })?.response
      if (detail?.status === 409) {
        // 存**原始 error**,不先拍平成一句话 —— 拍平了请求编号与技术详情就没有
        // 落点,而这正是 `<ErrorNotice>` 存在的理由(A12)。棘轮盯的也是这一点
        setConflict(err)
        invalidate()
        return
      }
      fallback(err)
    },
    [invalidate],
  )

  const save = useMutation({
    mutationFn: () => promptsApi.save(promptKey, content, note, seenVersion),
    onSuccess: (data) => {
      message.success(`已保存为第 ${data.saved_version} 版并生效`)
      setDraft(null)
      setNote('')
      invalidate()
    },
    onError: onConflictOr(onWriteError),
  })

  const activate = useMutation({
    mutationFn: (version: number) =>
      promptsApi.activate(promptKey, version, rollbackNote, seenVersion),
    onSuccess: (data) => {
      message.success(`已切回第 ${data.version} 版`)
      invalidate()
    },
    onError: onConflictOr(onWriteError),
  })

  const reset = useMutation({
    mutationFn: () => promptsApi.reset(promptKey, rollbackNote, seenVersion),
    onSuccess: () => {
      message.success('已回到内置默认提示词,历史版本仍然保留')
      invalidate()
    },
    onError: onConflictOr(onWriteError),
  })

  /**
   * 体检结果(BLOCK-14 前端半边的第二处)。
   *
   * ## 原来的写法为什么错
   *
   *     warnings = dirty ? (preview.data?.warnings ?? []) : (query.data?.warnings ?? [])
   *
   * `preview` 失败时 `data` 是 undefined,`?? []` 把它变成**空数组**,
   * 而空数组在下面渲染成"什么都不显示" —— 也就是说体检请求挂掉之后,
   * 界面表达的是「这一版没有问题」。这是一个 fail-open 的判定:
   * 失败方向指向"放心保存",而体检存在的全部理由是拦住那些不该放心保存的改动。
   *
   * 三种状态必须分开表达:检过了没问题 / 检过了有问题 / **没检成**。
   */
  const previewBroken = dirty && preview.isError
  const previewStale = dirty && checked !== content
  const warnings = dirty ? (preview.data?.warnings ?? []) : (query.data?.warnings ?? [])

  const columns = [
    {
      title: '版本',
      dataIndex: 'version',
      width: 90,
      render: (v: number, row: PromptVersion) => (
        <Space>
          <Text strong>v{v}</Text>
          {row.is_active && <BrandTag tone="success">生效中</BrandTag>}
        </Space>
      ),
    },
    { title: '说明', dataIndex: 'note', render: (v: string | null) => v || <Text type="secondary">—</Text> },
    { title: '署名', dataIndex: 'updated_by', width: 120, render: (v: string | null) => v || '—' },
    { title: '字数', dataIndex: 'chars', width: 90 },
    {
      title: '时间',
      dataIndex: 'created_at',
      width: 180,
      render: (v: string | null) => formatDateTime(v),
    },
    {
      title: '操作',
      width: 180,
      render: (_: unknown, row: PromptVersion) => (
        <Space>
          {/* 「想看一眼」和「想让它生效」在这之前是同一个动作:唯一能看到 v3
              正文的办法是把它切成生效版,而那会立刻改掉线上每一张图的评分口径 */}
          <Link to={`/prompts/${promptKey}/versions/${row.version}`}>查看</Link>
          {/* FE-314 正向互链:这一版跑过哪些测试。
              **口径在 a63 才定下来**(§3.96):评分链路落的是
              `prompt_templates.version` 自增序号,文案落的是 `模型:哈希` ——
              两类值形状不同,按 `prompt_version` 查必然对不上。这一页管的是
              评分那两份,所以按 `prompt_template_version` 查。
              `runsQueryForVersion` 判不出参数时返回 null,这里就不画入口 ——
              一个点进去永远是空的链接,会被读成「这一版没跑过测试」 */}
          {runsQueryForVersion(promptKey, row.version) && (
            <Link
              to={`/ai-tests?prompt_key=${promptKey}&prompt_template_version=${row.version}`}
            >
              跑过的测试
            </Link>
          )}
          {!row.is_active && (
            <Button
              size="small"
              loading={activate.isPending}
              onClick={() =>
                modal.confirm({
                  title: `切回第 ${row.version} 版?`,
                  content: (
                    <Space direction="vertical" size={8} style={{ width: '100%' }}>
                      <span>当前生效的版本不会被删除,随时可以再切回来。</span>
                      <Input
                        placeholder="为什么退回这一版(保存有理由,回滚在此之前没有)"
                        maxLength={500}
                        onChange={(e) => setRollbackNote(e.target.value)}
                      />
                    </Space>
                  ),
                  onOk: () => activate.mutateAsync(row.version),
                })
              }
            >
              切回这版
            </Button>
          )}
        </Space>
      ),
    },
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <PageHeader
        title={surface ? surface.label : '提示词'}
        subtitle="改动会落一个新版本,旧版本永远保留"
      />
      <Link to="/prompts">← 回到提示词列表</Link>

      {/* 路由上的 key 不在注册表里(手敲地址、或后端删了一处)。
          不显示一个空编辑框 —— 空框看起来像"这份提示词是空的",
          而它实际是"这份提示词不存在" */}
      {/* 这条查询失败时**不能静默**:失败 -> surfaces.data 是 undefined ->
          上面那句 `surfaces.data && !surface` 不成立 -> 界面什么都不说,
          而页头会显示"提示词"而不是这份提示词的名字。与本页 124 行起批判的
          fail-open 是同一个形状 */}
      {surfaces.isError && !isAuthError(surfaces.error) && (
        <ErrorNotice
          title="拉不到提示词清单,页头与可编辑性可能显示不全"
          type="warning"
          error={surfaces.error}
          onRetry={() => surfaces.refetch()}
          retrying={surfaces.isFetching}
        />
      )}
      {surfaces.data && !surface && (
        <Alert
          type="error"
          showIcon
          message={`没有名为 ${promptKey} 的提示词`}
          description="它可能已经被移除,或者地址敲错了。回到列表看看现在有哪些。"
        />
      )}
      {/* 409 不是"失败",是"有人在你之前动过"。所以它不走 ErrorNotice 那条
          「重试」话术 —— 重试正是这里最不该做的事:它会覆盖对方 */}
      {conflict != null && (
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          {/* **不给 `onRetry`。** 409 不是"失败",是"有人在你之前动过" ——
              而重试正是这里最不该做的事:它会覆盖对方。`<ErrorNotice>` 只在
              给了 onRetry 时才画重试按钮,所以这里靠不给来表达 */}
          <ErrorNotice
            type="warning"
            title="这条提示词在你编辑期间被别人改过"
            error={conflict}
            kind="write"
          />
          <Text type="secondary">
            下面的编辑框里仍然是<b>你写的那一版</b>,没有被覆盖。版本表已经刷新到最新,
            先点「查看」看看对方改了什么,再决定要不要按你的版本再存一次。
          </Text>
          <Button size="small" onClick={() => setConflict(null)}>
            我知道了
          </Button>
        </Space>
      )}
      {surface && !surface.editable && (
        <Alert
          type="warning"
          showIcon
          message="这处提示词的正文由代码拼装,存进库里不会生效"
          description="所以这里不提供保存入口。要改它,改代码。"
        />
      )}
      <UnsavedGuard dirty={dirty} what="提示词" />
      <Alert
        type="info"
        showIcon
        message="这段提示词决定评分口径"
        description={
          <>
            <Paragraph style={{ marginBottom: 4 }}>
              它只影响<Text strong>模型怎么打分</Text>。总分、A/B/C/D 分档、是否自动通过
              仍然全部由后端计算,改这里不会绕过任何一条业务规则。
            </Paragraph>
            <Paragraph style={{ marginBottom: 0 }}>
              评分维度清单、硬错误代码、JSON 格式要求由代码按评分深度注入到用户段,
              即使这里删光了也不影响运行 —— 但模型会少很多上下文,判断质量会下降。
              <Text strong>改完请用人工审核样本重新校准阈值。</Text>
            </Paragraph>
          </>
        }
      />

      {needsToken && (
        <Alert
          type="warning"
          showIcon
          message="需要管理员身份才能查看提示词"
          description="请登录管理员账号。旧版「在设置页填口令」的入口已随 localStorage 口令链一起移除(api/client.ts 382 行是现行口径),这里不再指路到一个不存在的输入框。"
        />
      )}
      {query.isError && !needsToken && (
        <Alert type="error" showIcon message="读取失败" description={readError(query.error)} />
      )}

      {/* FE-305:防注入段。它在编辑框**上方**而不是里面 —— 这一段是代码拼进
          默认提示词的,运营改了正文之后它可能已经不在编辑框里了,而
          `lint_prompt` 的 `no_anti_injection` 会在保存前告警。
          把它单独摆出来,是为了让「我删了什么」这件事在告警响之前就看得见 */}
      {query.data?.anti_injection_block && (
        <Collapse
          size="small"
          items={[
            {
              key: 'anti-injection',
              label: '防注入段(这段来自代码,删掉会告警)',
              children: (
                <Input.TextArea
                  value={query.data.anti_injection_block}
                  readOnly
                  autoSize={{ minRows: 4, maxRows: 14 }}
                  spellCheck={false}
                  style={{
                    fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                    fontSize: fontScale.body,
                  }}
                />
              ),
            },
          ]}
        />
      )}

      <Card
        loading={query.isLoading}
        title={
          <Space>
            <span>系统提示词</span>
            {query.data?.is_default ? (
              <Tag>内置默认</Tag>
            ) : (
              <BrandTag tone="accent">第 {query.data?.version} 版</BrandTag>
            )}
            {dirty && <BrandTag tone="sand">未保存</BrandTag>}
          </Space>
        }
        extra={
          <Space>
            <Button onClick={() => setDiffOpen(true)}>查看内置默认</Button>
            <Button
              disabled={query.data?.is_default}
              loading={reset.isPending}
              onClick={() =>
                modal.confirm({
                  title: '回到内置默认提示词?',
                  // 三个动作里它破坏性最强(一次把所有版本停用),
                  // 而在 FE-310 之前它是唯一不必解释自己的
                  content: (
                    <Space direction="vertical" size={8} style={{ width: '100%' }}>
                      <span>历史版本仍然保留,之后可以再切回任意一版。</span>
                      <Input
                        placeholder="为什么退回内置默认(会记进审计,三个月后这是唯一的线索)"
                        maxLength={500}
                        onChange={(e) => setRollbackNote(e.target.value)}
                      />
                    </Space>
                  ),
                  onOk: () => reset.mutateAsync(),
                })
              }
            >
              恢复默认
            </Button>
            <Button type="primary" disabled={!dirty} loading={save.isPending} onClick={() => save.mutate()}>
              保存并生效
            </Button>
          </Space>
        }
      >
        <Space direction="vertical" size="middle" style={{ width: '100%' }}>
          <Input.TextArea
            value={content}
            onChange={(e) => setDraft(e.target.value)}
            autoSize={{ minRows: 18, maxRows: 40 }}
            spellCheck={false}
            style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: fontScale.body }}
          />

          <Text type="secondary">{content.length} 字</Text>

          {/* 「没检成」要单独说,不能表现成「没有警告」—— 见 previewBroken 的注释。
              保存**不因此被禁用**:体检从一开始就是提示性的(文件头写着
              「全文开放编辑,不做内容拦截」),把它升级成闸门是另一个决定,
              不该由一次请求失败顺手做掉 */}
          {previewBroken && (
            <Alert
              type="warning"
              showIcon
              message="这一版没能体检成功,下面不显示警告不代表没有问题"
              description={
                <Space direction="vertical" size={4} style={{ width: '100%' }}>
                  <span>{readError(preview.error)}</span>
                  <Button size="small" onClick={() => preview.refetch()} loading={preview.isFetching}>
                    重新体检
                  </Button>
                </Space>
              }
            />
          )}
          {!previewBroken && previewStale && (
            <Text type="secondary">正在体检改动……</Text>
          )}

          {warnings.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message="体检发现几处值得确认的地方(不影响保存)"
              description={
                <ul style={{ margin: 0, paddingLeft: 18 }}>
                  {warnings.map((w) => (
                    <li key={w.code}>{w.message}</li>
                  ))}
                </ul>
              }
            />
          )}

          {dirty && (
            <Input
              placeholder="这一版改了什么、为什么改(回滚时这是唯一的线索)"
              value={note}
              onChange={(e) => setNote(e.target.value)}
              maxLength={500}
            />
          )}
        </Space>
      </Card>

      <Card title="版本历史" loading={query.isLoading}>
        {/* 内置默认在版本表里原本没有位置:它不是一行记录,而"当前生效的是它"
            却是版本表最该回答的事。钉一行虚拟的(FE-306),`version` 用 0 只作
            React key —— 它不会被传给任何接口,后端的 `expected_version`
            走 `query.data.version`(null),两者不混用 */}
        <Space direction="vertical" size="small" style={{ width: '100%' }}>
          <Space>
            <Text strong>内置默认</Text>
            {query.data?.is_default ? (
              <BrandTag tone="success">生效中</BrandTag>
            ) : (
              <Text type="secondary">未生效(库里有版本正生效)</Text>
            )}
            <Button size="small" onClick={() => setDiffOpen(true)}>
              与当前编辑中的内容对比
            </Button>
          </Space>
        </Space>
        {query.data?.versions.length ? (
          <Table
            rowKey="version"
            size="small"
            pagination={false}
            columns={columns}
            dataSource={query.data.versions}
          />
        ) : (
          <Text type="secondary">
            还没有保存过任何版本,当前用的是代码内置的默认提示词。
          </Text>
        )}
      </Card>

      <Modal
        open={diffOpen}
        onCancel={() => setDiffOpen(false)}
        footer={
          <Space>
            <Button onClick={() => setDiffOpen(false)}>关闭</Button>
            <Button
              type="primary"
              onClick={() => {
                setDraft(query.data?.default_content ?? '')
                setDiffOpen(false)
                message.info('已填入编辑框,还没有保存')
              }}
            >
              填入编辑框
            </Button>
          </Space>
        }
        title="内置默认 对 当前编辑中的内容"
        width={1000}
      >
        {/* 这个状态从第一版起就叫 `diffOpen`,而里面只有一个只读 textarea ——
            **没有任何对比**。名字先承认了这件事(FE-307)。
            右边取的是编辑框里的内容而不是已保存的版本:运营点开它,想知道的是
            "我现在写的这段和内置默认差在哪",不是"上一次保存的和默认差在哪" */}
        <TextDiff
          left={query.data?.default_content ?? ''}
          right={content}
          leftLabel="内置默认"
          rightLabel="当前编辑中"
        />
      </Modal>
    </Space>
  )
}
