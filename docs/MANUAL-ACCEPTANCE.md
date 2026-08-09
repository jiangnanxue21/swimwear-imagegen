# 完整人工验证与 UAT 验收手册

适用场景：功能开发完成后，验证一份发布候选版本是否真的具备“服装商品建档 → 图片生成 → 评分与审核 → 属性识别 → Listing → 渠道发布 → 状态轮询 → 驳回修复 → 更新与下架”的完整能力。

本文是执行手册，不是完成报告。每一步都必须留下本轮证据；没有执行的项目只能写“未验证”，不能按“代码已实现”判通过。

> 当前事实（2026-08-08）：仓库的自动化与 Simulator 链路已较完整，但 `docs/STATUS.md` 仍明确记录 **Docker build、Playwright、真实平台环境未执行，AC-01～AC-22 完整通过为 0/22**。FASHN 与真实属性抽取也仍欠真实端点验证。因此第一次执行本文时，不能预先勾选任何真环境项目。

> 本系统范围是服装；但泳装是当前唯一已校准、且有渠道 spec 的品类。首次完整 UAT 应先用泳装完成。测试其他品类前，必须先确认该品类的属性注册表、评分校准和渠道 spec 已完成。

---

## 1. 最重要的顺序

严格按下面顺序推进。前一关不通过，后一关不开始。

| 顺序 | 验证层 | 目的 | 失败时先查 |
|---:|---|---|---|
| 0 | 冻结版本与自动门禁 | 证明候选版本可构建、可迁移、自动测试全绿 | 代码、依赖、迁移 |
| 1 | 类生产 UAT 部署 | 证明同一版本能重复部署，DB/Redis/存储/worker 都真实可用 | 部署与基础设施 |
| 2 | 无费用 Simulator 单件闭环 | 先证明内部业务编排与页面动线完整 | 本系统业务逻辑 |
| 3 | Simulator 异常矩阵 | 证明失败状态、下一步与幂等处理正确 | 状态机与恢复入口 |
| 4 | 逐个接真实外部能力 | FASHN、评分、属性、文案逐个验证，便于归因 | 对应 Provider/模型 |
| 5 | 真实模型 + Simulator 全链路 | 证明真实内容生产能进入 Listing，仍不碰真实店铺 | 内容生产链路 |
| 6 | 小批量与故障恢复 | 证明 5～20 件不会重复付费、丢任务或互相污染 | 租约、回执、Celery |
| 7 | 真实渠道干跑 | 对照真实渠道字段和报文，不创建商品 | Adapter、凭证、字段 spec |
| 8 | 最小真实发布 | 只在测试店铺创建 1 件，验证创建/更新/轮询/驳回/下架 | 真实渠道差异 |
| 9 | 正式 UAT | 30 件、3 个批次、质量统计与故障演练 | 产品质量与运营效率 |
| 10 | 清理与退出 | 下架全部测试商品、核费用、归档缺陷 | 清理清单与平台后台 |

核心原则：

- 先 Mock/Simulator，后真实服务；先单件，后批量；先干跑，后真实发布。
- FASHN、评分模型、属性模型、文本模型不要第一次同时打开。一次只替换一个变量，成功后再替换下一个。
- 真实发布只允许专用测试店铺、测试类目、测试凭证；绝不直接拿生产店铺做首次验证。
- 不手工改数据库制造“通过”。如果页面或正式 API 无法完成一条主流程，应记为缺陷。
- 自动化测试库与 UAT 业务库必须是两个库。自动化夹具会清空测试库 schema，绝不能指向 UAT/生产库。

---

## 2. 人员、环境与数据准备

### 2.1 建议角色

| 角色 | 责任 |
|---|---|
| 发布负责人 | 冻结 commit、确认门禁、作最终 Go/No-Go 决定 |
| 运维/后端 | 部署、迁移、DB/Redis/worker、日志与故障演练 |
| 运营验收人 | 按真实工作方式走页面，判图片、属性、文案和下一步是否清楚 |
| 渠道负责人 | 提供测试店铺与凭证，核对真实平台商品、状态与清理结果 |
| 业务/品类专家 | 确认高风险属性、图片一致性、文案事实与质量阈值 |

至少找一位未参与开发的人执行主要页面流程。开发者自己知道系统“应该怎样”，容易绕过不清楚的交互。

### 2.2 两套数据库，不得混用

| 环境 | 示例库名 | 用途 | 是否允许测试框架清库 |
|---|---|---|---|
| 门禁测试库 | `imagegen_gate_test` | pytest、迁移升降级、并发测试 | 允许；库名必须以 `_test` 结尾，并显式设置 `ALLOW_DESTRUCTIVE_TEST_DB=1` |
| UAT 业务库 | `imagegen_uat` | 浏览器人工验证、真实 Provider、真实渠道 | 禁止；不要设置 `ALLOW_DESTRUCTIVE_TEST_DB=1` |

