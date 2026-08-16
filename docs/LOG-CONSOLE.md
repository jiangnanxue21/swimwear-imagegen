# 运行日志:归类与展示设计

> **已落地(a53)。** 取舍并入 `docs/DECISIONS.md` §3.80。
> 配套交互原型:`docs/log-console-prototype.html`(单文件,双击打开)。
>
> 这份文档是**设计**,不是实现说明。落地过程中有五处按代码订正了设计,
> 逐条记在第十章;那一章还记了一件设计里没有、接线时撞出来的缺陷。
> **正文里已经按订正后的口径改过**,所以正文与代码是一致的 ——
> 第十章存在的意义是"为什么变",不是"哪里不一样"。

## 〇、这份设计回答什么

日志的**采集**在这个仓库里是健康的:210 个调用点、190 个带结构化字段、
脱敏有测试钉着、编码问题有注释记着教训。不满意的是采集之后的两件事 ——

    归类   唯一的机器可读分类是 logger(代码结构)和 message(自由英文句子)
    展示   唯一的查看工具是一条只看 AI 调用的命令行
    原文   模型的请求与响应正文默认不留;留了也只能在那条命令行里看

这份设计只动这三件事,不动采集面:调用点怎么写日志、脱敏怎么跑、
stdout 怎么被外部收集,全部照旧 —— 包括 `LLM_LOG_PAYLOADS` 的默认值。
原文的解法不是「把归档面的开关打开」,是**另开一个有 TTL 的诊断窗口**(§6)。

---

## 一、病灶,逐条带证据

### 1.1 归类靠英文句子,而句子会改

`tools/watch_ai_logs.py`(已删,改名成 `tools/watch_logs.py`)里的过滤集
是 9 条硬编码的消息原文:

```python
AI_MESSAGES = {
    "image compacted for multimodal request budget",
    "llm request prepared",
    ...
}
```

消息措辞一改,查看器**安静漏事件** —— 不报错、不提示,就是少了。
这正是本仓库最忌讳的失效方式(中间件顺序、AGENTS.md 分叉,教训都记在案)。

同时,新代码已经自发长出了另一种写法:`batch.stale_outcomes`、
`publish.poll_status_changed`、`media.ingest.lost_dedupe_race`、
`spu.create_reused_request_key` —— 拿短码当 message。这说明写日志的人
已经在要一套分类法,只是没立起来。于是现在两种风格并存:
message 既要当人话又要当键,两个都当不好。

### 1.2 展示只有一条命令行,且只看 AI

运营和排障在 Web 里没有任何运行日志入口。审计页(`/audit`)收的是
业务写操作 —— 那是合规视角(谁改了什么),不是排障视角(系统怎么跑的)。
排一个「任务为什么卡住」,今天的路径是 ssh 进机器 grep 一个
单行 JSON 文件。

### 1.3 关联键都在,链路视角没有

`request_id` 全局钉在 contextvar 里,`llm_call_id` 串起重试,
`task_id` 在结构化字段里出现 **90 次**,`product_id` 28 次、`round` 10 次。
也就是说「给我看这个任务从领取到产出的完整时间线」所需的每一个键
都已经在日志里 —— 缺的只是一个会用这些键的界面。

### 1.4 例行噪音与真实告警同级

下面两条都是 WARNING,各占一行:

    scoring already leased by another worker, skipping     ← 健康并发的正常表现
    every candidate download failed; keeping external id   ← 这一轮白花钱了

看日志的人很快学会跳过前一种 —— 然后连后一种一起跳过。
a51 交接里批评过完全相同的模式:「`code_taken` 做成 422 的话,
`SW-0` 打到一半就红一次,运营会先学会忽略它。」日志里这个病更重,
因为例行事件的量级大得多(每次并发领取、每次幂等复用、每个 2xx 请求)。

### 1.5 想看原文的时候,原文不在

前四条讲的是「事件」——一行摘要。但排查模型问题时,真正要看的是**发出去的整段提示词**
和**收回来的整段响应**。这两样目前的处境是:

`transport.py` 已经把两侧都写好了,只差一个开关:

    if _payload_logging_enabled():
        request_fields["request_body"] = safe_payload_for_log(request.body)
    ...
    if _payload_logging_enabled():
        response_fields["response_body"] = safe_payload_for_log(body)

