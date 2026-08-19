# PRD:运行日志控制台整改 + 提示词统一管理与 AI 测试留档

- 版本:**v3.0**(v2.0 之上按 a55 / a56 / a57 的落地结果回填状态并决议两条待决问题)
- 状态:BLOCK-31 已收口;BLOCK-30 落到 S3 后端半,S2 本轮落码,S4 / S5 未开工
- 任务块:**BLOCK-31**(LG-401 ~ LG-412)、**BLOCK-30**(BE-301 ~ BE-311 / FE-301 ~ FE-315)
- 验收编号:AC-23 ~ AC-36(接现有 AC-01 ~ AC-22)
- 依赖:`prompt_templates`(migration 0007)、`evaluation_attempts`(migration 0009)、浏览器登录(PRD §3.3 方案 A)、`docs/LOG-CONSOLE.md`(a53/a54 已落地部分)

---

## 版本说明:v1.0 → v2.0 改了什么

v1.0 的取证我逐条核对过,**几乎全部属实**(行号、fail-open 的写法、四套版本口径、`diagnose_copy` 只记账不存输出、NG1 的依赖论证)。v2.0 的改动是六类:

| # | 改动 | 原因 |
|---|---|---|
| 1 | **迁移编号 0031/0032 → 0055/0056** | v1.0 写的两个编号**都已被占用**(`0031_receipt_provider_call_marker`、`0032_batch_request_idempotency`),当前最新是 `0054_async_attribute_extractions`。这条不改会在合并后撞车,而且是本地跑得通、合并才炸的撞法 |
| 2 | 新增 §6 **事件码登记义务** | `test_a53_log_console.py` 是**双向**比对:登记了没人写**也会红**。v1.0 全篇未提 |
| 3 | 新增 §4.5 **入库口径冲突的显式记录** | `LOG-CONSOLE.md` §九写着「不入库」,而 `ai_test_runs` 是入库。两份文档对同一件事口径相反,不写一笔就会被下一个人拿 §九否掉 |
| 4 | 新增 AC-30b、AC-35、AC-36 | AC-30 只证了「改措辞版本号必变」,没证反向;乐观锁没覆盖 activate/reset |
| 5 | §9 待决问题 2 **就地决议** | v1.0 建议 `MAPPING` 只读,却又在 §3 / BE-304 / AC-25 里把它当可编辑的。自相矛盾,本稿决议 |
| 6 | **新增 BLOCK-31 运行日志整改**,并排在 BLOCK-30 之前 | 见下 |

**为什么新增日志块,而且排在前面。** BLOCK-30 的 §5.5 要把 AI 测试落库并可查,那是一件观测性工作;而这个仓库已经有一套观测性设施(运行日志控制台,a53/a54),它现在有七条毛病。**在一套自己都不可信的观测设施旁边再盖一间,只会有两间不可信的房子。** 而且日志块的两条 P0 会直接影响 BLOCK-30 的排障效率——改一版提示词、跑一发测试、去日志里看它到底发了什么,是这个 PRD 描述的核心闭环。

---

## 版本说明:v2.0 → v3.0 改了什么

v2.0 是一份**没有状态列的计划书**。它写清了要做什么,没有地方回答"做到哪儿了" ——
而这个仓库里每一次"两份真相分叉"的事故,起手式都是一张改了代码没改的表(§3.70)。
v3.0 的改动是四类,一条新功能都没加:

| # | 改动 | 原因 |
|---|---|---|
| 1 | §6 / §13 / §16 三张表**各加一列状态** | 计划表不带状态,过期时不会有任何东西变红。a56 之后 PRD 与代码已经分叉过一次:§14.1 说男装"保留待决",而代码里那行 `ui_reachable=False` 早就把它决了 |
| 2 | §14.1 / §14.3 **就地决议**,§14.4 维持待决但写明当前口径 | 两条待决问题都已被落地事实回答,只是没人回来改这份文档 |
| 3 | §11.3 补记 **迁移 0055 指着一张不存在的表**,以及它为什么没被任何东西发现 | a57 修。这是本 PRD 迄今唯一一条会在真库上直接失败的交付物 |
| 4 | §11.4 保留期从"一个待产品拍板的数"改成**一个不变量 + 一个可调默认值** | 见 §14.3 |

**这份 PRD 从 v3.0 起是活文档,不是一次性方案书。** 判据很简单:哪一天代码变了而
这三张表没变,`docs/DECISIONS.md` 里就该多一条解释为什么 —— 而不是让读的人自己去
比对代码。

---

# 第一部分:BLOCK-31 运行日志控制台整改

## 1. 一句话

`docs/LOG-CONSOLE.md` 的设计是对的,a53/a54 落了大半;剩下七条毛病的共同形状是**界面看起来健康,而它正在丢东西或说假话**。本块把这七条修完,并把「守卫按代码形状判」这个已经放跑过一次的失效方式换成按行为判。

## 2. 背景:根因是它一次都没有被运行过

`docs/LOG-CONSOLE.md` 第十一章与第十二章末尾,项目自己记着:

> 真 Redis 一次都没连过、浏览器一次都没打开过、`pytest` / `make test-nodb` / 前端 `tsc` 与 Vitest 都没跑

包里有旁证:`backend/.api-stdout.log` 的时间戳是 **08-15 15:34**,而整套控制台的代码是 **08-16** 写的。那唯一一条真实日志是:

```json
{"ts":"2026-08-15T15:34:25+0800","level":"INFO","logger":"app.main",
 "message":"application started","request_id":"-","env":"local", ...}
```

**没有 `domain`,没有 `event`** —— 仓库里存在的唯一一份真实运行日志,是控制台上线之前的格式。

所以下面七条不是「难用」,是「没用过」。它们都是第一次打开就会撞上的。

## 3. 七条病灶,逐条带证据

### 3.1(P0)环形窗口被访问日志冲刷 —— a54 只修了自指那一半

a54 发现运行日志页 3 秒一拍轮询自己,四小时后环形里 100% 是「某人在看运行日志」,修法是 `core/log_ring.SelfTrafficFilter`,**只挡 `API_PREFIX + "/ops/"` 前缀**。

但前端有 **17 处 `refetchInterval`**:

```
TaskDetailPage        三个轮询器,2~3 秒一拍
WorkbenchBatchPage    两个,2~3 秒
ReviewQueuePage       15 秒(PENDING 时)
SystemStatusPage      15 秒
DashboardPage         30 秒
```

`app/main.py` 的中间件对**每个请求**写一条 `http.request_completed`,无采样。打开一个任务详情页 ≈ 90 请求/分钟 = **5400 条/小时**,而 `OPS_LOG_RING_CAP` 默认 5000。

**一个开着任务详情页的浏览器标签,一小时内把整个诊断窗口洗一遍** —— 而排障的人恰恰会开着任务详情页。

比不修更糟的是:这些行标了 `routine`,在流视角里折进计数条;`held/cap` 显示 5000/5000,看起来非常健康。**自指那一半修了,其余全部没修,而修过的那一半让人以为这个病已经过去了。**

### 3.2(P0)跟随模式的开销没有真的降下来,而守卫是绿的

a54 有一条守卫 `test_the_stream_is_filtered_before_it_is_shaped`,断言 `list_logs` 里 `_matches` 的首次调用行号小于 `_shape` 的。它是绿的。而 `api/ops_logs.py:214-216`:

```python
for row in rows:                                  # rows = 全窗 5000 条
    raw = json.dumps({...}, ensure_ascii=False)   # ← 每条都做,无条件
    if not _matches(row, ..., raw=raw):
        continue
```

`_shape()` 确实挪到了筛选后面,**但真正贵的那次 `json.dumps` 还留在前面** —— 因为 `q` 子串匹配需要它。而 `core/log_ring.read_ring()` 把 LRANGE 拿回来的原始字符串 `json.loads` 之后就丢了,所以这里只能重新序列化一遍。

`_matches` 的文档字符串写着「`q` 直接匹配 LRANGE 拿回来的那一行原文」。**代码没有这么做。**

于是每 3 秒一拍 = LRANGE 5000 条(约 2~10MB)+ 5000 次 `loads` + 5000 次 `dumps`,**与 `limit` 填 100 还是 1000 无关**。

这是本仓最忌讳那种失效方式的教科书样本:**守卫按代码形状判(行号先后),而它要挡的是开销。形状对了,开销没变,守卫绿着。**

### 3.3(P0)分不出这条日志是哪个进程写的

`JsonFormatter.build_payload` 产出的顶层字段是 `ts / level / logger / domain / message / request_id` + extra。**没有 `service`,没有 `pid`,没有 `host`。**

API、Celery worker、beat 全部 LPUSH 进同一个 `ops:log_ring` 键。于是:

- 「`gen` 域为什么是空的」→ 是没跑任务,还是 **worker 挂了**?页面回答不了。
- 多副本部署时两个 worker 的日志混在一起,分不开。

唯一带进程信息的是 `seq`(`f"{pid:x}-{n}"`),但它没有被解析、没有暴露成可读字段、更不能筛。

