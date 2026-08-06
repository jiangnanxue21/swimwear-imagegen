import { useCallback, useEffect, useState } from 'react'
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
} from 'antd'

import { VISION_SYSTEM_PROMPT, promptsApi } from '../api/prompts'
import { isAuthError, readError } from '../api/client'
import { useWriteError } from '../hooks/useWriteError'
import type { PromptVersion } from '../api/types'
import UnsavedGuard from '../components/UnsavedGuard'
import { formatDateTime } from '../utils/datetime'
import BrandTag from '../components/BrandTag'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { fontScale } from '../theme'
import PageHeader from '../components/PageHeader'

const { Paragraph, Text } = Typography

/**
 * 系统提示词编辑页。
 *
 * 全文开放编辑,不做内容拦截 —— 这是明确选择过的。但保存前会跑一遍体检,
 * 把结果显示出来:提示词和 JSON Schema 是同一份约束的两种表达,
 * 手滑删掉一个约束不会当场报错,只会让模型开始间歇性输出解析失败的结果,
 * 而界面上只显示「这张图评分失败」。查这种问题很费劲,提前一行提示成本几乎为零。
 */
export default function PromptsPage() {
  useDocumentTitle('提示词')
  const { message, modal } = App.useApp()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<string | null>(null)
  const [note, setNote] = useState('')
  const [diffOpen, setDiffOpen] = useState(false)

  const query = useQuery({
    queryKey: ['prompts', VISION_SYSTEM_PROMPT],
    queryFn: () => promptsApi.read(VISION_SYSTEM_PROMPT),
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
    queryFn: () => promptsApi.preview(VISION_SYSTEM_PROMPT, checked),
    enabled: dirty && checked.length > 0,
  })

  const invalidate = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['prompts', VISION_SYSTEM_PROMPT] })
  }, [queryClient])

  /**
   * 三个动作都会改变**在线上生效的评分口径**(BLOCK-05)。
   *
   * 保存超时后再点一次会落第二个版本;切版本和恢复默认超时后再点一次,
   * 生效的是哪一版取决于两个请求到达后端的顺序。而"现在生效的是哪一版"
   * 决定了每一张图怎么被打分 —— 先刷新,版本表自己会说。
   */
  const onWriteError = useWriteError(invalidate)

  const save = useMutation({
    mutationFn: () => promptsApi.save(VISION_SYSTEM_PROMPT, content, note),
    onSuccess: (data) => {
      message.success(`已保存为第 ${data.saved_version} 版并生效`)
      setDraft(null)
      setNote('')
      invalidate()
    },
    onError: onWriteError,
  })

  const activate = useMutation({
    mutationFn: (version: number) => promptsApi.activate(VISION_SYSTEM_PROMPT, version),
    onSuccess: (data) => {
      message.success(`已切回第 ${data.version} 版`)
      invalidate()
    },
    onError: onWriteError,
  })

  const reset = useMutation({
    mutationFn: () => promptsApi.reset(VISION_SYSTEM_PROMPT),
    onSuccess: () => {
      message.success('已回到内置默认提示词,历史版本仍然保留')
      invalidate()
    },
    onError: onWriteError,
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
      width: 100,
      render: (_: unknown, row: PromptVersion) =>
        row.is_active ? null : (
          <Button
            size="small"
            loading={activate.isPending}
            onClick={() =>
              modal.confirm({
                title: `切回第 ${row.version} 版?`,
                content: '当前生效的版本不会被删除,随时可以再切回来。',
                onOk: () => activate.mutateAsync(row.version),
              })
            }
          >
            切回这版
          </Button>
        ),
    },
  ]

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
    <PageHeader title="提示词" subtitle="生成与评分用的提示词模板,改动会落新版本" />
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
          message="需要口令才能查看提示词"
          description="在「设置」页顶部填入后端 ADMIN_TOKEN 里的口令并点「记住」。"
        />
      )}
      {query.isError && !needsToken && (
        <Alert type="error" showIcon message="读取失败" description={readError(query.error)} />
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
                  content: '历史版本仍然保留,之后可以再切回任意一版。',
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
        title="内置默认提示词"
        width={900}
      >
        <Input.TextArea
          value={query.data?.default_content ?? ''}
          readOnly
          autoSize={{ minRows: 16, maxRows: 30 }}
          style={{ fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace', fontSize: fontScale.body }}
        />
      </Modal>
    </Space>
  )
}
