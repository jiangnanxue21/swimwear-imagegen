"""A45-batch14-2:对 batch14 交付包(含合入的 batch13-3)的走读修复。

## 这一批修的四条

    F1  `plan_evidence` 不按字段名去重 —— 模型在同一份响应里把一个字段
        答两遍(`fields` 是数组,Schema 挡不住重复项,而解析器明说
        "只解析不过滤"),这一张图就投了两票。而 `extractors/merge.py`
        的分歧阈值 0.34 是按「三张图里有一张不同意」定的,那条推理
        **假设一图一票**:两条重复证据能把 3:1(通过)变成 3:2(CONFLICT)
    F2  `missing_reason` 没有任何界面渲染 —— 后端从模型一路铺到出参,
        `EvidenceOut` 的注释直接指定「界面按它区分」,而前端连类型都没有它。
        这是 batch13-3 / R4 的原样重演,发生在同一天的另一个批次上
    F3  单次识别上限的兜底只长在 `over_call_budget()` 内部 —— 于是
        **判定用 12、提示语用 0**:`.env` 里写 `EXTRACTOR_MAX_IMAGES_PER_RUN=0`
        时闸按默认值拦人,拒绝信息却告诉运营"一次识别最多 0 张图",
        没有任何批量满足得了那句话
    F4  batch13-3 / R1 的规矩写成了全称句(「凡是 ondelete=RESTRICT 的外键,
        反向关系必须带 passive_deletes="all"」),守卫却只点名了
        `ColorVariant.skus` 一个关系 —— 规矩活在散文里,不活在不变式里,
        而这正是 `_is_identity_column()` 自己骂过的"第二张名单"

## 守卫为什么长这个样子

F1 是纯函数,直接调用穷举边界 —— 不用 AST。
F3 同理。F2 只能钉文本(无 node_modules,`tsc` 跑不了),按 batch13-3 / M11
的教训**锚在 JSX 表达式的开括号上**,不是数字符串出现次数。
F4 是本批的重点:它不点名任何一个关系,而是**遍历模型层**自己找出
"跨 RESTRICT 外键的反向集合"再逐个断言 —— 新加的表自动进入覆盖,
这正是 R1 那条规矩需要的形状。
"""
from __future__ import annotations

import ast
import re

from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT

MODELS_DIR = BACKEND_ROOT / "app" / "models"
ATTRIBUTE_TAB = (
    PROJECT_ROOT / "frontend" / "src" / "components" / "workbench" / "AttributeTab.tsx"
)
ATTRIBUTES_API = PROJECT_ROOT / "frontend" / "src" / "api" / "attributes.ts"


# ================================================================ F1 一图一票


class _Obs:
    def __init__(self, name, value=None, confidence=None, evidence=None):
        self.name = name
        self.value = value
        self.confidence = confidence
        self.evidence = evidence


class _Missing:
    def __init__(self, name, reason):
        self.name = name
        self.reason = reason


class _Result:
    def __init__(self, fields=(), unreadable_fields=(), missing=()):
        self.fields = tuple(fields)
        self.unreadable_fields = tuple(unreadable_fields)
        self.missing = tuple(missing)


TARGETS = ("neckline", "secondary_colors")


def test_one_image_casts_one_vote_per_field():
    """同一个字段在一张图里出现两次,只能产生**一条**证据。

    这是这条修复的全部要害。`merge_field()` 把每条证据当一票
    (`scores[value] += weight`),没有任何按素材去重的环节 ——
    所以"一张图一票"这个前提只能在这里成立,成立不了就没有第二道防线。
    """
    from app.extractors.evidence_plan import plan_evidence

    plan = plan_evidence(
        _Result(fields=(_Obs("neckline", "V_NECK", 0.9), _Obs("neckline", "V_NECK", 0.3))),
        TARGETS,
    )
    rows = [e for e in plan.evidence if e.field_name == "neckline"]
    assert len(rows) == 1, f"同名同值应折成一条,实际 {len(rows)} 条 —— 一张图投了多票"


