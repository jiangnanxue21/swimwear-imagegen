"""A45-batch14-7:`evidence_class` 单点派生(PRD §4.8 / §5.1 判定那一半)。

## 这一批守的是一条**正在烧钱**的规则

STATUS「已知限制」那一行:`run_extraction` 今天走 `usable_assets(product_id)`,
只过滤 `status = READY`;于是 AI 生成的候选图如果落成该商品素材,
**会进识别输入并产生一次真实的付费调用**。§5.1 存在的全部理由就是消灭这笔开销。

对策是 PRD 风险表 D3 开的方子:派生 + 单写入点 + 库级 CHECK。
这个文件覆盖前两件 —— 第三件(CHECK / 迁移 / 查询实现)要真库,
不在这台机器上,但**判定已经在这里验过了**,那边只是物化与翻译。

## 写法上的两条讲究

**一、money 那条不变式直接比字符串,不经过 `violates_ai_check`。**
`violates_ai_check` 是库级 CHECK 的 Python 孪生,它自己也可能被写坏。
守卫如果只会转述它的结论,那么把它打瘸的那条变异(D2)就不会响 ——
batch14 阶段 3「32/32 全红好得可疑」正是这个形状:守卫在骗人。
所以最要紧的那条断言不借道任何判定函数,直接和字面量比。

**二、能穷举的就不挑样本。**
`derive_evidence_class` 的输入空间不到五千组,全跑一遍不到一秒。
挑样本意味着「我以为重要的那几组」,而 D3 那种穿透恰恰发生在
没人想到要挑的那一组上。
"""
from __future__ import annotations

import ast
import itertools
from pathlib import Path

from app.core.enums import AssetType, EvidenceClass, MediaRole, MediaSource
from app.media import evidence_rules
from app.media.evidence_rules import (
    derive_evidence_class,
    is_extraction_input,
    violates_ai_check,
)
from app.media.mapping import LEGACY_ASSET_TYPE_TO_ROLE

# isort 把这一行排在 `from app...` 之后,于是它"保证 app 在 sys.path 上"的
# 旧说辞不再成立(上面的 import 已经跑完了)。真正的保证在两个运行器里:
# run_pure_tests.main() 第一句 insert(BACKEND_ROOT),pytest 走 pyproject 的
# pythonpath=["."]。留着它只为与全套件其余文件同一副形状。
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401

APP = Path(evidence_rules.__file__).resolve().parents[1]
RULES_FILE = Path(evidence_rules.__file__).resolve()
EXTRACTION_SERVICE = APP / "attributes" / "service.py"
MEDIA_SERVICE = APP / "media" / "service.py"

#: §5.1 白名单**今天住在哪个函数里**。A45-batch14-19 之前它长在
#: `run_extraction` 里,那一批把它搬进了取数入口(PRD 阶段 2 交付原文
#: 「白名单查询助手成为唯一取数入口」)。
#:
#: 写成常量而不是散在断言里,是因为下面两条守卫原来把「过滤写在
#: run_extraction 里」当成了不变式 —— 搬家那天它们双双变红,而口径
#: 其实更严了。这是同一个形状的**第五次**(见 `DECISIONS.md` §3.33)。
#: 搬家再发生一次时,改这两行就够,断言本身不必动。
WHITELIST_HOST = (MEDIA_SERVICE, "evidence_assets_for")


# ------------------------------------------------------------------ 输入空间

#: 五个正经取值,外加三种「认不出来」:空、缺失、将来新增而这里还没跟上的。
#: 后三种不是凑数 —— 兜底方向写反时,它们是唯一会红的输入。
ALL_SOURCES = (
    *[s.value for s in MediaSource],
    None,
    "",
    "SOME_SOURCE_ADDED_LATER",
)

#: 九个 `MediaRole` 取值 + 缺失 + 那个**不属于 `MediaRole`** 的字符串。
#: 最后一个是刻意的:PRD 规则表按字面写的就是它,而 `MediaRole` 里没有这个成员。
ALL_ROLES = (*[r.value for r in MediaRole], None, "MODEL_REFERENCE")

ALL_LEGACY_TYPES = (*[t.value for t in AssetType], None)


