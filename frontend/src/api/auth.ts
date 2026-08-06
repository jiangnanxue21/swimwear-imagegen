/**
 * 身份自查(A5)。
 *
 * `/health` 回答"后端起来了没",这个接口回答"它认不认我这把口令"。
 * 两件事必须分开问,否则冷启动横幅只能靠"localStorage 里有没有字符串"
 * 来猜口令好不好 —— 而复制口令时多带一个空格,猜出来的结论是"已配置"。
 *
 * 它走 apiClient,所以请求拦截器会照常带上口令,响应拦截器会照常在 401 时
 * 点亮横幅:这一层不需要自己判断"口令好不好",判断只有 client.ts 那一份。
 */
import { apiClient } from './client'

export interface WhoAmI {
  /** 会写进审计日志的操作者名 */
  name: string
  /** admin / operator / dev */
  role: string
  /** 菜单按角色收敛用(A8)。权限边界仍在后端 require_admin */
  is_admin: boolean
}

export const authApi = {
  whoami: async (): Promise<WhoAmI> =>
    (await apiClient.get<WhoAmI>('/auth/whoami', { timeout: 5_000 })).data,
}