**这一条与 a54 第 1 条是同一个坑的下一层**:a54 修的是「worker 的日志一条都没进过环形」,修完之后新问题是「进来了但认不出是谁」,而排障的第一个问题恰恰是「哪个进程」。

### 3.4(P0)排序是「Redis 到达顺序」,而链路模式承诺「顺着读」

后端不排序;前端也不排序 —— `inTrace ? [...rows].reverse() : rows`。列表顺序 = LPUSH 到达顺序。

跨进程时这个顺序不等于时间顺序:worker 与 API 各自 fire-and-forget 写入(0.2 秒超时),排队与网络抖动都会打乱先后。而链路模式的横幅上写着「**按时间顺读,旧在上**」。

**一条从 API 领取、worker 执行、API 回写的任务链路,展示顺序可能是错的,而界面正在向你保证它是对的。**

### 3.5(P1)没有时间窗,而设计文档写了

`docs/LOG-CONSOLE.md` §5.2 白纸黑字:「级别、时间窗、搜索在顶栏」。

`GET /api/ops/logs` 的参数是 `domain / event / level / request_id / task_id / q / limit`。**没有任何时间参数**,前端也没有任何时间控件。

排障最常见的第一句话是「昨天下午三点前后发生了什么」。这套控制台回答不了。

### 3.6(P1)`limit` 截断是静默的,而它和域计数互相矛盾

`list_logs` 里 `if len(items) < limit: items.append(...)`,超出就停,**响应里没有任何字段说明被截断了**。

而 a54 刚把域计数改成按全窗算。于是:命中 800 条、`limit=200` 时,左侧域轨道显示 **800**,右侧列表显示 **200**,两个数字互相矛盾,页面上没有一处解释。

同页 `oldest_ts` 那行小字说「窗口里最早的一条是 X —— 更早的不是没发生,是滚出窗口了」。它说的是**全窗**最老的一条,而列表因为被截断,实际起点比 X 晚得多。这行字此刻在误导人。

另外 `OpsLogPage.tsx` 域轨道的卡片标题写着「领域(**本屏计数**)」,而同一个组件的 `note` 属性写着「计数按整个环形窗口算,不受选中的域影响」。**标题是 a54 改动前的遗留,和自己的副标题打架。**

### 3.7(P1)CLI 用错了编码,而正确答案就写在隔壁文件

`core/logging.py:126-147` 有一段很长的注释,讲为什么必须强制 UTF-8:

> 中文 Windows 上 stdout 默认是 GBK,轻则整份日志不是 UTF-8、采集端读出乱码,重则遇到一个 GBK 编不出的字符抛 `UnicodeEncodeError` —— logging 会吞掉它,**整条记录消失**。而这类记录往往正是出问题的那一条。

然后 `tools/watch_logs.py:69` 读同一个文件:

```python
def _encoding() -> str:
    return locale.getpreferredencoding(False) or "utf-8"
```

**写的时候强制 UTF-8,读的时候用系统代码页。** 在中文 Windows 开发机上(仓库路径实测是 `D:\source code\swimwear-imagegen`),所有中文 message 与字段值会变成乱码 —— 而 JSON 结构是纯 ASCII,`json.loads` 照样成功,**不报任何错**。

同一个文件还有第二条:`_follow_file` 开头 `stream.seek(0, 2)` 直接跳到文件末尾,**CLI 完全看不了历史**,只能等新日志。而「刚才出的那个错」正是最常见的用法。

### 3.8(P2,顺带)交付包里仍然带着运行日志

`tools/pack.sh:160` 与 `tools/pack.ps1:86` 的禁品数组里都有 `'*.log'`,`docs/DECISIONS.md` 的 A46 P0-1 记着这是修过的事故(运行日志随包出去过两次,含图片绝对路径与上游 URL 查询串)。

**本次收到的交付包里仍然有 `backend/.api-stderr.log` 与 `backend/.api-stdout.log`。** 三道拦截全部没拦住。本次内容无害,但机制显然没生效 —— 需要确认是不是没走 `make pack`。

## 4. 目标与非目标

### 4.1 目标

- **LG-G1** 诊断窗口装的是诊断信息,不是「有人在用这个系统」的证据。
- **LG-G2** 跟随模式的开销与 `limit` 成正比,不与 `cap` 成正比。
- **LG-G3** 每一条日志答得出「哪个进程写的」。
- **LG-G4** 界面上的时间顺序就是真实时间顺序;做不到时说出来,不假装。
- **LG-G5** 界面上任何一个数字都能和旁边的数字对上;对不上时页面自己解释。
- **LG-G6** 守卫按**行为**判,不按代码形状判。

### 4.2 非目标

- **LG-NG1 不上日志收集栈。** `LOG-CONSOLE.md` §九那条依旧成立。
- **LG-NG2 不动归档面。** stdout 的内容、格式、`LLM_LOG_PAYLOADS` 的默认值,一个字节不动。本块所有取舍只作用于**环形诊断窗口**。
- **LG-NG3 不把运行日志入库。** 与 §九一致。`ai_test_runs`(BLOCK-30)是另一件事,理由见 §4.5。
- **LG-NG4 不迁移 217 个调用点的书写形态。** `extra={"extra_fields": {...}}` 这层双重包裹容易写漏(a54 抓到 14 处),但换 API 要动 217 处。本轮**只加一个可选的辅助函数**,不做迁移,见 §5.9。

## 5. 功能规格

### 5.1 LG-401 环形只收有诊断价值的访问日志

新增配置:

```python
#: 访问日志进不进诊断窗口。
#:   "errors"(默认)  只有 >=400 的进环形;2xx/3xx 只进 stdout
#:   "all"           全进(a54 的行为)
OPS_LOG_RING_ACCESS: Literal["errors", "all"] = "errors"
```

`SelfTrafficFilter` 更名为 **`AccessNoiseFilter`**,语义从「挡住 `/api/ops/` 的自指流量」扩成「按 `OPS_LOG_RING_ACCESS` 决定 2xx/3xx 访问日志进不进环形」。三条边界不变:

- **只挡 2xx/3xx。** `/api/ops/logs` 自己 500 了是要看见的。
- **只挡环形,不挡 stdout。** 采集端仍然收到全部访问日志。
- **判定在 handler 的 filter 上**,不在 logger 上。

**界面必须说出来。** `/meta` 返回 `ring.access_mode`,`OpsLogPage` 在 `http` 域被选中时显示一行小字:

> 访问日志只在 4xx/5xx 时进入诊断窗口。全量在 stdout —— 这不是「这段时间没有请求」。

这条小字是这一整块的口径:**窗口可以少装东西,但不许让人把「我没收」读成「没发生」。**

### 5.2 LG-402 原始行不再重新序列化

`read_ring()` 的返回从 `list[dict]` 改为 `list[tuple[str, dict]]`(原始行,解析后的 dict),原始行不再丢弃。

- `q` 子串匹配直接用**原始行**(免费,它已经在手上)
- `_shape()` 里那次「剥掉 `seq` 再 dumps」只对**入选的 `limit` 条**做

成本从 `5000 loads + 5000 dumps` 降到 `5000 loads + limit dumps`。

**守卫换成按行为判(LG-410)**:用一个可计数的假 `json.dumps` 打桩,断言调用次数 `<= limit + 常数`,而不是断言两个函数的行号先后。

### 5.3 LG-403 每条日志带进程身份

`setup_logging(level, *, service: str)`,新增顶层字段:

```
service   "api" | "worker" | "beat" | "script"   谁写的
pid       进程号(十进制整数)                     哪一个
```

调用点:`app/main.py` → `"api"`;`celery_app.py` 的 `worker_process_init` → `"worker"`,beat 钩子 → `"beat"`;`app/scripts/*` → `"script"`。

- `GET /api/ops/logs` 新增 `service` 筛选参数
- `/meta` 返回 `services`(**从当前窗口里实际出现过的值归并**,不是一张写死的表 —— 写死的表会在 worker 没起来时依然列出 `worker`,而那正是要发现的事)
- 行内 chip 显示 `service`,点击即筛
- **`归档面同样带这两个字段`** —— 这是本块唯一一处动 stdout 的地方,是加字段不是改字段,LG-NG2 的例外在此登记

**这一条顺带解决「`gen` 域为什么空着」**:窗口里 `service=worker` 一条都没有,就是 worker 没在写日志,而不是没跑任务。

### 5.4 LG-404 按时间排序,排不了就说

- 后端对**筛选命中的行**按 `(ts, seq)` 排序后再截 `limit`,不再依赖 LPUSH 到达顺序
- `ts` 解析失败的行排在末尾并标 `ts_unparsed: true`,界面上单独一根提示条 —— 不静默丢,也不假装它在某个位置
- 流视角新在前,链路模式旧在前(现有语义不变,但基准从「到达顺序」换成「时间戳」)

### 5.5 LG-405 时间窗

`GET /api/ops/logs` 新增 `since` / `until`(ISO 8601,含时区)。在内存里过滤,与其余筛选同一遍循环。

前端顶栏加一个 Segmented:`最近 15 分钟 / 1 小时 / 6 小时 / 全部`,选中值换算成 `since` 进 URL(**进 URL 的是绝对时间戳,不是「15 分钟」** —— 相对值会让分享出去的链接在对方那里指向另一段时间)。

