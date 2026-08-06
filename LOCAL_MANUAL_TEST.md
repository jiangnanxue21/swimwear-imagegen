# 本地手工测试指南

目标状态：**本地 Docker 起来之后，用浏览器把 Mock 主链路走完一遍。**

不在范围内：接真实 SHEIN、接真实生成 Provider、生产级部署、性能与并发压测。
本文所有步骤都跑在 `DEFAULT_PROVIDER=mock` / `EVALUATOR_BACKEND=mock` /
Simulator 渠道上，**不会产生任何外部调用与费用**。

> ⚠️ **本文档尚未在真实 Docker 环境中端到端验证过。**
> 编写它的环境没有 Docker、没有网络、没有 PostgreSQL，能验证的只有静态部分
> （compose 语义、迁移链、镜像依赖、认证分支、脚本入口）。
> 第 0 节列出的就是「照着做的时候最可能先崩的地方」——它们是推导出来的，
> 不是跑出来的。第一次执行的人请按第 9 节回填。

---

## 0. 第一次执行前，先看这三条

按经验，全新环境卡住基本都在这三处，都不需要改数据库：

| 现象 | 原因 | 处理 |
|---|---|---|
| `docker compose up` 立刻报 `env file .env not found` | `.env` 在 `.gitignore` 里，仓库只有 `.env.example` | **用 `make up`，不要直接 `docker compose up`**。`make up` 依赖 `make init`，它会 `cp .env.example .env` |
| `worker` / `beat` 一直不启动，`docker compose ps` 里 backend 显示 `starting` 或 `unhealthy` | 两者都是 `depends_on: backend: condition: service_healthy`。backend 的探针打 `http://localhost:8000${API_PREFIX:-/api}/health` —— 改过 `API_PREFIX` 而没同步的话它永远不通 | 先 `make logs` 看 backend。探针失败时 worker/beat **不会**报错，只是不出现 |
| 前端容器起得很慢（几分钟） | `command` 是 `npm ci && npm run dev`，每次 `up` 都重装依赖树 | 正常。等它。`docker compose logs -f frontend` 看到 Vite 的 `ready in` 才算好 |

---

## 1. 环境要求

| 项 | 版本 | 说明 |
|---|---|---|
| Docker Engine | ≥ 24 | 需要 `docker compose` v2 子命令（不是 `docker-compose`） |
| Docker Compose | v2 | compose 文件用了 `depends_on.condition`，v1 不支持 |
| GNU Make | 任意 | 只是命令别名，不装也行，照抄 Makefile 里的命令即可 |
| 磁盘 | ≥ 5 GB | 镜像 + pgdata + storage 卷 |
| 内存 | ≥ 4 GB | 6 个容器，Vite 开发服务器最占 |
| 网络 | 首次构建需要 | 拉基础镜像、`pip install`、`npm ci`。**之后可离线** |

浏览器：Chrome / Firefox 任一。**建议至少用 Firefox 跑一次导出下载**——
`saveBlob` 的 object URL 吊销时机在 Chrome 上永远看不出问题（见 §7 已知限制）。

主机不需要装 Python、Node、PostgreSQL。全部在容器里。

---

## 2. 启动

```bash
# 在仓库根目录
make up          # = make init（生成 .env）+ docker compose up -d --build
```

首次大约 5–15 分钟（构建后端镜像 + `npm ci`）。

### 确认六个容器都健康

```bash
docker compose ps
```

期望：

```
postgres   Up (healthy)
redis      Up (healthy)
backend    Up (healthy)
worker     Up
beat       Up
frontend   Up
```

`worker` / `beat` / `frontend` **没有** healthcheck，显示 `Up` 就是正常的；
判断它们真的活着要看日志和 §3 的那次 ping。

```bash
make logs                      # backend + worker 跟随日志
docker compose logs beat       # beat 应该在按节拍打点
docker compose logs frontend   # 应该有 Vite 的 "ready in xxx ms"
```

**如果 worker / beat 压根没出现在 `ps` 里**，去看第 0 节第二行。

### 端口

| 服务 | 地址 | 备注 |
|---|---|---|
| 前端 | http://localhost:5173 | **从这里进** |
| 后端 API | http://127.0.0.1:8000/api | 只绑本机 |
| PostgreSQL | 127.0.0.1:5432 | 只绑本机 |
| Redis | 127.0.0.1:6379 | 只绑本机 |

从**另一台机器**测试时只开 5173：前端走 Vite 代理（`VITE_PROXY_TARGET`）
访问后端，同源、无 CORS。不要去改 `VITE_API_BASE_URL` 指向本机 IP，
那会同时撞上 CORS 白名单。

