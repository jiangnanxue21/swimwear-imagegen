"""A45-batch14-16:溯源列落库,候选落盘写入(§4.8)。

三个判定早就写好在读这两列(`getattr` 取),它们缺的一直是列本身:

    evidence_rules.derive_evidence_class   §4.8 规则表的第一条
    provenance_conflict.verdict            AC-22「AI 图伪装拦截」
    provenance_conflict.may_fill_role      去重命中那一路的补角色闸

后两条在这两列之前**只有过渡记号那一路真的会命中** —— §11 那批自己写下
这句话并把它钉成了断言。本批把「唯一」那个词收掉。
"""
from __future__ import annotations

import ast
import re

from app.media import evidence_rules as er
from tests.pure._helpers import BACKEND_ROOT, window

APP = BACKEND_ROOT / "app"
MIGRATIONS = BACKEND_ROOT / "migrations" / "versions"
COLUMNS = ("generation_task_id", "generation_candidate_id")


def _src(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


def _decl(block: str, name: str) -> str:
    """切出一条列声明。`    )` 是分隔符不是地标,所以按行扫(§3.30 第三节)。"""
    lines = block.splitlines()
    begin = next(i for i, ln in enumerate(lines) if f"{name}: Mapped" in ln)
    stop = next(i for i in range(begin + 1, len(lines)) if lines[i].rstrip() == "    )")
    return "\n".join(lines[begin : stop + 1])


# ================================================================ 一、列


def test_both_provenance_columns_exist_and_stay_nullable():
    """可空是硬要求:人工上传的样品**本来就没有**溯源,
    NOT NULL 会让整条上传链路当场不可用。"""
    block = window(_src("models/media_asset.py"), "class MediaAsset", "class MediaDerivative")
    for column in COLUMNS:
        decl = _decl(block, column)
        assert "nullable=True" in decl, f"{column} 不再可空 —— 人工上传的图没有溯源可填"
        assert "ondelete=" in decl and "SET NULL" in decl, (
            f"{column} 不是 SET NULL —— 清理生成任务会连坐拦住素材"
        )
        assert "index=True" in decl, f"{column} 没有索引 —— §5.1 的白名单按它过滤"


def test_the_migration_is_additive_and_does_not_backfill():
    """**刻意不回填。**回填要 join 候选表并信任 `legacy_id` 指得到候选行,
    而那是一个迁移期约定、不是外键 —— `legacy_id` 上没有任何约束。

    回填错的表现是**一张人工上传的样品被标成 AI 生成**,从此永久掉出
    识别输入,而没有任何地方会说为什么。
    """
    text = (MIGRATIONS / "0038_media_asset_provenance.py").read_text(encoding="utf-8")
    assert 'revision: str = "0038"' in text
    assert 'down_revision: Union[str, None] = "0037"' in text
    up = window(text, "def upgrade()", "def downgrade()")
    for forbidden in ("UPDATE ", "execute(", "alter_column", "legacy_id"):
        assert forbidden not in up, f"这条迁移在回填或改既有列:{forbidden}"
    # 列名由模块级的 `_COLUMNS` 驱动(两列形状相同,循环比抄两遍安全),
    # 所以名字查全文而不是查 upgrade() 那一段 —— 第一版查窄了,红在这里
    for column in COLUMNS:
        assert column in text, f"迁移里没有 {column}"
    down = window(text, "def downgrade()", what="downgrade")
    assert down.count("drop_column") == 1 and "for column" in down, "回退拿不回去"


# ================================================================ 二、候选落盘


def _shadow_call() -> ast.Call:
    tree = ast.parse(_src("media/service.py"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "shadow_from_candidate"
    )
    return next(
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "ingest"
    )


def test_the_candidate_shadow_writes_both_provenance_columns():
    """§4.8 的「候选落盘写入」就是这一处。少写一条的表现不是报错,
    是那张 AI 图在 §5.1 白名单和 AC-22 拦截里少一个信号。"""
    keywords = {kw.arg for kw in _shadow_call().keywords}
    for column in COLUMNS:
        assert column in keywords, f"候选落盘没写 {column}"


def test_the_task_id_comes_from_the_candidates_own_task():
    """取的是候选行的 `task_id`,不是 attempt 或别的什么。

    取错的表现是溯源**指向另一次生成** —— 而按它去查「这张图是哪来的」
    会查到一条无关的任务,那比没有溯源更难查。
    """
    for kw in _shadow_call().keywords:
        if kw.arg == "generation_task_id":
            assert isinstance(kw.value, ast.Attribute) and kw.value.attr == "task_id", (
                "generation_task_id 不是取自 candidate.task_id"
            )
        if kw.arg == "generation_candidate_id":
            assert isinstance(kw.value, ast.Attribute) and kw.value.attr == "id"


def test_the_legacy_marker_is_kept_alongside_the_real_columns():
    """**两条并存,不是二选一。**

    存量的 AI 影子行只有过渡记号,而本批刻意不回填。删掉它的表现是
    历史上所有 AI 影子行一次性失去溯源 —— 而判定认「任一信号命中即算
    带溯源」,所以并存不会造成任何重复判定。
    """
    keywords = {kw.arg for kw in _shadow_call().keywords}
    assert "legacy_kind" in keywords and "legacy_id" in keywords, (
        "过渡记号被删了 —— 存量 AI 影子行会一次性失去溯源"
    )


def test_ingest_actually_stores_what_it_was_handed():
    """入参收了得真的落到行上。收了不写是「算好了没人读」那一类,
    而这一类在这里的后果是每一张新 AI 图都少两个溯源信号。"""
    tree = ast.parse(_src("media/service.py"))
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "ingest"
    )
    args = {a.arg for a in fn.args.kwonlyargs} | {a.arg for a in fn.args.args}
    row = next(
        n for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "MediaAsset"
    )
    written = {kw.arg for kw in row.keywords}
    for column in COLUMNS:
        assert column in args, f"ingest 不收 {column}"
        assert column in written, f"ingest 收了 {column} 却没写进行里"


# ================================================================ 三、判定现在真的用得上


def test_a_row_with_only_a_candidate_id_is_a_generated_result():
    """**PRD 的 CHECK 原文漏了 `generation_candidate_id`**(14-8 第三节记的)。

    在这两列落库之前,这条只能拿假对象验;现在它是一条真实可达的数据形状:
    候选落盘写的正是这两列。
    """
    assert er.derive_evidence_class(
        source="MANUAL_UPLOAD", generation_candidate_id="cand-1"
    ) == er.EvidenceClass.GENERATED_RESULT
    assert er.derive_evidence_class(
        source="MANUAL_UPLOAD", generation_task_id="task-1"
    ) == er.EvidenceClass.GENERATED_RESULT


def test_such_a_row_never_enters_the_extraction_input():
    """AC-22 那条动线的终点:一张被人重新上传成 MANUAL_UPLOAD、
    但指着候选行的图,不进识别输入 —— 不产生付费调用。"""

    class _Row:
        source = "MANUAL_UPLOAD"
        status = "READY"
        role = "PRODUCT_FRONT"
        generation_task_id = None
        generation_candidate_id = "cand-1"

    assert er.asset_is_extraction_input(_Row()) is False
    plain = _Row()
    plain.generation_candidate_id = None
    assert er.asset_is_extraction_input(plain) is True, (
        "反向也得钉住:干净的人工样品必须进得来,否则上面那条可能是"
        "「什么都不让进」平凡通过"
    )


def test_the_model_declares_the_columns_the_judgements_read():
    """三个判定读的列名,和模型上的列名必须是同一套。

    读错列名不会报错(`getattr` 拿不到就是 None),只会让 AI 图静静地
    重新变成一条干净的素材。
    """
    block = window(_src("models/media_asset.py"), "class MediaAsset", "class MediaDerivative")
    declared = set(re.findall(r"^    (\w+): Mapped", block, re.M))
    for source_file in ("media/evidence_rules.py", "media/provenance_conflict.py"):
        text = _src(source_file)
        read = set(re.findall(r'getattr\(\s*\w+,\s*"(generation_\w+)"', text))
        assert read <= declared, f"{source_file} 读了模型上没有的列:{sorted(read - declared)}"
        assert read, f"{source_file} 没有读任何溯源列"
