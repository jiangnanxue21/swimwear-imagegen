# 使用指南:页面与接口

这份文档回答「每一页是干什么的」和「有哪些接口」。
业务规则本身不在这里 —— 那在 [`../ARCHITECTURE.md`](../ARCHITECTURE.md) 与
[`../STATUS.md`](../STATUS.md)。

![运营流程](../assets/operating-flow.svg)

## 登录与两个账号

打开网页需要先登录。两个固定账号,密码由部署的人在服务器上配:

| 账号 | 能做什么 |
| --- | --- |
| `admin` | 改配置、改 Provider、改提示词、测连接、看运行日志 —— 以及 operator 能做的一切 |
| `operator` | 日常生产:传商品、建任务、审图、上架 |

四件需要知道的事:

**非本机环境三项必填,配不全后端起不来。** 判据是 `APP_ENV` 不属于 local/dev/development
—— `uat`、`staging`、`test` 都算「非本机」,因为那几个名字对应的往往正是别人也能访问
的真机器。用 `docker-compose.prod.yml` 部署时,compose 会在**创建容器之前**就报变量
未设置,而不是让容器起来又退出。

**本机默认不开。** 三项全空时沿用旧的 Header 口令模式。但只要填了**任意一项**,
本机也会真的走登录 —— 这是刻意的:否则本地人工验收永远测不到 admin/operator 的差异、
退出登录和 403。

**换掉 `AUTH_SESSION_SECRET` 等于把所有人当场登出。** 签名 Cookie 是无状态的,
服务端不存会话表。反过来说,**多机部署各节点必须配同一把**,否则用户会「隔一次请求
就掉线」。

**登录状态是滑动过期,不是绝对存活时长。** `AUTH_SESSION_MAX_AGE_SECONDS` 计的是
**空闲**时间:页面只要还在发请求,Cookie 就会被不断续期。

> 设置页里那两把 `ADMIN_TOKEN` / `OPERATOR_TOKENS` 是**机器凭据**,给 CLI、脚本和
> pytest 用,和网页登录是两回事 —— 改它们不会影响任何人的登录密码。浏览器**不再持有
> 任何 Token**:凭据是 HttpOnly 签名 Cookie。

## 页面

侧栏按**一天的顺序**分四组(`frontend/src/App.tsx` 的 `NAV`)。`/` 重定向到 `/today`,
**首页是它,不是仪表盘**。

**「系统管理」整组只对 `admin` 显示**;但**路由全部照常注册** —— operator 手输
`/settings` 打得开,页面上是一句「当前账号没有管理员权限」,真正拦住他的是后端
`require_admin`。**菜单收敛是可发现性,不是权限边界。**

### 今日工作

| 路径 | 内容 |
| --- | --- |
| `/today` | **首页**。七张待办卡片,计数全部来自后端 |
| `/workbench-review` | **审核中心**:顶部按类别给计数(候选图 / 图片集 / 文案 / 属性冲突),下面是逐件快审。一屏一件,J/K/A/R 键盘流 |
| `/workbench-exceptions` | 异常与驳回。异常按「步骤 → 问题码」两层分组 |

### 商品生产

| 路径 | 内容 |
| --- | --- |
| `/workbench`、`/workbench/:id` | 商品工作台:七步流程聚合视图。支持「按 SKU / 按款」切换,默认按 SKU |
| `/wizard/:id` | 一体化向导:七步逐步引导,支持刷新恢复 |
| `/media` | 素材库:去重、归属与授权 |

### 导出与上架

| 路径 | 内容 |
| --- | --- |
| `/workbench-batches` | 批量与导出:批量任务与导出文件 |
| `/publish` | 发布上架:提交、状态、驳回回流、下架 |

### 系统管理(仅管理员可见)

| 路径 | 内容 |
| --- | --- |
| `/tasks`、`/tasks/:id` | 生成任务列表与详情(**排障用**):按轮次分组的候选图、评分抽屉、Provider 调用记录 |
| `/ai-tests` | AI 能力测试:对生产评分器与文案生成器单独打一次,评分只写诊断留痕,文案只回显 |
| `/dashboard` | 指标仪表盘:商品 / 任务 / 分档分布 / Provider 调用 / 出图覆盖率 / 最近失败 |
| `/spend` | 付费调用花费与预算 |
| `/model-templates` | 模特模板:上传、标签、姿势、体型、启用停用 |
| `/providers` | 出图服务商:能力、是否已配置、连接测试(只读) |
| `/prompts` | 提示词模板 |
| `/settings` | 设置:Provider 密钥、生成模型、评分模型、下载白名单 |
| `/audit` | 操作审计,可按操作人筛 |
| `/ops-logs` | **运行日志**:域 / 事件 / 级别 / request_id / task_id 筛选,可展开一次模型调用的完整往返 |
| `/system` | 系统状态:进程与依赖组件连通性 |

### 保留但不在菜单里的路由

| 路径 | 为什么保留 |
| --- | --- |
| `/reviews`、`/reviews/:id` | 候选图人工审核队列与详情。入口在审核中心顶部 |
| `/products`、`/products/:id` | 商品与 SKU 原始信息、**网站成品图与导出**、生成历史。日常走工作台详情,这里留给排障 |
| `/spus/new`、`/spus/:spuId` | 三步建档与 SPU 详情。入口在工作台的「新建」按钮 |
| `/workbench-import` | 批量导入 SKU(CSV)。入口同上,导入完成后回工作台 |
| `/workbench-spus` | SPU 聚合视图。已被工作台的「按款」视图取代 |

## 接口

**全量以运行中的 `/docs`(OpenAPI)为准**,路由装配的唯一真相在
`backend/app/api/router.py`。下面这张表是主链路,不是全量。

