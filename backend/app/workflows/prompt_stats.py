"""提示词调用统计的判定(PRD §12.1 BE-302 / §12.4 FE-301 / FE-306)。

## 这一层回答什么

「这一份提示词近 7 天被调用了多少次,其中多少次没评成」—— 列表页每行一个数,
详情页每个版本一行。数据源是 `evaluation_attempts`:那张表**成功与失败一视同仁**
地留痕,而它存在的理由(见模型 docstring)恰好就是这个问题:

    换了提示词之后解析失败率有没有下降?

## 为什么判定不在 SQL 里

三件事在 SQL 里写得出来,但**验不了**:

**哪些行算一次调用。** 整轮汇总那两行不对应任何一次 API 调用,算进去既虚增
分母、又把已经逐张记过的失败再加一遍。计费侧为同一条踩过坑
(`_record_attempt` 的 `billable_provider` 只在逐张三处传)。

**失败率的分母。** 一次调用都没有时,失败率不是 0% —— 那会让一个**没人用过**的
版本和一个**跑得很好**的版本在界面上长得一模一样。没有分母就没有比率,
这里返回 None,由展示层说「暂无调用」。

**未归属的调用怎么报。** 0059 之前的历史行没有 `prompt_key`,推不出来
(见迁移 0059 的模块文档)。它们既不能算进任何一份提示词,也不该消失 ——
消失的话「近 7 天 3 次调用」会被读成「这一份只被用了 3 次」,而实际可能是 300 次,
另外 297 次在统计的起点之前。所以单独报一个数,让界面说得出「另有 N 次未归属」。

## 这一层不碰 ORM

判定在 `workflows/`(本仓给零依赖判定留的地方),SQL 在服务层。这样"整轮行会不会
被算进调用数""零调用会不会显示成 0% 失败率"能在没有库的单测里穷举 ——
而那正是这类聚合最容易出错、也最难在集成测试里覆盖全的地方
(与 `ai_test_query.py` 同一条理由)。
"""
from __future__ import annotations

from dataclasses import dataclass

#: FE-301 那一列的窗口。PRD §12.1 写死「近 7 天」。
#:
#: 做成常量而不是参数,是因为它同时是**界面文案**的一部分:列头写着「近 7 天」,
#: 而窗口与文案分开维护的话,改了一个不改另一个没有任何东西会红 ——
#: 界面会理直气壮地用另一个窗口的数字说「近 7 天」。
RECENT_WINDOW_DAYS = 7

#: 没评成的三种结局(`core.enums.EvaluationOutcome`)。这里**按字面量列**而不是
#: import 那个枚举:本模块要保持零依赖,而枚举在 `app.core` 里 —— 依赖它本身
#: 无害,但一旦开了口子,下一个人会顺手 import models。
#:
#: 字面量与枚举分叉的风险由 `test_a70_prompt_stats.py` 的一条守卫双向钉着,
#: 而不是靠这条注释。
OUTCOME_SUCCEEDED = "SUCCEEDED"
OUTCOME_PARSE_FAILED = "PARSE_FAILED"
OUTCOME_PROVIDER_ERROR = "PROVIDER_ERROR"
OUTCOME_UNAVAILABLE = "UNAVAILABLE"
OUTCOME_ROUND_RETRY_SCHEDULED = "ROUND_RETRY_SCHEDULED"

#: 算作「一次失败」的结局。`ROUND_RETRY_SCHEDULED` **不在**里面 —— 它只出现在
#: 整轮汇总行上,而整轮行根本不进统计(见 `counts_as_call`)。
FAILURE_OUTCOMES = (
    OUTCOME_PARSE_FAILED,
    OUTCOME_PROVIDER_ERROR,
    OUTCOME_UNAVAILABLE,
)


def counts_as_call(*, round_level: bool) -> bool:
    """这一行算不算「一次调用」。

    整轮汇总行不算:它不对应任何一次 API 调用。判据取自写入时钉在
    `meta.round_level` 上的事实,**不是** outcome、**也不是** `candidate_id`——
    前者在逐张与整轮两处都会写 PROVIDER_ERROR,后者是 `ondelete="SET NULL"`,
    候选图被清理之后会把逐张行变成 NULL。两条都试过了,都分不干净。
    """
    return not round_level


