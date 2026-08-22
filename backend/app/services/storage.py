"""存储抽象。本地文件存储 + S3/MinIO 兼容存储(阶段 6)。

不变量:
- 落盘路径由 sha256 推导,同一份内容全局只存一份;
- 目标已存在时**不覆盖**,直接复用(需求第十九章「原始上传文件不得覆盖」);
- 所有写入都经过 resolve_within,杜绝路径穿越;
- **访问级别按前缀区分**:只有 ``outputs/`` 是公开的,其余一律签名 URL。

## 为什么要区分访问级别(评审第 2 条)

以前整个存储目录被原封不动挂成 ``/files`` 静态服务,S3 实现也对所有路径
一视同仁地拼公开 URL。于是商品原始图、模特参考图、还没通过审核的候选图,
和"已经批准上网站的成品图"共用同一个访问级别 —— 补上 API 鉴权也没用,
**URL 一旦泄露就是永久匿名可读的**,而这些 URL 会出现在日志、浏览器历史、
发给第三方 Provider 的请求体里。

现在分两级:

    outputs/                    公开。它本来就是要挂到网站上的
    uploads/ models/ candidates/  私有。只能通过短期签名 URL 访问

调用方不需要记住哪个前缀是哪一级 —— 用 `asset_url()`,它按前缀自己分派。
"""
from __future__ import annotations

import base64
import contextlib
import errno
import hashlib
import hmac
import os
import tempfile
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path

from app.core.errors import ConfigError
from app.core.hashing import shard_path
from app.core.paths import resolve_within

#: 允许匿名公开访问的前缀。**只有成品图**。
#:
#: 写成元组而不是"除了这几个都公开",是因为这两种写法在加一个新前缀时
#: 行为相反:白名单下新前缀默认私有,黑名单下默认公开。默认值错的那一次
#: 不会有任何报错,只会安静地把素材挂上公网。
PUBLIC_PREFIXES: tuple[str, ...] = ("outputs",)

#: 签名 URL 的默认有效期。够加载一个页面,不够被人转发到别处长期使用。
#: A45-#28:从 600 秒抬到 1800 秒。
#:
#: 列表页 / 图片集 / 素材库都**不轮询**(那是刻意的:轮询只为了刷新图片地址
#: 是一笔很贵的交易)。于是页面挂机超过 TTL 之后,所有私有图一起 403 裂掉,
#: 而现象酷似"图坏了" —— 快审页对"读取失败"有防呆,对"链接过期"没有。
#:
#: 1800 秒覆盖一次正常的审图会话(快审一件 30 秒左右,一轮几十件)。
#: **不抬得更高**:签名 URL 一旦离开浏览器(截图、复制链接、日志),
#: TTL 就是它的可用窗口,那个窗口应当短于"这个人还在岗"的时间尺度。
#:
#: **这只是把窗口挪开,不是修好。** 真正的修法是 `<Image onError>` 触发一次
#: 重新签名 —— 本轮只在图片集页接了那一处(它有现成的 `refresh()`),
#: 素材库与商品详情页仍然会裂。剩余面记在 A45-#28。
SIGNED_URL_TTL_SECONDS = 1800

#: `os.link` 报这些 errno 时,说明**这个文件系统不支持硬链接**,不是写失败。
#: 其余 OSError(磁盘满、权限、IO 错误)必须原样抛出去 —— 把它们混进
#: "退回 replace" 的分支会让真正的原因被第二次失败盖掉。
#:
#: EPERM 也在里面:FAT / 部分 fuse 挂载对 link 返回的正是它,而不是 ENOSYS。
_NO_HARDLINK_ERRNOS: frozenset[int] = frozenset(
    {
        errno.EPERM,       # 文件系统拒绝 link(FAT、部分 fuse)
        errno.ENOSYS,      # 内核/驱动没实现
        errno.EXDEV,       # 跨设备(不该发生,同目录 temp,但便宜)
        errno.EMLINK,      # 链接数到顶
        errno.EOPNOTSUPP,  # 明确不支持
    }
)


@dataclass(frozen=True)
class StoredFile:
    storage_path: str  # 相对路径,如 ab/cd/<hash>.jpg
    file_hash: str
    size: int
    deduplicated: bool  # True 表示内容已存在,本次未写盘


def is_public_path(storage_path: str) -> bool:
    """这个路径是不是公开的。"""
    head = (storage_path or "").strip("/").split("/", 1)[0]
    return head in PUBLIC_PREFIXES


