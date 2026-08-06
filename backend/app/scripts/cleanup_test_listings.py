"""人工测试的清理预案命令行(方案 v4.1 任务 16 / 4.1 节 H / 10.7 节)。

三个子命令,和 `cleanup_service` 里那三个问题一一对应:

    inventory   列清单:本轮测试在平台上创建了哪些外部商品
    verify      核对:逐行问平台,本地记的和平台上的对不对得上
    delist      下架:把还占着位置的商品排进 DELIST 队列

## 用法

    python -m app.scripts.cleanup_test_listings inventory --tag uat-2026-08
    python -m app.scripts.cleanup_test_listings verify    --tag uat-2026-08
    python -m app.scripts.cleanup_test_listings delist    --tag uat-2026-08          # 只看
    python -m app.scripts.cleanup_test_listings delist    --tag uat-2026-08 --apply  # 真排队

或走 Makefile:`make cleanup TAG=uat-2026-08`,加 `APPLY=1` 才真的排队。

## 两道护栏

**一、必须给作用域。** `--tag` / `--shop` / `--channel` 至少一个,由
`cleanup_service._require_scope()` 强制。不限定的话这次调用会覆盖全部渠道的
全部商品,而在平台上下架通常撤不回来。

**二、默认只看。** `delist` 不加 `--apply` 只打印计划。这与
`requeue_stranded.py` 的 `--apply` 是同一个约定 —— 一个会改外部世界的脚本,
默认行为必须是无害的,而不是靠使用者记得加参数来避免事故。

排队之后真正的下架请求由 `publish.deliver_outbox` 那个 beat 任务发出(或
`publish_service.run_due`)。这个脚本**不发请求**:它跑在运维的终端上,
而一个会在终端里直接调用外部 API 的脚本,中途 Ctrl-C 的后果没人说得清。
"""
from __future__ import annotations

import argparse
import json
import sys

from app.db.session import SessionLocal
from app.services import cleanup_service


def _scope(args: argparse.Namespace) -> dict:
    return {
        "test_batch_tag": args.tag,
        "shop_id": args.shop,
        "channel": args.channel,
        "include_simulated": not args.real_only,
    }


def _print(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, indent=2))


def _summary(report: cleanup_service.CleanupReport) -> None:
    """人先看这几行,再决定要不要读 JSON。

    `clean` 单独打一行是因为它是 4.1 节 H 真正要的那个结论;埋在 JSON 里
    就得靠人自己去数,而人会数错。
    """
    print(
        f"共 {report.total} 行(真实 {report.real} / 模拟 {report.simulated})",
        file=sys.stderr,
    )
    print(f"仍占着平台位置:{report.occupying}", file=sys.stderr)
    if report.not_delistable:
        print(
            f"其中 {report.not_delistable} 行自动下架处理不了,需人工去平台后台",
            file=sys.stderr,
        )
    # **这一行才回答「按下 --apply 会发生什么」**。上面两个数只算真实商品,
    # 而 `run_delist` 是按全部行排的(含 SIM-)。两个口径都打出来,
    # 不要让人自己去推 —— 推错的代价是排出去的量比看到的多一个数量级
    if report.to_queue or report.to_skip:
        print(
            f"若执行:排队 {report.to_queue} 行、跳过 {report.to_skip} 行(含模拟行)",
            file=sys.stderr,
        )
    print(f"清理干净:{'是' if report.clean else '否'}", file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="cleanup_test_listings",
        description="人工测试清理预案:列清单 / 核对 / 下架(方案 4.1 节 H)",
    )
    parser.add_argument("command", choices=("inventory", "verify", "delist"))
    parser.add_argument("--tag", default=None, help="测试批次号")
    parser.add_argument("--shop", default=None, help="店铺 ID")
    parser.add_argument("--channel", default=None, help="渠道代号")
    parser.add_argument(
        "--real-only",
        action="store_true",
        help="排除 SIM- 开头的模拟商品(去平台后台核对时用这个)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="delist 专用:真的排队。不加只打印计划",
    )
    parser.add_argument("--actor", default="ops:cleanup", help="审计里记谁做的")
    args = parser.parse_args(argv)

    scope = _scope(args)

    if args.command == "verify":
        # 核对要逐行问平台,而每一次问都是一段自己的事务 —— 所以这里传的是
        # 会话工厂,不是会话(同 `run_due` / `poll_due`)
        report = cleanup_service.verify(SessionLocal, **scope)
        _print(report.as_dict())
        _summary(report)
        return 0 if report.clean else 1

    session = SessionLocal()
    try:
        if args.command == "inventory":
            report = cleanup_service.inventory(session, **scope)
            _print(report.as_dict())
            _summary(report)
            return 0

        # delist
        if not args.apply:
            report = cleanup_service.plan_delist(session, **scope)
            _print(report.as_dict())
            _summary(report)
            print("\n以上只是计划。加 --apply 才会真的排队。", file=sys.stderr)
            return 0

        result = cleanup_service.run_delist(session, actor=args.actor, **scope)
        session.commit()
        _print(result)
        print(
            f"\n已排队 {len(result['queued'])} 件,跳过 {len(result['skipped'])} 件。\n"
            "真正的下架请求由 publish.deliver_outbox 发出;"
            "发完之后再跑一次 verify 确认。",
            file=sys.stderr,
        )
        return 0
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
