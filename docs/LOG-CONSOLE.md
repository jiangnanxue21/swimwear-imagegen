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

> **这条禁令的适用边界(a55 补)。** 它反对的是「把高频自动写入塞进 OLTP 库」,
> 判据是**触发方式与量级**,不是「它是不是观测数据」。`ai_test_runs`
> (BLOCK-30)是人点按钮触发、每天个位数、要和提示词版本对照留 180 天 ——
> 三条都不成立,所以那张表不受这一条约束。理由记在 `docs/DECISIONS.md` §3.87;
> 写在这里是因为不写的话,下一个人会拿这一段把那张表否掉。

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

## 十一、a53 那一轮**没有**做的

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

## 十二、落地记录(a54):走查捡到的十件事

a53 的守卫全绿、门禁全绿,而下面这十件事**一件都没有被它们看见**。共同形状:
不报错、不变红,只在某个具体时刻让控制台少说一句真话。全部补了守卫,并用
`tools/mutate_a54.py` 逐条变异验红(11/11)。

### 1. worker 的日志一条都没进过环形(P0)

`setup_logging()` 只在 `app/main.py` 顶层调过,而 worker 的入口不 import 它。
于是页面上 `gen` / `batch` / `publish` 三个域基本空着,**看起来像"这段时间
没跑任务"**。取舍与钩子的选择记在 `DECISIONS.md` §3.81。

第九章那条自检顺序(先看 `held` 涨不涨)恰好发现不了它 —— API 进程自己在写,
那个数一直在涨。**自检第一步改成:跑一个生成任务,看 `gen` / `batch` 域
出不出条目。**

### 2. 打开这一页的动作在冲刷诊断窗口(P0)

见 §3.81 第二条。修法是 `core/log_ring.SelfTrafficFilter`。

### 3. 级别筛选:API 精确匹配,CLI 是「及以上」

同一个词在两个入口两种意思,而 §5.3 写的是「一个人在终端学会的过滤,换到
页面上不用重学」。更糟的是精确匹配把 §1.4 那个病放了回来:运营选 WARNING
想找问题,**ERROR 被过滤掉了**。

级别序、`level_at_least()`、`folds_away()` 三样并进 `core/log_events.py`,
API 与 CLI 都调它。顺带修掉 CRITICAL:上一版 API 写 `level != "ERROR"`、
CLI 写 `< 40`,同一条 CRITICAL 的例行事件页面折起来、终端展开着。

### 4. 环形被关掉时,界面画的正是一张空列表

`OPS_LOG_RING_ENABLED=false` 时 handler 不挂,但 `/logs` 照样去读 Redis,
读到空 —— 而前端那条提示只在有 `unavailable_reason` 时才渲染,
`!ring.enabled` 那半句嵌在它里面,**永远到不了**。现在直接返回
`unavailable_reason="ring_disabled"`。

### 5. 字节预算砍掉的字段不显形

顺序是 `truncated_paths()` 在前、`_fit()` 在后,于是 256KB 那一档砍掉的东西
一条都没进 `truncated`,而它恰恰砍得最狠。§6.2 第三条边界写的是**截断必须显形**。

### 6. 连接自检往旁挂库写垃圾键

`evaluators/vision.py` 的 `test_connection` 直接调 `_send_once`,绕过
`_send_with_retries`,contextvar 还是默认值 `"-"`。上一版只挡空串,于是每点
一次「测试连接」就往 `ops:llm:-` 覆盖写一次,还顺手续 TTL。`"-"` 不是 id,
是「这条路径没有 id」。

### 7. 旁挂库写失败一条都不记

`except` 直接 `return`:不记数、不进冷却期。环形那边的口径是「不赌,但也不瞎」,
旁挂库这边是纯瞎 —— `/api/ops/llm/{id}` 报 404 时分不清"过期了"还是"从来
没写进去过",而这两者的下一步完全相反。计数现在随 `/meta` 的 `payload_capture` 出来。

### 8. `duration_ms` 永远是 null

`_send_once` 没测时长,`capture_attempt` 传的是写死的 `None`。而 §6.4 样例里
两次尝试的耗时对照(775ms / 7244ms)正是载荷面板并排摆两个页签的理由。
顺带给 `llm.retrying` 补上 `llm_call_id` —— 少了它,「为什么这一次等了 8 秒」
在链路模式里会从这条调用的时间线上掉队。

### 9. 前端:三件设计写了而没落地的,加两件坏掉的

    没落地   链路按 round 分段(§5.2 的签名交互)、折叠条点开全展(§3.5)、
             事件精筛的入口(后端/类型/URL 都支持,唯独没有界面)
    没落地   载荷面板的图片 chip、headers、展开全文、output_text 解析块、
             整包下载(§6.5 逐条)
    坏掉的   「滚动即暂停跟随」监听的是一个**不滚动的 div**(没有 overflow、
             没有高度),`scrollTop` 恒为 0,这个功能一次都没触发过
    坏掉的   域计数按已经筛过的那一屏算,点进一个域之后其余十四格全是 0