class ObjectStorage(ABC):
    @abstractmethod
    def save(self, data: bytes, *, file_hash: str, extension: str, prefix: str = "") -> StoredFile:
        ...

    @abstractmethod
    def exists(self, storage_path: str) -> bool:
        ...

    @abstractmethod
    def read(self, storage_path: str) -> bytes:
        ...

    @abstractmethod
    def public_url(self, storage_path: str) -> str:
        """无期限、无凭据即可访问的地址。**只对 ``outputs/`` 有意义。**"""

    def signed_url(self, storage_path: str, *, expires_in: int = SIGNED_URL_TTL_SECONDS) -> str:
        """短期有效的访问地址。默认退回公开地址,子类应当覆盖。

        刻意不是 abstractmethod:加一个抽象方法会让所有已有实现当场失效。
        但覆盖它是**必须**的 —— 不覆盖等于私有素材仍然公开可读,
        `build_storage()` 返回的两个实现都覆盖了。
        """
        return self.public_url(storage_path)

    def delete(self, storage_path: str) -> bool:
        """删掉一个对象。删不掉或本来就不存在都返回 False,不抛。

        **只给失败回滚用。** 存储是内容寻址的,同样的内容会被去重成同一个路径,
        所以删除一个还有记录指向的对象等于把别人的文件删了。调用方必须自己确认
        这个对象是本次新建的(``StoredFile.deduplicated`` 为 False)且没有登记成功,
        并且**在删之前查一遍数据库还有没有别的记录指向它**
        (`output_service._discard_orphans`)。

        刻意不是 abstractmethod:加一个抽象方法会让所有已有的实现当场失效,
        而这个能力是可选的 —— 拿不到就多留几个孤儿文件,不该让系统起不来。
        """
        raise NotImplementedError

    def healthcheck(self) -> None:
        """就绪探针用。不可用时抛异常,异常类型即诊断信息。

        默认实现什么都不做:一个连自检都没有的后端,至少不该假装自己是好的 ——
        所以两个真实实现都覆盖了它,这里只是不让第三方实现被迫改代码。
        """
        return None


def asset_url(
    storage: ObjectStorage,
    storage_path: str,
    *,
    expires_in: int = SIGNED_URL_TTL_SECONDS,
) -> str:
    """按访问级别给出可用的地址。**所有对外暴露 URL 的地方都该用这个。**

    直接调 ``public_url()`` 是评审第 2 条的根因:那个函数的名字听起来像
    "取这个对象的地址",实际含义是"把这个对象当成公开的"。调用点分散在
    七八个文件里,没有一处会去想自己拿的是不是私有素材。
    """
    if not storage_path:
        return ""
    if is_public_path(storage_path):
        return storage.public_url(storage_path)
    return storage.signed_url(storage_path, expires_in=expires_in)


# ------------------------------------------------------------------ 本地签名

def _signing_key() -> bytes:
    from app.core.secrets import url_signing_key

    return url_signing_key()


def sign_local_path(storage_path: str, expires_at: int) -> str:
    """给本地私有对象算签名。

    签的是 ``路径|到期时间`` 而不是只签路径:只签路径的话签名永久有效,
    "短期"两个字就是假的。到期时间必须一起进 HMAC,否则改 URL 里的 exp
    就能续期。
    """
    payload = f"{storage_path}|{expires_at}".encode()
    digest = hmac.new(_signing_key(), payload, hashlib.sha256).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def verify_local_signature(storage_path: str, expires_at: str, signature: str) -> bool:
    """校验签名与有效期。任何一项不对都返回 False,不解释是哪一项。"""
    try:
        deadline = int(expires_at)
    except (TypeError, ValueError):
        return False
    if deadline < int(time.time()):
        return False
    try:
        expected = sign_local_path(storage_path, deadline)
    except Exception:  # noqa: BLE001 - 拿不到密钥就是验不过,不能放行
        return False
    # 定长比较:签名校验里用 == 会泄露前缀匹配长度
    return hmac.compare_digest(expected, signature or "")