`redaction.py` 的脱敏也已经是对的:图片替换成 MIME + base64 字符数 + sha256_16,
密钥键替换成 `***`,签名 URL 削掉查询串,长字符串截到 12k 并留 `…[truncated N chars]`。
这套东西不需要重做。

问题在**默认关**,以及**关的理由和开的代价挂在同一根绳上**:

1. `LLM_LOG_PAYLOADS=False`(config.py:94)。出事的那一次调用,载荷已经没了。
   「打开开关重跑一遍」对模型问题基本无效 —— 值得排查的失败恰恰是不可复现的那些:
   偶发的格式跑偏、偶发的限流、某张图触发的拒答。等你开了开关,它不来了。
2. 开关一开,载荷进的是 **stdout**。那是采集面、归档面、会被复制到别处的那一面。
   12k 字符 × 每次调用 × 每个采集端,既贵,又把一份受授权约束的商品数据
   送进了一个没人管的地方。**所以默认关是对的** —— 错的不是这个决定,
   是「要么进归档面、要么不留」这个二选一。
3. 就算开了,唯一能看的地方还是 `watch_ai_logs.py` 的终端,而它被那 9 条硬编码
   消息挡着(§1.1)。写下来了,但读不到。

还有一处更隐蔽的:响应侧真正需要原文的时刻,恰恰是响应**不是 JSON** 的时刻 ——

    try:
        body: Any = response.json()
    except ValueError:
        body = response.text

上游返回网关的 HTML 错误页、返回一段散文、返回被截断的 JSON,摘要字段里
只会留下 `http_status` 和 `content_type`,`finish_reason` 和 `usage` 全是空。
这时候不看原文,是查不下去的。

---

---

## 二、方案总览

```
调用点(照旧)
   │  extra_fields 里多一个可选键:event="llm.attempt_failed"
   ▼
JsonFormatter(小改)
   │  event 提升为顶层字段;domain 从 event 前缀取,
   │  没写 event 的按 logger 前缀推导(兼容期兜底,见 §3.4)
   ├──────────────► stdout(逐字照旧 —— 外部采集面不变)
   ▼
RingHandler(新增)
   │  LPUSH + LTRIM 到 Redis 定长列表,fire-and-forget
   ▼
/api/ops/logs · /api/ops/logs/meta(新增,管理员闸)
   │
   ├──► Web「运行日志」页(新增):流视角 / 域筛选 / 链路模式
   └──► tools/watch_logs.py(改造):同一套事件注册表,同一套过滤语义


另一条路,只为模型原文(§6):

LLM 传输层(照旧调 safe_payload_for_log —— 脱敏一行不改)
   ├──► stdout 日志行     开关 LLM_LOG_PAYLOADS,默认 false,**不动**
   └──► ops:llm:{call_id} 开关 OPS_LLM_PAYLOAD_CAPTURE,默认 true,TTL 24h
              │
              └──► /api/ops/llm/{call_id} ──► 行内「模型载荷」页签
```

六件东西,两条守卫原则:**事实来源只有一个**(事件注册表),
**前端不持有任何一张分类表**(硬规则 4)。
外加一条口径:**载荷与事件分路**,因为它们的寿命和风险面本来就不同。

---

## 三、事件分类法

### 3.1 event 码

每条日志一个稳定标识:`域.动作[_限定]`,小写下划线,英文。

    llm.attempt_failed        gen.round_evaluated        publish.delivery_dead

`message` 退回它该干的事:一句给人读的话,想怎么改就怎么改,
不再有任何工具拿它当键。已经在用短码当 message 的调用点
(`batch.*` 等)把短码挪进 `event=`,message 补回人话。

### 3.2 域

十五个,从现有 210 个调用点的实际分布归并而来,不是凭空设计:

