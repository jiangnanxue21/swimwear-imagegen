# evaluators · 评分与分档

**目录**:`backend/app/evaluators/`
**图**:[轮级决策](../ARCHITECTURE.md#3-轮级决策什么时候重生什么时候找人)、[视觉评分调用](../assets/vision-call-lifecycle.svg)

## 边界与依赖契约

这一层分成两半,而**它们的依赖面完全不同**:

```
vision.py / mock.py      要发网络请求、要读配置、要落审计元数据
scoring / rules /        纯函数:算总分、判档、选修复策略、做轮级决策
repair / decision        —— 不碰 models / db / services / sqlalchemy
```

后一半由 import-linter 的 `grading-stays-pure` 契约钉着。理由是可测性:
「这张图为什么判 C」必须永远能在一次不带数据库的单元测试里复现。

## 总分由后端算

**不采信评分器自报的数字。** 模型自报的总分存进 `model_reported_overall`,
只用来监控打分漂移。Mock 评分器故意自报一个不同的数,等于给这条规则内置了活体探针。

权重表在 `scoring.DEFAULT_WEIGHTS`(11 个维度),可被 `RuleSet` 覆盖。

## 失败关闭,不退回 Mock

原先的规则是「评分器用不了就退回 Mock」,理由是「闭环仍然成立,只是判断力下降」。
**那条理由是错的。** Mock 按文件指纹给分,真实商品图有相当比例会被判成 A 档,
于是自动通过、自动出图、自动发布 —— 而运营端只看到一行「任务成功」。

现在:

```
显式配置 EVALUATOR_BACKEND=mock   → 用 Mock,这是文档里的离线演练模式
配了真实评分器但用不了 + 开发环境 → 退回 Mock,并留一条 warning
配了真实评分器但用不了 + 其它环境 → 抛 EvaluatorUnavailableError,任务转人工审核
```

放行名单**刻意不含 `test`**:「测试环境」在多数团队里是一台真机器,连着真实的商品
数据和真实的 Provider Key。少一个字母的差别不该决定评分能不能被悄悄跳过。

## 一套代码四种后端

`VISION_MODEL_API_STYLE` 分适配器:OpenAI Responses / OpenAI 兼容 Chat Completions /
火山方舟豆包 / 阿里云百炼千问 VL。配置见 [`../VISION-EVALUATOR.md`](../VISION-EVALUATOR.md)。

## 元数据先于解析

截断和解析失败都是**已经计费的成功 HTTP 调用**。响应 ID、厂商实际路由到的模型、
token 用量、finish reason 只在那一刻存在,所以 `_build_metadata` 必须跑在
`_extract_or_fail` 之前,并把元数据挂在异常上带出去 —— 否则
`evaluation_attempts` 里那条失败记录只剩一句错误说明。

`resolved_model_name()` 由评分器自己回答,而不是从模型输出里读:
`model_name` 是回答「三个月前这批图是谁打的分」的唯一依据,**让被审计对象自报是没有
意义的**。

## 硬错误只淘汰候选

硬错误代码中任意一个出现即判 D;但只要还有轮次,任务就继续自动重生,
**不立刻交给人工**。人工审核的对象是**商品任务**,不是每一张低分候选图。

修复策略按**问题码**选方向(换 seed / 换模特 / 降融合强度 / 核对受众),
所以码不能混用 —— 详见 [`../ARCHITECTURE.md`](../ARCHITECTURE.md#3-轮级决策什么时候重生什么时候找人) 里那段解释。

## 加一个评分后端

见 [`../cookbook/add-an-evaluator-backend.md`](../cookbook/add-an-evaluator-backend.md)。
