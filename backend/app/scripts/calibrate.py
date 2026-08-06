"""评分器校准(阶段 7 之后的第一件事)。

**要回答的问题**:"什么样的图算 A 档"这条线,画得对不对?

分档逻辑本身有 26 个用例守着,确定性也验过。但那些验的都是
"给定分数,分档是否正确";没有任何东西验证过
"给定一张真实的图,分数是否合理"。后者只能靠人来校准。

好消息是数据已经在库里了 —— 这正是 A 档随机抽检存在的理由:

    抽检项       模型判 A,人复核 -> 人也认可吗?(查假阳性)
    审核队列项   模型判不达标,人复核 -> 人觉得其实能用吗?(查假阴性)

两类加起来就是一张混淆矩阵。假阳性是最贵的错误:烂图直接上了网站。

用法::

    docker compose exec backend python -m app.scripts.calibrate
    docker compose exec backend python -m app.scripts.calibrate --since 2026-06-01 --verbose

样本不够时会明说,不会用 3 个样本算出一个"准确率 100%"来糊弄人。
"""
from __future__ import annotations

import argparse
import sys
from collections import Counter
from datetime import datetime
from typing import Any

from sqlalchemy import select

from app.core.enums import Grade, ReviewReason, ReviewStatus
from app.db.session import SessionLocal
from app.evaluators.base import DIMENSION_LABELS
from app.models.evaluation import CandidateEvaluation, ReviewItem

GREEN, RED, YELLOW, DIM, RESET = "\033[32m", "\033[31m", "\033[33m", "\033[2m", "\033[0m"

#: 少于这个数就不给结论。小样本上的比例数字只会误导人
MIN_SAMPLES = 20


def _human_verdict(item: ReviewItem) -> str | None:
    """把人工决策折成二元结论:这张图到底能不能用。"""
    if item.status == ReviewStatus.APPROVED.value:
        return "USABLE"
    if item.status == ReviewStatus.REJECTED.value:
        return "UNUSABLE"
    # 要求重生 = 人也觉得不行,但还想再试;归到不可用一侧
    if item.status == ReviewStatus.REGENERATION_REQUESTED.value:
        return "UNUSABLE"
    return None  # 还没处理,不计入


def _model_verdict(item: ReviewItem) -> str | None:
    """模型对这个任务的结论。抽检项 = 模型说能用;其余进队列的 = 模型说不能用。"""
    if item.reason == ReviewReason.SPOT_CHECK.value:
        return "USABLE"
    if item.reason in (ReviewReason.ROUNDS_EXHAUSTED.value, ReviewReason.PROVIDER_EXHAUSTED.value):
        return "UNUSABLE"
    return None  # 鉴权错误之类不是画质判断,排除


def collect(session, since: datetime | None) -> list[dict[str, Any]]:
    stmt = select(ReviewItem)
    if since is not None:
        stmt = stmt.where(ReviewItem.created_at >= since)

    samples: list[dict[str, Any]] = []
    for item in session.scalars(stmt):
        human = _human_verdict(item)
        model = _model_verdict(item)
        if human is None or model is None:
            continue
        samples.append(
            {
                "review_id": str(item.id),
                "task_id": str(item.task_id),
                "human": human,
                "model": model,
                "best_grade": item.best_grade,
                "best_score": float(item.best_score) if item.best_score is not None else None,
                "tags": list(item.manual_tags or []),
                "note": item.decision_note,
            }
        )
    return samples


def confusion(samples: list[dict[str, Any]]) -> dict[str, int]:
    matrix = Counter()
    for s in samples:
        matrix[(s["model"], s["human"])] += 1
    return {
        # 模型说能用、人也说能用
        "true_positive": matrix[("USABLE", "USABLE")],
        # 模型说能用、人说不能用 —— 最贵的错误:烂图上了网站
        "false_positive": matrix[("USABLE", "UNUSABLE")],
        # 模型说不能用、人说能用 —— 浪费:本可以直接过,却烧了额外轮次
        "false_negative": matrix[("UNUSABLE", "USABLE")],
        "true_negative": matrix[("UNUSABLE", "UNUSABLE")],
    }


def score_distribution(session, since: datetime | None) -> dict[str, Any]:
    stmt = select(CandidateEvaluation).where(CandidateEvaluation.depth == "FULL")
    if since is not None:
        stmt = stmt.where(CandidateEvaluation.created_at >= since)
    rows = list(session.scalars(stmt))
    if not rows:
        return {}

    grades = Counter(r.grade for r in rows)
    dimension_means: dict[str, float] = {}
    for dimension in DIMENSION_LABELS:
        values = [float(r.scores[dimension]) for r in rows if dimension in (r.scores or {})]
        if values:
            dimension_means[dimension] = sum(values) / len(values)
    return {"total": len(rows), "grades": dict(grades), "dimension_means": dimension_means}