### 5.6 LG-406 截断显形

响应新增:

```
matched     筛选命中的总条数(截断前)
truncated   bool,matched > len(items)
```

前端在列表顶部渲染:

> 命中 800 条,这里显示最近 200 条。调大右上角的条数,或者收窄筛选。

同时把 `oldest_ts` 那行小字改成两句 —— 一句说窗口边界,一句说当前列表的实际起点。**两个数字都在,谁也不冒充谁。**

顺带:域轨道卡片标题「领域(本屏计数)」→「领域(全窗计数)」,与它自己的 `note` 对齐。

### 5.7 LG-407 CLI 编码与历史

- `_encoding()` 删除;`_follow_file` 固定 `encoding="utf-8", errors="replace"`,与 `setup_logging` 的写入端同一个口径
- 新增 `--tail N`(默认 200):先回放最后 N 条再跟随。`--tail 0` 保持旧行为
- 新增 `--service` 筛选,与 Web 同名同义

**守卫**:一条测试断言 `watch_logs.py` 源码里不出现 `getpreferredencoding`,理由写在断言消息里 —— 免得下一个人为了「兼容本机」把它加回来。

### 5.8 LG-408 `/meta` 补齐自检信息

`/meta` 的 `ring` 段新增:

```
access_mode          "errors" | "all"
services_seen        窗口里出现过的 service 值 + 各自条数
```

`LOG-CONSOLE.md` 第十二章第 1 条留下的自检顺序是「跑一个生成任务,看 `gen` / `batch` 域出不出条目」。有了 `services_seen`,自检第一步变成**打开页面看有没有 `worker`**,不用先跑任务。

### 5.9 LG-409 一个可选的辅助函数(不迁移)

```python
def log(logger, level, message, *, event=None, **fields) -> None:
    """把 `extra={"extra_fields": {...}}` 那层包裹收进来。"""
```

a54 抓到 14 处漏写 `extra_fields` 的调用点 —— 少那一层不报错、不提示,日志只是比作者以为的少了一半,而作者是在出事时才会去读它的。修法当时是加了一条 AST 守卫。

**守卫留着,但守卫挡的是「已经写错了」,辅助函数挡的是「写得出错」。** 本轮只提供它、在文档里推荐它,**不迁移任何现有调用点** —— 217 处的机械改动会把这一轮的 diff 淹掉,而收益是逐步的。

### 5.10 LG-410 ~ LG-412 守卫

- **LG-410** 把 `test_the_stream_is_filtered_before_it_is_shaped` 换成按行为判(见 §5.2)。旧的行号断言删除,并在测试 docstring 里写明为什么删 —— 它绿着的时候问题还在。
- **LG-411** 新增:`AccessNoiseFilter` 在 `errors` 模式下放行 4xx/5xx、拦下 2xx/3xx;`all` 模式下全放行。用真的 `LogRecord` 跑,不看源码。
- **LG-412** 新增:排序守卫。构造三条 `ts` 乱序的记录喂给 `list_logs`,断言输出按时间;再喂一条 `ts` 损坏的,断言它在末尾且带 `ts_unparsed`。

## 6. 事件码登记义务(两个块共用)

`backend/tests/pure/test_a53_log_console.py` 的比对是**双向**的:

```
test_every_event_written_at_a_call_site_is_registered()   写了没登记 → 红
test_every_registered_event_is_actually_written_somewhere() 登记了没人写 → 红
```

后者是刻意的:一个没有任何调用点产出的事件码,会在筛选下拉里摆出一个永远筛不出东西的选项,**而运营会以为是「这段时间没发生」,不是「这个码是假的」。**

因此本 PRD 新增的每一条日志都必须**同时**改两处。两个块合计新增:

| 事件码 | 标签 | 块 | 调用点 | 状态 |
|---|---|---|---|---|
| `ops.log_ring_access_filtered` | 访问日志未进诊断窗口 | 31 | 启动时一条,说明当前 `access_mode` | ✅ a55 |
| `settings.prompt_save_conflict` | 提示词保存版本冲突 | 30 | `prompt_service._assert_expected_version` 的冲突分支 | ✅ a60 |
| `settings.prompt_rolled_back` | 提示词已回滚 | 30 | `activate_version` 带 note 的分支 | ✅ a60 |
| `eval.ai_test_recorded` | AI 测试已留档 | 30 | `ai_test_runs` 写入成功 | ✅ a57 |
| `eval.ai_test_record_failed` | AI 测试留档失败 | 30 | 写入失败,**不吞** | ✅ a57 |

**「不吞」的落地口径(a57 定)**:`ai_test_archive.archive()` **捕获异常但不重新抛出**,
出错走 `eval.ai_test_record_failed`(ERROR 级,带 kind / subject / prompt_version)。
理由是留档发生在模型调用**之后** —— 那次调用可能真花了钱、结果已经在手上,此时把
INSERT 异常抛出去,运营拿到的是 500:**钱花了、结果也没了,而丢掉的本来只是一条台账。**
所以「不吞」在这里的含义是**不静默**,不是**不捕获**:失败的去处是运行日志控制台
(BLOCK-31 刚做出来的那个),不是接口出参。两条守卫钉着 —— `except` 里不许有 `raise`,
且失败分支必须写那个事件码。

`log_events.py` 的 `LOGGER_DOMAIN_FALLBACK` 还有一条硬约束:**新模块不进这张表就红**。BLOCK-30 新增的 `app/prompts/` 必须加一行(建议归 `settings` 域,与 `prompt_service` 一致)。

## 7. 阶段(BLOCK-31)

| 阶段 | 内容 | 任务 | 依赖 |
|---|---|---|---|
| **L0** | LG-401 冲刷、LG-402 开销、LG-406 截断显形 | 3 项 | 无 |
| **L1** | LG-403 进程身份、LG-404 排序、LG-408 自检 | 3 项 | L0 |
| **L2** | LG-405 时间窗、LG-407 CLI | 2 项 | L1 |
| **L3** | LG-409 辅助函数、LG-410~412 守卫 | 4 项 | L0~L2 |

**L0 单独拎出来的理由**:这三条是窗口在丢证据、界面在自相矛盾。它们不依赖任何其他改动,也不该等在时间窗后面。

---

# 第二部分:BLOCK-30 提示词统一管理与 AI 测试留档

> 以下为 v1.0 正文,标 **【v2.0 改】** 的是本稿改动。未标记处与 v1.0 一致。

## 8. 现状盘点(按代码库核实)

提示词共 **8 处**,分布在 6 个模块。其中只有第 1 处能在界面上被看到和修改。

| # | 提示词 | 位置 | 可编辑 | 版本口径 | 原文可回溯 |
|---|---|---|---|---|---|
| 1 | 评分系统提示词(女装) | `evaluators/vision_schema.py:283` | ✅ 页面 | DB 自增 int | ✅ 表内 |
| 2 | 评分系统提示词(男装) | `evaluators/vision_schema.py:365` | ❌ **前端不可达** | DB 自增 int | ✅ 表内 |
| 3 | 评分用户段 | `evaluators/vision_schema.py:459` | ❌ | 无 | 靠 git |
| 4 | 评分深度指令 | `evaluators/vision_schema.py:445` | ❌ | 无 | 靠 git |
| 5 | 属性识别提示词 | `extractors/schema.py:149` | ❌ | 绑 schema 版本 | 靠 git |
| 6 | 文案生成系统提示词 | `listings/copy_generator.py:459` | ❌ | 手写常量 | 靠 git |
| 7 | 修复用正/负提示词补丁 | `evaluators/repair.py:33+` | ❌ | 无 | 拼装后落库 |
| 8 | 出图提示词拼装 | `workflows/generation_plan.py:636` | ❌ | 默认 `"v1"` | 全文落库 |

另有三处**间接注入**的文本源:`evaluators/base.py:41` 的 `DIMENSION_LABELS`、`evaluators/fact_consistency.py:93` 的 `FACT_CHECKS` 标签、`attributes/registry.py` 的枚举值。

### 8.1 四类具体问题

**问题 A:男装提示词是一条死路。** 后端全链路已通(`prompt_rules.py:21` 的 `KNOWN_KEYS` 收了它、`vision_schema.py:266` 的 `prompt_key_for()` 会选它、库能存、`lint_prompt` 能查),但 `frontend/src/pages/PromptsPage.tsx:16` 把 key 写死成 `VISION_SYSTEM_PROMPT`,`frontend/src` 里搜不到男装 key 的任何引用。代码注释自己写着「**这份提示词尚未校准**(阶段 4)」,而校准的唯一入口就是这个页面。

**问题 B:`prompt_version` 有四套口径,其中三套改了措辞版本号不动。**

| 链路 | 取值方式 | 改措辞版本号会不会变 |
|---|---|---|
| 评分 | DB 自增 int | ✅ 会 |
| 识别 | `f"vision-{EXTRACTION_SCHEMA_VERSION}"` | ❌ 不会 |
| 文案 | `PROMPT_VERSION = "llm-1"` 手写 | ❌ 靠人记得改 |
| 出图 | 函数默认参数 `"v1"` | ❌ 不会 |

