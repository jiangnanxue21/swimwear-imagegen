"""删掉再传回来:同一张图要不要复活(A52)。**纯判定,零三方依赖。**

## 这条路径以前是死胡同

删除是**软删**(`media/service.delete_asset`):行还在,只是 `status=DELETED`。
而去重键(`_dedupe_hit`)只看 `(spu, 颜色, sha256)`,**不看状态**;
老的 `ProductAsset` 那条记录压根没有软删这回事。两条合起来:

    删掉一张素材 -> 再传同一份字节
    -> `asset_service._find_same` 在 ProductAsset 上命中,直接 return
    -> 界面说「这张图已经在该商品下了,沿用已有素材」
    -> 而素材列表里一张图都不多

运营看到的是一句"成功"和一个没有变化的页面,**而且没有任何出路** ——
同一张图从此再也传不上来。`delete_asset` 的文档写着「误删的补救是重新上传
或重新出图」,那句承诺在这之前是假的;这个模块是让它变真的那一半。

## 为什么复活是对的,而不是"重传应该报错"

删除的语义是「这张是垃圾,拿走」。一个人**重新拿到那份字节又传了一次**,
是在推翻自己之前那个判断。`delete_asset` 不给 restore 接口,是为了不让删除
退化成第二个隔离(两颗看起来一样、语义不同的按钮,运营分辨不了该用哪个)——
而这条路径不是一颗按钮:它要求重新持有原文件,并且会留下一次新的上传审计。
两者不冲突。

## 为什么单独成模块

`media/service.py` 顶层 import sqlalchemy,而这条判定要能被穷举:
「哪些状态该复活」是一个五选一的全称命题,漏一格的表现是一次重复上传
把人工隔离冲成 READY。与 `provenance_conflict` 同一个理由、同一个位置。
"""
from __future__ import annotations

from app.core.enums import MediaStatus


class _Revivable:
    """这条判定读写的两列。协议而非实现 —— 真调用方递进来的是 ORM 行。"""

    status: str
    quarantine_reason: str | None


def revive_if_deleted(asset: _Revivable, *, status: MediaStatus) -> bool:
    """已删除的素材被重新上传时复活它。返回「有没有真的改」。

    ## 只对 DELETED 生效

    去重命中时不覆盖任何字段是 `ingest()` 的既有铁律:角色可能是人工改过的,
    状态可能是人工隔离的,让一次重复推送把它们冲掉是最难查的一类数据丢失。
    复活是那条铁律唯一的例外,所以它的适用面必须**刚好一格** ——
    放宽到 QUARANTINED 的话,重复上传就成了一条绕过人工复核的路。

    ## 状态取本次上传算出来的那个

    重新上传会重新跑一次检查,`status` 是这次检查的结论。沿用删除前的旧状态
    等于让一次新的检查结果被历史盖掉:一张当初没过检查、被隔离、随后删掉的图,
    重新传一份修好的字节,应该是 READY。
    """
    if asset.status != MediaStatus.DELETED.value:
        return False
    asset.status = status.value
    # 隔离理由是上一次留下的。复活成非隔离态时必须清掉 —— 一条 READY 的素材
    # 挂着一句说不清来路的隔离说明,比没有说明更难查。
    # 复活成隔离态时留着不动:调用方紧接着会写这次检查的那一句。
    if status is not MediaStatus.QUARANTINED:
        asset.quarantine_reason = None
    return True