### 2.3 测试数据分层

准备以下数据，并建立一张外部台账记录本地 ID、SPU/SKU、测试批次号、外部商品 ID 和费用：

1. 仓库样例：`sample-data/` 的 10 件商品与 30 张占位图，用于无费用冒烟。
2. 真实小样本：5 件已授权、信息完整的泳装商品，每件至少正面、背面、细节图。
3. 正式 UAT 样本：至少 30 件真实商品，覆盖不同颜色、图案、版型和高风险属性。
4. 批次样本：至少 3 批，每批 5～20 件。
5. 异常样本：缺图、证据不足、字段缺失、违规声明、Provider 失败、渠道驳回各至少 1 件。

统一测试批次号，例如：

```text
uat-20260808-r1
```

所有真实发布都必须填写同一个 `test_batch_tag`。没有批次号的外部商品很难完整清理。

### 2.4 每条用例要留什么证据

| 字段 | 记录内容 |
|---|---|
| 版本 | Git commit、镜像 tag、执行日期 |
| 环境 | UAT 地址、APP_ENV、渠道/站点/店铺，绝不记录密钥 |
| 数据 | SPU/SKU、本地 product/listing/batch ID、test_batch_tag |
| 操作 | 操作人、时间、步骤编号 |
| 结果 | 预期、实际、通过/失败/未验证 |
| 证据 | 截图、下载文件、request_id、脱敏日志、平台外部 ID |
| 费用 | 厂商后台用量、本系统 `/spend` 台账、差异说明 |
| 缺陷 | 严重级别、负责人、是否阻断上线 |

---

## 3. 配置清单

从 `.env.example` 复制一份 UAT 配置。密钥只放 secret manager、受控 `.env` 或后台设置，不写进仓库、截图、缺陷单和聊天记录。

### 3.1 UAT 基础配置

建议基线如下；值中的占位符必须替换：

```env
APP_ENV=uat
DEBUG=false
LOG_LEVEL=INFO
API_PREFIX=/api

POSTGRES_USER=imagegen_uat
POSTGRES_PASSWORD=<强随机口令>
POSTGRES_DB=imagegen_uat
DATABASE_URL=

REDIS_URL=redis://redis:6379/0
CELERY_TASK_ALWAYS_EAGER=false
BATCH_EXECUTION_MODE=celery

STORAGE_BACKEND=local
STORAGE_LOCAL_DIR=./storage
PUBLIC_BASE_URL=<UAT 对外可访问的 HTTPS 地址>

ADMIN_TOKEN=<独立管理员口令>
OPERATOR_TOKENS=alice:<操作员口令>,bob:<另一个操作员口令>
SETTINGS_SECRET_KEY=<32 字节 Fernet 主密钥>
SETTINGS_ENV_LOCK=false

SPEND_CURRENCY=USD
SPEND_MONTHLY_BUDGET_MICROS=<本轮 UAT 预算>
SPEND_WARN_RATIO=0.7
SPEND_CRITICAL_RATIO=0.9
PROVIDER_PRICE_BOOK=<按当前供应商合同填写的 JSON>
PRICING_VERSION=<本轮价目版本>
```

说明：

- 首轮可用 `STORAGE_BACKEND=local` 验证功能；类生产或多机 UAT 应换 S3/MinIO，并配置 `S3_*`。
- `PUBLIC_BASE_URL` 必须是厂商和测试人员都能访问的真实地址。仍为 localhost 时，不要打开任何 `*_SEND_PUBLIC_URLS=true`。
- 首先保持 `SETTINGS_ENV_LOCK=false`，验证后台设置的保存/恢复和来源标识；冻结 UAT 配置后改为 `true` 并重启，再确认环境变量项只读。
- `PROVIDER_PRICE_BOOK` 不知道价格时宁可留空并显示“未配价”，不要填 0 冒充免费。格式示例见 `backend/app/core/pricing.py`。
- 多机部署必须显式配置同一把 `SETTINGS_SECRET_KEY`；否则各节点无法解密彼此写入的设置。

### 3.2 第 2～3 阶段：无费用基线

```env
DEFAULT_PROVIDER=mock
EVALUATOR_BACKEND=mock
EXTRACTOR_BACKEND=mock
COPY_GENERATOR=template
CELERY_TASK_ALWAYS_EAGER=false
BATCH_EXECUTION_MODE=celery
```

这套配置不产生第三方费用，但只能证明系统编排，不证明 AI 质量和真实渠道能力。

### 3.3 第 4 阶段：真实图片生成 FASHN