`copy_generator.py:50` 的注释记录了这个坑已经发生过一次。修在了文案一处,识别与出图两处同款仍在。

数据库层也不一致(逐条核对属实):

```
evaluations.prompt_version        Integer      ← 只有它是 int
attribute_extractions.*           String(32)
generations.prompt_version        String(32)
listing_copies.prompt_version     String(32)
```

无法跨表聚合。

**问题 C:防注入段被逐字复制,而且副本会漂。** 女装系统提示词 50 个非空行中 **37 行与男装逐字相同(74%)**,含整段 `【最高优先级:数据与指令的边界】`。三份可能不同的副本:代码内置女装、代码内置男装、库里 active 的女装版本。运营可以在页面上把这段整体删掉,`lint_prompt` 不检查它。

**问题 D:AI 测试花了钱,结果留不下。**

- **评分测试**:`diagnose_candidate` 会写 `EvaluationAttempt`(`meta.diagnostic = true`),数据在库里,但 AI 测试页不读 `GET /generation-tasks/{task_id}/evaluation-attempts`。**记录在,路径不通。**
- **文案测试**:`workbench/service.py:983` 的 `diagnose_copy` 只调 `record_usage` 记账,生成的 title / bullet_points / description **一个字都没存**。花了钱,账单上有支出,买到的东西没留下。
- 两者都**没有在界面上显示本次用的 `prompt_version`**,而这正是 AI 测试页存在的理由。

### 8.2 为什么现在做

阶段 4 的硬门禁是「男装提示词加入后,女装历史样本重跑、分档结果不变」。这条门禁要求男装提示词能被反复调整并逐次比对效果 —— 而当前既改不了它,也留不下任何一次调整的效果记录。**这是阶段 4 的前置条件,不是锦上添花。**

## 9. 目标与非目标

### 9.1 目标

- **G1** 8 处提示词全部进入统一后台入口,按类型分层管理,共用一套版本与回滚机制。
- **G2** `prompt_version` 统一为内容派生值,消灭「改了措辞版本号不动」。
- **G3** AI 能力测试的输入、输出、成本、所用提示词版本全部留档且可查。
- **G4** 提示词版本与 AI 测试记录双向互链,「改一版 → 测一发 → 对比」形成闭环。
- **G5** 修掉提示词页三条会让使用者做出错误判断的界面缺陷。

### 9.2 非目标

- **NG1 不把提示词正文搬进 `services` 层。**(核对属实)`scoring.py:156` 有函数内 `from app.evaluators.vision_schema import dimensions_for`,而 `app.evaluators.scoring` 在 `.importlinter` 的 `grading-stays-pure` 契约内(禁 `app.models` / `app.db` / `app.services` / `sqlalchemy`)。grimp 建的是可达性图、函数内 import 照样算,所以 `scoring → vision_schema → services` 会让契约变红。**正文留在各自领域模块,统一的是注册表与界面,不是文件位置。**
- **NG2 不把体检(`lint_prompt`)升级成保存闸门。**「全文开放编辑、不做内容拦截」是已确认的产品决定。例外见 §11.3。
- **NG3 不做提示词的 A/B 分流或自动择优。**
- **NG4 不改评分口径、分档阈值或任何业务规则。** 见 AC-34。
- **NG5 不新增提示词。** 只是把已有的 8 处纳管。

## 10. 分层模型

统一管理的前提是承认它们形态不同。把 `build_user_prompt` 做成自由 textarea,会破坏 `vision_schema.py:459` 注释保护的那条不变量:

> 「清单在 `fact_consistency.FACT_CHECKS` 一处,这里只是把它排版出来 —— 在提示词里手抄一份的话,加一项时抄的那份不会跟着变,而模型只会回答提示词里说了的那几项。」

| 层 | 代号 | 成员 | 编辑形态 | 校验重点 |
|---|---|---|---|---|
| 自由文本 | `FREE` | #1 #2 #6 | 全文 textarea | 现有 lint + 防注入段存在性 |
| 槽位模板 | `TEMPLATE` | #3 #4 #5 | textarea,槽位由代码填充 | **必需槽位一个都不能少** |
| 键值映射 | `MAPPING` | #7 及 `DIMENSION_LABELS` / `FACT_CHECKS` 标签 | 表格 | **【v2.0 改】本轮只读,见 §14.2** |

三层**共用同一套版本机制**。`prompt_templates.content` 是 `Text`,`MAPPING` 层把 JSON 序列化进去即可,**不新建表**。

`TEMPLATE` 层的编辑语义:「你能改措辞和排版,但 `{dimension_lines}`、`{check_lines}`、`{codes}`、`{image_lines}`、`{metadata_text}` 这些槽位删掉就报错」。

## 11. 数据模型变更

### 11.1 `prompt_templates`(现有表,不改结构)

复用。新增的 key 只是新的 `key` 取值。`content` 对 `MAPPING` 层存 JSON 字符串。

### 11.2 `prompt_surfaces`(代码内注册表,不入库)

位置:`backend/app/prompts/registry.py`(新模块,只依赖 `app.core`)。**只存元数据,不存正文**,正文通过点分路径在 API 层惰性解析。

```python
@dataclass(frozen=True)
class PromptSurface:
    key: str                    # 与 prompt_templates.key 一致
    label: str                  # 侧栏显示名
    tier: Tier                  # FREE / TEMPLATE / MAPPING
    default_ref: str            # "app.evaluators.vision_schema:DEFAULT_SYSTEM_PROMPT"
    required_slots: tuple[str, ...] = ()   # 仅 TEMPLATE 层
    key_source: str | None = None          # 仅 MAPPING 层,封闭集合的来源
    editable: bool = True
    ui_reachable: bool = True
    consumers: tuple[str, ...] = ()        # 谁读它,写给排障的人看
```

做法照抄 `providers/comfyui.py` 的 `UNWIRED_CONFIG_FIELDS` —— 那里有一张同样性质的表,配一条 `tests/pure/test_a51_comfyui_config_wiring.py` 逐条比对。沿用这个模式,见 AC-23。

**【v2.0 改】** `LOGGER_DOMAIN_FALLBACK` 必须加一行 `("app.prompts", "settings")`,否则 `test_every_module_with_a_logger_is_in_the_fallback_table` 变红(§6)。

### 11.3 `prompt_version` 列统一(**迁移 0055**)

> **【v2.0 改】** v1.0 写的是「迁移 0031」。`0031_receipt_provider_call_marker.py` 已存在,当前最新是 `0054_async_attribute_extractions.py`。

- `evaluations.prompt_version`:`Integer` → `String(32)`,存量值 `str()` 转换
- 全链路统一取值函数:`app.prompts.versioning.content_version(content) -> str`,实现为 `sha256(content)[:12]`
- 保留 `prompt_templates.version`(自增 int)作为**人可读的序号**,与 `prompt_version`(内容哈希)并存:前者回答「这是第几版」,后者回答「这次调用用的到底是哪段文本」

**【v3.0 增】这份迁移原来指着一张不存在的表(a57 修)。** `_TABLE` 原文写的是
`"evaluations"` —— **全仓没有这张表**:评分域是 `rule_sets` / `candidate_evaluations` /
`evaluation_problems` / `review_items`,带 int 版本列的那张叫 `evaluation_attempts`
(迁移 0009 建)。它在任何真实库上都会以 `relation "evaluations" does not exist` 当场失败。

没被发现的原因是 `DECISIONS.md` §3.89 的最后一句:「迁移 0055 也只落文件未在任何库上
执行」。**一份从未被执行过的迁移,它的表名没有任何东西在校验** —— 模型侧不知道迁移
写了什么,门禁只查迁移链 head 唯一,autogenerate 不回头看手写的 alter。a57 补了一条
全量守卫:任何 `op.alter_column` 的目标表,必须在本仓的迁移里被 `create_table` 过。

同时订正原文的第二句。它说「BE-305 之后版本值是 12 位内容哈希,int 列根本装不下」——
**对这一列不成立**:§3.89 接的三条链路是识别 / 文案 / 出图,评分链路的版本至今仍是
`prompt_templates.version` 那个自增序号(`prompt_service.get_active_content` 返回它)。
改型的理由只剩第一条:四张表同型才聚合得了,而且评分链路将来接内容哈希时不必再改一次表。

**改型必须五个面一起改,少一面比不改更糟(a57)。** 迁移改了而 ORM 没改,就是
`models/prompt_template.py` 顶部记着的那个坑 ——「测试库和生产库的结构不一致,比没有
测试更危险:它让人以为测过了」。五个面是:迁移目标表 / ORM 列类型 / 出参 schema /
前端类型声明 / **写入侧的取值函数**。最后一个最容易漏:原来那句
`prompt_version if isinstance(prompt_version, int) else None` 在列还是 Integer 时是
对的,而它同时是一个**静默丢弃器** —— 评分链路一旦接上内容哈希,落库的版本会集体
变空,不报错、不告警。现已归一到 `prompts/versioning.version_text()`(强制转文本,
不是放宽 isinstance:放宽会让 int 和 str 两种形状同时存在于一列里)。

