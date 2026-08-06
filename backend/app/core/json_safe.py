"""审计/JSONB 载荷的 JSON 安全化。**纯标准库,零依赖。**

## 为什么必须有这一层

`AuditLog.payload` 是 JSONB 列,而 SQLAlchemy 对 JSON/JSONB 的默认序列化器
就是 `json.dumps` —— 引擎(`db/session.py`)没有配自定义 `json_serializer`。
于是任何一个 `datetime` 混进审计 payload,提交时就是:

    TypeError: Object of type datetime is not JSON serializable

A45 batch10 恰好造出了第一条这样的路径:`model_template_service.update_template`
先把 `license_expires_at` 归一成 `datetime`(为了 DateTime 列),又把**同一个值**
原样放进审计 payload(`_AUDITED_FIELDS` 点名要留痕的正是它)。结果是修改授权
到期日的 PATCH 在 commit 时整体回滚 —— 授权日期编辑入口不可用。

修在**审计层**而不是那一个调用点:`_AUDITED_FIELDS` 以后每加一个日期字段,
或任何服务顺手把 UUID / Decimal / 枚举塞进 payload,都是同一次崩。
单点收口,调用方不必各自记得转换。

## 转换规则

    datetime / date / time  -> ISO 8601 字符串(审计要人读,ISO 最不歧义)
    UUID                    -> str
    Decimal                 -> str(不转 float:审计里的金额不许丢精度)
    Enum                    -> 其 value 再递归
    set / frozenset         -> **排序后的** list(审计两次写同一事实必须逐字节相同)
    bytes                   -> UTF-8 尽力解码,失败给十六进制摘要
    dict / list / tuple     -> 递归
    其余未知对象            -> str(value)。宁可失真也不崩:一条略糙的审计
                               远好过整笔业务写入被回滚
"""
from __future__ import annotations

import datetime as _dt
import decimal
import enum
import uuid
from typing import Any


def json_safe(value: Any) -> Any:
    """把任意结构转换成 `json.dumps` 一定能接受的等价物。"""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (_dt.datetime, _dt.date, _dt.time)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, enum.Enum):
        return json_safe(value.value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_safe(v) for v in value]
    if isinstance(value, (set, frozenset)):
        return sorted(json_safe(v) for v in value)
    if isinstance(value, bytes):
        try:
            return value.decode("utf-8")
        except UnicodeDecodeError:
            return value.hex()
    return str(value)
