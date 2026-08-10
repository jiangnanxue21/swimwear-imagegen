"""Celery Redis broker 的 RESP2 兼容 transport。

redis-py 8.1 默认在连接时发送 ``HELLO 3``。当前部署使用的 Redis 前置代理只兼容
RESP2，会直接关闭该连接。result backend 可以在 URL 上加 ``protocol=2``，但 Kombu
的 Redis transport 没有暴露这个选项，URL query 会被传给不接收它的 Kombu
Connection。因此只在底层 redis-py Connection 子类中补默认值，其他 Kombu 行为
保持原样。
"""
from __future__ import annotations

from kombu.transport.redis import Channel as RedisChannel
from kombu.transport.redis import Transport as RedisTransport
from redis import Connection, SSLConnection


class RESP2Connection(Connection):
    """为普通 Redis broker 连接固定 RESP2。"""

    def __init__(self, *args, health_check_interval: int = 0, **kwargs):
        # health_check_interval 必须留在显式签名中：Kombu 会用签名探测底层连接是否
        # 支持健康检查；只写 **kwargs 会被误判为不支持并把参数删掉。
        kwargs["protocol"] = 2
        super().__init__(
            *args,
            health_check_interval=health_check_interval,
            **kwargs,
        )


class RESP2SSLConnection(SSLConnection):
    """为 TLS Redis broker 连接固定 RESP2。"""

    def __init__(self, *args, health_check_interval: int = 0, **kwargs):
        kwargs["protocol"] = 2
        super().__init__(
            *args,
            health_check_interval=health_check_interval,
            **kwargs,
        )


class RESP2RedisChannel(RedisChannel):
    connection_class = RESP2Connection
    connection_class_ssl = RESP2SSLConnection


class RESP2RedisTransport(RedisTransport):
    Channel = RESP2RedisChannel
