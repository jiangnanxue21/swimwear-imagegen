"""回填 `media_assets.evidence_class`(A45-batch14-26,迁移 0045 的后半段)。

    python -m app.scripts.backfill_evidence_class              # 只看,不动
    python -m app.scripts.backfill_evidence_class --apply
    python -m app.scripts.backfill_evidence_class --apply --batch 1000

## 为什么这件事是脚本而不是迁移的一部分

**这是本脚本存在的全部理由,改它之前先读完这一段。**

迁移不许 import `app.*`(全仓 42 份没有一份破例)。所以在 0045 里回填
只剩一条路:把 `derive_evidence_class` 的四条规则冻成一段 SQL CASE。

那就是 PRD 风险表 D3 描述的**第二个判定点**。规则今天还在演进(§4.8
那张表还在长),而 SQL CASE 不会跟着演进 —— 它会安静地停在写它的那一天,
且停下来之后没有任何地方会报,回填过的行看起来完全正常。

脚本可以 import 派生函数。于是回填与运行时**走同一段代码**,
"唯一写入点"这句话在回填这条路径上也成立。

0040 逐条权衡过同一道题并拒绝了 SQL CASE,14-23 复述过一次,
本批不推翻那个结论 —— 绕开它。

## 幂等

只处理 `evidence_class IS NULL` 的行。重复跑安全,中途 Ctrl-C 之后
直接再跑,不需要记录进度。

**注意它的代价:已经算过的行不会被重算。** 派生规则改版之后想让存量
跟着走,要显式 `--recompute`,而那是一个会改动已有判定的动作 ——
它可能让一批素材退出识别输入(或者进入),所以默认关着,
且干跑时会逐类打印差异。

## 为什么不用一条 UPDATE ... FROM

派生要读五个字段并按顺序判四条规则,那是 Python 侧的事。
分批取行 → 算 → 写回,每批提交一次。理由与 `backfill_media_assets`
相同:一次几十万行的事务会长时间持锁,期间线上写入全部阻塞。
"""
from __future__ import annotations

import argparse
from collections import Counter

from sqlalchemy import func, select

from app.core.logging import get_logger, setup_logging
from app.db.session import SessionLocal
from app.media.evidence_rules import derive_evidence_class, violates_ai_check
from app.attributes.service import EVIDENCE_IMPORTED_URL_TRUSTED
from app.models.media_asset import MediaAsset

logger = get_logger(__name__)

#: 与 `backfill_media_assets` 同一个数量级。太小则提交开销占比过高,
#: 太大则单次持锁时间过长。
DEFAULT_BATCH = 500


def main() -> int:
    parser = argparse.ArgumentParser(description="回填 media_assets.evidence_class")
    parser.add_argument("--apply", action="store_true", help="真的写入;不加只统计")
    parser.add_argument("--batch", type=int, default=DEFAULT_BATCH, help="每批提交行数")
    parser.add_argument(
        "--recompute",
        action="store_true",
        help="连已经算过的行一起重算(规则改版后用)。会改动已有判定,默认关",
    )
    args = parser.parse_args()

    setup_logging("INFO")
    session = SessionLocal()
    try:
        _print_coverage(session, "回填前")

        if not args.apply:
            preview = _preview(session, recompute=args.recompute)
            print("\n将要写入的分布:")
            for cls, count in sorted(preview["to_write"].items()):
                print(f"  {cls:<20} {count}")
            if args.recompute and preview["changes"]:
                print("\n**重算会改动已有判定**:")
                for (old, new), count in sorted(preview["changes"].items()):
                    print(f"  {old} → {new}  {count} 行")
            print("\n只是看看。要真的执行,加 --apply")
            return 0

        written = _backfill(session, batch=args.batch, recompute=args.recompute)
        print(f"\n回填完成:写入 {written} 行")
        _print_coverage(session, "回填后")

        remaining = _null_count(session)
        if remaining:
            # 不该发生:派生函数对任何输入都返回一个枚举值,没有"算不出来"这一路。
            # 真发生了说明有行在本次扫描之后新增,而新增路径本该在落库前就算好。
            print(f"\n⚠ 仍有 {remaining} 行为 NULL —— 写入侧可能有绕过派生函数的路径")
            return 1
        return 0
    finally:
        session.close()


def _null_count(session) -> int:
    return session.scalar(
        select(func.count()).select_from(MediaAsset).where(
            MediaAsset.evidence_class.is_(None)
        )
    ) or 0


