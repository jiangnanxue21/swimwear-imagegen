/**
 * CSV 导入最小版(FE-305)。
 *
 * 验收结果:**错误行不影响正确行**。这一页把这件事说在落库**之前** ——
 * 预览先告出"47 行会新增、2 行已存在跳过、1 行有错",运营看着这三个数字
 * 再决定要不要按"确认导入"。
 *
 * 错误文件是这一页的第二个要点。只弹一句"第 37 行有错"等于让运营自己去数到
 * 第 37 行,而 Excel 的行号和 CSV 的行号在有多行单元格时还不一样。所以后端
 * 把出错的行连同**原始内容**一起吐回来,诊断列放在最后 ——
 * 改完可以直接重新上传,不用先删两列。
 */
import { useState } from 'react'
import {
  Alert, App, Button, Card, Descriptions, Empty, Space, Statistic, Steps, Table, Tag,
  Tooltip, Upload,
} from 'antd'
import type { ColumnsType } from 'antd/es/table'
import type { UploadFile } from 'antd/es/upload/interface'
import { DownloadOutlined, InboxOutlined, UploadOutlined } from '@ant-design/icons'
import { useMutation, useQuery } from '@tanstack/react-query'
import { readError } from '../api/client'
import { noRefresh, useWriteError } from '../hooks/useWriteError'
import {
  IMPORT_ROW_PLAN_LABEL,
  batchApi,
  saveBlob,
  type ImportPreviewResponse,
  type ImportPreviewRow,
} from '../api/batch'
import { brandVars, fontScale } from '../theme'
import ErrorNotice from '../components/ErrorNotice'
import PageHeader from '../components/PageHeader'
import { useDocumentTitle } from '../hooks/useDocumentTitle'

