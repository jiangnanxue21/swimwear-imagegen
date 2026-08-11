# FASHN 接入说明

FASHN 是第一家接入的真实 Provider。本文档是**接入手册**,不是阶段报告 ——
记录实现依据、配置方法、费用模型、已知限制和排查手册。

实现依据是你提供的官方 skill,原样存档在 `docs/vendor/fashn-skill/`
(`docs/vendor/fashn-skill/SKILL.md` 与 `docs/vendor/fashn-skill/reference.md`,
标注校验于 TypeScript SDK `fashn@0.15.0`)。
代码里没有一处接口细节是凭记忆写的;
文档没写的能力一律按"不支持"处理。

---

## 一、接口形状

FASHN 只有一个预测端点,靠 `model_name` 区分能力:

```
POST /v1/run          {"model_name": ..., "inputs": {...}}  -> {"id", "error"}
GET  /v1/status/{id}                                        -> {"id","status","output","error"}
GET  /v1/credits                                            -> {"credits": {...}}
```

鉴权 `Authorization: Bearer $FASHN_API_KEY`,Key 从 https://app.fashn.ai/api 取。

本项目用到的模型:

| 生成模式 | `model_name` | 商品图字段 | 模特图 | 一次调用出图数 |
| --- | --- | --- | --- | --- |
| `virtual_try_on`(默认) | `tryon-max` | `product_image` | 必填 `model_image` | `num_images` 1-4 |
| `virtual_try_on`(可切换) | `tryon-v1.6` | `garment_image` | 必填 `model_image` | `num_samples` 1-4 |
| `product_to_model` | `product-to-model` | `product_image` | 选填(传了进 try-on 模式) | **文档无此字段,只出 1 张** |

## 二、配置

最少只要一个 Key:

```bash
FASHN_API_KEY=fa-xxxxxxxx
```

其余全部可选,留空走官方默认值:

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `FASHN_BASE_URL` | `https://api.fashn.ai/v1` | 覆盖端点用,一般不填 |
| `FASHN_TRYON_MODEL` | `tryon-max` | 切 `tryon-v1.6` 可换成快且便宜的版本 |
| `FASHN_GENERATION_MODE` | 空 | tryon-max:`balanced`/`quality`;product-to-model:`fast`/`balanced`/`quality` |
| `FASHN_RESOLUTION` | 空(=1k) | `1k`/`2k`/`4k`,越高越贵 |
| `FASHN_OUTPUT_FORMAT` | `png` | `png`/`jpeg` |
| `FASHN_POLL_INTERVAL_SECONDS` | 2 | 轮询间隔 |
| `FASHN_POLL_TIMEOUT_SECONDS` | 300 | 与官方 SDK 的 `subscribe` 默认上限一致 |
| `FASHN_MAX_IMAGE_MB` | 10 | 内联素材大小上限 |
| `FASHN_SEND_PUBLIC_URLS` | false | 存储换成公网可达的对象存储后打开 |

配置完验证:

```bash
curl -X POST http://localhost:8000/api/providers/fashn/test
# 期望: {"configured": true, "reachable": true, "message": "连接正常,剩余额度 234"}
```

自检走 `GET /v1/credits` —— 只读、不排队、**不产生费用**。

## 三、三个必须解释的实现选择

**一、素材默认转 base64 内联。** FASHN 接受 HTTPS URL 或 base64 data-URI。
开发期我们的素材地址是 `http://localhost:8000/files/...`,FASHN 的服务器根本访问不到,
所以默认把素材读出来编码成 data-URI 再发。等存储换成公网可达的对象存储(阶段 6),
把 `FASHN_SEND_PUBLIC_URLS=true` 打开就能省掉这次编码。

**二、轮询放在 Provider 内部。** 编排层的调用顺序是 submit → get_status → fetch_results,
各一次(Mock 是瞬时的,阶段 3 这样够用)。FASHN 一次生成 20-120 秒,
因此 `fetch_results` 自己轮询到终态才返回,语义等同官方 SDK 的 `subscribe()`。
**代价**:这一轮的 Celery 任务会阻塞最多 `FASHN_POLL_TIMEOUT_SECONDS`,
期间协作式取消不会立即生效。阶段 5 接受这个代价(需求第二十三章:不提前做并发优化)。

