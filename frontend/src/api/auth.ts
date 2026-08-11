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

/** `/auth/login` 的入参。**没有 `role`** —— 角色由服务端在校验通过后写进 Session */
export interface LoginRequest {
  username: string
  password: string
}

export const authApi = {
  whoami: async (): Promise<WhoAmI> =>
    (await apiClient.get<WhoAmI>('/auth/whoami', { timeout: 5_000 })).data,

  /**
   * 用户名 + 密码换一个 HttpOnly Session Cookie。
   *
   * **返回的形状和 `whoami` 完全一致**(后端 `_identity_payload` 是同一个函数),
   * 所以登录页可以直接 `queryClient.setQueryData(['auth-probe'], data)`,
   * 不必再补一次探测。两处形状一旦分叉,那次 setQueryData 写进去的会是一个
   * 不同构的对象 —— 而它**不会报错**,只会让菜单在登录后的第一秒按错误的
   * `is_admin` 渲染一次。后端那边的注释记着同一件事,两边一起改。
   *
   * 超时给 15 秒而不是默认 60:登录是人在等,一个转一分钟的登录按钮
   * 会让他以为自己点漏了,然后再点一次。
   */
  login: async (payload: LoginRequest): Promise<WhoAmI> =>
    (await apiClient.post<WhoAmI>('/auth/login', payload, { timeout: 15_000 })).data,

  /**
   * 退出登录。**幂等**,后端没登录时也回 200。
   *
   * 所以调用方不必先判断"当前有没有登录态"就能调它 —— 那个判断只会多一处
   * 可能与后端不一致的地方,而这个动作本来就不需要任何权限。
   */
  logout: async (): Promise<void> => {
    await apiClient.post('/auth/logout', undefined, { timeout: 15_000 })
  },
}
