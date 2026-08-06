# 视觉大模型评分器

把候选图交给多模态模型逐维度打分。一个评分器,四种后端:OpenAI Responses、
OpenAI 兼容 Chat Completions、火山方舟豆包、阿里云百炼千问 VL。

代码里**不按厂商名分支**,只按 `VISION_MODEL_API_STYLE` 分两个适配器。
厂商名一旦进了业务判断,换一家就要改代码 —— 而换一家恰恰是这层存在的理由。

---

## 一、先说清楚模型做什么、不做什么

| | 谁来做 |
| --- | --- |
| 逐维度 0-100 打分 | **模型** |
| 指出硬错误(从固定枚举里选) | **模型** |
| 记录看不清的地方 | **模型** |
| 总分 | 后端按权重算(`scoring.compute_overall`) |
| A/B/C/D 分档 | 后端算(`rules.grade_candidate`) |
| 自动通过 / 重生 / 转人工 | 后端算 |

模型自报的 `overall_score` 只留档看打分漂移,**不作为分档依据**。
提示词里明确要求它不要输出 grade 和 recommended_action;真输出了也会被忽略。

模型自造的硬错误代码不算数:不在 `HardFailCode` 里的代码会被丢进
`uncertain_items` 留痕,不会触发硬错误分档。否则模型可以自造一个代码
让任意问题冒充硬错误,分档规则就没有确定性可言。

**解析失败 = 这张候选图评分失败**,走重生流程,不会补一个默认分。
评分器坏掉时静默给 80 分,会让烂图直接自动通过上网站 —— 那是最坏的失败模式。

---

## 二、三家的配置

密钥一律走 `.env`,文档里不写真实值。

### OpenAI

```env
EVALUATOR_BACKEND=vision
VISION_MODEL_BASE_URL=https://api.openai.com/v1
VISION_MODEL_NAME=gpt-5.6-sol
VISION_MODEL_API_STYLE=responses
VISION_MODEL_RESPONSE_FORMAT=json_schema
VISION_MODEL_FULL_IMAGE_DETAIL=original
VISION_MODEL_QUICK_IMAGE_DETAIL=low
VISION_MODEL_REASONING_EFFORT=low
VISION_MODEL_FAIL_CLOSED=true
```

想压成本就把完整评分换成便宜档:

```env
VISION_MODEL_NAME=gpt-5.6-terra
```

### 火山方舟豆包

```env
EVALUATOR_BACKEND=vision
VISION_MODEL_BASE_URL=https://ark.cn-beijing.volces.com/api/v3
VISION_MODEL_NAME=doubao-seed-2.1-pro
VISION_MODEL_API_STYLE=responses
VISION_MODEL_RESPONSE_FORMAT=json_schema
VISION_MODEL_FULL_IMAGE_DETAIL=high
VISION_MODEL_QUICK_IMAGE_DETAIL=low
VISION_MODEL_REASONING_EFFORT=low
VISION_MODEL_FAIL_CLOSED=true
```

账号要求走推理接入点时,`VISION_MODEL_NAME` 直接填 Endpoint ID。
代码里没有硬编码任何模型 ID,填什么发什么。

### 阿里云百炼千问 VL

```env
EVALUATOR_BACKEND=vision
VISION_MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
VISION_MODEL_NAME=qwen3-vl-plus
VISION_MODEL_API_STYLE=chat_completions
VISION_MODEL_RESPONSE_FORMAT=json_object
VISION_MODEL_FULL_IMAGE_DETAIL=high
VISION_MODEL_QUICK_IMAGE_DETAIL=low
VISION_MODEL_REASONING_EFFORT=none
VISION_MODEL_FAIL_CLOSED=true
```

`REASONING_EFFORT=none` 是有意的:思考内容混进输出会破坏标准 JSON。
设成 `none` 时代码改发 `temperature=0` 而不发 `reasoning` 字段。

---

## 三、FULL 与 QUICK

预排序之后只对前几名做完整评分,其余做快速硬错误检查,以此压低评分成本
(需求第十三章)。深度经 `rule_set["_evaluation_depth"]` 传进评分器 ——
走 rule_set 而不是改 `evaluate()` 签名,是因为那个签名是所有评分器共用的公开接口。

| | FULL | QUICK |
| --- | --- | --- |
| 维度 | 全部 11 个 | 4 个:`garment_identity`、`color_fidelity`、`anatomy_realism`、`website_usability` |
| 图片 detail | `VISION_MODEL_FULL_IMAGE_DETAIL` | `VISION_MODEL_QUICK_IMAGE_DETAIL` |
| JSON Schema | 要求全部维度 | 只要求快速维度 |