**【v2.0 增】哈希输入必须是稳定序列化。** 拼装内容里任何不稳定成分(dict 迭代顺序、时间戳、浮点格式化)都会让同一份提示词每次哈希不同,于是历史记录变成一堆孤儿版本 —— **那比「版本号不动」更糟,因为它看起来是在正常工作**。见 AC-30b。

### 11.4 `ai_test_runs`(新表,**迁移 0056**)

> **【v2.0 改】** v1.0 写的是「迁移 0032」,已被 `0032_batch_request_idempotency.py` 占用。

```
id                       uuid  pk
kind                     str   -- 'evaluation' | 'copy'
subject_type             str   -- 'generation_candidate' | 'product'
subject_id               uuid
prompt_key               str   nullable
prompt_version           str(32) nullable   -- 内容哈希,与 §11.3 同源
prompt_template_version  int   nullable     -- 人可读序号
generator                str   nullable     -- 'template' | 'llm' | evaluator 名
model_name               str   nullable
payload_in               jsonb              -- 输入摘要(facts / 候选图引用 / 深度)
payload_out              jsonb              -- 完整输出
success                  bool
error_code               str   nullable
error_message            text  nullable
duration_ms              int   nullable
total_tokens             int   nullable
billable                 bool
actor                    str
created_at               timestamptz
```

**评分测试不重复落一份输出** —— 它已经有 `EvaluationAttempt`。`ai_test_runs` 对 `kind='evaluation'` 只记指针(`payload_out = {"attempt_id": ...}`)与索引字段。**文案测试则完整落 `payload_out`**,因为目前它一行都没存。

**【v3.0 改】保留策略:一个不变量 + 一个可调默认值,不是一个待拍板的数。**
决议见 §14.3。落地在 `app/workflows/ai_test_record.purgeable()`:

```
可调的那半    DEFAULT_RETENTION_DAYS = 180,产品说改就改,只影响磁盘占用
不可协商的半  prompt_version 仍是某个 key 的当前生效版本 -> 多老都不删
```

`active_versions` 由调用方从 `prompt_templates` **现查**,不缓存 —— 缓存一份"当时的
生效版本"去做删除决定,是这条不变量最容易被绕开的方式。

**`purgeable()` 本轮(a57)刻意没有调用点。** 清理拍子要和 `GET /ai-tests/runs`
(BE-311)一起才验得动:没有读接口,「删对了没有」只能靠直接查库看。它因此**不进**
`verify_delivery.WIRED_MODULES` —— 登记了会当场红,而红的原因不是缺陷。接线时同批登记。

**【v3.0 增】索引按查询形状建,三条,没有第四条。** `created_at`(历史表倒序)、
`prompt_key + prompt_version`(FE-314 的正向互链)、`kind + created_at`(BE-311 的过滤)。
一张每天进个位数行的表,多一个索引的维护成本高于它省下的那次扫描。

**【v3.0 增】`subject_id` 刻意不加外键。** 被测对象可能在留档之后被清理(候选图会随
任务清理走),而"那次测试跑过"这件事不该跟着消失。悬空的 id 比消失的档案好 ——
前者查不到对象,后者连查都无从查起。

**【v2.0 增】`payload_in` / `payload_out` 必须过脱敏。** 走 `app/llm/redaction.py` 的 `safe_payload_for_log`,与旁挂库同一条规矩 —— `LOG-CONSOLE.md` §八守卫二的理由在这里同样成立:**新开一个去向,最容易漏的就是在新去向上把老规矩忘了。** 配一条 AST 断言。

### 11.5 【v2.0 增】与「运行日志不入库」的口径冲突,显式登记

`docs/LOG-CONSOLE.md` §九写着:

> **不入库。** 审计表管合规。运行日志入库意味着每次 LLM 调用多三次 INSERT,以及一张只增不减、无人清理的表。

`ai_test_runs` 是入库。**两份文档对「观测数据要不要入库」给了相反的口径**,不写一笔,下一个人会拿 §九来否掉这张表。

判据是**触发方式与量级**,不是「它是不是日志」:

| | 运行日志 | AI 测试留档 |
|---|---|---|
| 触发 | 系统自己,每次调用 | 人点按钮,每次都要勾费用确认 |
| 量级 | 每次评分 ≈ 12 条 | 每天个位数 |
| 寿命 | 诊断窗口,分钟到小时 | 要和提示词版本对照,180 天 |
| 清理 | 环形自动滚出 | 复用 migration 0024 的清理任务 |

§九反对的是「把高频自动写入塞进 OLTP 库」。**这两条都不成立时,那条禁令不适用。** 本条并入 `docs/DECISIONS.md` §3.87,并在 `LOG-CONSOLE.md` §九那段后面加一句反向指引。

## 12. 功能规格(提示词)

### 12.1 提示词管理中心(BE-301 ~ BE-304 / FE-301 ~ FE-304)

`/prompts` 从单页改为**列表 + 详情**,复用「系统管理」组现有位置(`App.tsx:161`),不新增侧栏条目。

```
/prompts                     提示词列表(按 tier 分组)
/prompts/:key                单个提示词详情(编辑 + 版本历史 + 测试记录)
/prompts/:key/versions/:n    单版本只读视图
```

- **FE-301** 列表页按 tier 分组,每行显示:名称、tier 标签、当前是内置默认还是第几版、最近修改时间、**近 7 天调用次数与解析失败率**
- **BE-301** `KNOWN_KEYS` 从硬编码二元组改为 `registry.all_keys()`
- **BE-302** 新增 `GET /prompts` 返回注册表全量(含 tier、consumers、统计)
- **BE-303** 新增 `GET /prompts/{key}/versions/{version}` 返回**单版本正文**
- **BE-304** `lint_prompt` 按 tier 分派:`FREE` 走现有规则 + 防注入段检查;`TEMPLATE` 检查必需槽位;`MAPPING` 校验 JSON 结构与键的封闭性

**男装接入(AC-24)**:`registry` 中 `vision_system_prompt_men` 的 `ui_reachable = True`,列表页与详情页与女装完全对等。若本轮决定不上线,**必须显式设 `ui_reachable = False` 并在 `consumers` 里写明原因** —— 不允许「后端支持但前端悄悄看不见」这个中间态继续存在。

### 12.2 `prompt_version` 统一(BE-305 ~ BE-307)

- **BE-305** 新增 `app/prompts/versioning.py`,提供 `content_version(content: str) -> str`
- **BE-306** 识别(`extractors/vision.py:93`)、文案(`listings/copy_generator.py:53`)、出图(`services/generation_service.py:336`)三条链路改为调用它;`EXTRACTION_PROMPT_VERSION` 与 `PROMPT_VERSION` 两个常量删除
- **BE-307** 迁移 0055 改列类型 + 存量转换

**兼容性**:`prompt_version` 从「`llm-1` 这种人写的标签」变成 12 位十六进制。现有落库值不做反向解析,只做类型转换,历史行保持原值。查询侧不依赖格式。

### 12.3 防注入段抽取(BE-308 / FE-305)

> **【a68 补记】FE-305 从 v1.0 起就没被排进 §16 的任何一格。** S4 那格写的是
> 「FE-306/307/309/310 页面整改 + BE-308 防注入 | 5 项」—— 5 项正好是那四个 FE 加 BE-308,
> **FE-305 不在里面**。于是 BE-308 a56 就落了,而它的前端那一半七轮里没有任何人认领,
> 也没有出现在任何一条欠账里。a68 补进 S4(5 项 -> 6 项)并落码。
>
> 值得记的不是漏了一项,是**漏的方式**:§12 按功能分组、§16 按阶段分组,两张表
> 各自都自洽,而「§12 里的每一项都在 §16 里出现过」这件事没有任何东西在看。

- **BE-308** 抽出 `ANTI_INJECTION_BLOCK` 常量,女装与男装两份默认提示词引用它;`lint_prompt` 对 `FREE` 层新增 `no_anti_injection` 警告
- **FE-305** 详情页在编辑框上方以只读折叠块展示这段,标注「这段来自代码,删掉会告警」

内置默认自己**不会**触发这条警告,所以不会造成「一打开页面就是黄色告警」,也就不会训练人忽略整个警告面板 —— 理由与 `prompt_rules.py` 末尾那段「刻意不检查维度清单」的注释一致。

**【v2.0 增】这条只防手滑,不防不懂。** 一个只警告不拦截的检查,防不了「看到黄色提示照样点保存」。NG2 不改(保存闸门是产品决定),但**删除防注入段时的保存动作增加一次二次确认**,确认文案写明这段是干什么的。二次确认不是闸门 —— 它不阻止你保存,只保证你知道自己在删什么。

### 12.4 提示词页整改(FE-306 ~ FE-311)

**FE-306 版本历史要能看内容。** 现状:`describe()` 返回的 versions 里只有 `chars`,没有 `content`;也没有取单版本正文的端点。想看 v3 写了什么,唯一办法是**把它切成生效版本** —— 而这个动作会立刻改掉线上每一张图的评分口径。「想看一眼」和「想让它生效」被绑成了一个动作。
→ 版本表新增「查看」(走 BE-303)与「与当前生效版对比」(行级 diff);`describe()` 保持不返回全部版本正文。

