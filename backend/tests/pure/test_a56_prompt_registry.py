"""提示词注册表与内容派生版本的守卫(PRD BLOCK-30,a56)。

覆盖的验收条款:AC-23(注册表一致性)、AC-26(必需槽位)、AC-30 / AC-30b
(版本随内容变、且恒等稳定)、AC-31 离线半(防注入段)、AC-36(fallback)。

模式照 `test_a51_comfyui_config_wiring.py`(PRD 点名沿用):注册表是一张
机器可读的盘点,这里逐条比对 —— 表与代码分叉时,红的是这里,不是线上。
"""
from __future__ import annotations

import ast
import json
from pathlib import Path

from app.core.log_events import LOGGER_DOMAIN_FALLBACK
from app.evaluators.vision_schema import (
    ANTI_INJECTION_BLOCK,
    DEFAULT_SYSTEM_PROMPT,
    DEFAULT_SYSTEM_PROMPT_MEN,
    SCORING_DEPTH_INSTRUCTIONS_KEY,
    SCORING_USER_PROMPT_KEY,
    VISION_SYSTEM_PROMPT,
    VISION_SYSTEM_PROMPT_MEN,
)
from app.extractors.schema import EXTRACTION_PROMPT_KEY
from app.listings.copy_generator import COPY_LLM_SYSTEM_PROMPT_KEY
from app.prompts import registry
from app.prompts.versioning import content_version
from app.services.prompt_rules import KNOWN_KEYS, lint_prompt

BACKEND = Path(__file__).resolve().parents[2]


# ============================================================ AC-23:注册表一致性


def test_every_surface_answers_the_inventory_questions():
    """8 处提示词,每一处答得出 key / tier / 默认值 / 消费方(AC-23)。

    PRD §8 的盘点表答不出「这个引用还解析得出来吗」—— 这条守卫答得出:
    `resolve_default` 悬空会在这里抛,而不是在某个运维打开列表页的下午。
    """
    assert len(registry.PROMPT_SURFACES) == 8, "PRD §8 盘点是 8 处,注册表数目对不上"
    seen: set[str] = set()
    for surface in registry.PROMPT_SURFACES:
        assert surface.key and surface.key not in seen, f"key 重复或为空:{surface.key!r}"
        seen.add(surface.key)
        assert isinstance(surface.tier, registry.Tier)
        assert surface.consumers, f"{surface.key} 的 consumers 是空的 —— 排障的人问谁去"
        if surface.default_ref is None:
            # 没有静态默认的必须在 consumers 里说清楚为什么(如实描述,不是登记不全)。
            assert any("拼装" in one or "默认" in one for one in surface.consumers), (
                f"{surface.key} 没有 default_ref,却没有解释"
            )
            continue
        resolved = registry.resolve_default(surface)
        assert resolved is not None, f"{surface.key} 的 default_ref 解析出了 None"
        if surface.editable:
            # 可落库编辑的,默认值必须是一段拿得出手的文本 —— 页面的
            # 「恢复内置默认」按钮按的就是它。
            assert isinstance(resolved, str) and resolved.strip(), (
                f"{surface.key} 可编辑,但默认值不是非空字符串"
            )


def test_every_surface_has_a_human_readable_preview():
    """登记不等于可用：提示词中心必须让人看到正文，而不只是 key 和代码路径。"""
    for surface in registry.PROMPT_SURFACES:
        preview = registry.resolve_preview(surface)
        assert preview is not None, f"{surface.key} 没有可展示的正文"
        assert registry.preview_text(surface).strip(), f"{surface.key} 的正文不能序列化展示"
        if callable(registry.resolve_default(surface)):
            assert isinstance(preview, str) and preview.strip(), (
                f"{surface.key} 是动态构造器，却没有给出代表性展开结果"
            )

    generation = registry.surface_of("generation_prompt_compose")
    assert generation is not None
    rendered = registry.resolve_preview(generation)
    assert isinstance(rendered, str)
    assert "商业电商影棚" in rendered and "FRONT×1" in rendered


