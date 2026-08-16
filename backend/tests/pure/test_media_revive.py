"""删掉再传回来:同一张图必须能复活(A52)。

## 这条路径以前是死胡同

删除是软删,`MediaAsset` 那行还在;而去重键(`_dedupe_hit`)不看状态。
`ProductAsset` 那条老记录压根不软删。两条合起来的结果是:

    删除一张素材 -> 再传同一份字节
    -> `asset_service._find_same` 命中 -> 直接 return,影子写都没跑
    -> 界面说「这张图已经在该商品下了,沿用已有素材」
    -> 而素材列表里一张图都不多

运营看到的是一句"成功"和一个没有变化的页面,**而且没有任何出路**:
同一张图从此再也传不上来。`delete_asset` 的文档写着「误删的补救是重新上传
或重新出图」,那句承诺在这之前是假的。

## 为什么这批断言不连库

`media/revive.py` 是纯判定:输入是一行素材的状态与这次上传算出来的状态,
输出是"要不要改、改成什么"。它不需要 Session,所以能在 tests/pure 里穷举。
接线那一半(去重命中时也跑影子写)由源码守卫钉住 —— 真库用例欠着,
如实记在交付说明里。
"""
from __future__ import annotations

import ast
from pathlib import Path

from app.core.enums import MediaStatus
from app.media.revive import revive_if_deleted

BACKEND = Path(__file__).resolve().parents[2]
ASSET_SERVICE = BACKEND / "app" / "services" / "asset_service.py"


class _Row:
    """只带这条判定读写的两列。用真 ORM 要 sqlalchemy,而这批用例零三方依赖。"""

    def __init__(self, status: str, quarantine_reason: str | None = None) -> None:
        self.status = status
        self.quarantine_reason = quarantine_reason


def test_a_deleted_asset_comes_back_with_this_uploads_verdict():
    """复活后的状态是**这次上传**算出来的,不是删除前的那个。

    沿用旧状态等于让一次新的检查结果被历史盖掉:一张当初因为没过检查而
    被隔离、后来被删掉的图,重新传一份修好的字节,应该是 READY。
    """
    row = _Row(MediaStatus.DELETED.value, quarantine_reason="上传检查未通过")
    assert revive_if_deleted(row, status=MediaStatus.READY) is True
    assert row.status == MediaStatus.READY.value
    # 旧的隔离理由必须清掉:一条 READY 的素材挂着一句说不清来路的隔离说明,
    # 比没有说明更难查
    assert row.quarantine_reason is None


def test_reviving_into_quarantine_keeps_room_for_the_new_reason():
    """复活成隔离态时不清理由 —— 调用方随后会写这次检查的那一句。"""
    row = _Row(MediaStatus.DELETED.value, quarantine_reason="旧的")
    assert revive_if_deleted(row, status=MediaStatus.QUARANTINED) is True
    assert row.status == MediaStatus.QUARANTINED.value


def test_a_live_asset_is_never_touched_by_a_duplicate_upload():
    """没删过的行,重复上传一个字段都不许动。

    去重命中时不覆盖是 `ingest` 的既有铁律(角色可能是人工改过的,状态可能
    是人工隔离的)。复活这条新分支**只对 DELETED 生效**,别的一律放过 ——
    否则一次重复上传就能把人工隔离冲成 READY。
    """
    for status in (
        MediaStatus.READY,
        MediaStatus.PENDING,
        MediaStatus.QUARANTINED,
        MediaStatus.FAILED,
    ):
        row = _Row(status.value, quarantine_reason="人工隔离:疑似水印")
        assert revive_if_deleted(row, status=MediaStatus.READY) is False
        assert row.status == status.value, status
        assert row.quarantine_reason == "人工隔离:疑似水印", status


def test_every_media_status_is_covered_by_exactly_one_branch():
    """穷举:枚举里每一个状态都被上面两条覆盖到,加一个取值会漏进来。"""
    revived = {
        s for s in MediaStatus
        if revive_if_deleted(_Row(s.value), status=MediaStatus.READY)
    }
    assert revived == {MediaStatus.DELETED}, (
        f"复活分支覆盖到了不该覆盖的状态:{sorted(s.value for s in revived)}"
    )


def test_the_dedupe_shortcut_still_runs_the_shadow_write():
    """去重命中那一支**必须**也跑影子写,否则复活永远轮不到发生。

    这是整条修复里唯一没法用纯判定表达的一跳:`asset_service.upload_asset`
    在 `ProductAsset` 上命中去重时,以前直接 return —— 素材侧那行 DELETED
    的记录连碰都没碰到。判定写得再对也没用,因为没有人调它。

    锚在 AST 上而不是字符串上:`if existing is not None:` 那个分支里必须
    真的有一次 `_shadow(...)` 调用。
    """
    tree = ast.parse(ASSET_SERVICE.read_text(encoding="utf-8"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "upload_asset"
    )
    dedupe_branches = [
        n for n in ast.walk(fn)
        if isinstance(n, ast.If)
        and ast.unparse(n.test) == "existing is not None"
    ]
    assert len(dedupe_branches) == 1, "去重命中的分支不止一处或已改名"
    calls = {
        ast.unparse(n.func)
        for n in ast.walk(dedupe_branches[0])
        if isinstance(n, ast.Call)
    }
    assert "_shadow" in calls, (
        "去重命中时没有跑影子写 —— 删掉再传回来又会变回死胡同"
    )
