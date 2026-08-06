# A44 第五批修复说明（C-13 · B-07 · 十一条毛边）

> 依据：`REVIEW-A44-FINAL-verified-r2-1.md`（修订二）第三、六、七节及 P3 观察。
> 环境同前：**无网络，`pydantic / sqlalchemy / fastapi / alembic / pytest` 均未安装。**
> 纯逻辑回归 **1609 → 1626，失败数恒为 2**（缺 `sqlalchemy` / `pydantic` 的 import 失败）。
> 前端 79 个 `.ts/.tsx` 语法解析全部通过（仍**不是** `tsc` 类型检查）。

## 一、这一批修了什么

| 编号 | 级 | 位置 | 做法 |
|---|:--:|---|---|
| **C-13** | **P0** | `migrations/0027` downgrade | 按长度前缀切分，不按第一个 `/` |
| **B-07** | **P0** | `backend/tools/repair_variant_owners.py`（新增） | 给出可执行的人工归属路径 |
| **A-09** | P2 | `hooks/useIdentity.ts` | 失败之后才轮询，后端恢复横幅自己消失 |
| **A-30** | P3 | `ImageSetTab.tsx` | Tooltip 补上 `!imageSetId` / `isLoading` 两支 |
| **A-34** | P2 | `ExportTab.tsx` | `revokeObjectURL` 延后一拍 |
| **A-35** | P3 | `ExportTab.tsx` | 动作文案单源到 `AUDIT_ACTION_LABEL`，本地只留颜色 |
| **A-36** | P3 | `App.tsx` + `pages/NotFoundPage.tsx`（新增） | 兜底路由改 404，并打印访问的路径 |
| **A-37** | P2 | `image_set_service` | 只有版本冲突进重试环，其余原样抛 |
| **A-42** | P3 | `Makefile` | `psql` 跟随 `POSTGRES_USER/DB` |
| **A-43** | P3 | `frontend/nginx.conf` | `/healthz` 改 `default_type`，去掉 `add_header` |
| **R1-23** | P3 | `workbench/batch.py` · `workflows/dispatch_policy.py` · CI | 三处模块级 `assert` 改 `if…raise`，加 `-O` 冒烟 |
| **R1-28** | P3 | `api/deps.py` · `.env.example` | 名字必须长得像名字，否则整段按共用口令 |
| **R1-37** | P3 | `docker-compose.prod.yml` | 三个 `POSTGRES_*` 改 `:?` 必填 |

## 二、C-13：这是 0027 里唯一会损坏数据的地方

命名空间形式是 `<len>:<spu>/<variant_id>`，而那个长度前缀存在的**唯一理由**
就是 SPU 可以含 `/`。downgrade 原来写 `POSITION('/' IN owner_id)` —— 取第一个
斜杠，也就是把长度前缀当成不存在：

```
spu="AB/CD"   owner_id="5:AB/CD/Red"
按第一个 /  ->  "CD/Red"    ← 错的，而且看起来像个合法值
按长度前缀  ->  "Red"       ← 对的
```

错值直接写进 `owner_id`，降级完成后再 upgrade 也回不来——原值已经没了。

新算式：`POSITION(':') + CAST(SPLIT_PART(owner_id, ':', 1) AS INT) + 2`。
SPU 自己含冒号也不影响，因为取的是**第一个**冒号，而正则 `^[0-9]+:` 已经
保证它是长度前缀。口径与 Python 侧 `split_variant_owner_id()` 完全一致——
**那边一直是对的，错的只有这条 SQL。**

测试把这个算式在 Python 里复算了一遍（`POSITION` / `SPLIT_PART` / `SUBSTRING`
的语义可以逐字对应），所以它跑的是真逻辑，不是"SQL 文本里有没有某个词"。
另有一条单独断言迁移文件里那条 SQL 真的是它。

## 三、B-07：把"无可执行修复"变成"有一条路"

`0027` 的 CTE 用**当前** `primary_color` 算 bare，而 `owner_id` 是**写入当时**
的颜色名。中间改过一次颜色文案的行匹配不上、不被改写，那一批的跨 SPU 串档
在迁移里根本没修，**而迁移是绿的**。

**补不回来。** 属性表没有 `product_id`，库里只剩一个字符串，没有任何一列
记着它当初属于哪件商品。硬猜就回到了这条 CTE 一开始要避免的那件事。

所以新增 `tools/repair_variant_owners.py`，把决定交给人：

```
list    把证据摆出来（字段、取值、来源、时间、候选 SPU）
assign  按人给出的归属改写，一次一个 owner
```

三条安全约束：**默认干跑**（不带 `--apply` 只打印）、**撞唯一索引先停下**
（目标变体已有同名字段的当前值时不合并，要人决定留哪个）、**每次改写进审计**
且 `--actor` 必填。

**刻意不提供 `--all`**：批量参数会把这个工具变成 0027 那条 CTE 的手工版本，
而那正是问题的来源。测试里有一条专门钉这一点。

