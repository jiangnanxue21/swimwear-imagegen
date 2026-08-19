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