def _every_input():
    """派生函数的整个输入空间。"""
    return itertools.product(
        ALL_SOURCES,
        (None, "task-1"),
        (None, "cand-1"),
        ALL_ROLES,
        ALL_LEGACY_TYPES,
        (False, True),
    )


def _derive(source, task, cand, role, legacy, trusted):
    return derive_evidence_class(
        source=source,
        generation_task_id=task,
        generation_candidate_id=cand,
        role=role,
        legacy_asset_type=legacy,
        imported_url_trusted=trusted,
    )


# ------------------------------------------------- 第一条:AI 图不许成为证据


def test_no_ai_input_can_ever_derive_product_evidence():
    """**这是本批最要紧的一条断言。** 穷举整个输入空间。

    比的是字面量 `"PRODUCT_EVIDENCE"`,不经过 `violates_ai_check` ——
    后者是 CHECK 的孪生,它自己被写坏时,借道它的守卫会一起哑掉。
    """
    offenders = []
    for combo in _every_input():
        source, task, cand, *_ = combo
        ai = task is not None or cand is not None or source == "AI_GENERATED"
        if not ai:
            continue
        result = _derive(*combo)
        if str(result) == "PRODUCT_EVIDENCE":
            offenders.append(combo)
    assert not offenders, (
        f"{len(offenders)} 组 AI 输入派生出了商品证据,白名单被穿透。"
        f"首例:{offenders[0]}"
    )


def test_no_ai_input_can_ever_reach_the_extraction_whitelist():
    """把派生和白名单串起来再穷举一遍 —— 这才是那笔付费调用的完整路径。

    单独验派生是不够的:白名单判定要是认错了等级(比如写成
    「不是 REFERENCE_ONLY 就放行」),派生再对也拦不住。
    """
    leaked = []
    for combo in _every_input():
        source, task, cand, *_ = combo
        ai = task is not None or cand is not None or source == "AI_GENERATED"
        if not ai:
            continue
        if is_extraction_input(evidence_class=_derive(*combo), status_is_ready=True):
            leaked.append(combo)
    assert not leaked, f"{len(leaked)} 组 AI 输入进了识别白名单。首例:{leaked[0]}"


def test_the_derivation_never_produces_a_value_the_db_check_would_reject():
    """派生结果与库级 CHECK 的孪生互不矛盾 —— 整个输入空间。

    上面那条证明的是「派生不产出违规值」;这条证明的是「派生与 CHECK
    对同一组输入的看法一致」。两条都要:CHECK 拦的是绕过派生函数的直写,
    但如果两边口径本来就不一样,合法写入也会在真库上被拒,
    而那种失败要等到有 PostgreSQL 的机器上才看得见。
    """
    for combo in _every_input():
        source, task, cand, *_ = combo
        result = _derive(*combo)
        assert not violates_ai_check(
            evidence_class=result,
            source=source,
            generation_task_id=task,
            generation_candidate_id=cand,
        ), f"派生结果 {result} 会被库级 CHECK 拒绝:{combo}"


def test_the_check_twin_actually_catches_a_hand_built_violation():
    """CHECK 孪生自己得会咬人 —— 上一条否则是平凡通过的。

    三种 AI 来路各验一次:两个溯源列 + AI 来源枚举。
    """
    for kwargs in (
        {"generation_task_id": "task-1"},
        {"generation_candidate_id": "cand-1"},
        {"source": MediaSource.AI_GENERATED.value},
    ):
        payload = {"source": MediaSource.MANUAL_UPLOAD.value, **kwargs}
        assert violates_ai_check(
            evidence_class=EvidenceClass.PRODUCT_EVIDENCE, **payload
        ), f"CHECK 孪生放过了违规组合:{payload}"

    # 反向:AI 来源 + 非商品证据是完全合法的,不该被拦
    assert not violates_ai_check(
        evidence_class=EvidenceClass.GENERATED_RESULT,
        source=MediaSource.AI_GENERATED.value,
        generation_task_id="task-1",
    )


