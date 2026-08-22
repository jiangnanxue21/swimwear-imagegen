# 子系统

每个子系统一页:**它负责什么、边界在哪、有哪几条不能破的约定**。
这些页面是给「要改这一块代码」的人看的 —— 它们不重复 `ARCHITECTURE.md` 里的流程图,
也不重复各自领域文档里的配置细节,只写那个模块自己的契约。

不确定该看哪一页时,从 [`../ARCHITECTURE.md`](../ARCHITECTURE.md) 的图开始,
图下面写着对应的代码位置。

| 子系统 | 负责什么 |
| --- | --- |
| [providers](providers.md) | 图像生成 Provider 抽象、路由、错误策略 |
| [evaluators](evaluators.md) | 候选图评分、A/B/C/D 分档、修复策略、轮级决策 |
| [prompts](prompts.md) | 提示词注册表与版本机制 |
| [attributes](attributes.md) | 属性识别、证据合并、置信度与人工确认 |
| [media](media.md) | 素材域:来源、角色、去重、证据分层 |
| [listings](listings.md) | 图片集、文案、SKU 矩阵、草稿与导出 |
| [channels](channels.md) | 渠道字段 spec 与传输层 |
| [publish](publish.md) | 发布上架、Outbox、状态轮询与下架 |
| [workbench](workbench.md) | 运营流程判定、批次租约、异常聚合 |
| [settings](settings.md) | 后台设置页、配置层次、密钥落库 |
| [auth](auth.md) | 浏览器登录、机器凭据、权限边界 |
| [ops-logs](ops-logs.md) | 运行日志分类法、环形缓冲、模型载荷旁挂库 |

## 三条跨子系统的硬约定

这三条在每一页都会再遇到,所以写在这里一次:

**一、前端不推测状态。** 后端返回 `display_status` / `next_action` / `blocking_reasons` /
`allowed_actions`,前端只展示和触发。反面教材是 `describe_extractors()` 曾经硬编码
`configured: true` —— 前端老实展示了,错在后端给了一个不是从真实来源推出来的值。
**后端返回的每个状态字段都必须能追溯到真实来源,不许为了接口形状完整填常量。**

**二、纯判定层不许碰基础设施。** 分档、修复策略、轮级决策、流程判定、渠道映射全是
纯函数,由 import-linter 的三条契约钉着。它们在纯函数层而不是 service 层,是因为
这些判断需要被穷举测试,而**只要它能查库,就没人会写那个穷举测试**。

**三、注释写「为什么」,不写「是什么」。** 本仓库的注释密度偏高是刻意的:
大部分注释记录的是某个决定背后踩过的坑。改代码时如果发现注释和代码对不上,
先查是哪一边过时了 —— 别默认删注释。

## 请求的事务边界:归接口所有

`db/session.py::get_session()` **不替所有请求提交**,它只剩异常回滚与关闭。

```
写端点      自己 session.commit()。漏了 = 接口返回 200 但什么都没存下来
GET 端点    一律不提交。唯一例外 download_batch_file(只增不改的下载审计流水)
批次执行    跨付费调用的长事务是署名例外,不要"顺手修好"
```

改这一段之前先读三条:

1. **提交这件事,集成测试结构上验不了。** `tests/conftest.py` 的 `client` 夹具把
   `db_session` 覆盖成一个不提交的 session(每个用例一个事务,结束回滚),同一个
   session 里读得到未提交的写 —— 于是「真的提交了」和「只是 flush 了」在 API 测试里
   完全等价。**唯一防线是 `tests/pure/test_transaction_boundaries.py` 的「HTTP 边界」
   一节。** 加白名单绕过它之前,先明白你绕过的是这件事的全部防线。
2. **白名单放行的是「不提交」,不是「不用管」。** `preview_import` 在白名单里,同时被
   反向钉着**一处会话写都不许有** —— 它哪天真开始写库,缺的那次提交会当场变红,
   而不是变成一个悄悄不落库的接口。
3. **批次执行的长事务不许"修"。** `try_advisory_xact_lock` 是事务级锁,必须活到回执
   对别人可见那一刻(也就是提交)。提前 commit 把锁放掉,第二个请求拿到锁、查不到
   回执、照样调一次付费模型。发布链路能分三段,是因为它的幂等靠唯一键不靠锁 ——
   两条链路的幂等机制不同,事务形状因此不同。

兜底任务的返回形状也归在这一节:**异常分支不许手抄一份返回字典**。成功路径加一个键时
手抄的那份不会跟着变,而读它做看板的一侧恰恰在出错时拿到另一种形状。`relay_dispatches`
与 `reap_batch_leases` 都从成功路径那个构造器取形状,门禁盯着。

## 时间与出参形状

**时间一律走 `core/clock.utc_now()`。** `db/session.py` 在连接上钉死了 `-c timezone=utc`。
别在新模块里重新写 `datetime.now(UTC).replace(tzinfo=None)`。

这是对**新代码**的要求,不是对现状的描述:直接 `datetime.now(UTC)` 的地方还有一批,
清单与影响评估在 `core/clock.py` 的「收敛没有做完」一节,数量由
`tests/pure/test_a45_batch17_2_clock_ledger.py` 每次现数一遍并和清单比对 ——
所以那个数是**被守着的**,不是被记着的,也不要照抄下游文档里的旧值。

**出参里的时间戳走 `core/clock.iso_utc()`,不要裸 `.isoformat()`。** `SessionLocal` 配的是
`expire_on_commit=False`,于是"刚写完就回读"拿到的是 Python 侧的 naive 值、"刷新页面
重新查"拿到的是 timestamptz 的 aware 值 —— **同一列同一个接口会序列化出两种形状**。
前端两种都认,所以它一直没表现出来;导出文件和第三方消费者会看见。工作台与批次两组
出参已收,`platform_service` 与发布接口那几组还没收。
