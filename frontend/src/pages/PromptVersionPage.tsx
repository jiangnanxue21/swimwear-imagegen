import { useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useParams } from 'react-router-dom'
import { Alert, Button, Card, Descriptions, Input, Space, Typography } from 'antd'

import { promptsApi } from '../api/prompts'
import { isAuthError } from '../api/client'
import BrandTag from '../components/BrandTag'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import TextDiff from '../components/TextDiff'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { fontScale } from '../theme'

const { Text } = Typography

/**
 * 单版本只读视图(`/prompts/:key/versions/:n`,FE-303)。
 *
 * ## 为什么值得单开一个页面
 *
 * 在 BE-303 之前,`describe()` 返回的版本列表里只有 `chars`,没有正文,也没有
 * 取单版本正文的端点。想看 v3 写了什么,**唯一的办法是把它切成生效版本** ——
 * 而这个动作会立刻改掉线上每一张图的评分口径。「想看一眼」和「想让它生效」
 * 被绑成了同一个动作,而这两件事的风险差着一整条业务链路。
 *
 * ## 这一页刻意什么都不能做
 *
 * 没有编辑框、没有保存、没有「切到这版」。要切版本回详情页去切 —— 那里有
 * 二次确认,而确认文案说的是"当前生效的版本不会被删除"。把切换按钮放在一个
 * 标题写着"只读"的页面上,是在训练人忽略这一页的名字。
 *
 * ## 与当前生效版对比(FE-306,a68 补)
 *
 * 这段注释原来写着「不在这一轮 —— **它需要一个 diff 组件**」。`TextDiff` 是 a60
 * 写的,那个理由从 a60 起就不成立,而它在原地又躺了七轮 —— §3.33 那句
 * 「过期的理由比没有理由更糟」说的正是这种:读的人会照着它判断"还差一个组件",
 * 而实际上只差把两段文本递进去。
 *
 * 对比的右边取**当前生效正文**而不是编辑框:这一页是只读的,它回答的问题是
 * 「这一版和现在跑着的差在哪」,不是「和我正在写的差在哪」(那是详情页 FE-307)。
 */
export default function PromptVersionPage() {
  const { key: routeKey, version: routeVersion } = useParams<{
    key: string
    version: string
  }>()
  const promptKey = routeKey ?? ''
  // 路由参数永远是字符串。`Number('abc')` 是 NaN,不拦的话会去请求
  // `/prompts/x/versions/NaN`,拿回一个 404 而界面说"读取失败" ——
  // 那句话是对的但没用:错在地址,不在服务端
  const version = Number(routeVersion)
  const versionValid = Number.isInteger(version) && version >= 1

  useDocumentTitle(versionValid ? `提示词 · 第 ${version} 版` : '提示词')

  const [compare, setCompare] = useState(false)

  // 当前生效正文。与详情页共用 react-query 缓存,从那边点进来时不会多发一次请求
  const current = useQuery({
    queryKey: ['prompts', promptKey],
    queryFn: () => promptsApi.read(promptKey),
    enabled: compare,
    retry: (count, err) => !isAuthError(err) && count < 2,
  })

  const query = useQuery({
    queryKey: ['prompt-version', promptKey, version],
    queryFn: () => promptsApi.readVersion(promptKey, version),
    enabled: versionValid,
    retry: (count, err) => !isAuthError(err) && count < 2,
  })
  const needsLogin = query.isError && isAuthError(query.error)

  return (
    <Space direction="vertical" size="large" style={{ width: '100%' }}>
      <PageHeader
        title={`第 ${routeVersion} 版`}
        subtitle="只读。要让这一版生效,回详情页切换"
      />
      <Link to={`/prompts/${promptKey}`}>← 回到这条提示词</Link>

      {!versionValid && (
        <Alert
          type="error"
          showIcon
          message="版本号不是一个正整数"
          description="地址里的版本号需要是 1 起的整数,比如 /versions/3。"
        />
      )}
      {needsLogin && (
        <Alert
          type="warning"
          showIcon
          message="需要管理员身份才能查看提示词"
          description="请登录管理员账号后重试。"
        />
      )}
      {query.isError && !needsLogin && (
        <ErrorNotice
          title={`读不到第 ${routeVersion} 版`}
          error={query.error}
          onRetry={() => query.refetch()}
          retrying={query.isFetching}
        />
      )}

      {versionValid && (
        <Card loading={query.isLoading}>
          <Space direction="vertical" size="middle" style={{ width: '100%' }}>
            <Descriptions size="small" column={2}>
              <Descriptions.Item label="状态">
                {query.data?.is_active ? (
                  <BrandTag tone="success">生效中</BrandTag>
                ) : (
                  <Text type="secondary">未生效</Text>
                )}
              </Descriptions.Item>
              <Descriptions.Item label="署名">
                {query.data?.updated_by || <Text type="secondary">—</Text>}
              </Descriptions.Item>
              <Descriptions.Item label="说明" span={2}>
                {query.data?.note || (
                  <Text type="secondary">
                    这一版没写说明 —— 回滚时它本来是唯一的线索
                  </Text>
                )}
              </Descriptions.Item>
            </Descriptions>

            <Space>
              <Button size="small" onClick={() => setCompare((on) => !on)}>
                {compare ? '看正文' : '与当前生效版对比'}
              </Button>
              {compare && current.isFetching && <Text type="secondary">正在取生效版…</Text>}
            </Space>

            {/* 这条查询失败时**不能静默**:失败 -> `current.data` 是 undefined ->
                右边是空串 -> diff 会把整份正文画成"全部被删掉",而那是一句假话。
                与本页 44 行起批判的 fail-open 是同一个形状 */}
            {compare && current.isError && !isAuthError(current.error) && (
              <ErrorNotice
                title="取不到当前生效版,没法对比"
                type="warning"
                error={current.error}
                onRetry={() => current.refetch()}
                retrying={current.isFetching}
              />
            )}

            {compare && !current.isError ? (
              <TextDiff
                left={query.data?.content ?? ''}
                right={current.data?.content ?? ''}
                leftLabel={`第 ${routeVersion} 版`}
                rightLabel="当前生效"
              />
            ) : (
              <Input.TextArea
                value={query.data?.content ?? ''}
                readOnly
                autoSize={{ minRows: 18, maxRows: 40 }}
                spellCheck={false}
                style={{
                  fontFamily: 'ui-monospace, SFMono-Regular, Menlo, monospace',
                  fontSize: fontScale.body,
                }}
              />
            )}
            <Text type="secondary">{(query.data?.content ?? '').length} 字</Text>
          </Space>
        </Card>
      )}
    </Space>
  )
}
