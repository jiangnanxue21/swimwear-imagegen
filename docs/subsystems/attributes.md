# attributes · 属性识别与确认

**目录**:`backend/app/attributes/`、`backend/app/extractors/`

## 注册表同时是三样东西

`attributes/registry.py` 是**唯一来源**:

```
识别 Schema    模型被允许输出哪些字段、每个字段的取值范围
DSL 白名单     渠道 spec 里 attr.<field> 能引用什么
阈值表         哪个字段用哪一组自动确认阈值
```

写成三份的代价很具体:spec 里引用了一个识别层根本不会输出的字段,
要到做真实模板映射时才会发现 —— 而那时候改的是提示词、Schema、阈值配置和 spec 四处。
`extractors/schema.py` 因此是**由注册表生成,不手写**。

注册表是**纯数据**,只 import `core/enums.py`,不碰数据库。

## 两件事严格分开

```
run_extraction()   逐图调模型 -> 落证据。**不产生任何结论**
decide()           证据 -> 决定采信什么。纯判定,不调模型
```

## 为什么是逐图

一次调用只看一张图,而且**不告诉它已有的值**。早期版本让模型「先独立判断,再与
known 比对」—— 模型看到 `{"primary_color": "NAVY"}` 之后就会把图判成 NAVY,
所谓交叉验证只是复读我们喂进去的答案。

逐图另有两个好处:新增一张素材时只识别新图(增量);某张图解析失败不牵连别的图。

## 一图一字段一条证据

证据落 `attribute_evidence`,按素材颜色归组合并:共享字段跨全部证据合并,
颜色字段只在该颜色的证据子集内合并。合并后生成 `SUGGESTED` / `CANDIDATE` /
`CONFLICT`,运营逐字段确认(SPU 共享一次,颜色分别)。

## 三条硬规则

- **模型对禁止字段给出确定值** → 过滤并计入「不可见属性编造率」指标,不入确认队列。
- **未校准的(字段 × 模型 × 提示词)组合一律不自动确认** —— fail-closed。
  自动确认是校准积累后的产物,不是承诺。
- **模型不确定时返回 `missing_reason`,不猜测。**

## 双作用域指纹

完成条件按指纹判:所有必填共享事实 `CONFIRMED` 且其 `input_fingerprint` 等于当前
**共享指纹**;每个 ACTIVE 颜色的必填颜色事实 `CONFIRMED` 且指纹匹配该颜色的当前指纹;
无未处理的关键 `CONFLICT`。

两个指纹分开是为了让重认是增量的:换一张 SPU 通用图不该让十个颜色的已确认事实全部
作废,换一张颜色样品也不该波及别的颜色。

## 异步运行

识别走 Celery:`POST /api/spus/{id}/attribute-extraction-runs` 返回 `run_id`,
之后可取消、看逐图成绩单、按失败颜色精确重试;relay / reaper 兜底续跑。
运行状态见 `ExtractionRunStatus`(含 `PARTIAL_SUCCESS` —— 部分图成功不是失败)。

## 加一个品类

见 [`../cookbook/add-a-category.md`](../cookbook/add-a-category.md)。
