# workbench · 运营工作台

**目录**:`backend/app/workbench/`

## 判定与翻译分开

```
flow.py     判定。零依赖纯函数,列表页与详情页读同一份结论
service.py  翻译。查 media_assets / attribute_values / listing_image_sets /
            listing_copies / listing_drafts,组装成 flow 的输入
```

`flow.py` 在纯函数层是因为「缺背面图时下一步是不是补素材」这类判断需要被穷举测试,
而只要它能查库,就没人会写那个穷举测试。

## 四个问题,一个推荐动作

它回答:做到哪一步了(七步里第一个没做完的)、卡在什么地方(三级问题)、
完成度多少、下一步干什么(**有且只有一个**)。

步骤顺序写成常量 `STEP_ORDER`,不靠字典顺序 —— 「唯一下一步」的正确性完全依赖它,
它必须是一个能被测试直接断言的对象。

## 三级问题不是三种颜色

```
BLOCKING       不解决就走不下去,也不允许导出
NEEDS_CONFIRM  要人做一个决定(冲突、低置信度),系统不替他决定
REMINDER       可以带着它继续走,但导出前最好看一眼
```

分级的意义在于「阻断数」这个数字要能被信任。把提醒也算成阻断,运营会发现每件商品
都是红的,然后不再看这个数 —— 那时候真正的阻断也一起被忽略了。

## 批次:领取靠租约,恢复靠回收

`claim_items` **一次领一件**(`CLAIM_CHUNK = 1`),`FOR UPDATE SKIP LOCKED` +
条目租约。两个 worker 跑同一批次时各领各的,重复投递因此变成安全操作 ——
这是「敢自动重投」的前提。

**领取批量参与租约不变量**(`ITEM_LEASE_SECONDS > CLAIM_CHUNK × 单件最长合法耗时`),
所以它住在 `batch.py` 而不是服务层;要调大必须先做续租,否则导入期 assert 直接拦住。

恢复:租约过期 → `reap_expired_leases` 放回 PENDING(上限 3 次,超过落 `WORKER_LOST`);
有 PENDING 却很久没动的批次 → `redispatch_stalled_batches` 重投一次。
beat 每 60 秒一拍,没 beat 时退回 `make requeue`。

**没有新建 outbox 表** —— 条目本身就是意图记录。

## 按款视图没有款级「下一步」

不同 SKU 卡在不同步骤时,任何单选都是编的。所以按款视图给的是**卡点分布**,
不是一个款级 `next_action`。

口径提醒:`blocking_count` 数问题条数,`blocked_steps` 数 SKU 个数,两者不相等。

## 计数即筛选

异常页每个计数都能点进去看到对应的那一批 —— 一个不能点的数字会让人自己去猜口径。
**读不到的整块不显示**:一个「0」和一个「没查到」在界面上长得一样,而运营会按前者理解。