export default function WorkbenchImportPage() {
  /*
   * A45-#34:走 `App.useApp()`,不用静态 `message`。
   *
   * 静态实例拿不到 `ConfigProvider` 的上下文 —— 主题、语言、以及 antd v5
   * 的 CSS-in-JS 样式注入都不生效,表现是这一页的提示框和别处长得不一样,
   * 而且在 React 18 严格模式下会有一条控制台警告。
   *
   * 全仓其余地方都用 `useApp()`(`BatchActionBar` 里有注释说明原因),
   * 这里是唯一一处例外 —— 唯一的例外就是漂移的起点。
   */
  const { message } = App.useApp()
  useDocumentTitle('商品导入')
  const [file, setFile] = useState<File | null>(null)
  const [preview, setPreview] = useState<ImportPreviewResponse | null>(null)
  const [committed, setCommitted] = useState<ImportPreviewResponse | null>(null)

  const columns = useQuery({
    queryKey: ['import-columns'],
    queryFn: () => batchApi.importColumns(),
  })

  const template = useMutation({
    mutationFn: () => batchApi.importTemplate(),
    onSuccess: ({ blob, filename }) => saveBlob(blob, filename),
    onError: (err) => message.error(readError(err)),
  })

  const runPreview = useMutation({
    mutationFn: (target: File) => batchApi.importPreview(target),
    onSuccess: (data) => {
      setPreview(data)
      setCommitted(null)
    },
    onError: (err) => message.error(readError(err)),
  })

  /**
   * 落库是写请求(BLOCK-05),而且是这一页唯一改变业务状态的那一个。
   *
   * 模板下载与预览都只读,继续用 `readError`:重下一次模板、重跑一次预览
   * 都没有代价,而对它们喊"结果未知"属于狼来了。
   *
   * 提交超时不能说"请重试":后端可能已经建了那一批商品,重来一次会撞上
   * SKU 唯一约束、把一次成功导入报成一屏错误。这里没有可刷新的列表
   * (导入结果只存在于本次响应里),所以让运营去商品页核对 —— 显式 `noRefresh`
   */
  const onCommitError = useWriteError(noRefresh)

  const commit = useMutation({
    mutationFn: (target: File) => batchApi.importCommit(target, preview?.preview_token ?? ''),
    onSuccess: (data) => {
      setCommitted(data)
      setPreview(data)
      if (data.committed) {
        message.success(
          `导入完成:新增 ${data.created ?? 0}、跳过 ${data.skipped_existing ?? 0}、` +
            `错误 ${data.failed ?? 0}`,
        )
      }
    },
    onError: onCommitError,
  })

  const pick = (next: File) => {
    setFile(next)
    setPreview(null)
    setCommitted(null)
    runPreview.mutate(next)
  }

  const rowColumns: ColumnsType<ImportPreviewRow> = [
    {
      title: '行号',
      dataIndex: 'row_number',
      key: 'row_number',
      width: 70,
      render: (value: number) => (
        <Tooltip title="表头是第 1 行,与 Excel 左侧行号对齐">
          <span className="mono">{value || '—'}</span>
        </Tooltip>
      ),
    },
    {
      title: '处理',
      key: 'plan',
      width: 120,
      render: (_, row) => {
        const meta = IMPORT_ROW_PLAN_LABEL[row.plan]
        return <Tag color={meta?.color}>{meta?.text ?? row.plan_label}</Tag>
      },
    },
    { title: 'SKU', dataIndex: 'sku', key: 'sku', width: 200, className: 'mono' },
    { title: 'SPU', dataIndex: 'spu', key: 'spu', width: 160, className: 'mono' },
    { title: '商品名', dataIndex: 'name', key: 'name' },
    {
      title: '问题',
      key: 'problems',
      render: (_, row) =>
        row.problems.length ? (
          <Space direction="vertical" size={0}>
            {row.problems.map((p, index) => (
              <span key={index} style={{ fontSize: fontScale.body }}>
                <Tag color="error">{p.field}</Tag>
                {p.message}
              </span>
            ))}
          </Space>
        ) : (
          <span style={{ color: brandVars.textFaint }}>—</span>
        ),
    },
  ]

  const counts = preview?.counts
  const step = committed ? 2 : preview ? 1 : 0

  return (
    <Space direction="vertical" size={12} style={{ width: '100%' }}>
      <PageHeader title="商品导入" subtitle="先看逐行计划,确认无误再落库" />

      <Steps
        size="small"
        current={step}
        items={[
          { title: '下模板 / 选文件' },
          { title: '预览' },
          { title: '确认导入' },
        ]}
      />

      <Card size="small" styles={{ body: { padding: 12 } }}>
        <Space direction="vertical" size={10} style={{ width: '100%' }}>
          <Space wrap>
            <Button
              icon={<DownloadOutlined />}
              loading={template.isPending}
              onClick={() => template.mutate()}
            >
              下载固定模板
            </Button>
            <Upload
              accept=".csv"
              maxCount={1}
              showUploadList={false}
              beforeUpload={(f) => {
                pick(f as unknown as File)
                return false // 不走 antd 自己的上传,预览要带口令请求头
              }}
              fileList={file ? ([{ uid: '1', name: file.name }] as UploadFile[]) : []}
            >
              <Button type="primary" icon={<UploadOutlined />} loading={runPreview.isPending}>
                选择 CSV 并预览
              </Button>
            </Upload>
            {file && <Tag>{file.name}</Tag>}
          </Space>

          <Alert
            type="info"
            showIcon
            message="模板里只有机器列名,没有中文表头"
            description="加一行中文说明会被解析器当成数据行然后报错——一份下载下来就导不进去的模板比没有模板更糟。中文列名与可选值见下面的对照表。Excel 另存时请选「CSV UTF-8」。"
          />

          {/*
            * 列说明拉不到时要说出来(FE-IMPORT-01)。原来是 `columns.data &&`,
            * 失败时整张对照表**连同标题一起消失** —— 页面上方那句"中文列名与
            * 可选值见下面的对照表"于是指向一片空白,运营会以为自己看漏了
            */}
          {columns.isError && (
            <ErrorNotice
              title="拉不到列名对照表"
              error={columns.error}
              onRetry={() => columns.refetch()}
              retrying={columns.isFetching}
            />
          )}
          {columns.data && (
            <Descriptions size="small" bordered column={2}>
              {columns.data.map((column) => (
                <Descriptions.Item
                  key={column.name}
                  label={
                    <span>
                      <span className="mono">{column.name}</span>
                      {column.required && (
                        <Tag color="error" style={{ marginInlineStart: 6 }}>
                          必填
                        </Tag>
                      )}
                    </span>
                  }
                >
                  <div>{column.label}</div>
                  {column.enum && (
                    <div style={{ color: brandVars.slate, fontSize: fontScale.meta }}>
                      可选:{column.enum.join(' / ')}
                    </div>
                  )}
                </Descriptions.Item>
              ))}
            </Descriptions>
          )}
        </Space>
      </Card>

      {preview?.blocked && (
        <Alert
          type="error"
          showIcon
          message="整份文件被拒:表头不对"
          description={
            <Space direction="vertical" size={2}>
              {preview.header_problems.map((p, index) => (
                <span key={index}>
                  {p.field}:{p.message}
                </span>
              ))}
              <span>请用「下载固定模板」拿到正确的表头。</span>
            </Space>
          }
        />
      )}

      {preview && !preview.blocked && counts && (
        <>
          <Card size="small">
            <Space size={28} wrap>
              <Statistic title="总行数" value={counts.total} valueStyle={{ fontSize: fontScale.metric }} />
              <Statistic
                title="会新增"
                value={counts.will_create}
                valueStyle={{ fontSize: fontScale.metric, color: brandVars.success }}
              />
              <Tooltip title="已存在的 SKU 记为跳过而不是错误 —— 增量补充是批量导入的常态,重复执行必须安全">
                <Statistic
                  title="已存在,跳过"
                  value={counts.will_skip}
                  valueStyle={{ fontSize: fontScale.metric }}
                />
              </Tooltip>
              <Tooltip title="错误行不影响正确行:其余行照常导入">
                <Statistic
                  title="有错的行"
                  value={counts.error_rows}
                  valueStyle={{ fontSize: fontScale.metric, color: counts.error_rows ? brandVars.danger : undefined }}
                />
              </Tooltip>
              <Space direction="vertical" size={6}>
                <Button
                  type="primary"
                  disabled={!file || !preview.preview_token || counts.will_create === 0}
                  loading={commit.isPending}
                  onClick={() => file && commit.mutate(file)}
                >
                  确认导入 {counts.will_create} 行
                </Button>
                {preview.error_csv && (
                  <Button
                    size="small"
                    icon={<DownloadOutlined />}
                    onClick={() =>
                      saveBlob(
                        new Blob([`\ufeff${preview.error_csv}`], {
                          type: 'text/csv;charset=utf-8',
                        }),
                        'import-errors.csv',
                      )
                    }
                  >
                    下载错误行文件
                  </Button>
                )}
              </Space>
            </Space>
          </Card>

          {preview.unknown_columns.length > 0 && (
            <Alert
              type="warning"
              showIcon
              message={`文件里有 ${preview.unknown_columns.length} 个模板不认识的列,它们会被忽略`}
              description={preview.unknown_columns.join('、')}
            />
          )}

          {committed?.committed && (
            <Alert
              type="success"
              showIcon
              message={`已导入:新增 ${committed.created ?? 0} 件,跳过 ${
                committed.skipped_existing ?? 0
              } 件,错误 ${committed.failed ?? 0} 行`}
              description="错误行没有进库,其余行不受影响。改完错误行文件可以直接重新上传。"
            />
          )}

          <Table<ImportPreviewRow>
            rowKey="row_number"
            size="small"
            bordered
            columns={rowColumns}
            dataSource={preview.rows}
            pagination={{ pageSize: 20, showSizeChanger: false }}
            rowClassName={(row) => (row.plan === 'ERROR' ? 'row-error' : '')}
            locale={{ emptyText: <Empty description="文件里没有数据行" /> }}
          />
        </>
      )}

      {!preview && !runPreview.isPending && (
        <Card size="small">
          <Empty
            // 这个 36 是**图标尺寸**,不是字号 —— 图标用 fontSize 定大小是 antd 的
              // 约定,它不属于 fontScale 那张排版表,所以刻意不收进令牌
              image={<InboxOutlined style={{ fontSize: 36, color: brandVars.sand }} />}
            description="先下载模板填好,再选文件预览。预览不写库,可以反复试。"
          />
        </Card>
      )}
    </Space>
  )
}
