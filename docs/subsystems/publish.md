# publish · 发布上架

**代码**:`backend/app/services/publish_service.py`、`poll_service.py`、`app/api/publish.py`、`app/workflows/publish_*.py`

## 三段事务,不许改

```
业务事务    enqueue():建 listing / attempt / outbox。**只 flush,不 commit**
            —— 事务归调用方(API 或 use case)所有
checkpoint  投递前:领租约 + 把 attempt 标成 IN_FLIGHT,commit
事务外      真正发请求。**一个数据库事务不许跨越一次外部调用**
新事务      保存结果
```

## 提交超时不猜结果

落 `SUBMIT_RESULT_UNKNOWN` 等人确认。「超时了大概是没成功」这种猜测会导致重复上架,
而重复上架在平台侧往往不可逆。

## 轮询是读,退避不通往放弃

到期轮询 + 指数退避,**封顶 1 小时但不通往放弃** —— 轮询是读操作,停下来等于本地与
平台永久分叉。**404 绝不当作已下架**:那可能是平台侧的临时不可见。

平台驳回自动进既有驳回台账,`located_by=publish_attempt`。

## 状态派生只有一份

在 `workflows/publish_view.py`:后端给 `display_status` / `next_action` /
`blocking_reasons` / `allowed_actions`,前端只展示和触发。

那里多一个状态机里没有的 `STALLED` —— listing 说在途而 outbox 已 DEAD 的组合,
少了它界面会一直显示「提交中」。

## 下架

`DELIST` 不看草稿状态、不带报文内容。清理预案见 `make cleanup`:
必须限定作用域,默认只看不做,`verify` 未清干净时退出码 1。

## 平台侧驳回目前是手工录入

平台状态 + 驳回台账;定位把握分 audit / current_draft / unlocated 三档如实标注。
**不接平台 API** —— 这一条在 [`../STATUS.md`](../STATUS.md) 的能力表里也标着。

## 链路由七个模块组成

```
app/workflows/publish_policy.py   纯判定:提交响应怎么算、退避多久、什么时候放弃
app/workflows/poll_policy.py      纯判定:轮询回答怎么算、退避多久(轮询**不通往放弃**)
app/channels/registry.py          渠道 -> 谁构造报文、谁发出去(is_simulator 由此推出)
app/services/publish_service.py   事务编排:enqueue 不 commit,投递分三段事务
app/services/poll_service.py      事务编排:领取推进 next_poll_at 即租约,同样三段
app/services/cleanup_service.py   列清单 / 核对 / 下架,先看后做
app/workbench/platform_service.py 驳回台账(`record_api_rejection` 是轮询的落点)
```

**改这条链路之前先读 `publish_service.py` 顶部的「事务边界」。** 那四行(业务事务 /
checkpoint / 事务外 / 新事务)由 `tests/pure/test_publish_policy.py` 末尾用 AST 钉着,
不是靠自觉。

## 四条已经踩过的坑

1. **轮询封顶的是频率不是次数。** 投递重试每次都花钱,所以有放弃;轮询是读,放弃
   意味着本地看板和平台永久分叉,所以只有 `MAX_POLL_INTERVAL_SECONDS`。
2. **404 永远不写 DELISTED。** 查不到可能是 ID 错了。猜成已下架的代价是一个仍挂在
   平台上的商品从清理清单里消失 —— 那正是清理模块要防的结局本身。
3. **API 上架的驳回不走 `locate_export()`。** 那个函数找不到导出记录就抛 409,而 API
   上架的商品可能一辈子没导过 Excel。走 `record_api_rejection()`,定位依据换成提交
   尝试(`located_by="publish_attempt"`),表和状态机不变。
4. **`STALLED` 是一个组合状态,`PublishStatus` 里没有它。** listing 说 SUBMITTING、
   attempt 说 IN_FLIGHT、outbox 已经 DEAD —— 四个来源没有任何一个单独知道「这件事
   死了」。少了它,界面会一直说「提交中」,运营会一直等。

## 判定必须留在 `workflows/`

```
app/workflows/publish_view.py   纯判定:四个来源 -> display_status / next_action /
                                blocking_reasons / allowed_actions
app/api/publish.py              端点只做取数 / 调判定 / 持有事务
```

`workflows/` 零依赖,所以能在 `tests/pure/` 被穷举;搬进接口函数之后,覆盖「某个状态
组合下按钮不该亮」就要起一个 FastAPI 加一个库,而那种测试没人会为一个枚举分支去写。

## 驳回闸只认成功的提交尝试

`platform_service._publish_attempt_entries()` 把「驳回之后有一次**成功的**提交尝试」
当作等价证据喂进 `resolve_gates()` 与解决路径,关联口径是 `ChannelListing.draft_id`,
指纹那半边落在当前草稿指纹上 —— **所以没改草稿的重复提交过不了闸**。

只认 `SUCCEEDED`:PENDING / IN_FLIGHT 还没有结果,UNKNOWN 不知道平台收没收到,
FAILED / ABORTED 根本没提交成功。

仍然欠着两件,别把它们读成"没做":

```
真库 seam   从一行真实 PublishAttempt 穿过 platform_service 到驳回关闭,
            没有一条真库用例走完过(判定与服务接线都有纯测试)
旧数据      `draft_id IS NULL` 的历史驳回关联不上尝试,仍然只能人工标记
```

## 花费台账是本系统的账,不是厂商余额

`/spend` 页 + 全局预算告警横幅,数据来自 `provider_usage_records` 的真实行。
文案一律「预算」不许写「余额」,理由见 `backend/app/services/spend.py` 顶部。
