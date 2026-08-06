"""外部下载的 URL 安全校验(需求第十九章)。

背景:Provider 返回一个图片地址,我们照着去下载。这条路径是**别人的输入**
决定我们的服务器去访问哪里 —— 典型的 SSRF 面。真实风险不是理论上的:
云环境里 `http://169.254.169.254/` 一把就能读到实例凭据。

难点在于不能一刀切封掉内网:ComfyUI 部署在 `http://comfyui:8188` 是完全正当的,
它返回的结果地址就在内网。所以规则是:

    链路本地地址(含云元数据端点)   永远拒绝,没有正当用途
    其余私有地址                     默认拒绝,除非主机在允许清单里
    公网地址                         允许

允许清单从配置来(`DOWNLOAD_ALLOWED_HOSTS`),部署 ComfyUI 时把它的主机名加进去。
这样"我们信任哪些内网地址"是一个显式决定,而不是代码里一个悄悄的例外。
"""
from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urljoin, urlparse

ALLOWED_SCHEMES = frozenset({"http", "https"})

#: 云元数据端点所在网段。任何情况下都不允许,加进允许清单也不行。
LINK_LOCAL_NETWORKS = (
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("fe80::/10"),
)

#: 运营商级 NAT。Python 的 `is_private` **不含**它(RFC 6598 不在那张表里),
#: 而这个网段在真实部署里就是内网 —— 少了它,一个解析到 100.64.x.x 的主机
#: 会被当成公网放行。
EXTRA_PRIVATE_NETWORKS = (ipaddress.ip_network("100.64.0.0/10"),)


class UnsafeDownloadURL(ValueError):
    """URL 不允许下载。调用方应把它当作输入错误,不重试。"""


def _parse_hosts(raw: str) -> set[str]:
    return {h.strip().lower() for h in (raw or "").replace(";", ",").split(",") if h.strip()}


def _unmap(address):
    """IPv4-mapped IPv6 -> 对应的 IPv4。其余原样返回。

    **这一步不能省。** `::ffff:169.254.169.254` 是云元数据端点的合法写法,
    而它:

        in ip_network("169.254.0.0/16")   False   ← 网段判断不认它
        in ip_network("fe80::/10")        False
        .is_link_local                    False   ← 标准库也不认
        .is_private                       True

    于是 `_is_link_local` 放行,而允许清单那条分支**只查 `_is_link_local`**
    (信任的主机跳过 `_is_private`)—— 一个被加进 `DOWNLOAD_ALLOWED_HOSTS`
    的域名只要解析到这个映射地址,实例凭据就出去了。

    这直接违反本模块声明的不变量:「链路本地永远拒绝,加进允许清单也不行」。
    所以归一化放在两个判定函数的入口,而不是让每个调用点自己记得展开。
    """
    mapped = getattr(address, "ipv4_mapped", None)
    return mapped if mapped is not None else address


def _is_link_local(address) -> bool:
    address = _unmap(address)
    return any(address in network for network in LINK_LOCAL_NETWORKS)


def _is_private(address) -> bool:
    address = _unmap(address)
    return (
        address.is_private
        or address.is_loopback
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
        or any(address in network for network in EXTRA_PRIVATE_NETWORKS)
    )


def _resolve(host: str) -> list[ipaddress.IPv4Address | ipaddress.IPv6Address]:
    """把主机名解析成 IP。解析不了就返回空 —— 由调用方决定怎么办。"""
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []
    addresses = []
    for info in infos:
        try:
            addresses.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return addresses