| 域 | 中文标签 | 覆盖的 logger 前缀 | 调用点规模 |
| --- | --- | --- | --- |
| `http` | 请求 | `app.main` 的访问日志 | 每请求 1 条 |
| `auth` | 登录与鉴权 | `app.api.auth`、`login_throttle`、静态挡板 | ~10 |
| `app` | 进程生命周期 | `app.main` 的 started/stopped、启动自检 | ~8 |
| `gen` | 生成流水线 | `generation_service`、`dispatch_service`、`tasks.generation_tasks` | ~45 |
| `llm` | 模型调用 | `app.llm.*` | 10 |
| `eval` | 评分 | `app.evaluators.*`、`evaluation_service` | ~12 |
| `attr` | 属性提取 | `app.attributes.*`、`extractors`、`tasks.attribute_tasks` | ~15 |
| `media` | 素材 | `app.media.*` | 5 |
| `listing` | 图片集与文案 | `app.listings.*` | 10 |
| `spu` | 建档 | `spu_service` | ~4 |
| `batch` | 批量任务 | `workbench.batch_service`、`tasks.batch_tasks` | ~20 |
| `publish` | 发布上架 | `publish_service`、`poll_service`、`platform_service`、`tasks.publish_tasks` | ~18 |
| `output` | 产出与导出 | `output_service` | ~12 |
| `settings` | 设置与密钥 | `settings_*`、`core.secrets` | ~14 |
| `ops` | 维护与清理 | `cleanup_service`、`maintenance_tasks`、`scripts` | ~15 |

域的判据是**业务领域**,不是模块路径。`eval` 覆盖评分与人工审核两侧
(`review_service` 在内):排障的人问的是「这张图的判定怎么了」,
而不是「哪个 service 怎么了」。

**这张表的第一版把 `poll_service` 划给了 `gen`,那是错的**(第十章第一条):
那个文件是**发布轮询**,五条调用点的 message 全部以 `publish.` 开头,
`CLAUDE.md` 也把它列在发布链路七模块里。按代码订正成 `publish`。

### 3.3 注册表:唯一事实来源

新增 `backend/app/core/log_events.py`(纯模块,零依赖,core 层放得下):

```python
DOMAINS: dict[str, str] = {"http": "请求", "gen": "生成流水线", ...}

@dataclass(frozen=True)
class LogEvent:
    key: str          # "llm.attempt_failed"
    label: str        # "调用尝试失败" —— 界面上显示的中文
    routine: bool     # 例行事件:默认折叠,见 §3.5

EVENTS: dict[str, LogEvent] = {...}

LOGGER_DOMAIN_FALLBACK: tuple[tuple[str, str], ...] = (
    ("app.llm", "llm"),
    ("app.evaluators", "eval"),
    ("app.services.publish", "publish"),
    ...  # 前缀最长匹配
)
```

Web 页的筛选下拉、CLI 的 `--domain` 取值、formatter 的推导表,
全部 import 这里。中文标签只在这一处 —— AuditLogPage 前端持有两张
翻译表是「拉到数据之前就要能列出取值」的历史妥协,ops 页不重复它,
用 `/api/ops/logs/meta` 拿(§5.2)。

### 3.4 兼容期推导:查看器里永远不出现「未分类」

210 个调用点不可能一次迁完,也不应该为了迁移冻结其他开发。
所以 formatter 的规则是:

    写了 event   → domain = event 前缀,注册表提供中文标签
    没写 event   → domain = LOGGER_DOMAIN_FALLBACK 按 logger 前缀最长匹配
                   event 字段缺省,界面上显示 message 原文

推导表覆盖全部 43 个持 logger 的模块,所以迁移期间每一条日志
都有域可归 —— 差别只是没迁的那些没有中文事件标签、不能按事件精筛。
迁移压力就此变成「渐进补精度」而不是「一次换血」。

### 3.5 例行标记:把噪音折叠掉,而不是丢掉

`routine=True` 标在**事件**上(注册表里的元数据,不是调用点参数),
判据:单独一条不承载决策信息、成批出现的过程事件。命中现状里的:

    并发让位   task already claimed / scoring already leased /
               formatting already leased / provider results already leased /
               phase lease was taken over(WARN)
    幂等复用   extraction request reused an existing run / idempotency race resolved /
               batch.create_reused_request_key / spu.create_reused_request_key /
               publish.enqueue_reused
    生命周期   llm.attempt_started / llm.response_received / llm.retrying
    访问日志   http.request_completed 的 2xx/3xx