## 四、R1-23：一条门禁在钉住一个坏形状

三处守卫原来是模块级 `assert`，而 `python -O` 会把它们整条剥离——也就是
三条**只在开发机上生效**的守卫，而 -O 最可能出现在生产。

改成 `if …: raise` 之后，`test_batch_lease.py` 里一条既有门禁当场红了：
它**要求**那个不变量写成 `ast.Assert`。

这件事值得单独记：**一条门禁在钉住一个坏形状**。想改的人会先看到它红，
而红的时候最省事的做法是把代码改回去——门禁于是变成了坏形状的保镖。
已把那条改成"必须是 `if…raise`，**且明确禁止 assert**"，并在 CI 加了
`python -O -c "import …"` 冒烟（改完之后这一步才有意义：它真的会执行那几个判断）。

## 五、其余几条的取舍

**A-37** 判据是约束名，先读 psycopg 的 `diag.constraint_name`，读不到退回字符串
匹配。两条都拿不到时**按版本冲突处理并重试**——多试几次是几毫秒，而把真正的
版本冲突当成入参错误抛出去，会让一次正常的并发派生变成一条看不懂的 500。

**A-09** 用 state 而不是 `refetchInterval` 的函数式写法：那个回调的签名在
react-query v4 与 v5 之间变过（`(data, query)` → `(query)`），写错不报错，
只会安静地永远不轮询——又是一个不报错的错答案。

**R1-28** 完全消歧做不到（`alice:tok` 两种读法都合法）。加的是"名字必须长得
像名字"（`[A-Za-z0-9_.-]{1,64}`）：`p@ss:w0rd` 不再拆，`alice:tok-a` 仍然拆。
两可的写法一律按 `名字:口令` 读，`.env.example` 里写明了这一点。

**A-34** 延后 60 秒再 revoke，是随手取的上界：比任何一次"开始读"都长得多，
又不至于把 blob 一直挂在内存里。

## 六、这一批踩的坑

**注释陷阱第四次出现**，这次在 YAML 上：R1-37 那条红在我自己写的
`# 改之前是 ${POSTGRES_PASSWORD:-imagegen}` 注释上。已加 `_no_hash_comments()`。
三批四次，结论不用再推了——**任何字符串包含类断言，先剥注释。**

**"红得看不懂"第三次出现**：A-34 那条第一版直接 `index("const ACTION_COLOR")`，
在改动前的代码上抛 `ValueError: substring not found`。已改成先断锚点。

**断言范围写宽第二次**：A-09 那条第一版查全文件的 `refetchOnWindowFocus: false`，
而 `auth-probe` 关掉聚焦重试是**对的**（它由存口令那一步显式 invalidate）。
一刀切会把一个正确的写法一起判红。已按 health 那一段收窄。

**依赖不可用时的绕法**：R1-28 与 A-37 两条要读的是纯逻辑，但所在模块会拉起
`fastapi` / `sqlalchemy`。做法是把目标函数连同它的判据常量用 AST 单独取出来
`exec` ——**跑的仍然是仓库里那份源码**，不是复刻品。复刻一份到测试里的话，
源码改了测试不会红。

## 七、验证缺口

| 归属 | 待验项 |
|---|---|
| **D-02** | **C-13 的 SQL 一次都没执行过。** 真库上应造一个含 `/` 的 SPU，跑 upgrade → downgrade → upgrade，断言 `owner_id` 逐字回到原值。这是本批风险最高的一条：它改的是**数据修复路径本身**。 |
| **D-01** | B-07 那个工具**完全没跑过**，连 `--help` 都没跑过（缺 sqlalchemy）。接真库后第一件事是 `list` 干跑。 |
| **D-01** | A-37 的约束名判据依赖 psycopg 的 `diag.constraint_name`，**没有验证过它真的有值**。退化表现是回落到字符串匹配（仍可用，但更脆）。 |
| **D-05 ～ D-08** | 五个前端文件只做了语法解析。`NotFoundPage` 是新文件，`useIdentity` 新引入了 `useState/useEffect`，类型错误只有 `tsc` 能发现。 |
| **D-10** | nginx `/healthz` 改动没有在真容器里 `curl` 过；prod overlay 的 `:?` 必填没有跑过一次 `docker compose config`。 |

## 八、还剩什么

A 类只剩：A-10（`hasToken` 非响应式，代码自承）· A-11/A-12（有意取舍）·
A-15（裸 `.isoformat()`，面积较大）· A-22/A-23（计费与领取语义，**均有设计张力，
建议连同 C-01/C-02 一起做**）· A-31（渠道专用集前端无入口）· A-32（`job_out` 无条件全查）。

C 类剩余风险最高的是 **C-01 / C-02（付费幂等与长调用租约，两条 P0）**，
它们要动的是执行链本身，不适合在没有真库的情况下改。
