"""AI 测试留档的成形与保留判定(PRD §11.4 / §12.5,BE-309 / BE-310)。

## 这一层为什么是纯的,以及为什么住在 `workflows/` 而不是 `prompts/`

留档要回答的是「某一版提示词跑出过什么」。那个问题必须能在一个没有数据库的
单测里复现 —— 与 `grading-stays-pure` 想守的是同一件事:一旦"这条记录长什么样"
只能靠起一套库来观察,就没有人会去质询它,而这张表的全部价值在于它可信。

**第一版放在 `app/prompts/` 下,被 a56 的守卫当场挡住。** 那条守卫要求
`app/prompts/*.py` 只许 import `app.core`,理由是 lint(services 层)与评分
键选择(evaluators 层)两边都要 import 注册表,它一旦拖进领域模块,依赖面
就会顺着这两条线灌进 `grading-stays-pure`。而这个模块必须 import
`app.llm.redaction` —— 脱敏规则只能有一份,把它挪到调用方等于给这条规矩
开第二个入口,而 §11.4 点名要防的就是"新开一个去向时把老规矩忘了"。

两个要求都对,冲突的是位置。`workflows/` 正是本仓给"零依赖判定"留的地方
(CLAUDE.md:「判定必须留在 `workflows/`。那里零依赖,所以能在 `tests/pure/`
被穷举」),所以搬这里。**没有去放宽那条守卫** —— 一条为了容纳新文件而
放宽的门禁,下一次就挡不住真正该挡的东西。

所以这里只做三件事:决定一行留档**长什么样**、决定 `payload_out` 里放**指针
还是全文**、决定一条旧记录**能不能删**。写库、发日志、拿 actor 都在
`app/services/ai_test_archive.py`。

## `payload_out`:评分放指针,文案放全文

评分测试已经有 `EvaluationAttempt` 完整落库(`raw` 里连模型原始响应都在)。
再抄一份的代价不是磁盘,是**两份会分叉** —— 修了一处忘了另一处的时候,
没有任何东西会红,而读的人不知道该信哪一份。所以 `kind='evaluation'` 只落
`{"attempt_id": ...}`,索引字段照落,要看正文顺着指针走。

文案测试相反:`diagnose_copy` 明确不建 ContentPlan / ListingCopy,于是**今天
它的输出一行都没存**。关掉页面就没了。所以这一侧必须落全文。

## 脱敏:新开一个去向,最容易漏的就是在新去向上把老规矩忘了

`payload_in` / `payload_out` 一律过 `llm.redaction.safe_payload_for_log`,与旁挂库
同一个函数。这不是防御性冗余:文案测试的 `payload_out` 里有模型原始输出,
评分测试的 `payload_in` 里有素材路径,而这张表将来会被 `GET /ai-tests/runs`
(BE-311)整表读出去。

## 保留期不是一个可以拍出来的数(PRD §14.3 决议)

v2.0 把「180 天够不够」列为待决,等产品回答"要回看多久"。那个问题问错了对象:
**一条留档记录的寿命下界,由它引用的东西还活不活着决定,不由日历决定。**

于是这里把它拆成两半:

    可调的那半   `DEFAULT_RETENTION_DAYS`,产品说改就改,只影响磁盘占用
    不可协商的半 引用着**当前生效版本**的记录,多老都不删

第二条是不变量。删掉当前生效那一版的唯一证据,等于在它最会被查的时候把它清掉 ——
而那正是"回看多久"这个问题真正在担心的事。把它写成代码之后,产品那个数从
"决定这张表对不对"降级成"决定这张表多大",可以先带默认值上线。

**`purgeable()` 本轮没有调用点。** 清理拍子(挂 beat、复用 migration 0024 那套)
留在 BE-311 同批,理由是它要和 `GET /ai-tests/runs` 一起才验得动:没有读接口,
"删对了没有"只能靠直接查库看,而那是本轮环境给不了的。这一条刻意不进
`verify_delivery.WIRED_MODULES` —— 登记了会当场红,而红的原因不是缺陷。
"""
from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.llm.redaction import safe_payload_for_log

#: 留档的两类。字符串而不是枚举:`core/enums.py` 里那套枚举是业务状态机的,
#: 而这两个值是"哪条链路产生的档案",跟着 §11.4 的表定义走。
KIND_EVALUATION = "evaluation"
KIND_COPY = "copy"

SUBJECT_CANDIDATE = "generation_candidate"
SUBJECT_PRODUCT = "product"

#: 产品可调。**改它只改磁盘占用,不改这张表的正确性** —— 正确性由
#: `_still_referenced()` 那条不变量守着,见模块文档。
DEFAULT_RETENTION_DAYS = 180


