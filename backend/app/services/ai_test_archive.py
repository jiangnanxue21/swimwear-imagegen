"""把一次 AI 测试写进 `ai_test_runs`(PRD §12.5 / BE-310)。

## 分工

成形规则全在 `app/workflows/ai_test_record.py`(纯层,零依赖,可穷举)。
这里只做三件带副作用的事:拿到那个 record、`session.add`、失败时喊一声。

## 写入失败为什么不往上抛

留档发生在模型调用**之后**。那次调用可能真花了钱,结果已经在手上。
此时把 INSERT 的异常抛出去,运营拿到的是一个 500 —— **钱花了、结果也没了**,
而丢掉的本来只是一条台账。

所以这里吞异常,但**不静默**:出错走 `eval.ai_test_record_failed`,ERROR 级,
带上 kind / subject / prompt_version。BLOCK-31 刚刚把运行日志控制台做出来,
这条事件码在筛选下拉里是可选中的 —— "留档失败"的去处是那里,不是接口出参。

**这与 a53「日志绝不反噬业务」不是同一条,方向也相反。** 那条说的是日志
不许拖垮业务;这里说的是**台账不许拖垮已经付过费的结果**。两条共用一个形状
(捕获 + 记录 + 继续),但这里多一个义务:必须留下可检索的痕迹,因为这张表
的价值恰恰在于它完整。吞掉不喊 = 一张有洞而看起来完好的档案,比没有档案更糟。

## 为什么调用点在两个 `diagnose_*` 里面,不在 api 层

与 `evaluation_service.diagnose_candidate` 里那条封顶注释同一个理由:
任何入口都要留档,**漏接一个入口就等于这条台账不完整**,而不完整的台账
读的人分不出"没跑过"和"跑了没记上"。`actor` 是 api 层的事实,所以它作为
参数传进来 —— 传一个参数,比在两个 api 端点各抄一遍写入好。
"""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from app.core.logging import get_logger
from app.models.ai_test_run import AiTestRun
from app.workflows.ai_test_record import AiTestRecord

logger = get_logger(__name__)


def archive(session: Session, record: AiTestRecord) -> AiTestRun | None:
    """落一行留档。成功返回该行,失败返回 None(并记一条 ERROR)。

    只 `add` + `flush`,**不 commit** —— 提交归调用请求所有(任务 19 后半)。
    `flush` 是必要的:不 flush 的话约束冲突要等到请求末尾的 commit 才炸,
    那时候异常已经不在这个 try 里,吞不住,运营还是会看见 500。
    """
    fields = _log_fields(record)
    try:
        # **逐个具名传,不用 `AiTestRun(**kwargs)`(a63 订正)。**
        #
        # `**kwargs` 让 `audit_column_writers.py` 把整个模型划进「判不了」——
        # 而那条审计存在的全部理由就是「每一列都答得出谁写它」。代价是实测出来的:
        # `prompt_template_version` 从建表起就没有任何写入方、恒为 NULL,
        # **而两道防线同时没看见** —— 列审计因为 `**kwargs` 看不见整张表,
        # a57 那条 `test_the_row_kwargs_cover_every_column_the_model_declares`
        # 验的是**键集**,而那个键一直在、值一直是 None。
        #
        # 键在、值恒空,是本仓反复记的那种缝:两侧各自的测试都绿,没有一条跨过中间。
        kwargs = record.as_row_kwargs()
        row = AiTestRun(
            kind=kwargs["kind"],
            subject_type=kwargs["subject_type"],
            subject_id=kwargs["subject_id"],
            prompt_key=kwargs["prompt_key"],
            prompt_version=kwargs["prompt_version"],
            prompt_template_version=kwargs["prompt_template_version"],
            generator=kwargs["generator"],
            model_name=kwargs["model_name"],
            payload_in=kwargs["payload_in"],
            payload_out=kwargs["payload_out"],
            success=kwargs["success"],
            error_code=kwargs["error_code"],
            error_message=kwargs["error_message"],
            duration_ms=kwargs["duration_ms"],
            total_tokens=kwargs["total_tokens"],
            billable=kwargs["billable"],
            actor=kwargs["actor"],
        )
        session.add(row)
        session.flush()
    except Exception as exc:  # noqa: BLE001 —— 见模块文档:此处必须吞,但必须喊
        logger.error(
            "ai test run archive failed",
            extra={
                "extra_fields": {
                    "event": "eval.ai_test_record_failed",
                    **fields,
                    "error": type(exc).__name__,
                }
            },
        )
        return None
    logger.info(
        "ai test run archived",
        extra={"extra_fields": {"event": "eval.ai_test_recorded", **fields,
                                "run_id": str(row.id)}},
    )
    return row


def _log_fields(record: AiTestRecord) -> dict[str, Any]:
    """两条事件码共用同一组字段。

    抄两份的话,成功那份加一个键时失败那份不会跟着变 —— 而读它的人
    恰恰是在出错时才去看。这是 a54 修过的形状(`relay_dispatches`
    的异常分支手抄了一份返回字典),同一个坑不重新踩一遍。
    """
    return {
        "kind": record.kind,
        "subject_type": record.subject_type,
        "subject_id": str(record.subject_id) if record.subject_id else None,
        "prompt_key": record.prompt_key,
        "prompt_version": record.prompt_version,
        "success": record.success,
        "billable": record.billable,
    }