---

## 3. 初始化

```bash
# 1) 迁移（backend 启动命令里已经跑过一次，这里是确认幂等）
make migrate

# 2) 灌样例数据：10 个 SKU + 30 张图
make seed

# 3) 确认 worker 真的在消费队列（不是只是进程活着）
make worker-ping
```

`make worker-ping` 应该在 10 秒内打印 `pong`。**超时就说明 broker 链路断了**，
后面所有生成任务都会永远停在排队中——先解决它再往下走。

### 迁移升降级验证

```bash
docker compose exec backend alembic current      # 期望 0030 (head)
docker compose exec backend alembic downgrade -1
docker compose exec backend alembic upgrade head
docker compose exec backend alembic current      # 仍然 0030
```

链路是 `0001 → 0030` 单 head，每一版都有非空 `downgrade()`。
**做完这一步请重新 `make seed`**——降级会删表，样例数据不一定还在。

---

## 4. 测试口令

默认 `.env` 里 `ADMIN_TOKEN=` 和 `OPERATOR_TOKENS=` 都是空的，
配合 `APP_ENV=local`，`api/deps.py` 走 `ROLE_DEV` 分支——**不需要任何口令，
全部放行**。想快速走通链路就保持默认。

### 但是：验证权限相关的修复必须配口令

`regenerateFile` 的 403（A45-#1）在**空口令环境下复现不出来**，因为
`ROLE_DEV` 会把所有请求都放行，缺不缺 `X-Admin-Token` 都一样过。
要真正验证它，编辑 `.env`：

```ini
ADMIN_TOKEN=admin-local-test
OPERATOR_TOKENS=tester:op-local-test
```

然后 `docker compose up -d backend worker beat` 重启这三个容器。

浏览器里到 **设置页** 填入两个口令（前端存在 localStorage，
操作口令走全局拦截器，管理口令由 `adminHeaders()` 按需附加）。

| 口令 | 值 | 用途 |
|---|---|---|
| 操作口令 | `op-local-test` | 传商品、建任务、审图等日常写操作 |
| 管理口令 | `admin-local-test` | 改设置、改提示词、测 Provider、**重新生成上架文件** |

---

## 5. 测试数据

`make seed` 灌入 `sample-data/`：

- `products.csv` — 10 个泳装 SKU（`SW-001` … `SW-010`），含颜色/尺码维度
- `images/` — 每个 SKU 三张：`_front` / `_back` / `_detail`，共 30 张

需要另外造数时用 `sample-data/generate_images.py`。
**不要手工往库里插数据**——如果某条链路非得手改库才能继续，那是 bug，记进 §9。

---

## 6. 手工测试步骤

从 http://localhost:5173 开始。每一步都标了「怎么算过」。

### 6.1 商品导入

1. 商品列表页 → 导入，上传 `sample-data/products.csv`
2. ✅ 10 个 SKU 入列，变体维度（颜色/尺码）解析正确
3. ✅ **刷新页面，数据还在**（验收项）

### 6.2 素材上传

1. 进任一 SKU 详情 → 上传 `sample-data/images/` 下该 SKU 的三张图
2. ✅ 缩略图出得来，front/back/detail 角色标注正确
3. ✅ 媒体库页能看到这三张

### 6.3 生成

1. 该 SKU → 新建生成任务，Provider 选 **mock**
2. ✅ 任务进入排队，**worker 日志里出现这条任务**
3. ✅ 状态推进到完成，候选图出现

> 卡在排队中不动 = worker 没消费。回 §3 的 `make worker-ping`。

### 6.4 评分

1. 任务详情 → 查看评分（`EVALUATOR_BACKEND=mock`）
2. ✅ 每张候选图有分数与档位（GradeTag）
3. ✅ 评分明细（EvaluationDetail）打得开

### 6.5 审核

1. 审核队列页 → 打开这个任务
2. 通过一张、驳回一张（驳回要填理由）
3. ✅ 状态各自变更，审计日志页能看到两条记录、操作者名字正确
4. ✅ **刷新后状态不回滚**

### 6.6 成品输出

1. 通过的图应该产出成品资产（output asset）
2. ✅ 商品详情页能看到成品
3. ✅ 单件导出能下载成功

### 6.7 工作台草稿

1. 工作台 → 选中这个 SKU → 建批次
2. 走一遍页签：概览 / 属性 / 素材 / 图集 / 文案 / 草稿 / 导出
3. ✅ 图集按规则编排，文案生成有内容
4. ✅ 草稿页展示待发布内容，校验项能过
5. ✅ **导出页点「生成上架文件」→ 再点「下载」，zip 能存下来**
6. ✅ 用 §4 的口令模式时，点**「重新生成」**（填理由）→ **不报 403**
   ← 这是 A45-#1 的验收点

