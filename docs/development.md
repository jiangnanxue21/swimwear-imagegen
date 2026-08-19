# 开发指南

这份文档回答四件事:本机怎么把它跑起来、代码放在哪、门禁分几层、日常敲哪些命令。
设计取舍与踩过的坑不在这里 —— 那些在 [`DECISIONS.md`](DECISIONS.md) 和各模块的注释里。

## 环境准备

| 依赖 | 说明 |
| --- | --- |
| Python 3.12+ | 后端;纯逻辑测试只要有 `python3` 就能跑 |
| Node.js 22+ | 前端;`npm ci` 而不是 `npm install`(见下) |
| PostgreSQL 16 | 真库测试与本机开发;也可以只跑容器里的那一个 |
| Redis 7 | Celery broker + 结果后端 + 运行日志环形缓冲 |
| Docker Compose 2.24.4+ | `docker-compose.prod.yml` 用了 `!override` 标签 |

### 不用 Docker 的本机开发

```bash
# 后端
cd backend
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload

# 前端
cd frontend
npm install
npm run dev
```

需要本机 PostgreSQL 与 Redis;或只改 `DATABASE_URL` 指向已有实例。

设置测试库:

```bash
export TEST_DATABASE_URL=postgresql+psycopg://imagegen:imagegen@localhost:5432/imagegen_test
```

> **开发装依赖用 `npm ci`,不是 `npm install`。** 两者装出来的树可以不一样
> (`install` 会按 semver 悄悄升次版本并改写 lockfile),于是「本地好的、CI 红的」
> 这类问题没有任何线索。生产镜像一直是 `npm ci`;要故意升依赖时显式跑
> `npm install <pkg>` 并提交 lockfile。

## 目录结构

```
backend/app/api/         HTTP 路由;装配的唯一真相在 router.py
backend/app/core/        配置、枚举、错误、哈希、路径安全、日志分类法(零三方依赖)
backend/app/db/          引擎与会话
backend/app/models/      SQLAlchemy 模型
backend/app/schemas/     Pydantic 出入参
backend/app/services/    业务逻辑
backend/app/providers/   图像生成 Provider 抽象与各家适配器
backend/app/evaluators/  候选图评分器:打分、分档、修复策略、轮级决策
backend/app/prompts/     提示词注册表与版本机制(只依赖标准库与 app.core)
backend/app/workflows/   状态机与编排策略(纯函数层)
backend/app/tasks/       Celery
backend/app/attributes/  属性注册表、校准、置信度与运行态
backend/app/extractors/  属性抽取器(视觉 / mock)与调用预算
backend/app/media/       素材域:证据规则、样本完整度、来源冲突
backend/app/listings/    Listing:SKU 矩阵、文案、变体键、图片集、导出写出
backend/app/workbench/   运营工作台:流程判定、批次、平台、颜色维聚合
backend/app/channels/    渠道 Adapter 与字段 spec(spec/{category_id}.yaml)
backend/app/llm/         多模态与文本模型传输层、端点信任、脱敏、载荷旁挂库
backend/app/scripts/     一次性脚本:冒烟、校准、回填、清理、基线
backend/tools/           门禁与审计脚本(零依赖,不 import app 的那几个尤其)
frontend/src/            React + TS + AntD 后台
comfyui/                 工作流 JSON 与节点映射
sample-data/             示例商品与素材
docs/assets/             架构图册的 SVG 源文件
```

### 依赖方向由 import-linter 钉着

`backend/.importlinter` 里三条契约,`make arch-check` 跑:

| 契约 | 内容 |
| --- | --- |
| `core-is-the-bottom` | `app.core` 不许反向依赖 services / models / api / db / tasks / workbench |
| `grading-stays-pure` | 评分与修复(`scoring` / `rules` / `repair` / `decision`)不许碰 models / db / services / sqlalchemy |
| `channels-take-only-the-contract` | 渠道适配器只接受 `CanonicalProduct`,不直接查库 |

它建的是**完整依赖图**,看的是可达性而不是单跳 —— 因为没有人会直接写一条明显反向的
import,都是顺手引了个「工具模块」,而那个模块背地里连着数据库。

## 门禁分层

![门禁分层](assets/gates.svg)

```bash
make check-offline # 离线子集:纯逻辑用例 + 六道审计。不需要 node_modules,不需要网络
make test-nodb     # 不碰真库的全部 pytest 用例(需已装后端 dev 依赖)
make fe-check      # 前端全部门禁:npm ci → typecheck → lint → test → build → syntax-check(需联网)
make check         # = check-offline + test-nodb + fe-check
```

**`make check-offline` 跑绿 ≠ 全都过了。** 它覆盖不到前端类型、lint、Vitest 与构建 ——
那四层只有 `make check`(或单独的 `make fe-check`)会跑,而它们需要网络装依赖。
`check-offline` 目标自己会在末尾打印这句话。交付前两条都要跑。

**`make check` 跑绿仍然 ≠ CI 会绿。** 之后仍然没跑到的:

```
backend 的真库那一半   requires_db 用例 + Alembic 升降级   ← make test(要 PostgreSQL + Redis)
e2e                    Playwright                          ← make fe-e2e(要 npx playwright install)
images                 docker build ×2                     ← 要 docker daemon,本地无等价物
```

本地能跑的那一份**结构上比 CI 窄,而窄的方向永远是更松**。

### 测试分两层