def test_a_candidate_id_alone_is_enough_to_be_a_generated_result():
    """PRD 的 CHECK 原文只点了 `generation_task_id`(见 evidence_rules 文档第四条)。

    一条只有 `generation_candidate_id` 的记录同样是 AI 产物,而按原文那条
    CHECK,它可以合法地成为 `PRODUCT_EVIDENCE`。落库时两个溯源列都要写进 CHECK。
    """
    assert (
        derive_evidence_class(
            source=MediaSource.MANUAL_UPLOAD.value, generation_candidate_id="cand-1"
        )
        is EvidenceClass.GENERATED_RESULT
    )


# ------------------------------------------- 第二条:模特参考图那条死分支


def test_the_model_reference_rule_fires_through_the_legacy_asset_type():
    """**今天只有这一路是活的。**

    PRD 写的是 `role = MODEL_REFERENCE`,而 `MediaRole` 没有这个成员 ——
    按字面落码是一条死分支。见 `evidence_rules` 文档第一条。
    """
    assert (
        derive_evidence_class(
            source=MediaSource.MANUAL_UPLOAD.value,
            legacy_asset_type=AssetType.MODEL_REFERENCE.value,
        )
        is EvidenceClass.REFERENCE_ONLY
    )


def test_a_real_model_shot_of_the_product_is_still_product_evidence():
    """把 `MODEL_FRONT` 当成模特参考图,会掐掉识别最想要的那类图。

    这是「按字面写不通就改认 MODEL_FRONT」那条歧路的守卫。
    `MODEL_FRONT` 上**同时**坐着两种图(见下一条),而误伤的代价是
    每一张「模特穿着本商品」的正经图都退出证据集。
    """
    for role in (MediaRole.MODEL_FRONT, MediaRole.MODEL_BACK):
        assert (
            derive_evidence_class(
                source=MediaSource.MANUAL_UPLOAD.value, role=role.value
            )
            is EvidenceClass.PRODUCT_EVIDENCE
        ), f"{role} 被误当成模特参考图"


def test_the_model_reference_gap_is_still_exactly_where_we_left_it():
    """把今天这个「凑合状态」钉住,别让它无声退化。

    三件事一起成立时,`legacy_asset_type` 那个入参是模特参考图与识别输入
    之间**唯一**的东西:

        一、`MediaRole` 没有 `MODEL_REFERENCE` 成员
        二、旧枚举有,而且映射是有损的:`MODEL_REFERENCE -> MODEL_FRONT`
        三、于是新模型里,模特参考图和正经模特图坐在同一个 role 上

    第一条哪天不成立了(补了 `MediaRole.MODEL_REFERENCE`),这条守卫会红 ——
    那时该做的是把 `role` 那一路接上,并重写这条守卫,而不是删掉它。
    """
    assert "MODEL_REFERENCE" not in {r.value for r in MediaRole}, (
        "`MediaRole` 多了 MODEL_REFERENCE —— role 那一路可以接上了,"
        "请同时重写本守卫与 evidence_rules 文档第一条"
    )
    assert (
        LEGACY_ASSET_TYPE_TO_ROLE[AssetType.MODEL_REFERENCE.value]
        is MediaRole.MODEL_FRONT
    ), "旧枚举映射变了,模特参考图的去向要重新算"


def test_model_reference_wins_over_a_trustworthy_source():
    """人工上传的模特参考图,`source` 是 MANUAL_UPLOAD —— 顺序错了就成证据。

    后果不是多花一次钱,是**从一张「模特长什么样」的图上读出这件泳衣的领型**。
    """
    for source in (MediaSource.MANUAL_UPLOAD, MediaSource.SUPPLIER_FEED):
        assert (
            derive_evidence_class(
                source=source.value,
                legacy_asset_type=AssetType.MODEL_REFERENCE.value,
                imported_url_trusted=True,
            )
            is EvidenceClass.REFERENCE_ONLY
        )


def test_generated_wins_over_model_reference():
    """两条降级规则撞在一起时,溯源那条赢。

    结果都进不了识别,但素材库里「这是我们生成的」和「这是参考图」
    是两件事 —— 前者要能按任务追回去。
    """
    assert (
        derive_evidence_class(
            source=MediaSource.MANUAL_UPLOAD.value,
            generation_task_id="task-1",
            legacy_asset_type=AssetType.MODEL_REFERENCE.value,
        )
        is EvidenceClass.GENERATED_RESULT
    )