def test_a_repeated_identical_value_is_not_counted_as_fabrication():
    """重复不是编造,只是啰嗦。第一条的置信度与理由留下。

    把重复计进 `fabricated_field_count` 会污染 §6.3 那个指标 ——
    它测量的是"模型编了没问它的字段",不是"模型话多"。
    """
    from app.extractors.evidence_plan import plan_evidence

    plan = plan_evidence(
        _Result(
            fields=(
                _Obs("neckline", "V_NECK", 0.9, "领口呈 V 形"),
                _Obs("neckline", "V_NECK", 0.3),
            )
        ),
        TARGETS,
    )
    assert plan.fabricated == 0, "同名同值被算成了编造"
    row = next(e for e in plan.evidence if e.field_name == "neckline")
    assert row.value == "V_NECK"
    assert row.confidence == 0.9, "留下的应当是第一条(确定行为,不是随手挑)"
    assert row.evidence_text == "领口呈 V 形"


def test_a_self_contradicting_field_yields_no_value_not_a_coin_flip():
    """同名异值:两个值都不要,落一条 INSUFFICIENT_EVIDENCE 的无值证据。

    挑哪一个都是替模型做决定,而它自己给出了两个互斥的答案。
    与规则 4(既给值又说没有值 -> 没有值胜出)同源。
    """
    from app.extractors.evidence_plan import (
        REASON_INSUFFICIENT_EVIDENCE,
        plan_evidence,
    )

    plan = plan_evidence(
        _Result(fields=(_Obs("neckline", "V_NECK", 0.9), _Obs("neckline", "ROUND_NECK", 0.8))),
        TARGETS,
    )
    rows = [e for e in plan.evidence if e.field_name == "neckline"]
    assert len(rows) == 1
    assert rows[0].value is None, "自相矛盾时不许挑一个值出来"
    assert rows[0].missing_reason == REASON_INSUFFICIENT_EVIDENCE
    assert plan.fabricated == 1, "多出来的那条要计入编造(其中至少一个值是造的)"


def test_duplicate_detection_handles_unhashable_values():
    """列表值字段(`secondary_colors`)也要去重 —— 不能用集合去做。

    `{["red"]}` 直接 TypeError;拿 repr 比字典又是键序敏感的假阴性。
    这条用例钉的是"实现方式不能退化成 set/repr"。
    """
    from app.extractors.evidence_plan import plan_evidence

    same = plan_evidence(
        _Result(
            fields=(
                _Obs("secondary_colors", ["red", "blue"]),
                _Obs("secondary_colors", ["red", "blue"]),
            )
        ),
        TARGETS,
    )
    rows = [e for e in same.evidence if e.field_name == "secondary_colors"]
    assert len(rows) == 1 and rows[0].value == ["red", "blue"]
    assert same.fabricated == 0

    differ = plan_evidence(
        _Result(
            fields=(_Obs("secondary_colors", ["red"]), _Obs("secondary_colors", ["blue"]))
        ),
        TARGETS,
    )
    rows = [e for e in differ.evidence if e.field_name == "secondary_colors"]
    assert len(rows) == 1 and rows[0].value is None


def test_rule_four_still_wins_over_the_new_dedup():
    """模型说"看不清"时,重复的带值观测仍然全部计入编造、全部不落值。

    新加的去重不能把规则 4 挤掉:两条改动落在同一个循环里,
    而"后加的分支悄悄改变了前一条规则"是这一类修改最常见的塌陷。
    """
    from app.extractors.evidence_plan import plan_evidence

    plan = plan_evidence(
        _Result(
            fields=(_Obs("neckline", "V_NECK"), _Obs("neckline", "V_NECK")),
            unreadable_fields=("neckline",),
        ),
        TARGETS,
    )
    rows = [e for e in plan.evidence if e.field_name == "neckline"]
    assert len(rows) == 1 and rows[0].missing_reason is not None
    assert plan.fabricated == 2, "两条越权带值的观测都要计数"