@dataclass(frozen=True)
class AiTestRecord:
    """一次 AI 测试的留档行。字段与 §11.4 的表定义一一对应。

    冻结是刻意的:调用方拿到之后不许再改 —— 成形规则(尤其是脱敏与
    指针/全文的取舍)必须只有这一个入口,否则"这一列是怎么来的"又会变成
    要读三个调用点才答得出的问题。
    """

    kind: str
    subject_type: str
    subject_id: Any
    success: bool
    billable: bool
    actor: str
    prompt_key: str | None = None
    prompt_version: str | None = None
    prompt_template_version: int | None = None
    generator: str | None = None
    model_name: str | None = None
    payload_in: dict[str, Any] | None = None
    payload_out: dict[str, Any] | None = None
    error_code: str | None = None
    error_message: str | None = None
    duration_ms: int | None = None
    total_tokens: int | None = None

    def as_row_kwargs(self) -> dict[str, Any]:
        """喂给 ORM 构造函数的 kwargs。

        逐字段列出而不是 `dataclasses.asdict()`:模型列改名时前者当场
        TypeError,后者会安静地多带一个键进去(SQLAlchemy 会忽略它),
        于是那一列永远是 NULL 而没有任何东西变红。
        """
        return {
            "kind": self.kind,
            "subject_type": self.subject_type,
            "subject_id": self.subject_id,
            "prompt_key": self.prompt_key,
            "prompt_version": self.prompt_version,
            "prompt_template_version": self.prompt_template_version,
            "generator": self.generator,
            "model_name": self.model_name,
            "payload_in": self.payload_in,
            "payload_out": self.payload_out,
            "success": self.success,
            "error_code": self.error_code,
            "error_message": self.error_message,
            "duration_ms": self.duration_ms,
            "total_tokens": self.total_tokens,
            "billable": self.billable,
            "actor": self.actor,
        }


def _redacted(value: Any) -> dict[str, Any] | None:
    """过脱敏并保证出来的是 dict(jsonb 列不接受裸标量)。"""
    if value is None:
        return None
    safe = safe_payload_for_log(value)
    if isinstance(safe, Mapping):
        return dict(safe)
    # 非 mapping 包一层而不是丢掉:丢掉等于让"这次没有输出"和
    # "输出不是 dict"长得一样,而后者是拼装逻辑变了的信号
    return {"value": safe}


def _clean_str(value: Any, limit: int | None = None) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    return text[:limit] if limit else text


def _int_or_none(value: Any) -> int | None:
    """`bool` 是 `int` 的子类,不排掉的话 `success=True` 会变成 token 数 1。"""
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _int_version(value):
    """把 `evaluation_attempts.prompt_version` 还原成整数序号。

    那一列在迁移 0055 之后是 String(32),装的仍是 `prompt_templates.version`
    这个自增序号(评分链路还没接内容哈希,见 §3.90)。转不动就返回 None ——
    **不兜底成 0**:0 会让「没有版本」和「第 0 版」长得一样,而
    `prompt_service._assert_expected_version` 那边刚为同一个理由拒绝过哨兵值。
    """
    if value is None:
        return None
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def record_for_evaluation(
    *,
    candidate_id: Any,
    attempt_id: Any,
    success: bool,
    billable: bool,
    actor: str,
    prompt_key: str | None = None,
    template_version=None,
    prompt_version: str | None = None,
    evaluator: str | None = None,
    model_name: str | None = None,
    depth: Any = None,
    reference_count: int | None = None,
    error_code: Any = None,
    error_message: str | None = None,
    duration_ms: Any = None,
    total_tokens: Any = None,
) -> AiTestRecord:
    """评分测试的留档行。`payload_out` **只放指针**,理由见模块文档。"""
    return AiTestRecord(
        kind=KIND_EVALUATION,
        subject_type=SUBJECT_CANDIDATE,
        subject_id=candidate_id,
        success=bool(success),
        billable=bool(billable),
        actor=_clean_str(actor, 64) or "system",
        prompt_key=_clean_str(prompt_key, 64),
        # **评分链路落的是序号那一列,不是哈希那一列(a63 订正)。**
        #
        # a57 把 `attempt.prompt_version` 塞进了 `prompt_version` —— 而那一列的
        # 文档写着「内容哈希,与 §11.3 同源」,装进去的却是 `prompt_templates.version`
        # 这个自增序号。后果不是这一行错,是**两列的语义一起塌了**:
        # `prompt_version` 混装两种形状(评分是 "3",文案是 "gpt-4o:a1b2c3"),
        # 而 `prompt_template_version` 从建表起就没有任何写入方、恒为 NULL。
        # 这正是 FE-314 互链一直定不下口径的原因:按哪一列查都对不上。
        prompt_template_version=_int_version(template_version),
        # 评分链路**目前没有内容派生版本**,留 None 而不是把序号塞进来 ——
        # 塞进来的话,BE-305 真接上那天没有任何东西能分辨哪些行是旧口径
        prompt_version=_clean_str(prompt_version, 32),
        generator=_clean_str(evaluator, 64),
        model_name=_clean_str(model_name, 128),
        payload_in=_redacted(
            {
                "depth": _clean_str(getattr(depth, "value", depth)),
                "reference_count": _int_or_none(reference_count),
            }
        ),
        # 指针本身也过脱敏:它今天只有一个 uuid,而"今天只有 uuid"
        # 不是一条能被守卫看见的保证
        payload_out=_redacted({"attempt_id": str(attempt_id)} if attempt_id else None),
        error_code=_clean_str(error_code, 64),
        error_message=_clean_str(error_message),
        duration_ms=_int_or_none(duration_ms),
        total_tokens=_int_or_none(total_tokens),
    )