def test_only_the_two_reference_asset_types_lose_evidence_status():
    """降级只降那两个「参考」旧类型,别顺手把细节图、透明底图一起降了。

    `BACKGROUND_REFERENCE` 那一半是这条守卫**穷举**旧枚举时撞出来的 ——
    PRD 规则表只点了模特参考图(见 `evidence_rules` 文档第五条)。
    挑样本的写法撞不到它:没人会专门挑一张背景布的照片来验证据等级。
    """
    for kind in AssetType:
        expected = (
            EvidenceClass.REFERENCE_ONLY
            if kind.value in evidence_rules.REFERENCE_ONLY_MARKERS
            else EvidenceClass.PRODUCT_EVIDENCE
        )
        got = derive_evidence_class(
            source=MediaSource.MANUAL_UPLOAD.value, legacy_asset_type=kind.value
        )
        assert got is expected, f"{kind} 派生成了 {got},期望 {expected}"
    assert evidence_rules.REFERENCE_ONLY_MARKERS == {
        "MODEL_REFERENCE",
        "BACKGROUND_REFERENCE",
    }, "参考类标记集合变了 —— 先确认新增/删除的那个到底该不该算证据"


# ------------------------------------------------- 第三条:外链的「可信」


def test_an_imported_url_is_not_evidence_until_someone_says_it_is():
    """全仓没有可信域名注册表(见 `evidence_rules` 文档第二条)。

    默认可信 = 谁贴一条外链谁就产出一条商品证据,那是把 §5.1 的口子
    换个地方重开一遍。
    """
    assert (
        derive_evidence_class(source=MediaSource.IMPORTED_URL.value)
        is EvidenceClass.REFERENCE_ONLY
    )
    assert (
        derive_evidence_class(
            source=MediaSource.IMPORTED_URL.value, imported_url_trusted=True
        )
        is EvidenceClass.PRODUCT_EVIDENCE
    )


def test_the_trust_flag_only_ever_moves_imported_urls():
    """可信标记不该顺带提升别的来源 —— 穷举五个枚举取值各验一遍。"""
    for source in MediaSource:
        if source is MediaSource.IMPORTED_URL:
            continue
        assert derive_evidence_class(
            source=source.value, imported_url_trusted=False
        ) is derive_evidence_class(source=source.value, imported_url_trusted=True), (
            f"可信标记改变了 {source} 的派生结果"
        )


# ------------------------------------------------- 第四条:兜底方向


def test_an_unrecognised_source_fails_closed():
    """PRD 的规则表没有 else 分支(见 `evidence_rules` 文档第三条)。

    空值、脏数据、将来新增而这里还没跟上的枚举值,都必须落在「不算证据」那边。
    兜底写成 `PRODUCT_EVIDENCE` 的话,加一个新来源枚举就等于开一个新口子,
    而加枚举的那个人不会想到自己动了识别成本。
    """
    for source in (None, "", "SOME_SOURCE_ADDED_LATER", "manual_upload"):
        assert (
            derive_evidence_class(source=source) is EvidenceClass.REFERENCE_ONLY
        ), f"来源 {source!r} 兜底成了商品证据"


def test_platform_sync_is_its_own_class():
    """回抓图讲的是「平台上现在挂着什么」,不是商品事实。

    它和 `REFERENCE_ONLY` 在白名单眼里没区别(都进不去),分开是给人看的:
    一次上架错误如果在下一轮识别里被洗成「确认过的事实」,
    追责时要能分清这张图是从哪回来的。
    """
    assert (
        derive_evidence_class(source=MediaSource.PLATFORM_SYNC.value)
        is EvidenceClass.CHANNEL_DERIVATIVE
    )


# ------------------------------------------------- 第五条:白名单判定那一半


def test_only_ready_product_evidence_enters_extraction():
    """两个条件都要,穷举四个等级 × 两种状态。"""
    for cls in EvidenceClass:
        for ready in (True, False):
            expected = ready and cls is EvidenceClass.PRODUCT_EVIDENCE
            assert (
                is_extraction_input(evidence_class=cls, status_is_ready=ready)
                is expected
            ), f"{cls} / ready={ready} 的白名单判定错了"


