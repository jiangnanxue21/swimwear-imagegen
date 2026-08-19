# 网站商品展示图自动生成系统

服装商品展示图的生产流水线:上传商品资料与原图 → 多 Provider 生成候选图 →
自动评分分档 → 自动重生或人工审核 → 输出网站可直接使用的多尺寸图片 URL。

[快速开始](#快速开始) ·
[架构图册](docs/ARCHITECTURE.md) ·
[文档索引](docs/README.md) ·
[能力现状](docs/STATUS.md) ·
[开发](docs/development.md) ·
[部署](docs/DEPLOYMENT.md)

![整条流水线](docs/assets/generation-pipeline.svg)

品类是参数,不是写死的:渠道字段 spec 按 `spec/{category_id}.yaml` 载入,属性注册表按品类校准。
**泳装是目前唯一已校准、且有渠道 spec 的品类**,所以样例数据与评分提示词里说的是泳装 ——
那是在描述这个品类,不是系统的边界。

## 状态

这个系统在**持续交付中**,各能力的成熟度不一样:Mock Provider 与 Mock 评分器可以
零外部依赖跑通全链路;FASHN 已按官方文档接入但未用真实 Key 验证;fal.ai 与 ComfyUI
仍是骨架,选中会在创建任务时被挡下。

> **想知道某项能力现在能不能用,从 [`docs/STATUS.md`](docs/STATUS.md) 开始。**
> 那份文档按能力逐条写明状态与已知限制,是本仓库里唯一负责回答「现在到底能不能用」的地方。

> ⚠️ **升级一个已在运行的部署之前,先读 [`docs/DECISIONS.md`](docs/DECISIONS.md) 第三节。**
> 那里按主题归并了全部升级须知:必须做的人工动作(主密钥轮换、`ADMIN_TOKEN`、beat 进程)、
> 原本能跑通但现在会被挡下的操作、以及几处不报错的看板口径变更。

## 快速开始

### 用 Docker 跑起来

```bash
cp .env.example .env
docker compose up -d --build
make migrate
make smoke      # 一分钟内告诉你闭环通不通
```

| 地址 | 内容 |
| --- | --- |
| http://localhost:5173 | 后台前端(Vite 开发服务器) |
| http://localhost:8000/api/health | 存活探针 |
| http://localhost:8000/api/health/ready | 依赖就绪探针(DB / Redis / 存储) |
| http://localhost:8000/docs | OpenAPI 文档,接口全量以它为准 |

导入示例数据(幂等,可重复执行。**条数不写在这里** —— 增删样例时写死的数字会静默过期,
要当前口径跑 `cd backend && python3 tools/verify_sample_data.py`):

```bash
python3 sample-data/generate_images.py   # 首次需先生成占位图
make seed
make worker-ping                          # 期望输出 {'pong': True, ...}
```

### 生成一张图试试

打开 http://localhost:5173 → 商品 → 任选一个 → 创建生成任务 → Provider 选 `mock` → 提交。

任务立即返回,后台自动跑完整闭环:每轮出 4 张候选 → 轮内预排序 → 评分 → A/B/C/D 分档
→ 达到 A 档就自动通过,否则淘汰并换 seed 重生 → 轮次耗尽仍不达标才转人工审核。

创建任务弹窗里有两个 Mock 专用旋钮,不需要任何外部服务就能演练各条分支:

| 旋钮 | 取值 | 用途 |
| --- | --- | --- |
| 模拟生成结果 | 成功 / 失败 / 超时 / 无候选 / 限流 / 内容安全 | Provider 侧的失败分支 |
| 模拟评分结果 | A / B / C / D / 硬错误 / 逐轮变好 / 始终不达标 | 分档、重生、转人工 |

想直接看到人工审核队列:选「始终不达标」,最多轮次设 2,提交后到 `/reviews` 即可。

这条闭环上有三条规则值得先知道:

**总分由后端按权重算,不采信评分器自报的数字。** 两者差值存进 `model_reported_overall`,
用来监控大模型打分漂移。Mock 评分器故意自报一个不同的数,等于给这条规则内置了活体探针。

**硬错误只淘汰候选,不终结任务。** 硬错误代码(`core/enums.HardFailCode`,按受众分组)
中任意一个出现即判 D;但只要还有轮次,任务就继续自动重生,**不立刻交给人工**。
人工审核的对象是**商品任务**,不是每一张低分候选图 —— 否则队列会被本可自动解决的废图淹没。

**A 档要同时过四条底线。** 总分之外,商品身份一致性、结构一致性、人体真实性、
网站可用性缺一不可。总分 96 但商品身份 88 的图判不到 A。阈值与权重都在数据库的
`RuleSet` 里,逐项见 [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md#3-轮级决策什么时候重生什么时候找人)。

**不需要任何第三方 API Key,也不需要视觉大模型。** 未配置 FASHN / fal.ai / ComfyUI 时
系统照常启动,对应 Provider 显示为「未配置」;没有外部评分模型时使用 Mock 评分器,
整条评分闭环照样跑通。但 **Mock 只是离线演练模式**:它按文件指纹给分,不能用来决定
图片上不上网站。接真实评分模型见 [`docs/VISION-EVALUATOR.md`](docs/VISION-EVALUATOR.md)。

### 类生产部署

```bash
docker compose -f docker-compose.yml -f docker-compose.prod.yml up -d --build
# 前端构建产物由 Nginx 托管,监听 127.0.0.1:8080;backend 不再对外发布端口
```

非本机环境(`APP_ENV` 不属于 local / dev / development)**必须配齐浏览器登录三项,
否则后端起不来**:

```ini
ADMIN_PASSWORD=换成一个真密码
OPERATOR_PASSWORD=换成另一个真密码       # 不能和上面相同
AUTH_SESSION_SECRET=                    # 至少 32 字符
```

```bash
python3 -c "import secrets;print(secrets.token_urlsafe(48))"   # 生成签名密钥
openssl rand -base64 32                                        # 生成设置页主密钥
```

完整部署步骤、Windows / macOS 上的三处坑、故障对照表见
[`docs/DEPLOYMENT.md`](docs/DEPLOYMENT.md)。

## 文档

| 想知道 | 看这份 |
| --- | --- |
| 各条流程长什么样 | [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) |
| 每一页、每一个接口是干什么的 | [`docs/user/guide.md`](docs/user/guide.md) |
| 本机开发、门禁、目录结构 | [`docs/development.md`](docs/development.md) |
| 某一块代码的边界与契约 | [`docs/subsystems/README.md`](docs/subsystems/README.md) |
| 怎么加一个 Provider / 品类 / 配置项 | [`docs/cookbook/README.md`](docs/cookbook/README.md) |
| 某项能力能不能用、已知限制 | [`docs/STATUS.md`](docs/STATUS.md) |
| 为什么这么设计、升级须知 | [`docs/DECISIONS.md`](docs/DECISIONS.md) |

全部文档的入口在 [`docs/README.md`](docs/README.md)。

## 开发

```bash
make check-offline   # 离线子集:纯逻辑用例 + 六道审计,不需要 node_modules 也不需要网络
make check           # = check-offline + test-nodb + fe-check,需联网
```

`make check-offline` 跑绿 ≠ 全都过了:它覆盖不到前端类型、lint、Vitest 与构建,
那四层只有 `make check`(或单独的 `make fe-check`)会跑,而它们需要网络装依赖。
门禁分层、每一层验不到什么、目录结构与日常命令,见
[`docs/development.md`](docs/development.md)。

用 Claude Code 或其他 agent 开工前,先读 [`CLAUDE.md`](CLAUDE.md)(`AGENTS.md`
与它逐字一致,由守卫钉着不许分叉)。

## 设计约定

- **UUID 主键**,所有实体统一。
- **统一错误体**:`{"error": {"code": "...", "message": "..."}}`,后端不向客户端返回数据库异常原文。
- **日志脱敏**:键名命中 `api_key/secret/password/token/authorization/credential` 的值一律记为 `***`;
  自由文本(message、异常堆栈)另走值级脱敏。
- **落盘路径由 sha256 推导**(`ab/cd/<hash>.jpg`),用户提供的文件名只用于展示,不参与路径拼接。
- **原始上传文件永不覆盖**;成品图重算不删旧图。
- **前端不推测状态**:后端返回 `display_status` / `next_action` / `blocking_reasons` /
  `allowed_actions`,前端只展示和触发。

## 未确认的第三方字段

FASHN 已按官方文档接入,不再有 TODO。以下内容**没有**凭记忆写入代码,全部标记为 `TODO`:

| Provider | 待确认 |
| --- | --- |
| fal.ai | 使用哪个 model endpoint、该 endpoint 的输入 schema、队列与轮询路径 |
| ComfyUI | 服务地址、真实工作流 JSON、各输入节点的真实 ID |

在填写之前,这两个 Provider 的 `submit()` 会抛 `NotImplementedError` 或
`NotConfiguredError`,**不会发出任何真实请求,也不会产生费用**。