另外 `expanded` 这个 URL 参数**声明了却全页零引用**,展开态刷新即丢
(GAP-033 的教训没兑现);`<a href="/audit">` 在 SPA 里是整页刷新;
§5.1 的互链只做了一半,审计页那边现在补上了 `request_id -> 运行日志`。

哪条事件当链路段头由后端给一个布尔(`round_summary`),前端不认事件码 ——
硬规则第 4 条在这一处的形状。

### 10. `/logs` 先成形再筛

`_shape` 里有一次 `json.dumps`,上一版对全窗 5000 条每条都做一遍,而绝大多数
会被随后的筛选丢掉;跟随模式 3 秒一拍,这笔开销是常驻的。现在先在原始 dict
上判,只有入选的那 `limit` 条才成形。

---

**这一轮仍然没有做的:** 真 Redis 一次都没连过、浏览器一次都没打开过、
`pytest` / `make test-nodb` / 前端 `tsc` 与 Vitest 都没跑(这台机器没有
fastapi / sqlalchemy,也没有 `node_modules`)。也就是说:**前端那八项修复
一行都没有被执行过**,它们只过了源码级的守卫与人工复读。

---

## 十三、落地记录(a55):七件事,和一条看走了眼的守卫

a54 的守卫全绿、门禁全绿。下面七件事**一件都没有被它们看见**,而其中一件
就是守卫自己。

### 0. 根因:这套东西一次都没有被运行过

第十一章、第十二章末尾各写了一遍"真 Redis 没连过、浏览器没打开过"。
仓库里有旁证:`backend/.api-stdout.log` 的时间戳是 08-15,而整套控制台的
代码是 08-16 写的。那唯一一条真实日志**没有 `domain`、没有 `event`** ——
仓库里存在的唯一一份真实运行日志,是控制台上线之前的格式。

所以下面这七条不是"难用",是"没用过"。它们都是第一次打开就会撞上的。

### 1. 环形被访问日志冲刷 —— a54 只修了自指那一半(P0)

a54 挡住了 `/api/ops/` 前缀。而前端有 **17 处 `refetchInterval`**:打开一个
任务详情页 ≈ 90 请求/分钟 = **5400 条/小时**,cap 是 5000。**一个开着任务
详情页的标签,一小时内把整个诊断窗口洗一遍** —— 而排障的人恰恰会开着
任务详情页。

**修了一半比不修更危险**:这些行标了 routine,折进计数条;`held/cap` 显示
5000/5000,看起来非常健康,于是没人再怀疑它。

判据从"路径"换成"这条访问日志有没有诊断价值":`OPS_LOG_RING_ACCESS`
默认 `errors`(只收 4xx/5xx),`all` 保留 a54 的行为。`SelfTrafficFilter`
更名 `AccessNoiseFilter`,旧名留作别名。

**归档面一个字节没动**,而且界面必须把这个取舍说出来 —— `/meta` 报
`access_mode`,页面画一行小字。窗口可以少装东西,但不许让人把"我没收"
读成"没发生"。

### 2. 跟随模式的开销没降,而守卫是绿的(P0,**本轮最重要的一条**)

a54 的 `test_the_stream_is_filtered_before_it_is_shaped` 断言 `list_logs` 里
`_matches` 的调用**行号**小于 `_shape` 的。它一直是绿的。而调用方的循环里
还留着一行:

```python
for row in rows:
    raw = json.dumps(...)          # ← 无条件,全窗 5000 条各一次
    if not _matches(row, raw=raw): continue
```

`_shape` 确实挪到了筛选后面,**而真正贵的那次 dumps 没有** —— 因为 `q`
匹配需要一行原文,而 `read_ring` 把 LRANGE 拿回来的字符串 `json.loads`
完就丢了。`_matches` 的文档字符串当时就写着「`q` 直接匹配 LRANGE 拿回来的
那一行原文」。**代码没有这么做。**

修法是让 `read_ring` 返回 `(原始行, 解析后)`。开销从
`5000 loads + 5000 dumps` 降到 `5000 loads + limit dumps`。

**守卫换成数调用次数**,旧的行号断言删除。删除的理由写在原处 ——
把它改回 AST 断言看起来会更"快、更纯",而那正是它上一次失效的样子。

> 这一条是第八章那四条守卫的一个反例。「约定不许只写在文档里」是对的,
> 但**写成守卫也不等于守住了**:一条按代码形状判的断言,挡不住"形状对了
> 而行为没变"。凡是"做了什么"能被观察的,就别去比代码长什么样。

### 3. 分不出这条日志是哪个进程写的(P0)

`build_payload` 的顶层字段里没有 `service`、没有 `pid`。API / worker / beat /
script 全部 LPUSH 进同一个键,于是「`gen` 域为什么是空的」**答不出来** ——
是没跑任务,还是 worker 挂了?这两件事的下一步完全相反。