def test_the_whitelist_takes_a_class_not_a_source():
    """白名单不再自己查 source —— 那会造出第二个判定点。

    两个判定点漂移时没人会发现:派生说这是 AI 图,白名单按 source 说
    这不是 AI 图,而两边都"有道理"。
    """
    names = _params(_func(RULES_FILE, "is_extraction_input"))
    assert "source" not in names, "白名单判定长出了对 source 的第二个判定点"
    assert names == ["evidence_class", "status_is_ready"], f"入参变了:{names}"


# ------------------------------------------------- 第六条:单写入点(AST)


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _func(path: Path, name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里没有函数 {name}")


def _params(node: ast.FunctionDef) -> list[str]:
    args = node.args
    return [a.arg for a in (*args.posonlyargs, *args.args, *args.kwonlyargs)]


def _app_sources() -> list[Path]:
    return sorted(p for p in APP.rglob("*.py") if p.resolve() != RULES_FILE)


def test_nobody_outside_the_rules_module_computes_an_evidence_class():
    """单写入点(§4.8「唯一写入函数,禁止路由层/脚本直写」),按投影列那套模式。

    判据是「赋给 `evidence_class` 的**值**是不是 `derive_evidence_class(...)`
    的返回」。允许赋值、不允许自己算 —— 服务层本来就要把派生结果落到列上,
    禁的是绕过派生函数直接写一个字面量或另一套 if。

    今天全仓零命中(存储列还没落),所以这条现在扫不出东西 —— 它是给
    阶段 2 落库那一刀准备的。留在这里而不是等落库时再写,是因为
    「先有守卫」这件事在 batch13-3 / M11 上已经付过学费。
    """
    offenders: list[str] = []
    for path in _app_sources():
        for node in ast.walk(_tree(path)):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, ast.AnnAssign):
                targets = [node.target]
            else:
                continue
            hit = any(
                (isinstance(t, ast.Attribute) and t.attr == "evidence_class")
                or (isinstance(t, ast.Name) and t.id == "evidence_class")
                for t in targets
            )
            if not hit or node.value is None:
                continue
            # 模型层的列声明(`evidence_class: Mapped[str] = mapped_column(...)`)
            # 是声明不是写入,放行
            if isinstance(node, ast.AnnAssign) and path.parent.name == "models":
                continue
            call = node.value
            ok = (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Name)
                and call.func.id == "derive_evidence_class"
            ) or (
                isinstance(call, ast.Call)
                and isinstance(call.func, ast.Attribute)
                and call.func.attr == "derive_evidence_class"
            )
            if not ok:
                offenders.append(f"{path.relative_to(APP.parent)}:{node.lineno}")
    assert not offenders, f"绕过派生函数直写 evidence_class:{offenders}"


def test_the_four_class_names_are_not_hardcoded_anywhere_else():
    """四个取值的字面量只许出现在枚举与判定模块里。

    别处出现一个 `"PRODUCT_EVIDENCE"` 字符串,意味着有人在那里
    重新实现了一遍白名单条件 —— 而 D3 说的就是这种第二实现会漂移。
    走属性访问(`EvidenceClass.PRODUCT_EVIDENCE.value`)不受影响,
    那正是希望大家用的写法。
    """
    allowed = {"enums.py", "evidence_rules.py"}
    names = {c.value for c in EvidenceClass}
    offenders: list[str] = []
    for path in _app_sources():
        if path.name in allowed:
            continue
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Constant) and node.value in names:
                offenders.append(f"{path.relative_to(APP.parent)}:{node.lineno} {node.value}")
    assert not offenders, f"证据等级被硬编码在别处:{offenders}"


def test_the_rules_module_stays_dependency_free():
    """这个模块的全部价值在于「没有数据库也能穷举」。一条 import 就能毁掉它。"""
    forbidden = {"sqlalchemy", "fastapi", "pydantic", "httpx", "celery", "PIL"}
    seen: set[str] = set()
    for node in ast.walk(_tree(RULES_FILE)):
        if isinstance(node, ast.Import):
            seen.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            seen.add(node.module.split(".")[0])
    leaked = sorted(forbidden & seen)
    assert not leaked, f"判定模块长出了三方依赖:{leaked}"
    assert seen <= {"__future__", "typing", "app"}, f"意料之外的 import:{sorted(seen)}"


