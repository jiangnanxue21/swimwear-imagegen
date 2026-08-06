"""多模态调用的图片准备层(M2 从 evaluators 抽出)。

把「一个地址」变成「一张可以发给模型的图」,中间做完全部安全与合规检查:

    SSRF 校验        逐跳跟随重定向,每一跳重新校验
    DNS Rebinding    连接钉在已校验的 IP 上
    大小与像素上限    边读边算,不是读完再判断
    格式白名单        只接受 JPEG / PNG / WEBP
    EXIF 旋转        手机直出的图不转会让模型如实报告"人体姿态异常"

## 为什么必须共用

识别层(M3)要逐图调用模型,需要的图片准备逻辑和评分层**完全一样**。
如果它自己写一遍,写出来的多半是「requests.get(url).content」——
上面五条一条都不会有,而 SSRF 那条正是这个项目已经修过两轮的地方。

抽出来之后,`app/evaluators` 与 `app/extractors` 引用同一个 `ImageResolver`,
不存在两份实现。`tests/pure/test_llm_layer.py` 用 AST 守住这条。

依赖方向:本模块**不得** import `app/evaluators`、`app/extractors`、`app/channels`。
"""
from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from typing import Any

from app.core.logging import get_logger
from app.providers.errors import ProviderInputError

logger = get_logger(__name__)

#: Pillow 格式名 -> MIME。只支持这三种,其余一律拒收 ——
#: 视觉模型对 GIF/BMP/TIFF 的支持参差不齐,与其赌不如明确报错。
SUPPORTED_FORMATS: dict[str, str] = {
    "JPEG": "image/jpeg",
    "PNG": "image/png",
    "WEBP": "image/webp",
}

DATA_URL_PREFIX = "data:"

#: base64 相对原文的膨胀率。4/3 再留一点余量给换行和 padding。
BASE64_OVERHEAD = 1.40

#: 单边像素上限。超过这个尺寸的图对评分没有任何帮助 —— 视觉模型自己也会缩,
#: 但**我们**得先在内存里把它解出来才能缩,那才是问题所在。
MAX_IMAGE_EDGE_PX = 12000

#: 总像素上限(约 8000×8000)。压缩后的字节数完全挡不住这一类攻击:
#: 一张 50000×50000 的纯色 PNG 可以只有几十 KB,解码出来是 7.5 GB。
#: Pillow 自己的 DecompressionBomb 阈值是 ~1.79 亿像素,对我们来说太宽松了。
MAX_IMAGE_PIXELS = 64_000_000


@dataclass(frozen=True)
class PreparedImage:
    """一张准备好发出去的图片。

    ``url`` 与 ``data_url`` 二选一:走公网 URL 模式时只有前者,
    内联模式时只有后者。``byte_size`` 与 ``mime_type`` 两种模式都会填,
    因为它们要进日志和 raw_response,而 base64 本身不会。
    """

    role: str
    mime_type: str
    byte_size: int
    url: str | None = None
    data_url: str | None = None

    @property
    def inline(self) -> bool:
        return self.data_url is not None

    def payload_url(self) -> str:
        """发给模型的那个字符串。"""
        value = self.data_url or self.url
        if not value:
            raise ProviderInputError(f"{self.role} 没有可用的图片地址", provider="vision")
        return value

    def describe(self) -> dict[str, Any]:
        """可以安全写进日志和 raw_response 的摘要。刻意不含 url 与 base64。"""
        return {
            "role": self.role,
            "mime_type": self.mime_type,
            "byte_size": self.byte_size,
            "inline": self.inline,
        }


def _too_large(role: str, size: int, limit: int) -> ProviderInputError:
    return ProviderInputError(
        f"{role} 图片 {size // 1024} KB,超过上限 {limit // 1024 // 1024} MB;"
        "请调整 VISION_MODEL_MAX_IMAGE_MB 或先压缩素材",
        provider="vision",
        detail={"role": role, "byte_size": size, "limit_bytes": limit},
    )