@dataclass(frozen=True)
class PromptCallStats:
    """一份(或一个版本的)提示词的调用统计。

    `calls` 已经排除整轮汇总行,所以它和 `failures` 是同一个分母上的两个数。
    """

    calls: int = 0
    parse_failed: int = 0
    provider_error: int = 0
    unavailable: int = 0
    #: 其中由人工能力测试(AI 测试页)触发的次数。**不从 `calls` 里扣掉** ——
    #: 它们是真实调用、真的花了钱、真的用了这一版提示词,是失败率的有效证据。
    #: 单独报出来是因为诊断调用常常挑边缘用例跑,失败率偏高时要能看出是不是它带的
    diagnostic: int = 0

    @property
    def failures(self) -> int:
        """FE-306 那一列的 M(「调用 N 次 · 失败 M 次」)。"""
        return self.parse_failed + self.provider_error + self.unavailable

    @property
    def parse_failure_rate(self) -> float | None:
        """FE-301 那一列的解析失败率。**没有调用时返回 None,不是 0.0。**

        0.0 会让一个没人用过的版本和一个跑得很好的版本在界面上长得一样,
        而这一列存在的全部理由是把这两者分开。
        """
        if self.calls <= 0:
            return None
        return self.parse_failed / self.calls

    @property
    def failure_rate(self) -> float | None:
        """全部失败占比。空分母同上,返回 None。"""
        if self.calls <= 0:
            return None
        return self.failures / self.calls

    def as_dict(self) -> dict:
        """出参形状。比率保留四位小数 —— 前端要显示百分比一位小数。

        **两个比率照样输出 None**,不在这里兜底成 0:接口形状完整不是把
        判不出来的值填成常量的理由(硬规则第 4 条)。
        """
        return {
            "calls": self.calls,
            "failures": self.failures,
            "parse_failed": self.parse_failed,
            "provider_error": self.provider_error,
            "unavailable": self.unavailable,
            "diagnostic": self.diagnostic,
            "parse_failure_rate": _round4(self.parse_failure_rate),
            "failure_rate": _round4(self.failure_rate),
        }


def _round4(value: float | None) -> float | None:
    return None if value is None else round(value, 4)


def tally(rows: list[dict]) -> PromptCallStats:
    """把一批 attempt 行数成一份统计。

    `rows` 的每一项形如 ``{"outcome": ..., "round_level": ..., "diagnostic": ...}``,
    可带一个 ``"count"``(缺省 1)—— 服务层用 ``GROUP BY`` 把几十万行压成十几组
    再送进来,判定不变而数据库只回传计数。传 dict 而不是 ORM 对象,是这一层
    零依赖的代价,也是它能被穷举的原因。

    **认不出来的 outcome 计进 `calls` 但不计进任何一类失败。** 反过来
    (当成失败)会让新增一个枚举取值静默抬高所有历史提示词的失败率;
    而漏进 calls 会让分母比实际小,失败率虚高 —— 两害相权,分母要全。
    """
    stats = {"calls": 0, "parse_failed": 0, "provider_error": 0, "unavailable": 0, "diagnostic": 0}
    for row in rows:
        if not counts_as_call(round_level=bool(row.get("round_level"))):
            continue
        n = row.get("count", 1)
        n = n if isinstance(n, int) and not isinstance(n, bool) and n > 0 else 0
        stats["calls"] += n
        if row.get("diagnostic"):
            stats["diagnostic"] += n
        outcome = str(row.get("outcome") or "")
        if outcome == OUTCOME_PARSE_FAILED:
            stats["parse_failed"] += n
        elif outcome == OUTCOME_PROVIDER_ERROR:
            stats["provider_error"] += n
        elif outcome == OUTCOME_UNAVAILABLE:
            stats["unavailable"] += n
    return PromptCallStats(**stats)