折叠规则:**流视角里** routine 事件收成一根细计数条
(`例行 ×12 · 租约让位 ×9 · 幂等复用 ×3`,内含 WARN 时单独点名),
点开全展;**链路模式里全展**(attempt_started 对单链路排障有用);
**ERROR 永不折叠**,routine 标记对 ERROR 级不生效。

这样解决 §1.4:告警不再靠级别在噪音里挣扎,靠的是噪音先退场。

---

## 四、存储与查询

### 4.1 Redis 定长列表,不是新依赖,也不是归档

排障要的是「现在、这台、最近几千条」。为此上 Loki/ELK 是杀鸡用牛刀,
写进 PostgreSQL 则把写放大带进每一次 LLM 调用(一次评分 = 4 张候选
× 3 条生命周期日志)。而 Redis 已经是硬依赖(Celery broker、
readiness 探针都在),且 Celery worker 与 API 是**不同进程** ——
进程内环形缓冲会漏掉最有价值的那部分日志(tasks 域 59 个调用点
全在 worker 里)。跨进程、有界、已在部署面里的,只有 Redis。

    键        ops:log_ring(LPUSH 新条目,LTRIM 到 cap)
    cap       OPS_LOG_RING_CAP,默认 5000
    口径      诊断窗口,不是归档。Redis 重启即清空 —— 可接受,
              归档仍然是 stdout + 外部收集器,这条链路一个字节没动

### 4.2 RingHandler:日志绝不反噬业务

`setup_logging` 挂第二个 handler,直连 Redis(不复用 Celery 连接池):

    socket/connect timeout 0.2s;任何异常静默吞掉,进程内计数器 +1
    掉了多少条要能看见:计数器随 /meta 暴露(dropped_since_boot)——
    不赌,但也不瞎
    handler 内部不产生任何 logging 调用(redis-py 的 logger 钉到 WARNING),
    杜绝递归
    写入的就是 JsonFormatter 的产物 —— 脱敏在 formatter 里已经完成,
    环形里没有第二套脱敏逻辑,也就没有第二套漏法

### 4.3 API 契约

```
GET /api/ops/logs?domain=&event=&level=&request_id=&task_id=&q=&limit=200
GET /api/ops/logs/meta
```

两个端点都走管理员闸(与 `/settings` 同级;进 action_gate 的口径
由实现轮定,只读端点按现行规则应属 UNGATED 但要登记理由)。

`/logs` 返回 `{items, ring: {cap, held, oldest_ts, dropped_since_boot}}` ——
窗口边界明说,查不到早于 `oldest_ts` 的不是没发生,是滚出窗口了。
分页从简:cap 才 5000,`limit` 上限 1000,一次拉够;跟随模式
前端按 `(ts, seq)` 去重增量刷新(seq 是 handler 进程内单调计数
+ 进程标识,只为去重与稳定排序,不承诺全局连续)。

`/meta` 返回 `{domains, events, ring}` —— 前端下拉的唯一来源。

`q` 在服务端对 message 与扁平化后的字段值做子串匹配;
`task_id`/`request_id` 精确匹配。都在 API 进程内存里过一遍完成,
5000 条的量级不值得更聪明的做法。

---

## 五、展示

### 5.1 与审计页的分工,一句话说死

    审计(/audit)   谁在什么时候改了什么 —— 合规,入库,长留
    运行(/ops/logs) 系统怎么跑的 —— 排障,环形,短留

两页互链:审计行带着 request_id,点击跳运行日志过滤同一请求;
反向,运行日志里 `settings.updated` 这类有审计对应的事件,
行内给「查审计」链接。入口在侧栏分开放,不合并 —— 合并的结果
是运营在合规页里看见 lease 让位,谁都不舒服。

### 5.2 「运行日志」页:三个视角,一个签名交互

**流视角(默认)。** 紧凑时间线,新在上:

    时间(mono) · 级别色点 · 域 Tag · 事件中文标签 · message ·
    关键字段片(task_id 前 8 位、round、attempt、http_status、provider)

routine 折叠成计数条(§3.5)。行点开:全部结构化字段的键值网格;
`llm.response_received` 这类带脱敏 payload 的,payload 单独一块,
`LLM_LOG_PAYLOADS=false` 时这一块显示「未开启留痕」而不是空着 ——
界面不许说一件没发生的事(走查 P0-3 的口径)。