class LocalObjectStorage(ObjectStorage):
    def __init__(
        self,
        base_dir: str | Path,
        public_base_url: str = "",
        api_prefix: str = "/api",
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.public_base_url = public_base_url.rstrip("/")
        # 前缀**注入进来**,不在这里读配置 —— 同 `build_storage` 里那句
        # 「配置在这里读而不是在类里读:类保持可注入」。在类里 import
        # `app.core.config` 还有第二个代价:它把整个 pydantic 拖进来,
        # 而 `tests/pure/` 是零三方依赖跑的,一签 URL 就 ModuleNotFoundError
        self.api_prefix = (api_prefix or "/api").rstrip("/")

    def _relative(self, file_hash: str, extension: str, prefix: str) -> str:
        rel = shard_path(file_hash, extension)
        return f"{prefix.strip('/')}/{rel}" if prefix else rel

    def save(self, data: bytes, *, file_hash: str, extension: str, prefix: str = "") -> StoredFile:
        """写入。**"这次是不是我创建的"由内核回答,不由我们自己判断。**

        以前是「先 exists() 再写」:两个事务并发写同一份内容(不同商品渲染出
        相同缩略图很常见)时,两边都在 exists() 里看到不存在,于是**两边都
        返回 deduplicated=False**。其中一边随后落库失败,按"这是我创建的"
        把文件删掉 —— 删的却是另一边已经提交成功的商品的图。

        ``os.link`` 是原子的:目标存在时直接抛 FileExistsError。谁拿到成功谁
        就是创建者,内核说了算,不存在两个人同时"以为"的可能。
        """
        relative = self._relative(file_hash, extension, prefix)
        target = resolve_within(self.base_dir, relative)

        if target.exists():
            # 内容哈希相同即认为文件相同,绝不覆盖已有原始文件
            return StoredFile(relative, file_hash, target.stat().st_size, deduplicated=True)

        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(dir=target.parent, suffix=".part")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)
                # A45-#12:**内容寻址下"路径即内容承诺",这里值一次 fsync。**
                #
                # 不 flush + fsync 的话,write 之后 os.link 之前断电,可以留下一个
                # **路径正确、内容截断**的对象。此后每一次同哈希写入都会在
                # `target.exists()` 那里命中去重、如实回报 `deduplicated=True`,
                # 于是那份坏字节被**永久复用**,而系统里没有任何地方会发现:
                # 哈希是我们算的,文件是别人写坏的,两者从此再也不比对。
                #
                # 同仓 `core/secrets.py` 写主密钥时做了 fsync,理由完全一样。
                # 代价是每次落盘一次同步写 —— 素材上传本来就是 IO 密集路径,
                # 这一次的相对开销远小于"永久复用坏字节"的期望损失。
                fh.flush()
                os.fsync(fh.fileno())
            try:
                os.link(tmp_name, target)
            except FileExistsError:
                # 从上面那次 exists() 到这里之间,别人先建好了。
                # 那份内容和我们要写的一模一样(路径由哈希推导),直接复用,
                # 并且**如实回报 deduplicated=True** —— 这个标记决定了
                # 回滚时会不会去删这个文件。
                return StoredFile(
                    relative, file_hash, target.stat().st_size, deduplicated=True
                )
            except AttributeError:
                # 平台没有 os.link(理论上的 Windows 老版本)。同下。
                os.replace(tmp_name, target)
                tmp_name = ""
                return StoredFile(
                    relative, file_hash, target.stat().st_size, deduplicated=True
                )
            except OSError as exc:
                # **只有"这个文件系统不支持硬链接"才退回 replace。**
                #
                # 以前这里把所有 OSError 一网打尽,于是磁盘满(ENOSPC)、
                # 权限不足(EACCES)也会走进退回分支 —— 再 replace 一次、
                # 再失败一次,抛出去的是**第二个**异常,而第一个(真正的原因)
                # 没了。表现是"存储坏了"但错误指向 replace,查半天查不到磁盘满。
                if exc.errno not in _NO_HARDLINK_ERRNOS:
                    raise
                # 不支持硬链接的文件系统(某些网络挂载)。退回 replace:
                # 它会覆盖,但内容寻址下覆盖的是同样的字节,不会丢数据。
                os.replace(tmp_name, target)
                tmp_name = ""
                # **退化路径不许自称创建者。**
                #
                # `os.link` 那条路上"谁创建的"是内核回答的,所以
                # deduplicated=False 是一个事实。`os.replace` 没有这个保证:
                # 两个并发写入者都会走到这里,都会得到 False,于是**两边都认为
                # 这个文件是自己建的**。其中一边随后落库失败、按"这是我建的"
                # 把文件删掉 —— 删的是另一边已经提交成功的商品的图。
                #
                # 报 True 的代价是失败回滚时留下一个孤儿文件(内容寻址,
                # 下次同样内容还会复用它);报 False 的代价是删掉别人的数据。
                # 两者不对等。
                return StoredFile(
                    relative, file_hash, target.stat().st_size, deduplicated=True
                )
        except BaseException:
            if tmp_name:
                Path(tmp_name).unlink(missing_ok=True)
            raise
        finally:
            if tmp_name:
                Path(tmp_name).unlink(missing_ok=True)
        return StoredFile(relative, file_hash, len(data), deduplicated=False)

    def exists(self, storage_path: str) -> bool:
        return resolve_within(self.base_dir, storage_path).exists()

    def read(self, storage_path: str) -> bytes:
        return resolve_within(self.base_dir, storage_path).read_bytes()

    def public_url(self, storage_path: str) -> str:
        return f"{self.public_base_url}/files/{storage_path}"

    def signed_url(self, storage_path: str, *, expires_in: int = SIGNED_URL_TTL_SECONDS) -> str:
        """私有对象的短期地址,由 ``<API_PREFIX>/media`` 校验签名后代发。

        不能像公开图那样走 ``/files``:那个挂载点现在只服务 ``outputs/``。
        私有素材必须每次都经过一段会检查签名和有效期的代码
        (``api/media_files.py``)。
        """
        deadline = int(time.time()) + max(1, expires_in)
        signature = sign_local_path(storage_path, deadline)
        # 前缀不硬编码 `/api`。`API_PREFIX` 是一个**可配项**,写死之后改它的人
        # 会得到:接口全部搬到 /v1,私有素材地址仍然指向 /api —— 每一张原图、
        # 模特图、候选图都 404,且错误看起来像是存储坏了。
        return (
            f"{self.public_base_url}{self.api_prefix}/media-files/{storage_path}"
            f"?exp={deadline}&sig={signature}"
        )

    def delete(self, storage_path: str) -> bool:
        # 走同一套路径守卫:storage_path 来自数据库,但"来自数据库"不等于可信
        target = resolve_within(self.base_dir, storage_path)
        try:
            target.unlink()
            return True
        except FileNotFoundError:
            return False

    def healthcheck(self) -> None:
        """真的写一个字节再删掉。

        只判断目录存在是不够的:只读挂载、磁盘写满、权限被改,目录都还在,
        而这三种情况下每一次上传都会失败。探针要回答的是"现在能不能存图",
        不是"路径拼对了没有"。
        """
        # A45-#13:**探针文件写进独立子目录,并在每次探活时清扫残留。**
        #
        # 原来是直接写在存储根下的 `.health-*.tmp`。进程崩在 unlink 之前
        # (探活恰好撞上 OOM / 容器被杀)就会留一个下来,而运维在素材根目录里
        # 看到一堆陌生的点文件,无从判断它属于谁、能不能删。
        #
        # 收进 `.health/` 之后这个问题变成"这个目录是谁的"——目录名自解释,
        # 而且清扫可以只在它内部做,不会误伤任何素材(素材路径由哈希推导,
        # 永远不会落进一个以点开头的目录)。
        probe_dir = self.base_dir / ".health"
        probe_dir.mkdir(parents=True, exist_ok=True)
        for stale in probe_dir.glob("probe-*.tmp"):
            # 清扫失败不该让探活失败:探活回答的是"能不能写",不是"干不干净"
            with contextlib.suppress(OSError):
                stale.unlink()
        fd, tmp_name = tempfile.mkstemp(dir=probe_dir, prefix="probe-", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(b"ok")
        finally:
            Path(tmp_name).unlink(missing_ok=True)


class S3ObjectStorage(ObjectStorage):
    """S3 / MinIO 兼容存储(阶段 6)。

    与本地存储共用同一套路径推导(sha256 分片),因此两种后端之间可以直接
    rsync/mc mirror 迁移,数据库里存的 storage_path 一个字都不用改。

    boto3 延迟导入:本地开发模式不该被迫装一个用不到的依赖
    (`pip install -e ".[s3]"` 才需要)。
    """

    def __init__(
        self,
        *,
        bucket: str,
        endpoint_url: str = "",
        access_key: str = "",
        secret_key: str = "",
        region: str = "",
        public_base_url: str = "",
    ) -> None:
        if not bucket:
            raise ValueError("S3 存储缺少 S3_BUCKET")
        self.bucket = bucket
        self.endpoint_url = endpoint_url or None
        self.public_base_url = public_base_url.rstrip("/")
        self._region = region or None
        self._access_key = access_key or None
        self._secret_key = secret_key or None
        self._client = None

    @property
    def client(self):
        if self._client is None:
            try:
                import boto3
                from botocore.config import Config
            except ImportError as exc:  # pragma: no cover - 取决于部署时装没装
                raise RuntimeError(
                    'STORAGE_BACKEND=s3 需要 boto3,请执行 pip install -e ".[s3]"'
                ) from exc

            self._client = boto3.client(
                "s3",
                endpoint_url=self.endpoint_url,
                aws_access_key_id=self._access_key,
                aws_secret_access_key=self._secret_key,
                region_name=self._region,
                # 探针必须有超时。默认是 60 秒重试三次,readiness 会先被
                # 编排系统判超时,拿不到任何诊断信息
                config=Config(
                    connect_timeout=3,
                    read_timeout=5,
                    retries={"max_attempts": 1},
                ),
            )
        return self._client

    @staticmethod
    def _key(file_hash: str, extension: str, prefix: str) -> str:
        rel = shard_path(file_hash, extension)
        return f"{prefix.strip('/')}/{rel}" if prefix else rel

    def save(self, data: bytes, *, file_hash: str, extension: str, prefix: str = "") -> StoredFile:
        """写入。优先用条件写让**服务端**判断"是不是我创建的"。

        ``IfNoneMatch="*"`` 让 S3 在对象已存在时直接返回 412,而不是覆盖。
        这样 ``deduplicated`` 是一个事实而不是一次猜测 —— 见 LocalObjectStorage.save
        里那段关于跨商品误删的说明,S3 上的竞态窗口只会更宽,因为
        head_object 和 put_object 之间隔着两次网络往返。

        老版本 MinIO 不认这个头,退回 head + put 的老路,并把这次退化记进日志。
        """
        from botocore.exceptions import ClientError

        key = self._key(file_hash, extension, prefix)
        if self.exists(key):
            # 内容寻址:同哈希即同内容,绝不覆盖(需求第十九章)
            return StoredFile(key, file_hash, len(data), deduplicated=True)

        try:
            self.client.put_object(
                Bucket=self.bucket,
                Key=key,
                Body=data,
                ContentType=_content_type(extension),
                IfNoneMatch="*",
            )
            return StoredFile(key, file_hash, len(data), deduplicated=False)
        except ClientError as exc:
            code = str(exc.response.get("Error", {}).get("Code", ""))
            status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
            if code in {"PreconditionFailed", "412"} or status == 412:
                # 我们和别人同时写同一份内容,别人赢了。内容一样,直接复用。
                return StoredFile(key, file_hash, len(data), deduplicated=True)
            if code in {"NotImplemented", "InvalidRequest", "InvalidArgument"} or status == 501:
                # 端点不支持条件写。退回老路 —— 竞态窗口回来了。
                #
                # 原来这里的理由是"output_service 在删孤儿之前会查数据库引用,
                # 那一层仍然拦得住"。**那句话只覆盖了一半**:数据库里查得到的
                # 只有**已提交**的引用,而竞争对手此刻可能正拿着一个尚未提交的
                # 事务引用着同一个 key。查不到 -> 判定为孤儿 -> 删掉 ->
                # 对方随后提交成功,而它指向的对象已经不存在了。
                #
                # 所以和本地退化路径一样:不自称创建者。多留一个孤儿文件,
                # 好过删掉一个还有人用的对象。
                self.client.put_object(
                    Bucket=self.bucket,
                    Key=key,
                    Body=data,
                    ContentType=_content_type(extension),
                )
                return StoredFile(key, file_hash, len(data), deduplicated=True)
            raise

    def exists(self, storage_path: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self.client.head_object(Bucket=self.bucket, Key=storage_path)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def read(self, storage_path: str) -> bytes:
        response = self.client.get_object(Bucket=self.bucket, Key=storage_path)
        return response["Body"].read()

    def delete(self, storage_path: str) -> bool:
        """删掉一个对象。返回值是"删完之后它不在了",不是"这次调用成功了"。

        A-14:原来是 `except ClientError: return False`,把**一切**都吞成
        "没删掉" —— 403(凭据过期 / 桶策略改了)、限流、5xx 全部伪装成
        "对象不存在"。调用方(下架清理、素材删除)据此认为已经处理完,
        于是一次凭据故障的表现是"清理任务一直在跑,但什么都没清掉,
        也没有任何一条错误日志"。

        判据抄的是同文件上方的 `exists()`:只有那三个"确实不在"的错误码
        算 False,其余一律 `raise`,让调用方或告警看到真正的原因。

        (S3 对不存在的 key 通常直接返回 204,这一支主要是给
        S3 兼容实现留的 —— 它们的行为并不统一。)
        """
        from botocore.exceptions import ClientError

        try:
            self.client.delete_object(Bucket=self.bucket, Key=storage_path)
            return True
        except ClientError as exc:
            if exc.response.get("Error", {}).get("Code") in {"404", "NoSuchKey", "NotFound"}:
                return False
            raise

    def public_url(self, storage_path: str) -> str:
        if self.public_base_url:
            # 走 CDN 或自定义域名
            return f"{self.public_base_url}/{storage_path}"
        if self.endpoint_url:
            return f"{self.endpoint_url.rstrip('/')}/{self.bucket}/{storage_path}"
        return f"https://{self.bucket}.s3.amazonaws.com/{storage_path}"

    def signed_url(self, storage_path: str, *, expires_in: int = SIGNED_URL_TTL_SECONDS) -> str:
        """预签名 URL。到期之后同一个地址就打不开了。

        Bucket 本身应当配成非公开:预签名只有在"没有签名就进不去"时才有意义。
        """
        return self.client.generate_presigned_url(
            "get_object",
            Params={"Bucket": self.bucket, "Key": storage_path},
            ExpiresIn=max(1, expires_in),
        )

    def healthcheck(self) -> None:
        """真的问一次 Bucket。

        以前 readiness 无论后端是什么都只检查本地目录,于是 S3 凭据过期、
        Bucket 被删、网络不通时,实例照样报告 storage=ok 并留在轮转里 ——
        每一次上传都失败,而看板全绿。
        """
        self.client.head_bucket(Bucket=self.bucket)


def _content_type(extension: str) -> str:
    ext = extension.lower().lstrip(".")
    return {
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "png": "image/png",
        "webp": "image/webp",
    }.get(ext, "application/octet-stream")


def build_storage(
    backend: str,
    base_dir: str | Path,
    public_base_url: str,
    api_prefix: str = "/api",
) -> ObjectStorage:
    """按后端名造一个存储实现。**配置由调用方读好传进来。**

    `api_prefix` 给默认值是为了让纯测试与脚本能一行造出一个可用实例;
    生产路径一律显式传 `settings.API_PREFIX`,否则改了前缀之后签出来的
    素材地址会整体指向一个不存在的路径。
    """
    if backend == "local":
        # C-12:**生产误用本地存储要当场炸,不能安静地跑起来。**
        #
        # 本地存储把字节写进容器里的一个目录。生产上这意味着:换一台机器
        # 或者重建一次容器,所有已上架的素材和已生成的导出文件**全部消失**,
        # 而库里那些行还好好地指着它们。表现是界面正常、图片全裂,
        # 而且是在重建之后才出现,没有人会把它和存储配置联系起来。
        #
        # 只认明确的 `production/prod`(见 `settings.is_production` 的说明),
        # 不做"除了 local 都算生产"的推断。
        try:
            from app.core.config import settings as _settings
        except ImportError:  # 被裁剪过的运行环境:读不到配置就不做这个判断
            pass
        else:
            if _settings.is_production:
                raise ConfigError(
                    "生产环境不允许使用本地文件存储(STORAGE_BACKEND=local):"
                    "容器重建会丢掉全部素材与导出文件,而数据库里的引用还在。"
                    "请配置 S3 兼容存储,或把 APP_ENV 改成非生产值。"
                )
        return LocalObjectStorage(base_dir, public_base_url, api_prefix)
    if backend == "s3":
        # 配置在这里读而不是在类里读:类保持可注入,便于测试
        try:
            from app.core.config import settings

            return S3ObjectStorage(
                bucket=settings.S3_BUCKET,
                endpoint_url=settings.S3_ENDPOINT_URL,
                access_key=settings.S3_ACCESS_KEY_ID,
                secret_key=settings.S3_SECRET_ACCESS_KEY,
                region=settings.S3_REGION,
                public_base_url=settings.S3_PUBLIC_BASE_URL or public_base_url,
            )
        except ImportError:  # 被裁剪过的运行环境
            raise ValueError("S3 存储需要可用的 app.core.config") from None
    raise ValueError(f"未知存储后端:{backend}")
