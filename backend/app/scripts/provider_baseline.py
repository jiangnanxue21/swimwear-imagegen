"""Provider 基线对比(需求第二十一章阶段 5:与 Mock 基线用相同样本比较)。

用同一个商品、同一批素材、同一个模特模板,分别跑 Mock 和目标 Provider,
把两边的分档、维度分、轮次、耗时、额度消耗并排打出来。

**为什么需要它**:接入一家新 Provider 时,唯一有意义的问题是
"它比现在这条基线好在哪、差在哪"。没有对照就只能凭感觉说"看起来还行"。

用法::

    # 只跑 Mock,确认样本和流水线本身没问题
    docker compose exec backend python -m app.scripts.provider_baseline --sku SW-001-BLK-S

    # 对比 Mock 与 FASHN(需要 FASHN_API_KEY,会真实消耗额度)
    docker compose exec backend python -m app.scripts.provider_baseline \\
        --sku SW-001-BLK-S --providers mock,fashn --mode virtual_try_on

注意:真实 Provider 会**产生费用**。脚本在提交前会打印将要花掉几次调用,
并要求 --yes 才继续。
"""
from __future__ import annotations

import argparse
import sys
import time
from typing import Any

from sqlalchemy import select

from app.core.enums import TaskStatus
from app.db.session import SessionLocal
from app.models.evaluation import CandidateEvaluation
from app.models.generation import GenerationTask
from app.models.product import Product
from app.models.product_asset import ProductAsset
from app.providers.registry import get_provider
from app.services import generation_service as gs

#: 已经跑完、不会再变的状态
TERMINAL = {
    TaskStatus.AUTO_APPROVED.value,
    TaskStatus.AUTO_REJECTED.value,
    TaskStatus.MANUAL_REVIEW.value,
    TaskStatus.MANUALLY_APPROVED.value,
    TaskStatus.MANUALLY_REJECTED.value,
    TaskStatus.COMPLETED.value,
    TaskStatus.FAILED.value,
    TaskStatus.CANCELLED.value,
}


def _product(session, sku: str) -> Product:
    product = session.scalar(select(Product).where(Product.sku == sku))
    if product is None:
        raise SystemExit(f"找不到 SKU {sku};先跑 `make seed` 导入示例数据")
    return product


def _assets(session, product_id) -> list[ProductAsset]:
    return list(
        session.scalars(select(ProductAsset).where(ProductAsset.product_id == product_id))
    )


def _launch(session, *, product, assets, provider: str, mode: str, candidates: int,
            max_rounds: int, seed: int) -> GenerationTask:
    """用完全相同的输入创建任务。seed 固定,让两边的差异只来自 Provider 本身。

    ## `override_plan=True` 不是可选项,是这个脚本成立的前提(a48)

    a47 之后 `create_task` 会用**方案**的 provider / 模特模板 / 一轮张数 /
    场景姿势覆盖调用方传的值。对线上出图那是修复;对这个脚本是致命的 ——
    只要这只 SKU 所属 SPU 有一份 ACTIVE 方案,两条腿的 `provider=` 会**双双**
    被换成方案里那一个,而脚本照旧打印一张对比表。**不报错,答案是假的**,
    而且假在「上面写着 mock、下面写着 fashn」的那一栏里。

    `LOCAL_MANUAL_TEST.md` §4.6 的 §5 验收第一步正是「给一个 SPU 配一份
    ACTIVE 方案」,所以这不是一个要等很久才会撞上的场景:照着文档走一遍,
    `make baseline` 就废了。

    绕过方案**不绕过预算**(`_assert_budget` 排在 `plan_applied` 判定之前),
    也不绕过 §10.5 / §11 那两道闸 —— 它只让「参数由调用方给」这件事重新成立,
    而那正是基线对比唯一需要的东西。绕过之后任务的 `generation_plan_id` 与
    `plan_fingerprint` 两列留空,这是对的:这批图确实不是按哪份方案出的。

    这里能直接绕,是因为走的是 service 层;HTTP 那一侧 `override_plan` 是
    管理员专属(非管理员 403)。基线脚本本来就只有能进库的人跑得动。
    """
    task, _ = gs.create_task(
        session,
        product_id=product.id,
        mode=mode,
        provider=provider,
        routing_mode="MANUAL",
        input_asset_ids=[a.id for a in assets],
        model_template_id=None,
        candidate_count=candidates,
        max_rounds=max_rounds,
        base_seed=seed,
        # 幂等键必须区分 Provider,否则第二个任务会被判成重复
        idempotency_key=f"baseline-{product.sku}-{provider}-{seed}",
        override_plan=True,
        actor="baseline-script",
    )
    # 和线上路径一样走 Outbox:基准脚本要测的就是真实链路,
    # 自己另开一条直连队列的近路,测出来的就不是线上会发生的事。
    from app.core.enums import DispatchReason
    from app.services import dispatch_service

    dispatch_service.enqueue(session, task.id, reason=DispatchReason.CREATE.value)
    session.commit()
    dispatch_service.deliver_pending(session, task.id)
    return task


