# 新增一个评分模型后端

现有四种 API 形状(OpenAI Responses / OpenAI 兼容 Chat Completions / 火山方舟豆包 /
阿里云百炼千问 VL)都由**同一套代码**驱动,按 `VISION_MODEL_API_STYLE` 分适配器。
所以先问一句:**新的这家真的需要第五种形状吗?** 多数厂商兼容 OpenAI Chat Completions,
那样只要填地址和模型名,不用改代码。

## 需要改代码时

1. `app/evaluators/vision.py`:加一种 api style 的请求构造与响应解析。
2. `app/llm/transport.py`:如果它的鉴权头、错误体形状不同,在这里适配。
3. `.env.example` + `Settings` + `settings_schema.py` 三处加配置项。

## 三条必须守的

**元数据先于解析。** 响应 ID、实际模型、token 用量、finish reason 必须在解析响应体
**之前**取好并挂在异常上 —— 截断和解析失败都是已经计费的成功调用,那些字段只在
那一刻存在。

**`resolved_model_name()` 由评分器自己回答**,不从模型输出里读。它是回答「三个月前
这批图是谁打的分」的唯一依据,让被审计对象自报是没有意义的。

**`is_simulator` / `billable` 两个标记都要填。** 前者决定状态条说什么,
后者决定这次调用进不进用量流水。

## 别碰的

不要在 `scoring` / `rules` / `repair` / `decision` 里 import 任何新东西 ——
`grading-stays-pure` 契约禁止它们触达 models / db / services / sqlalchemy。
分档逻辑不该因为换了一家模型而改动。

## 验证

```bash
cd backend && python3 tools/run_pure_tests.py vision      # 纯层:请求形状、解析、预算拟合
make test-nodb                                            # 含 test_vision_http.py 那一批
```

真跑一次会花钱,走 `/ai-tests` 页面对单张候选图打一次分即可 ——
那一页写诊断留痕,不会污染正式评分记录。
