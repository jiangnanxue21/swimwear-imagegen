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
}

export const environmentApi = {
  read: async (): Promise<EnvironmentStatus> =>
    (await apiClient.get<EnvironmentStatus>('/environment')).data,
}
