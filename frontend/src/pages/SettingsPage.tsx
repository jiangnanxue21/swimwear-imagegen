import { type ReactNode, useCallback, useMemo, useState } from 'react'
import {
  Alert,
  App,
  Button,
  Card,
  Collapse,
  Input,
  InputNumber,
  Select,
  Skeleton,
  Space,
  Switch,
  Tag,
  Tooltip,
  Typography,
} from 'antd'
import { LockOutlined } from '@ant-design/icons'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useWriteError } from '../hooks/useWriteError'
import { settingsApi } from '../api/settings'
import { providersApi } from '../api/generation'
import {
  isAuthError,
  readAdminToken,
  readError,
  readOperatorToken,
  writeAdminToken,
  writeOperatorToken,
} from '../api/client'
import { SETTING_GROUP_PROVIDER, SETTING_SOURCE_LABEL } from '../api/types'
import type { SettingField, SettingGroup } from '../api/types'
import UnsavedGuard from '../components/UnsavedGuard'
import BrandTag from '../components/BrandTag'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { brandVars, fontScale } from '../theme'

type Draft = Record<string, string>

/** 字段当前该显示什么。密钥永远从空开始 —— 界面上的打码串不是可编辑的值。 */
function displayValue(field: SettingField, draft: Draft): string {
  const pending = draft[field.key]
  if (pending !== undefined) return pending
  return field.secret ? '' : field.value
}

function SourceTag({ field }: { field: SettingField }) {
  if (field.locked) {
    return (
      <Tooltip title="SETTINGS_ENV_LOCK 已开启,这一项由部署时的环境变量说了算">
        <Tag icon={<LockOutlined />}>环境变量锁定</Tag>
      </Tooltip>
    )
  }
  if (field.source === 'db') return <BrandTag tone="accent">{SETTING_SOURCE_LABEL.db}</BrandTag>
  if (field.source === 'env') return <Tag color="default">{SETTING_SOURCE_LABEL.env}</Tag>
  return <Tag color="default">{SETTING_SOURCE_LABEL.default}</Tag>
}

interface RowProps {
  field: SettingField
  draft: Draft
  onChange: (key: string, value: string) => void
  onReset: (key: string) => void
  resetting: boolean
}

function FieldRow({ field, draft, onChange, onReset, resetting }: RowProps) {
  const value = displayValue(field, draft)
  const disabled = field.locked
  const dirty = draft[field.key] !== undefined

  // 下面的 if/else 链是**穷尽的**(最后有 else),所以初始值 null 永远读不到。
  // eslint 10 的 no-useless-assignment 会把它报成 error。不写初始值、只声明类型:
  // TS 的控制流分析认得穷尽赋值,漏掉任何一条分支反而会当场变成类型错误 ——
  // 比一个 `= null` 兜底更安全,那个兜底只会让漏掉的分支静默渲染成空白。
  let control: ReactNode
  if (field.type === 'password') {
    control = (
      <Input.Password
        value={value}
        disabled={disabled}
        autoComplete="new-password"
        placeholder={field.has_value ? `已配置 ${field.value},输入新值可替换` : '未配置'}
        onChange={(e) => onChange(field.key, e.target.value)}
      />
    )
  } else if (field.type === 'number' || field.type === 'integer') {
    // integer 项后端会拒绝带小数的输入,这里把 step 设成 1 并禁掉小数,
    // 让"只能填整数"在填的时候就看得见,而不是点保存之后才被告知
    const integerOnly = field.type === 'integer'
    control = (
      <InputNumber
        value={value === '' ? null : Number(value)}
        disabled={disabled}
        min={field.minimum ?? undefined}
        max={field.maximum ?? undefined}
        step={integerOnly ? 1 : undefined}
        precision={integerOnly ? 0 : undefined}
        style={{ width: '100%' }}
        onChange={(next) => onChange(field.key, next === null ? '' : String(next))}
      />
    )
  } else if (field.type === 'bool') {
    control = (
      <Switch
        checked={value === 'true'}
        disabled={disabled}
        onChange={(checked) => onChange(field.key, checked ? 'true' : 'false')}
      />
    )
  } else if (field.type === 'select') {
    control = (
      <Select
        value={value}
        disabled={disabled}
        style={{ width: '100%' }}
        options={field.options}
        onChange={(next: string) => onChange(field.key, next)}
      />
    )
  } else {
    control = (
      <Input
        value={value}
        disabled={disabled}
        placeholder={field.placeholder}
        onChange={(e) => onChange(field.key, e.target.value)}
      />
    )
  }

  return (
    <div className="settings-row">
      <div className="settings-label">
        <div>
          {field.label}
          {dirty && <span className="settings-dirty" title="尚未保存"> ·</span>}
        </div>
        <div className="mono settings-key">{field.key}</div>
      </div>
      <div className="settings-control">
        {control}
        {field.help && <div className="settings-help">{field.help}</div>}
      </div>
      <div className="settings-meta">
        <SourceTag field={field} />
        {field.source === 'db' && !field.locked && (
          <Button size="small" type="link" loading={resetting} onClick={() => onReset(field.key)}>
            恢复
          </Button>
        )}
      </div>
    </div>
  )
}