# ------------------------------------------------- 第七条:刻意不作为输入


def test_the_derivation_deliberately_ignores_role_source_and_status():
    """两个「本该在这里、但故意不在」的入参,理由见 `evidence_rules` 文档。

    这条守卫钉的是一个决定,不是一段行为:哪天有人把 `role_source` 加进来,
    它会红,红的意思是「先去读那段理由,再决定要不要删掉这条守卫」。

    `role_source`  降级要不要等人工确认?不等 —— 等的代价是在人来点确认
                   之前每一轮识别都照付。
    `status`       「能不能用」由白名单的 READY 回答,编进证据等级会造出
                   第二个事实源,而人工放行会改状态、不该改证据等级。
    """
    names = _params(_func(RULES_FILE, "derive_evidence_class"))
    for forbidden in ("role_source", "status", "session", "asset"):
        assert forbidden not in names, (
            f"派生函数长出了入参 {forbidden} —— 先读 evidence_rules 顶部"
            f"「两个刻意不作为输入的东西」那一节"
        )


class _FakeAsset:
    """假素材行。刻意**不带**两个溯源列,复现今天的模型形状。"""

    def __init__(self, source, status="READY", role=None, **extra):
        self.source = source
        self.status = status
        self.role = role
        for k, v in extra.items():
            setattr(self, k, v)


def test_the_asset_adapter_keeps_real_uploads_and_drops_ai_candidates():
    """适配器的语义,拿假对象穷举 —— 这是接线唯一能在纯层验到的一层。

    `shadow_from_candidate` 写的正是 `source=AI_GENERATED` + `status=READY`,
    所以第二条断言复现的就是那条**每一轮生成之后必然被踩到**的路径。
    """
    keep = _FakeAsset(MediaSource.MANUAL_UPLOAD.value)
    assert evidence_rules.asset_is_extraction_input(keep)

    candidate = _FakeAsset(MediaSource.AI_GENERATED.value)
    assert not evidence_rules.asset_is_extraction_input(candidate), (
        "AI 候选图进了识别输入 —— 这正是每张一次真实付费调用的那条路"
    )


def test_the_asset_adapter_covers_every_source_and_status():
    """穷举 5 个来源 × 5 个状态 × 有没有溯源列。"""
    for source in MediaSource:
        for status in ("READY", "PENDING", "QUARANTINED", "FAILED", "DELETED"):
            asset = _FakeAsset(source.value, status=status)
            got = evidence_rules.asset_is_extraction_input(
                asset, imported_url_trusted=True
            )
            expected = status == "READY" and source in {
                MediaSource.MANUAL_UPLOAD,
                MediaSource.SUPPLIER_FEED,
                MediaSource.IMPORTED_URL,
            }
            assert got is expected, f"{source}/{status} 判成了 {got}"


def test_the_adapter_starts_honouring_the_traceability_columns_the_day_they_land():
    """两个溯源列今天不存在,列一落库适配器要自动开始生效。

    写死 None 的话,列落库**之后**它会继续瞎 —— 不报错、不留痕,
    要等有人查账单才发现 AI 图又进来了。这里拿一个带列的假对象验前向兼容。
    """
    for column in ("generation_task_id", "generation_candidate_id"):
        asset = _FakeAsset(MediaSource.MANUAL_UPLOAD.value, **{column: "x-1"})
        assert not evidence_rules.asset_is_extraction_input(asset), (
            f"{column} 落库后适配器没有认它"
        )


def test_the_adapter_defaults_imported_urls_to_untrusted():
    """默认值留给新调用点,新调用点该 fail closed。"""
    asset = _FakeAsset(MediaSource.IMPORTED_URL.value)
    assert not evidence_rules.asset_is_extraction_input(asset)
    assert evidence_rules.asset_is_extraction_input(asset, imported_url_trusted=True)