def decode_data_url(
    value: str, *, role: str, max_bytes: int | None = None
) -> tuple[bytes, str]:
    """解析 data URL,返回 (字节, 声明的 MIME)。

    声明的 MIME 只是参考值,调用方仍然要用 Pillow 复核 —— 不然一个
    ``data:image/png;base64,<PDF>`` 就能骗过所有检查。
    """
    if not value.startswith(DATA_URL_PREFIX):
        raise ProviderInputError(f"{role} 不是 data URL", provider="vision")
    try:
        header, encoded = value.split(",", 1)
    except ValueError:
        raise ProviderInputError(f"{role} 的 data URL 缺少逗号分隔", provider="vision") from None
    if "base64" not in header:
        raise ProviderInputError(f"{role} 的 data URL 不是 base64 编码", provider="vision")

    declared = header[len(DATA_URL_PREFIX):].split(";")[0].strip() or "application/octet-stream"

    # **先看文本长度,再解码。** 以前是解完整个 base64 才去比大小,
    # 于是一个 200 MB 的 data URL 会先在内存里变成 150 MB 的 bytes,
    # 然后我们才礼貌地告诉它"超过上限了" —— 该守的那道门在它身后。
    if max_bytes is not None:
        limit = int(max_bytes * BASE64_OVERHEAD) + 128
        if len(encoded) > limit:
            raise _too_large(role, int(len(encoded) / BASE64_OVERHEAD), max_bytes)

    try:
        payload = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError):
        raise ProviderInputError(f"{role} 的 base64 内容无法解码", provider="vision") from None
    return payload, declared


def verify_image(payload: bytes, *, role: str) -> tuple[bytes, str]:
    """确认这是一张受支持的图片,并应用 EXIF 旋转。

    返回 (可发送的字节, MIME)。只有真的做了旋转时才会重新编码 ——
    无条件重编码会白白损失一次 JPEG 画质,而画质正是这次评分要看的东西。
    """
    try:
        from PIL import Image, ImageOps
    except ImportError:  # pragma: no cover - 取决于运行环境
        raise ProviderInputError(
            "缺少 Pillow 依赖,无法校验图片", provider="vision"
        ) from None

    import io
    import warnings

    # 像素上限和警告过滤都是**进程级**状态,必须用完就还。
    #
    # 以前这里每次调用都无条件执行 warnings.filterwarnings("error", ...) 并
    # 改写 Image.MAX_IMAGE_PIXELS,从此整个 Celery worker 进程都跟着变:
    # 素材上传、多尺寸出图、以及任何并发线程,都会继承一份它们没要求过的策略。
    # 表现是"同样一张图,先跑评分再上传就失败,反过来就成功" ——
    # 一个只和调用顺序有关的偶发故障,排查起来毫无线索。
    #
    # catch_warnings 是上下文管理器,退出时恢复整张过滤表;MAX_IMAGE_PIXELS
    # 自己存旧值再还回去。两者都不再泄漏到函数外面。
    #
    # 注意 catch_warnings 本身不是线程安全的(它改的是全局过滤表),
    # 所以真正的解法是在进程启动时统一配置一次;这里的收敛只保证
    # **本函数不再是污染源**,不承诺在多线程下把窗口关到零。
    previous_max_pixels = Image.MAX_IMAGE_PIXELS
    with warnings.catch_warnings():
        # DecompressionBombWarning 默认只是一条 warning,程序照常把图解出来 ——
        # 而它警告的正是"这张图解开会吃掉大量内存"。把它升成异常,
        # 让这类图在 Pillow 真正分配内存之前就被挡下来。
        warnings.filterwarnings("error", category=Image.DecompressionBombWarning)
        # 自己的阈值比 Pillow 默认的 ~1.79 亿像素严:我们是批量后台任务,
        # 一个 worker 同时处理好几张图,吃不起那么宽的余量。
        Image.MAX_IMAGE_PIXELS = MAX_IMAGE_PIXELS
        try:
            return _verify_image_inner(payload, role=role, io=io, Image=Image, ImageOps=ImageOps)
        finally:
            Image.MAX_IMAGE_PIXELS = previous_max_pixels


