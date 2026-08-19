# channels · 渠道字段 spec 与传输层

**目录**:`backend/app/channels/`

## 映射层与传输层分开注册

```
映射   map_fields / validate / build_request    generic/     我们自己的字段定义
传输   submit / poll                            simulator.py 平台行为的模拟
```

这不是过渡期的将就。拿不到官方文档与凭证之前不允许宣称某个平台已完成,
退而求其次是实现严格的 Simulator。把两者绑成一个「渠道对象」会让这个事实被一个名字
盖住 —— 而前端状态条要如实显示「真实渠道 / Simulator」,那条显示需要一个能追溯到
来源的判据,不是一个常量。

`describe_channels()` 返回的 `is_simulator` 就是那个判据:它来自这张表里传输层实际
是谁,不是手写的 True/False。

## 依赖契约

`app.channels` 不许 import `app.models` / `app.db` / `app.services`(import-linter
的 `channels-take-only-the-contract`)。它只做「名字 → 函数」的查表,数据由调用方
传进来。

## spec 按品类载入

`spec/{category_id}.yaml`。品类码由**受众 + 品类族**单向派生,派生逻辑只在
`core/audience.category_code_for` 一处。反向(从码里抠受众)只在
`embedded_audience_of` 允许,而它的唯一用途是**校验一致性**,不是当作受众的来源;
两者不一致时以 `audience` 为准并**报错**,不静默取其一。

草稿的类目一律走 `category_id_for(product)`,不取模块常量 —— 库里只有一个品类时
不出问题,加了男装之后,一件男泳裤会用女装 spec 静默导出成功(spec 校验的是
「这份 spec 的必填字段填了没有」,而男泳裤确实可以在「领型」留空时通过一份不要求
领型的 spec)。

## spec_version 必须跟着涨

`spec_version` 进 `listing_drafts.source_fingerprint`,涨一次所有在旧规则下算过的
草稿就重新判一次。改了校验规则却不涨版本的后果是:一份在旧规则下「校验通过、可以
导出」的草稿会**保持**那个状态,而它的价格可能是三位小数。

## 价格、库存、备货时效一律手填

`manual_fields` 里那几项**不允许模型生成**。让大模型给一个会真的向消费者收款的数字,
是这条链路里唯一不可挽回的错误。

## 字段名是我们自己的

`generic/` 里的字段名全部是本项目自己定义的,不冒充任何第三方平台 ——
第三方字段不凭记忆写代码。等官方文档到手,要做的是在 `channels/<平台>/` 下再写一份
spec,而不是回来改这里。

## 加一个品类

见 [`../cookbook/add-a-category.md`](../cookbook/add-a-category.md)。
