# 新增一个品类

品类是参数,不是写死的。但「参数化」意味着要在**三个地方**各补一份,
少补哪一份都会以不同的方式静默失败。

## 1. 渠道字段 spec

`backend/app/channels/generic/spec/<category_id>.yaml`。

品类码由**受众 + 品类族**派生,派生逻辑只在 `core/audience.category_code_for` 一处 ——
所以文件名要和它派生出来的码一致(如 `men_swimwear.yaml`)。

必须填的:`spec_version`、`sites`、`manual_fields`、`consts`、`header_fields`。

**价格、库存、备货时效一律进 `manual_fields`** —— 让大模型给一个会真的向消费者收款
的数字,是这条链路里唯一不可挽回的错误。

> `spec_version` 改校验规则时必须跟着涨:它进 `listing_drafts.source_fingerprint`,
> 不涨的话,一份在旧规则下「校验通过」的草稿会保持那个状态。

## 2. 属性注册表

`backend/app/attributes/registry.py` 加字段与取值范围。这张表同时是识别 Schema、
DSL 白名单和阈值表 —— 所以 spec 里 `attr.<field>` 能引用什么,由它决定。

如果新品类需要新的枚举(比如新的版型、闭合方式),先加进 `core/enums.py`,
注册表引用它,**不要在注册表里手抄一份取值清单**:手抄的那份不会在加一项时跟着变。

## 3. 硬错误代码与受众规则

新品类如果有自己的「结构被改了」形态(女装的领口、男装的腰头是既有例子),
在 `core/enums.HardFailCode` 加码,并在规则包里决定它对哪个受众启用。

**别用 `GARMENT_WRONG` 顶替。** 那条码的语义是「模型画的不是这件衣服」,
对策是换 seed / 换 Provider;而结构被改的对策是别的方向 —— 修复策略按码选方向,
混用会让一件正确的衣服被反复换 seed 重生,每一轮都是真实付费调用。

## 4. 校准

**未校准的(字段 × 模型 × 提示词)组合一律不自动确认。** 新品类上线时的正常状态是
**全量人工确认**;自动确认是校准积累后的产物。

```bash
make calibrate    # 人工判定 vs 模型分档的一致率;样本不足 20 条拒绝给结论
```

## 5. 样例数据(可选但建议)

`sample-data/` 加几件,让 `make smoke` 能走到新品类。
`sample-data/README.md` 里的图片张数由守卫钉着真值,改了数据要同步改那个数。

## 会因为你漏做而变红的

- `tests/pure/test_m0_contracts.py`:注册表自洽性(spec 引用了识别层不输出的字段)
- 渠道 spec 加载用例:yaml 结构、必填段缺失
- `make verify-sample-data`:样例数据自检