- **`backend/tests/pure/`** —— 零三方依赖,同时兼容 pytest 与
  `backend/tools/run_pure_tests.py`,后者用于没装 pytest 的环境。覆盖哈希去重、
  路径穿越、上传校验、图片探测、CSV 导入、存储不变量、配置契约、日志脱敏与分类法、
  模型与迁移一致性、状态机转移表、Provider 契约与路由、幂等键、错误策略、
  评分权重与解析、A/B/C/D 分档、轮级决策、候选预排序、前后端契约、设置项契约与打码、
  提示词注册表、以及若干评审回归。
- **`backend/tests/test_*.py`** —— 需要真实 PostgreSQL,覆盖 ORM 约束、REST API、
  Alembic 升降级。没有可用数据库时自动 skip,不会让 CI 假红。

**纯测试里不许出现 `import pytest`。** `run_pure_tests.py` 在跑任何用例之前用 AST
检查这一条 —— 破了它,那批用例在只有 python3 的机器上会在导入期炸掉,而运行器会把它
记成一条普通失败:曾经有一个文件带着 `import pytest` 躺了很久,套件显示「只差一条」,
实际上那个文件里 9 个用例一个都没跑过。

### 六道审计脚本

它们不测业务,测的是「守卫本身还在不在」:

| 命令 | 盯什么 |
| --- | --- |
| `make verify-delivery` | 交付卫生 + 门禁接线:每条门禁在 `ci.yml` 里都有对应的**命令字面量** |
| `make verify-sample-data` | 样例数据自检(也是「示例条数」的唯一真值来源) |
| `make verify-imports` | `app.*` 的 import 是否都指向真实存在的东西 |
| `make audit-anchors` | 变异脚本的锚点还对不对(只解析,不执行) |
| `make audit-guards` | 守卫的窗口封不封闭(反向断言不许吃切窄的源码) |
| `make audit-doc-refs` | 文档与注释里的路径引用指不指得到东西 |
| `make audit-columns` | 每一列都答得出「谁写它」(落库无写入路径的列会被点名) |

> `audit-anchors` 只解析不执行是刻意的:变异脚本的循环写在模块顶层、没有 `__main__`
> 保护,import 它就是当场改写真实工作树。**审计工具把被审对象跑起来,本身就是事故。**

### CI

`.github/workflows/ci.yml` 六个 job:`gates` / `backend` / `frontend` / `e2e` /
`images` / `all-green`。分支保护挂 `all-green` 一个就够。

**门禁清单不许只写在文档里。** `verify_delivery.py` 会逐条在 `ci.yml` 里找命令字面量
(不是 step 名字)。加一条门禁 = 改三个地方:`Makefile`、`ci.yml`、
`check_ci_runs_every_gate()` 里的那张表。

## 日常命令

```bash
make up / down     # 启停全部服务
make logs          # 跟踪后端与 worker 日志
make migrate       # 执行迁移
make revision M="add xxx"   # 生成迁移脚本
make seed          # 导入示例 SPU / 颜色 / SKU 与素材
make psql          # 进数据库
make worker-ping   # 验证 Celery worker 存活
make smoke         # 生成链路冒烟:健康 → 素材 → 模特 → 生成 → 评分 → 成品图 → 导出
make baseline SKU=SW-001-BLK-S P=mock,fashn   # Provider 基线对比
make calibrate     # 评分器校准:人工判定 vs 模型分档的一致率
make requeue       # 找出并重新派发滞留任务(加 APPLY=1 真的派发)
make secret-key    # 生成设置页主密钥
make pack V=aNN    # 打交付包
```

`make smoke` **不覆盖审核之后的链路**(人工审核 / 图片集 / 文案 / 草稿 / 发布 / 轮询 /
下架)—— 那段由真库 pytest 做集成验证,交互路径按 [`../LOCAL_MANUAL_TEST.md`](../LOCAL_MANUAL_TEST.md) 手工走。

## 本地真实基础设施验证须由用户明确触发

日常协作默认只运行不依赖真实 PostgreSQL / Redis 的验证。**没有明确指令,不得**设置
真库测试环境变量、运行 `requires_db` 用例或 Alembic 真库升降级,也不得连接 Redis 做
PING、Celery worker / broker 或其他 Redis 集成验证。

如果改动的完整验收确实需要上述验证,本轮只做两件事:在交付说明中明确标为「未执行」,
并提醒需要验证的范围与建议命令。这条是本地协作执行约束,**不改变** CI、发布或阶段
验收原有的真库 / Redis 门禁,也不允许把未执行写成已验证。

## 环境变量

见 `.env.example`,按主题分组(应用、连接池与请求并发、数据库、Redis/Celery、存储、
上传限制、Provider、评分与视觉模型、属性识别、批量执行、文案、文本模型、后台设置页、
浏览器登录、费用与预算)。**分组数不写在这里** —— 以那个文件为准。

`backend/tests/pure/test_config_contract.py` 会静态校验 `.env.example` 与 `Settings`
字段一一对应,新增配置项时会自动提醒补文档。

## 提交前的自检清单

1. 改了后端逻辑 → `make check-offline`,必要时 `make test-nodb`
2. 改了前端 → `make fe-check`(只跑 offline 等于没验前端)
3. 改了文档里的路径引用 → `make audit-doc-refs`
4. 加了门禁 → 同时改 `Makefile` / `ci.yml` / `check_ci_runs_every_gate()`
5. 加了日志事件 → 在 `core/log_events.py` 登记,否则守卫双向变红
6. 加了会被 `build_facet` 读的注册表 → 去 `tests/pure/test_environment.py`
   的「注册表接缝」加一行
7. 改了 `CLAUDE.md` → 把整份拷进同级 `AGENTS.md`(守卫钉着逐字一致)
