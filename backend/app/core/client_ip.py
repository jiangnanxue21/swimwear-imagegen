"""这次请求从哪儿来(a51)。零依赖,给限流当键用。

## 为什么需要它:真实来源 IP 此前到不了应用层

`frontend/nginx.conf` 一直在设 `X-Real-IP` 与 `X-Forwarded-For`,但:

    uvicorn   启动命令里没有 `--proxy-headers`
    应用      全仓没有任何一处读这两个头

所以生产环境里每一个请求的 `request.client.host` 都是**nginx 容器的地址**,
所有人长得一模一样。在没有任何东西用到来源的年代这不要紧;a51 要按来源
限流,它就成了先决条件 —— 键要是对所有人都相同,限流就从防护变成了
"任何人都能把全站登录关掉"。

## 为什么在应用里解析,而不是给 uvicorn 加 `--proxy-headers`

加那个参数会改写 `request.client` 本身,影响面是**所有**读到它的地方
(访问日志、以后可能出现的审计字段),而它的信任边界由
`--forwarded-allow-ips` 一个字符串决定,写错不会报错。

这里只解析出一个字符串给限流用,`request.client` 保持原样。信任与否是
一个显式的配置项(`TRUST_PROXY_HEADERS`),而且**默认关**。

## 取头的顺序,以及为什么 XFF 取最后一段

    X-Real-IP          nginx 设成 `$remote_addr` —— 它看见的对端,客户端伪造不了
    X-Forwarded-For    nginx 设成 `$proxy_add_x_forwarded_for`,展开是
                       `<客户端自己带来的 XFF>, <nginx 看见的对端>`

所以 XFF 里**最后一段**才是可信的那一段:前面的部分是客户端自己写的,
它想写什么写什么。取第一段(很多示例代码这么写)等于让来源完全由攻击者指定,
限流当场作废 —— 每发一次换一个值就永远不会命中同一个键。

只有一层反代时,`X-Real-IP` 与 XFF 最后一段是同一个值。多层时以最外层
那台设的 `X-Real-IP` 为准,所以它排在前面。

## 信任这两个头的前提

`TRUST_PROXY_HEADERS` 打开,意味着"能连到这个进程的东西都是我们的反代"。
本仓的 compose 把后端端口绑在 `127.0.0.1:8000`,外面只有 nginx 进得来,
前提成立。**把后端直接暴露到公网时必须把它关掉** —— 否则任何人加一个
`X-Real-IP: <随机值>` 就能绕过限流。

关掉之后来源退化成反代的地址(所有人相同),限流因此按"用户名 + 同一个来源"
计,阈值会被所有人共享。那是一个更差但仍然安全的状态:它可能误伤,不会漏放。
"""
from __future__ import annotations

from collections.abc import Mapping

#: 拿不到任何东西时的占位。**不是空串** —— 空串会和"某个头存在但值为空"
#: 混在一起,而后者是可以被构造出来的:那样所有伪造请求会共用同一个键,
#: 看起来像限流生效了,实际上是所有真实来源也被并进了同一个桶。
UNKNOWN = "unknown"


def _last_forwarded_hop(raw: str) -> str:
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return parts[-1] if parts else ""


def client_key(
    headers: Mapping[str, str],
    peer: str | None,
    *,
    trust_proxy: bool,
) -> str:
    """这次请求的来源标识。

    `headers` 用小写键查找(FastAPI 的 `request.headers` 本身大小写不敏感,
    这里为了能在纯测试里传普通 dict,统一按小写取)。
    """
    if trust_proxy:
        real_ip = (headers.get("x-real-ip") or "").strip()
        if real_ip:
            return real_ip
        forwarded = _last_forwarded_hop(headers.get("x-forwarded-for") or "")
        if forwarded:
            return forwarded
    return (peer or "").strip() or UNKNOWN