**域筛选。** 左侧轨道列出十五个域 + 实时计数,点选即筛;
级别、时间窗、搜索在顶栏。全部筛选状态进 URL
(复用 `useUrlFilters`,GAP-033 的教训:刷新不许丢筛选)。

**链路模式(签名交互)。** 点任何一个 `task_id` / `request_id` 字段片,
整页收束成这一条链路的时间线,旧在上(链路要顺着读),
其余一切退场;`task_id` 链路按 `round` 字段分段,段头取该轮
`gen.round_evaluated` 的摘要:

    ── 第 1 轮 · 4 张候选 · B 档 ──────────────

没有 round 的事件(领取、审批、产出)归段外。routine 在链路里全展。
顶部横幅常驻「链路 · 任务 3fca7d2e」+ 退出。这是这一页存在的理由:
§1.3 说字段都在、视角没有 —— 这就是那个视角。

**跟随。** 3s 轮询(项目里 `refetchInterval` 的既有手法),
顶栏一颗「跟随中 / 已暂停」;用户向下滚动即自动暂停,
避免正在读的行被顶走。

视觉全部取 `theme.ts` 令牌:级别三色 danger/warning/textMuted,
域 Tag 一律 marineSoft 底 marine 字(域靠文字区分,不靠颜色 ——
十五种颜色没人记得住),轮次分段线用 sand。暗色跟随现有 darkTokens。

### 5.3 CLI:watch_ai_logs.py → watch_logs.py

保持它最好的两个决定 —— 只读不管进程、编码按本机首选 ——
换掉它最坏的一个:`AI_MESSAGES` 硬编码集删除,过滤从注册表取。

    python backend/tools/watch_logs.py --domain llm,gen --level warning --routine

不带参数默认 `--domain llm --no-routine`,行为向后兼容今天的用途。
CLI 与 Web 是同一套注册表、同一套语义 —— 一个人在终端学会的过滤,
换到页面上不用重学。

---

## 六、原始载荷:看得见发出去的和收回来的

### 6.1 判断:载荷不该和事件走同一条路

事件流要**短、快、能全量扫**;载荷要**完整、有独立寿命、按需取**。
把它们塞进同一条路,就只能二选一:要么载荷压垮事件流(5000 条的环形装几十条就满),
要么事件流挤掉载荷(也就是今天的默认关)。

所以:**两个去向,两个开关,两套寿命。**

| | 去向 | 开关 | 默认 | 寿命 | 谁能看 |
|---|---|---|---|---|---|
| 事件 | 环形缓冲 + stdout | 无 | 一直记 | 环形 5000 条 / stdout 由采集方定 | 管理员 |
| 载荷(诊断) | 旁挂库 `ops:llm:{id}` | `OPS_LLM_PAYLOAD_CAPTURE` | **true** | TTL 24h,到期自灭 | 管理员 |
| 载荷(归档) | stdout 日志行 | `LLM_LOG_PAYLOADS` | **false,不动** | 由采集方定 | 采集链路 |

第三行**逐字维持现状** —— 那条决策的理由(归档面不该躺着商品数据)现在依然成立,
这份设计不碰它。新增的是第二行:一个有 TTL、有管理员闸、不出本机 Redis 的诊断窗口。
风险面不同,所以默认值可以不同;这是整章唯一需要点头的地方。

### 6.2 旁挂库

    键        ops:llm:{llm_call_id}
    值        JSON:请求 + 每次尝试的响应
    写入      HSET + EXPIRE,与 RingHandler 同款 fire-and-forget(§4.2)
              (设计原文是 SETEX,落地改成 hash —— 理由见第十章第四条)
    TTL       OPS_LLM_PAYLOAD_TTL,默认 86400
    上限      OPS_LLM_PAYLOAD_MAX_BYTES,默认 256KB / 次调用,超出按字段截断

三条边界:

- **复用 `safe_payload_for_log`,一行不改。** 图片永远只留 MIME + 字符数 + sha256_16,
  连旁挂库也不存正文 —— 那条规矩是对的,它不是「归档面才需要的谨慎」。
