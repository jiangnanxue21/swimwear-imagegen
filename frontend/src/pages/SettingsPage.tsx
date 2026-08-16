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
import { isAuthError, readError } from '../api/client'
import { SETTING_GROUP_PROVIDER, SETTING_SOURCE_LABEL } from '../api/types'
import type { SettingField, SettingGroup } from '../api/types'
import UnsavedGuard from '../components/UnsavedGuard'
import BrandTag from '../components/BrandTag'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'
import { useIdentity } from '../hooks/useIdentity'
import { fontScale } from '../theme'

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
  // 服务下面的 403 早退:operator 手输地址进来,在发出任何请求之前就把话
  // 说清楚。判定不在这里做 —— isAdmin 的真相来源是后端 `/auth/whoami`,
  // 这一页只是读它。(这行注释原来说"只为了那句『这两把不是你的登录密码』",
  // 那句 Alert 随口令录入卡一起删了,identity 的用途跟着换了)
  const identity = useIdentity()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<Draft>({})

  /**
   * 已经知道这不是管理员时**根本不发这个请求**。
   *
   * 2026-08-11:上面那句注释写着"在发出任何请求之前就把话说清楚",而它不成立 ——
   * hook 不能条件调用,`useQuery` 在下面那个 403 早退分支之前就已经执行了。
   * 于是 operator 手输 `/settings` 的动线是:界面正确地显示"没有权限",
   * 后端同时收到一个注定 403 的 `GET /settings`。
   *
   * 不是安全漏洞(边界在 `require_admin`,它照常拒),但那次请求毫无意义:
   * 它在后端日志里留一条 403、在前端留一次失败查询,而两者都只会让排障的人
   * 去查一个不存在的权限问题。判据用 `identity.who &&`:身份还没探出来时
   * **要发**,否则管理员每次刷新都要多等一个 whoami 往返才开始读配置。
   */
  const forbiddenByRole = Boolean(identity.who) && !identity.isAdmin

  // 口令不对时重试三次没有任何意义,只会让人多等三个来回
  const query = useQuery({
    queryKey: ['settings'],
    queryFn: settingsApi.read,
    enabled: !forbiddenByRole,
    retry: (count, err) => !isAuthError(err) && count < 2,
  })
  /*
   * 读不到配置,而且是身份问题。本轮之后这只有一种解释:**当前账号不是管理员**。
   *
   * 口令时代它还有第二种解释("口令没填 / 填错了"),那时这一支引导人去下面那张
   * 录入卡填口令。卡片随 localStorage 口令链一起删掉了(PRD §28),
   * 所以这里的话也必须跟着改 —— 指着一个不存在的输入框比不说更糟。
   */
  const forbidden = query.isError && isAuthError(query.error)

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

  /*
   * operator 手输 `/settings`(PRD §31)。路由仍然注册,所以这一页会被渲染 ——
   * 这里在**发请求之前**就把话说清楚,而不是让他看着一页空白等一个 403 回来。
   *
   * 「发请求之前」这半句现在是真的:同一个判据(`forbiddenByRole`)同时关掉了
   * 上面那次 `useQuery`。原来它只关掉渲染,请求照发 —— 见那里的说明。
   *
   * 判据取 `identity.who && !identity.isAdmin`,不是 `!identity.isAdmin`:
   * 后者在身份还没探出来时也为真,于是管理员每次刷新都会先闪一下 403。
   *
   * 这**不是**权限边界:边界是后端 `require_admin`,下面那个 `forbidden`
   * 分支接的就是它的回答。两处都要有 —— 少了后端那半是漏洞,少了这半是难用。
   */
  if (forbiddenByRole) {
    return (
      <>
        <PageHeader title="设置" subtitle="密钥、模型、服务商与系统参数" />
        <Alert
          type="error"
          showIcon
          message="当前账号没有管理员权限"
          description="设置页只对管理员账号开放。要改服务商、密钥或系统参数,请用管理员账号登录 —— 侧栏里没有「系统管理」这一组入口也是同一个原因。"
        />
      </>
    )
  }

  return (
    <Space direction="vertical" size={12} style={{ width: '100%', maxWidth: 940 }}>
      <UnsavedGuard dirty={dirty} what="设置" />
      <PageHeader title="设置" subtitle="密钥、模型、服务商与系统参数" />

      {forbidden && (
        <Alert
          type="error"
          showIcon
          style={{ marginBottom: 12 }}
          message="当前账号没有管理员权限"
          description={`${readError(query.error)}。设置页需要用管理员(admin)账号登录;运营(operator)看不到这一页的内容。`}
        />
      )}
      {query.isError && !forbidden && (
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
              保存后台立即生效,出图的后台执行进程最迟十几秒内跟上,不需要重启。
            </Typography.Text>
          </Space>
        </Card>
      )}
    </Space>
  )
}
