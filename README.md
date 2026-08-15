# 网站商品展示图自动生成系统

<div align="center">

**服装商品展示图的生产流水线** · 多 Provider 生成 · 确定性评分分档 · 自动重生与人工审核

🚀 [快速开始](#快速开始) · 🗺️ [架构图册](docs/ARCHITECTURE.md) · 📊 [能力现状](docs/STATUS.md) · 🧪 [评分器接入](docs/VISION-EVALUATOR.md) · 🚢 [部署](docs/DEPLOYMENT.md) · 🧭 [设计决策](docs/DECISIONS.md)

</div>

---

上传商品资料与原图 → 多 Provider 生成候选图 → 自动评分分档 → 自动重生或人工审核 → 输出网站可直接使用的多尺寸图片 URL。

```mermaid
flowchart LR
    A["建档与素材<br/>SPU / SKU / 原图"] --> B["生成<br/>每轮 4 张候选"]
    B --> C["评分分档<br/>A / B / C / D"]
    C -->|"A 档"| D["五个尺寸版本<br/>导出与上架"]
    C -->|"还有轮次"| B
    C -->|"轮次耗尽"| E["人工审核"]
    E -->|"通过"| D
    E -->|"追加轮次"| B
```

> 每一步的展开、以及轮级决策、Provider 错误策略、请求预算拟合、脱敏链路、
> 交付闸门的完整图解,见 **[`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md)**。

品类是参数,不是写死的:渠道字段 spec 按 `spec/{category_id}.yaml` 载入,属性注册表按品类校准。
**泳装是目前唯一已校准、且有渠道 spec 的品类**,所以样例数据与评分提示词里说的是泳装 ——
那是在描述这个品类,不是系统的边界。

### 文档地图

| 想知道 | 看这份 |
| --- | --- |
| 各条流程长什么样(图) | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| 某项能力能不能用、已知限制 | [`docs/STATUS.md`](docs/STATUS.md) —— **不确定时从这份开始** |
| 为什么这么设计、升级须知 | [`docs/DECISIONS.md`](docs/DECISIONS.md) |
| 部署与运维 | [`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md) |
| 接真实视觉评分模型 | [`docs/VISION-EVALUATOR.md`](docs/VISION-EVALUATOR.md) |
| 接 FASHN | [`docs/PROVIDER-FASHN.md`](docs/PROVIDER-FASHN.md) |
| 后台设置项的取舍 | [`docs/SETTINGS.md`](docs/SETTINGS.md) |
| 人工验收步骤 | [`docs/MANUAL-ACCEPTANCE.md`](docs/MANUAL-ACCEPTANCE.md) |

> ⚠️ **升级一个已在运行的部署之前,先读 `docs/DECISIONS.md` 第三节。**
> 那里按主题归并了全部升级须知:必须做的人工动作(主密钥轮换、`ADMIN_TOKEN`、
> beat 进程)、原本能跑通但现在会被挡下的操作、以及几处不报错的看板口径变更。
>
> ⚠️ **升级一个已在运行的部署之前,先读 `docs/DECISIONS.md` 第三节。**
> 那里按主题归并了全部升级须知:必须做的人工动作(主密钥轮换、`ADMIN_TOKEN`、
> beat 进程)、原本能跑通但现在会被挡下的操作、以及几处不报错的看板口径变更。

---

## 快速开始

```bash
cp .env.example .env
docker compose up -d --build
make migrate
make smoke      # 一分钟内告诉你闭环通不通
```

启动后:

| 地址 | 内容 |
| --- | --- |
| http://localhost:8000/api/health | 存活探针 |
| http://localhost:8000/api/health/ready | 依赖就绪探针(DB / Redis / 存储) |
| http://localhost:8000/docs | OpenAPI 文档 |
| http://localhost:5173 | 后台前端(开发服务器) |

> **前端要从别的机器打开?** 可以,直接访问 `http://<这台机器的 IP>:5173` 即可 ——
> 前端与后端走 Vite 同源代理(`docker-compose.yml` 里的 `VITE_PROXY_TARGET`),
> 不涉及 CORS。**不要**给前端配 `VITE_API_BASE_URL`:那是绝对地址、由浏览器解析,
> 配上之后就只有 docker 宿主机本机能用了(A20 评审 P0-2)。
>
> 上面这条命令跑的是 **Vite 开发服务器**,只适合开发与 UAT。类生产部署用:
>
> ```bash
> docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
> # 前端构建产物由 Nginx 托管,监听 127.0.0.1:8080;backend 不再对外发布端口
> ```

导入示例数据(幂等,可重复执行。**条数不写在这里** —— 增删样例时写死的数字
会静默过期,要当前口径跑 `cd backend && python3 tools/verify_sample_data.py`):

```bash
python3 sample-data/generate_images.py   # 首次需先生成占位图
make seed
```

验证队列连通:

```bash
make worker-ping     # 期望输出 {'pong': True, ...}
```

## 已实现的接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/health` | 存活探针,不依赖外部组件;匿名可访问,顺带回 `auth_mode` |
| GET | `/api/health/ready` | 就绪探针,逐项报告 DB / Redis / 存储 |
| POST | `/api/auth/login` | 浏览器登录,用户名+密码换 HttpOnly Session Cookie;匿名可访问 |
| POST | `/api/auth/logout` | 退出登录,**幂等**(没登录也回 200);匿名可访问 |
| GET | `/api/auth/whoami` | 这次请求带的凭据是谁,冷启动横幅与顶栏身份区据此渲染 |
| POST | `/api/products` | 新增商品,SKU 重复返回 409 |
| GET | `/api/products` | 列表,支持 search / status / category / garment_type / 分页 |
| GET | `/api/products/{id}` | 商品详情 |
| PATCH | `/api/products/{id}` | 局部更新,SPU/SKU 不可改 |
| POST | `/api/products/import` | 批量导入,支持 CSV 文件或 JSON 数组;重复 SKU 记为跳过 |
| POST | `/api/products/{id}/assets` | 上传素材,返回是否命中去重 |
| GET | `/api/products/{id}/assets` | 素材列表,含可访问 URL |
| POST | `/api/generation-tasks` | 创建生成任务,**立即返回**,生成在后台进行 |
| GET | `/api/generation-tasks` | 任务列表,支持商品/状态/Provider 筛选 |
| GET | `/api/generation-tasks/{id}` | 任务详情,含各轮候选图与 Provider 调用记录 |
| POST | `/api/generation-tasks/{id}/cancel` | 取消任务 |
| POST | `/api/generation-tasks/{id}/retry` | 失败后重新排队 |
| GET | `/api/providers` | Provider 列表与能力,**不返回任何密钥** |
| POST | `/api/providers/{name}/test` | 连接测试,需要 `X-Admin-Token`(会拿真 Key 打厂商接口) |
| GET/POST | `/api/model-templates` | 模特模板列表与上传 |
| GET | `/api/reviews` | 人工审核队列,按状态与进队原因筛选 |
| GET | `/api/reviews/{id}` | 审核详情:原图、各轮最佳候选、评分、硬错误 |
| POST | `/api/reviews/{id}/approve` | 人工通过,可指定采用哪张候选 |
| POST | `/api/reviews/{id}/reject` | 人工驳回,任务进终态 |
| POST | `/api/reviews/{id}/regenerate` | 追加轮次重新生成,可切换 Provider |
| GET | `/api/generation-tasks/{id}/evaluations` | 该任务全部候选图的结构化评分 |
| GET | `/api/rule-sets` | 当前分档标准,只读 |
| GET | `/api/evaluators` | 评分器列表,**不返回任何密钥** |
| GET | `/api/dashboard/summary` | 仪表盘全部指标,一次给全 |
| GET | `/api/exports/products/{id}` | 单商品成品图 URL,`?format=csv` 出表格 |
| GET | `/api/exports/products` | 批量导出,支持商品列表同款筛选,上限 1000 行 |
| GET | `/api/settings` | 可配置项、当前值与来源,需要 `X-Admin-Token`,**密钥只给末位打码串** |
| PUT | `/api/settings` | 保存配置,需要 `X-Admin-Token` |
| POST | `/api/settings/reset` | 删掉指定项的后台覆盖,回到环境变量/默认值,需要 `X-Admin-Token` |

**这张表是主链路,不是全量。** 后来的几个阶段又加了七组接口,数量已经超过
一张手写表能跟上的程度 —— 全量以运行中的 `/docs`(OpenAPI)为准,
路由装配的唯一真相在 `backend/app/api/router.py`:

| 接口组 | 做什么 |
| --- | --- |
| `/api/spus/*` | SPU / 颜色变体 / SKU 展开(建档,商品的上游) |
| `/api/attributes/*` | 属性识别与人工校准 |
| `/api/media/*`、`/api/media-files/*` | 素材库、私有素材签名代发 |
| `/api/image-sets/*` | 图片集编排与校验 |
| `/api/generation-plans/*` | 生成方案(创建任务前解析出当前生效的那一份) |
| `/api/workbench/*` | 运营工作台:流程聚合、文案、草稿、批量任务、异常、审计查询 |
| `/api/publish/*` | 发布上架:提交、清单、详情、刷新状态、下架、清理预案 |
| `/api/usage/*`、`/api/environment` | 付费调用台账、环境真实性(状态条按它渲染) |



## 生成一张图试试

```bash
make seed                          # 导入示例商品与素材
```

打开 http://localhost:5173 → 商品 → 任选一个 → 创建生成任务 → Provider 选 `mock` → 提交。

任务立即返回,后台自动跑完整闭环:每轮出 4 张候选 → 轮内预排序 → 评分 → A/B/C/D 分档
→ 达到 A 档就自动通过,否则淘汰并换 seed 重生 → 轮次耗尽仍不达标才转人工审核。

创建任务弹窗里有两个 Mock 专用旋钮,不需要任何外部服务就能演练各条分支:

| 旋钮 | 取值 | 用途 |
| --- | --- | --- |
| 模拟生成结果 | 成功 / 失败 / 超时 / 无候选 / 限流 / 内容安全 | Provider 侧的失败分支 |
| 模拟评分结果 | A / B / C / D / 硬错误 / 逐轮变好 / 始终不达标 | 分档、重生、转人工 |

想直接看到人工审核队列:选「始终不达标」,最多轮次设 2,提交后到 `/reviews` 即可。

## 已实现的页面

侧栏按**一天的顺序**分四组(`App.tsx` 的 `NAV`)。`/` 重定向到 `/today`,
**首页是它,不是仪表盘**;`/dashboard` 在 A9 之后归到「系统管理」组。
菜单与路由都**不按账号裁剪** —— 非管理员看得见管理入口,点进去由后端返回 403,
理由在 `App.tsx` 的「路由和菜单都不按角色裁剪」一节。

| 路径 | 组 | 内容 |
| --- | --- | --- |
| `/login` | — | 浏览器登录。未登录访问任何页面都会弹到这里,并带 `?next=` |
| `/today` | 今日工作 | **首页**。七张待办卡片,计数全部来自后端 |
| `/workbench-review` | 今日工作 | **审核中心**:顶部按类别给计数(候选图/图片集/文案/属性冲突),下面是逐件快审 |
| `/reviews`、`/reviews/:id` | 路由保留,不在菜单 | 候选图人工审核队列与详情:原图对照、各轮最佳候选、维度分、通过/驳回/重生。入口在审核中心顶部 |
| `/workbench-exceptions` | 今日工作 | 异常与驳回 |
| `/workbench`、`/workbench/:id` | 商品生产 | 商品工作台:七步流程聚合视图 |
| `/wizard/:id` | 商品生产 | 一体化向导:七步逐步引导,支持刷新恢复 |
| `/products`、`/products/:id` | 路由保留,不在菜单 | 商品与 SKU 原始信息:列表筛选、详情、**网站成品图与导出**、生成历史。日常走工作台详情,这里留给排障与看原始信息 |
| `/spus/new`、`/spus/:spuId` | 路由保留,不在菜单 | 新建商品款式(三步建档)与 SPU 详情。入口在工作台的「新建」按钮 |
| `/workbench-import` | 路由保留,不在菜单 | 批量导入 SKU(CSV)。入口在工作台的「新建」按钮,导入完成后回工作台 |
| `/media` | 商品生产 | 素材库:去重、归属与授权 |
| `/tasks`、`/tasks/:id` | 系统管理 | 生成任务列表与详情(**排障用**):按轮次分组的候选图、评分抽屉、Provider 调用记录。运营看自己商品的任务走工作台详情的「生成任务」页签 |
| `/workbench-spus` | 路由保留,不在菜单 | SPU 聚合视图。已被工作台的「按款」视图取代 |
| `/workbench-batches` | 导出与上架 | 批量与导出:批量任务与导出文件 |
| `/publish` | 导出与上架 | 发布上架:提交、状态、驳回回流、下架 |
| `/dashboard` | 系统管理 | 指标仪表盘:商品/任务/分档分布/Provider 调用/出图覆盖率/最近失败 |
| `/spend` | 系统管理 | 付费调用花费与预算 |
| `/model-templates` | 系统管理 | 模特模板:上传、标签、姿势、体型、启用停用 |
| `/providers` | 系统管理 | Provider:能力、是否已配置、连接测试(只读) |
| `/prompts` | 系统管理 | 提示词模板 |
| `/settings` | 系统管理 | 设置:Provider 密钥、生成模型、评分模型、下载白名单 |
| `/audit` | 系统管理 | 操作审计,可按操作人筛 |
| `/system` | 系统管理 | 系统状态:进程与依赖组件连通性 |

**不需要任何第三方 API Key,也不需要视觉大模型。** 未配置 FASHN / fal.ai / ComfyUI 时系统照常启动,
对应 Provider 显示为"未配置";没有外部评分模型时使用 Mock 评分器,整条评分闭环照样跑通。
但 **Mock 只是离线演练模式**:它按文件指纹给分,不能用来决定图片上不上网站。
接真实评分模型见 `docs/VISION-EVALUATOR.md`(含 OpenAI / 豆包 / 千问 VL 三家的完整配置)。
要接入真实服务时,密钥既可以写 `.env`,也可以在 `/settings` 页填 —— 见下面「后台设置」一节。

## 评分与自动决策(阶段 4)

候选图不靠人眼逐张看,而是走一条确定性的流水线:

```mermaid
flowchart TD
    R["每轮 4 张候选"] --> PRE["轮内预排序<br/>感知哈希 + 真实性代理指标(可关闭)"]
    PRE --> SPLIT["前两名完整评分<br/>后两名快速硬错误检查"]
    SPLIT --> G{"A / B / C / D 分档"}
    G -->|"A"| PASS["自动通过<br/>按比例随机抽检"]
    G -->|"B"| FIX["按问题代码生成<br/>确定性修复参数,定向重生"]
    G -->|"C"| SWAP["淘汰,换 seed 或模特模板<br/>必要时换 Provider"]
    G -->|"D"| DROP["当前候选直接淘汰"]
    FIX & SWAP & DROP --> MORE{"还有轮次?"}
    MORE -->|"有"| R
    MORE -->|"耗尽且无 A 档"| HUMAN["任务进人工审核"]
```

> 分档门槛、硬错误全表(18 项)与 Provider 错误策略矩阵见
> [`docs/ARCHITECTURE.md` 第 2、3 节](docs/ARCHITECTURE.md#2-轮级决策什么时候重生什么时候找人)。

三条规则值得单独强调:

**总分由后端按权重算,不采信评分器自报的数字。** 两者差值存进 `model_reported_overall`,
用来监控大模型打分漂移。Mock 评分器故意自报一个不同的数,等于给这条规则内置了活体探针。

**硬错误只淘汰候选,不终结任务。** 硬错误代码(`core/enums.HardFailCode`,
按受众分女装 / 男装 / 通用三组)中任意一个出现即判 D;
但只要还有轮次,任务就继续自动重生,**不立刻交给人工**。
人工审核的对象是**商品任务**,不是每一张低分候选图 —— 否则队列会被本可自动解决的废图淹没。

**A 档要同时过四条底线。** 总分 ≥ 85 之外,商品身份一致性 ≥ 90、结构一致性 ≥ 90、
人体真实性 ≥ 85、网站可用性 ≥ 85 缺一不可。总分 96 但商品身份 88 的图判不到 A。

阈值、权重、预排序开关、抽检比例都在数据库的 `RuleSet` 里,后台 `/reviews` 页可只读查看。
分档与修复策略是纯函数(不碰数据库、不发网络请求),"这张图为什么被判 C"
永远可以在一次单元测试里复现。

## 接入 FASHN(阶段 5)

只填一个 Key 就能用,其余走官方默认值:

```bash
# .env
FASHN_API_KEY=fa-xxxxxxxx
```

```bash
docker compose restart backend worker
curl -X POST http://localhost:8000/api/providers/fashn/test \
     -H "X-Admin-Token: $ADMIN_TOKEN"    # APP_ENV=local 下可省;其它环境必带
# {"configured": true, "reachable": true, "message": "连接正常,剩余额度 234"}
```

之后在创建生成任务时把 Provider 选成 `fashn` 即可,业务流程与 Mock 完全一致 ——
评分、分档、自动重生、人工审核那套逻辑一行都没有为 FASHN 改过。

| 生成模式 | FASHN 模型 | 一次调用出图 |
| --- | --- | --- |
| `virtual_try_on` | `tryon-max`(可切 `tryon-v1.6`) | 1-4 张 |
| `product_to_model` | `product-to-model` | 1 张,靠多次提交凑候选数 |

**会产生费用。** 默认配置下一轮约 8 额度,自检接口不花钱。
接入依据、费用表、错误映射、排查手册见 **`docs/PROVIDER-FASHN.md`**;
官方文档原样存档在 `docs/vendor/fashn-skill/`。

上量前先跑基线对比,看清它相对 Mock 到底好在哪:

```bash
make baseline SKU=SW-001-BLK-S P=mock,fashn
```

## 网站图片输出(阶段 6)

通过审核的图会自动产出五个版本,商品详情页可直接看到并导出:

| 用途 | 目标尺寸 | 方式 |
| --- | --- | --- |
| `ORIGINAL` | 原尺寸 | 只重新编码 |
| `DETAIL` | 1200×1600 内 | 等比缩放 |
| `THUMBNAIL` | 400×533 内 | 等比缩放 |
| `MOBILE` | 750×1000 内 | 等比缩放 |
| `SQUARE` | 1000×1000 | 等比缩放后补边 |

两条规则值得知道:**绝不放大** —— 源图是 768×1024 时详情页就输出 768×1024,
不拉成假高清,并在 `note` 里写明原因;**正方形补边而不是裁剪** ——
竖构图的服装图裁成 1:1,十有八九要么切掉头要么切掉下半身。

导出:

```bash
curl 'http://localhost:8000/api/exports/products/{id}'              # JSON
curl -O 'http://localhost:8000/api/exports/products/{id}?format=csv' # CSV
curl -O 'http://localhost:8000/api/exports/products?format=csv&only_complete=true'
```

CSV 是一个 SKU 一行、各用途一列,CRLF + utf-8-sig,Excel 双击打开不乱码。

**换对象存储**:改 `STORAGE_BACKEND=s3` 并填 `S3_*`,装 `pip install -e ".[s3]"`。
两种后端共用同一套路径推导,已有数据 `mc mirror` 过去即可,数据库不用改。

## 后台设置(阶段 8)

密钥和模型不必再改 `.env` 重启。打开 http://localhost:5173/settings:

| 能改 | 例子 |
| --- | --- |
| 生成通用 | 默认 Provider、路由方式、单任务轮次上限 |
| FASHN | API Key、试穿模型(tryon-max / tryon-v1.6)、档位、分辨率、输出格式、各项超时 |
| fal.ai / ComfyUI | 地址与 Key(请求映射仍待接入,选中会被挡下) |
| 评分模型 · 多模态 | 评分器、接口地址、模型名、API Key、API 形状、输出约束 |
| 评分模型 · 多模态调参 | 图片精度、推理强度、超时重试、图片与参考图上限、失败关闭 |
| 文本模型 · 非多模态 | **预留,当前无调用点**,填了不生效 |
| 下载安全 | 允许下载结果图的内网主机白名单 |

数据库地址、Redis、存储后端、S3 密钥这些**故意不放进来** ——
改错就是整站不可用,只能走部署。

三件需要知道的事:

**密钥加密落库,明文不出后端。** 主密钥取自 `SETTINGS_SECRET_KEY`;
留空则自动在密钥目录(`SETTINGS_KEY_DIR`,默认项目根下 `.secrets/`)生成 `.settings.key`
并打日志提醒,单机开箱可用,多机部署必须显式配置,否则各节点解不开对方写的值。
密钥目录**独立于存储目录** —— 存储目录会被挂成 `/files` 静态服务,
主密钥放进去等于连同数据库一起公开(`docker compose` 里已是独立的 `secrets` 卷)。
页面上永远只显示末位打码串,拿不到加密能力时**拒绝保存密钥**而不是退化成明文。

```bash
# 生成一把主密钥
openssl rand -base64 32
```

**看和改配置都要口令。** 请求头 `X-Admin-Token`,值取自 `.env` 里的 `ADMIN_TOKEN`,
覆盖设置页读写与 Provider 连接测试。没配它时:`APP_ENV=local/dev/development` 放行,
其它环境一律拒绝(`test` 不在放行名单里 —— 测试环境通常连着真 Key)。
口令在设置页顶部填一次,存在浏览器本地,只跟随需要它的那几个请求发出。

**改完就生效,不用重启。** 后端立即生效;Celery worker 最迟 `SETTINGS_CACHE_TTL_SECONDS`
(默认 10 秒)内跟上。已经排队的任务不受影响。

想把配置钉死在部署流水线上,设 `SETTINGS_ENV_LOCK=true`:
凡是 `.env` 给过值的项在网页上只读,数据库覆盖也不生效。生产环境建议这样。

每一项旁边标着值是谁给的 —— 后台设置 / 环境变量 / 默认值;
后台改过的项可以一键「恢复」,删掉覆盖即回到 `.env` 说了算的状态。

设计取舍与未完成事项见 `docs/SETTINGS.md`。

## 浏览器登录

打开网页需要先登录。两个固定账号,密码由部署的人在服务器上配:

| 账号 | 能做什么 |
| --- | --- |
| `admin` | 改配置、改 Provider、改提示词、测连接 —— 以及 operator 能做的一切 |
| `operator` | 日常生产:传商品、建任务、审图、上架 |

配置在 `.env` 里,三项:

```ini
ADMIN_PASSWORD=换成一个真密码
OPERATOR_PASSWORD=换成另一个真密码       # 不能和上面相同
AUTH_SESSION_SECRET=                    # 至少 32 字符,见下面那行命令
```

```bash
# 生成一把签名密钥
python3 -c "import secrets;print(secrets.token_urlsafe(48))"
```

四件需要知道的事:

**非本机环境三项必填,配不全后端起不来。** 判据是 `APP_ENV` 不属于
local/dev/development —— `uat`、`staging`、`test` 都算"非本机",因为那几个名字
对应的往往正是别人也能访问的真机器。用 `docker-compose.prod.yml` 部署时,
compose 会在**创建容器之前**就报变量未设置,而不是让容器起来又退出。

**本机默认不开。** 三项全空时沿用旧的 Header 口令模式(`APP_ENV=local` 下连口令
都不用)。但只要填了**任意一项**,本机也会真的走登录 —— 这是刻意的:否则本地
人工验收永远测不到 admin/operator 的差异、退出登录和 403。

**换掉 `AUTH_SESSION_SECRET` 等于把所有人当场登出。** 签名 Cookie 是无状态的,
服务端不存会话表,旧 Cookie 的签名换一把密钥就验不过了。反过来说,
**多机部署各节点必须配同一把**,否则用户会"隔一次请求就掉线"。

**登录状态是滑动过期,不是绝对存活时长。** `AUTH_SESSION_MAX_AGE_SECONDS`
(默认 12 小时)计的是**空闲**时间:页面只要还在发请求,Cookie 就会被不断续期。
要做"登录满 N 小时强制重登",得另外在会话里记登录时刻,当前不做。

设置页里那两把 `ADMIN_TOKEN` / `OPERATOR_TOKENS` 是**机器凭据**,给 CLI、脚本和
pytest 用,和网页登录是两回事 —— 改它们不会影响任何人的登录密码。浏览器**不再
持有任何 Token**:凭据是 HttpOnly 签名 Cookie,localStorage 里那两把口令与设置页
上的录入卡在 a46-phase6 一起删掉了。

**侧栏按角色收敛。** 「系统管理」整组(仪表盘、花费、模特模板、Provider、
提示词、设置、审计、系统状态)**只对 `admin` 显示**;`operator` 的顶栏菜单里也
没有「系统设置」。但**路由全部照常注册** —— operator 手输 `/settings` 打得开,
页面上是一句「当前账号没有管理员权限」,真正拦住他的是后端 `require_admin`。
菜单收敛是可发现性,不是权限边界。

## 本机开发(不用 Docker)

```bash
# 后端
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
# 需要本机 PostgreSQL 与 Redis;或只改 DATABASE_URL 指向已有实例
uvicorn app.main:app --reload

# 前端
cd frontend
npm install
npm run dev
```

## 测试

```bash
make check         # 全部门禁 = check-offline + fe-check。**需联网**:fe-check 要 npm ci
make check-offline # 离线子集:全部纯逻辑用例 + 六道审计。不需要 node_modules,不需要网络
make fe-check      # 前端全部门禁:npm ci → typecheck → lint → test → build → syntax-check(需联网)

make test-pure     # 只跑纯逻辑用例
make smoke         # 端到端冒烟:对着跑起来的系统走一遍完整闭环
make test          # 容器内 pytest 全量(含需要 PostgreSQL 的模型/API/迁移测试)
```

**`make check-offline` 跑绿 ≠ 全都过了。** 它覆盖不到前端类型、lint、Vitest 与构建 ——
那四层只有 `make check`(或单独的 `make fe-check`)会跑,而它们需要网络装依赖。
`check-offline` 目标自己会在末尾打印这句话。交付前两条都要跑。

> 这一段在 A45-batch15-merged 之前是**反的**:它把 `make check` 写成「离线全部门禁,
> 不需要网络」,把「覆盖不到类型与构建」也记在 `make check` 头上。实际上
> `check: check-offline fe-check`,离线的是 `check-offline`。照着旧文案做的人
> 会在一台没网的机器上敲 `make check`,得到一个装依赖失败的红,
> 然后以为门禁本身坏了。
>
> 旧文案里还写着「1270+ 纯逻辑用例」。**这一版把用例数整个删掉了,不是改新。**
> 订正这一段时我先写了当时的真值,然后本批自己新增 5 条守卫就把它作废了 ——
> 根因不是数字抄错,是**把一个每批都在变的事实复制进了散文**。
> 真值由 `make test-pure` 自己打印,这里不留第二份。

测试分两层:

- **`tests/pure/`** —— 零三方依赖,覆盖哈希去重、路径穿越、上传校验、图片探测、
  CSV 导入、存储不变量、配置契约、日志脱敏、模型与迁移一致性、内部 import 完整性、
  状态机转移表、Provider 契约与路由、幂等键、错误策略、编排流水线仿真、
  评分权重与解析、A/B/C/D 分档、轮级决策、候选预排序、前后端契约、设置项契约与打码、
  评审回归(幂等键、CSV 注入、重定向 SSRF、认领状态、字段上限)。
  同时兼容 pytest 与 `tools/run_pure_tests.py`,后者用于没装 pytest 的环境。
- **`tests/test_*.py`** —— 需要真实 PostgreSQL,覆盖 ORM 约束、REST API、Alembic 升降级。
  没有可用数据库时自动 skip,不会让 CI 假红。

设置测试库:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://imagegen:imagegen@localhost:5432/imagegen_test
```

## 目录结构

```
backend/app/api/         HTTP 路由
backend/app/core/        配置、日志、错误、哈希、路径安全(零三方依赖,可独立测试)
backend/app/db/          引擎与会话
backend/app/models/      SQLAlchemy 模型
backend/app/schemas/     Pydantic 出入参
backend/app/services/    业务逻辑
backend/app/providers/   图像生成 Provider 抽象(阶段 3)
backend/app/evaluators/  候选图评分器(阶段 4)
backend/app/workflows/   状态机与编排(阶段 3)
backend/app/tasks/       Celery
backend/app/attributes/  属性注册表、校准、置信度与运行态
backend/app/extractors/  属性抽取器(视觉 / mock)与调用预算
backend/app/media/       素材域:证据规则、样本完整度、来源冲突
backend/app/listings/    Listing:SKU 矩阵、文案、变体键、图片集、导出写出
backend/app/workbench/   运营工作台:流程、批次、平台、颜色维聚合
backend/app/channels/    渠道 Adapter 与字段 spec(`spec/{category_id}.yaml`)
backend/app/llm/         多模态与文本模型传输层、端点信任、脱敏
backend/app/scripts/     一次性脚本:冒烟、校准、回填、清理、基线
frontend/src/            React + TS + AntD 后台
comfyui/                 工作流 JSON 与节点映射(阶段 5)
sample-data/             示例商品与素材
```

## 设计约定

- **UUID 主键**,所有实体统一。
- **统一错误体**:`{"error": {"code": "...", "message": "..."}}`,后端不向客户端返回数据库异常原文。
- **日志脱敏**:键名命中 `api_key/secret/password/token/authorization/credential` 的值一律记为 `***`。
- **落盘路径由 sha256 推导**(`ab/cd/<hash>.jpg`),用户提供的文件名只用于展示,不参与路径拼接。
- **原始上传文件永不覆盖**。

## 常用命令

```bash
make help          # 列出所有命令
make up / down     # 启停
make logs          # 跟踪后端与 worker 日志
make migrate       # 执行迁移
make psql          # 进入数据库
make clean         # 停止并删除数据卷
make baseline SKU=SW-001-BLK-S P=mock,fashn   # Provider 基线对比
make calibrate     # 评分器校准:人工判定 vs 模型分档的一致率
make requeue       # 找出并重新派发滞留任务(加 APPLY=1 真的派发)
make secret-key    # 生成设置页主密钥
```

## 环境变量

见 `.env.example`,按主题分组(应用、数据库、Redis/Celery、存储、上传限制、
Provider、评分与视觉模型、属性识别、批量执行、文案、后台设置页、**浏览器登录**、
费用与预算)。**分组数不写在这里** —— 以那个文件为准。
`tests/pure/test_config_contract.py` 会静态校验 `.env.example` 与 `Settings` 字段一一对应,新增配置项时会自动提醒补文档。


## 未确认的第三方字段

FASHN 已按官方文档(你提供的 skill)接入,不再有 TODO。
以下内容**没有**凭记忆写入代码,全部标记为 `TODO`,必须对照官方文档填写:

| Provider | 待确认 |
| --- | --- |
| fal.ai | 使用哪个 model endpoint、该 endpoint 的输入 schema、队列与轮询路径 |
| ComfyUI | 服务地址、真实工作流 JSON、各输入节点的真实 ID |

(视觉评分模型已接入,支持 OpenAI Responses / OpenAI 兼容 Chat Completions /
火山方舟豆包 / 阿里云百炼千问 VL —— 配置见 `docs/VISION-EVALUATOR.md`。)

在填写之前,这两个 Provider 的 `submit()` 会抛 `NotImplementedError` 或
`NotConfiguredError`,**不会发出任何真实请求,也不会产生费用**。
