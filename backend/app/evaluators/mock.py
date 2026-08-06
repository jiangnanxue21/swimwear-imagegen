"""离线评分器(需求第十七章)。

没有视觉大模型时,整条"评分 -> 分档 -> 重生 -> 人工审核"闭环必须照样能跑通,
否则阶段 4 的业务逻辑就无从验证。

**确定性**是这里最重要的性质:分数 = f(候选图指纹, 轮次, 模式),
不掺时间戳、不掺随机种子。同一张图重跑一百次必须得到同一个分数,
否则"这张图为什么被判 C"永远说不清。

行为通过 rule_set["mock_evaluator"] 控制,用来演示各分档:

    {"outcome": "a" | "b" | "c" | "d" | "hard_fail" | "improving" | "stuck" | "auto"}

    improving  每轮变好,第 3 轮到 A —— 演示自动重生最终成功
    stuck      永远 D           —— 演示轮次耗尽后转人工审核
    auto       由图片指纹决定(默认),轮次越高越宽松
"""
from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

from app.core.enums import HardFailCode
from app.evaluators.base import SCORE_DIMENSIONS, ImageQualityEvaluator
from app.evaluators.vision_schema import QUICK_DIMENSIONS

#: 各档的基准分。刻意落在需求第十二章区间的中部,避免测试贴着边界写。
GRADE_BASELINE: dict[str, int] = {"a": 93, "b": 78, "c": 62, "d": 44}

#: 维度偏移。让各维度分数有差异,便于在前端看出短板在哪。
DIMENSION_OFFSET: dict[str, int] = {
    "garment_identity": 2,
    "structural_fidelity": 1,
    "color_fidelity": 3,
    "pattern_fidelity": 0,
    "anatomy_realism": -1,
    "face_realism": -3,
    "skin_realism": -5,
    "lighting_realism": -2,
    "background_realism": 1,
    "composition": 0,
    "website_usability": 2,
}

#: 低分档会附带的问题,让修复计划有东西可依据
GRADE_PROBLEMS: dict[str, list[dict[str, Any]]] = {
    "b": [
        {
            "code": "LIGHTING_FLAT",
            "severity": "MINOR",
            "dimension": "lighting_realism",
            "message": "布光平淡,泳衣面料高光丢失",
        }
    ],
    "c": [
        {
            "code": "SKIN_PLASTIC",
            "severity": "MAJOR",
            "dimension": "skin_realism",
            "message": "皮肤过度磨皮,呈塑料感",
        },
        {
            "code": "COMPOSITION_OFF",
            "severity": "MINOR",
            "dimension": "composition",
            "message": "主体偏离画面中心",
        },
    ],
    "d": [
        {
            "code": "SKIN_PLASTIC",
            "severity": "MAJOR",
            "dimension": "skin_realism",
            "message": "皮肤质感严重失真",
        }
    ],
}


def _fingerprint(candidate_image: str) -> int:
    """候选图指纹。优先读文件内容,读不到就退回路径字符串。

    读内容意味着"同一张图 = 同一个分",即使它被存到了不同路径,
    这正是幂等与重放验证需要的性质。
    """
    path = Path(candidate_image)
    try:
        if path.is_file():
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        else:
            digest = hashlib.sha256(candidate_image.encode("utf-8")).hexdigest()
    except OSError:
        digest = hashlib.sha256(candidate_image.encode("utf-8")).hexdigest()
    return int(digest[:8], 16)


def _pick_grade(outcome: str, fingerprint: int, round_number: int) -> str:
    """选出这张图落在哪一档。纯函数。"""
    outcome = (outcome or "auto").lower()

    if outcome in GRADE_BASELINE:
        return outcome
    if outcome == "hard_fail":
        return "d"
    if outcome == "stuck":
        return "d"
    if outcome == "improving":
        # 第 1 轮 C,第 2 轮 B,第 3 轮起 A
        return {1: "c", 2: "b"}.get(round_number, "a")

    # auto:指纹决定基础档位,轮次越高越宽松
    # (真实系统里靠换 seed 和修复提示词变好,这里模拟那个趋势)
    bucket = fingerprint % 100
    threshold_a = 55 + (round_number - 1) * 20
    threshold_b = 25 + (round_number - 1) * 15
    threshold_c = 10
    if bucket >= 100 - threshold_a + 45:
        return "a"
    if bucket >= threshold_b:
        return "b"
    if bucket >= threshold_c:
        return "c"
    return "d"