def test_mapping_surfaces_are_editable_json_with_a_real_key_source():
    """MAPPING 可以改值，但默认正文和固定键必须都可校验。"""
    for surface in registry.PROMPT_SURFACES:
        if surface.tier is registry.Tier.MAPPING:
            assert surface.editable, f"{surface.key} 的消费链路已经接库却仍不可编辑"
            assert surface.key_source, f"{surface.key} 没写 key_source"
            default = registry.resolve_default(surface)
            assert isinstance(default, str)
            assert isinstance(json.loads(default), dict)
            assert registry.mapping_keys(surface)


def test_unreachable_surfaces_say_why():
    """`ui_reachable=False` 必须写明原因与解锁条件(PRD §14.1)。

    「后端支持、前端不可达、且无任何记录说明这是有意的」—— 男装提示词的
    这个中间态是 PRD 问题 A,本轮必须消失。显式 False + 原因就是那条记录。
    """
    for surface in registry.PROMPT_SURFACES:
        if surface.ui_reachable:
            continue
        blob = "".join(surface.consumers)
        assert "ui_reachable=False 的原因" in blob, (
            f"{surface.key} 标了前端不可达,却没写原因 —— 中间态又回来了"
        )
        assert "解锁条件" in blob, f"{surface.key} 没写解锁条件"


def test_editable_means_the_consumer_actually_reads_the_store():
    """`editable` 的语义 = 消费链路读库。两边不许分叉。

    保存端点准入必须恰好等于注册表 editable 集合；新增项还必须在下面列出，
    避免只翻一个布尔值就制造「保存成功、消费方不读」的假编辑。
    """
    assert KNOWN_KEYS == registry.editable_keys()
    assert KNOWN_KEYS == (
        VISION_SYSTEM_PROMPT,
        VISION_SYSTEM_PROMPT_MEN,
        SCORING_USER_PROMPT_KEY,
        SCORING_DEPTH_INSTRUCTIONS_KEY,
        EXTRACTION_PROMPT_KEY,
        COPY_LLM_SYSTEM_PROMPT_KEY,
        "repair_prompt_additions",
        "generation_prompt_compose",
    )


