"""发布幂等键推导(方案 v4.1 第 7.4 节)。

## 为什么不复用 `workflows/idempotency.py`

那一个回答的是「这次**生成**要不要重跑」,输入是商品、素材、模板、prompt ——
判据是「改了它,出来的图会不一样吗」。

这一个回答的是「这次**提交**是不是平台已经收过的那一次」,输入是渠道、店铺、
站点、操作类型、外部商品 ID。两者的输入集合几乎不相交,而且失效后果完全不同:
生成键算错了,多花一次钱;发布键算错了,平台上多出一个商品,或者一次更新被
当成重复请求丢掉。合并成一个函数意味着以后任何一边加字段都要重新论证另一边。

## 键里必须有 operation

`core/enums.py` 的 `PublishOperation` 注释已经写明了原因,这里再钉一次,因为
它是本模块唯一一条「漏了不会报错、只会在生产上出事」的规则:

    v1 的幂等键不含操作类型 → CREATE 和 UPDATE 算出同一个键
      → 要么更新被当成重复创建吞掉(改了价格,平台上还是旧的)
      → 要么创建被当成更新放过去(平台上没有这个商品,更新报 404)

所以 `operation` 不给默认值 —— 调用方必须显式写出这次是在做什么。

## 为什么 external_listing_id 也进键

同一份草稿在同一个店铺可能被上架两次(第一次下架了,后来又重新上)。
第二次的 `external_listing_id` 不同,那就是两次不同的提交,不该互相幂等掉。
第一次创建时它是空的,空值参与哈希不影响 —— 那时本来就还没有外部 ID。
"""
from __future__ import annotations

import hashlib
import json

#: 键的可读前缀。落库后靠它一眼区分发布键与生成键 —— 两者都会出现在
#: 审计日志里,而 `auto-xxxx` 和 `pub-xxxx` 混在一起时没人分得清哪个是哪个。
KEY_PREFIX = "pub-"

#: 摘要截断长度。48 个十六进制字符 = 192 位,碰撞概率远低于「两个不同提交
#: 被算成同一次」这件事在业务上可接受的门槛;加上前缀共 52 字符,
#: 留在 `String(128)` 列里有充足余量。
_DIGEST_CHARS = 48


def build_publish_idempotency_key(
    *,
    channel: str,
    shop_id: str,
    site: str,
    operation: str,
    product_id: str,
    draft_fingerprint: str,
    spec_checksum: str = "",
    external_listing_id: str | None = None,
    explicit_key: str | None = None,
) -> str:
    """按 7.4 节的字段集合算出这次提交的幂等键。

    参数全部 keyword-only:这八个字段里有五个是字符串,位置传参一旦顺序写错
    ——比如 channel 和 site 调了个个儿——算出来的键完全合法,只是和上一次
    不一样,而幂等失效是静默的。

    `explicit_key` 是给「平台自己发的请求 ID」这类场景留的口子,原样使用。
    """
    if explicit_key and explicit_key.strip():
        return explicit_key.strip()[:128]

    if not str(operation or "").strip():
        raise ValueError(
            "幂等键必须包含 operation。缺了它,CREATE 与 UPDATE 会算出同一个键 —— "
            "见 core/enums.py::PublishOperation 的注释。"
        )

    payload = {
        "channel": str(channel).strip().lower(),
        "shop_id": str(shop_id).strip(),
        "site": str(site).strip().upper(),
        "operation": str(operation).strip().upper(),
        "product_id": str(product_id).strip(),
        "draft_fingerprint": str(draft_fingerprint).strip(),
        "spec_checksum": str(spec_checksum or "").strip(),
        "external_listing_id": str(external_listing_id or "").strip(),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
    return KEY_PREFIX + digest


def build_request_fingerprint(payload: dict | list | str) -> str:
    """请求体指纹。用来回答「这次重试发出去的,和上次是不是同一份内容」。

    和幂等键的区别值得写清楚,否则下一个人会以为二者重复:

        幂等键     —— 提交前算,决定「要不要发」
        请求指纹   —— 发出后存,决定「发出去的是什么」

    超时重试时两者都要:键相同说明是同一次逻辑提交,指纹相同才说明平台
    收到的确实是同一份 payload。键相同而指纹不同,意味着中间有人改了草稿,
    那是 `SUBMIT_RESULT_UNKNOWN` 之外的另一种需要人工介入的情况。
    """
    if isinstance(payload, str):
        canonical = payload
    else:
        canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:_DIGEST_CHARS]
