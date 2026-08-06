"""回收被作废的批量导出对象(C-17 的入口)。

    python -m tools.purge_exports                 # 干跑,只报告
    python -m tools.purge_exports --apply
    python -m tools.purge_exports --days 30 --apply

## 为什么这个文件必须存在

`cleanup_service.purge_superseded_exports()` 在第六批就写好了,**但没有任何
地方调用它** —— 一个实现完整、无人可达的函数。

这恰恰是评审里 A-21 和 A-26 的形状:409 提示"请重新生成"而全仓没有重新生成
的入口;覆盖横幅说"3 个变体绑定了 0 个"而界面上无处可点。第七批的自查里
`grep` 到它 0 处引用时,这条修复实际上和没做是一样的。

所以入口和实现同批交付,不留"下次接上"。

## 默认干跑

不带 `--apply` 只打印将要删除的对象。删除是不可逆的,而这批对象的存在理由
(重新生成之后留一份对照物)本身就说明"万一还要用"是真实场景。
"""
from __future__ import annotations

import argparse
import sys

from app.core.config import settings
from app.db.session import SessionLocal
from app.services import cleanup_service
from app.services.storage import build_storage


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    parser.add_argument(
        "--days",
        type=int,
        default=cleanup_service.SUPERSEDED_EXPORT_RETENTION_DAYS,
        help="作废满这么多天的才回收(默认 %(default)s)",
    )
    parser.add_argument(
        "--apply", action="store_true", help="真的删。不带这个参数只干跑"
    )
    args = parser.parse_args(argv)

    # 参数口径抄 `review_service` / `generation_tasks` 那两处,不自己拼:
    # 少传 `API_PREFIX` 的话签出来的地址会整体指向一个不存在的路径
    storage = build_storage(
        settings.STORAGE_BACKEND,
        settings.storage_dir,
        settings.PUBLIC_BASE_URL,
        settings.API_PREFIX,
    )
    with SessionLocal() as session:
        found = cleanup_service.purge_superseded_exports(
            session, storage, older_than_days=args.days, apply=args.apply
        )
        if args.apply:
            session.commit()

    if not found:
        print(f"没有满 {args.days} 天且已无人引用的作废导出对象")
        return 0

    for entry in found:
        mark = "已删" if entry.deleted else ("失败" if entry.error else "待删")
        print(f"[{mark}] {entry.storage_path}  批次={entry.batch_id}  原因={entry.reason}")
        if entry.error:
            print(f"        {entry.error}")

    if not args.apply:
        print(f"\n干跑:{len(found)} 个对象可回收。确认无误后加 --apply")
    else:
        deleted = sum(1 for e in found if e.deleted)
        print(f"\n删除 {deleted} / {len(found)};失败的见上面的「失败」行")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