**QUICK 没查出硬错误不等于这张图合格。** 提示词里明确写了这一条,
业务侧也不能凭 QUICK 结果自动判 A —— 现有分档规则对此的处理保持不变。

总分按**实际出现的维度**重新归一化,不把缺失维度当 0 分。
否则快速检查出来的候选图会被系统性判死。

---

## 四、图片怎么送

支持四类来源:HTTP(S) URL、data URL、本地存储路径、S3 存储路径。

顺序永远是:参考图 1..N,然后候选图。每张图前面紧跟一个角色标签
(`REFERENCE_IMAGE_1` / `CANDIDATE_IMAGE`),提示词里的描述与实际发送顺序一致。
标签和图错位一格,整次比对就是错的,而且从结果上看不出来 —— 所以有专门的测试盯着。

默认转 data URL 内联发送。`VISION_MODEL_SEND_PUBLIC_URLS=true` 时改发公网 URL,
但只发 https 地址:开发期的 `http://localhost:8000/files/...` 厂商根本访问不到,
发出去只会换来一个含混的 400。

每张图在编码前检查:

- 大小超过 `VISION_MODEL_MAX_IMAGE_MB`(默认 8MB)→ 明确报错,**不会悄悄压缩后发出去**;
- 用 Pillow 验证确实是图片,只接受 JPEG / PNG / WEBP;
- 扩展名不作数 —— 一个改名成 `.png` 的 PDF 会在这里被拦下;
- 自动应用 EXIF Orientation。不转的话模型会看到一张躺倒的图,
  然后如实报告"人体姿态异常" —— 一个纯粹由我们自己引入的假阳性。

参考图最多发 `VISION_MODEL_MAX_REFERENCE_IMAGES` 张(默认 4),超出部分截断,
保留原顺序 —— 顺序即重要性。候选图缺失或参考图为空一律直接失败:
没有参考图就无从判断"是不是同一件衣服",而那是这套评分的第一优先级。

HTTP(S) 地址一律过项目已有的 SSRF 校验(`net_safety.check_download_url`)。
让厂商去访问 `169.254.169.254` 和我们自己去访问是一样的问题,区别只是日志里看不见。

---

## 五、生产环境 fail-closed

`VISION_MODEL_FAIL_CLOSED=true` 且 `APP_ENV` 是 `production` / `prod` 时,
配了 `EVALUATOR_BACKEND=vision` 但评分器不可用 → **抛 `EvaluatorUnavailableError`,
任务转人工审核**,绝不静默回退 Mock。

为什么这条重要:Mock 评分器按文件指纹给分,真实商品图有相当比例会被判成 A 档,
于是自动通过、自动出图、自动发布,而运营端只看到一行「任务成功」。
那不是"判断力下降",那是**没被真正评过的图上了网站**。

| 环境 | 评分器不可用时 |
| --- | --- |
| `local` / `dev` / `development` / `test` | 退回 Mock,留一条 warning |
| `production` / `prod` + fail-closed | 抛错,转人工审核 |
| 显式 `EVALUATOR_BACKEND=mock` | 用 Mock(离线演练模式,任何环境都允许) |

读不到配置时按最保守的来:不允许回退。默认安全,不默认方便。

---

## 六、错误与重试

| 情况 | 重试 | 说明 |
| --- | --- | --- |
| 连接失败 / 读写超时 | 是 | 指数退避 + 抖动 |
| 408 / 429 / 500 / 502 / 503 / 504 | 是 | |
| 429 且消息含 quota / 余额 | **否** | 限流等一会儿就好,余额不足等多久都不会好 |
| 400 / 401 / 403 / 404 | 否 | |
| 图片过大 / 无法解析 | 否 | 重试多少次都一样 |
| 模型拒答 / 内容安全 | 否 | 自动重试只会重复触发 |
| 输出被截断 | 否 | 报错时直接说该调大哪个配置 |
| 业务 JSON 解析失败 | 否 | 这张候选图判失败,走重生 |

最多重试 `VISION_MODEL_MAX_RETRIES` 次。响应带 `Retry-After` 时优先尊重它,
但上限 30 秒 —— 见过返回 3600 的实现,那会把整轮生成挂死。

错误信息里一定有:评分器名、模型名、API Style、HTTP 状态码、可安全展示的摘要。
**一定没有**:API Key、base64、完整请求体、Authorization 头。

`raw_response` 里留档的是:模型名、响应 ID、token 用量、finish reason、
API Style、HTTP 状态码、解析后的结构化评分、每张图的大小与 MIME。
不留 base64 —— 那会把数据库和界面一起撑爆,而且毫无用处。

---

## 七、连接测试