def report(samples: list[dict[str, Any]], distribution: dict[str, Any], verbose: bool) -> int:
    print("\n" + "=" * 66)
    print("评分器校准报告")
    print("=" * 66)

    if distribution:
        print(f"\n完整评分样本:{distribution['total']} 条")
        grades = distribution["grades"]
        total = sum(grades.values()) or 1
        print("  分档分布:", "  ".join(
            f"{g.value} {grades.get(g.value, 0)}({grades.get(g.value, 0) / total:.0%})"
            for g in Grade
        ))

        means = distribution["dimension_means"]
        if means:
            weakest = sorted(means.items(), key=lambda kv: kv[1])[:3]
            print("  最弱三个维度:", "  ".join(
                f"{DIMENSION_LABELS.get(k, k)} {v:.0f}" for k, v in weakest
            ))
            print(f"  {DIMENSION_DIM_HINT}")

    print(f"\n人工复核样本:{len(samples)} 条")
    if len(samples) < MIN_SAMPLES:
        print(f"{YELLOW}  样本不足 {MIN_SAMPLES} 条,不给准确率结论 ——{RESET}")
        print(f"{DIM}  小样本上的比例只会误导人。先让系统多跑一段时间,{RESET}")
        print(f"{DIM}  并确保运营真的在审核队列里做判定(而不是放着不管)。{RESET}\n")
        return 0

    matrix = confusion(samples)
    tp, fp, fn, tn = (
        matrix["true_positive"], matrix["false_positive"],
        matrix["false_negative"], matrix["true_negative"],
    )
    print(f"""
                    人工:能用    人工:不能用
    模型:能用        {tp:>6}        {fp:>8}   <- 假阳性,最贵的错误
    模型:不能用      {fn:>6}        {tn:>8}
""")

    agreement = (tp + tn) / max(1, len(samples))
    print(f"  一致率        {agreement:.1%}")
    if tp + fp:
        precision = tp / (tp + fp)
        print(f"  自动通过准确率 {precision:.1%}  {DIM}(模型说能用时,人也认可的比例){RESET}")
        if precision < 0.9:
            print(f"{RED}  ! 自动通过里有超过一成是人不认可的 —— A 档门槛偏松{RESET}")
            print(f"{DIM}    先调 a_min_garment_identity 与 a_min_structural_fidelity,{RESET}")
            print(f"{DIM}    它们直接对应\"这是不是同一件商品\",比调总分线更有针对性。{RESET}")
    if fn + tn:
        waste = fn / max(1, fn + tn)
        print(f"  误杀率        {waste:.1%}  {DIM}(模型说不能用,人却觉得可以的比例){RESET}")
        if waste > 0.3:
            print(f"{YELLOW}  ! 超过三成的转人工其实是可用的 —— 门槛偏严,在白烧额度{RESET}")

    if verbose:
        print("\n分歧样本:")
        for s in samples:
            if s["model"] != s["human"]:
                tags = ",".join(s["tags"]) or "无标签"
                print(f"  {s['task_id'][:8]}  模型={s['model']:<9} 人={s['human']:<9} "
                      f"{s['best_grade']} {s['best_score']}  {tags}")
                if s["note"]:
                    print(f"    {DIM}{s['note'][:100]}{RESET}")

    print(f"\n{DIM}这些数字回答的是\"分档线画得对不对\",不是\"评分器准不准\"。{RESET}")
    print(f"{DIM}线画错了调 RuleSet 阈值;评分器本身不准,得换评分器。{RESET}\n")
    return 0


DIMENSION_DIM_HINT = (
    f"{DIM}维度均分持续偏低说明的是生成质量问题,不是分档问题 —— 调阈值治标不治本。{RESET}"
)


def main() -> int:
    parser = argparse.ArgumentParser(description="评分器校准")
    parser.add_argument("--since", help="只统计这个日期之后的数据,格式 2026-06-01")
    parser.add_argument("--verbose", action="store_true", help="列出模型与人判断不一致的样本")
    args = parser.parse_args()

    since = None
    if args.since:
        try:
            since = datetime.fromisoformat(args.since)
        except ValueError:
            print(f"日期格式不对:{args.since},应为 2026-06-01")
            return 1

    session = SessionLocal()
    try:
        samples = collect(session, since)
        distribution = score_distribution(session, since)
        return report(samples, distribution, args.verbose)
    finally:
        session.close()


if __name__ == "__main__":
    sys.exit(main())
