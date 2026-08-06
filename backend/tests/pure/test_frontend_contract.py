"""前后端跨语言契约。

## 这个文件在 v4.1 Phase 0 里被拆掉了 76 条

上一版 86 条，是全仓低价值测试最集中的地方。方案 8.3 节把它们分成几类去向：

    交给 TypeScript          重复顶层变量、未绑定变量、query handle 绑定
    交给 `npm ci`            package.json 与 lockfile 一致
    交给 `npm run lint`      lint 配置可运行、lint 脚本不说谎
    交给 Vitest              未保存保护、URL 筛选、管理员菜单、错误提示、冷启动
    交给 Playwright（P5）    路由可达、今日卡片跳转、快审无批量操作、批次重试按钮
    直接删除                 「源码里有没有某个常量」这一类——那是在测实现形状

拆除的**顺序**是有约束的（方案 8.3 节末尾 + 12.1 节依赖表）：先接 Vitest（任务 2），
再拆这个文件（任务 1b）。反过来做会留下一段前端零覆盖的窗口期。
本次按这个顺序做：Vitest 已经绿了 56 条才动这里。

## 剩下的 10 条为什么删不掉

它们全部是**跨语言**的：一边是 Python 的枚举/常量，另一边是 TypeScript 的
文案表/联合类型。这条缝没有任何单语言工具能覆盖——

    tsc 看不见 `app/core/enums.py`
    ruff 看不见 `types.ts`
    Vitest 跑得到前端那一半，但它没有后端那一半可以对
    Playwright 能发现「界面上冒出英文常量」，但要等那个枚举值真的出现在页面上

后端加一个枚举值、前端忘了补中文，后果是界面上冒出一串英文常量，
而且**不会有任何人立刻发现**——这正是需要机器盯着的那种事。

正确的终局是 OpenAPI 类型生成（方案 8.3 节「迁移到 OpenAPI/类型生成」），
那时候这 10 条里的大部分会由生成器保证。在生成器落地之前，它们留着。

## 为什么是一条循环而不是二十条函数

上一版给每张文案表写一个测试函数，于是「加一张表」= 「抄一个函数」。
抄的时候会漏——`STALE_COMPONENT_LABEL` 就是后来补上去的。

现在改成一张声明式的表 + 一次循环，并且**把所有不一致一次报全**，
而不是在第一个 assert 上停下。修的人一趟就能补完全部文案。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT, window

FRONTEND_API = PROJECT_ROOT / "frontend" / "src" / "api"
API_DIR = BACKEND_ROOT / "app" / "api"


# ================================================================ TS 源码取值
#
# 只做括号配平与正则，不引入 JS 解析器：这批用例要在只有 python3 的机器上跑。


def _ts(name: str) -> str:
    return (FRONTEND_API / name).read_text(encoding="utf-8")


def _frontend_src() -> Path:
    """前端源码根。契约测试只读它,从不写。"""
    return PROJECT_ROOT / "frontend" / "src"


def _object_keys(source: str, const_name: str) -> set[str]:
    """取出 `export const X ... = { a: ..., b: ... }` 的顶层键。"""
    match = re.search(rf"export const {const_name}\b[^=]*=\s*\{{", source)
    assert match, f"找不到 {const_name}"

    start = match.end()  # 已经越过左花括号
    depth = 1
    i = start
    while i < len(source) and depth:
        char = source[i]
        if char in "{[(":
            depth += 1
        elif char in "}])":
            depth -= 1
        i += 1
    body = source[start : i - 1]

    keys: set[str] = set()
    depth = 0
    for line in body.splitlines():
        stripped = line.strip()
        if depth == 0:
            key = re.match(r"^([A-Za-z_][A-Za-z0-9_]*)\s*:", stripped)
            if key:
                keys.add(key.group(1))
        depth += (
            stripped.count("{") + stripped.count("[")
            - stripped.count("}") - stripped.count("]")
        )
    return keys


def _array_values(source: str, const_name: str) -> list[str]:
    """按**顺序**取出 `export const X = ['a', 'b']` 的元素。顺序有时候就是契约。"""
    match = re.search(rf"export const {const_name}\b[^=]*=\s*\[(.*?)\]", source, re.S)
    assert match, f"找不到 {const_name}"
    return re.findall(r"'([^']+)'", match.group(1))


def _union_members(source: str, type_name: str) -> set[str]:
    """取出 `export type X = 'a' | 'b'` 的成员。

    工作台与批次的枚举在前端是字符串联合类型而不是对象——这是刻意的：
    它让 `state === 'DONE'` 这类比较在编译期就能查错，对象键做不到。
    """
    match = re.search(rf"export type {type_name}\s*=([^;]+?)(?:\n\n|\nexport|\n/\*)", source, re.S)
    assert match, f"找不到 type {type_name}"
    return set(re.findall(r"'([^']+)'", match.group(1)))


def _values(source) -> set[str]:
    """把后端那一半归一成字符串集合：枚举类、dict、可迭代都接。"""
    if isinstance(source, dict):
        return {k.value if hasattr(k, "value") else str(k) for k in source}
    return {m.value if hasattr(m, "value") else str(m) for m in source}


# ================================================================ 契约表
#
# 加一张前端文案表时，在这里加一行；不要再抄一个测试函数出来。


def _label_contracts() -> list[tuple[str, str, str, set[str]]]:
    """(前端文件, 常量名, 说明, 后端应有的取值集合)"""
    from app.core.enums import (
        CopyStatus,
        DraftStatus,
        ExtractionRunStatus,
        Grade,
        HardFailCode,
        ImageSetStatus,
        OutputPurpose,
        ProblemSeverity,
        RecommendedAction,
        ReviewReason,
        ReviewStatus,
        TaskStatus,
    )
    from app.evaluators.base import DIMENSION_LABELS
    from app.workbench.audit_view import ENTITY_LABELS, PAYLOAD_ACTION_LABELS
    from app.workbench.batch import (
        BatchAction,
        BatchErrorCode,
        BatchItemStatus,
        BatchJobStatus,
        FileState,
    )
    from app.workbench.flow import FlowStep, IssueLevel, NextActionCode, StepState
    from app.workbench.import_plan import ROW_PLAN_LABELS

    # `settings_service` 顶层 import 了 sqlalchemy，而这批用例刻意零三方依赖
    # （见 `test_pure_suite_is_self_contained` 的约束），import 不进来。
    # 所以这一处读常量声明而不是 import 模块——是被运行环境逼出来的，
    # 不是偷懒。真装了 sqlalchemy 的环境下由 pytest 那一路覆盖同一件事。
    service = (BACKEND_ROOT / "app" / "services" / "settings_service.py").read_text(
        encoding="utf-8"
    )
    setting_sources = set(re.findall(r'^SOURCE_\w+ = "([a-z]+)"', service, re.M))
    assert setting_sources, "settings_service 里找不到 SOURCE_* 常量"

    return [
        # ---- types.ts：评分、审核、任务 ----
        ("types.ts", "GRADE_LABEL", "分档", _values(Grade)),
        ("types.ts", "HARD_FAIL_LABEL", "硬错误码", _values(HardFailCode)),
        ("types.ts", "DIMENSION_LABEL", "评分维度", _values(DIMENSION_LABELS)),
        ("types.ts", "REVIEW_STATUS_LABEL", "审核状态", _values(ReviewStatus)),
        ("types.ts", "REVIEW_REASON_LABEL", "送审原因", _values(ReviewReason)),
        ("types.ts", "SEVERITY_LABEL", "问题严重度", _values(ProblemSeverity)),
        ("types.ts", "RECOMMENDED_ACTION_LABEL", "建议动作", _values(RecommendedAction)),
        ("types.ts", "TASK_STATUS_LABEL", "任务状态", _values(TaskStatus)),
        ("types.ts", "OUTPUT_PURPOSE_LABEL", "成品图用途", _values(OutputPurpose)),
        ("types.ts", "SETTING_SOURCE_LABEL", "设置项来源", setting_sources),
        # ---- workbench.ts：单件流程 ----
        ("workbench.ts", "FLOW_STEP_LABEL", "流程步骤", _values(FlowStep)),
        ("workbench.ts", "STEP_TAB", "步骤跳转", _values(FlowStep)),
        ("workbench.ts", "STEP_STATE_LABEL", "子状态", _values(StepState)),
        ("workbench.ts", "ISSUE_LEVEL_LABEL", "问题级别", _values(IssueLevel)),
        ("workbench.ts", "NEXT_ACTION_LABEL", "下一步动作", _values(NextActionCode)),
        ("workbench.ts", "ACTION_TAB", "下一步跳转", _values(NextActionCode)),
        ("workbench.ts", "COPY_STATUS_LABEL", "文案状态", _values(CopyStatus)),
        ("workbench.ts", "DRAFT_STATUS_LABEL", "草稿状态", _values(DraftStatus)),
        ("workbench.ts", "IMAGE_SET_STATUS_LABEL", "图片集状态", _values(ImageSetStatus)),
        # ---- batch.ts：批量与审计 ----
        ("batch.ts", "BATCH_ACTION_LABEL", "批量动作", _values(BatchAction)),
        ("batch.ts", "BATCH_ACTION_HINT", "批量动作说明", _values(BatchAction)),
        ("batch.ts", "BATCH_ITEM_STATUS_LABEL", "批次条目状态", _values(BatchItemStatus)),
        ("batch.ts", "BATCH_JOB_STATUS_LABEL", "批次状态", _values(BatchJobStatus)),
        ("batch.ts", "BATCH_ERROR_LABEL", "批量错误码", _values(BatchErrorCode)),
        ("batch.ts", "FILE_STATE_LABEL", "导出文件状态", _values(FileState)),
        ("batch.ts", "FILE_STATE_COLOR", "导出文件状态色", _values(FileState)),
        ("batch.ts", "AUDIT_ENTITY_LABEL", "审计对象类型", _values(ENTITY_LABELS)),
        ("batch.ts", "AUDIT_ACTION_LABEL", "审计动作", _values(PAYLOAD_ACTION_LABELS)),
        ("batch.ts", "IMPORT_ROW_PLAN_LABEL", "导入行处置", _values(ROW_PLAN_LABELS)),
        # ---- attributes.ts:识别 run(A45-batch14-11 / §4.6)----
        #
        # 两张表都必须**全覆盖**,包括今天不会出现的 QUEUED / RUNNING。
        # 少一个键的表现不是报错,是 §4.6 异步落地那天界面拿到 undefined
        # 然后一个字都不显示 —— 而那一档恰恰是「还在跑,别再点了」。
        ("attributes.ts", "RUN_STATUS_LABEL", "识别 run 状态", _values(ExtractionRunStatus)),
        ("attributes.ts", "RUN_STATUS_NOTICE", "识别 run 提示", _values(ExtractionRunStatus)),
    ]


def _union_contracts() -> list[tuple[str, str, set[str]]]:
    from app.workbench.batch import BatchAction, BatchItemStatus, BatchJobStatus
    from app.workbench.flow import FlowStep, NextActionCode

    # 派发状态的四个取值声明在 `api/workbench_batch.py` 顶部,而那个模块顶层
    # import 了 FastAPI —— 这批用例刻意零三方依赖(见 `_label_contracts` 里
    # `setting_sources` 的同一段说明),import 不进来。所以读常量声明。
    api_source = (BACKEND_ROOT / "app" / "api" / "workbench_batch.py").read_text(
        encoding="utf-8"
    )
    dispatch_states = set(re.findall(r'^DISPATCH_\w+ = "([A-Z_]+)"', api_source, re.M))
    assert dispatch_states, "workbench_batch.py 里找不到 DISPATCH_* 常量"

    from app.core.enums import ExtractionRunStatus

    return [
        ("workbench.ts", "FlowStep", _values(FlowStep)),
        # 这一条是**判定依据**不是文案:`RUN_STATUS_NOTICE[result.status]`
        # 少一个取值时 tsc 当场报错,而那正是我们要的 —— 它替代的是
        # 一段前端自己重算三档的三元运算(硬规则 4)
        ("attributes.ts", "ExtractionRunStatus", _values(ExtractionRunStatus)),
        ("workbench.ts", "NextActionCode", _values(NextActionCode)),
        ("batch.ts", "BatchAction", _values(BatchAction)),
        ("batch.ts", "BatchItemStatus", _values(BatchItemStatus)),
        ("batch.ts", "BatchJobStatus", _values(BatchJobStatus)),
        # 这一条比别的更要紧一点:它不是文案,是**判定依据**。前端少一个取值
        # 时 `readDispatchOutcome` 的 switch 会落到 default 分支,退回旧的
        # `dispatched` + `status` 推断 —— 那个推断恰恰把 NOT_REQUESTED 读成
        # 「broker 投递失败」并弹 error 级通知。少一个取值不会报错,会说错话。
        ("batch.ts", "BatchDispatchState", dispatch_states),
    ]


# ================================================================ 1. 文案表对齐


def test_every_frontend_label_table_covers_its_backend_enum():
    """后端加一个枚举值而前端不加文案，界面上会冒出英文常量。

    一次报全部差异，不在第一处停下——修的人一趟就能补完。
    """
    problems: list[str] = []
    for filename, const, what, backend in _label_contracts():
        try:
            frontend = _object_keys(_ts(filename), const)
        except AssertionError as exc:
            problems.append(f"{filename}::{const}（{what}）{exc}")
            continue
        missing = sorted(backend - frontend)
        extra = sorted(frontend - backend)
        if missing or extra:
            problems.append(
                f"{filename}::{const}（{what}）缺 {missing or '无'}；多 {extra or '无'}"
            )

    assert not problems, "前端文案表与后端枚举不一致：\n  " + "\n  ".join(problems)


def test_file_state_labels_are_word_for_word_identical():
    """导出文件状态的中文**逐字**一致。

    只对齐键是不够的：同一个状态在横幅上叫一个名字、在表格里叫另一个，
    运营会以为那是两件事。而这张表恰好是他决定「这个包还能不能用」的唯一依据。
    """
    from app.workbench.batch import FILE_STATE_LABELS, FileState

    source = _ts("batch.ts")
    pairs = re.findall(
        r"^\s*([A-Z_]+):\s*'([^']+)',$", source[source.index("FILE_STATE_LABEL") :], re.M
    )
    frontend = dict(pairs[: len(FileState)])
    backend = {k.value: v for k, v in FILE_STATE_LABELS.items()}
    assert frontend == backend, f"前端 {frontend} vs 后端 {backend}"


# ================================================================ 2. 联合类型对齐


def test_every_frontend_union_type_matches_its_backend_enum():
    """联合类型比文案表更要紧：它是编译期比较的依据。

    后端多一个取值而联合类型没跟上，`state === 'NEW_ONE'` 会被 tsc 判成
    「不可能的比较」而报错——好事，但报错点在一个和根因毫无关系的文件里。
    """
    problems: list[str] = []
    for filename, type_name, backend in _union_contracts():
        try:
            frontend = _union_members(_ts(filename), type_name)
        except AssertionError as exc:
            problems.append(f"{filename}::{type_name} {exc}")
            continue
        if frontend != backend:
            problems.append(
                f"{filename}::{type_name} 缺 {sorted(backend - frontend)}；"
                f"多 {sorted(frontend - backend)}"
            )

    assert not problems, "前端联合类型与后端枚举不一致：\n  " + "\n  ".join(problems)


# ================================================================ 3. 顺序契约


def test_ordered_tables_match_the_backend_order():
    """顺序本身就是契约的两处。

    `FLOW_STEP_ORDER` 决定列表页那五列的排法——顺序错了，运营读到的是
    一条不存在的动线。`OUTPUT_PURPOSE_ORDER` 决定成品图区块的排法，
    并且必须不重不漏，重了会渲染出两个一样的区块。
    """
    from app.core.enums import OutputPurpose
    from app.workbench.flow import STEP_ORDER

    steps = _array_values(_ts("workbench.ts"), "FLOW_STEP_ORDER")
    assert steps == [s.value for s in STEP_ORDER], (
        f"前端流程顺序 {steps} 与后端 STEP_ORDER {[s.value for s in STEP_ORDER]} 不一致"
    )

    purposes = _array_values(_ts("types.ts"), "OUTPUT_PURPOSE_ORDER")
    assert len(purposes) == len(set(purposes)), f"用途顺序表里有重复项: {purposes}"
    assert set(purposes) == _values(OutputPurpose)


# ================================================================ 4. 跳转目标


def test_jump_tables_point_at_tabs_that_actually_exist():
    """「唯一下一步」按钮的跳转目标必须是声明过的标签页。

    拼错一个字符串的后果不是报错，是点了按钮跳到 `undefined` 标签、页面空白——
    而「唯一下一步」正是阶段 1 的核心验收项。tsc 拦不住它：
    那两张表的值类型写的是 string。
    """
    source = _ts("workbench.ts")
    tabs = _union_members(source, "WorkbenchTab")
    assert tabs, "workbench.ts 里没解析出 WorkbenchTab"

    problems: list[str] = []
    for const_name in ("ACTION_TAB", "STEP_TAB"):
        match = re.search(rf"export const {const_name}\b[^=]*=\s*\{{(.*?)\n\}}", source, re.S)
        assert match, f"找不到 {const_name}"
        unknown = sorted(set(re.findall(r"'([^']+)'", match.group(1))) - tabs)
        if unknown:
            problems.append(f"{const_name} 指向了不存在的标签页: {unknown}")

    assert not problems, "\n  ".join(problems)


# ================================================================ 5. 路由对齐


def _router_prefix(tree: ast.Module) -> str:
    """取出 `APIRouter(prefix="/x")` 里的前缀，没写就是空串。

    不看前缀的话，工作台的 `/workbench/products` 会被记成 `/products`，
    素材库的 `/media/{}/role` 会被记成 `/{}/role`——前者假失败，后者假通过。
    """
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
        if name != "APIRouter":
            continue
        for keyword in node.keywords:
            if keyword.arg == "prefix" and isinstance(keyword.value, ast.Constant):
                return str(keyword.value.value)
    return ""


def _backend_routes() -> set[str]:
    """从 @router.get("/x") 这类装饰器里取出全部注册路径，路径参数归一为 {}。"""
    routes: set[str] = set()
    for path in API_DIR.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        prefix = _router_prefix(tree)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not isinstance(func, ast.Attribute) or func.attr not in {
                "get", "post", "patch", "put", "delete",
            }:
                continue
            if not (isinstance(func.value, ast.Name) and func.value.id == "router"):
                continue
            if node.args and isinstance(node.args[0], ast.Constant):
                # 空路径 + 前缀 = 前缀本身（`@router.get("")` 挂在 /image-sets 上）
                full = f"{prefix}{node.args[0].value}" or "/"
                routes.add(re.sub(r"\{[^}]+\}", "{}", full))
    return routes


def _expand_template(raw: str) -> set[str]:
    """把一条模板字符串路径展开成它可能命中的全部后端路由。

    大多数插值是路径参数，归一成 `{}` 就行。但有一处不是：

        `/model-templates/${id}/${enabled ? \'enable\' : \'disable\'}`

    这里的第二个插值不是参数，是**两条不同的路由**。把它也归一成 `{}`，
    得到的 `/model-templates/{}/{}` 在后端根本不存在，于是这条测试会
    假报一个不存在的问题；而如果为了让它变绿去放宽匹配，真正的拼写错误
    也会一起放过去。所以这里如实展开成两条。
    """
    out = {raw}
    while True:
        grown: set[str] = set()
        changed = False
        for candidate in out:
            match = re.search(r"\$\{([^{}]*)\}", candidate)
            if not match:
                grown.add(candidate)
                continue
            changed = True
            literals = re.findall(r"\'([^\']*)\'", match.group(1))
            replacements = literals or ["{}"]
            for value in replacements:
                grown.add(candidate[: match.start()] + value + candidate[match.end() :])
        out = grown
        if not changed:
            return out


def test_every_path_the_frontend_calls_exists_in_the_backend():
    """前端调用的每一条路径都必须在后端注册过。

    上一版按文件写了五六条几乎一样的测试（reviews / exports / settings /
    workbench / batch / media / attributes…），加一个 api 模块就要再抄一条，
    而**抄漏的那一条不会有任何征兆**——没有测试的文件不会红。

    改成扫 `frontend/src/api/` 下的全部文件：新增模块自动纳入。
    """
    routes = _backend_routes()
    assert routes, "没有从 app/api 解析出任何路由"

    problems: list[str] = []
    scanned = 0
    for ts_file in sorted(FRONTEND_API.glob("*.ts")):
        source = ts_file.read_text(encoding="utf-8")
        called: set[str] = set()
        for raw in re.findall(r"apiClient\.\w+<[^>]*>\(\s*`([^`]+)`", source):
            called |= _expand_template(raw)
        for raw in re.findall(r"apiClient\.\w+<[^>]*>\(\s*'([^']+)'", source):
            called.add(raw)
        if not called:
            continue
        scanned += 1
        missing = sorted(called - routes)
        if missing:
            problems.append(f"{ts_file.name} 调用了后端没有注册的路径: {missing}")

    assert scanned >= 5, f"只从 {scanned} 个前端 api 文件里解析出路径，解析规则可能失效了"
    assert not problems, "\n  ".join(problems)


# ================================================================ 6～10. 数值与映射契约


def test_product_field_limits_match_the_backend():
    """表单限长必须逐字段等于后端 field_limits（数据库列宽的唯一来源）。

    前端比后端严，合法值在界面被拦；前端比后端松，错误要到提交才暴露——
    两个方向都是缺陷，所以做相等断言而不是「前端 ≤ 后端」。
    """
    from app.core.field_limits import (
        MAX_SECONDARY_COLOR_LENGTH,
        MAX_SECONDARY_COLORS,
        PRODUCT_FIELD_LIMITS,
    )

    source = _ts("types.ts")
    pairs = dict(
        re.findall(
            r"^\s*([a-z_]+):\s*(\d+),\s*$",
            source[source.index("export const PRODUCT_FIELD_LIMITS") :].split("} as const")[0],
            re.M,
        )
    )
    assert {k: int(v) for k, v in pairs.items()} == PRODUCT_FIELD_LIMITS

    def _const(name: str) -> int:
        match = re.search(rf"export const {name}\s*=\s*(\d+)", source)
        assert match, f"types.ts 里找不到 {name}"
        return int(match.group(1))

    assert _const("MAX_SECONDARY_COLORS") == MAX_SECONDARY_COLORS
    assert _const("MAX_SECONDARY_COLOR_LENGTH") == MAX_SECONDARY_COLOR_LENGTH


def test_setting_field_types_match_the_backend_type_constants():
    """设置项控件类型少一个，那一类字段会掉进 else 分支渲染成纯文本框。

    这条是补上去的：`integer` 在前端联合类型里漏了很久，页面代码却正确地
    处理着它——于是 tsc 把那个分支判成不可能，`npm run build` 直接失败。
    """
    schema = (BACKEND_ROOT / "app" / "core" / "settings_schema.py").read_text(encoding="utf-8")
    backend = set(re.findall(r'^TYPE_\w+ = "(\w+)"', schema, re.M))
    assert backend, "settings_schema 里找不到 TYPE_* 常量"

    match = re.search(
        r"export type SettingFieldType\s*=([^\n]*(?:\n\s*\|[^\n]*)*)", _ts("types.ts")
    )
    assert match, "types.ts 里找不到 SettingFieldType"
    frontend = set(re.findall(r"'([^']+)'", match.group(1)))
    assert frontend == backend, (
        f"缺: {sorted(backend - frontend)}；多: {sorted(frontend - backend)}"
    )


def test_gate_dimensions_and_paid_actions_match_the_backend():
    """两组「错了会直接产生业务后果」的清单。

    A 档四条底线错了，界面上的判定标准与真实评分器不是一回事；
    计费动作清单错了，「这个动作会计费」的确认框会挂在错的按钮上——
    而那个方向的错误要到账单上才被发现。
    """
    from app.evaluators.rules import DEFAULT_THRESHOLDS
    from app.workbench.batch import PAID_ACTIONS

    declared = set(_array_values(_ts("types.ts"), "GATE_DIMENSIONS"))
    from_thresholds = {
        name[len("a_min_") :]
        for name in vars(DEFAULT_THRESHOLDS)
        if name.startswith("a_min_") and name != "a_min_overall"
    }
    assert declared == from_thresholds, f"前端 {sorted(declared)} vs 后端 {sorted(from_thresholds)}"

    paid = set(_array_values(_ts("batch.ts"), "PAID_BATCH_ACTIONS"))
    assert paid == {a.value for a in PAID_ACTIONS}, (
        f"前端 {sorted(paid)} vs 后端 {sorted(a.value for a in PAID_ACTIONS)}"
    )


def test_settings_group_provider_map_points_at_real_things():
    """「测试连接」按钮挂在分组上，两头都必须存在：分组要有，Provider 也要有。"""
    from app.core.settings_schema import SETTING_GROUPS
    from app.providers.registry import PROVIDER_FACTORIES

    source = _ts("types.ts")
    keys = _object_keys(source, "SETTING_GROUP_PROVIDER")
    unknown_groups = sorted(keys - {g.key for g in SETTING_GROUPS})
    assert not unknown_groups, f"映射里有不存在的分组: {unknown_groups}"

    match = re.search(r"export const SETTING_GROUP_PROVIDER\b[^=]*=\s*\{(.*?)\n\}", source, re.S)
    assert match, "找不到 SETTING_GROUP_PROVIDER"
    unknown = sorted(set(re.findall(r"'([^']+)'", match.group(1))) - set(PROVIDER_FACTORIES))
    assert not unknown, f"映射指向了不存在的 Provider: {unknown}"


# ================================================================ 11. 写入口的入参契约


def _query_params(module: str, func_name: str) -> dict[str, ast.Call]:
    """取出某个端点函数里所有 `x: T = Query(...)` 的参数名 -> 那次 Query 调用。

    只认 `Query(...)`:路径参数、`Depends`、请求体模型都不走查询串,
    混进来会让下面那条断言去要求前端发一个它根本不该发的东西。
    """
    tree = ast.parse((API_DIR / module).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name != func_name:
            continue
        args = node.args
        found: dict[str, ast.Call] = {}
        # 位置参数与仅关键字参数的默认值分两个列表,右对齐。
        # `strict=True` 是本仓库对 zip 的一贯写法(B905):两边长度由语法保证
        # 相等,真不相等说明 AST 的形状和这里的假设已经对不上,该当场炸
        positional = args.args[-len(args.defaults):] if args.defaults else []
        pairs = list(zip(positional, args.defaults, strict=True))
        pairs += [
            (arg, default)
            for arg, default in zip(args.kwonlyargs, args.kw_defaults, strict=True)
            if default is not None
        ]
        for arg, default in pairs:
            if (
                isinstance(default, ast.Call)
                and getattr(default.func, "id", getattr(default.func, "attr", "")) == "Query"
            ):
                found[arg.arg] = default
        return found
    raise AssertionError(f"{module} 里找不到函数 {func_name}")


def _query_kwarg(call: ast.Call, name: str):
    for keyword in call.keywords:
        if keyword.arg == name and isinstance(keyword.value, ast.Constant):
            return keyword.value.value
    return None


def test_force_retry_sends_every_query_param_the_backend_declares():
    """强制重试的入参名两边必须一字不差。

    ## 为什么这条缝必须有机器盯着

    后端对「提交结果未知」的任务 fail-closed:普通重试稳定 409,要放行必须
    带 `force=true`,而带了 `force` 就**必须**带 `reconciliation_note`,
    否则 422(见 `services/generation_service.retry_task` 的说明 —— 这是全系统
    唯一一个由人主动承担重复计费风险的动作,没有依据就不许放行)。

    这两个名字任何一边改动或漏发,表现都不是崩溃,而是:运营照着提示去
    Provider 后台核对完,回来填了对账结论、点「我已核对，强制重试」,
    拿到一个校验错误。**这正是 BLOCK-06 要消除的那个死胡同**,只是从 409
    换成了 422 —— 而两个补丁各自合入时,系统真的就是这个样子。

    tsc 看不见 `api/generation_tasks.py`,ruff 看不见 `generation.ts`,
    Vitest 没有后端那一半可以对。所以它落在这个文件里。
    """
    declared = _query_params("generation_tasks.py", "retry_task")
    assert "force" in declared and "reconciliation_note" in declared, (
        f"后端 retry 端点的查询参数变了:{sorted(declared)}。"
        "前端那半边(generation.ts 的 retry)要跟着改,否则强制重试会 422"
    )

    source = _ts("generation.ts")
    match = re.search(r"\n  retry: async \(.*?\n  \},\n", source, re.S)
    assert match, "generation.ts 里找不到 retry 方法 —— 解析规则可能失效了"
    sent = set(re.findall(r"params\.(\w+)\s*=", match.group(0)))

    missing = sorted(set(declared) - sent)
    assert not missing, (
        f"后端 retry 声明了查询参数 {missing},前端一个都没发。"
        "漏 reconciliation_note 的后果是强制重试按钮稳定 422"
    )
    unknown = sorted(sent - set(declared))
    assert not unknown, f"前端发了后端不认的查询参数 {unknown}(会被静默忽略)"


def test_reconciliation_note_limit_matches_the_backend():
    """对账结论的字数上限两边同源。

    前端 `maxLength` 比后端松的话,运营写满一屏、点确认、拿到 422,
    而输入框刚刚还告诉他"还能再写 200 字"。比两个数字不一致更糟的是
    **只有超长时才发作** —— 平时怎么点都是对的。
    """
    declared = _query_params("generation_tasks.py", "retry_task")
    backend_max = _query_kwarg(declared["reconciliation_note"], "max_length")
    assert isinstance(backend_max, int), "后端没给 reconciliation_note 设 max_length"

    source = _ts("generation.ts")
    match = re.search(r"export const RECONCILIATION_NOTE_MAX = (\d+)", source)
    assert match, "generation.ts 里找不到 RECONCILIATION_NOTE_MAX"
    assert int(match.group(1)) == backend_max, (
        f"前端上限 {match.group(1)} vs 后端 max_length {backend_max}"
    )


def test_frontend_terminal_task_statuses_match_the_state_machine():
    """前端的「终态」必须就是后端状态机的终态,一个不多一个不少。

    ## 这条缝为什么值得一条测试

    前端拿这份清单**决定要不要继续轮询**。它多一个值,后果不是多刷几次,
    是那个状态下页面**永远停在旧数据**上 —— 而"多出来的那个值"的定义
    恰恰是"后端还会改它"。

    真实发生过的版本:清单里多了 `FAILED` 与 `MANUAL_REVIEW`。两个都有出边

        FAILED        -> QUEUED / FORMATTING / SCORING
        MANUAL_REVIEW -> MANUALLY_APPROVED / MANUALLY_REJECTED / REGENERATING

    于是运营 A 在审核台批准了一件,运营 B 的任务列表到下班都显示"待人工审核"。
    两个人,两份事实,谁也不会觉得是 bug —— 只会觉得对方记错了。

    tsc 看不见 `state_machine.py`,ruff 看不见 `types.ts`,Vitest 没有后端
    那一半可以对。所以它落在这个文件里。
    """
    from app.workflows.state_machine import TERMINAL_STATES

    frontend = set(_array_values(_ts("types.ts"), "TERMINAL_TASK_STATUSES"))
    backend = _values(TERMINAL_STATES)

    extra = sorted(frontend - backend)
    assert not extra, (
        f"前端把 {extra} 当成了终态,而后端这些状态还有出边。"
        "后果是这些状态下页面停止轮询,别人推进之后这一边永远不知道"
    )
    missing = sorted(backend - frontend)
    assert not missing, (
        f"后端终态 {missing} 不在前端清单里。后果只是多轮询几次,"
        "但两份清单一旦开始分叉,下一次分叉的方向就不一定是安全的那边了"
    )


def test_frontend_awaiting_human_statuses_match_the_state_machine():
    """「等人动」那一档必须**逐值等于**后端 `AWAITING_HUMAN_STATES`(A34)。

    ## 为什么从负向约束改成正向比对

    上一版只做负向约束:断言这份清单不含终态、不含 `STALLABLE_STATUSES`。
    那挡得住"放错值",挡不住**后端新增一个等人动的状态而前端不知道** ——
    新状态既不是终态也不在 worker 持有清单里,负向约束一条都不会红,
    而前端会把它归进 LIVE 档,每 2.5 秒问一次一个没有任何后台进程会改的东西。

    那正是硬规则 4(前端不许推测状态)要防的形状。终态那一份早就钉在后端了
    (`TERMINAL_STATES`),中间这一档是补上的另一半。

    ## 方向仍然是"两边都不许多也不许少"

    前端多一个:那个状态被慢轮询,而机器其实还在写它 —— 页面看起来卡了。
    前端少一个:那个状态被快轮询,纯浪费;更要紧的是两份清单一旦开始分叉,
    下一次分叉的方向就不一定是安全的那边了。
    """
    from app.core.enums import TaskStatus
    from app.workflows import state_machine as sm

    source = _ts("types.ts")
    frontend = set(_array_values(source, "AWAITING_HUMAN_TASK_STATUSES"))
    backend = _values(sm.AWAITING_HUMAN_STATES)
    assert backend, "后端 AWAITING_HUMAN_STATES 空了 —— 这一档被删掉的话应该连同本测试一起删"

    unknown = sorted(frontend - {s.value for s in TaskStatus})
    assert not unknown, f"{unknown} 不是后端的 TaskStatus 取值"

    assert frontend == backend, (
        f"前端缺 {sorted(backend - frontend)};多 {sorted(frontend - backend)}。"
        "这一档决定轮询节拍:少一个会让那个状态被当成机器在跑,"
        "多一个会让一个还在被写的任务 30 秒才刷一次"
    )

    # 这一档与终态那一档在前端是两个数组,`taskLiveness` 按顺序判。
    # 同一个值出现在两边时行为由分支顺序决定,而那不该是靠读代码才知道的事
    frontend_terminal = set(_array_values(source, "TERMINAL_TASK_STATUSES"))
    overlap = sorted(frontend & frontend_terminal)
    assert not overlap, f"{overlap} 同时出现在两份清单里,`taskLiveness` 的分支顺序会决定行为"


def test_batch_liveness_union_matches_the_backend_enum():
    """批次活性的三个值两边同源。

    少一个值**不报错,只是说错话**:前端的 switch 落进 default 分支,
    于是一个后端已经判定为"卡住"的批次被当成"在跑",页面继续每 3 秒
    问一次 —— 正好是这个字段被加出来要修的那个毛病。

    (同一个形状在 `BatchDispatchState` 上已经有一条,那条守的是写请求
    响应里的四个常量;这一条守的是每次读取都带的这三个。)
    """
    from app.workbench.batch import BATCH_LIVENESS_LABELS, BatchLiveness

    frontend = _union_members(_ts("batch.ts"), "BatchLiveness")
    backend = _values(BatchLiveness)
    assert frontend == backend, (
        f"前端 BatchLiveness {sorted(frontend)} vs 后端 {sorted(backend)}。"
        "少一个值不会报错,只会让那一档批次的轮询节拍算错"
    )

    unlabelled = sorted(m for m in BatchLiveness if not BATCH_LIVENESS_LABELS.get(m))
    assert not unlabelled, f"后端 {unlabelled} 没有中文标签,前端会拿到 undefined"


# ==========================================================
# 查询失败不许退化成"无数据"(a28 报告 FE-GLOBAL-03 / 阶段 A 验收第 6 条)
# ==========================================================


#: 发起查询但**故意**不写失败分支的组件,连同理由。
#:
#: 这份豁免必须逐个写理由,不许整目录开口 —— a28 报告里那一整类问题
#: (FE-PROVIDER-01 / FE-TEMPLATE-01 / FE-REVIEW-QUEUE-02 / FE-TASK-CREATE-04 …)
#: 的共同形状就是"失败时静默退化成空",而每一处当时看都像无害的兜底。
_QUERY_WITHOUT_ERROR_BRANCH_ALLOWED = {
    # 只用查询结果把 reason code 翻成中文标签,拉不到时**原样显示 code**。
    # 那不是"无数据",是一个信息量更低但仍然正确的显示 —— 且这一格
    # 装不下错误提示(它在表格单元里)
    "components/workbench/FlowBits.tsx",
    #
    # 全局预算横幅。拿不到就整条不显示,**这是一次自觉的取舍,不是遗漏**:
    # 这一条挂在每一个页面上,给它加失败提示等于让一个抖动的 /usage/spend
    # 在全站顶部常驻一条红条,而横幅这个位置的全部价值就是"平时不出现"。
    # 信息没有丢:/spend 页自己有完整的 isError 分支(那一页会说没拉到)。
    # 残留风险照实记在 HANDOVER 里 —— 持续失败时运营看不到超预算告警,
    # 要等他自己想到去花费页。彻底的做法是让横幅区分"没超"和"不知道",
    # 而那要先有一个不会因单次抖动就变红的判据
    "components/SpendAlertBanner.tsx",
}


def _query_handles(src: str) -> list[tuple[str, str]]:
    """从源码里找出每一次查询声明,返回 (句柄名, 声明原文)。

    只认这三种写法,因为全仓只有这三种(见下面那条 test 的守卫):

        const NAME = useQuery({...})       具名句柄
        const NAME = useQueries({...})     一组句柄,消费方式是 NAME.some/find
        const { a, b } = useQuery({...})   解构,句柄名是 ""

    刻意不做完整 TS 解析:那要么引 TypeScript(纯测试跑不了),要么自己写
    一个半吊子解析器 —— 而半吊子解析器出错时是**静默漏判**,正是这条门禁
    要防的形状。用正则加一条"写法必须在已知集合内"的守卫,漏判会变成红。
    """
    out: list[tuple[str, str]] = []
    for m in re.finditer(
        r"const\s+(\{[^}]*\}|[A-Za-z_$][\w$]*)\s*=\s*useQuer(y|ies)\s*\(", src
    ):
        decl, kind = m.group(1), m.group(2)
        name = "" if decl.startswith("{") else decl
        out.append((name, decl if not name else kind))
    return out


def test_every_query_handle_is_checked_for_failure():
    """**每一个** `useQuery` 的句柄都必须在同一个文件里被问过 `.isError`。

    ## 判据为什么必须绑到句柄上

    上一版这条只查"文件里有没有出现 isError 或 .error"。那挡不住真实的形状:
    一个页面里往往有若干次查询和若干个 mutation,而 mutation 的
    `onError: (err) => message.error(readError(err))` 里就有一个 `.error` ——
    于是**整个文件的查询一个都没查失败,这条门禁照样绿**。

    这不是假设:把 `ProvidersPage` 的失败分支整段拆掉之后,上一版判据全绿。
    变异验证是发现这件事的唯一途径,而"变异验证跑了"和"变异验证有效"
    是两件事。

    ## 为什么这一类值得一条门禁

    a28 报告 FE-GLOBAL-03:查询失败经常被包装成空数据。而空数据在界面上
    是**一句业务结论** —— "没有 Provider"、"没有模板"、"没有规则集,后端
    将使用代码默认值"。运营照着这些结论做的下一步,和照着"没拉到"做的
    下一步是相反的。
    """
    root = _frontend_src()
    offenders: list[str] = []
    for path in sorted(root.rglob("*.tsx")):
        rel = path.relative_to(root).as_posix()
        if rel in _QUERY_WITHOUT_ERROR_BRANCH_ALLOWED:
            continue
        src = path.read_text(encoding="utf-8")
        for name, decl in _query_handles(src):
            if not name:
                # 解构写法:isError / error 必须就在解构里
                if not re.search(r"\bisError\b|\berror\b", decl):
                    offenders.append(f"{rel}: 解构 {decl} 没有取 isError")
                continue
            if decl == "ies":
                # useQueries 的句柄是一个数组,问法是 NAME.find(q => q.isError)
                ok = re.search(
                    rf"\b{re.escape(name)}\.(some|find|filter|every|map)\([^;\n]*isError",
                    src,
                )
            else:
                ok = re.search(rf"\b{re.escape(name)}\.isError\b", src)
            if not ok:
                offenders.append(f"{rel}: {name} 没有被问过 .isError")

    assert not offenders, (
        "下面这些查询失败时会静默渲染成空数据:\n  "
        + "\n  ".join(offenders)
        + f"\n确有正当理由的话,连同理由加进 {sorted(_QUERY_WITHOUT_ERROR_BRANCH_ALLOWED)}"
    )


def test_the_query_declaration_forms_stay_within_what_the_scanner_understands():
    """扫描器只认三种写法。出现第四种时**这条会红**,而不是让上一条静默漏掉它。

    这是上面那个正则的安全网:正则漏认一次声明的后果是"那次查询没人管",
    而漏认在门禁上的表现和"全都合格"一模一样。
    """
    root = _frontend_src()
    unknown: list[str] = []
    for path in sorted(root.rglob("*.tsx")):
        src = path.read_text(encoding="utf-8")
        found = len(_query_handles(src))
        declared = len(re.findall(r"\buseQuer(?:y|ies)\s*\(", src))
        if found != declared:
            rel = path.relative_to(root).as_posix()
            unknown.append(f"{rel}: 声明 {declared} 处,认出 {found} 处")
    assert not unknown, (
        "出现了扫描器不认识的查询写法:\n  " + "\n  ".join(unknown)
        + "\n请扩展 _query_handles,不要放着不管 —— 认不出等于不检查"
    )


def test_the_allow_list_does_not_rot():
    """豁免名单里的文件必须还存在、且还真的在发起查询。

    名单最常见的坏法不是被滥用,是**过期**:文件改名或那次查询被删掉之后,
    豁免仍然留着,于是下一个搬进这个路径的组件白白继承了一张免死金牌。
    """
    root = _frontend_src()
    for rel in sorted(_QUERY_WITHOUT_ERROR_BRANCH_ALLOWED):
        path = root / rel
        assert path.exists(), f"豁免名单里的 {rel} 已经不在了,请删掉这条豁免"
        assert "useQuery(" in path.read_text(encoding="utf-8"), (
            f"{rel} 已经不发起查询了,这条豁免该删"
        )


def test_environment_fidelity_values_match_the_backend_enum():
    """状态条认的档位必须和后端 `Fidelity` 一字不差(任务 5/6)。

    前端按档位挑颜色和标题。多一档会挑不到(退回默认那一档,颜色是错的);
    少一档意味着后端加了新档而这里不知道 —— 那一档会走 `?? TONE.UNCONFIGURED`,
    把一个可能更严重的状态显示成"没配好"。
    """
    from app.core.environment import Fidelity

    banner = (
        PROJECT_ROOT / "frontend" / "src" / "components" / "EnvironmentBanner.tsx"
    ).read_text(encoding="utf-8")
    ts = (PROJECT_ROOT / "frontend" / "src" / "api" / "environment.ts").read_text(
        encoding="utf-8"
    )

    backend = {f.value for f in Fidelity}
    # 横幅只列"要显示"的那几档,REAL 是静默的 —— 所以它不该出现在 TONE 里
    tone_keys = set(re.findall(r"^  (\w+): \{", banner, re.M))
    assert tone_keys == backend - {"REAL"}, (
        f"TONE 表与后端档位对不上:后端 {sorted(backend)},TONE {sorted(tone_keys)}。"
        "REAL 不进 TONE 是刻意的(那一档静默)"
    )
    assert "'REAL'" in banner or '"REAL"' in banner or "trustworthy" in banner, (
        "横幅得有办法知道哪一档不用显示"
    )
    assert "trustworthy" in ts, "前端类型里要保留后端算好的 trustworthy,不要自己重算"


def test_the_environment_banner_does_not_recompute_the_verdict():
    """前端不许自己判"哪几档算可信"(硬规则 4)。

    后端已经算好 `trustworthy`。前端再写一遍 `fidelity === 'REAL'` 的话,
    将来加一档"真 Provider + Mock 评分"时两边会各说各话 —— 而它们不一致时,
    横幅显示的和接口返回的是两个结论。
    """
    banner = (
        PROJECT_ROOT / "frontend" / "src" / "components" / "EnvironmentBanner.tsx"
    ).read_text(encoding="utf-8")
    body = window(banner, "export default function", what="横幅组件本体")
    assert "=== 'REAL'" not in body and '=== "REAL"' not in body, (
        "在前端重算了可信判定。用后端给的 `trustworthy`"
    )
