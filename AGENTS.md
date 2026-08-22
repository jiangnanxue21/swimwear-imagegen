# CLAUDE.md — 仓库总纲

**服装 AI 图片生成与 API 自动上架系统。** 后端 FastAPI + Celery + PostgreSQL,
前端 React 18 + Vite + antd,图片走 Provider(FASHN / ComfyUI / mock),
产出 Listing 后经渠道 Adapter 自动上架。

**范围是服装,不是只有泳装。** 品类是参数:渠道字段 spec 按
`field_spec(category_id=...)` 读 `spec/{category_id}.yaml`,属性注册表按品类校准。
泳装是**目前唯一已校准、且有渠道 spec 的品类**,所以代码、样例数据、评分提示词里
出现「泳装 / swimwear」的地方多数是在如实描述那个品类,不是待清理的旧称。
判据与三类不动的东西见 `docs/DECISIONS.md` §3.20。

## 开工先读什么

| 要做的事 | 先读 |
| --- | --- |
| 任何改动 | [`docs/REVIEW.md`](docs/REVIEW.md) —— 施工方案,第 12 章任务表与依赖图。它是验收口径,`backend/tools/verify_delivery.py` 有一半检查直接引用它的节号 |
| 判断某项能力能不能用 | [`docs/STATUS.md`](docs/STATUS.md) —— 正文在文件开头,历史快照索引在末尾 |
| 改某一块代码 | [`docs/subsystems/README.md`](docs/subsystems/README.md) —— 每个子系统的边界与契约,跨子系统的硬约定也在这一页 |
| 跑门禁 / 搭本机环境 | [`docs/development.md`](docs/development.md) —— 门禁分层、每一层验不到什么、日常命令 |
| 查一条决定为什么这么定 | [`docs/DECISIONS.md`](docs/DECISIONS.md) —— 文件头有全量索引 |
| 升级一个已在运行的部署 | [`docs/UPGRADING.md`](docs/UPGRADING.md) —— 只写需要人工做的动作 |
| 想知道某个坑的全过程 | [`docs/notes/`](docs/notes/README.md) —— 事故与订正档案 |
| 写文档 | [`docs/STYLE.md`](docs/STYLE.md) —— 可判定的写作约定 |
| 最近一轮改了什么 | [`HANDOVER.md`](HANDOVER.md) —— 只留最近一轮,更早的在 `docs/notes/` |

子目录另有各自的 `CLAUDE.md`:[`backend/CLAUDE.md`](backend/CLAUDE.md)、
[`frontend/CLAUDE.md`](frontend/CLAUDE.md)。

## 目录

```
backend/          FastAPI + Celery + SQLAlchemy2 + Alembic
frontend/         React18 + Vite + antd + Vitest + Playwright
comfyui/          ComfyUI 工作流模板与配置样例
sample-data/      示例商品与示例图(图是生成物,首次先跑 generate_images.py)
data/             本机素材暂存,图片不入库也不进交付包
docs/             文档;notes/ 是事故档案,vendor/ 是第三方接口存档
tools/pack.sh     交付打包:先按黑名单排除,打完再解开复验
.github/workflows/ci.yml   门禁执行者
AGENTS.md         与同级 CLAUDE.md **逐字一致**的副本,给读 AGENTS.md 的工具。
                  改约定只改 CLAUDE.md 再同步过去 —— 两边分叉过一次,而全仓
                  没有任何东西盯着;现在有守卫了
```

## 门禁

```bash
make check          # = check-offline + test-nodb + fe-check,需联网
make check-offline  # 后端子集:纯测试 + ruff + 架构契约 + 交付自检 + 样例数据
make test-nodb      # 不碰真库的全部 pytest 用例
```

**这两条都不等于「全都过了」。** `check-offline` 覆盖不到前端四层,`check` 覆盖不到
真库、Playwright 与 docker build。每一层各自验不到什么,以及为什么「把本地说得比
实际更窄」和说得更宽一样危险,见 [`docs/development.md`](docs/development.md#门禁分层)。

**本地真实基础设施验证须由用户明确触发。** 没有明确指令不得设置真库测试环境变量、
运行 `requires_db` 用例、跑 Alembic 真库升降级,也不得连 Redis 做 PING 或
Celery 集成验证。需要它们才能验收的改动,本轮在交付说明里标「未执行」并写明建议命令。
完整口径见 [`docs/development.md`](docs/development.md#本地真实基础设施验证须由用户明确触发)。

## 硬规则

1. **凭据不进仓库树。** `.secrets/` / `.env` / `*.key` / `*.pem`。三道拦截:
   `.gitignore` → `tools/pack.sh` 打完复验 → `verify_delivery.py`。两个测试运行器
   自带固定测试密钥与临时密钥目录,**新增第三个运行器时那三行必须跟着走**。
   为什么:[主密钥两次随包泄露](docs/notes/2026-08-01-master-key-shipped-twice.md)、
   [只修了一个运行器](docs/notes/2026-08-02-settings-secret-key-only-pytest.md)

2. **打包只用 `make pack V=<版本>`。** 手打 `zip -r` 是上面那次事故的根因 ——
   手打的命令没有记忆。不该出去的东西有 git 与交付包两条出口,规则必须两侧都写,
   `verify_delivery.py` 逐条核对它们不许分叉。

3. **门禁清单不许只写在文档里。** `verify_delivery.py` 会逐条在
   `.github/workflows/ci.yml` 里找命令字面量,不是 step 名字。
   加一条门禁 = 改三个地方:Makefile、ci.yml、`check_ci_runs_every_gate()` 的表。

4. **前端不许推测状态。** 后端返回 `display_status` / `next_action` /
   `blocking_reasons` / `allowed_actions`,前端只展示和触发。每个状态字段都要能
   追溯到真实来源:不许填常量,也不许缺一列让默认值替它回答。接口形状要有一条
   真的调注册表、真的跑判定的用例守着
   ([为什么](docs/notes/2026-08-02-is-configured-missing-column.md))。

5. **注释陈述契约与事实,理由用链接指过去。** 保留行为、失败、时序、归属与
   安全使用这几类;评审史与订正史归 `docs/notes/`,不用比喻,不注释一眼看得见
   的事实。改代码时发现注释和代码对不上,先查是哪一边过时了 —— 别默认删注释。
   十一条可判定的约定在 [`docs/STYLE.md`](docs/STYLE.md),其中四条有门禁。

## 三件最容易读错的事

**「代码侧清空」不等于「阶段完成」。** 剩下的往往不是代码问题,是执行环境问题
(容器里下不了浏览器、没跑过 `docker build`、缺真实 runner)。当前进度以
`docs/STATUS.md` 为准,不写在这里 —— 写在这里的进度过期时没有任何东西会报错。

**`DELIVERY_STAGE` 是欠账结算闸,`CODE_STAGE` 才是落码进度。** 两者的差就是欠着的账。
调高结算闸会让到期的欠账守卫当场变红,那是它存在的意义;变红时该做的是还账或
重新认领还款日,不是把数字调回去。

**一个号绑两件交付物,排期就得靠猜按哪一句算。** 任务编号有过重号与拆号,现行口径
以 `docs/REVIEW.md` 12.1 的任务表为准,改动记在 `docs/DECISIONS.md`。
