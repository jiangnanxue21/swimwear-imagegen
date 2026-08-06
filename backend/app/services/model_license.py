"""模特授权的判定规则(PRD v2 §11.3)。**纯逻辑,零 ORM 依赖。**

与 `core/audience.py`、`workbench/audience_rules.py` 同一个惯例:判定规则
不依赖 SQLAlchemy,才能被纯测试直接跑。`model_template_service` 再导出一次,
调用方不必知道它住在哪。

`template` 用鸭子类型而不是标注成 `ModelTemplate`:这个模块不 import ORM,
它只要求四个属性(license_status / license_expires_at / allow_ai_dressing /
prohibited_categories),测试因此可以传任何简单对象。
"""
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from app.core.enums import ModelLicenseStatus
from app.core.errors import ErrorCode, ValidationError


def assert_usable(template: Any, *, category_id: str | None = None) -> None:
    """授权硬阻断(§11.3):**创建新任务前必经**,与 enabled 检查并列。

    阻断四种情况:

        EXPIRED / REVOKED          授权状态已失效
        license_expires_at 已过     到期日过了但状态没人改
        LICENSED 且不许 AI 换装     授权明确禁止本系统的用途
        LICENSED 且品类被禁用       授权把这个品类排除在外

    后两条**只在 license_status == LICENSED 时生效**,这不是漏判。
    默认值 `allow_ai_dressing=False` 的含义是"没人核实过",不是"授权禁止";
    对 UNVERIFIED 执行它会让存量模板全部停摆 —— 而那与迁移 0030 记的
    取向直接矛盾(UNVERIFIED 不阻断,进待补清单)。
    一旦有人把状态改成 LICENSED,就意味着他核对过条款,那份条款的限制
    从那一刻起是权威的。

    UNVERIFIED **不阻断**。历史商品与历史图片不受影响:这条只管"新任务",
    符合 §11.3 的分界。措辞按 §5.1:普通运营只看到"该模特暂不可用",
    不看到授权细节 —— 授权条款属于合同信息,不该出现在生成页的报错里。
    """
    status = str(template.license_status or "").upper()
    if status in (
        ModelLicenseStatus.EXPIRED.value,
        ModelLicenseStatus.REVOKED.value,
    ):
        raise ValidationError(
            "该模特暂不可用,请选择其他模特(授权已失效,管理员可在模特库查看详情)",
            code=ErrorCode.INPUT_INVALID,
        )
    expires = template.license_expires_at
    if expires is not None:
        # 库列是 naive UTC(与全仓约定一致);比较前把 now 也脱去 tzinfo
        now = datetime.now(UTC).replace(tzinfo=None)
        if expires <= now:
            raise ValidationError(
                "该模特暂不可用,请选择其他模特(授权已到期,管理员可在模特库续期)",
                code=ErrorCode.INPUT_INVALID,
            )

    if status != ModelLicenseStatus.LICENSED.value:
        # 未核实的授权不参与下面两条判定(见 docstring)
        return

    if not template.allow_ai_dressing:
        # 这个系统做的就是 AI 换装。授权说不许,就是不许 ——
        # 存下来却从不检查,等于把一份合规记录做成装饰
        raise ValidationError(
            "该模特暂不可用,请选择其他模特(授权范围不包含本系统的用途)",
            code=ErrorCode.INPUT_INVALID,
        )

    if category_id:
        prohibited = {str(c).strip().lower() for c in (template.prohibited_categories or [])}
        if prohibited and category_id.strip().lower() in prohibited:
            raise ValidationError(
                "该模特暂不可用,请选择其他模特(授权未覆盖该商品品类)",
                code=ErrorCode.INPUT_INVALID,
            )
