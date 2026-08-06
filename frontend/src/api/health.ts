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
}

export const healthApi = {
  ping: async (): Promise<HealthOut> =>
    (await apiClient.get<HealthOut>('/health', { timeout: 5_000 })).data,
}