def _print_coverage(session, label: str) -> None:
    total = session.scalar(select(func.count()).select_from(MediaAsset)) or 0
    filled = total - _null_count(session)
    print(f"\n{label}:  {filled}/{total} 已有 evidence_class")


def _rows_to_process(session, *, recompute: bool):
    stmt = select(MediaAsset)
    if not recompute:
        stmt = stmt.where(MediaAsset.evidence_class.is_(None))
    return stmt


def _derive_for(asset: MediaAsset) -> str:
    """对一行素材调派生函数,**只用于干跑预览与统计**。

    真正的写入路径不经过这里,见 `_backfill` 里那段注释 ——
    那里的赋值点必须字面上就是派生调用。

    参数逐个显式传,不用 `**vars(asset)` —— 后者会在模型加列时把无关字段
    喂进去,而派生函数的签名是关键字限定的,多一个参数当场 TypeError。
    那种炸法比静默传错好,但显式传参连炸都不用。
    """
    return derive_evidence_class(
        source=asset.source,
        generation_task_id=asset.generation_task_id,
        generation_candidate_id=asset.generation_candidate_id,
        role=asset.role,
        legacy_asset_type=getattr(asset, "legacy_asset_type", None),
        imported_url_trusted=EVIDENCE_IMPORTED_URL_TRUSTED,
    ).value


def _preview(session, *, recompute: bool) -> dict:
    to_write: Counter = Counter()
    changes: Counter = Counter()
    for asset in session.scalars(_rows_to_process(session, recompute=recompute)):
        new = _derive_for(asset)
        to_write[new] += 1
        if asset.evidence_class is not None and asset.evidence_class != new:
            changes[(asset.evidence_class, new)] += 1
    return {"to_write": to_write, "changes": changes}


def _backfill(session, *, batch: int, recompute: bool) -> int:
    written = 0
    pending = 0
    for asset in session.scalars(
        _rows_to_process(session, recompute=recompute).execution_options(yield_per=batch)
    ):
        # **赋值点本身就是派生调用,这是刻意的写法,不要"整理"成
        # `x = _derive_for(asset)` 再 `asset.evidence_class = x`。**
        #
        # `test_a45_batch14_7_evidence_class.py::
        #  test_nobody_outside_the_rules_module_computes_an_evidence_class`
        # 按 AST 判「赋给 evidence_class 的值是不是 derive_evidence_class(...)
        # 的返回」,而它的 docstring 逐字写着禁的是"路由层/**脚本**直写"。
        # 经过一个本地变量之后,在赋值点上**看不出** `x` 是派生来的还是
        # 谁手搓的一段 if —— 那正是这条守卫要抓的形状。本批第一版就是
        # 那么写的,守卫当场红,而当时第一反应是"守卫太严"。它不严。
        #
        # 不写 `.value`:`EvidenceClass` 是 StrEnum,本身就是 str,String(24)
        # 列直接收得下。加上 `.value` 会让表达式从 Call 变成 Attribute,
        # 守卫又红 —— 那时正确的做法仍然不是改守卫。
        asset.evidence_class = derive_evidence_class(
            source=asset.source,
            generation_task_id=asset.generation_task_id,
            generation_candidate_id=asset.generation_candidate_id,
            role=asset.role,
            legacy_asset_type=getattr(asset, "legacy_asset_type", None),
            imported_url_trusted=EVIDENCE_IMPORTED_URL_TRUSTED,
        )

        # 库级 CHECK 的 Python 孪生。派生函数**证明过**产不出违规组合
        # (纯层穷举),所以这里命中意味着派生函数被改坏了 —— 那时该停,
        # 不该让一批违规值撞到 CHECK 上,再以 IntegrityError 的形式出现在
        # 第 400 行(那时前 399 行已经提交)。
        # 赋值在前、判定在后:回滚会把这一行的赋值一起撤掉,不留痕。
        if violates_ai_check(
            evidence_class=asset.evidence_class,
            source=asset.source,
            generation_task_id=asset.generation_task_id,
            generation_candidate_id=asset.generation_candidate_id,
        ):
            bad = asset.evidence_class
            session.rollback()
            raise RuntimeError(
                f"派生函数对素材 {asset.id} 产出了违反 AI 不变式的 {bad} —— "
                "这在纯层被穷举证明过不可能,说明 derive_evidence_class 被改坏了。"
                "已回滚本批,先修派生函数。"
            )

        written += 1
        pending += 1
        if pending >= batch:
            session.commit()
            logger.info("evidence_class 回填进度", extra={"written": written})
            pending = 0

    if pending:
        session.commit()
    return written


if __name__ == "__main__":
    raise SystemExit(main())