- **字符串上限单独放宽。** 归档面用的 `MAX_LOG_STRING_CHARS = 12_000` 不动;
  旁挂库用 `OPS_PAYLOAD_STRING_CHARS`(默认 40k),因为系统提示词本身就有几千字,
  截在 12k 会把「输出要求」那段切掉,而那段正是排查格式问题要看的。
- **截断必须显形。** `…[truncated N chars]` 界面要画出来,不能让人误以为看到的是全文。

### 6.3 可核对:脱敏不等于看不出真相

请求侧已经有现成的锚点 —— `canonical_json_bytes()` 算出的 `request_sha256_16`,
就是 `content=` 真正发出去的那串字节的摘要(那个函数的存在理由本身就是
「预算与发送必须是同一串字节」)。

界面把它和脱敏视图并排显示,配一行小字:**你看到的是脱敏视图,哈希对应的是发出去的原始字节。**
这样「为什么图片是一行 sha 而不是图」有答案,而不是让人怀疑控制台在藏东西。

### 6.4 API

    GET /api/ops/llm/{llm_call_id}        管理员闸,与 /api/ops/logs 同一道

    200 {
      "llm_call_id": "c41f8a09d2e3",
      "provider": "vision-openai-compat",
      "model": "vision-prod-2026-05",
      "captured_at": "...", "expires_at": "...",
      "request": {
        "endpoint": "...", "params": {...},
        "system": "...", "user": "...",
        "images": [{"tag":"CANDIDATE_IMAGE","mime_type":"image/png",
                    "base64_chars":136204,"sha256_16":"7c02be91d4470f3a"}],
        "headers": {"authorization": "***"},
        "body_bytes": 412083, "sha256_16": "9b7d31e0aa02c4f8"
      },
      "attempts": [
        {"attempt":1,"http_status":429,"duration_ms":775,"content_type":"text/html",
         "upstream_request_id":"vr_01JBXQ8T2P","body":"<html>…"},
        {"attempt":2,"http_status":200,"duration_ms":7244,"content_type":"application/json",
         "upstream_request_id":"vr_01JBXQ9F6D","body":{…}}
      ],
      "truncated": ["request.system"]
    }

    404 {"detail":"载荷已过期或未捕获"}     ← 界面要把这两种情况说清楚,不能只显示一个空面板

### 6.5 界面:原文永远在一键之内

行展开从「一张字段网格」改成**三个页签**:

    字段          结构化 extra_fields —— 索引用
    原始日志行    stdout 里逐字的那一行,带复制
    模型载荷      旁挂库的完整请求与响应(仅带 llm_call_id 的行)

**「原始日志行」对每一条事件都有。** 这条是态度问题:分类法是索引,不是转述。
控制台把 event 码和中文标签摆在前面,是为了让人快速定位;
一旦定位到了,原文必须零成本可得,否则这套分类就变成了一层遮挡。

**「模型载荷」的形状**:左请求右响应。

- 左:模型与参数一行;`sha256_16` 与字节数一行(§6.3);system / user 分块,
  超长默认收起留「展开全文(N 字符)」;图片画成 chip,显示 tag、MIME、base64 字符数、sha;
  headers 里的 `Bearer` 已是 `***`。
- 右:多次尝试用小页签切,红点失败绿点成功。**这是这一屏真正的价值** ——
  「重试后成功了但结果不一样」「第一次是 429 第二次是 200」这类问题,
  只有把两次尝试摆在同一个切换器里才看得出来。
- 非 JSON 响应**原文照显**,并标注 `content-type`(§1.5 末尾那种情况)。
- JSON 响应额外给一块 `output_text(解析后)` —— 模型把结构化结果塞在字符串里返回是常态,
  转义后的原文没法读,但**原文那块必须同时在场**,解析块是补充不是替代。
- 整次调用可下载成 JSON,方便贴进 issue 或发给厂商。

`call {id}` 芯片直接点进载荷页签 —— 与 `task_id` 芯片点进链路是同一个手势。

---