def _verify_image_inner(payload: bytes, *, role: str, io, Image, ImageOps) -> tuple[bytes, str]:
    """`verify_image` 的实际逻辑。

    单独抽出来只为让上面那个函数的作用域管理一目了然 —— 全局状态的设置、
    使用和恢复挤在一个函数里时,中间任何一条 return 都可能绕过恢复。
    """
    try:
        with Image.open(io.BytesIO(payload)) as probe:
            fmt = (probe.format or "").upper()
            size = probe.size
            probe.verify()
    except Image.DecompressionBombWarning as exc:
        raise ProviderInputError(
            f"{role} 的图片像素数过大,拒绝解码",
            provider="vision",
            detail={"role": role, "reason": str(exc)[:200]},
        ) from None
    except Exception:  # noqa: BLE001 - Pillow 的异常类型很杂,一律当成"不是图片"
        raise ProviderInputError(
            f"{role} 不是可识别的图片文件", provider="vision", detail={"role": role}
        ) from None

    # 尺寸检查放在 verify 之后、真正解码之前。
    # 只限制压缩后的字节数是挡不住的:一张 50000×50000 的纯色 PNG 可以只有几十 KB,
    # 解码出来却是几个 GB —— 而 EXIF 旋转那一步一定会解码。
    width, height = size
    if width > MAX_IMAGE_EDGE_PX or height > MAX_IMAGE_EDGE_PX:
        raise ProviderInputError(
            f"{role} 的图片尺寸 {width}×{height} 超过单边上限 {MAX_IMAGE_EDGE_PX} 像素",
            provider="vision",
            detail={"role": role, "width": width, "height": height},
        )
    if width * height > MAX_IMAGE_PIXELS:
        raise ProviderInputError(
            f"{role} 的图片共 {width * height} 像素,超过上限 {MAX_IMAGE_PIXELS}",
            provider="vision",
            detail={"role": role, "width": width, "height": height},
        )

    if fmt not in SUPPORTED_FORMATS:
        raise ProviderInputError(
            f"{role} 的图片格式 {fmt or '未知'} 不受支持,只接受 JPEG / PNG / WEBP",
            provider="vision",
            detail={"role": role, "format": fmt},
        )

    mime = SUPPORTED_FORMATS[fmt]

    # EXIF Orientation:手机直出的参考图很常见。不转的话模型会看到一张躺倒的图,
    # 然后如实报告"人体姿态异常" —— 一个纯粹由我们自己引入的假阳性。
    try:
        with Image.open(io.BytesIO(payload)) as image:
            exif = image.getexif()
            orientation = exif.get(0x0112) if exif else None
            if not orientation or int(orientation) == 1:
                return payload, mime
            fixed = ImageOps.exif_transpose(image)
            buffer = io.BytesIO()
            save_format = "JPEG" if fmt == "JPEG" else fmt
            params: dict[str, Any] = {"quality": 95} if save_format == "JPEG" else {}
            if fixed.mode in ("RGBA", "P") and save_format == "JPEG":
                fixed = fixed.convert("RGB")
            fixed.save(buffer, format=save_format, **params)
            return buffer.getvalue(), mime
    except ProviderInputError:
        raise
    except Exception:  # noqa: BLE001 - 转不了就用原图,不该因为方向问题整单失败
        logger.warning(
            "cannot apply exif orientation, sending the original bytes",
            extra={"extra_fields": {"role": role, "format": fmt}},
        )
        return payload, mime


def to_data_url(payload: bytes, mime_type: str) -> str:
    return f"data:{mime_type};base64," + base64.b64encode(payload).decode("ascii")