export default function SettingsPage() {
  useDocumentTitle('设置')
  const { message } = App.useApp()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Draft>({})
  const [token, setToken] = useState<string>(readAdminToken())
  const [operatorToken, setOperatorToken] = useState<string>(readOperatorToken())

  // 口令不对时重试三次没有任何意义,只会让人多等三个来回
  const query = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.read,
    retry: (count, err) => !isAuthError(err) && count < 2,
  })
  const needsToken = query.isError && isAuthError(query.error)

  /**
   * 改配置是写请求(BLOCK-05)。它能切 Provider、能改 API Key、能改预算 ——
   * 也就是能让下一批生成花在另一家、或者花超。超时后"这次改动到底进没进去"
   * 必须先看一眼:配置页显示的就是后端当前的真实值。
   *
   * `test` 不在其中:它是一次探活,重复调用没有业务后果。
   */
  const refreshSettings = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ['settings'] })
    queryClient.invalidateQueries({ queryKey: ['providers'] })
  }, [queryClient])

  const onWriteError = useWriteError(refreshSettings)

  const save = useMutation({
    mutationFn: () => settingsApi.update(draft),
    onSuccess: (result) => {
      message.success(result.message)
      setDraft({})
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: onWriteError,
  })

  /*
   * A45-#20:同 #19 —— `reset.isPending` 是整个 mutation 的状态,而每一行都有
   * 一个"恢复"按钮。点一行会让整页的恢复按钮一起转圈,运营看到的是
   * "我好像把所有配置都恢复了"。这一页尤其不该给人这种错觉。
   */
  const [resettingKey, setResettingKey] = useState<string | null>(null)

  const reset = useMutation({
    mutationFn: (key: string) => {
      setResettingKey(key)
      return settingsApi.reset([key])
    },
    onSuccess: (result) => {
      message.success(result.message)
      queryClient.invalidateQueries({ queryKey: ['settings'] })
      queryClient.invalidateQueries({ queryKey: ['providers'] })
    },
    onError: onWriteError,
    onSettled: () => setResettingKey(null),
  })

  const test = useMutation({
    mutationFn: providersApi.test,
    onSuccess: (result) => {
      const notice = result.configured ? message.info : message.warning
      const model = result.tryon_model ? `(模型 ${result.tryon_model})` : ''
      notice(`${result.provider}:${result.message}${model}`)
    },
    onError: (err) => message.error(readError(err)),
  })

  const dirtyCount = useMemo(() => Object.keys(draft).length, [draft])

  const handleChange = (key: string, value: string) => {
    setDraft((prev) => ({ ...prev, [key]: value }))
  }

  const handleReset = (key: string) => {
    setDraft((prev) => {
      const next = { ...prev }
      delete next[key]
      return next
    })
    reset.mutate(key)
  }

  const saveToken = () => {
    // 写入可能只落在内存里(隐私模式 / 存储被禁用 / 配额满)。
    // 那种情况下说"已记住"是假话:刷新一次就没了,而用户会以为配好了
    //
    // **两个都要先算出来再合并,不能写成 `a() && b()`。** `&&` 会短路:
    // 管理口令持久化失败时 `writeOperatorToken` 根本不会被调用,于是运营口令
    // 连内存都没进 —— 提示说"仅本次会话有效",而实际上这次会话也没有。
    // 隐私模式下两次写必然同时失败,所以这个坑一旦踩到就是必现的。
    const adminPersisted = writeAdminToken(token.trim())
    const operatorPersisted = writeOperatorToken(operatorToken.trim())
    const persisted = adminPersisted && operatorPersisted
    const hasToken = Boolean(token.trim() || operatorToken.trim())
    if (!hasToken) message.success('口令已清除')
    else if (persisted) message.success('口令已记住')
    else message.warning('口令仅在本次会话有效:浏览器不允许写入本地存储,刷新后需重新填写')

    // 读接口也要口令,所以存完立刻重拉一次:填对了页面当场就出来
    queryClient.invalidateQueries({ queryKey: ['settings'] })
    // 身份探针也必须失效。它 staleTime 60 秒且不在窗口聚焦时重取 ——
    // 不主动失效的话,operator 换成 admin 之后管理菜单要过一分钟才出来,
    // 反过来 admin 降成 operator 时管理菜单还亮着(后端仍会拦,
    // 但界面在说一件不成立的事)
    queryClient.invalidateQueries({ queryKey: ['auth-probe'] })
  }

  const renderGroup = (group: SettingGroup) => {
    const basic = group.fields.filter((f) => !f.advanced)
    const advanced = group.fields.filter((f) => f.advanced)
    const provider = SETTING_GROUP_PROVIDER[group.key]

    return (
      <Card
        key={group.key}
        size="small"
        title={
          <Space size={8}>
            <span>{group.title}</span>
            {group.badge && <Tag>{group.badge}</Tag>}
          </Space>
        }
        extra={
          provider ? (
            <Button size="small" loading={test.isPending} onClick={() => test.mutate(provider)}>
              测试连接
            </Button>
          ) : null
        }
      >
        {group.description && (
          <Typography.Paragraph type="secondary" style={{ marginBottom: 12, fontSize: fontScale.body }}>
            {group.description}
          </Typography.Paragraph>
        )}

        {basic.map((field) => (
          <FieldRow
            key={field.key}
            field={field}
            draft={draft}
            onChange={handleChange}
            onReset={handleReset}
            resetting={reset.isPending && resettingKey === field.key}
          />
        ))}

        {advanced.length > 0 && (
          <Collapse
            ghost
            size="small"
            items={[
              {
                key: 'advanced',
                label: `高级选项(${advanced.length})`,
                children: advanced.map((field) => (
                  <FieldRow
                    key={field.key}
                    field={field}
                    draft={draft}
                    onChange={handleChange}
                    onReset={handleReset}
                    resetting={reset.isPending && resettingKey === field.key}
                  />
                )),
              },
            ]}
          />
        )}
      </Card>
    )
  }

  /** A11:改过的设置项都攒在 draft 里,一个键就算有未保存内容 */
  const dirty = Object.keys(draft).length > 0

  return (
    <Space direction="vertical" size={12} style={{ width: '100%', maxWidth: 940 }}>
      <UnsavedGuard dirty={dirty} what="设置" />
      <PageHeader title="设置" subtitle="口令、密钥、模型与白名单。改一次就不常动的东西" />

      {needsToken && (
        <Alert
          type="warning"
          showIcon
          message="需要口令才能查看配置"
          description={`${readError(query.error)}。在下方填入后端 ADMIN_TOKEN 里的口令并点"记住"。`}
        />
      )}

      {query.isError && !needsToken && (
        <Alert type="error" showIcon message="读取配置失败" description={readError(query.error)} />
      )}

      {query.data && !query.data.encryption.available && (
        <Alert
          type="warning"
          showIcon
          message="密钥暂时无法在后台保存"
          description={`${query.data.encryption.message}。在此之前请把密钥写进 .env 后重启后端。`}
        />
      )}

      {query.data?.env_lock && (
        <Alert
          type="info"
          showIcon
          message="环境变量锁定已开启"
          description="凡是 .env 里给过值的项都只能看不能改。要在网页上改,请把 SETTINGS_ENV_LOCK 设为 false 后重启后端。"
        />
      )}

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space direction="vertical" size={8} style={{ width: '100%' }}>
          <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
            后端读写接口都需要身份。两把口令分工不同,建议都填:
          </Typography.Text>
          <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
            <span className="mono">OPERATOR_TOKENS</span>
            —— 日常使用。看列表、传商品、建任务、审图都要它,
            <b>每个请求都会带上</b>。
          </Typography.Text>
          <Space.Compact style={{ width: 420, maxWidth: '100%' }}>
            <Input.Password
              value={operatorToken}
              placeholder="操作口令,日常浏览和业务操作用"
              autoComplete="off"
              onChange={(e) => setOperatorToken(e.target.value)}
            />
          </Space.Compact>
          <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
            <span className="mono">ADMIN_TOKEN</span>
            —— 改配置、改提示词、测 Provider 连接。它能改 API Key、能烧光额度,
            所以<b>只跟随这几个页面的请求发出</b>,拉列表时不会捎上。
          </Typography.Text>
          {/*
            评审 P2-3:这里原本写着「只配了它也能用:admin 天然包含 operator」。
            那句话是对的,但它引导人**只填管理口令** —— 而一旦如此,
            `client.ts` 拦截器的回落就会生效,于是每一次拉列表都会带上
            那把能改 API Key 的口令。拦截器注释里刻意要防的正是这件事。
            事实留下(不填 operator 也能用,不然新人会以为自己被卡住),
            但把代价一起说出来,并且只在真的只填了一把时才提醒。
          */}
          {!operatorToken.trim() && Boolean(token.trim()) && (
            <Typography.Text style={{ fontSize: fontScale.body, color: brandVars.dangerDeep }}>
              只填管理口令也能用(admin 天然包含 operator),但那样<b>每一次拉列表都会带上这把口令</b>。
              上面那一栏一起填,日常请求就只用得到操作口令。
            </Typography.Text>
          )}
          <Space.Compact style={{ width: 420, maxWidth: '100%' }}>
            <Input.Password
              value={token}
              placeholder="管理口令,改配置时用"
              autoComplete="off"
              onChange={(e) => setToken(e.target.value)}
            />
            <Button onClick={saveToken}>记住</Button>
          </Space.Compact>
          <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
            本机开发(APP_ENV=local/dev 且两项都没配)可以留空。
            口令只存在这台浏览器上。
          </Typography.Text>
        </Space>
      </Card>

      {query.isLoading && <Skeleton active />}

      {query.data?.groups.map(renderGroup)}

      {query.data && (
        <Card size="small" styles={{ body: { padding: 12 } }}>
          <Space size={12} wrap>
            <Button
              type="primary"
              disabled={dirtyCount === 0}
              loading={save.isPending}
              onClick={() => save.mutate()}
            >
              保存 {dirtyCount > 0 ? `(${dirtyCount} 项)` : ''}
            </Button>
            <Button disabled={dirtyCount === 0} onClick={() => setDraft({})}>
              放弃改动
            </Button>
            <Typography.Text type="secondary" style={{ fontSize: fontScale.body }}>
              保存后台立即生效,生成 worker 最迟十几秒内跟上,不需要重启。
            </Typography.Text>
          </Space>
        </Card>
      )}
    </Space>
  )
}