## 七、迁移

    P0  注册表 + formatter 小改 + RingHandler + watch_logs 改造
        (半天量级;从这一刻起查看器不再依赖消息原文)
    P1  五个高价值域补 event 码:llm / gen / eval / batch / publish
        (覆盖排障流量的大头;每域一张映射表进 PR 描述)
    P1½ 载荷旁挂库 + /api/ops/llm/{id}
        (只碰 transport.py 两处已有的 if 分支旁边,加一个写旁挂库的调用;
         脱敏函数不改,归档面开关不改。可与 P1 并行,也可先于 P1 上 ——
         它不依赖 event 码,只依赖已经存在的 llm_call_id)
    P2  /api/ops/logs + /meta + 前端页(含三个页签与载荷面板)
        (P0/P1 不阻塞它 —— 推导兜底保证没迁的域也能看)

llm 域的完整映射,作为 P1 的样板:

| 现 message | event | routine |
| --- | --- | --- |
| llm request prepared | `llm.request_prepared` | 否 |
| llm request attempt started | `llm.attempt_started` | 是 |
| llm http response received | `llm.response_received` | 是 |
| llm request completed | `llm.request_completed` | 否 |
| llm request attempt failed | `llm.attempt_failed` | 否 |
| llm request retrying | `llm.retrying` | 是 |
| image compacted for multimodal request budget | `llm.image_compacted` | 否 |
| vision request fitted to endpoint body budget | `llm.request_fitted` | 否 |
| image needed exif rotation, sending inline instead of by url | `llm.image_inline_fallback` | 否 |
| cannot apply exif orientation, sending the original bytes | `llm.exif_rotation_failed` | 否 |

## 八、守卫

按仓库惯例,约定不许只写在文档里。四条,前三条 AST:

一,**事件必注册。** 扫描全部调用点里的 `event="..."` 字面量,
每一个必须在 `EVENTS` 里;`EVENTS` 每一项的域前缀必须在 `DOMAINS` 里;
`LOGGER_DOMAIN_FALLBACK` 必须覆盖全部持 `get_logger(__name__)` 的模块
(新模块不进推导表就红 —— 「未分类」不许在查看器里出现)。

二,**载荷只走脱敏函数。** 写旁挂库的那一处,AST 断言实参必须是
`safe_payload_for_log(...)` 的返回值,不许把 `request.body` / `response.json()`
直接递进去。这条和现有的「摘要里不出现 base64」是同一条规矩的延长线 ——
新开一个去向,最容易漏的就是在新去向上把老规矩忘了。
配一条运行时测试:构造带 `data:image/png;base64,...` 与 `Authorization` 的请求,
断言落进旁挂库的 JSON 里既没有 base64 正文也没有明文密钥。

三,**查看器不持有消息字面量。** `tools/watch_logs.py` 源码里
不许出现任何注册表事件的 message 原文,且必须 import `log_events`
—— 反向断言吃去注释的源码(a51 变异验红的教训:正向断言会命中
docstring 里的同一串字)。

四,**前端不内置分类表。** Vitest 检查 ops 页源文件不含任何
event 码字面量、不 import 本地常量表 —— 下拉取值只能来自 `/meta`。
这是硬规则 4 在这一页的具体形状。

## 九、刻意不做的

**不上日志收集栈。** 单节点 compose 的部署现实下,Loki/ELK 换来的
是一个要运维的新系统,解决的却是环形缓冲已经解决的问题。
外部收集器想接,接 stdout —— 那条链路一个字节没动。

**不入库。** 审计表管合规。运行日志入库意味着每次 LLM 调用多三次
INSERT,以及一张只增不减、无人清理的表。

**不强制全量 event。** 守卫钉「写了的必须注册」,不钉「必须写」。
推导兜底让没迁的调用点保持可归类,迁移是补精度,不是还债。

**`LLM_LOG_PAYLOADS` 的语义与默认值不动。** 归档面那条决策依旧成立;
新增的旁挂库是另一个去向、另一套寿命(§6.1),不是把它偷偷打开。

**图片正文依旧永不留存。** 连旁挂库也只存 MIME、字符数与 sha256_16。

**不做提示词版本管理与 diff。** 要的是「这一次到底发了什么」,
不是一个提示词工程平台。两次尝试的对照到「并排切换」为止。

---

## 十、落地记录(a53):五处订正,加一件撞出来的缺陷

设计与实现出入的地方逐条记在这里。**正文已经按订正后的口径改过**,
所以读正文不会被误导;这一章回答的是"为什么变",给下一个想改回去的人看。

### 1. `poll_service` 归 `publish`,不归 `gen`(§3.2)