```env
DEFAULT_PROVIDER=fashn
FASHN_API_KEY=<真实 UAT Key>
FASHN_TRYON_MODEL=tryon-max
FASHN_GENERATION_MODE=balanced
FASHN_RESOLUTION=1k
FASHN_OUTPUT_FORMAT=png
FASHN_SEND_PUBLIC_URLS=false
```

第一次只生成 1 张、最多 1 轮。确认费用与下载链路后，才提高候选数。完整配置与错误映射见 `docs/PROVIDER-FASHN.md`。

### 3.4 第 4 阶段：真实评分模型

```env
EVALUATOR_BACKEND=vision
VISION_MODEL_API_KEY=<评分模型 Key>
VISION_MODEL_BASE_URL=<端点>
VISION_MODEL_NAME=<模型或 Endpoint ID>
VISION_MODEL_API_STYLE=responses
VISION_MODEL_RESPONSE_FORMAT=json_schema
VISION_MODEL_FAIL_CLOSED=true
VISION_MODEL_SEND_PUBLIC_URLS=false
```

API 形状和结构化输出能力按实际厂商调整，不能按厂商名猜。OpenAI、方舟、百炼示例见 `docs/VISION-EVALUATOR.md`。

### 3.5 第 4 阶段：真实属性抽取

```env
EXTRACTOR_BACKEND=vision
EXTRACTOR_MODEL_API_KEY=<属性模型 Key>
EXTRACTOR_MODEL_BASE_URL=<端点>
EXTRACTOR_MODEL_NAME=<模型或 Endpoint ID>
EXTRACTOR_MODEL_API_STYLE=responses
EXTRACTOR_MODEL_RESPONSE_FORMAT=json_schema
EXTRACTOR_MODEL_SEND_PUBLIC_URLS=false
EXTRACTOR_MODEL_SEND_IDEMPOTENCY_KEY=false
```

评分与属性配置故意分开，即使使用同一个厂商也要分别填写。第一次真实调用前保持 `EXTRACTOR_MODEL_SEND_IDEMPOTENCY_KEY=false`；只有确认网关接受该请求头后才能打开。

### 3.6 第 4 阶段：真实文案模型

```env
COPY_GENERATOR=llm
TEXT_MODEL_API_KEY=<文本模型 Key>
TEXT_MODEL_BASE_URL=<端点>
TEXT_MODEL_NAME=<模型名>
TEXT_MODEL_API_STYLE=chat_completions
```

先用 `template` 证明 Listing 流程，再切 `llm` 检查文案质量、claims 校验、版本与费用。

### 3.7 真实渠道配置

真实渠道 Adapter 的环境变量、签名方式、站点、店铺 ID 和沙箱凭证必须由该 Adapter 的接入文档和 `.env.example` 提供。当前仓库只定义了 Simulator，没有可据实填写的真实渠道配置项，本文不虚构变量名。

真实渠道阶段开始前必须同时满足：

- `/api/environment` 把渠道显示为 **REAL/真实渠道**，不是 Simulator。
- 前端 `/publish` 每一行能明确显示“真实渠道”。
- 渠道 spec 中没有 `TODO_`；生产/类生产启动不会报 `CHANNEL_SPEC_INCOMPLETE`。
- 凭证属于专用测试店铺，与生产店铺隔离。
- 已确认平台的幂等语义、创建/更新/轮询/下架端点和测试商品清理方式。

任一项不满足，只能完成到 Simulator 验收，不能写“真实自动上架通过”。

---

## 4. 阶段 0：冻结版本和自动门禁

### 4.1 冻结候选版本

记录：

```text
Git commit:
镜像 tag:
数据库迁移 head:
测试负责人:
冻结时间:
```

冻结后除阻断缺陷外不再合入功能。每次修复都生成新候选版本并重跑受影响阶段。

### 4.2 必须通过的自动门禁

1. CI 的 `all-green` 连续两次通过。
2. 本地或可信 runner 执行 `make check`，确认后端离线门禁与前端类型、lint、Vitest、build 全部通过。
3. 执行 Playwright：

   ```bash
   cd frontend
   npm ci
   npx playwright install chromium
   npm run e2e
   ```

4. 后端与前端生产镜像都能构建。
5. `make p0-gate ARGS=--verbose` 最终报告 P0-1～P0-6 全部“通过”，不得有“未验证”。

### 4.3 真库门禁的安全前提

真库 pytest 会清空测试库的 `public` schema。运行前同时确认：

```env
TEST_DATABASE_URL=postgresql+psycopg://<user>:<password>@127.0.0.1:5432/imagegen_gate_test
DATABASE_URL=postgresql+psycopg://<user>:<password>@127.0.0.1:5432/imagegen_gate_test
ALLOW_DESTRUCTIVE_TEST_DB=1
REDIS_URL=redis://127.0.0.1:6379/0
```

