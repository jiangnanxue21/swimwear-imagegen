"""AI 测试留档的查询判定(PRD §12.5 / BE-311)。

## 为什么过滤参数要有一层判定,而不是直接塞进 SQL

三条各自踩过坑的理由:

**认不出来的取值要拒绝,不能静默变成"不筛"。** `settings.prompt_rolled_back`
那一批就是这么修的(§3.88 第 2 条):`level=WARN` 打错成 `level=WARN ` 时被兜底成
INFO,于是筛选静默失效、拉回整窗,而运营会把那一屏读成"这段时间只有这些"。
这里同样:`kind=evaluatoin` 拼错一个字母,返回全量比返回空更危险。

**页大小要有上限,而且上限由后端说。** 这张表带完整文案输出,一条可能上千字;
`limit=100000` 会把一次点击变成一次数据库全表扫描 + 一坨传不完的 JSON。

**时间窗的两端都要能解析,解析不了就 400。** `ops_logs` 的 `parse_ts` 有过一次
500(§3.88 ①):无时区输入返回 naive,与 aware 比较时抛 TypeError,整个端点挂掉。
那条修法的教训是**容一手第三方写法的那条路径恰恰是崩溃源**,所以这里不容 ——
形状不对就 400,让调用方知道自己传错了。

## 这一层不碰 ORM

判定在 `workflows/`(本仓给零依赖判定留的地方),SQL 在 api 层。这样"limit 会不会
被绕过""认不出的 kind 会不会静默放行"能在没有库的单测里穷举,而那正是这类参数
最容易出错、也最难在集成测试里覆盖全的地方。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.workflows.ai_test_record import KIND_COPY, KIND_EVALUATION

#: 一页最多多少条。**上限由后端说,不接受调用方传更大的值。**
#: 这张表的 `payload_out` 对文案测试是全文,一条可能上千字
MAX_PAGE_SIZE = 100
DEFAULT_PAGE_SIZE = 20

KNOWN_KINDS = (KIND_EVALUATION, KIND_COPY)


class FilterRejected(ValueError):
    """参数认不出来。**不静默变成"不筛"** —— 见模块文档第一条。"""


@dataclass(frozen=True)
class RunQuery:
    """一次查询的已归一参数。"""

    kind: str | None = None
    prompt_key: str | None = None
    prompt_version: str | None = None
    #: 序号那一列(FE-314 按它互链)。评分链路落它,文案链路恒为 None
    prompt_template_version: int | None = None
    since: datetime | None = None
    until: datetime | None = None
    limit: int = DEFAULT_PAGE_SIZE
    offset: int = 0


def _clean(value: Any, limit: int) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:limit] if text else None


def parse_ts(value: Any) -> datetime:
    """时间窗的一端。解析不了就抛,不兜底。

    `ops_logs` 那条兜底路径的教训:无时区输入返回 naive,与 aware 比较时抛
    TypeError,整个端点 500(§3.88 ①)。这里恒返 aware,无时区按 UTC 解释 ——
    **而不是按本机时区**:这是一个 API 入参,调用方可能在任何时区,而
    `created_at` 落库就是 timestamptz。日志那边选本机时区是因为写入端
    `formatTime` 用的就是本机钟,两处的正确答案不同。
    """
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=UTC)
    text = str(value or "").strip()
    if not text:
        raise FilterRejected("时间不能为空")
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise FilterRejected(
            f"时间格式认不出来:「{text}」—— 请按 2026-08-19T09:30:00Z 这样的格式填"
        ) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def build_query(
    *,
    kind: Any = None,
    prompt_key: Any = None,
    prompt_version: Any = None,
    prompt_template_version: Any = None,
    since: Any = None,
    until: Any = None,
    limit: Any = None,
    offset: Any = None,
) -> RunQuery:
    """把接口入参归一成一次查询。认不出的取值一律抛 `FilterRejected`。"""
    clean_kind = _clean(kind, 32)
    if clean_kind is not None and clean_kind not in KNOWN_KINDS:
        raise FilterRejected(
            f"认不出的类型:「{clean_kind}」—— 只有 {'/'.join(KNOWN_KINDS)}。"
            "拼错一个字母时返回全量,比返回空更危险"
        )

    start = parse_ts(since) if since not in (None, "") else None
    end = parse_ts(until) if until not in (None, "") else None
    if start and end and start > end:
        raise FilterRejected("时间窗的起点晚于终点 —— 这样的窗口永远是空的")

    size = DEFAULT_PAGE_SIZE if limit in (None, "") else int(limit)
    if size < 1:
        raise FilterRejected("每页条数至少是 1")
    # **收到上限,不报错。** 传 100000 是想"一次拿全",拒绝它只会让调用方
    # 改成循环拉一万次;收到 100 并如实在出参里说明,才是那个意图的正确答案
    size = min(size, MAX_PAGE_SIZE)

    skip = 0 if offset in (None, "") else int(offset)
    if skip < 0:
        raise FilterRejected("偏移量不能为负")

    template_version: int | None = None
    if prompt_template_version not in (None, ""):
        try:
            template_version = int(str(prompt_template_version).strip())
        except (TypeError, ValueError):
            raise FilterRejected(
                f"版本序号要填数字,收到的是「{prompt_template_version}」"
            ) from None
        if template_version < 1:
            # 序号从 1 起。0 或负数查出来必然是空,而"空"和"这一版没跑过测试"
            # 在界面上长得一样 —— 与 `runsQueryForVersion` 那侧同一条判据
            raise FilterRejected("版本序号从 1 起")

    return RunQuery(
        kind=clean_kind,
        prompt_key=_clean(prompt_key, 64),
        prompt_version=_clean(prompt_version, 32),
        prompt_template_version=template_version,
        since=start,
        until=end,
        limit=size,
        offset=skip,
    )


def serialize_run(row: Any, *, iso: Any) -> dict[str, Any]:
    """一条留档的出参形状。

    `payload_in` / `payload_out` **原样带出** —— 它们在写入时已经过脱敏
    (`workflows/ai_test_record` 是唯一构造点),这里再过一次没有意义,
    而"两处各脱敏一次"会让人以为其中一处可以省掉。

    时间戳走 `iso`(调用方传 `core.clock.iso_utc`)。裸 `.isoformat()` 会让
    同一列在"刚写完就回读"与"刷新页面重查"两种路径下序列化出两种形状 ——
    `expire_on_commit=False` 那个坑,`CLAUDE.md` 第 5 条记着。
    """
    return {
        "id": str(getattr(row, "id", "") or ""),
        "kind": row.kind,
        "subject_type": row.subject_type,
        "subject_id": str(row.subject_id) if row.subject_id else None,
        "prompt_key": row.prompt_key,
        "prompt_version": row.prompt_version,
        "prompt_template_version": row.prompt_template_version,
        "generator": row.generator,
        "model_name": row.model_name,
        "success": bool(row.success),
        "error_code": row.error_code,
        "error_message": row.error_message,
        "duration_ms": row.duration_ms,
        "total_tokens": row.total_tokens,
        "billable": bool(row.billable),
        "actor": row.actor,
        "created_at": iso(row.created_at),
        "payload_in": row.payload_in,
        "payload_out": row.payload_out,
    }
