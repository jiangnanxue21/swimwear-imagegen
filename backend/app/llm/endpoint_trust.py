"""端点信任判定(A45-batch14 从 evaluators/vision.py 抽出)。

回答两个配置期的问题,和"发请求"无关但每个多模态调用方都要答:

    endpoint_requires_api_key()     这个端点该不该要求配 API Key
    provider_label_from_base_url()  这个端点的开销记在哪个厂商名下

## 为什么要抽出来

识别层(阶段 3)接真实后端时需要的判定和评分层**一字不差**:同样的
"公网一律要 Key、私网字面量 IP 放行、猜错方向必须是多要不是少要",
同样的"厂商名从 base_url 推,推不出来不猜"。各写一份的话,A45-#36
那个缺陷(对主机名做前缀匹配,把 `10.gpu.example.com` 判成私网,
于是缺 Key 也 `configured=True`,每次调用 401)会在第二份里原样重生。

依赖方向:本模块**不得** import `app/evaluators`、`app/extractors`、
`app/channels` —— 和 `transport.py` / `images.py` 同一条规矩。
"""
from __future__ import annotations

from urllib.parse import urlparse

#: 自建端点的主机特征。命中这些时允许不配 API Key ——
#: 但公网地址一律要求 Key,不能默认"大家都不需要鉴权"。
LOCAL_HOSTNAMES = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def is_private_host(host: str) -> bool:
    """这个主机是不是私网地址。**只对字面量 IP 成立**(A45-#36)。

    `ipaddress.ip_address()` 解析不了的(也就是域名)一律返回 False ——
    域名指向哪里要等到 DNS 解析那一刻才知道,而"要不要 Key"是配置期的判断。
    配置期判不出来时按"要 Key"处理,与调用方自述的失败方向一致。

    真正防 SSRF 的是 `core/net_safety` 那三层(早失败 / 逐跳校验 / DNS 钉住),
    这里只回答"该不该要 Key",两者不是一回事,不要合并。
    """
    import ipaddress

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    return address.is_private or address.is_loopback or address.is_link_local


def endpoint_requires_api_key(base_url: str, *, allow_anonymous: bool = False) -> bool:
    """这个端点需不需要 Key。

    ``allow_anonymous=True`` 是显式声明"这个端点不鉴权",优先级最高。
    没显式声明时按主机猜 —— 但猜错的方向是**多要一个 Key**,不是少要:
    回环、私网字面量 IP、没有点的容器服务名(``http://ollama:11434``)
    算自建,允许无鉴权;其余(含一切域名)一律要求 Key。

    反过来做 —— 默认所有端点都不需要 Key —— 的后果是:忘了配 Key 时
    ``is_configured()`` 返回 True,后端被选中,然后每一次调用都 401,
    而界面上显示的是"已配置"。宁可在配置期就说缺 Key。
    """
    if allow_anonymous:
        return False
    host = (urlparse(base_url).hostname or "").lower()
    if not host:
        return True
    if host in LOCAL_HOSTNAMES:
        return False
    if "." not in host and ":" not in host:
        # docker-compose 服务名这类内部主机名
        return False
    return not is_private_host(host)


def provider_label_from_base_url(base_url: str, *, fallback: str) -> str:
    """这个端点的开销该记在谁名下。**从配置的 base_url 推,不写死。**

    推导规则很窄:取 host 去掉 `api.` 前缀和顶级域,`api.openai.com` -> `openai`。
    推不出来时退回调用方给的名字,**不猜** —— 一个猜出来的厂商名会让人
    拿着它去对不存在的账单。

    至少要有「名字 + 顶级域」两段才认;单段(比如内网主机名)说明不了厂商。
    """
    host = (urlparse(str(base_url or "")).hostname or "").lower()
    parts = [p for p in host.split(".") if p and p != "api"]
    return parts[0][:32] if len(parts) >= 2 else fallback