def test_the_registry_only_depends_on_core():
    """`app.prompts` 只依赖标准库与 `app.core`。

    lint 在 services 层、评分键选择在 evaluators 层,两边都要 import 注册表 ——
    它一旦 import 领域模块,依赖面就会顺着这两条线灌进 `grading-stays-pure`
    契约。`default_ref` 走 importlib 惰性解析,正是为了这一条。
    """
    for path in sorted((BACKEND / "app" / "prompts").glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and node.module:
                names = [node.module]
            for name in names:
                if name.startswith("app.") and not name.startswith("app.core"):
                    raise AssertionError(
                        f"{path.name} import 了 {name} —— 注册表只许依赖 app.core"
                    )


# ============================================================ AC-30 / AC-30b:版本


def test_prompt_version_changes_with_content_and_only_with_content():
    """改一个字必变(AC-30);同输入 100 次恒等(AC-30b);非字符串直接拒。"""
    base = content_version("你是一名严格的审核员。")
    assert base != content_version("你是一名严格的审核员!")
    assert len({content_version("同一份输入") for _ in range(100)}) == 1
    assert len(base) == 12 and all(c in "0123456789abcdef" for c in base)
    for bad in ({"a": 1}, ["x"], 3.14, None, b"bytes"):
        try:
            content_version(bad)  # type: ignore[arg-type]
        except TypeError:
            continue
        raise AssertionError(
            f"content_version 接受了 {type(bad).__name__} —— 不稳定序列化会造出孤儿版本堆"
        )


def test_the_three_pipelines_derive_their_version_from_content():
    """识别 / 文案 / 出图三条链路都接了内容派生,两个旧常量已删(BE-306)。

    识别与文案在沙箱可直接行为验证;出图链路 import sqlalchemy,
    对它做源码断言 —— 这里断的是「接线存在」,不是行号形状。
    """
    from app.extractors.schema import EXTRACTION_PROMPT_TEMPLATE
    from app.extractors.vision import _EXTRACTION_PROMPT_VERSION
    from app.listings.copy_generator import _LLM_PROMPT_VERSION, LLM_SYSTEM_PROMPT

    assert _EXTRACTION_PROMPT_VERSION == content_version(EXTRACTION_PROMPT_TEMPLATE)
    assert _LLM_PROMPT_VERSION == content_version(LLM_SYSTEM_PROMPT)

    # 「旧常量已删」用 AST 判模块级赋值名,不用源码子串 —— 讲述删除理由的
    # 注释里合法地含着旧常量的名字,子串断言会把自己的墓志铭当成尸体。
    def _module_assign_names(path: Path) -> set[str]:
        names: set[str] = set()
        for node in ast.parse(path.read_text(encoding="utf-8")).body:
            if isinstance(node, ast.Assign):
                names.update(t.id for t in node.targets if isinstance(t, ast.Name))
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                names.add(node.target.id)
        return names

    assert "EXTRACTION_PROMPT_VERSION" not in _module_assign_names(
        BACKEND / "app/extractors/vision.py"
    ), "「改一次提示词必须 bump 一次」的手动常量又回来了"
    copy_names = _module_assign_names(BACKEND / "app/listings/copy_generator.py")
    assert "PROMPT_VERSION" not in copy_names, "靠人记得改的文案版本常量又回来了"
    assert "TEMPLATE_VERSION" in copy_names, (
        "模板生成器(非 LLM)的 TEMPLATE_VERSION 是刻意保留的 —— 它被顺手删了"
    )

    gen_src = (BACKEND / "app/services/generation_service.py").read_text(encoding="utf-8")
    assert "_resolved_prompt_version(prompt, prompt_version)" in gen_src, (
        "出图链路没接内容派生 —— prompt_version 又回到函数默认参数了"
    )
    assert 'prompt_version: str = "v1"' not in gen_src


def test_the_generation_version_resolver_keeps_legacy_identity():
    """出图版本解析:显式值尊重;有提示词按内容;没提示词保持 v1。

    最后一条不是怀旧 —— 存量任务的幂等身份里躺着 `"v1"`,无提示词的任务
    换个版本号等于让同一件事换了个身份。纯逻辑,不碰 DB,直接从源码里
    把函数体取出来验行为(模块整体 import 要 sqlalchemy)。
    """
    src = (BACKEND / "app/services/generation_service.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_resolved_prompt_version"
    )
    namespace: dict = {"content_version": content_version}
    exec(  # noqa: S102 - 执行的是仓库内这一个函数的源码
        compile(ast.Module(body=[fn], type_ignores=[]), "<gen_service>", "exec"),
        namespace,
    )
    resolver = namespace["_resolved_prompt_version"]
    assert resolver("正面全身照", None) == content_version("正面全身照")
    assert resolver("正面全身照", "explicit-7") == "explicit-7"
    assert resolver(None, None) == "v1"


# ============================================================ AC-26 / AC-31:lint 分派


def test_a_template_missing_a_slot_is_named_not_hinted():
    """TEMPLATE 层删掉任一必需槽位,警告指名道姓缺哪个(AC-26)。"""
    surface = registry.surface_of("scoring_user_prompt")
    assert surface is not None and surface.required_slots
    full = "".join("{" + slot + "}" for slot in surface.required_slots)
    assert not [w for w in lint_prompt("scoring_user_prompt", full) if w.code == "missing_slot"]

    dropped = surface.required_slots[0]
    partial = full.replace("{" + dropped + "}", "")
    hits = [w for w in lint_prompt("scoring_user_prompt", partial) if w.code == "missing_slot"]
    assert len(hits) == 1 and dropped in hits[0].message, (
        f"缺 {dropped} 却没被指名:{[w.message for w in hits]}"
    )


def test_mapping_lint_checks_json_and_the_code_owned_key_closure():
    """MAPPING 值可改，删掉代码键或凭空造键都会被明确指出。"""
    repair = registry.surface_of("repair_prompt_additions")
    assert repair is not None
    default = registry.resolve_default(repair)
    assert isinstance(default, str)
    assert not lint_prompt("repair_prompt_additions", default)

    one_key = registry.mapping_keys(repair)[0]
    missing = [w.code for w in lint_prompt(
        "repair_prompt_additions", json.dumps({one_key: {}})
    )]
    assert "missing_mapping_keys" in missing

    parsed = json.loads(default)
    parsed["NOT_REGISTERED"] = {}
    unexpected = [w.code for w in lint_prompt(
        "repair_prompt_additions", json.dumps(parsed)
    )]
    assert "unexpected_mapping_keys" in unexpected

    bad = [w.code for w in lint_prompt("repair_prompt_additions", "{半截")]
    assert bad == ["invalid_json"]
    not_object = [w.code for w in lint_prompt("repair_prompt_additions", "[1,2]")]
    assert not_object == ["invalid_json"]


def test_the_anti_injection_block_is_derived_not_copied():
    """防注入段是从女装正文**切片派生**的,不是第三份手写副本(BE-308)。

    女装与男装 74% 逐字相同的核心正是这一段(PRD 问题 C:三份副本会漂)。
    派生 + 双向包含断言把「漂移」变成 import 时立刻可见的错误;两份默认
    提示词本身一个字节没动 —— AC-34(历史样本分档一致)没有能跑的环境,
    改写正文拼接方式的风险这轮不背。
    """
    assert ANTI_INJECTION_BLOCK.startswith("【最高优先级:数据与指令的边界】")
    assert ANTI_INJECTION_BLOCK in DEFAULT_SYSTEM_PROMPT
    assert ANTI_INJECTION_BLOCK in DEFAULT_SYSTEM_PROMPT_MEN
    assert "SUSPICIOUS_EMBEDDED_INSTRUCTION" in ANTI_INJECTION_BLOCK, (
        "切片边界漂了 —— 段落核心句不在提取结果里"
    )


def test_deleting_the_anti_injection_block_warns_and_defaults_do_not():
    """删掉防注入段出 `no_anti_injection`;内置默认不触发(AC-31 离线半)。

    后一半是「别训练人忽略警告面板」:一打开页面就一片黄的检查,
    等于没有检查。
    """
    naked = "你是审核员。只输出 JSON,不要 markdown,记得 hard_fail。"
    codes = [w.code for w in lint_prompt(VISION_SYSTEM_PROMPT, naked)]
    assert "no_anti_injection" in codes

    for key, default in (
        (VISION_SYSTEM_PROMPT, DEFAULT_SYSTEM_PROMPT),
        (VISION_SYSTEM_PROMPT_MEN, DEFAULT_SYSTEM_PROMPT_MEN),
    ):
        codes = [w.code for w in lint_prompt(key, default)]
        assert "no_anti_injection" not in codes, f"{key} 的内置默认自己触发了警告"


def test_lint_still_speaks_the_old_free_rules():
    """FREE 层的既有规则一条没丢:分派是在前面加了岔路,不是换了路。"""
    codes = {w.code for w in lint_prompt(VISION_SYSTEM_PROMPT, "随便写点散文")}
    assert {"no_json_instruction", "no_anti_injection"} <= codes
    assert [w.code for w in lint_prompt(VISION_SYSTEM_PROMPT, "   ")] == ["empty"]


# ============================================================ AC-36:日志域


def test_app_prompts_is_in_the_logger_domain_fallback():
    """`app.prompts` 进了 fallback 表(PRD §11.2 v2.0 改)。

    没有这一行,这个包里第一个建 logger 的人会让
    `test_every_module_with_a_logger_is_in_the_fallback_table` 变红 ——
    与其等那天,不如现在就把域定了:提示词管理归 settings 域。
    """
    table = dict(LOGGER_DOMAIN_FALLBACK)
    assert table.get("app.prompts") == "settings"
