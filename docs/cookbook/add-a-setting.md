# 新增一个后台配置项

## 判断:它该不该进设置页

**不该进的**:数据库地址、Redis、存储后端、S3 密钥、CORS、`ADMIN_TOKEN`、
`SETTINGS_SECRET_KEY` 自身。这些改错就是整站不可用,或者等于把锁和钥匙放在同一个
抽屉里 —— 有一条断言钉死,加进去会红。

**该进的**:改了之后希望**不重启就生效**,且改错了最多影响下一次任务的那些值。

## 三个地方各加一份

```
.env.example              加一行,带注释说明它是什么、默认值多少
app/core/config.py        Settings 加字段
app/core/settings_schema.py  SETTING_GROUPS 里加 Field
```

前两处的一一对应由 `tests/pure/test_config_contract.py` 静态校验,漏一个会红。

## Field 怎么填

```python
Field(
    key="FASHN_TIMEOUT_SECONDS",     # 与 Settings 字段同名
    label="提交超时",                 # 中文,运营看得懂
    type=TYPE_INTEGER,               # TEXT / SELECT / INTEGER / NUMBER / BOOL
    help="超过这个秒数就当作提交失败,进重试。",
    secret=False,                    # True 时:加密落库、回传打码、日志脱敏
    advanced=True,                   # 超时、轮询这类不该干扰第一次配置
    minimum=1, maximum=600,
)
```

**前端不认识任何一个具体配置项**:它拿到分组、类型、选项就把页面画出来了。
所以加一项**只改这一处**,不要去前端加输入框。

## 读的时候走唯一入口

```python
from app.providers._config import provider_setting, provider_flag
value = provider_setting("FASHN_TIMEOUT_SECONDS", "30")
```

**不要直接读 `settings.XXX`** —— 那样后台改了要重启才生效,而那正是这一层要消灭的东西。
(历史上有两处绕过了这个入口:`review_service` 的轮次上限、`generation_service` 的
默认 Provider,后来都接了回来。)

## 密钥类的额外一条

`secret=True` 的项:拿不到加密能力时**拒绝保存**,而不是退化成明文;
页面上永远只显示末位打码串。这一条在服务层已经实现,你只要把标记填对。

## 验证

```bash
cd backend && python3 tools/run_pure_tests.py settings   # 契约、打码、覆盖层
```

改完在页面上存一次,然后确认 worker 也跟上了 —— 最迟 `SETTINGS_CACHE_TTL_SECONDS`
(默认 10 秒)。已经排队的任务不受影响,它用的是入队时那一份。