数据库名必须以 `_test` 结尾。屏幕上再次核对 URL 后再运行；绝不把这里的 URL 换成 `imagegen_uat` 或生产库。

### 4.4 通过标准

- CI 两次全绿有链接或截图。
- `make p0-gate` 没有失败、未验证、skip。
- Playwright 真实启动 Chromium 并通过，不是只安装了骨架。
- 两个 Dockerfile 都实际构建成功。
- Alembic `heads` 只有一个；测试库完成 `upgrade head → downgrade base → upgrade head`。

任何一项不满足：停止，不部署正式 UAT 候选版本。

---

## 5. 阶段 1：类生产 UAT 部署

### 5.1 部署

首次验证优先使用类生产编排，而不是 Vite 开发服务器：

```bash
cp .env.example .env
# 填写第 3 节配置
docker compose version
docker compose -f docker-compose.yml -f docker-compose.prod.yml config
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
```

Compose 必须 ≥ 2.24.4，因为生产 overlay 使用 `!override`。远程 UAT 应在 127.0.0.1:8080 前增加带 TLS 和访问控制的反向代理，不要直接暴露 backend、PostgreSQL 或 Redis。

### 5.2 基础设施检查

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml ps
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec backend alembic heads
docker compose -f docker-compose.yml -f docker-compose.prod.yml exec backend alembic current
curl -fsS http://127.0.0.1:8080/api/health
curl -fsS http://127.0.0.1:8080/api/health/ready
make worker-ping
```

通过标准：

- PostgreSQL、Redis、backend 健康；worker、beat、frontend 为 Up。
- `alembic heads` 与 `alembic current` 是同一个单一 head。不要在文档里写死迁移编号。
- ready 探针逐项确认 DB、Redis、存储可用。
- worker ping 在 10 秒内返回 pong。
- beat 只有一个实例。
- 类生产 frontend 由 Nginx 提供，backend 没有对宿主机发布 8000 端口，backend/worker/beat 没有源码 bind mount 覆盖镜像代码。

### 5.3 鉴权与配置安全

用无口令、操作员口令、管理员口令分别验证：

| 动作 | 无口令 | 操作员 | 管理员 |
|---|---|---|---|
| `/api/health` | 可访问 | 可访问 | 可访问 |
| 查看业务页面 | 拒绝 | 允许 | 允许 |
| 创建商品/任务/审核 | 拒绝 | 允许 | 允许 |
| 设置页读写、Provider 测试 | 拒绝 | 拒绝 | 允许 |

再确认：

- 设置页密钥只显示末位打码串，刷新后不回显明文。
- `SETTINGS_SECRET_KEY` 与存储目录分离。
- `SETTINGS_ENV_LOCK=true` 后，环境变量提供的项变为只读，数据库覆盖不生效。
- PostgreSQL、Redis 只绑定本机或私网，不暴露公网。
- 日志中搜索 token、Authorization、API key 和 base64，均无明文。

---

## 6. 阶段 2：无费用 Simulator 单件闭环

先按 `LOCAL_MANUAL_TEST.md` 跑一次仓库样例和 `make smoke`。注意 `make smoke` 只覆盖到生成、评分、成品图和导出，不覆盖审核后的 Listing/发布链路；完整闭环仍需继续执行本节。

如果 UAT 已启用口令，当前 `smoke_test.py` 不会自动带操作口令。可在独立的无口令 local 环境运行它；不要为了让脚本通过而关闭共享 UAT 的鉴权。

### 6.1 建档与素材

1. 打开 `/spus/new`，按三步建档创建 1 个 SPU、至少 2 个颜色、每色至少 2 个尺码。
2. 在商品/素材页面上传正面、背面、细节图，确认 owner、颜色、角色、来源和授权信息正确。
3. 准备并启用一张已授权模特模板。

通过标准：

- SPU → 颜色变体 → SKU 的身份关系清楚，刷新后不丢。
- 同图重复上传命中去重，但不会跨错误 owner 静默合并。
- AI 生成图不被当作商品事实证据。
- 缺授权、证据不足或归属不清时明确阻断，不能靠手工改库继续。

### 6.2 Mock 图片生成、评分与审核

1. 创建生成任务，Provider 选 `mock`。
2. 分别走自动 A 档和“始终不达标 → 人工审核”两条路径。
3. 在 `/reviews` 对照原图与候选图，执行批准、驳回、重生。

通过标准：

- 环境横幅明确显示 Mock/模拟，不冒充真实。
- 任务状态会推进；进行中页面持续轮询，等人处理的状态不会被错误判成终态。
- 候选图有分档、维度分、硬错误和审计记录。
- 原图/生成图并排、400% 缩放与同步平移可用。
- 批准后产生多尺寸成品，刷新后仍存在。

### 6.3 属性、图片集、文案和草稿

在 `/workbench/:id` 依次完成：

1. 属性：执行 Mock 抽取；逐字段检查证据、置信度与来源；确认或改正；高风险未知值必须阻断。
2. 图片集：按颜色/角色生成图片集；检查颜色-SKU-图片映射；批准图片集。
3. 文案：先用 `template` 生成；检查标题、卖点、描述、关键词和 claims；编辑后批准。
4. 草稿：生成 ListingDraft；补齐渠道必填字段；检查 stale/上游快照；预览并导出。

通过标准：

- 页面顶部只给一个真实下一步；阻断原因能跳到正确页签。
- 属性事实、图片映射、文案 claims 都能追溯到来源。
- 改上游内容后草稿变 stale，旧草稿不能继续发布。
- 同内容重复生成的指纹稳定；改内容后指纹变化。
- 必填字段缺失时干跑和真实提交都被阻断。

### 6.4 Simulator 发布主线

1. 在导出页点“发布到平台”，进入 `/publish?product_id=...`。
2. 填专用测试店铺与 `test_batch_tag`，先勾选干跑。
3. 检查干跑报文，不应出现外部商品 ID，也不应产生真实外部调用。
4. 取消干跑并提交到 Simulator。
5. 查看 listing 详情中的草稿、attempt、outbox、安全请求/响应摘要。
6. 手动刷新状态或等待 beat 轮询到 `LISTED`。
7. 改一个有效字段，生成新草稿并执行 UPDATE；外部商品身份应保持一致。
8. 执行下架，最终到 `DELISTED`。

通过标准：

- 页面显眼显示“模拟”，干跑与提交外观不同。
- `display_status`、`next_action`、`blocking_reasons`、`allowed_actions` 与后端一致。
- 创建/更新使用不同幂等键；重复点击不会创建第二件商品。
- API 已受理不等于上架成功，审核中状态不会显示成已上架。
- 下架后刷新仍为已下架。

---

## 7. 阶段 3：Simulator 异常矩阵

Simulator 根据 Listing 报文中的 SPU 前缀选场景。为每个场景创建独立测试商品，不要复用同一草稿。

| SPU 前缀 | 模拟行为 | 预期结果 |
|---|---|---|
| 普通 SPU | CREATE_OK / UPDATE_OK | 同步创建/更新成功 |
| `SIM-CONFLICT-` | 幂等冲突 409 | 当作“平台已有该商品”的可恢复成功，保存既有 external ID，不重复创建 |
| `SIM-RATELIMIT-` | 429 + Retry-After | 保持排队并按退避重试；耗尽后给出真实下一步，不假装成功 |
| `SIM-AUTH-` | 鉴权失败 401 | 提交失败、不可盲目重试，提示检查凭证 |
| `SIM-FIELD-` | 字段拒绝 422 | 错误落到具体字段并回流修复 |
| `SIM-REVIEW-` | 异步审核 | 先审核中；约 60 秒后轮询为已上架 |
| `SIM-LIVE-` | 异步发布成功 | 先受理；约 60 秒后为已上架 |
| `SIM-REJECT-` | 平台驳回 | 约 60 秒后进入驳回台账，定位到图片问题与修复页签 |

每个场景都验证：状态、下一步、允许操作、审计记录、刷新后持久化、重复提交行为。

另外必须覆盖：

- 干跑不会投递。
- 同一幂等键连续提交两次，只有一条外部商品记录。
- CREATE 成功后修改草稿并 UPDATE，外部 ID 不变。
- 驳回后修复、生成新草稿、重新提交，驳回闸能自动关闭；若任务 20-B 尚未完成，只能记为已知阻断，不能手工标通过。
- `SUBMIT_RESULT_UNKNOWN` / DEAD 的 reconcile、redeliver 出口必须用正式故障注入或已有集成测试制造。不要直接改数据库伪造状态。

阶段通过标准：全部可构造场景符合预期，重复创建为 0，前端没有自己猜状态。

---

## 8. 阶段 4：逐个接真实外部能力

每接一个能力，都先记录切换前配置、切换后配置和首个调用 ID；成功后再接下一个。

### 8.1 FASHN

顺序：

1. `/providers` 测试 FASHN 连接和额度；只读，不产生生成费用。
2. 运行 1 张、1 轮基线：

   ```bash
   docker compose exec backend python -m app.scripts.provider_baseline \
     --sku SW-001-BLK-S --providers fashn --candidates 1 --max-rounds 1 --yes
   ```

3. 确认真实候选图下载、MIME、尺寸、EXIF、metadata、prediction ID 和 credits。
4. 再运行 `mock,fashn` 同样本对比；仍限制 1 轮。

通过标准：Provider 和环境横幅显示真实；不是 Mock 图；厂商后台调用数、响应头用量和本系统 `provider_usage_records` 能解释一致。若 `x-fashn-credits-used` 缺失或语义不同，记录为 `inferred`，不得假装精确。

### 8.2 真实评分模型

1. 选 10 张已有人工作为 A/B/C/D 判断的候选图。
2. 切 `EVALUATOR_BACKEND=vision`，确认环境横幅变化。
3. 对同一批图跑 FULL/QUICK，检查模型名、prompt version、原始响应安全摘要和 token/调用台账。
4. 制造一次结构化输出降级：`json_schema → json_object → prompt_only`，确认厂商实际支持哪档。
5. 制造或观察输出截断，必须报“输出被截断”，不能只报 JSON 非法。
6. 配错 Key 后确认 fail-closed：任务转人工，不退回 Mock 自动通过。
7. 用 50～100 张人工样本完成阈值校准；每次换模型、推理强度或提示词都重跑。

通过标准：10 张均有非 Mock 评分；后端自行算总分和分档；模型自造的未知硬错误不会直接生效；人工与模型分档一致率达到本轮预定阈值。

### 8.3 真实属性抽取

1. 用 5 件真实商品，每件含正面、背面、细节证据图。
2. 切 `EXTRACTOR_BACKEND=vision`，逐件执行属性识别。
3. 检查每个字段的值、证据、置信度、missing_reason、模型/提示词版本和来源。
4. 验证 AI 生成图不会进入事实识别输入。
5. 对证据不足的高风险字段，结果必须是未知并阻断发布，不能猜。
6. 核对供应商调用数与 `operation='attribute_extract'` 的台账行和 `provider_attempts`。

通过标准：高风险字段准确率先达到 ≥95%，一般字段 ≥85%；调用次数可解释；配错配置时状态条显示未配置并明确报错，不回退 Mock。

### 8.4 真实文案模型

1. 先保存 5 件 template 文案作为基线。
2. 切 `COPY_GENERATOR=llm`，对同样事实重新生成。
3. 检查语言、标题长度、卖点、关键词、禁止声明、claims 和来源追溯。
4. 只重生一个字段，确认其他人工修改不被覆盖。
5. 同一 SPU 的多个尺码不应对相同输入重复发多次 LLM 调用。

通过标准：5 条都通过规则校验；文案模型与 prompt version 有留档；事实错误为 0；调用与费用台账可解释。

---

## 9. 阶段 5：真实模型 + Simulator 完整单件链路

现在同时打开已经分别验证过的 FASHN、真实评分、真实属性和真实文案，但渠道仍用 Simulator。

选 1 件真实商品完整执行：

```text
SPU/颜色/SKU 建档
→ 上传并确认真实证据图
→ FASHN 真实生成
→ 真实视觉评分
→ 人工图片审核
→ 真实属性识别与人工确认
→ 图片集映射与批准
→ LLM 文案与批准
→ ListingDraft 与渠道校验
→ Simulator 干跑
→ Simulator CREATE
→ 轮询到 LISTED
→ 修改并 UPDATE
→ DELIST
```

通过标准：

- 每一段的环境标识都诚实；渠道仍明确显示 Simulator。
- 页面给出的唯一下一步能把运营带到正确位置。
- 所有真实外部调用都有调用 ID、模型/版本、结果、用量和费用记录。
- 任何一步失败后能从该步恢复，不要求整件从头重来。
- 页面刷新、重新登录、worker/beat 正常重启后数据与状态不回滚。

---

## 10. 阶段 6：小批量与故障恢复

### 10.1 无费用 10 件批次

先用 Mock/模板 + Simulator 跑 10 件：

1. 批次计划页先显示可执行/不可执行数量和预计动作。
2. 创建批次后由 Celery 执行，不能在 HTTP 请求里同步跑。
3. 注入 1 件明确失败，其余 9 件应继续完成。
4. 只重试失败的 1 件，成功的 9 件不动。
5. 重复投递同一批次，已成功回执不重复执行。
6. 成功项继续进入发布，失败项留在异常列表并给出下一步。

### 10.2 真实服务小批次

成本确认后，用 3～5 件真实商品跑真实生成/评分/属性/文案。先设本轮预算与告警，不直接上 20 件。

核对：

- 厂商后台调用数。
- `provider_usage_records` 的业务调用数、`provider_attempts`、billable units、units_source。
- `/spend` 的金额与未配价提示。
- 重试过的调用没有被少记；preflight 失败没有被误记成已计费成功。

### 10.3 Worker 重启演练

仅在可丢弃 UAT 环境执行：

```bash
docker compose restart worker
```

在批次运行中重启 worker，验证：

- 已完成项不重跑。
- 正在执行项在租约到期后被回收，或明确进入可处理失败。
- 批次最终不永久停在 RUNNING。
- 没有第二条 billable 调用；若真实厂商已受理但结果未知，应进入人工闸，不得盲目再付费。

当前默认条目租约可能长达一小时。应为演练预留完整等待时间；不要在共享 UAT 中临时改小租约或直接改 `lease_until`。

### 10.4 Redis/派发恢复演练

仅在可丢弃 UAT 环境、无真实付费调用在途时执行：

```bash
docker compose stop redis
# 通过正式页面创建一个无费用任务/批次，记录表现
docker compose start redis
make worker-ping
```

通过标准：派发失败不被报成业务成功；Redis 恢复后 outbox/兜底任务能继续派发；没有重复执行。

### 10.5 阶段通过标准

- 10 件批次跑通；1 件失败不污染另外 9 件。
- 失败项可独立重试；成功项可继续发布。
- 重复 Celery 投递不产生重复付费。
- worker/Redis 故障后最终状态明确，没有永久假活。
- 真实用量与费用差异有书面解释。

---

## 11. 阶段 7：真实渠道干跑

真实渠道配置完成后，先重新部署并检查 `/api/environment` 与 `/publish` 的“真实渠道”标识。

选 3 件通过 Simulator 的商品，只执行干跑：

1. 逐字段对照官方文档、测试店铺类目、枚举、长度、图片 URL、币种、库存与 SKU 结构。
2. 检查 CREATE 和 UPDATE 报文路径、方法和幂等键不同。
3. 检查凭证、签名、Authorization、密钥不出现在安全快照、日志和审计表。
4. 将报文交给渠道负责人或沙箱校验器确认。
5. 对 1 件制造必填字段缺失，必须在本地校验阶段阻断，不发送请求。

通过标准：3 件干跑全绿；渠道负责人确认字段含义；无密钥泄漏；没有任何真实商品被创建。

真实平台不提供某种沙箱失败模式时，必须在报告中写“真实平台未验证该错误，仅 Simulator/自动化通过”，不能用 Simulator 结果替代真实平台证据。

---

## 12. 阶段 8：最小真实发布

本阶段会改变外部平台状态并可能产生费用。执行前由渠道负责人确认测试店铺、测试类目、批次号、预算和清理责任人。

### 12.1 创建 1 件

1. 选 1 件已通过干跑的商品。
2. 再次确认页面显示“真实渠道”、正确店铺、站点、语言和 `test_batch_tag`。
3. 提交 CREATE，只点一次。
4. 记录本地 listing/attempt/outbox ID、幂等键摘要、request ID、external SPU/SKU ID。
5. 去平台后台核对只有 1 件，字段、图片、变体与本地一致。

### 12.2 幂等与更新

1. 对同一未改内容再次提交。
2. 平台商品数仍为 1；本地沿用或识别既有提交，不产生第二件。
3. 修改一个安全字段，生成并批准新草稿，执行 UPDATE。
4. 平台同一 external SPU 内容变化，外部 ID 不变。

重复创建事故一旦出现，立即停止全部真实发布并回到幂等实现检查；这是零容忍问题。

### 12.3 轮询、驳回与未知结果

- 轮询：从已受理 → 审核中 → LIVE，全程不能把“API 200/202”显示成已上架。
- 驳回：若测试店铺支持，制造 1 次可控驳回，确认自动进入异常/驳回台账；修复后新提交能关闭旧闸。
- 超时未知：只使用渠道沙箱或正式故障注入开关。确认 `SUBMIT_RESULT_UNKNOWN` 后先 reconcile，不直接 CREATE。若没有安全注入手段，记为真实渠道未验证，保留 Simulator/集成测试证据。
- 404：绝不能自动写成已下架。

### 12.4 下架并核对

在 `/publish` 对该商品执行 DELIST；确认平台商品已下架，本地到 `DELISTED`，再次轮询不会反向变成 LIVE。

阶段通过标准：创建 1、更新 1、平台状态闭环 1、重复创建 0、下架 1；测试店铺最终无残留商品。

---

## 13. 阶段 9：正式 UAT

### 13.1 覆盖量

- [ ] 至少 30 件真实商品走完全链路。
- [ ] 至少 3 个批次，每批 5～20 件。
- [ ] 至少 5 件经历“平台驳回 → 修复 → 重提交 → 通过”。
- [ ] 至少 3 件执行 UPDATE。
- [ ] 至少 2 次故障演练：Provider 超时、worker 重启。
- [ ] Chrome 与 Firefox 各完成一次关键流程和文件下载。
- [ ] 1366px 分辨率下关键页面无不可接受的横向滚动。

### 13.2 建议退出阈值

开始 UAT 前由业务与运营书面确认阈值，期间不得为了通过而调低：

| 指标 | 建议阈值 | 算法 |
|---|---:|---|
| 图片一次通过率 | ≥60% | 首轮人工批准数 / 首轮生成数 |
| 高风险属性准确率 | ≥95% | 人工核对正确字段 / 已核字段 |
| 一般属性准确率 | ≥85% | 同上 |
| 目标语言文案可直接使用率 | ≥70% | 无需改写的文案 / 已审文案 |
| 平台提交成功率 | ≥90% | 成功提交 / 排除平台自身故障后的提交 |
| 重复创建事故 | 0 | 平台商品数与本地记录核对 |
| 重复扣费事故 | 0 | 厂商账单与 billable 调用核对 |

后两项零容忍，出现一次就停止测试并回到发布/付费幂等修复。

### 13.3 运营可用性

让未参与开发的运营完成以下任务，不给口头提示：

1. 找出今天最先该处理的 3 件商品。
2. 解释一件商品卡在哪里、为什么、下一步是什么。
3. 审一张图、一个属性冲突、一份文案。
4. 从工作台进入发布页，区分干跑、Simulator、真实提交。
5. 处理一条平台驳回。
6. 找到某次付费调用和操作审计。

如果运营需要开发者解释按钮含义或必须打开数据库才能继续，该项失败。

---

## 14. 阶段 10：清理、费用与退出

### 14.1 先列清单，不动外部平台

```bash
make cleanup TAG=uat-20260808-r1 CMD=inventory
make cleanup TAG=uat-20260808-r1 CMD=verify
make cleanup TAG=uat-20260808-r1 CMD=delist
```

第三条默认只打印计划。先核对总数、真实/模拟数、将排队数、跳过数和无法自动下架数。

### 14.2 确认作用域后执行下架

```bash
make cleanup TAG=uat-20260808-r1 CMD=delist APPLY=1
make cleanup TAG=uat-20260808-r1 CMD=verify
```

破坏性清理必须带 `TAG` 或 `SHOP`。只给 `CHANNEL` 不允许 `APPLY=1`，不要绕过这道保护。

对 `SUBMIT_RESULT_UNKNOWN` 且没有 external SPU ID 的行，脚本会给出 locator。必须由渠道负责人按店铺、SPU、批次和时间窗去平台后台人工核对；这类行未核清时，不能写“清理完成”。

### 14.3 费用核对

分别导出或截图：

- 厂商后台：FASHN、评分模型、属性模型、文本模型实际用量和费用。
- 本系统 `/spend`：调用数、provider attempts、billable units、units_source、金额、未配价数。
- 差异解释：共用 Key、赠送额度、失败但计费、币种、厂商响应头缺失或累计口径。

本系统页面是本地台账，不是厂商余额。两边不一致必须解释，不能强行改本地数字对平。

### 14.4 最终退出条件

- [ ] 自动门禁与 P0 证据归档。
- [ ] 第 13 节覆盖量和阈值达到。
- [ ] 所有阻断上线缺陷清零。
- [ ] “上线后修”有负责人和日期；“不修”有书面理由。
- [ ] 外部测试商品全部下架，平台后台与本地清单数量一致。
- [ ] 不存在未核清的结果未知商品。
- [ ] 测试数据与生产数据隔离确认。
- [ ] 真实用量、费用与预算核对完成。
- [ ] 配置、截图、日志和报告中没有密钥。
- [ ] Go/No-Go 结论由发布负责人、运营和渠道负责人签字。

---

## 15. 快速判定：哪个环节必须先走通

如果只记住一页，按下面执行：

1. **先让 CI、P0、镜像、Playwright、真库迁移全通过。** 否则不是人工测业务，而是在替自动门禁捡漏。
2. **再让类生产部署的 health/ready、worker ping、鉴权走通。** worker 不通时所有任务都会假排队。
3. **再用 Mock + template + Simulator 完成单件全链路。** 这一步不花钱，用来证明内部链路。
4. **然后分别验证 FASHN、评分、属性、文案。** 一次只接一个，先小样本。
5. **再把真实模型组合起来，但渠道仍用 Simulator。** 先证明内容能稳定产出 Listing。
6. **再跑 10 件批次和故障恢复。** 单件不等于批量可靠。
7. **最后才接真实渠道：先 3 件干跑，再真实创建 1 件。** 创建后立刻验证重复提交、更新、轮询和下架。
8. **正式 UAT 才扩到 30 件。** 结束时先清理、再谈通过。

任何阶段出现“重复创建”或“重复扣费”，立即停止后续阶段；任何真实外部能力仍显示 Mock/Simulator，也立即停止，不能继续写验收通过。