def test_run_extraction_puts_every_asset_through_the_whitelist():
    """接线守卫:识别取数必须经过适配器,**且过滤结果必须真的被用上**。

    这一条守的不是判定,是「判定有没有被接上」。判定验得再干净,
    识别路径不调用它、或者算出来不用,那条付费路径一分钱都没省下 ——
    而套件会全绿(`verify_delivery` 的接线门禁是白名单式的)。

    ## 断言钉的是"证据从哪来",不是"过滤写在哪"

    原来这条直接在 `run_extraction` 的函数体里找 `asset_is_extraction_input`。
    A45-batch14-19 把过滤搬进取数入口之后它红了,**而口径其实更严了** ——
    这是"点名做法"的第五次(§3.33)。

    现在分两段问:

        一、白名单宿主(`WHITELIST_HOST`)真的在过滤,且结果就是它的返回值
        二、`run_extraction` 的证据来自那个宿主,而不是别处

    第二段顺带钉死一条新的:识别路径里**不许出现 `usable_assets`**。
    那个函数返回未过滤的行,而识别路径上只要出现过一个装着未过滤行的变量,
    下一个人就可能顺手拿它去喂抽取器 —— 那一步不会有任何地方报错。
    """
    host_path, host_name = WHITELIST_HOST
    host = _func(host_path, host_name)
    host_calls = {
        c.func.attr
        for c in ast.walk(host)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
    }
    assert "asset_is_extraction_input" in host_calls, (
        f"{host_name}() 没有过证据白名单 —— 取数入口不过滤等于没有白名单"
    )
    returns = [n for n in ast.walk(host) if isinstance(n, ast.Return) and n.value]
    assert returns and any(
        any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == "asset_is_extraction_input"
            for c in ast.walk(r.value)
        )
        for r in returns
    ), (
        f"{host_name}() 算了白名单,但返回的不是过滤后的结果 —— "
        "「算出来了但没用」是接线最常见的坏法"
    )

    node = _func(EXTRACTION_SERVICE, "run_extraction")
    used = {
        c.func.attr
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
    }
    assert host_name in used, (
        f"run_extraction 没有走 {host_name}() —— 证据是从别的地方取的"
    )
    assert "usable_assets" not in used, (
        "run_extraction 里出现了 usable_assets —— 它返回未过滤的行。"
        "只要有一个装着未过滤行的变量,下一个人就可能拿它去喂抽取器,"
        "而那一步不会有任何地方报错。要条数请用 usable_asset_count()"
    )


def test_the_whitelist_condition_is_not_negated_or_wrapped():
    """**这一条是变异 P2 撞出来的洞补的。**

    条件前面加一个 `not`,过滤就从「留下能进的」变成「留下不能进的」——
    识别会**只跑在 AI 图上**,后果比不修还严重(付费调用一次不少,
    而证据全部来自模型自己的输出)。

    而这个改动对前面几条守卫完全隐形:调用还在、结果照样赋回 `assets`、
    没有硬编码字面量、没有第二套条件。AST 看得见「有没有调用」,
    看不见「调用前面多了个 not」—— 除非专门去看条件的形状。

    所以这里钉死形状:过滤条件必须**就是那一个调用本身**,
    不许取反、不许和别的条件拼、不许只有半个。

    A45-batch14-19:窗口从 `run_extraction` 换成 `WHITELIST_HOST` ——
    形状这条性质跟着过滤一起搬了家,断言本身一个字没改。
    """
    node = _func(*WHITELIST_HOST)
    comps = [
        c
        for c in ast.walk(node)
        if isinstance(c, ast.ListComp)
        and any(
            isinstance(x, ast.Call)
            and isinstance(x.func, ast.Attribute)
            and x.func.attr == "asset_is_extraction_input"
            for x in ast.walk(c)
        )
    ]
    assert len(comps) == 1, f"白名单过滤出现了 {len(comps)} 处,应当只有一处"
    conditions = comps[0].generators[0].ifs
    assert len(conditions) == 1, f"过滤条件有 {len(conditions)} 个,应当只有一个"
    only = conditions[0]
    assert isinstance(only, ast.Call), (
        f"过滤条件不是一个裸调用,而是 {type(only).__name__} —— "
        "取反或拼接都会让白名单反过来筛"
    )
    assert (
        isinstance(only.func, ast.Attribute)
        and only.func.attr == "asset_is_extraction_input"
    ), "过滤条件调用的不是白名单适配器"