def test_dedup_does_not_reorder_the_surviving_evidence():
    """去重不改变字段的出现顺序。

    顺序本身不参与判定,但它决定了运营在证据列表里看到的排列;
    用 dict 归拢时顺手把顺序丢掉是这类重构的经典副作用。
    """
    from app.extractors.evidence_plan import plan_evidence

    plan = plan_evidence(
        _Result(
            fields=(
                _Obs("secondary_colors", ["red"]),
                _Obs("neckline", "V_NECK"),
                _Obs("secondary_colors", ["red"]),
            )
        ),
        TARGETS,
    )
    names = [e.field_name for e in plan.evidence]
    assert names == ["secondary_colors", "neckline"], f"顺序变了:{names}"


# ================================================================ F3 生效上限


def test_the_effective_ceiling_is_a_named_function_both_sides_read():
    """兜底不许长在判定内部 —— 提示语必须报闸真正在用的那个数。

    `.env` 里的 `EXTRACTOR_MAX_IMAGES_PER_RUN=0` 绕得过设置页的
    `minimum=1`(配置有三条来源,只有一条经过那张表单)。
    """
    from app.extractors.call_budget import (
        DEFAULT_MAX_IMAGES_PER_RUN,
        effective_ceiling,
        over_budget_message,
    )

    assert effective_ceiling(0) == DEFAULT_MAX_IMAGES_PER_RUN
    assert effective_ceiling(-5) == DEFAULT_MAX_IMAGES_PER_RUN
    assert effective_ceiling(3) == 3

    message = over_budget_message(image_count=40, ceiling=0)
    assert str(DEFAULT_MAX_IMAGES_PER_RUN) in message, (
        "上限配成 0 时提示语报的还是 0 —— 那句话没有任何批量满足得了"
    )
    assert "最多 0 张" not in message


def test_the_gate_and_the_message_never_disagree():
    """闸说超了,提示语里的数字就必须真的小于本次张数。

    这条比上一条强:它不认某个具体默认值,只认"两边同一个数"。
    """
    from app.extractors.call_budget import over_budget_message, over_call_budget

    for ceiling in (-3, 0, 1, 5, 12, 100):
        for count in (1, 5, 13, 500):
            if not over_call_budget(image_count=count, ceiling=ceiling, billable=True):
                continue
            message = over_budget_message(image_count=count, ceiling=ceiling)
            reported = int(re.search(r"最多 (\d+) 张", message).group(1))
            assert reported < count, (
                f"闸拦了 {count} 张,提示语却说上限是 {reported} —— 两边不是同一个数"
            )


# ================================================================ F4 全树不变式


def _model_classes() -> dict[str, dict]:
    """从 `app/models/*.py` 抽出每个类的表名、外键、关系。**不 import。**

    这台机器没有 sqlalchemy,而这条不变式恰恰是关于 sqlalchemy 行为的 ——
    所以只能读源码。读源码也有好处:它同时覆盖那些因为缺依赖而
    永远 import 不起来的模块。
    """
    classes: dict[str, dict] = {}
    for path in sorted(MODELS_DIR.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.ClassDef):
                continue
            info = {
                "file": path.name,
                "tablename": None,
                "fks": [],          # (列名, 目标表, ondelete)
                "relationships": {},  # 属性名 -> {back_populates, passive_deletes}
            }
            for stmt in node.body:
                # __tablename__ = "..."
                if isinstance(stmt, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "__tablename__"
                    for t in stmt.targets
                ):
                    if isinstance(stmt.value, ast.Constant):
                        info["tablename"] = stmt.value.value
                if not isinstance(stmt, ast.AnnAssign) or not isinstance(
                    stmt.target, ast.Name
                ):
                    continue
                attr = stmt.target.id
                call = stmt.value
                if not isinstance(call, ast.Call):
                    continue
                fname = getattr(call.func, "id", getattr(call.func, "attr", ""))
                if fname == "relationship":
                    kws = {k.arg: k.value for k in call.keywords}
                    info["relationships"][attr] = {
                        "back_populates": (
                            kws["back_populates"].value
                            if isinstance(kws.get("back_populates"), ast.Constant)
                            else None
                        ),
                        "passive_deletes": (
                            kws["passive_deletes"].value
                            if isinstance(kws.get("passive_deletes"), ast.Constant)
                            else None
                        ),
                    }
                elif fname == "mapped_column":
                    for arg in call.args:
                        if not isinstance(arg, ast.Call):
                            continue
                        if getattr(arg.func, "id", getattr(arg.func, "attr", "")) != (
                            "ForeignKey"
                        ):
                            continue
                        target = (
                            arg.args[0].value
                            if arg.args and isinstance(arg.args[0], ast.Constant)
                            else ""
                        )
                        ondelete = next(
                            (
                                k.value.value
                                for k in arg.keywords
                                if k.arg == "ondelete"
                                and isinstance(k.value, ast.Constant)
                            ),
                            None,
                        )
                        info["fks"].append((attr, str(target).split(".")[0], ondelete))
            if info["tablename"] or info["relationships"] or info["fks"]:
                classes[node.name] = info
    return classes