这是第十二章第 1 条的下一层:那一条修的是"worker 的日志一条都没进过环形",
修完之后的新问题是"进来了但认不出是谁"。

`setup_logging(level, *, service)`,四个入口各自报名。默认值是 `api` ——
所以**一个没报名字的 worker 会自称 api**,那比没有这个字段更糟:它不是
"不知道",是一句错话,而且看起来完全正常。守卫逐个入口钉着。

`/meta` 的 `services_seen` 是**从窗口里数出来的**,不是一张写死的表:
写死的表会在 worker 没起来时依然列出 `worker`,而那正是要发现的事。
第十二章第 1 条留下的自检顺序因此可以改:**第一步变成"打开页面看有没有
worker"**,不用先跑一个生成任务。

### 4. 排序是"Redis 到达顺序",而链路模式承诺"顺着读"(P0)

后端不排序,前端也不排序。环形是多个进程 fire-and-forget LPUSH 的,列表
顺序是到达顺序;而链路模式的横幅上写着「按时间顺读,旧在上」。**一条
API 领取、worker 执行、API 回写的任务链路,展示顺序可能是错的,而界面
正在向你保证它是对的。**

改成对命中的行按 `(ts, seq)` 排序后再截 `limit`。时间戳解析不出来的沉到
末尾并带 `ts_unparsed`,界面画一个「时间未知」标 —— 不静默丢,也不假装
它在某个位置。兜底一个时间会让它安静地落在某处,而"它到底该排哪"
恰恰是答不出来的那件事。

### 5. 没有时间窗,而 §5.2 写了(P1)

§5.2 白纸黑字「级别、时间窗、搜索在顶栏」,而端点参数里一个时间字段都没有。
排障最常见的第一句话「昨天下午三点前后发生了什么」这套控制台答不出来。

新增 `since` / `until`。**解析不出来就 400,不当没填** —— 当没填的话调用方
会拿到一整窗的结果并以为"那就是那段时间里发生的全部事"。

前端预设按钮换算出的是**绝对时间戳**才进 URL:相对值会让分享出去的链接
在对方那里指向另一段时间,而"把链接发给同事"是这一页最常见的动作之一。

### 6. `limit` 截断是静默的,而它和域计数互相矛盾(P1)

`if len(items) < limit: append`,超出就停,响应里一个字都没有。而 a54 刚把
域计数改成按全窗算 —— 于是命中 800 条、limit 200 时,**左边显示 800、
右边显示 200,两个数字互相矛盾,页面上没有一处解释**。

同页那行「窗口里最早的一条是 X」说的是全窗边界,而列表因为被截断实际起点
比 X 晚得多 —— 那句话在此刻是在误导人。

新增 `matched` / `truncated` / `shown_oldest_ts`。**`shown_oldest_ts` 与
`oldest_ts` 是两个数,谁也不冒充谁。**

顺带:域轨道卡片标题「领域(本屏计数)」改成「全窗计数」—— 它和自己下面
那行 `note`(「计数按整个环形窗口算」)从 a54 起就一直在打架,而两句话里
只有一句是真的。

### 7. CLI 用错了编码,而正确答案就写在隔壁文件(P1)

`core/logging.py` 有一整段注释讲为什么必须强制 UTF-8(中文 Windows 上 GBK
会让**整条记录消失**,而那类记录往往正是出问题的那一条)。而
`tools/watch_logs.py` 读同一个文件用的是 `locale.getpreferredencoding(False)`。

**写的时候强制 UTF-8,读的时候用系统代码页。** 中文 Windows 下所有中文
字段变成乱码,而 JSON 结构是纯 ASCII,`json.loads` 照样成功,**不报任何错**
—— 查看器安静地展示乱码,而人会以为是日志本身写坏了。

固定 UTF-8;顺带新增 `--tail`(默认 200):上一版 `seek(0, 2)` 直接跳到文件
末尾,CLI **完全看不了历史**,而这个工具最常见的用法恰恰是"刚才出的那个
错,给我看看"。

### 8. 一个可选的辅助函数(不迁移)

a54 抓到 14 处漏写 `extra_fields` 的调用点,修法是加一条 AST 守卫。
**守卫挡的是"已经写错了",`core.logging.log()` 挡的是"写得出错"。**
两者不冲突,后者更早。本轮只提供它,**不迁移任何现有调用点** ——
217 处的机械改动会把这一轮的 diff 淹掉。

---

**这一轮仍然没有做的:** 真 Redis 一次都没连过、浏览器一次都没打开过、
`pytest` / `make test-nodb` / 前端 `tsc` 与 Vitest 都没跑(这台机器没有
fastapi / sqlalchemy,也没有 `node_modules`)。**前端那六项改动一行都没有
被执行过。** 与第十一、十二章同一个处境 —— 而本轮修的七条里有三条的根因
就是这个处境,这一点值得写在这里而不是被含糊过去。
