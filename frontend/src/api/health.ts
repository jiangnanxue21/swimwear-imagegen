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
   * 那一刻手上只有一个 401,而三种模式的 401 长得一模一样。
   *
   *     session   浏览器登录。401 = 登录失效,把人送到 /login
   *     dev       本机免口令模式。没有登录这个动作;401 = 后端配置刚变过
   *     token     只配了 Legacy Header Token。**浏览器进不来** ——
   *               它不发 Header 口令,界面上也没有填口令的地方
   *
   * `dev` 是 2026-08-11 评审补的第三档。在它之前 `dev` 与 `token` 合报成
   * `token`,于是"本机免登录、一切正常"和"浏览器彻底进不来"在前端是
   * **同一个值** —— 而后者需要说的那句话完全说不出来,登录页只会指着
   * 一个已经被删掉的口令输入框。
   *
   * 可选是因为旧后端不返回这个字段。缺席时按 `token` 处理(见 client.ts):
   * 那是保守方向 —— 多解释一句总比把人送进一个登不进去的登录页好。
   */
  auth_mode?: 'session' | 'dev' | 'token'
}

export const healthApi = {
  ping: async (): Promise<HealthOut> =>
    (await apiClient.get<HealthOut>('/health', { timeout: 5_000 })).data,
}