class ImageResolver:
    """把一个地址解析成 PreparedImage。

    storage 是注入进来的,因为本地存储和 S3 的读取方式不同,而评分器不该关心这件事。
    """

    def __init__(self, storage: Any, *, max_bytes: int, allowed_hosts: str = "") -> None:
        self._storage = storage
        self._max_bytes = max_bytes
        self._allowed_hosts = allowed_hosts

    async def resolve(
        self, reference: str, *, role: str, prefer_public_url: bool = False
    ) -> PreparedImage:
        if not reference or not str(reference).strip():
            raise ProviderInputError(f"{role} 的图片地址为空", provider="vision")

        value = str(reference).strip()

        if value.startswith(DATA_URL_PREFIX):
            payload, _declared = decode_data_url(
                value, role=role, max_bytes=self._max_bytes
            )
            return await self._finish(payload, role=role)

        if value.startswith(("http://", "https://")):
            return await self._from_url(value, role=role, prefer_public_url=prefer_public_url)

        # 剩下的都当成项目内的 storage_path
        return await self._from_storage(
            value, role=role, prefer_public_url=prefer_public_url
        )

    # ------------------------------------------------------------------ 内部

    async def _finish(self, payload: bytes, *, role: str) -> PreparedImage:
        """大小 -> 校验 -> 编码。顺序不能换:先限大小才不会被超大文件拖垮。

        Pillow 的解码和 EXIF 旋转是 CPU 密集的同步调用,放进线程池 ——
        直接在事件循环里跑,四张图就能把循环卡住足够久,而这个函数长得像异步的。
        """
        import asyncio

        if len(payload) > self._max_bytes:
            raise _too_large(role, len(payload), self._max_bytes)
        verified, mime = await asyncio.to_thread(verify_image, payload, role=role)
        # EXIF 旋转后可能变大,再查一次
        if len(verified) > self._max_bytes:
            raise _too_large(role, len(verified), self._max_bytes)
        return PreparedImage(
            role=role,
            mime_type=mime,
            byte_size=len(verified),
            data_url=to_data_url(verified, mime),
        )

    async def _from_url(self, url: str, *, role: str, prefer_public_url: bool) -> PreparedImage:
        """HTTP(S) 地址。

        即使只是把地址转发给模型厂商,也要过 SSRF 校验 —— 让厂商去访问
        ``http://169.254.169.254/`` 和我们自己去访问是一样的问题,
        区别只是日志里看不见。
        """
        from app.core.net_safety import UnsafeDownloadURL, check_download_url

        try:
            check_download_url(url, allowed_hosts=self._allowed_hosts)
        except UnsafeDownloadURL as exc:
            raise ProviderInputError(
                f"{role} 的图片地址未通过安全校验:{exc}",
                provider="vision",
                detail={"role": role},
            ) from None

        if prefer_public_url and url.startswith("https://"):
            # 只把 https 地址交给厂商:http 地址多半是开发期的 localhost,
            # 厂商根本访问不到,发出去只会换来一个含混的 400。
            #
            # 但**转发地址不等于跳过检查**。以前这里直接返回 URL 并把 mime 和
            # byte_size 填成空值和零,于是这条路径上没有任何东西确认过:
            # 文件还在不在、是不是图片、有没有超限、有没有在生成之后被替换掉。
            # 结果是同一张图走内联模式会被拒收,走公网模式就一路畅通 ——
            # 两条路径的安全性不该差这么多。
            payload = await self._download(url, role=role)
            return await self._verified_public_url(url, payload, role=role)

        payload = await self._download(url, role=role)
        return await self._finish(payload, role=role)

    async def _verified_public_url(
        self, url: str, payload: bytes, *, role: str
    ) -> PreparedImage:
        """校验字节内容,通过之后才把 URL 交给厂商。

        ``payload`` 由调用方取:存储路径直接读文件(本地/S3 都比 HTTP 便宜),
        外部 HTTP 地址才真的下一遍。两条路径共用同一套校验,
        所以\"走公网模式\"和\"走内联模式\"受到的约束完全一样 ——
        以前不是这样:公网模式下 mime 和 byte_size 直接填空值和零,
        文件在不在、是不是图片、有没有被替换掉,一概没查。
        """
        import asyncio

        if len(payload) > self._max_bytes:
            raise _too_large(role, len(payload), self._max_bytes)
        verified, mime = await asyncio.to_thread(verify_image, payload, role=role)

        if len(verified) != len(payload):
            # EXIF 旋转改写了字节:厂商拿到的将是**未旋转**的原图,和我们校验过的
            # 那份不是同一张。这种情况必须改走内联,否则模型看到的是一张躺倒的图,
            # 然后如实报告"人体姿态异常"——一个由我们自己引入的假阳性。
            logger.info(
                "image needed exif rotation, sending inline instead of by url",
                extra={"extra_fields": {"role": role}},
            )
            return PreparedImage(
                role=role,
                mime_type=mime,
                byte_size=len(verified),
                data_url=to_data_url(verified, mime),
            )

        return PreparedImage(
            role=role, mime_type=mime, byte_size=len(verified), url=url
        )

    async def _download(self, url: str, *, role: str) -> bytes:
        """流式下载,**逐跳**跟随重定向。

        以前是 ``follow_redirects=False`` 之后直接把响应体当图片解析。那有两个后果:
        合法的 CDN 重定向(302 到实际存储地址)会拿到一个空 body 然后报
        "不是可识别的图片";而如果哪天有人改成 ``follow_redirects=True``,
        httpx 会在库内部完成跳转,只校验最初那个 URL 等于没校验 ——
        攻击者给一个公网地址,服务器回 302 指向内网,凭据就出去了。

        同步版 `net_safety.stream_checked` 已经解决过同样的问题。这里是它的
        异步对应物:每一跳都重新过一次 URL 与 DNS 校验。
        """
        import httpx

        from app.core.net_safety import (
            MAX_REDIRECTS,
            UnsafeDownloadURL,
            check_download_url,
            pinned_transport,
        )

        current = url
        try:
            # transport 把连接钉在已校验的 IP 上。逐跳校验挡的是 302 绕过,
            # 钉 IP 挡的是 DNS Rebinding —— 两者防的不是同一件事,都要有。
            async with httpx.AsyncClient(
                timeout=30.0,
                follow_redirects=False,
                transport=pinned_transport(
                    allowed_hosts=self._allowed_hosts, is_async=True
                ),
            ) as client:
                for _ in range(MAX_REDIRECTS + 1):
                    async with client.stream("GET", current) as response:
                        if response.is_redirect:
                            location = response.headers.get("location", "")
                            if not location:
                                raise ProviderInputError(
                                    f"{role} 的图片地址重定向但没有 Location 头",
                                    provider="vision",
                                    detail={"role": role},
                                )
                            from urllib.parse import urljoin

                            # 相对跳转基于 **current** 补全,不能用 response.url。
                            #
                            # pinned_transport 会把请求 URL 的主机名改写成校验过的
                            # IP,于是 response.url 拿到的是 https://1.2.3.4/... ——
                            # 用它做 base,下一跳就变成了对着裸 IP 发请求:
                            # 原始 Host 和 SNI 全丢了,虚拟主机路由错、TLS 证书
                            # 也对不上。`current` 始终是逻辑地址,同步版
                            # net_safety.stream_checked 用的也是它。
                            current = urljoin(current, location)
                            try:
                                check_download_url(
                                    current, allowed_hosts=self._allowed_hosts
                                )
                            except UnsafeDownloadURL as exc:
                                raise ProviderInputError(
                                    f"{role} 的图片地址重定向到了不允许的位置:{exc}",
                                    provider="vision",
                                    detail={"role": role},
                                ) from None
                            continue

                        if response.status_code >= 400:
                            raise ProviderInputError(
                                f"{role} 的图片地址返回 HTTP {response.status_code}",
                                provider="vision",
                                detail={"role": role, "status": response.status_code},
                            )
                        chunks: list[bytes] = []
                        total = 0
                        # 边读边算。读完再判断等于没判断:声称 10KB 实际 2GB 的地址
                        # 会在触发上限之前就把内存吃光
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > self._max_bytes:
                                raise _too_large(role, total, self._max_bytes)
                            chunks.append(chunk)
                        return b"".join(chunks)

                raise ProviderInputError(
                    f"{role} 的图片地址重定向超过 {MAX_REDIRECTS} 跳,拒绝继续跟随",
                    provider="vision",
                    detail={"role": role},
                )
        except ProviderInputError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ProviderInputError(
                f"{role} 的图片下载失败:{type(exc).__name__}",
                provider="vision",
                detail={"role": role},
            ) from exc

    async def _from_storage(
        self, storage_path: str, *, role: str, prefer_public_url: bool
    ) -> PreparedImage:
        if self._storage is None:
            raise ProviderInputError(
                f"{role} 是存储路径但没有可用的存储后端", provider="vision"
            )

        if prefer_public_url:
            try:
                from app.services.storage import asset_url

                # 素材多半落在私有前缀下,拿到的是短期签名地址;
                # 直接调 public_url 会给出一个**永久匿名可读**的地址,
                # 而它随后会被发给第三方厂商并留在对方日志里
                public = asset_url(self._storage, storage_path)
            except Exception:  # noqa: BLE001 - 拿不到公网地址就退回内联,不是致命错误
                public = ""
            if public.startswith("https://"):
                # 和 HTTP 分支同样的规矩:转发地址之前必须自己确认过一遍。
                # 存储里的对象可能已经被删掉、被别的内容覆盖,或者根本不是图片。
                # 字节直接从存储读,不绕 HTTP —— 那是同一份数据,而且便宜得多。
                import asyncio as _asyncio

                try:
                    raw = await _asyncio.to_thread(self._storage.read, storage_path)
                    return await self._verified_public_url(public, raw, role=role)
                except ProviderInputError:
                    # 验不过就不发这个地址。继续往下走会读同一份字节再验一次、
                    # 再失败一次,所以直接抛出去 —— 让评分那一张明确地失败,
                    # 而不是把一个没验过的 URL 发给厂商。
                    raise
                except FileNotFoundError:
                    raise ProviderInputError(
                        f"{role} 对应的文件不存在:{storage_path}",
                        provider="vision",
                        detail={"role": role},
                    ) from None
                except Exception:  # noqa: BLE001 - 读不到就退回内联那条路重试一次
                    logger.warning(
                        "cannot read the object for public-url verification, "
                        "falling back to inline",
                        extra={"extra_fields": {"role": role}},
                    )

        import asyncio

        try:
            # 本地是文件 IO,S3 是网络 IO,两者都是同步阻塞调用
            payload = await asyncio.to_thread(self._storage.read, storage_path)
        except FileNotFoundError:
            raise ProviderInputError(
                f"{role} 对应的文件不存在:{storage_path}",
                provider="vision",
                detail={"role": role},
            ) from None
        except Exception as exc:  # noqa: BLE001
            raise ProviderInputError(
                f"{role} 读取失败:{type(exc).__name__}",
                provider="vision",
                detail={"role": role},
            ) from exc
        return await self._finish(payload, role=role)