**FE-307 「查看内置默认」要真的是 diff。** `PromptsPage.tsx:43` 的状态叫 `diffOpen`,但 Modal 里只有一个只读 textarea,**没有任何对比**。变量名已经承认这里本来该有对比。
→ 改为左右分栏 diff,保留「填入编辑框」按钮。

**FE-308 清空提示词时必须出警告(最高优先级)。** 现状是一个 fail-open,`PromptsPage.tsx:79`:

```ts
enabled: dirty && checked.length > 0,                          // 内容为空 → 不发请求
const warnings = dirty ? (preview.data?.warnings ?? []) : ...  // undefined ?? [] → []
```

清空文本框之后:preview 不跑 → `preview.data` 是 `undefined` → `?? []` → 空数组 → **界面上什么都不显示**。不是「没检成」,不是「正在体检」,是完全静默。保存按钮照常可点。

而 `lint_prompt("")` 的第一条返回值恰恰是 `PromptWarning("empty", "提示词是空的,模型将只收到用户段指令")`。

**这与 `PromptsPage.tsx:124-137` 那段注释批判的是同一个 bug** —— `preview.isError` 那一支修好了,`enabled: false` 这一支漏了。
→ `enabled` 去掉 `checked.length > 0`,空内容照常送体检;四种状态分开表达:检过没问题 / 检过有问题 / 没检成 / 还没检。

**FE-309 并发编辑要报冲突。** `dirty` 只比较本地 draft 与自己这次拉到的 content。A 保存了 v5,B 的页面还停在 v4 上编辑,B 点保存 → 直接落 v6,A 的改动无声消失。`save_version` 只保证版本号不撞,没有乐观锁。而页面自己的注释写着「三个动作都会改变**在线上生效的评分口径**」。
→ `PromptSaveIn` 新增 `expected_version: int | None`,不匹配返回 409 并附带「服务端现在是第 N 版,由 X 于 T 保存」;前端弹冲突对话框,提供「查看对方改了什么」与「放弃我的改动」。

> **【v2.0 改】乐观锁必须同时覆盖 activate 与 reset。** v1.0 只加在 `PromptSaveIn` 上,但这三个动作是**同一条注释点名的同一类风险**:A 在切 v3、B 同时在切 v5,一样静默覆盖。`PromptActivateIn` 与 reset 端点同样接 `expected_version`。见 AC-35。

> **【a60 落地口径】理由不写进版本行的 `note`。** 原文说「版本表增加「最近一次被切回的
> 时间与理由」」,而第一版把理由拼到被切到那一版的 note 上,踩了两个坑:两个理由撞在
> 一起(那一版的 note 说「当初为什么这么写」,回滚的理由说「后来为什么退回来」),
> 而且 `row.note = ...` 让 `audit_column_writers.py` 按**列名**把
> `ListingImageSet.note` 也算成有写入点(那个审计刻意"多认一次"),它的欠账条目当场
> 失效 —— **一次改动顺手抹掉了另一张表上的一笔账**。现在理由只进审计 payload 与
> `settings.prompt_rolled_back` 事件;版本表上那一列要从审计里读回来,需要一个按
> entity 查审计的读路径,**本轮不做**。

**FE-310 回滚要有理由。** 保存有 note(占位文字写着「回滚时这是唯一的线索」),**回滚没有**。三个月后看到 v3 生效、v5 存在,没人知道为什么退回去了。按这个页面自己的标准,回滚的理由比保存的理由更该留 —— 保存至少还能从内容 diff 猜出来。
→ `PromptActivateIn` 与 reset 端点各加 `note`,进审计 payload;版本表增加「最近一次被切回的时间与理由」。

**FE-311 修掉指向不存在输入框的提示。** `PromptsPage.tsx:213` 写着「在「设置」页顶部填入后端 ADMIN_TOKEN 里的口令并点「记住」」,而 `SettingsPage.tsx:189` 的注释说「卡片随 localStorage 口令链一起删掉了(PRD §28)」。**那个输入框已经不存在。** `api/client.ts:382` 里的措辞才是对的。
→ 文案对齐 `client.ts:382`;同时全仓扫一遍同类残留(`ColdStartBanner.tsx:99`、`LoginPage.tsx:125` 需人工判定)。

**其余三条(并入 FE-306)**:

- 内置默认在版本表里没有位置 → 表格顶部钉一行虚拟的「内置默认 · 生效中」
- 版本表缺最该有的一列:目前最接近「质量」的列是**字数**;`evaluation_attempts.prompt_version` 存着,join 即可得出调用次数、解析失败率、平均耗时 → 增加「调用 N 次 · 失败 M 次」列
- `useDocumentTitle('提示词')` 不带 key 名 → 改为 `提示词 · {label}`

### 12.5 AI 测试留档(BE-309 ~ BE-311 / FE-312 ~ FE-315)

- **BE-309** 迁移 0056 建 `ai_test_runs`
- **BE-310** `diagnose_candidate` 与 `diagnose_copy` 各写一条;**文案侧必须落完整 `payload_out`**
- **BE-311** `GET /ai-tests/runs`,支持按 `kind` / `prompt_key` / `prompt_version` / 时间范围过滤
- **FE-312** AI 测试页下方新增「历史记录」表:时间、类型、对象、**所用提示词 key 与版本**、生成器/模型、成功与否、耗时、token、**是否计费**、操作者
  > **【a68 订正】原文写的是「成本」,而 §11.4 的表给不出。** `ai_test_runs` 只有 `billable: bool`,没有任何金额列 —— 金额在 `provider_usage_records` 里,要按调用关联才拿得到。
  > 这一列因此如实画「是否计费」。**要真的显示金额,是关联那张表的一项新工作,不是这一列的实现细节。**
  > 两份文档对同一件事口径不一致时,改的应该是说不通的那一份,而不是让实现去凑一个编出来的数。
- **FE-313** 结果区的 Tag 行补上 `prompt_version`。当前只渲染 `evaluator` / `model_name` / `duration_ms` / `diagnostic`,**缺的恰好是这个页面存在的理由**
- **FE-314** 双向互链:提示词详情页 →「这一版跑过的测试」;测试记录行 →「用的是哪一版」
- **FE-315** 费用确认勾选(`scoreCostConfirmed` / `copyCostConfirmed`)在每次测试成功或失败后重置。当前勾上之后一直勾着,换任务时调了 `scoreTest.reset()` 但确认状态没跟着重置 —— 而这个勾的全部意义是「每一次都可能真的花钱」

**顺带**:页面上「请保持页面打开,不要刷新」这句提示本身就是问题的自述。结果落库并可查之后,改为「可以离开本页,结果会记入下方历史」。

## 13. 验收标准

| 编号 | 内容 | 验证方式 | 状态 |
|---|---|---|---|
| **AC-23** | 注册表一致性:遍历 `PROMPT_SURFACES`,每项答得出 key、tier、默认值可解析、consumers 非空、`editable` 与 `ui_reachable` 与前端实际路由一致 | 离线单测,照 `test_a51_comfyui_config_wiring.py` | ✅ a56 |
| **AC-24** | 男装提示词可在界面查看/编辑/保存/回滚/恢复默认,与女装对等;或显式标 `ui_reachable=False` 且写明原因 | 前端集成 + AC-23 | 🟡 a58 翻转 `ui_reachable=True`,详情页按 `:key` 走同一段代码;**浏览器未实测** |
| **AC-25** | `FREE` 与 `TEMPLATE` 两层各取一个代表,完成「编辑 → 体检 → 保存 → 查看历史 → 对比 → 回滚」全流程 **【v2.0 改:MAPPING 本轮只读,不在此列】** | 手工验收 | ⬜ 待 S4 |
| **AC-26** | `TEMPLATE` 层删掉任一必需槽位,保存时出现明确警告并指出缺哪个 | 离线单测 | ✅ a56 |
| **AC-27** | 清空提示词内容,界面出现 `empty` 警告(当前完全静默) | 前端单测 | ✅ a56(S0 的 FE-308) |
| **AC-28** | 体检四种状态可区分:没问题 / 有问题 / 没检成 / 还没检 | 前端单测 | ⬜ 待 S4 |
| **AC-29** | 两个会话并发保存,后者收到 409 且能看到对方改了什么;无静默覆盖 | 集成测试 | 🟡 a60 落码(乐观锁 + 409 带对方版本与署名);**真库并发未跑** |
| **AC-30** | 修改任一提示词措辞而不改 schema,落库的 `prompt_version` **必然变化**(四条链路各验一次) | 离线单测 | 🟡 三条链路 a56;评分链路仍是自增序号,见 §11.3 |
| **AC-30b** | **【v2.0 增】** 同一份输入连续拼装 100 次,`prompt_version` **恒等**;dict 顺序、浮点、时间戳都不得进入哈希输入 | 离线单测 | ✅ a56 |
| **AC-31** | 删掉防注入段并保存,出现 `no_anti_injection` 警告 + 二次确认;使用**内置默认**时该警告**不出现** | 离线单测 + 前端单测 | 🟡 离线半 a56;前端半待 S4 |
| **AC-32** | 跑一次文案测试后刷新页面,历史表里能看到完整输出、所用提示词版本与成本 | 手工验收 | 🟡 a62 落码(表+写入+读接口+历史表);**浏览器与真库都未跑**,而这条验收本身就是「刷新页面」——只有真跑才算 |
| **AC-33** | 从提示词某一版能跳到该版的全部测试记录,反向亦可 | 手工验收 | 🟡 **两个方向都已落码**:正向 a63,反向 a70。a63 记的拦路理由是「版本可能已被删」—— a70 去核实后**它不成立**:`prompt_service` 里没有任何删除路径,`reset_to_default` 只把版本停用、行留着(它自己的注释写着「历史仍然留着」)。真正需要判的是另一件事:**文案链路落的是内容哈希,没有对应的版本页**,所以那一半不画链接(判定在 `utils/aiTestRuns.versionPathFor`,5 条行为测试)。**浏览器未跑**,所以仍是 🟡 |
| **AC-34** | **零回归门禁**:女装历史样本全量重跑,分档结果与本轮改动前逐条一致 | 回归脚本 | ⬜ 待历史样本 + 真库(S5) |
| **AC-35** | **【v2.0 增】** activate 与 reset 同样受乐观锁保护:并发切版本,后者 409 | 集成测试 | 🟡 a60 落码,三个动作各一条守卫;**真库并发未跑** |
| **AC-36** | **【v2.0 增】** 本 PRD 新增的每一条事件码,`EVENTS` 与调用点**双向**都在;`app.prompts` 已进 `LOGGER_DOMAIN_FALLBACK` | 复用 `test_a53_log_console.py` | ✅ a60(五条全部登记且都有调用点)。**a66 补:§6 那张表与 `EVENTS` 现在由 `test_a66_prd_status_truth.py` 双向钉着** —— 这两条在 a60 就落了码,而表里到 a66 仍写着「待 S3 前端半」,是我自己造的第二次状态分叉 |