Provider 页面的"测试连接"会发一个最小请求:一张内存里的 8×8 PNG +
一句"只返回 `{"ok": true}`",`max_output_tokens=64`。不碰任何真实商品图 ——
连接测试是运维随手点的按钮,不该产生业务级的推理开销。

返回 `configured` / `reachable` / `model` / `api_style` / `latency_ms` / `message`。
任何异常都转成结果对象,不向 API 页面抛 —— 后台页面不能因为自检打不开。

---

## 八、上线前必须做的事

**换模型 = 重新校准阈值。** 这不是可选项。

A/B/C/D 的阈值是针对某一个模型的分数分布定的。换一个模型(甚至同一模型换一个
`reasoning_effort`),同一张图的分数会整体平移,阈值不动的话要么大批图被误判成 D
(白烧生成额度),要么大批烂图混成 A(直接上网站)。

建议流程:

1. 攒 50-100 张已经人工判过 A/B/C/D 的候选图作为校准集;
2. 用目标模型跑一遍,拿到每张的维度分与总分;
3. 对比人工结论,调 `rule_set` 里的阈值和权重,直到两边一致率可接受;
4. 特别检查硬错误的**漏报**率 —— 漏报比误报危险得多,前者会让废图上网站,
   后者只是多生成一轮;
5. 之后每次换模型或改提示词都重跑这一遍。

先用 `EVALUATOR_BACKEND=mock` 把闭环跑通,再切真实评分器,再校准阈值。

---

## 九、提示词可编辑(带版本)

系统提示词不再是代码常量,在「提示词」页可以全文编辑。

**改它只影响模型怎么打分。** 总分、A/B/C/D 分档、是否自动通过仍然全部由后端计算,
改这里绕不过任何一条业务规则。评分维度清单、硬错误代码、JSON 格式要求由代码按
评分深度注入到**用户段**,即使系统提示词删光了也不影响运行 ——
但模型会少掉大量上下文,判断质量会明显下降。

### 版本怎么走

| 动作 | 结果 |
| --- | --- |
| 保存 | 新建一个版本并立即生效,旧版本原样留档 |
| 切回某一版 | 只改 active 指向,不删任何东西 |
| 恢复默认 | 把所有版本停用,回到代码内置的 `DEFAULT_SYSTEM_PROMPT`,历史仍在 |

**为什么必须留档**:提示词决定评分口径。三个月后有人问"这批图当时为什么判 C",
如果提示词是原地覆盖的,这个问题就答不了 —— 评分记录里存着分数,
但产生那个分数的判断标准已经没了。每次评分会把 `prompt_version` 记进
`raw_response._vision_meta`,两边对得上。

数据库层面用部分唯一索引保证同一个 key 只有一条 active,不靠服务层的
"先停用旧的再启用新的"两步操作 —— 那两步之间崩一次就会留下两条 active,
而评分器只会取到其中一条,取到哪条看排序。

### 体检不是门禁

保存前会跑一遍 `lint_prompt`,把问题显示出来,但**不阻止保存**。
检查项都指向同一类事故:提示词和 JSON Schema 说的不是一回事,于是模型输出
通不过 parser —— 而这类失败不会在保存时暴露,只会在下一批任务里零零散散地出现,
界面上只显示「这张候选图评分失败」。

刻意**不检查**系统提示词里有没有列出评分维度和硬错误代码:两者都由用户段注入,
不写也照常工作。为此报警的话内置默认自己就会带两条警告,而一打开页面就是黄色告警,
正是让人从此忽略整个警告面板的开始。

### 提示词读不到时会怎样

用内置默认,并记一条 warning。提示词是**可以降级**的输入 ——
运营改坏了最多是评分口径回到出厂设置,而整批评分停摆是更大的损失。

注意这和「模型输出解析失败」是两类事:那个必须失败关闭,绝不给默认分。

### 一轮之内不换版本

`evaluate_round` 在候选图循环**外**取一次提示词,一轮里所有候选图用同一版。
否则同一轮的分数之间不可比,而预排序正是拿它们比出来的。

---

## 十、文本模型(非多模态)

设置页有一组「文本模型 · 非多模态(预留)」,**当前代码里没有任何调用点**,
填了不会有任何效果,也不会影响评分。

留这一组是为了让多模态和非多模态从一开始就分开配置:等真接上纯文本用途时
不必再做一次配置迁移,也不会有人误以为改 `VISION_MODEL_*` 能影响文本任务。
页面上明写了「预留」和「没有任何调用点」—— 预留而不说明,等于骗填它的人。

接上用途时,把 `TEXT_MODEL_*` 读进一个和 `VisionEvaluatorConfig` 平行的配置类即可,
`normalize_endpoint` 和两个适配器都能直接复用。
