/**
 * 环境真实性状态条(REVIEW 任务 6 / 4.1 节 F)。
 *
 * ## 它要解决的事
 *
 * 运营在界面上看到一张图、一个属性值、一条上架回执,没有任何地方告诉他
 * 这些是真的调了外部服务得来的,还是本地假数据。而这两者**在界面上长得
 * 一模一样**:Mock 出图是真的图片文件,Mock 属性是合理的中文取值,
 * Simulator 上架会返回一个像模像样的平台单号。
 *
 * 报告 §10 人工测试准入第 4 条就是这件事:「Mock/真实可见 —— 界面截图;
 * 切到 Mock 后状态条改变」。
 *
 * ## 判定不在这里
 *
 * 档位、哪一档算可信、每一档说什么话,全部来自 `/api/environment`
 * (判定在后端 `core/environment.py`,穷举测试盯着)。这里只决定**长什么样**。
 * 硬规则 4:前端不推测状态。
 *
 * ## 为什么"真实"时反而不显示
 *
 * 一条常驻的「一切正常」横幅出现三次之后就没人再读横幅了 —— 包括它
 * 真的变成警告的那一次。所以 REAL 档静默,只有产出不能当真时才占地方。
 */
import { Alert, Space, Tag, Typography } from 'antd'
import { useQuery } from '@tanstack/react-query'
import { environmentApi, type EnvironmentFacet } from '../api/environment'
import { fontScale, space } from '../theme'

/** 每一档的呈现。文案来自后端,这里只管颜色和标题。 */
const TONE: Record<string, { type: 'warning' | 'error'; title: string }> = {
  SIMULATED: {
    type: 'error',
    title: '当前是模拟环境,界面上的产出不能当真',
  },
  UNAVAILABLE: {
    type: 'warning',
    title: '有环节还没实现,链路跑不通',
  },
  UNCONFIGURED: {
    type: 'warning',
    title: '有环节没配好,会在运行时报错',
  },
}

export default function EnvironmentBanner() {
  const query = useQuery({
    queryKey: ['environment'],
    queryFn: environmentApi.read,
    /*
     * 五分钟一次。环境是靠改配置变的,不是靠跑批变的 —— 更勤只会给一个
     * 聚合了四张注册表的接口加压。切了 Provider 之后最迟五分钟状态条跟上,
     * 而设置页保存后会主动 invalidate 这个 key,所以那条路径是即时的。
     */
    refetchInterval: 5 * 60_000,
  })

  /*
   * 拉不到时**说出来**,不静默(a28 FE-GLOBAL-03)。
   *
   * 这一条和 SpendAlertBanner 那条豁免不一样:预算横幅缺席只是少一条提醒,
   * 而这一条缺席意味着"没人告诉你这些数据是假的" —— 沉默在这里等于
   * 默认回答了"是真的",而那正是这个组件存在的理由。
   */
  if (query.isError) {
    return (
      <Alert
        type="warning"
        showIcon
        style={{ marginBottom: space.lg }}
        message="拉不到环境状态"
        description="现在无法确认界面上的产出是真实的还是模拟的。在确认之前,不要把这里的结果当成真实数据。"
      />
    )
  }

  const data = query.data
  if (!data || data.trustworthy) return null

  const tone = TONE[data.fidelity] ?? TONE.UNCONFIGURED

  return (
    <Alert
      type={tone.type}
      showIcon
      style={{ marginBottom: space.lg }}
      message={tone.title}
      description={
        <Space direction="vertical" size={4} style={{ width: '100%' }}>
          {data.facets
            .filter((f: EnvironmentFacet) => f.fidelity !== 'REAL')
            .map((f: EnvironmentFacet) => (
              <Typography.Text key={f.key} style={{ fontSize: fontScale.body }}>
                <Tag>{f.label}</Tag>
                <span className="mono">{f.backend}</span> —— {f.detail}
              </Typography.Text>
            ))}
        </Space>
      }
    />
  )
}