class MockImageQualityEvaluator(ImageQualityEvaluator):
    evaluator_name = "mock"
    #: 按文件指纹给分,一个请求都不发。分数是编的
    is_simulator = True
    display_name = "Mock 评分器(离线)"

    def is_configured(self) -> bool:
        return True  # 永远可用,这正是它存在的意义

    async def test_connection(self) -> dict[str, Any]:
        return {
            "evaluator": self.evaluator_name,
            "configured": True,
            "reachable": True,
            "message": "Mock 评分器始终可用,无需配置",
        }

    async def evaluate(
        self,
        product_assets: list[str],
        candidate_image: str,
        product_metadata: dict,
        rule_set: dict,
    ) -> dict:
        options = (rule_set or {}).get("mock_evaluator") or {}
        outcome = str(options.get("outcome", "auto"))
        round_number = int(options.get("round_number", 1) or 1)
        quick_only = bool(options.get("quick_only", False))

        fingerprint = _fingerprint(candidate_image)
        grade = _pick_grade(outcome, fingerprint, round_number)
        baseline = GRADE_BASELINE[grade]

        # 每个维度在基准分上做确定性微扰,幅度 ±3
        scores: dict[str, float] = {}
        for index, dimension in enumerate(SCORE_DIMENSIONS):
            jitter = ((fingerprint >> (index * 2)) % 7) - 3
            value = baseline + DIMENSION_OFFSET.get(dimension, 0) + jitter
            scores[dimension] = float(max(0, min(100, value)))

        hard_fail_codes: list[str] = []
        problems = [dict(p) for p in GRADE_PROBLEMS.get(grade, [])]

        if outcome == "hard_fail" or (outcome == "auto" and fingerprint % 17 == 0):
            # 硬错误的种类也由指纹决定,保证可复现
            code = sorted(HardFailCode)[fingerprint % len(HardFailCode)].value
            hard_fail_codes.append(code)
            problems.append(
                {
                    "code": code,
                    "severity": "CRITICAL",
                    "hard_fail": True,
                    "message": f"Mock:模拟硬错误 {code}",
                }
            )

        if quick_only:
            # 快速检查只产出硬错误相关的最少信息(需求第十三章)。
            #
            # **维度集合必须来自 `QUICK_DIMENSIONS`,不能在这里手写一份。**
            # 原来这里硬写的是 garment_identity / structural_fidelity /
            # anatomy_realism,而 vision_schema 里的口径是 garment_identity /
            # color_fidelity / anatomy_realism / website_usability ——
            # 多一个、少两个。`_parse_scores` 对 QUICK 是严格比对(缺了报
            # missing、多了报 unexpected),于是 mock 评分器**每一张走快速
            # 通道的候选图都解析失败**:不报错、不改状态,只是静默留在
            # DOWNLOADED 且没有分数,整轮决策拿一半的数据做。
            # 默认 full_evaluation_count=2,4 张候选图里固定有 2 张这样。
            scores = {k: v for k, v in scores.items() if k in QUICK_DIMENSIONS}
            problems = [p for p in problems if p.get("hard_fail")]

        return {
            "hard_fail": bool(hard_fail_codes),
            "hard_fail_codes": hard_fail_codes,
            "scores": scores,
            # 故意报一个和加权总分不同的数:后端必须用自己算的那个(需求第十章)。
            # 这里等于是给"不要相信大模型总分"这条规则内置了一个活体探针。
            "overall_score": float(min(100, baseline + 6)),
            "uncertain_items": [],
            "problems": problems,
            "recommended_action": "AUTO_APPROVE" if grade == "a" else "REGENERATE",
            "model_name": "mock-evaluator-v1",
        }