def _restrict_backrefs() -> list[tuple[str, str, str | None]]:
    """全树的「跨 RESTRICT 外键的反向集合」。返回 (类名, 属性名, passive_deletes)。

    判据是**双向**匹配,不是"这个类上有 RESTRICT 外键"那种近似:
    P.R 的 back_populates 指向 C.A,C.A 的 back_populates 指回 R,
    并且 C 上存在一条指向 P 表、ondelete=RESTRICT 的外键。
    """
    classes = _model_classes()
    by_table = {
        info["tablename"]: name for name, info in classes.items() if info["tablename"]
    }
    found = []
    for parent, pinfo in classes.items():
        for attr, rel in pinfo["relationships"].items():
            child_attr = rel["back_populates"]
            if not child_attr:
                continue
            for child_info in classes.values():
                back = child_info["relationships"].get(child_attr)
                if not back or back["back_populates"] != attr:
                    continue
                restricts = [
                    fk
                    for fk in child_info["fks"]
                    if fk[2] == "RESTRICT"
                    and by_table.get(fk[1]) == parent
                ]
                if restricts:
                    found.append((parent, attr, rel["passive_deletes"]))
    return found


def test_every_restrict_backref_leaves_the_children_to_the_database():
    """§3.25 第一节的规矩,做成全树不变式而不是一张点名单。

    batch13-3 / R1 写下的是全称句,守卫却只点了 `ColorVariant.skus`。
    今天全树只有那一条命中,所以代码是对的 —— 但**下一个**给 RESTRICT
    外键加反向集合的人不会被拦住,而那正是 R1 那个缺陷的来源。

    这条用例不认任何具体表名:模型层加一张表,它自动进入覆盖。
    """
    offenders = [
        f"{cls}.{attr}(passive_deletes={value!r})"
        for cls, attr, value in _restrict_backrefs()
        if value != "all"
    ]
    assert not offenders, (
        "这些反向集合跨在 ondelete=RESTRICT 的外键上,却没有 "
        'passive_deletes="all" —— 删父行时 SQLAlchemy 会先把子行外键置 NULL,'
        f"RESTRICT 根本没机会响:{offenders}"
    )


def test_the_invariant_actually_finds_the_known_case():
    """不变式必须**够得着** —— 一条命中都没有的不变式是绿着装样子。

    batch13 的 M9(永远触发不了的上限)与 batch13-3 的 M11(锚错的正则)
    是同一个教训:恒不触发的保护比没有更糟。
    """
    hits = {(cls, attr) for cls, attr, _ in _restrict_backrefs()}
    assert ("ColorVariant", "skus") in hits, (
        "扫描没有找到已知的那一条 —— 说明这条不变式对任何新表也不会生效"
    )


# ================================================================ F2 无值上屏


def test_the_missing_reason_is_typed_on_the_frontend():
    """后端出参里有的字段,前端类型里必须有 —— 否则谁也用不了它。"""
    src = ATTRIBUTES_API.read_text(encoding="utf-8")
    # 整行锚定,行首只允许空白。**第一版没锚,`// missing_reason: string | null`
    # 照样匹配 —— 变异验证当场抓到(N15 第一轮 GREEN)。**
    # 这是本批第三次撞上同一个陷阱(batch13-3 的 M2 与 M11 是前两次):
    # "文件里出现过这串字"从来不等于"这行代码在生效"
    assert re.search(r"^\s*missing_reason: string \| null$", src, re.M), (
        "frontend 的 Evidence 类型缺 missing_reason —— 后端 EvidenceOut 有这一列"
    )