设计表把它划给 `gen`,多半是把它读成了"生成轮询"。它不是:五条调用点的
message 全部以 `publish.` 开头(`publish.poll_status_changed` 等),
`CLAUDE.md` 把它列在发布链路七个模块里。

划错的代价很具体:排一次"上架卡住了",运营在**生成流水线**那一格里找不到
任何东西,而答案就在隔壁。

### 2. `llm` 域是 11 条,不是 §7 表里的 10 条

§7 那张表写着「llm 域的完整映射」,而它:

    收进了一条其实住在 `app/evaluators/` 的      `llm.request_fitted`
    漏掉了 `app/llm/images.py` 的第四个调用点     `llm.image_url_unverifiable`

前一条**是对的做法**(事件码优先于 logger 前缀,一个模块可以产出别的域的
事件),后一条是漏。两条都按代码收进注册表。

### 3. `gen.round_evaluated` 与 §3.2 的域表冲突,取前者

§5.2 明确写着链路分段的段头取 `gen.round_evaluated`,而 §3.2 把
`evaluation_service` 划给 `eval`。冲突取 §5.2:"这一轮评完了"是生成流水线的
里程碑,它出现在任务链路的时间线上,而不是评分器的内部账。同一个文件里
其余八条仍归 `eval`。

**这正是"域的判据是业务领域"那句话的用法** —— 域跟着排障的人怎么问走,
不跟着文件路径走。

### 4. 旁挂库是 hash,不是 SETEX(§6.2)

一次调用要写多次:一次请求 + 最多 N 次尝试。SETEX 意味着每次尝试都要
"读回来、改一改、整个写回去" —— 三次重试就是三次读加三次写,而每一次都在
付费调用的热路径上。`HSET + EXPIRE` 是同一件事的一次往返版本,TTL 语义不变。

### 5. 域从"十五个"变成十五个,但 `eval` 的标签改了

`eval` 的中文标签从「评分」改成「评分与审核」:`review_service` 与
`api/reviews.py` 的调用点没有别的去处,而把它们塞进一个叫「评分」的格子里,
运营找"人工审核出了什么问题"时不会点进去。

### 6. 撞出来的缺陷:14 个调用点的结构化字段**从来没进过日志**

接线时发现的,设计文档里没有,因为它不在设计的范围内 —— 它一直躺在采集面上:

```python
logger.warning("could not write the in-flight marker", extra={"key": ..., "error": ...})
                                                              ↑ 少了 extra_fields 那一层
```

`JsonFormatter` 只读 `record.extra_fields`。少了那层包裹,`logging` 会把这些键
挂到 record 上,然后**没有任何人去看**。不报错、不提示,那条日志只是比作者
以为的少了一半 —— 而作者是在出事时才会去读它的。

14 处里最贵的一条是
`batch.billed_result_unknown_refusing_paid_retry`(已计费但结果未知,拒绝付费
重试):它记的 `key` / `action` / `status` 一个都没落地,于是"到底是哪一件被
拒了"这个问题,在日志里查不到答案。

全部补上了包裹,并加了第四条守卫(`test_a53_log_console.py` 的
`test_every_logger_call_wraps_its_fields_in_extra_fields`),配一条把
formatter 真的跑一遍的用例钉住理由 —— 免得下一个人把守卫读成风格洁癖。

## 十一、这一轮**没有**做的

- **浏览器里一次都没实测。** 前端门禁(typecheck / lint / Vitest / build)全绿,
  Playwright 用例没写(任务 24)。
- **真 Redis 一次都没连过。** 环形写入、旁挂库读写、TTL 到期,三条都只有
  纯测试用假客户端覆盖。本地协作默认不碰真实基础设施(`CLAUDE.md` 那一节),
  需要用户明确触发。
- **`make test-nodb` 与 `pytest` 没跑。** 这台机器没装 fastapi / sqlalchemy。
  受影响的面:三个新端点没有被 TestClient 打过,`action_gate` 的写端点闸表
  与它们无关(全是 GET),但这句话本身也**只是推理,不是验证**。
- **迁移到 event 码的是全部 210 个调用点**,但"迁完了"只等于"码写上了、
  注册了、双向对得上",不等于每条码的**中文标签**都被人读过一遍。
