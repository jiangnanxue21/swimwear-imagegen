# CLAUDE.md — 后端

FastAPI + SQLAlchemy 2(声明式 `Mapped[]`)+ Alembic + Celery + PostgreSQL 16。
Python ≥3.11。依赖装法:`pip install -e ".[dev]"`。

## 分层与三条架构契约

契约写在 `.importlinter`,由 `make arch-check`(即 `lint-imports`)执行。
它建的是**完整依赖图**看可达性,不是单跳 —— 绕一层的反向依赖也会被抓住,
而绕一层恰恰是这类问题真实发生的方式。

```
1  app.core 是最底层        不许依赖 services / models / api / db / tasks / workbench
2  评分与修复必须是纯函数     app.evaluators.{scoring,rules,repair,decision}
                            不许碰 models / db / services / sqlalchemy
3  渠道适配器只接 CanonicalProduct   app.channels 不许依赖 models / db / services
```

第 3 条在 Phase 2 最容易被顺手破掉(要新建发布域表、写 Adapter)。
Adapter 需要数据就让调用方传进来,别在 `app/channels/` 里 import 模型。

## 两个测试运行器 —— 这是本仓库最容易踩的一处

```
tests/pure/     零三方依赖。不许 import pytest / sqlalchemy / fastapi。
                两种跑法都要能过:
                  python3 tools/run_pure_tests.py     ← 只有 python3 的机器
                  pytest                              ← CI、容器内
                断言用 assert,需要 raises 就用 tests/pure/_helpers.expect_raises

tests/*.py      需要真实 PostgreSQL。文件顶部写 pytestmark = requires_db。
                不退化到 SQLite —— 项目用了 JSONB、UUID、部分唯一索引,
                换引擎跑出来的绿是假的。
```

在 `tests/pure/` 里写 `import pytest` 会让整个文件在导入期炸掉,而运行器只会
把它记成**一条**失败 —— 曾经有个文件带着它躺了很久,套件显示 1167/1168,
看起来只差一条,实际上那个文件里 9 个用例一个都没跑过。
`run_pure_tests.py` 启动时用 AST 自检拦这件事,别绕过它。

**CI 里连不上库直接失败,不跳过。** 判据是 `CI` 环境变量或
`REQUIRE_TEST_DATABASE=1`。本机没库时才会 skip。理由:曾经的
`730 passed, 73 skipped` 里那 73 个恰好是全部集成、迁移、并发测试。

跑真库测试要两样:`TEST_DATABASE_URL` 指向**以 `_test` 结尾**的库,
外加 `ALLOW_DESTRUCTIVE_TEST_DB=1`。夹具会无条件 `DROP SCHEMA public CASCADE`,
两道护栏都在 `conftest._assert_safe_to_wipe()` 里,少一道就拒绝执行。

**本地协作默认不跑真实基础设施验证。** 除非用户在当前任务中明确要求,
不得运行上述真库测试、Alembic 真库升降级或任何 Redis / Celery broker 集成验证;
只运行纯测试和其他不依赖真实 PostgreSQL / Redis 的相关检查。若完整验收需要真库或
Redis,交付时明确写「未执行」并提醒用户验证范围与命令,等用户指令后再跑。
这不改变 CI 与正式验收必须执行这些门禁的要求。

## 密钥副作用(改 conftest 之前必读)

`app.core.config.settings` 是**模块级单例**,import 那一刻就把环境变量读完了。
`conftest.py` 顶部在 `from app.core.config import settings` **之前**设
`SETTINGS_SECRET_KEY` 和 `SETTINGS_KEY_DIR` —— 顺序是刻意的,放进 fixture 里无效。

不这么做的后果:`app.core.secrets._material()` 找不到密钥会走
`_read_or_create_key_file()`,在仓库根的 `.secrets/` 下**真的生成一个主密钥文件**。
测试全绿,无声无息,直到下一次交付。会话末尾还有一条 autouse fixture 验尸。

## 枚举

全部在 `app/core/enums.py`,一律 `StrEnum`。**落库用 `String(n)` 不用数据库枚举** ——
加一个取值不该触发一次迁移。给列宽留余量:`RejectionStatus` 加了个
20 字符的中间态,原来的 `String(16)` 装不下,写入被 PostgreSQL 直接拒掉。

发布域有**两个**独立枚举,别合并:

```
PublishStatus          商品在渠道上现在是什么状态(当前事实,会变)
PublishAttemptStatus   第 N 次提交发生了什么(历史记录,写完不改)
```

一次 UNKNOWN 的尝试之后,listing 可能已经变成 LISTED,但那条尝试记录必须
保持 UNKNOWN —— 后来查清楚了不能倒回去改写历史,否则「我们当时是不是真的
不知道」这个问题永远查不清。

## 迁移

`migrations/versions/NNNN_名字.py`,四位数字顺序编号,`down_revision` 串成一条链。

- **必须写 `downgrade()`**,且要能真的把表删干净。
  `test_migrations.py::test_downgrade_removes_every_table` 断言的是**全部** ORM 表。
- **建表/删表顺序按外键依赖**,downgrade 里反过来。
- 约束显式命名,和 ORM 的 `__table_args__` 一字不差。命名约定在 `db/base.py`;
  多列联合唯一时自动名只取第一列,会误导人,所以显式写。
- 测试库用 `alembic upgrade head` 建,不用 `create_all()` —— 那才是生产库真正的形状。

## 幂等键有两个,不要合并

```
workflows/idempotency.py           这次**生成**要不要重跑(输入:商品/素材/模板/prompt)
workflows/publish_idempotency.py   这次**提交**平台是不是已经收过(输入:渠道/店铺/站点/操作)
```

两者输入几乎不相交,失效后果也不同:生成键算错多花一次钱,发布键算错会在
平台上多出一个商品、或让一次更新被当成重复请求丢掉。

发布键**必须含 `operation`**。不含它 CREATE 和 UPDATE 算出同一个键 ——
要么更新被当成重复创建吞掉,要么创建被当成更新放过去(平台没这商品,404)。
函数会对空 operation 直接抛 ValueError,别加默认值。

## Ruff

`ruff>=0.16,<0.17`,**版本钉死区间不写 `>=`**。lint 规则集随版本长,
`>=` 会让每台机器装到不同版本 —— 一个「今天绿明天红而中间没人改过代码」的
门禁最后一定会被关掉。这条是被咬过之后才写死的(第一次接 CI 报了 337 条)。

`B008` 对 FastAPI 的 `Depends/Query/Path/...` 全局豁免(配在
`extend-immutable-calls`),不要逐行 `# noqa` —— 115 个 noqa 会把真正
该被这条规则拦下的写法一起淹掉。