### 6.8 Simulator 发布

1. 草稿页 → 发布到 **Simulator** 渠道
2. ✅ 生成发布回执，状态进入已提交
3. ✅ 发布记录页能查到

### 6.9 轮询

1. beat 的 `poll_listings` 会自动推进状态（不用手动触发）
2. ✅ 一两个节拍内状态从「已提交」推进到「已上架」
3. ✅ `docker compose logs beat` 里能看到轮询打点

### 6.10 下架

1. 已上架的记录 → 下架
2. ✅ 状态变为已下架
3. ✅ **刷新后仍是已下架**

### 6.11 Worker 重启后的行为（验收项）

在 6.3 生成任务**跑到一半时**：

```bash
docker compose restart worker
```

✅ 期望二选一，**都算通过**：
- 任务被重新领取、继续跑完；或
- 任务明确失败、界面上给出失败原因

❌ 不接受：永远停在运行中、界面上看起来是活的但什么都没发生。

> `run_batch_task` 已按 A45-batch9 加了 `autoretry_for=(OperationalError,)`；
> 批次条目的租约由 `reap_batch_leases`（60 秒节拍）回收，
> **但回收时机取决于 `lease_until`（默认 1800 秒），不是 60 秒**。
> 也就是说重启后最长可能要等半小时才看到条目被回收——
> 这不是卡死，是设计值。等不及就把 `.env` 里的租约时长调小重测。

---

## 7. 已知限制

**功能范围**
- 只跑 Mock：`DEFAULT_PROVIDER=mock`、`EVALUATOR_BACKEND=mock`、渠道 Simulator。
  真实 SHEIN 与真实生成 Provider 均未接入，不在本轮范围。
- `app/scripts/smoke_test.py`（`make smoke`）只覆盖到**第 5 步评分**，
  审核之后的链路（6.5–6.10）**没有脚本，只能手工点**。

**配置生效**
- 设置页改的值通过 `settings_runtime` 的 TTL 缓存生效，
  **默认 10 秒**（`SETTINGS_CACHE_TTL_SECONDS`），worker 是另一个进程，同样等 TTL。
  改完立刻测会看到旧值——**等 10 秒，不需要重启 worker**。
- 例外：`SETTINGS_ENV_LOCK=true` 时，`.env` 里已给值的键**在设置页改了也不生效**，
  这是刻意的。默认 `false`。
- `DOWNLOAD_ALLOWED_HOSTS` 已统一走 `provider_setting`（A45-#31），
  三条下载路径（素材/评分取图/候选图下载）行为一致，同样受上面这条 TTL 约束。

**浏览器**
- 导出下载的 object URL 在 60 秒后才吊销（A45-#22）。
  Chrome 同步取字节，改坏了也看不出来；**Firefox / Safari 才能证伪**。
  自动化侧由 `frontend/tests/component/download.test.ts` 盯着。

**其他**
- `beat` 只能有一个实例，`docker compose up --scale beat=2` 会重复排产。
- 前端容器每次 `up` 都跑 `npm ci`，慢是正常的。
- `.env` 不进版本库，换机器要重新 `make init`。

---

## 8. 停止与清理

```bash
make down          # 停容器，保留数据卷（pgdata / storage / secrets）
```

```bash
make clean         # = docker compose down -v，删掉全部数据卷
```

⚠️ `make clean` 会一起删掉 `secrets` 卷里的设置页主密钥。
删掉之后，之前存进数据库的加密配置（API Key 等）**再也解不开**。
本地 Mock 测试无所谓；如果在这个环境里存过任何真 Key，先备份。

从干净状态重来：

```bash
make clean && make up && make migrate && make seed && make worker-ping
```

---

## 9. 第一次执行后请回填

本文档的启动/初始化部分尚未在真实 Docker 里跑过。执行时请记录：

- [ ] `make up` 之后六个容器是否都到位？花了多久？
- [ ] `make worker-ping` 是否 10 秒内返回 `pong`？
- [ ] `alembic downgrade -1` → `upgrade head` 是否干净通过？
- [ ] 6.1–6.10 哪一步先断的？错误信息原文
- [ ] **有没有出现「必须手工改数据库才能继续」？** 有就是阻塞级 bug，单独提
- [ ] 6.11 的 worker 重启，实际等了多久看到结果？

有偏差直接改本文，不要另起一份。
