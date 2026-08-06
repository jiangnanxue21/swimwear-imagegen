"""渠道字段规格的加载与全量校验(§9.2)。

spec 是**外部配置文件**。它会被运营改、会从平台文档抄、将来可能从库里读,
所以加载是一道关口而不是一次 `yaml.safe_load`:

    每一条 source 表达式必须能被受限 DSL 解析
    每一条 transform 必须在白名单里、参数齐全
    SPU 级字段不得引用 `variant.*`(只有 SKU 行上下文才有变体)
    字段 key 不得重复
    生产环境下存在 TODO 字段一律拒绝加载

**这些检查全部在加载期做,不留到运行期。** 一个解析不了的 source
在运行期的表现是「这个字段是空的」,而空字段要到平台驳回时才会被发现,
那时候错误信息通常毫无指向性。

这个模块只依赖标准库 + pyyaml + 本项目的契约,不碰数据库。
"""
from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import yaml

from app.channels.base import (
    FIELD_TYPES,
    NUMERIC_FIELD_TYPES,
    ChannelFieldSpec,
    FieldSpec,
)
from app.channels.source_expr import (
    SourceExprError,
    parse_source,
    validate_transform,
)
from app.core.errors import AppError, ErrorCode


class ChannelSpecError(AppError):
    """spec 不合法。启动期抛,不留到运行期。"""

    code = ErrorCode.CHANNEL_SOURCE_EXPR_INVALID
    http_status = 500

    def __init__(self, message: str, *, code: ErrorCode | None = None, **detail: Any) -> None:
        super().__init__(message, code=code, detail=detail)


def _field_from_dict(raw: Mapping[str, Any], *, where: str, allow_variant: bool) -> FieldSpec:
    key = str(raw.get("key") or "").strip()
    if not key:
        raise ChannelSpecError(f"{where}: 字段缺少 key")

    source = raw.get("source")
    if source is not None:
        source = str(source)
        if source != "TODO":
            try:
                ref = parse_source(source)
            except SourceExprError as exc:
                raise ChannelSpecError(f"{where}.{key}: {exc}") from exc
            if ref.is_row_only and not allow_variant:
                raise ChannelSpecError(
                    f"{where}.{key}: {ref} 只能用于 SKU 行字段,不能用于 SPU 级字段"
                )

    transforms = tuple(raw.get("transforms") or raw.get("transform") or ())
    for item in transforms:
        if not isinstance(item, Mapping):
            raise ChannelSpecError(f"{where}.{key}: transform 必须是对象")
        try:
            validate_transform(item)
        except SourceExprError as exc:
            raise ChannelSpecError(f"{where}.{key}: {exc}") from exc

    allowed = tuple(str(v) for v in (raw.get("allowed_values") or ()))
    max_length = raw.get("max_length")
    if max_length is not None:
        max_length = int(max_length)
        if max_length <= 0:
            raise ChannelSpecError(f"{where}.{key}: max_length 必须为正数")

    value_type, minimum, maximum, decimal_places = _numeric_rules(
        raw, where=f"{where}.{key}"
    )

    return FieldSpec(
        key=key,
        required=bool(raw.get("required", False)),
        max_length=max_length,
        allowed_values=allowed,
        source=source,
        transforms=tuple(dict(t) for t in transforms),
        column_header=raw.get("column_header") or key,
        note=raw.get("note"),
        value_type=value_type,
        minimum=minimum,
        maximum=maximum,
        decimal_places=decimal_places,
    )


def _decimal(raw: Any, *, where: str, label: str) -> Decimal | None:
    """把 spec 里的一个边界值折成 `Decimal`。

    走字符串再进 `Decimal`,不走 `float`:YAML 把 `minimum: 0.1` 解析成
    float,而 `Decimal(0.1)` 是 `0.1000000000000000055511151231257827…`。
    那个尾巴会让一个恰好等于下界的价格被判成越界,且报错信息里
    看到的仍然是 `0.1` —— 没有人能从那条信息里看出问题在哪。
    """
    if raw is None:
        return None
    try:
        value = Decimal(str(raw).strip())
    except InvalidOperation as exc:
        raise ChannelSpecError(f"{where}: {label} 不是合法的数字:{raw!r}") from exc
    if not value.is_finite():
        raise ChannelSpecError(f"{where}: {label} 必须是有限数,收到 {raw!r}")
    return value