**三、product-to-model 靠多次提交凑候选数。** 官方参数表里 `tryon-max` 有 `num_images`、
`tryon-v1.6` 有 `num_samples`,而 `product-to-model` **没有**这一项。
所以该模式下一次调用只出一张图,要 4 张就提交 4 次、每次换一个 seed,
外部任务 ID 用逗号拼起来存。宁可多发几次请求,也不给文档里不存在的字段瞎填一个名字。

> 如果 FASHN 后来给 `product-to-model` 加了候选数字段,改 `PRODUCT_TO_MODEL` 这一个
> dataclass 即可(`candidate_field` 与 `max_per_call`),业务代码一行都不用动。

## 四、费用

官方文档给出的只有 `tryon-max` 的额度表(× `num_images`):

| generation_mode | 1k | 2k | 4k |
| --- | --- | --- | --- |
| `balanced` | 2 | 3 | 4 |
| `quality` | 3 | 4 | 5 |

- **失败的预测不计费。**
- 实际消耗从响应头 `x-fashn-credits-used` 读,已写进候选图的 `metadata.credits_used`。
- 其它端点的价格文档没给,标注为"见 https://fashn.ai/pricing#api"。**没有猜。**
- 余额:`GET /v1/credits`,或直接点后台 Provider 页的"测试连接"。

按默认配置(tryon-max / balanced / 1k / 每轮 4 张)估算:**一轮约 8 额度**,
一个跑满 3 轮的任务最多 24 额度。上量前先用 `--max-rounds 1` 摸一遍真实分档分布。

## 五、与 Mock 基线对比

需求第二十一章要求"每次只接入一个 Provider,并使用相同测试样本与 Mock 基线比较"。
脚本已就绪:

```bash
# 先只跑 Mock,确认样本和流水线本身没问题
make baseline SKU=SW-001-BLK-S

# 对比(会真实消耗额度,脚本会先要 --yes)
docker compose exec backend python -m app.scripts.provider_baseline \
    --sku SW-001-BLK-S --providers mock,fashn --max-rounds 1 --yes
```

输出并排给出两边的状态、轮次、最佳候选分档与总分、分档分布、耗时,
并逐维度打出 FASHN 相对 Mock 的差值。对比时建议 `--max-rounds 1` ——
多轮重生会把 Provider 的差异混进重生策略里,看不清是谁的功劳。

## 六、错误映射

FASHN 的错误分两类,本项目的重试策略完全不同,**不能混为一谈**:

**作业开始前**(HTTP 4xx/5xx,体形如 `{"error": "<Code>", "message": ...}`):

| FASHN | 本项目分类 | 重试 | 换 Provider | 转人工 |
| --- | --- | --- | --- | --- |
| `BadRequest` | 输入错误 | 否 | 否 | 否 |
| `UnauthorizedAccess` | 鉴权错误 | 否 | 是 | **是** |
| `RateLimitExceeded` / `ConcurrencyLimitExceeded` | 限流 | 是(退避) | 是 | 否 |
| `OutOfCredits` | **配额用尽**(阶段 5 新增) | 否 | 是 | **是** |
| `InternalServerError` | 服务错误 | 是 | 是 | 否 |

`OutOfCredits` 特意没并进限流:限流等一会儿就好,余额用尽等多久都不会好,
必须有人去充值。并进去只会让退避重试白烧三次。

**作业跑了但失败**(HTTP 200 + `status: "failed"` + `error.name`):

| FASHN | 本项目分类 | 处置 |
| --- | --- | --- |
| `ImageLoadError` | 输入错误 | 检查素材可达性与 data-URI 前缀 |
| `ContentModerationError` | 内容安全 | **直接转人工**,自动重试只会重复触发 |
| `PoseError` | 输入错误 | 模特图检测不到人体姿态,换模特模板 |
| `InputValidationError` | 输入错误 | 参数组合非法 |
| `ThirdPartyError` / `3rdPartyProviderError` | 生成失败 | 改输入后重试 |
| `PipelineError` | 生成失败 | 重试(失败不计费) |
| `PollingTimeout` | 网络超时 | 先查状态再决定是否重提,**不直接重提** |

文档没列到的代码按 HTTP 状态码兜底,再兜不住才归为生成失败。

## 七、已知限制

