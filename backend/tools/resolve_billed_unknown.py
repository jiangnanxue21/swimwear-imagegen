"""对账之后解除「费用不明」的回执(A45-batch12)。**默认干跑。**

## 为什么需要这个脚本

`batch.receipt_route` 现在会在一条作用域自动付费执行满
`MAX_BILLED_EXECUTIONS` 次之后停下来,落 `BILLED_RESULT_UNKNOWN`
(needs_human=True,拿不到重试按钮)。那道闸是对的 —— 再跑一次可能是
第三次付费,而发起者是回收器,没有人决定过要不要花这笔钱。

但闸门必须有出口,否则它就是个死胡同:回执按幂等键索引,而幂等键由
动作 + 作用域 + 上游指纹算出来,不会自己变。人去 Provider 后台核对完
"这次其实没跑成",没有任何界面能让这一件重新开始。

这个脚本就是那个出口。它做的事只有一件:**把回执删掉**,于是下一次执行
`receipt_route` 拿到 None,按"没做过"正常走。

## 为什么是删除,不是把 executions 归零

归零会留下一条 IN_FLIGHT 且 executions=0 的回执 —— 一个在别处都不成立的
状态(`_claim_in_flight` 写的每一条 executions 都 >= 1)。而删除的语义是
干净的:"经人工核对,这次执行没有产生结果,当它没发生过"。

审计不靠这条记录留存 —— 删除动作本身会写一条 audit_logs,里面有操作者、
对账结论、以及被删回执的全部关键字段。那比留一条形状可疑的回执可靠。

## 为什么必须填对账结论

与 `generation_service.retry_task(force=True)` 完全同一条理由:这是**唯一**
一个由人主动承担重复计费风险的动作。填不出结论,说明对账根本没做。

## 用法

    # 看有哪些卡住了(默认干跑,什么都不改)
    python -m tools.resolve_billed_unknown

    # 解除一条(必须带对账结论)
    python -m tools.resolve_billed_unknown --key <幂等键> \\
        --note "FASHN 后台查 2026-08-04 无该 request,未计费" --apply
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))


def main() -> int:
    parser = argparse.ArgumentParser(
        description="对账之后解除「费用不明」的批量回执(默认干跑)"
    )
    parser.add_argument("--key", help="要解除的幂等键;不给则只列出候选")
    parser.add_argument("--note", default="", help="人工对账结论,--apply 时必填")
    parser.add_argument("--apply", action="store_true", help="真的执行,不加则只看")
    parser.add_argument("--limit", type=int, default=50)
    args = parser.parse_args()

    from sqlalchemy import or_ as sa_or, select

    from app.core.enums import AuditAction
    from app.db.session import SessionLocal
    from app.models.batch_job import BatchActionReceipt
    from app.services import audit
    from app.workbench import batch as rules

    session = SessionLocal()
    try:
        stmt = (
            select(BatchActionReceipt)
            .where(
                BatchActionReceipt.status == rules.ReceiptStatus.IN_FLIGHT.value,
                # A45-batch12-2 / EX-02:取数条件跟着 `receipt_route` 一起改。
                #
                # 原来只有 `executions >= MAX_BILLED_EXECUTIONS` 一条 —— 那是
                # "自动付费额度用完了"的口径。现在 IN_FLIGHT 只要**调用已经
                # 发出去过**就当场停下,`executions` 还是 1。照旧条件取数的话,
                # 这个出口会对新卡住的那一批**一条都列不出来**,而它们正是
                # 现在唯一会卡住的那一批 —— 闸门装上了、出口对不上号。
                #
                # 两条取或:新口径(调用已发出)与旧口径(额度耗尽)。
                # 旧口径留着是因为 `FAILED_BILLED` + 不可复校动作仍然走它,
                # 以及 0031 之前就已经卡在那里的存量行
                sa_or(
                    BatchActionReceipt.provider_call_at.is_not(None),
                    BatchActionReceipt.executions >= rules.MAX_BILLED_EXECUTIONS,
                ),
            )
            .order_by(BatchActionReceipt.last_used_at.desc())
            .limit(args.limit)
        )
        rows = list(session.scalars(stmt))

        if not args.key:
            if not rows:
                print("没有卡在「费用不明」的回执。")
                return 0
            print(f"卡住的回执 {len(rows)} 条:\n")
            for row in rows:
                print(
                    f"  {row.idempotency_key}\n"
                    f"      动作 {row.action} · 作用域 {row.scope_key}\n"
                    f"      开跑 {row.executions} 次 · 已计费调用 {row.paid_calls} 次"
                    f" · 最后 {row.last_used_at}\n"
                    # EX-02:这一行是对账真正要用的东西。没有它,
                    # 「去后台核对」就退化成在整天的请求里大海捞针
                    f"      付费调用发出时刻 "
                    f"{row.provider_call_at or '(未发出 / 0031 之前的存量行)'}\n"
                )
            print(
                "解除前请先到模型服务商后台核对这条作用域是否已经产生过结果。\n"
                "确认没有之后:\n"
                "  python -m tools.resolve_billed_unknown --key <键> "
                '--note "<对账结论>" --apply'
            )
            return 0

        target = session.scalar(
            select(BatchActionReceipt).where(
                BatchActionReceipt.idempotency_key == args.key
            )
        )
        if target is None:
            print(f"找不到回执:{args.key}")
            return 1
        if target.status != rules.ReceiptStatus.IN_FLIGHT.value:
            # 只解除 IN_FLIGHT。SUCCEEDED 被删掉的话,下一次执行会重新付费
            # 跑一遍一个**已经成功**的动作 —— 那是这个脚本最容易造成的伤害
            print(
                f"拒绝:回执状态是 {target.status},不是 IN_FLIGHT。"
                "这个脚本只解除「费用不明」那一类;删掉别的状态会让一个"
                "已有结论的动作重新付费执行"
            )
            return 1

        note = args.note.strip()
        if not args.apply:
            print(
                f"[干跑] 将删除回执 {target.idempotency_key}\n"
                f"       动作 {target.action} · 作用域 {target.scope_key}\n"
                f"       开跑 {target.executions} 次 · 已计费调用 {target.paid_calls} 次\n"
                "       加 --apply 才真的执行"
            )
            return 0
        if not note:
            print(
                "拒绝:--apply 必须带 --note(人工对账结论)。\n"
                "这是唯一一个由人主动承担重复计费风险的动作,填不出结论说明对账没做。"
            )
            return 1

        payload = {
            "idempotency_key": target.idempotency_key,
            "action": target.action,
            "scope_key": target.scope_key,
            "executions": target.executions,
            "paid_calls": target.paid_calls,
            "reconciliation_note": note[:500],
        }
        audit.record(
            session,
            actor="ops:resolve_billed_unknown",
            action=AuditAction.DELETE,
            entity_type="BatchActionReceipt",
            entity_id=target.id,
            payload=payload,
        )
        session.delete(target)
        session.commit()
        print(
            f"已解除 {target.idempotency_key}。\n"
            "下一次执行会按「没做过」正常走,并重新计费一次。审计已记录。"
        )
        return 0
    finally:
        session.close()


if __name__ == "__main__":
    raise SystemExit(main())