def _numeric_rules(
    raw: Mapping[str, Any], *, where: str
) -> tuple[str | None, Decimal | None, Decimal | None, int | None]:
    """解析并**交叉校验** `type` / `minimum` / `maximum` / `decimal_places`。

    这里对「约束写了但不生效」的组合一律拒绝加载,而不是忽略。
    忽略一条约束的运行期表现是「这一列没被校验」,而没被校验的列
    要到平台驳回时才会被发现 —— 和本模块开头列的其他几条是同一个理由。
    """
    value_type = raw.get("type")
    if value_type is not None:
        value_type = str(value_type).strip().lower()
        if value_type not in FIELD_TYPES:
            raise ChannelSpecError(
                f"{where}: 未知的 type {value_type!r};"
                f"只允许 {', '.join(sorted(FIELD_TYPES))}"
            )

    minimum = _decimal(raw.get("minimum"), where=where, label="minimum")
    maximum = _decimal(raw.get("maximum"), where=where, label="maximum")

    decimal_places = raw.get("decimal_places")
    if decimal_places is not None:
        try:
            decimal_places = int(decimal_places)
        except (TypeError, ValueError) as exc:
            raise ChannelSpecError(
                f"{where}: decimal_places 必须是整数,收到 {raw.get('decimal_places')!r}"
            ) from exc
        if decimal_places < 0:
            raise ChannelSpecError(f"{where}: decimal_places 不能为负数")

    declared = [
        name
        for name, value in (
            ("minimum", minimum),
            ("maximum", maximum),
            ("decimal_places", decimal_places),
        )
        if value is not None
    ]
    if declared and value_type not in NUMERIC_FIELD_TYPES:
        raise ChannelSpecError(
            f"{where}: {', '.join(declared)} 只能配在 "
            f"{' / '.join(sorted(NUMERIC_FIELD_TYPES))} 字段上,"
            f"当前 type 是 {value_type!r}"
        )
    if decimal_places and value_type == "integer":
        raise ChannelSpecError(
            f"{where}: integer 字段不能声明 decimal_places"
        )
    if minimum is not None and maximum is not None and minimum > maximum:
        raise ChannelSpecError(
            f"{where}: minimum({minimum}) 大于 maximum({maximum}),这条约束不可能被满足"
        )

    return value_type, minimum, maximum, decimal_places


def load_spec_dict(
    data: Mapping[str, Any], *, site: str, category_id: str, allow_todo: bool = False
) -> ChannelFieldSpec:
    """把一份已解析的 YAML 变成 `ChannelFieldSpec`,顺带全量校验。"""
    channel = str(data.get("channel") or "").strip()
    if not channel:
        raise ChannelSpecError("spec 缺少 channel")

    sites = [str(s) for s in (data.get("sites") or ())]
    if sites and site not in sites:
        raise ChannelSpecError(
            f"spec 未声明站点 {site}", channel=channel, declared=sites
        )

    header = tuple(
        _field_from_dict(f, where="header_fields", allow_variant=False)
        for f in (data.get("header_fields") or ())
    )
    rows = tuple(
        _field_from_dict(f, where="row_fields", allow_variant=True)
        for f in (data.get("row_fields") or ())
    )

    keys = [f.key for f in (*header, *rows)]
    duplicates = sorted({k for k in keys if keys.count(k) > 1})
    if duplicates:
        raise ChannelSpecError(f"字段 key 重复:{', '.join(duplicates)}")

    # ---- 规则包三项(PRD v2 §18.2),在加载时就校验 ----
    #
    # 硬错误码写错一个字母的话,评分层查启用清单永远查不到它 ——
    # 表现是那条硬错误静默失效,而 spec 文件看上去毫无问题。
    # 受众取值同理:一个写成 "WOMAN" 的 spec 会让 §17 的一致性断言
    # 把该品类下所有商品都判成受众不一致。**加载即拒绝**,不留到运行期。
    audience_raw = data.get("audience")
    audience: str | None = None
    if audience_raw is not None:
        from app.core.enums import Audience

        try:
            audience = Audience(str(audience_raw).strip().upper()).value
        except ValueError:
            raise ChannelSpecError(
                f"spec 的 audience 取值不合法:{audience_raw!r}"
                f"(可选:{', '.join(a.value for a in Audience)})",
                channel=channel,
            ) from None

    enabled_codes = tuple(
        str(c).strip().upper() for c in (data.get("enabled_hard_fail_codes") or ())
    )
    if enabled_codes:
        from app.core.enums import HardFailCode

        known = {c.value for c in HardFailCode}
        unknown_codes = sorted(set(enabled_codes) - known)
        if unknown_codes:
            raise ChannelSpecError(
                "spec 的 enabled_hard_fail_codes 里有未注册的码:"
                + ", ".join(unknown_codes),
                channel=channel,
            )

    spec = ChannelFieldSpec(
        channel=channel,
        category_id=category_id,
        site=site,
        spec_version=str(data.get("spec_version") or "0"),
        header_fields=header,
        row_fields=rows,
        manual_fields=tuple(str(m) for m in (data.get("manual_fields") or ())),
        consts=dict(data.get("consts") or {}),
        audience=audience,
        enabled_hard_fail_codes=enabled_codes,
        review_checks=tuple(str(c) for c in (data.get("review_checks") or ())),
    )

    todos = spec.todos()
    if todos and not allow_todo:
        raise ChannelSpecError(
            "spec 里仍有未从官方文档确认的字段,拒绝加载",
            code=ErrorCode.CHANNEL_SPEC_INCOMPLETE,
            channel=channel,
            todo_fields=[f.key for f in todos][:10],
        )
    return spec


def load_spec_file(
    path: Path, *, site: str, category_id: str, allow_todo: bool = False
) -> ChannelFieldSpec:
    if not path.exists():
        raise ChannelSpecError(f"spec 文件不存在:{path}")
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, Mapping):
        raise ChannelSpecError(f"spec 文件顶层必须是对象:{path}")
    return load_spec_dict(
        data, site=site, category_id=category_id, allow_todo=allow_todo
    )