| 项 | 说明 |
| --- | --- |
| 不支持取消 | 官方文档没有取消端点。任务取消只能等当前轮跑完,`supports_cancel=False` 是诚实声明 |
| Webhook 未启用 | `/run?webhook_url=` 是文档支持的,但我们还没有公网回调端点,仍走轮询。能力声明为 `true`,实现待阶段 6/7 |
| 轮询期间阻塞 worker | 见"实现选择二"。上量后应改成"提交即返回 + webhook/定时器驱动" |
| 画幅有损映射 | 任务上存的是像素尺寸,FASHN 只收枚举画幅,代码吸附到最近一档并在 metadata 留痕 |
| 未验证真实出图 | 见下节 |

## 八、尚未用真实 Key 验证过

**代码写完了,但没有对着真实 FASHN 服务跑过一次。** 交付环境没有 API Key、没有网络。
已验证的是:

- 44 个纯逻辑用例:字段名、错误映射、状态映射、批次拆分、画幅吸附(实跑通过)
- 16 个 HTTP 流程用例:用 `httpx.MockTransport` 走通提交 → 轮询 → 取结果
  (`tests/test_fashn_http.py`,**未实跑**,需要 httpx)

首次拿到 Key 后请按顺序验证:

```bash
# 1) 连通性与余额(不花钱)
curl -X POST http://localhost:8000/api/providers/fashn/test

# 2) HTTP 流程用例(不花钱,不联网)
docker compose exec backend pytest tests/test_fashn_http.py -v

# 3) 真跑一张(花钱,约 2-3 额度)
docker compose exec backend python -m app.scripts.provider_baseline \
    --sku SW-001-BLK-S --providers fashn --candidates 1 --max-rounds 1 --yes

# 4) 与 Mock 基线对比(约 8 额度)
docker compose exec backend python -m app.scripts.provider_baseline \
    --sku SW-001-BLK-S --providers mock,fashn --max-rounds 1 --yes
```

第 3 步如果报 `ImageLoadError`,九成是素材没编码成 data-URI 就发出去了 ——
检查 `FASHN_SEND_PUBLIC_URLS` 是不是被打开了而 `PUBLIC_BASE_URL` 还指着 localhost。

## 九、排查手册

| 现象 | 大概率原因 |
| --- | --- |
| Provider 页显示"未配置" | `FASHN_API_KEY` 没进容器环境。`docker compose exec backend env \| grep FASHN` |
| 测试连接返回 401 | Key 失效或复制时带了空格 |
| 任务停在 `PROVIDER_RUNNING` 超过 5 分钟 | 轮询超时会抛 `NETWORK_TIMEOUT`,查 attempt 的 error_code;真卡住看 worker 日志 |
| 每轮只出 1 张候选 | 模式是 `product_to_model`?那是文档限制,靠多次提交补齐,检查 `candidate_count` |
| `ImageLoadError` | 素材没内联成 base64,或 data-URI 少了 `data:image/...;base64,` 前缀 |
| `PoseError` | 模特图里检测不到人体姿态,换一张模特模板 |
| 额度掉得比预期快 | `FASHN_RESOLUTION` 设了 2k/4k,或 `generation_mode=quality` |

## 十、下一家

阶段 5 的剩余顺序是 **ComfyUI → fal.ai**。建议先 ComfyUI:它不依赖商务流程,
本地起一个就能对着真实工作流 JSON 把节点 ID 配起来,能最快验证 Provider 抽象
是否真的和厂商解耦 —— FASHN 这次已经证明了一半(业务代码一行没改)。

fal.ai 需要你先确定**用哪个 model endpoint**,它的输入 schema 因模型而异,
在此之前 `app/providers/fal.py` 保持骨架状态。

## 基线脚本绕过生成方案(a48)

`make baseline` / `app.scripts.provider_baseline` 现在显式 `override_plan=True`。

理由:a47 之后 `create_task` 会用**方案**的 provider / 模特模板 / 一轮张数覆盖
调用方传的值。基线对比的全部意义是「差异只来自 Provider 本身」——
SPU 上有一份 ACTIVE 方案时,`P=mock,fashn` 的两条腿会**双双**跑在方案那个
Provider 上,而脚本照旧打印一张对比表。不报错,答案是假的。

所以看到基线跑出来的任务 `generation_plan_id` 与 `plan_fingerprint` **两列为空**
是对的,不是漏写:这批图确实不是按哪份方案出的。绕过方案**不绕过预算**,
也不绕过 §10.5 / §11 那两道闸。结论在 `docs/DECISIONS.md` §3.73 第二节。