def record_for_copy(
    *,
    product_id: Any,
    result: Mapping[str, Any],
    actor: str,
    prompt_key: str | None = None,
    fact_count: int | None = None,
) -> AiTestRecord:
    """文案测试的留档行。`payload_out` **落全文**,理由见模块文档。

    入参是 `diagnose_copy` 的返回体本身,不是它的某几个字段 —— 那个函数
    的成功与失败两条分支返回同一套键,在这里再各拆一遍等于把那份对称
    重新拆散,而它正是靠对称才没有出过\"某个分支少一个键\"的事故。
    """
    trace = result.get("trace") or {}
    violations = result.get("violations") or []
    return AiTestRecord(
        kind=KIND_COPY,
        subject_type=SUBJECT_PRODUCT,
        subject_id=product_id,
        success=bool(result.get("success")),
        billable=bool(result.get("billable")),
        actor=_clean_str(actor, 64) or "system",
        prompt_key=_clean_str(prompt_key, 64),
        prompt_version=_clean_str(result.get("prompt_version"), 32),
        generator=_clean_str(result.get("generator"), 64),
        model_name=_clean_str(trace.get("model") or trace.get("model_name"), 128),
        payload_in=_redacted(
            {
                "fact_count": _int_or_none(fact_count),
                "generator": _clean_str(result.get("generator"), 64),
            }
        ),
        payload_out=_redacted(
            {
                "copy": result.get("copy"),
                "notes": list(result.get("notes") or []),
                # 违规项跟着输出一起存:三个月后问\"这一版提示词是不是老写超长标题\",
                # 答案在这里,不在正文里
                "violations": list(violations),
            }
        ),
        error_code=_clean_str(result.get("error_code"), 64),
        error_message=_clean_str(result.get("message")) if not result.get("success") else None,
        duration_ms=_int_or_none(trace.get("duration_ms")),
        total_tokens=_int_or_none(trace.get("total_tokens")),
    )


def _still_referenced(row: Any, active_versions: Iterable[str]) -> bool:
    """这条记录引用的提示词版本,是不是还有 key 正生效在它上面。"""
    version = _clean_str(getattr(row, "prompt_version", None))
    return bool(version) and version in set(active_versions)


def purgeable(
    rows: Sequence[Any],
    *,
    active_versions: Iterable[str],
    now: datetime,
    retention_days: int = DEFAULT_RETENTION_DAYS,
) -> tuple[Any, ...]:
    """哪些留档可以删。**两道闸,顺序不能换。**

    1. **年龄。** 早于 `now - retention_days` 的才考虑。
    2. **引用还活着吗。** `prompt_version` 仍是某个 key 的当前生效版本,
       就不删 —— 多老都不删。这一条是不变量,不是可调项:删掉生效版本的
       唯一证据,等于在它最会被查的时候把它清掉。

    `active_versions` 由调用方从 `prompt_templates` 里现查,不缓存 ——
    缓存一份"当时的生效版本"去做删除决定,是这条不变量最容易被绕开的方式。
    空集合是合法输入(一个模板都没启用),此时只剩年龄那道闸。

    返回**可删的那些行本身**而不是布尔:调用方要把它们逐条打进日志,
    只给一个数字的话,"删掉的是哪几条"事后答不出来。
    """
    if retention_days < 0:
        raise ValueError("retention_days 不能为负 —— 负数会把\"未来的记录\"也算成过期")
    cutoff = now - timedelta(days=retention_days)
    active = set(active_versions)
    doomed = []
    for row in rows:
        created = getattr(row, "created_at", None)
        if not isinstance(created, datetime) or created >= cutoff:
            continue
        if _still_referenced(row, active):
            continue
        doomed.append(row)
    return tuple(doomed)
