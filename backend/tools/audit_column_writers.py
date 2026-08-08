#!/usr/bin/env python3
"""每一列都要回答「谁写它」。**零三方依赖,只读文件,跑完不到一秒。**

## 它钉的是哪一件事

`docs/DECISIONS.md` §3.38 把一类洞点了名:**一列新增的、被判定读取的列,
落库的那一批必须同时回答「谁写它」。** 这个仓库为它付过两次代价:

    media_assets.color_variant_id     躲了五批(14-22 才补上写入路径)
    §4.8 新去重键                     躲了六批(14-23 才有人记账)

两次都是**人逐条对着 PRD 核才核出来的**。第三次不会有人这么核 —— 那正是
这份审计存在的理由。

## 判据是「有没有人写」,不是「有没有人读」

第一版按"有读无写"筛,读那一半噪声极大:`checked.note` 与
`ListingImageSet.note` 在同一个文件里长得一模一样,`GenerationCandidate`
在 `providers/base.py` 是 dataclass、在 `models/generation.py` 是模型。
**按名字撞出来的\"读\"不是读。**

所以判据只留写:一列在全仓找不到任何写入点,它就该被点名 —— 无论今天
有没有人读它。没人读的无写列同样是问题(它是一列永远为默认值的数据,
而 schema 里它看起来像是有内容的)。

## 认哪几种写

    Model(col=...)              构造点关键字
    Model(rel=...)              关系赋值。ORM 在 flush 时写外键列,
                                所以传关系 = 写它背后的那一列
    obj.col = ... / obj.col += 属性赋值
    setattr(obj, "col", ...)    常量名的 setattr
    .values(col=...)            Core 层 update/insert

## 认不了的,一律把整个模型划成「判不了」,不猜

    Model(**payload)            构造点收 **kwargs —— 哪些列被写取决于运行时
    Model(**payload)            构造点收 **kwargs —— 哪些列被写取决于运行时

划出去的模型会单独列出来。**把判不了的说成判过了,比不判更糟** ——
那会让这份审计变成一张假的全绿清单。

## `setattr(obj, name, ...)` 里 name 是变量时怎么办

第一版把这种情况判成"整个仓库都判不了" —— 全仓四处动态 setattr,
其中一处就让 40 个模型全部出局,审计输出「0 列答得出」。**一份把所有
对象都划成判不了的审计,和没有这份审计完全等价。**

实际情况是这四处的键都来自附近的字面量(`_outcome_values` 那个 dict、
入参白名单、payload 键表)。所以口径改成:**与这次动态 setattr 处在
同一个函数里、以字符串字面量出现过的列名,算被它写了。**

范围取函数而不是文件,是因为按文件取会把无关的字面量也算进来 ——
`batch_service.py` 里 `"reused"` 出现在出参组装那一段,与落库的
`_outcome_values` 隔着一千行,把它算成写入会**漏掉一列真的没人写的列**。

这是一个明确偏"多认写入"的近似 —— 它会漏掉列,不会造出假红。理由与
上一段同:漏一列的代价是这份审计少报一次,假红的代价是有人把整条门禁
删掉(§3.37 第一版当场踩过)。

## 已知的失真方向(向"少报"偏)

属性赋值按名字认,不解析接收者的类型。于是同名属性(`x.note = ...` 而
`x` 不是那个模型)会被当成写入 —— **这个方向是刻意选的**:多认一次写的
代价是漏掉一列,少认一次的代价是一条假红,而假红最省事的消法是把整条
门禁删掉(§3.37 第一版当场踩过)。

## 豁免表怎么用

`LEDGER` 里每一条都要写清"为什么这一列可以没有写入点"。豁免不是白名单
—— **它自己也会红**:某一列一旦有了写入点,它的豁免条目就失效并被点名,
必须删掉。豁免表因此不会烂在那里。

用法:python3 tools/audit_column_writers.py    # 或 make audit-columns
退出码非零 = 有列答不出「谁写它」。
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
APP = BACKEND / "app"
MODELS = APP / "models"

#: 基类/混入提供的列。它们由 `Base` / `TimestampMixin` / `UUIDPrimaryKeyMixin`
#: 在实例化时写,构造点不传是正常的
BASE_MANAGED = {"id", "created_at", "updated_at"}

#: `"Model.col"` -> 为什么它今天可以没有写入点。**每一条都要能被复核。**
#:
#: 这不是白名单,是**台账**。两条自我保护:
#:
#:   1. 某一列一旦有了写入点,它的条目就失效并被点名,必须删掉 ——
#:      台账因此不会烂在那里(§3.37 说的"自翻转"那一半)
#:   2. 属于真欠账的条目要写还款日,读法与 `docs/STATUS.md` 的
#:      `DELIVERY_STAGE` 一致:阶段 N 之前必须还清
#:
#: 分三类,写清是哪一类比写清理由更要紧 —— 类别决定了谁该去处理它。
LEDGER: dict[str, str] = {
    # ---- 一、整套特性未接线。列存在是因为 schema 先行,没有调用方 ----
    "MediaConsent.subject_type": "肖像授权整表未接线:全仓除模型外零引用",
    "MediaConsent.subject_ref": "肖像授权整表未接线",
    "MediaConsent.scope": "肖像授权整表未接线",
    "MediaConsent.allowed_regions": "肖像授权整表未接线",
    "MediaConsent.allowed_processors": "肖像授权整表未接线",
    "MediaConsent.training_opt_out": "肖像授权整表未接线",
    "MediaConsent.valid_from": "肖像授权整表未接线",
    "MediaConsent.valid_until": "肖像授权整表未接线",
    "MediaConsent.document_ref": "肖像授权整表未接线",
    "MediaAsset.training_opt_out": "肖像授权未接线,素材侧那一半;接线时与 MediaConsent 一起落",
    "AttributeEvidence.region_json": "证据框选(§4.4 图上画框)未接线,无人读也无人写",
    # ---- 二、运维灌数据,不由应用写 ----
    "AttributeCalibration.bin_lower": "整张表由运维灌数据(见 /attributes/calibration 的文档)",
    "AttributeCalibration.bin_upper": "整张表由运维灌数据",
    "AttributeCalibration.observed_accuracy": "整张表由运维灌数据",
    "AttributeCalibration.sample_count": "整张表由运维灌数据",
    "AttributeCalibration.calibration_version": "整张表由运维灌数据",
    "AttributeCalibration.notes": "整张表由运维灌数据",
    # ---- 三、真欠账:有人读、有人显示,而值恒定。**还款日:阶段 5** ----
    # `ColorVariant.display_name` **曾经在这里**(还款日:阶段 5)。
    # A45-batch23 接上了写入点(`attributes/service._project_colour_name`),
    # 这一条随之删除 —— 台账只记还没有写入路径的列。
    #
    # 顺带订正一句躲了几批的话:旧条目说触发字段是 `standard_color_name`,而注册表里是 `primary_color`。
    # 全仓没有前者这个字段。照那句话去接线的人
    # 会先找那个字段、找不到,然后得出"还缺前置"的结论 —— 这正是这笔账
    # 躲过去的方式。
    "PublishAttempt.provider_request_id": (
        "还款日:阶段 5。列注释写着「出事时这是唯一能和平台对账的东西」,"
        "而 PublishPage 上它恒显示 —— 。今天只有 Simulator transport,"
        "接第一个真实渠道时必须同批落"
    ),
    "ProductAttributeValue.calibration_version": (
        "还款日:阶段 5。恒为 NULL,于是 confidence 那一侧永远拿不到"
        "「这条事实按哪一版校准算的」。与 AttributeCalibration 灌数据是同一件事的两端"
    ),
    "ProductAttributeValue.schema_version": (
        "还款日:阶段 5。恒为 '1'。它存在的意义是 value_json 形状改版时能分辨新旧,"
        "而恒定的版本号让那次改版无法分辨"
    ),
    "ListingDraft.template_checksum": (
        "还款日:阶段 5。恒为 NULL,于是「模板改过了、草稿要重出」判不出来"
    ),
    # 迁移 0049 的两列(`ListingDraft.upstream_versions` /
    # `.color_sku_image_map`)**曾经在这里**,是本仓第一次在落列的同一批
    # 就记账的欠账(5-1)。A45-batch20 把 `build_draft` 的写入点接上之后
    # 两条已删除 —— 台账只记**还没有写入路径**的列,一条还清的账留在这里
    # 会让下一个读它的人以为缺口还开着(§3.42:说缺口已关比说过期贵,
    # 而反过来说一个已关的缺口还开着,代价是让人重做一遍已经做完的事)。
    "ModelTemplate.disabled_reason": (
        "还款日:阶段 5。前端有显示位,而停用原因恒为 NULL —— 运营看到模板不可用,"
        "看不到为什么"
    ),
    "ListingImageSet.note": "还款日:阶段 5。备注字段无入口,恒为 NULL",
    "GenerationCandidate.height": (
        "还款日:阶段 5。`width` 有写入点而 `height` 没有 —— 两列本该成对。"
        "Provider 侧回传的尺寸只落了一半"
    ),
    "ProductAttributeExtraction.prompt_tokens": (
        "还款日:阶段 5。识别侧 token 计量未接线,四列一起"
    ),
    "ProductAttributeExtraction.completion_tokens": (
        "还款日:阶段 5。识别侧 token 计量未接线,四列一起"
    ),
    "ProductAttributeExtraction.raw_response_ref": (
        "还款日:阶段 5。识别侧原始响应留存未接线,四列一起"
    ),
    "ProductAttributeExtraction.raw_response_summary": (
        "还款日:阶段 5。识别侧原始响应留存未接线,四列一起"
    ),
}


class Model:
    __slots__ = ("name", "columns", "relationships", "unknowable", "defaulted")

    def __init__(self, name: str) -> None:
        self.name = name
        self.columns: dict[str, int] = {}
        #: 带 `default=` / `server_default=` 的列。没有写入点时它们恒等于那个
        #: 默认值;**不带默认的那些恒为 NULL**,后者更严重也更难看出来
        self.defaulted: set[str] = set()
        #: 关系名 -> 它背后的外键列名。传关系等于写那一列
        self.relationships: dict[str, str] = {}
        self.unknowable: str | None = None


def _foreign_key_of(call: ast.Call) -> str | None:
    """`relationship(..., foreign_keys=[X.col])` / `back_populates` 都认不出列。

    真正能连上的是 `ForeignKey("table.col")` 那一侧,而它在**列**上不在关系上。
    所以这里走反向:同一个类里,如果某一列带 `ForeignKey(...)`,而某个关系的
    名字是那一列去掉 `_id` 的形状,就认为传关系会写那一列。

    这是启发式,方向同样偏"少报"。
    """
    return None


def _function_index(tree: ast.Module) -> dict[int, str]:
    """节点 id -> 它所在的函数名。一遍构好,查询 O(1)。

    第一版是每次查询重走一遍树,`app/` 全量跑了 42 秒 —— 一份要进 CI 的
    审计跑 42 秒,等于没人会在提交前跑它。
    """
    index: dict[int, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for child in ast.walk(node):
            index.setdefault(id(child), node.name)
    return index


def collect_models() -> dict[str, Model]:
    models: dict[str, Model] = {}
    for path in sorted(MODELS.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for cls in [n for n in ast.walk(tree) if isinstance(n, ast.ClassDef)]:
            model = Model(cls.name)
            for stmt in cls.body:
                if not (isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)):
                    continue
                value = stmt.value
                if not isinstance(value, ast.Call):
                    continue
                func = getattr(value.func, "id", "") or getattr(value.func, "attr", "")
                if func == "mapped_column":
                    model.columns[stmt.target.id] = stmt.lineno
                    kwargs = {kw.arg for kw in value.keywords}
                    if "default" in kwargs or "server_default" in kwargs:
                        model.defaulted.add(stmt.target.id)
                elif func == "relationship":
                    model.relationships[stmt.target.id] = ""
            if model.columns:
                # 关系名 `spu` 对上列名 `spu_id`
                for rel in list(model.relationships):
                    if f"{rel}_id" in model.columns:
                        model.relationships[rel] = f"{rel}_id"
                models[cls.name] = model
    return models


def collect_writes(models: dict[str, Model]) -> set[str]:
    """返回 `{"Model.col"}`。同时给收 `**kwargs` 的模型打上 unknowable。"""
    written: set[str] = set()
    dynamic: set[tuple[Path, str]] = set()
    literals: dict[tuple[Path, str], set[str]] = {}
    by_column: dict[str, list[str]] = {}
    for model in models.values():
        for col in model.columns:
            by_column.setdefault(col, []).append(model.name)

    for path in sorted(APP.rglob("*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        if MODELS in path.parents:
            continue
        where = _function_index(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
                model = models.get(func)
                if model is not None:
                    if any(kw.arg is None for kw in node.keywords):
                        model.unknowable = f"{path.relative_to(BACKEND)} 用 **kwargs 构造"
                    for kw in node.keywords:
                        if kw.arg in model.columns:
                            written.add(f"{model.name}.{kw.arg}")
                        elif kw.arg in model.relationships and model.relationships[kw.arg]:
                            written.add(f"{model.name}.{model.relationships[kw.arg]}")
                if func == "values":
                    for kw in node.keywords:
                        for owner in by_column.get(kw.arg or "", []):
                            written.add(f"{owner}.{kw.arg}")
                if getattr(node.func, "id", "") == "setattr" and len(node.args) >= 2:
                    target = node.args[1]
                    if isinstance(target, ast.Constant) and isinstance(target.value, str):
                        for owner in by_column.get(target.value, []):
                            written.add(f"{owner}.{target.value}")
                    else:
                        # 动态 setattr:把**同一函数**里出现过的列名字面量算作被写。
                        # 见模块顶部那一段 —— 这是刻意偏"多认写入"的近似
                        dynamic.add((path, where.get(id(node), "<module>")))
            targets: list[ast.expr] = []
            if isinstance(node, ast.Assign):
                targets = list(node.targets)
            elif isinstance(node, (ast.AugAssign, ast.AnnAssign)):
                targets = [node.target]
            for target in targets:
                if isinstance(target, ast.Attribute):
                    for owner in by_column.get(target.attr, []):
                        written.add(f"{owner}.{target.attr}")
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                key = (path, where.get(id(node), "<module>"))
                literals.setdefault(key, set()).add(node.value)

    for key in dynamic:
        for name in literals.get(key, set()):
            for owner in by_column.get(name, []):
                written.add(f"{owner}.{name}")
    return written


class Report:
    """审计的结论。**`main()` 只负责把它印出来,不重新判一次。**

    分成"算"和"印"两半,是为了让守卫能直接算 —— 第一版把判定写在
    `main()` 里,于是"台账条目失效会不会被报出来"这件事只能靠读源码断言,
    而读源码的断言在判定被搬走时会安静地继续绿。
    """

    __slots__ = ("orphans", "skipped", "stale", "covered")

    def __init__(self) -> None:
        #: (Model.col, 行号, 有没有默认值)
        self.orphans: list[tuple[str, int, bool]] = []
        self.skipped: list[str] = []
        #: 台账里已经不成立的条目 —— 那一列有写入点了
        self.stale: list[str] = []
        self.covered = 0

    @property
    def ok(self) -> bool:
        return not self.orphans and not self.stale


def report(ledger: dict[str, str] | None = None) -> Report:
    ledger = LEDGER if ledger is None else ledger
    models = collect_models()
    written = collect_writes(models)
    out = Report()

    for model in sorted(models.values(), key=lambda m: m.name):
        if model.unknowable:
            out.skipped.append(f"{model.name}（{model.unknowable}）")
            continue
        out.covered += len(model.columns)
        for col, lineno in sorted(model.columns.items()):
            key = f"{model.name}.{col}"
            if col in BASE_MANAGED:
                continue
            if key in written:
                if key in ledger:
                    out.stale.append(key)
                continue
            if key in ledger:
                continue
            out.orphans.append((key, lineno, col in model.defaulted))
    return out


def main() -> int:
    out = report()

    if out.skipped:
        print("判不了的模型（构造点收 **kwargs 或用变量名 setattr）:")
        for line in out.skipped:
            print(f"    {line}")
        print()

    if out.stale:
        print("台账条目已失效——这些列有写入点了，请从 LEDGER 里删掉:")
        for key in out.stale:
            print(f"    {key}")
        print()

    if out.orphans:
        always_null = [o for o in out.orphans if not o[2]]
        always_default = [o for o in out.orphans if o[2]]
        if always_null:
            print("这些列答不出「谁写它」，**而且没有默认值——它们恒为 NULL**:")
            for key, lineno, _ in always_null:
                print(f"    {key}  (models/…:{lineno})")
            print()
        if always_default:
            print("这些列答不出「谁写它」，有默认值——它们恒等于那个默认值:")
            for key, lineno, _ in always_default:
                print(f"    {key}  (models/…:{lineno})")
            print()

    if not out.ok:
        print(
            "每一条要么补上写入路径，要么进 LEDGER 并写清为什么、以及还款日。\n"
            "一列没有写入点时它恒等于默认值，而 schema 里它看起来像是有内容的——\n"
            "这类失效不会报错，只会让读它的那一侧永远拿到同一个答案。"
        )
        return 1

    print(
        f"{out.covered} 列都答得出「谁写它」"
        f"（{len(LEDGER)} 条在台账上，{len(out.skipped)} 个模型判不了）"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