AC-34 是本轮硬门禁。它与阶段 4 男装提示词的门禁是同一条,提前在这里执行一次,确保「统一管理」这件事本身没有改动任何评分口径。

**【v3.0 补】AC-34 卡的是结算,不是落码。** 它要历史样本 + 真库 + 视觉评分器,三样本地
一样都没有。a56 因此在 BE-308 上做了一个刻意的取舍:防注入段从女装默认正文里**按边界
切片派生**,再断言男装逐字包含 —— 两份默认提示词**一个字节没动**,于是"改写正文拼接
方式"这个风险根本没被引入,副本漂移由 import 时的 raise 当场暴露。**没有能跑 AC-34 的
环境时,正确的做法是让改动不触及它守的东西,而不是先改再说、把验证记成欠账。**

## 14. 待决问题的决议

> **【v2.0 改】** v1.0 的四条待决问题,前两条在此就地决议,后两条保留。

### 14.1 男装提示词本轮是否上线 —— **【v3.0 决议:走显式路径,a56 已落地】**

原文:上线走 AC-24 完整路径;不上线则在注册表里显式 `ui_reachable=False` 并写明解锁条件。**AC-23 会要求这两者同时成立** —— 「后端通、前端不可达、且无任何记录说明这是有意的」这个状态本轮必须消失。

> **【v3.0 后续】a58 已按第一条上线:`ui_reachable=True`,详情页从写死女装改成按路由 `:key`。下面这段保留为它当时的记录。**

**a56 选了第二条,并且已经落地。** `prompts/registry.py` 里 `vision_system_prompt_men` 标
`ui_reachable=False`,`consumers` 里写着原因与解锁条件:提示词页仍是单 key 页面
(`PromptsPage.tsx` 写死女装),**解锁条件 = FE-301 落地**。AC-23 的守卫强制这两样同时存在。

所以这一条不再是待决问题,而是一笔**有还款日的欠账**:S3 前端半交付时,这行 `False` 必须
跟着翻转,否则 AC-24 拿不到。这份文档到 v3.0 之前还写着"保留待决",而代码里早就决了 ——
那正是 §3.70 点名的那类分叉。

### 14.2 `MAPPING` 层是否纳入本轮 —— **决议:只读,不可编辑**

v1.0 在这里自相矛盾:待决问题建议「只做只读展示」,而 §3 的三层模型、BE-304 的分派、AC-25 的「三层各取一个代表」都已经把它当成可编辑的了。

**本稿决议按只读**:`repair.py` 的 `prompt_additions` 是 `HardFailCode → 一句话` 的映射,改它等于改**修复策略**,风险高于改措辞。落地形态:

- `MAPPING` 保留在 `Tier` 枚举里(注册表要能描述它)
- `PromptSurface.editable = False`,列表页与详情页只读展示
- BE-304 的 `MAPPING` 分支**本轮只实现「结构可解析」校验**,键封闭性校验推迟
- AC-25 相应改为两层

### 14.3 `ai_test_runs` 的 180 天保留期 —— **【v3.0 决议:拆成不变量与可调值】**

原文说需要产品确认「回看多久以前的测试」。**那个问题问错了对象。**

一条留档的寿命下界,由**它引用的东西还活不活着**决定,不由日历决定。今天生效的那一版
提示词,如果它唯一的测试证据因为"超过 180 天"被清掉,那么"这一版跑出过什么"就此无解 ——
而这恰恰是"要回看多久"这个问题真正在担心的事。把它交给一个日期,等于把一件确定的事
交给一个猜出来的数。

拆成两半:

| | 内容 | 谁说了算 | 改了会怎样 |
|---|---|---|---|
| 可调的那半 | `DEFAULT_RETENTION_DAYS = 180` | 产品 | 只影响磁盘占用 |
| 不可协商的半 | 引用着当前生效版本的记录,多老都不删 | 代码里的不变量 | 改不了,守卫钉着 |

落地在 `purgeable()`,两道闸顺序不能换:先判年龄,再判引用。守卫里有一条专门验
「保留期调到 0,引用还活着的记录仍然不删」——**可调的那半不许把不可协商的那半带走**。

于是产品那个数从「决定这张表对不对」降级成「决定这张表多大」,**可以先带默认值上线**,
不阻塞 S2。真要收敛,等 BE-311 的读接口上线、有人真的用过历史表之后再问,那时候的
回答才有依据。

### 14.4 提示词页是否需要独立权限位 —— **保留待决,本轮不做**

当前与设置页共用 `require_admin`。若未来出现「能改提示词但不能改 Provider 密钥」的角色,需要拆。

**【v3.0 补】维持待决,但当前口径要写下来,免得下一个人以为它是漏的。** AI 测试留档
(a57)沿用同一条:两个诊断端点的权限不变,留档只多记一个 `actor`,取值走
`deps.current_actor(request)` —— 与审计表同源。拆权限位那天,这两处跟着一起拆。

## 15. 风险与对策

| 风险 | 说明 | 对策 |
|---|---|---|
| 把不该开放的开放了 | `TEMPLATE` 一旦被当自由文本编辑,`FACT_CHECKS`「清单在一处」的不变量就没了 | 槽位校验(AC-26)+ 槽位以只读 chip 呈现 |
| `prompt_version` 类型迁移 | `evaluations.prompt_version` 从 int 改 str,可能有下游按整数比较 | 迁移前全仓 grep 该列读取点;历史值只 `str()` 不重算 |
| **哈希输入不稳定** **【v2.0 增】** | 每次拼装的 content 有细微差异 → 每次调用一个新版本号,历史变成孤儿堆 | AC-30b;`content_version` 只接受**已定型的最终字符串**,不接受 dict |
| `ai_test_runs` 无界增长 | 测试记录带完整输出,文案可能上千字 | 180 天保留,复用 migration 0024 的清理任务 |
| 统一管理界面成为新的评分口径事故源 | 8 处都能改之后,误改面变大 | 保存/回滚/恢复默认全部进审计(已有);版本表加调用统计;AC-34 每轮硬门禁 |
| 改动本身引入回归 | 本轮触及评分、识别、文案、出图四条链路 | AC-34 零回归门禁;各阶段独立可回滚 |
| **迁移编号再撞** **【v2.0 增】** | v1.0 就撞了两个 | 落码前 `ls migrations/versions \| tail -1` 复核一次,不照抄 PRD 里的数字 |

## 16. 阶段与依赖(全局)

