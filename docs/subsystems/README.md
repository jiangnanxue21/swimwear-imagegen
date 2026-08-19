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
