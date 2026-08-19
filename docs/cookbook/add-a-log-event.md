# 新增一条日志事件

## 两步,缺一步都会红

**一、在 `backend/app/core/log_events.py` 的 `EVENTS` 里登记。**
事件码形如 `<域>.<动作>`,域前缀必须已经在 `DOMAINS` 里(域是**业务领域**,
不是模块路径 —— 排障的人问的是「上架怎么了」,不是「poll_service 怎么了」)。

登记时要给中文标签,以及它是不是**例行**事件(例行的会被折叠,而不是丢掉)。

**二、在调用点写上它:**

```python
logger.info(
    "publish.poll_status_changed",
    extra_fields={"event": "publish.poll_status_changed", "listing_id": str(lid), ...},
)
```

守卫 `tests/pure/test_a53_log_console.py` **双向**比对:写了没登记会红,
**登记了没人写也会红**。后者是刻意的 —— 一个没有任何调用点产出的事件码,
会在筛选下拉里摆出一个永远筛不出东西的选项,而运营会以为是「这段时间没发生」,
不是「这个码是假的」。

## 结构化字段怎么挑

挑**能把这条日志和别的东西串起来**的键:`request_id`(全局 contextvar,自动带)、
`task_id`、`llm_call_id`、`product_id`、`round`、`attempt`、`provider`、`http_status`。

**不要把整段正文塞进字段。** 模型请求与响应的原文有专门的去处(旁挂库),
事件流要短、快、能全量扫。

## 脱敏是自动的,但有边界

键名命中 `api_key/secret/password/token/authorization/credential` 的值自动记为 `***`。
但**键名脱敏管不到自由文本** —— `message` 和异常堆栈这两个键名完全无辜、值却是字符串,
而堆栈里最常见的一行正是带 `Authorization` 头或带签名查询串的请求信息。
那一层由 `scrub_text` 负责,已经挂上了;你要做的是**不要把密钥拼进 message**。

## 别做的

- 别用英文句子当分类依据。上一版的查看器硬编码 9 条消息原文当过滤集,
  措辞一改就安静漏事件。
- 别在写日志的路径上加会抛异常的东西。**日志绝不反噬业务**:环形缓冲写不进去就丢。
- 别为了「让日志好看」去掉 `raw`。分类法是索引不是转述,原文必须零成本可得。

## 验证

```bash
cd backend && python3 tools/run_pure_tests.py test_a53_log_console.py
python3 tools/watch_logs.py --domain publish        # CLI 里筛一下
```

页面上打开 `/ops-logs`,确认新事件出现在下拉里、筛得出东西。