def check_download_url(
    url: str,
    *,
    allowed_hosts: str | set[str] = "",
    resolve: bool = True,
) -> str:
    """校验一个待下载的 URL,通过则原样返回,否则抛 `UnsafeDownloadURL`。

    ``resolve=False`` 时只做字面检查(不发 DNS),用于单元测试与离线环境。
    """
    if not url or not url.strip():
        raise UnsafeDownloadURL("下载地址为空")

    parsed = urlparse(url.strip())
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise UnsafeDownloadURL(f"只允许 http/https,收到 {parsed.scheme or '空'}")
    if not parsed.hostname:
        raise UnsafeDownloadURL("下载地址缺少主机名")

    host = parsed.hostname.lower()
    allowlist = allowed_hosts if isinstance(allowed_hosts, set) else _parse_hosts(allowed_hosts)

    # 字面量 IP:直接判断,不必解析
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    if literal is not None:
        if _is_link_local(literal):
            raise UnsafeDownloadURL("链路本地地址(含云元数据端点)一律拒绝")
        if _is_private(literal) and host not in allowlist:
            raise UnsafeDownloadURL(f"内网地址 {host} 不在下载允许清单里")
        return url

    if host in allowlist:
        # 显式信任的主机(例如自建 ComfyUI),仍然要挡住链路本地
        for address in _resolve(host) if resolve else []:
            if _is_link_local(address):
                raise UnsafeDownloadURL("该主机解析到链路本地地址,拒绝")
        return url

    if not resolve:
        return url

    addresses = _resolve(host)
    if not addresses:
        raise UnsafeDownloadURL(f"无法解析主机 {host}")
    for address in addresses:
        if _is_link_local(address):
            raise UnsafeDownloadURL("链路本地地址(含云元数据端点)一律拒绝")
        if _is_private(address):
            raise UnsafeDownloadURL(
                f"{host} 解析到内网地址 {address},不在下载允许清单里"
            )
    return url


#: 允许跟随的重定向跳数上限。CDN 常见 1-2 跳,给 5 足够,又不至于被无限跳耗死。
MAX_REDIRECTS = 5


def stream_checked(
    client,
    url: str,
    *,
    allowed_hosts: str | set[str] = "",
    resolve: bool = True,
    max_redirects: int = MAX_REDIRECTS,
):
    """跟着重定向走,但**每一跳都重新过一次 SSRF 校验**。

    为什么必须自己走:``follow_redirects=True`` 时 httpx 在库内部完成跳转,
    只校验最初那个 URL 等于没校验 —— 攻击者给一个公网地址,
    服务器回 302 指向 169.254.169.254,凭据就出去了。这是一个完整的绕过。

    返回的是已经打开的流式响应上下文,调用方照常 ``with`` 使用。

    **DNS Rebinding 由传输层负责**,不在这里。这个函数做的是"这个 URL 允不允许访问",
    而 rebinding 攻击的是"校验用的那次解析"和"连接用的那次解析"之间的缝 ——
    任何在发请求**之前**做的检查都堵不住它。调用方必须用
    `pinned_transport()` 构造 client,那样连接会打到已校验的那个 IP 上。
    见该函数的说明。

    刻意不 import httpx:``client`` 由调用方传入,跳转拼接用标准库的 urljoin。
    这样这个函数在没装 httpx 的环境里也能被单测覆盖 —— 而它恰恰是最需要被覆盖的那个。
    """
    current = check_download_url(url, allowed_hosts=allowed_hosts, resolve=resolve)
    for _ in range(max_redirects + 1):
        request = client.build_request("GET", current)
        response = client.send(request, stream=True, follow_redirects=False)
        if not response.is_redirect:
            return response
        location = response.headers.get("location", "")
        response.close()
        if not location:
            raise UnsafeDownloadURL("重定向响应没有 Location 头")
        # 相对跳转要先补全,否则校验的是一个没有主机名的串
        current = urljoin(current, location)
        current = check_download_url(current, allowed_hosts=allowed_hosts, resolve=resolve)
    raise UnsafeDownloadURL(f"重定向超过 {max_redirects} 跳,拒绝继续跟随")


# ------------------------------------------------------------------ DNS 钉住