| 阶段 | 块 | 内容 | 任务 | 依赖 | 状态 |
|---|---|---|---|---|---|
| **L0** | 31 | LG-401/402/406 冲刷、开销、截断显形 | 3 | 无 | ✅ a55 |
| **L1** | 31 | LG-403/404/408 进程身份、排序、自检 | 3 | L0 | ✅ a55 |
| **L2** | 31 | LG-405/407 时间窗、CLI | 2 | L1 | ✅ a55 / a56 |
| **L3** | 31 | LG-409~412 辅助函数与守卫 | 4 | L0~L2 | ✅ a55 / a56 |
| **S0** | 30 | FE-308 清空静默、FE-311 死指路 | 2 | 无 | ✅ a56 |
| **S1** | 30 | BE-305~307 版本统一 + 迁移 0055 | 3 | 无 | ✅ a56;**迁移 0055 由 a57 修正目标表**,仍未在任何库上执行 |
| **S2** | 30 | BE-309~311 + FE-312~315 测试留档 + 迁移 0056 | 7 | S1、**L1** | 🟡 BE-309/310 ✅ a57;BE-311 与 FE-312/313/315 ✅ a62(清理拍子同批接线);FE-314 **正向** ✅ a63(口径见 `DECISIONS.md` §3.96:评分落序号列、文案落哈希列,两列语义此前是串的);**反向** ✅ a70 —— a63 记的顾虑是「版本可能已被删」,a70 去代码里核实了:`prompt_service` 没有任何删除路径,`reset_to_default` 只停用不删行,所以评分链路的链接必然指得到东西;**文案链路仍然不画链接**(落的是内容哈希,没有对应版本页,拼出来必然 404)。**浏览器未实测** |
| **S3** | 30 | BE-301~304 + FE-301~304 管理中心 | 8 | S1 | 🟡 BE-301/303/304 ✅ a56;**BE-302 统计段** ✅ a70(+ 迁移 0059);FE-302~304 ✅ a58,**FE-301 近 7 天列** ✅ a70。八项全部落码,**但 AC-24 仍是 🟡**(见 §13,浏览器未实测)—— 这一格的状态不许比它依赖的验收项更乐观,所以仍是 🟡 而不是 ✅。〔a70 订正:a68 说这两项「数据已齐、只差排期」,**那句话的后半对、前半错** —— 数据当时并不齐:a68 指的源 `ai_test_runs` 只记诊断测试,拿它当调用统计会给出一个看起来像生产流量的假数;真正的源 `evaluation_attempts` 在 a70 之前**没有 `prompt_key` 列**,而版本号按 key 各自自增,女装 v3 与男装 v3 在库里一个字节都不差。补归属(0059)之后才真的齐〕 |
| **S4** | 30 | FE-305~307/309/310 页面整改 + BE-308 防注入 | 6 | S3 | 🟡 BE-308 ✅ a56;FE-305 ✅ a68(**它此前不在这一格的清单里**,见 §12.3 补记);FE-307/309/310 ✅ a60;**FE-306 差一项** —— 「查看」✅a58 / 「与当前生效版对比」✅a68 / 「内置默认钉一行」✅a60 / `useDocumentTitle` 带 label ✅a58,**「调用 N 次 · 失败 M 次」列** ✅ a70(与 FE-301 那一列同源)—— **六项至此全部落码**,但 AC-25 / AC-28 是手工与浏览器验收,仍未跑,所以这一格保持 🟡。〔a68 订正:上一版把「最近一次被切回的时间与理由」记在 FE-306 名下,那是 FE-310 的落地口径〕 |
| **S5** | 30 | AC-34 全量回归 | — | S1~S4 | ⬜ 未开工 |

> **【a59 订正】上一版这张表里 S3 写着 ✅「AC-24 达成」,而 §13 的 AC-24 那一行
> 写着 🟡「浏览器未实测」。两句话在同一份文档里、同一次改动中被写成互相矛盾 ——
> 正是 §3.70 点名的那类分叉,而它是这份 PRD 加状态列本来要防的东西。
> **阶段格的状态不许比它依赖的验收项更乐观**,这条现在写在这里。**

**【a58 更新】S3 前端半已交付,下一步是 S4。** 下面这段是 a57 写的排期依据,
三条理由里前两条已经兑现;保留原文,因为第三条对 S2 剩余部分仍然成立。

**S4 的四项(FE-306/307/309/310)有一个共同前提:它们都要改的是详情页,而详情页
现在才第一次有多个消费者。** FE-309 的并发冲突尤其 —— 它要动 `PromptSaveIn` /
`PromptActivateIn` 两个入参与三个端点的乐观锁,而"两个人同时改同一份提示词"这件事,
在提示词只有一个入口时几乎不会发生,有了列表页之后才成为常态。

**【v3.0 增】下一步是 S3 的前端半,不是 S2 的剩余部分。** 三条理由:

1. **它解锁的是一笔有还款日的欠账。** `ui_reachable=False` 那行注释写着「解锁条件 =
   FE-301 落地」,而 AC-24 至今拿不到。S2 剩下的 BE-311 + FE-312~315 是新功能,欠账优先。
2. **S4 整个压在它后面。** 依赖表里 S4 依赖 S3 全,而 S3 后端半 a56 就做完了 ——
   前端半是这条链上唯一的堵点。
3. **BE-311 与 FE-312 是同一件事的两半,分两轮做等于把读接口悬空一轮。** 没有页面消费的
   `GET /ai-tests/runs`,和 §14.1 那条「后端通、前端不可达」是同一个形状。

**共同的前提是一个能跑 `make fe-check` 的环境。** a54 交接里那句「下一步第一件事就是
`make fe-check`」到 a57 仍然没有兑现过 —— 前端已经连着四轮只过源码级守卫。这不是排期
问题,是环境问题,写在这里免得它再被读成"还没排到"。

> **【a70 兑现】那个环境在 a70 这轮有了,`make check` 整条跑绿了一次。**
> `npm ci`(387 包)与 `pip install -e ".[dev]"` 在本轮的容器里都装得上,于是
> **连着七轮没跑过的四条前端门禁**(typecheck / lint / Vitest / build)以及
> `test-nodb`、`lint-imports` 全部真的执行了:
>
>     后端纯测试     2951 passed        前端 Vitest    183 passed(22 文件)
>     非真库 pytest  3269 passed        前端行为测试   48 passed(24 → 48)
>     架构契约       3 kept, 0 broken   syntax-check   137/137
>
> 两条本轮**由门禁真的抓出来**的缺陷,都不是源码守卫看得见的:ESLint 拦下
> `<Tag color="blue">`(antd 预设调色板不受 `theme.ts` 控制,暗色模式下会漂色),
> `verify_delivery` 拦下六个新文件没进 Git。**前者正是"只过源码级守卫"这四轮
> 里没有任何东西在看的那一类。**
>
> **仍然没跑到的三条没有变**:真库那一半(`requires_db` + Alembic 升降级)、
> Playwright、`docker build`。所以本轮所有带「浏览器未实测」「真库未跑」的
> 验收项**一条都没有被翻成 ✅** —— 门禁面变宽了,但它没有覆盖到那三条。

**L0 / S0 单独拎出来的理由**:五条都是界面在说假话,或窗口在丢证据。它们不依赖任何其他改动,不该等在一个大改造后面。

**S1 必须排在 S2 前面**:测试留档要记 `prompt_version`,如果记的是当前那套「改了措辞不动」的值,留下来的档案会把不同的提示词标成同一版 —— **那比不留更糟,因为它看起来是可信的。**

**【v2.0 增】S2 依赖 L1**:AI 测试留档要写日志、要能在运行日志里追一次测试的完整链路。L1 之前 `service` 字段不存在、排序不可信,追出来的链路可能是错序的 —— 同上一条,错的证据比没有证据更糟。

## 17. 参考位置索引

| 主题 | 文件:行 |
|---|---|
| 女装默认系统提示词 | `backend/app/evaluators/vision_schema.py:283` |
| 男装默认系统提示词 | `backend/app/evaluators/vision_schema.py:365` |
| 评分用户段构造 | `backend/app/evaluators/vision_schema.py:459` |
| 评分深度指令 | `backend/app/evaluators/vision_schema.py:445` |
| 识别提示词构造 | `backend/app/extractors/schema.py:149` |
| 文案系统提示词 | `backend/app/listings/copy_generator.py:459` |
| 修复提示词补丁表 | `backend/app/evaluators/repair.py:33` |
| 出图提示词拼装 | `backend/app/workflows/generation_plan.py:636` |
| 提示词读写服务 | `backend/app/services/prompt_service.py` |
| 提示词体检规则 | `backend/app/services/prompt_rules.py`(`KNOWN_KEYS` 在 :21) |
| 提示词 API | `backend/app/api/prompts.py` |
| 提示词页面 | `frontend/src/pages/PromptsPage.tsx` |
| 文案诊断实现 | `backend/app/workbench/service.py:983` |
| 依赖方向契约 | `backend/.importlinter`(`grading-stays-pure` 在 :50) |
| 注册表模式先例 | `backend/app/providers/comfyui.py`(`UNWIRED_CONFIG_FIELDS`) |
| **日志格式化** | `backend/app/core/logging.py` |
| **事件注册表** | `backend/app/core/log_events.py` |
| **环形缓冲** | `backend/app/core/log_ring.py` |
| **日志读接口** | `backend/app/api/ops_logs.py` |
| **运行日志页** | `frontend/src/pages/OpsLogPage.tsx` |
| **CLI 查看器** | `backend/tools/watch_logs.py` |
| **日志守卫** | `backend/tests/pure/test_a53_log_console.py` |