def test_run_extraction_does_not_write_its_own_filter_conditions():
    """接线点不许自己写第二套条件 —— 那正是 D3 描述的漂移。

    判定住在 `evidence_rules`;这里连入参拼装都不该有。
    """
    node = _func(EXTRACTION_SERVICE, "run_extraction")
    literals = {
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and n.value in {c.value for c in EvidenceClass}
    }
    assert not literals, f"识别服务里硬编码了证据等级:{sorted(literals)}"
    calls = {
        c.func.attr
        for c in ast.walk(node)
        if isinstance(c, ast.Call) and isinstance(c.func, ast.Attribute)
    }
    assert "derive_evidence_class" not in calls, (
        "服务层又自己拼派生入参了 —— 那层翻译属于 evidence_rules"
    )


def test_an_all_filtered_run_says_so_instead_of_saying_no_assets():
    """全被过滤掉时的报错要和「一张图都没有」分开。

    素材库里明明摆着十几张图却告诉运营「没有可用素材」,会让人去传更多图,
    而传进来的如果还是 AI 图,一张也进不来 —— 那是一个能循环好几轮的误导。
    """
    node = _func(EXTRACTION_SERVICE, "run_extraction")
    messages = [
        n.value
        for n in ast.walk(node)
        if isinstance(n, ast.Constant) and isinstance(n.value, str) and "素材" in n.value
    ]
    joined = " ".join(messages)
    assert "不能作为识别证据" in joined, "全被过滤掉时没有单独的报错"
    assert "没有可用于识别的素材" in joined, "「一张图都没有」那条报错被改掉了"


def test_the_exhaustive_space_is_actually_exhaustive():
    """**这条守的是上面那几条穷举守卫本身。**

    「AI 图不许成为证据」如果因为输入空间塌了而平凡通过,是最难看见的一种假绿:
    把 `ALL_SOURCES` 删剩一个、或者把 `ai` 那个过滤条件写反,循环体一次都不执行,
    22 条照样全绿。batch14 阶段 3 那次「32/32 全红好得可疑」就是这个形状的另一面。

    所以这里把三件事钉死:空间多大、AI 子集多大、四个等级**各自都真的被产出过**。
    最后一件尤其要紧 —— 派生函数要是塌成恒返回 `REFERENCE_ONLY`,
    「没有 AI 输入能成为商品证据」会是一句正确的废话。

    数字变了不一定是错,但一定是**有人动了输入空间或派生规则**,
    该回来确认新增的那个取值落在哪一类,而不是顺手改掉期望值。
    """
    combos = list(_every_input())
    assert len(combos) == 4928, f"输入空间从 4928 变成了 {len(combos)}"

    ai = [c for c in combos if c[1] is not None or c[2] is not None or c[0] == "AI_GENERATED"]
    assert len(ai) == 3850, f"AI 子集从 3850 变成了 {len(ai)}"
    assert len(combos) - len(ai) == 1078, "非 AI 子集变了"

    produced: dict[str, int] = {}
    for combo in combos:
        key = str(_derive(*combo))
        produced[key] = produced.get(key, 0) + 1
    assert produced == {
        "GENERATED_RESULT": 3850,
        "REFERENCE_ONLY": 728,
        "PRODUCT_EVIDENCE": 250,
        "CHANNEL_DERIVATIVE": 100,
    }, f"派生结果分布变了:{produced}"


def test_the_enum_carries_exactly_the_four_prd_classes():
    """四个取值是 PRD §4.8 定死的,库级 CHECK 与白名单都按它写。

    少一个:那类素材会掉进别的等级,而掉进哪个是运气。
    多一个:白名单的 `= PRODUCT_EVIDENCE` 还是对的,但素材库里会出现
    一个没人定义过下游待遇的等级。
    """
    assert {c.value for c in EvidenceClass} == {
        "PRODUCT_EVIDENCE",
        "GENERATED_RESULT",
        "REFERENCE_ONLY",
        "CHANNEL_DERIVATIVE",
    }
    for member in EvidenceClass:
        assert member.name == member.value, f"{member.name} 的名与值不一致"