def resolve_pinned(host: str, *, allowed_hosts: str | set[str] = "") -> str:
    """把主机名解析成**一个已经校验过的 IP**,连接时直接用它。

    这是 DNS Rebinding 的唯一解法。只做请求前检查是不够的:

        我们解析 evil.com          -> 1.2.3.4(公网,放行)
        httpx 自己再解析一次       -> 127.0.0.1(攻击者把 TTL 设成 0)
        连接打到本机               -> 校验形同虚设

    两次解析之间那道缝没法用"再检查一次"补上,因为补丁本身也要解析一次。
    唯一能关掉它的办法是**让校验和连接用同一个解析结果**,也就是这里返回的 IP。

    字面量 IP 原样返回(它本来就没有解析这一步)。
    解析失败或全部落在禁止的网段时抛 `UnsafeDownloadURL`。
    """
    lowered = (host or "").strip().lower()
    if not lowered:
        raise UnsafeDownloadURL("下载地址缺少主机名")

    try:
        literal = ipaddress.ip_address(lowered)
    except ValueError:
        literal = None

    allowlist = allowed_hosts if isinstance(allowed_hosts, set) else _parse_hosts(allowed_hosts)

    if literal is not None:
        if _is_link_local(literal):
            raise UnsafeDownloadURL("链路本地地址(含云元数据端点)一律拒绝")
        if _is_private(literal) and lowered not in allowlist:
            raise UnsafeDownloadURL(f"内网地址 {lowered} 不在下载允许清单里")
        return lowered

    addresses = _resolve(lowered)
    if not addresses:
        raise UnsafeDownloadURL(f"无法解析主机 {lowered}")

    trusted = lowered in allowlist
    for address in addresses:
        # 链路本地永远拒绝,加进允许清单也不行 —— 云元数据端点没有正当用途
        if _is_link_local(address):
            raise UnsafeDownloadURL("该主机解析到链路本地地址,拒绝")
        if _is_private(address) and not trusted:
            raise UnsafeDownloadURL(
                f"{lowered} 解析到内网地址 {address},不在下载允许清单里"
            )

    # 取第一个。**不做故障转移轮询**:轮到第二个地址意味着又要重新决定连哪里,
    # 而"重新决定"正是我们刚刚关掉的那道缝。一个地址连不上就让这次下载失败,
    # 重试会走完整的解析 + 校验流程。
    return str(addresses[0])


def pinned_transport(*, allowed_hosts: str | set[str] = "", is_async: bool = False):
    """构造一个把连接钉在已校验 IP 上的 httpx transport。

    做法是在发请求前把 URL 里的主机名换成校验过的 IP,同时:

        Host 头       保留原始主机名 —— 虚拟主机和 CDN 全靠它路由
        SNI           保留原始主机名 —— 否则 TLS 握手会失败,证书也校验不上

    证书仍然按**原始主机名**校验(httpcore 用 ``sni_hostname`` 做 server_hostname),
    所以这不会削弱 TLS —— 它只改变"连到哪个 IP",不改变"信任谁"。

    每个请求各自钉一次,因此逐跳重定向的每一跳都受同样的保护。

    httpx 在函数内部 import:这个模块的其余部分要能在没装 httpx 的机器上跑测试。
    """
    import httpx

    base_class = httpx.AsyncHTTPTransport if is_async else httpx.HTTPTransport

    class _PinnedTransport(base_class):  # type: ignore[misc, valid-type]
        def _pin(self, request):
            host = request.url.host
            pinned = resolve_pinned(host, allowed_hosts=allowed_hosts)
            if pinned == host:
                return request
            # 原始 Host 头要带端口(如果 URL 里有),否则虚拟主机会路由错
            authority = request.url.netloc.decode("ascii")
            request.headers["Host"] = authority
            request.url = request.url.copy_with(host=pinned)
            request.extensions = {**request.extensions, "sni_hostname": host}
            return request

        def handle_request(self, request):
            return super().handle_request(self._pin(request))

        async def handle_async_request(self, request):
            return await super().handle_async_request(self._pin(request))

    return _PinnedTransport()
