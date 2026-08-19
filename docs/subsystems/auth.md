# auth · 登录与权限

**代码**:`backend/app/api/auth.py`、`app/api/deps.py`、`app/workflows/login_throttle.py`

## 两套凭据,两件事

```
浏览器登录   用户名 + 密码 → HttpOnly 签名 Cookie      给人用
机器凭据     ADMIN_TOKEN / OPERATOR_TOKENS 请求头     给 CLI、脚本、pytest 用
```

改机器凭据不会影响任何人的登录密码。浏览器**不再持有任何 Token**。

`GET /api/health` 顺带回 `auth_mode`,冷启动横幅与前端据此知道当前是哪一套。

## 非本机环境三项必填

`ADMIN_PASSWORD` / `OPERATOR_PASSWORD` / `AUTH_SESSION_SECRET`。判据是 `APP_ENV`
不属于 local / dev / development —— `uat`、`staging`、`test` 都算「非本机」,
因为那几个名字对应的往往正是别人也能访问的真机器。

`docker-compose.prod.yml` 用 `${KEY:?}` 要求它们,于是 compose 会在**创建容器之前**
就报变量未设置,而不是让容器起来又退出。三个读 Settings 的服务(backend / worker /
beat)都要引到那组变量上 —— 只给 backend 加的表现是:API 起来了,而任务队列在悄悄
重启,页面能打开、任务永远不动。

## 本机默认不开,但填了任意一项就真走登录

否则本地人工验收永远测不到 admin/operator 的差异、退出登录和 403。

## 会话是无状态的

签名 Cookie,服务端不存会话表。所以:换掉 `AUTH_SESSION_SECRET` 等于把所有人当场
登出;多机部署各节点必须配同一把,否则用户会「隔一次请求就掉线」。

`AUTH_SESSION_MAX_AGE_SECONDS` 计的是**空闲**时间(滑动过期):页面只要还在发请求,
Cookie 就会被不断续期。要做「登录满 N 小时强制重登」得另外在会话里记登录时刻,
当前不做。

## 匿名白名单默认是关的

`main.PUBLIC_PREFIXES` **读写一视同仁**。以前这个白名单只管写方法、读接口默认全开 ——
那不是一个疏漏,是一个错误的默认值:能读就能枚举未发布的商品、看到审核结论和驳回
原因、拿到源素材和模特模板的地址。

现在要开放得显式加进那个列表,于是「开一个匿名接口」变成一次**会出现在 diff 里的
动作**,而不是某个新路由忘了挂守卫。

## 401 与 403 是两句不同的话

`AUTH_FAILED`(登录态失效)与 `AUTH_FORBIDDEN`(权限不足)在前端是两句不同的提示,
**且 403 不清登录态、不跳登录页** —— 一个 operator 点进管理页看到「请重新登录」
会以为是自己掉线了,然后反复登录。

## 菜单收敛不是权限边界

「系统管理」整组只对 admin 显示,但**路由全部照常注册**:operator 手输 `/settings`
打得开,页面上是一句「当前账号没有管理员权限」,真正拦住他的是后端 `require_admin`。
菜单收敛是可发现性。