def test_every_missing_reason_maps_to_a_distinct_action():
    """三个取值 -> 三个不同的动作。合并成一句"没识别出来"就白做了。

    后端枚举的注释把三个动作写得很清楚(换清楚的图 / 补角度 / 等供应商),
    而合并之后运营唯一能想到的动作是重跑识别 —— 那对第三个取值
    (材质克重一类)重跑一万次也不会有值。
    """
    from app.core.enums import MissingReason

    src = ATTRIBUTES_API.read_text(encoding="utf-8")
    block = src[src.index("MISSING_REASON_ACTION") :]
    actions = re.findall(r"action: '([^']+)'", block)
    for reason in MissingReason:
        assert reason.value in block, f"前端的动作表漏了 {reason.value}"
    assert len(set(actions)) == len(actions) >= len(list(MissingReason)), (
        f"三个原因给了重复的动作:{actions}"
    )


def test_the_missing_reason_is_rendered_not_just_typed():
    """必须真的有人渲染 —— batch13-3 / R4 就是"只存在于类型里"。

    锚在 JSX 上:`{false && ...}` 那种死代码化的变异要能验红
    (batch13-3 / M11 的教训,它自己的守卫第一版就栽在这里)。
    """
    src = ATTRIBUTE_TAB.read_text(encoding="utf-8")
    assert "function MissingEvidenceNotice(" in src, "没有渲染组件"
    # **锚在行首**,不是子串匹配。子串匹配对
    # `{false && <MissingEvidenceNotice ... />}` 照样命中 —— 渲染死了、守卫还绿。
    # batch13-3 的 M11 第一版就栽在这里,而它栽的是**自己那一批**的守卫;
    # 抄那个教训比抄那段代码重要
    #
    # 锚点从"整行"放宽到"行首的标签":原来钉的是
    # `<MissingEvidenceNotice evidence={...} />` 这一整行,于是**给组件多传一个
    # prop 就会变红**,而多传一个 prop 不是回归。放宽之后它仍然挡得住死代码化
    # ——`{false &&` 会出现在同一行的标签之前,行首锚定就不成立。
    # 守的是"真的挂上去了",不是"参数表长什么样"。
    assert re.search(
        r"^\s*<MissingEvidenceNotice[\s>/]", src, re.M
    ), "组件没有作为独立元素挂进 JSX(或被条件死代码化了)"
    # 组件内部两个决定也要钉住:早退必须只在"没有无值证据"时发生,
    # 分组必须只收带 reason 的条目。少了这两条,把 `if (true) return null`
    # 写进去也能全绿
    assert "if (!byReason.length) return null" in src, (
        "早退不是按'有没有无值证据'判的 —— 组件可能恒不渲染"
    )
    assert "if (!item.missing_reason) continue" in src, (
        "分组没有按 missing_reason 过滤 —— 带值证据会被当成'没有值'报出来"
    )
    assert "attributesApi.extraction(" in src, (
        "没有人拉逐图证据 —— `missing_reason` 只在 /extractions/{id} 的出参里,"
        "属性行上没有这个信号"
    )
    assert "MISSING_REASON_ACTION[" in src, "渲染时没有查动作表,只显示了原因编码"
    # 查询失败必须有分支。这条是 `test_frontend_contract` 那道全局门禁的
    # 本地副本 —— 放在这里是因为本轮的病灶就是"诊断没上屏",而把失败
    # 渲染成空数据正是同一个病灶换了个入口:界面上"模型没报告任何无值字段"
    # 和"这条查询挂了"长得一模一样,而运营接下来该做的事是相反的
    assert re.search(r"^\s*\{lastExtraction\.isError && \($", src, re.M), (
        "逐图证据的查询没有失败分支 —— 拉不到会静默渲染成'什么都没有'"
    )
