import { apiClient } from './client'

/** 一个会影响产出真实性的环节。档位与文案都由后端给,前端不推测。 */
export interface EnvironmentFacet {
  key: string
  label: string
  /** REAL / SIMULATED / UNCONFIGURED / UNAVAILABLE。判定在后端 `core/environment.py` */
  fidelity: string
  /** 生效的那个后端叫什么。运营截图时这一格就是证据 */
  backend: string
  /** 一句给人看的话。**说后果,不说状态名** */
  detail: string
}

/**
 * 一个渠道的接入事实。**每一格都由后端算,前端只展示**(硬规则 4)。
 *
 * 这张表与上面的 `facets` 回答的不是同一个问题:facets 答"这一档整体可不可信"
 * (只能有一个档位),这里答"某个渠道现在到底接没接"。`GENERIC=SIMULATOR` 与
 * `SHEIN=BLOCKED` 并存时,压成一个值必然要丢掉一半。
 */
export interface ChannelRow {
  channel: string
  /** 挂着的发送端叫什么。没有发送端时是 null */
  transport: string | null
  /** SIMULATOR / REAL / NONE。三档而不是布尔 —— 见后端 `channels/registry.py` */
  transport_kind: string
  is_simulator: boolean
  /** spec 里还有没有 TODO。null = 读不出来(与 false 是两个不同的运维动作) */
  spec_complete: boolean | null
  /**
   * BLOCKED / FIXTURE_ONLY / REAL。只有接了解锁闸的渠道有这一格。
   * 判定在后端 `channels/shein/readiness.py`,前端不重算。
   */
  mode?: string
  /** 升不上去的逐条理由。后端给什么显示什么,**不在前端筛选或改写** */
  blocking_reasons?: string[]
  /** 还没被逐页核对过的官方来源条数 */
  sources_unverified?: number
  /** 页面已知不可复核的来源编号 */
  sources_stale?: string[]
}

export interface EnvironmentStatus {
  fidelity: string
  /** 产出能不能当真。只有四档里的 REAL 算 —— 判据在后端,不要在前端重算 */
  trustworthy: boolean
  facets: EnvironmentFacet[]
  /**
   * 运行形态。影响可靠性,**不影响产出真实性**,所以不参与上面的判定。
   * 理由见后端 `core/environment.py` 的模块注释。
   */
  deployment: {
    batch_execution_mode: string
    storage_backend: string
  }
  /** 每个渠道各一行,**不压成一个值**(后端 PRD §8.1 SH-CFG-005) */
  channels: ChannelRow[]
}

export const environmentApi = {
  read: async (): Promise<EnvironmentStatus> =>
    (await apiClient.get<EnvironmentStatus>('/environment')).data,
}
