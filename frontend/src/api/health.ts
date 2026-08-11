/**
 * 存活探针(OPS-REVIEW P5:冷启动引导)。
 *
 * 前端要区分三件长得很像、处理方式完全不同的事:
 *
 *     后端没起来      检查 docker compose,填口令没用
 *     口令没填        到设置页填,后端是好的
 *     口令填错了      到设置页改,后端也是好的
 *
 * 第一件只能靠一个**不需要口令**的接口来判断,否则"连不上"和"没口令"
 * 会永远混在一起 —— 两者的报错都是一片红,而新人第一天遇到的通常是后者。
 *
 * `/health` 正是那个接口:它在后端 `main.PUBLIC_PREFIXES` 白名单里(编排系统
 * 探活不带口令),而且刻意不碰数据库(见 `api/health.py`)。这一层依赖关系
 * 由 `test_frontend_contract` 钉住 —— 哪天有人把 /health 挪出白名单,
 * 这个探测会变成 401,横幅就会开始撒谎。
 */
import { apiClient } from './client'

export interface HealthOut {
  status: string
  app: string
  env: string
  /**
   * 这个部署认哪种凭据。**未登录时它是前端唯一的信息来源** ——
   * 那一刻手上只有一个 401,而两种模式的 401 长得一模一样。
   *
   *     session   浏览器登录。401 = 登录失效,把人送到 /login
   *     token     Legacy Header Token。401 = 口令没填或填错,指向 /settings
   *
   * 可选是因为旧后端不返回这个字段。缺席时按 `token` 处理(见 client.ts):
   * 会话模式下最坏是多说一句设置页,而反过来会把口令模式的人送进一个
   * 永远登不进去的登录页。
   */
  auth_mode?: 'session' | 'token'
}

export const healthApi = {
  ping: async (): Promise<HealthOut> =>
    (await apiClient.get<HealthOut>('/health', { timeout: 5_000 })).data,
}