### 探针与登录

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 存活探针,不依赖外部组件;匿名可访问,顺带回 `auth_mode` |
| GET | `/api/health/ready` | 就绪探针,逐项报告 DB / Redis / 存储 |
| POST | `/api/auth/login` | 用户名 + 密码换 HttpOnly Session Cookie;匿名可访问 |
| POST | `/api/auth/logout` | 退出登录,**幂等**(没登录也回 200) |
| GET | `/api/auth/whoami` | 这次请求带的凭据是谁 |

### 建档与素材

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| — | `/api/spus/*` | SPU / 颜色变体 / SKU 展开(建档,商品的上游) |
| POST | `/api/products` | 新增商品,SKU 重复返回 409 |
| GET | `/api/products` | 列表,支持 search / status / category / garment_type / 分页 |
| PATCH | `/api/products/{id}` | 局部更新,SPU/SKU 不可改 |
| POST | `/api/products/import` | 批量导入,支持 CSV 文件或 JSON 数组;重复 SKU 记为跳过 |
| POST | `/api/products/{id}/assets` | 上传素材,返回是否命中去重 |
| — | `/api/media/*`、`/api/media-files/*` | 素材库、私有素材签名代发 |
| — | `/api/attributes/*` | 属性识别与人工校准 |

### 生成与评分

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| — | `/api/generation-plans/*` | 生成方案(创建任务前解析出当前生效的那一份) |
| POST | `/api/generation-tasks` | 创建生成任务,**立即返回**,生成在后台进行 |
| GET | `/api/generation-tasks/{id}` | 任务详情,含各轮候选图与 Provider 调用记录 |
| POST | `/api/generation-tasks/{id}/cancel` | 取消任务 |
| POST | `/api/generation-tasks/{id}/retry` | 失败后重新排队 |
| GET | `/api/generation-tasks/{id}/evaluations` | 该任务全部候选图的结构化评分 |
| GET | `/api/providers` | Provider 列表与能力,**不返回任何密钥** |
| POST | `/api/providers/{name}/test` | 连接测试,需要管理员身份(会拿真 Key 打厂商接口) |
| GET | `/api/evaluators` | 评分器列表,**不返回任何密钥** |
| GET | `/api/rule-sets` | 当前分档标准,只读 |

### 审核与产出

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/reviews` | 人工审核队列,按状态与进队原因筛选 |
| GET | `/api/reviews/{id}` | 审核详情:原图、各轮最佳候选、评分、硬错误 |
| POST | `/api/reviews/{id}/approve` | 人工通过,可指定采用哪张候选 |
| POST | `/api/reviews/{id}/reject` | 人工驳回,任务进终态 |
| POST | `/api/reviews/{id}/regenerate` | 追加轮次重新生成,可切换 Provider |
| — | `/api/image-sets/*` | 图片集编排与校验 |
| GET | `/api/exports/products/{id}` | 单商品成品图 URL,`?format=csv` 出表格 |
| GET | `/api/exports/products` | 批量导出,支持商品列表同款筛选,上限 1000 行 |

### 运营与上架

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| — | `/api/workbench/*` | 流程聚合、文案、草稿、批量任务、异常、审计查询 |
| — | `/api/publish/*` | 提交、清单、详情、刷新状态、下架、清理预案 |
| GET | `/api/dashboard/summary` | 仪表盘全部指标,一次给全 |
| — | `/api/usage/*`、`/api/environment` | 付费调用台账、环境真实性(状态条按它渲染) |

### 管理

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET/PUT | `/api/settings` | 可配置项、当前值与来源;**密钥只给末位打码串** |
| POST | `/api/settings/reset` | 删掉指定项的后台覆盖,回到环境变量 / 默认值 |
| GET | `/api/prompts`、`/api/prompts/{key}` | 提示词清单与详情(含注册表元数据) |
| PUT | `/api/prompts/{key}` | 保存新版本;**只接受消费链路会读库的 key** |
| POST | `/api/prompts/{key}/activate`、`/reset`、`/preview` | 切版本、恢复默认、体检预览 |
| GET | `/api/ops/logs`、`/logs/meta`、`/llm/{id}` | 运行日志事件流、域与事件注册表、一次模型调用的完整往返 |

管理类端点一律走 `require_admin`。**提示词决定评分口径,能改它的人等于能改「什么图算
合格」** —— 这比改一个超时值的权重大得多。

## 网站图片输出

通过审核的图会自动产出五个版本,商品详情页可直接看到并导出:

| 用途 | 目标尺寸 | 方式 |
| --- | --- | --- |
| `ORIGINAL` | 原尺寸 | 只重新编码 |
| `DETAIL` | 1200×1600 内 | 等比缩放 |
| `THUMBNAIL` | 400×533 内 | 等比缩放 |
| `MOBILE` | 750×1000 内 | 等比缩放 |
| `SQUARE` | 1000×1000 | 等比缩放后补边 |

两条规则值得知道:**绝不放大** —— 源图是 768×1024 时详情页就输出 768×1024,
不拉成假高清,并在 `note` 里写明原因;**正方形补边而不是裁剪** —— 竖构图的服装图
裁成 1:1,十有八九要么切掉头要么切掉下半身。

导出的 CSV 是一个 SKU 一行、各用途一列,CRLF + utf-8-sig,Excel 双击打开不乱码。

```bash
curl 'http://localhost:8000/api/exports/products/{id}'               # JSON
curl -O 'http://localhost:8000/api/exports/products/{id}?format=csv' # CSV
curl -O 'http://localhost:8000/api/exports/products?format=csv&only_complete=true'
```