def _wait(session, task_id, timeout: float) -> GenerationTask:
    deadline = time.monotonic() + timeout
    while True:
        session.expire_all()
        task = session.get(GenerationTask, task_id)
        if task is not None and task.status in TERMINAL:
            return task
        if time.monotonic() > deadline:
            print(f"  ! 等待超时({timeout:.0f}s),当前状态 {task.status if task else '未知'}")
            return task
        time.sleep(2)


def _summarise(session, task: GenerationTask) -> dict[str, Any]:
    rows = list(
        session.scalars(
            select(CandidateEvaluation).where(CandidateEvaluation.task_id == task.id)
        )
    )
    full = [r for r in rows if r.depth == "FULL"]
    grades: dict[str, int] = {}
    for row in rows:
        grades[row.grade] = grades.get(row.grade, 0) + 1

    best = max(full, key=lambda r: float(r.overall_score), default=None)
    duration = None
    if task.started_at and task.finished_at:
        duration = (task.finished_at - task.started_at).total_seconds()

    return {
        "provider": task.provider,
        "status": task.status,
        "rounds": task.current_round,
        "evaluated": len(rows),
        "grades": grades,
        "best_score": float(best.overall_score) if best else None,
        "best_grade": best.grade if best else None,
        "dimensions": dict(best.scores) if best else {},
        "hard_fails": sorted({c for r in rows for c in (r.hard_fail_codes or [])}),
        "seconds": duration,
        "error": task.error_code,
    }


def _print_table(results: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 78)
    print(f"{'Provider':<12}{'状态':<18}{'轮次':<6}{'最佳':<10}{'分档分布':<18}{'耗时':<8}")
    print("-" * 78)
    for r in results:
        grades = " ".join(f"{k}×{v}" for k, v in sorted(r["grades"].items())) or "—"
        score = round(r["best_score"]) if r["best_score"] is not None else "—"
        best = f"{r['best_grade'] or '—'} {score}"
        seconds = f"{r['seconds']:.0f}s" if r["seconds"] else "—"
        print(
            f"{r['provider']:<12}{r['status']:<18}{r['rounds']:<6}"
            f"{best:<10}{grades:<18}{seconds:<8}"
        )

    baseline = next((r for r in results if r["provider"] == "mock"), None)
    for r in results:
        if baseline is None or r is baseline or not r["dimensions"]:
            continue
        print(f"\n--- {r['provider']} vs mock 维度差 ---")
        for dim, score in sorted(r["dimensions"].items()):
            base = baseline["dimensions"].get(dim)
            if base is None:
                continue
            delta = score - base
            arrow = "↑" if delta > 0 else ("↓" if delta < 0 else "=")
            print(f"  {dim:<24}{base:>6.0f} -> {score:>6.0f}  {arrow}{abs(delta):>5.0f}")

    for r in results:
        if r["hard_fails"]:
            print(f"\n{r['provider']} 出现的硬错误:{', '.join(r['hard_fails'])}")
        if r["error"]:
            print(f"{r['provider']} 失败:{r['error']}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Provider 基线对比")
    parser.add_argument("--sku", required=True, help="用哪个商品的素材")
    parser.add_argument("--providers", default="mock", help="逗号分隔,例如 mock,fashn")
    parser.add_argument("--mode", default="virtual_try_on",
                        choices=["virtual_try_on", "product_to_model"])
    parser.add_argument("--candidates", type=int, default=4)
    parser.add_argument("--max-rounds", type=int, default=1,
                        help="对比时建议 1 轮:多轮重生会把 Provider 的差异混进重生策略里")
    parser.add_argument("--seed", type=int, default=20240101)
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--yes", action="store_true", help="确认真实 Provider 会产生费用")
    args = parser.parse_args()

    names = [n.strip() for n in args.providers.split(",") if n.strip()]
    paid = [n for n in names if n != "mock"]
    if paid and not args.yes:
        print(f"将调用付费 Provider {', '.join(paid)},每家约 {args.candidates} 张候选图。")
        print("确认后加 --yes 重新执行。")
        return 1

    session = SessionLocal()
    try:
        product = _product(session, args.sku)
        assets = _assets(session, product.id)
        if not assets:
            raise SystemExit(f"{args.sku} 没有素材")

        print(f"样本:{product.sku} · {product.name} · {len(assets)} 张素材")
        print(f"参数:mode={args.mode} candidates={args.candidates} "
              f"rounds={args.max_rounds} seed={args.seed}")

        results = []
        for name in names:
            provider = get_provider(name)
            if not provider.is_configured():
                print(f"\n跳过 {name}:未配置")
                continue
            print(f"\n>>> {name} 提交中...")
            task = _launch(
                session, product=product, assets=assets, provider=name, mode=args.mode,
                candidates=args.candidates, max_rounds=args.max_rounds, seed=args.seed,
            )
            task = _wait(session, task.id, args.timeout)
            results.append(_summarise(session, task))

        if results:
            _print_table(results)
        else:
            print("没有可用的 Provider")
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
