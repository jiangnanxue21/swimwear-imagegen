"""A44 第六批:A-10 · A-22 · A-23 · C-04(部分)· C-11(部分)· C-14 · C-17 ·
R1-15 · R1-36 · R1-39。

C-14 那几条是**新立的静态规则**,不是修某一处代码 —— 评审说的就是
「防御到位,缺的是静态规则/契约测试」。
"""
from __future__ import annotations

import ast
import re

from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"
PROJECT = BACKEND_ROOT.parent
FRONTEND = PROJECT / "frontend" / "src"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", without_block)


def _no_hash_comments(source: str) -> str:
    return re.sub(r"(?<!\$)#[^\n]*", "", source)


def _fn(path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(_read(path))):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到 {name}")


# ==========================================================
# A-22:付费之后失败的那笔钱
# ==========================================================


def test_a_failure_after_payment_can_report_what_it_spent():
    """A-22:异常分支不再硬编码 0。

    改之前无论异常发生在付费前还是付费后,台账都记 0。少记的那一笔
    **不会让任何数字对不上**,所以永远不会有人发现 —— 与同仓"宁可多算
    不少算"的方向正好相反。

    猜不出来就不猜:执行器自己知道花没花钱,约定由它在异常上带出来。
    """
    from app.workbench import batch as rules

    assert rules.paid_calls_of(rules.PaidCallFailure("炸了", paid_calls=3)) == 3
    assert rules.paid_calls_of(ValueError("没带")) == 0
    # 包过一层的异常只要把属性透传出来就仍然算数(读属性不判类型)
    wrapped = RuntimeError("外层")
    wrapped.paid_calls = 2
    assert rules.paid_calls_of(wrapped) == 2
    # 属性存在但不是个数 —— 装作没看见,不要在失败路径上抛第二个异常
    broken = RuntimeError("坏值")
    broken.paid_calls = "两次"
    assert rules.paid_calls_of(broken) == 0
    assert rules.paid_calls_of(rules.PaidCallFailure("负数", paid_calls=-1)) == 0

    src = ast.unparse(_fn(APP / "workbench" / "batch.py", "run_plan"))
    assert "paid_calls_of(exc)" in src, "异常分支又硬编码 paid_calls 了(A-22)"


# ==========================================================
# A-23:过滤必须发生在 UPDATE 之前
# ==========================================================


def test_already_seen_items_are_never_claimed_in_the_first_place():
    """A-23:`seen` 下推到 SQL,不在 Python 里过滤。

    留在 Python 里的话,被过滤掉的行**已经被 `claim_items` 改过了**:
    置 RUNNING、盖新租约、`attempts + 1`。本轮既不跑也不放,白烧一次尝试;
    烧满 `MAX_ITEM_ATTEMPTS` 之后它落 `WORKER_LOST`,而它一次都没真正跑过。
    """
    path = APP / "workbench" / "batch_service.py"
    claim = _fn(path, "claim_items")
    assert any(a.arg == "exclude_ids" for a in claim.args.kwonlyargs), (
        "claim_items 不接收 exclude_ids —— 过滤只能留在 UPDATE 之后(A-23)"
    )
    body = ast.unparse(claim)
    assert "notin_" in body, "exclude_ids 没有真的进 WHERE"

    runner = ast.unparse(_fn(path, "run_batch"))
    assert "exclude_ids=seen" in runner, "调用方没有把 seen 传下去"
    assert "if row.id not in seen" not in runner, (
        "又在 Python 里过滤已领取的行了(A-23)"
    )


# ==========================================================
# R1-15 / R1-39:两处口径含糊
# ==========================================================


def test_an_unknown_status_is_counted_not_dropped():
    """R1-15:认不出的状态要进 `total`,并单独有个数。

    改之前它既不进任何桶也不进 total(total 是各桶之和),于是条目页列出
    50 行、汇总说 48 条 —— 一个没人解释得了的差额。
    """
    from app.workbench import batch as rules

    rows = [
        {"status": "SUCCEEDED"},
        {"status": "SUCCEEDED"},
        {"status": "SOMETHING_NEW"},
    ]
    counts = rules.count_rows(rows)
    assert counts.total == 3, f"未知状态被丢掉了,total={counts.total}"
    assert counts.unknown_status == 1
    assert counts.succeeded == 2
    # 干净数据上恒为 0 —— 这个字段非 0 本身就是信号
    assert rules.count_rows([{"status": "FAILED"}]).unknown_status == 0


def test_the_attempted_count_no_longer_hides_two_meanings_in_one_or():
    """R1-39:`int(x or 0) or (a + b)` 把"None 当 0"和"0 当没测到"压在一起。

    行为一字不变,只是让读的人分得出 `image_count == 0` 走的是哪一条。
    """
    src = ast.unparse(_fn(APP / "workbench" / "batch_service.py", "_exec_extract"))
    assert "images if images > 0 else" in src, "又写回一串 or 了(R1-39)"
    assert "billable_calls=attempted" in src, "逐图计费口径不能跟着动"


# ==========================================================
# C-11:上传不再一次性读满上限
# ==========================================================


def test_the_upload_stops_reading_as_soon_as_it_is_over_the_limit():
    """C-11(部分):分块读 + 先看 Content-Length。

    **这不是完整的 C-11。** `upload_asset()` 收的是 `bytes`,整份内容终究要在
    内存里过一遍;真正的流式要连同存储层的分段上传一起改。这条守的是
    "超限的那些不再先被完整收进来"。
    """
    fn = _fn(APP / "api" / "deps.py", "read_within_limit")
    src = ast.unparse(fn)
    assert "content-length" in src, "没有先看 Content-Length,超限也要先读一遍"
    assert "while True" in src and "_UPLOAD_CHUNK_BYTES" in src, "不是分块读"
    assert "chunks.clear()" in src, "超限时没有立刻扔掉已经收进来的部分"

    for module in ("products.py", "model_templates.py"):
        caller = _read(APP / "api" / module)
        assert "read_within_limit(file, limit)" in caller, (
            f"{module} 没走统一的分块读 —— 两处各写一份迟早分叉"
        )
        assert "await file.read(limit + 1)" not in caller, (
            f"{module} 又一次性读满上限了(C-11)"
        )


# ==========================================================
# C-14:variant_key 的静态规则(评审说"缺的正是这个")
# ==========================================================


def test_a_variant_key_is_never_shown_to_a_human_directly():
    """C-14:`variant_key` 长得像颜色名,但它**不是**显示值。

    key 是分配那一刻的颜色名;改过名之后它是一个历史值。直接打到界面上,
    运营会看到自己刚改掉的旧名字,然后再改一次。

    评审对这条的结论是"属实且防御到位,**缺的是静态规则/契约测试**"——
    防御指 `label_of()` / `nameOfVariant()`,而没有任何东西拦住下一个人
    绕开它们。这条就是那个拦。
    """
    offenders: list[str] = []
    for path in FRONTEND.rglob("*.tsx"):
        source = _code_only(_read(path))
        for lineno, line in enumerate(source.splitlines(), 1):
            # 把 key 直接塞进 JSX 文本或标签内容的写法
            if re.search(r"\{\s*(?:coverage\.)?required\[[^\]]+\]\s*\}", line):
                offenders.append(f"{path.name}:{lineno}")
            if re.search(r">\s*\{\s*\w*[Kk]ey\s*\}\s*<", line) and "variant" in line.lower():
                offenders.append(f"{path.name}:{lineno}")
    assert not offenders, (
        "把 variant key 直接显示给人看了,应当走 `nameOfVariant()` -> " + "; ".join(offenders)
    )


def test_the_display_name_helper_prefers_the_current_colour():
    """两端各有一个"显示名"函数,口径必须同向:**当前颜色优先,key 兜底**。"""
    from app.listings import variant_key

    assert variant_key.label_of(key="Red", color="Crimson") == "Crimson"
    assert variant_key.label_of(key="Red", color="") == "Red"
    assert variant_key.label_of(key="Red", color=None) == "Red"

    tab = _code_only(_read(FRONTEND / "components" / "workbench" / "ImageSetTab.tsx"))
    assert "coverage.labels[key] ?? key" in tab, (
        "前端的 nameOfVariant 口径变了 —— 两端必须同向,否则同一个变体两个名字"
    )


def test_the_legacy_namespaced_owner_id_still_parses():
    """C-14 的另一半。**铸造在阶段 1 退役了,解析没有。**

    库里 0046 之前写下的行还是 `<len>:<spu>/<variant_id>`,而
    `orphaned_variant_owners()` 与 0046 的降级都要切它。切错的表现是
    一批属性被认成属于另一个 SPU(那正是 C-13)。

    ## 期望值从"铸造函数的返回值"换成了**字面量**

    原来这条是 round-trip:拼一个再拆回来。那是一个弱断言 —— 铸造和解析
    一起漂的话它照样绿,而库里真正躺着的那串字节谁都读不了。铸造退役之后
    这个弱点必须补上,所以下面钉的是真实字节。
    """
    from app.attributes import validation

    for raw, expected in [
        ("6:SW-001/black", ("SW-001", "black")),
        ("5:AB/CD/Red", ("AB/CD", "Red")),
        ("3:A:B/Red~2", ("A:B", "Red~2")),
        ("4:SW-1/Red/Blue", ("SW-1", "Red/Blue")),
    ]:
        assert validation.split_variant_owner_id(raw) == expected, (
            f"{raw!r} 拆不回 {expected!r} —— 存量行会被认成属于另一个 SPU"
        )
    # 裸 id(A44 之前写下的)必须**明确**拆不出来 —— 那个 None 是巡检的信号
    assert validation.split_variant_owner_id("black") is None
    # 阶段 1 之后写下的是裸 UUID,同样拆不出来,而那是**对的**:
    # 它不需要被拆,`fingerprint_scope` 先按 UUID 认它
    assert validation.split_variant_owner_id(
        "0f9f1c2e-1111-4a00-8000-000000000001"
    ) is None


# ==========================================================
# C-17:孤儿导出对象的回收
# ==========================================================


def test_orphan_exports_have_a_reclaimer_with_three_guards():
    """C-17:`regenerate` 留下的旧对象终于有人收了。

    不推翻 `regenerate_batch_export()` 那个"留着当对照物"的取舍,只给它
    加一个期限。三道闸少一道都可能删掉在用的文件 —— 尤其第二道:
    `storage.save()` 按内容哈希落盘,两次生成字节相同就是同一个对象,
    此时"旧路径"和"新路径"是同一个字符串。
    """
    path = APP / "services" / "cleanup_service.py"
    src = ast.unparse(_fn(path, "purge_superseded_exports"))

    assert "older_than_days" in src, "没有年龄闸"
    assert "BatchJob.file_path.in_" in src, (
        "没有「还在被引用就不删」这一闸 —— 同字节的重新生成会删掉在用的文件"
    )
    assert "if not apply" in src, "不是默认干跑"
    assert "regenerate_export_file" in _read(path), "线索没有接到审计上"

    # 删失败不该让整轮停下:A-14 之后 delete() 会把 403/5xx 抛出来
    assert "except Exception" in src and "continue" in src, (
        "一个对象删不掉就会中断整轮回收"
    )


# ==========================================================
# A-10 / C-04:口令这两条
# ==========================================================


# ============================================================================
# 这里原来有两条守卫,随浏览器登录一起退役(PRD §41.6 / §41.7)
#
#     test_the_token_presence_is_reactive
#         钉 `TOKEN_CHANGED_EVENT` 广播 + `useIdentity` 订阅。被守的那条链
#         (localStorage 双口令 + 自定义事件)本轮整个删掉了,它没有对象了。
#
#     test_the_admin_fallback_is_visible_to_the_operator
#         钉"没有 operator 口令时回落带 admin 口令,并且界面要说出来"。
#
# **不要照着它们的失败信息把代码改回去。** 第二条的失败信息原文是:
#
#     "回落被整个删掉了 —— 只配 admin 的部署会整站 401,那不是修复"
#
# 那句话在口令时代是对的。今天它是错的:浏览器不再持有任何 Token,
# "整站 401"的正确解法是**登录**,不是往 localStorage 里塞一把能改 API Key
# 的口令。两条都用 `.index()` 切窗口,删掉被测代码之后它们是**抛 ValueError**
# 而不是断言失败 —— 排查成本比普通红灯高得多,这也是必须整条删而不是留着的原因。
#
# 它们守的两件事今天由别处接住:
#
#     身份变化要让界面跟上   `useIdentity` 的 `onSessionExpiredChange` 订阅
#                            (401 -> resetQueries -> 重新探身份),
#                            由 test_a46_phase2_browser_login_seam.py 钉着
#     只配一种凭据也能用     后端 `resolve_identity` 的三级回落,
#                            由 tests/test_auth_session.py 的 Legacy 那几条钉着
# ============================================================================


def test_the_dev_install_matches_the_image_build():
    """R1-36:两边都用 `npm ci`。

    `npm install` 会按 semver 悄悄升次版本并改写 lockfile,于是"本地好的、
    CI 红的"这类问题没有任何线索。
    """
    makefile = _no_hash_comments(_read(PROJECT / "Makefile"))
    compose = _no_hash_comments(_read(PROJECT / "docker-compose.yml"))
    dockerfile = _read(PROJECT / "frontend" / "Dockerfile")

    for name, text in (("Makefile", makefile), ("docker-compose.yml", compose)):
        assert "npm ci" in text, f"{name} 没有用 npm ci"
        assert "npm install" not in text, (
            f"{name} 里还有 npm install —— 装出来的树可能和镜像不一样(R1-36)"
        )
    assert "npm ci" in dockerfile, "镜像构建的口径变了,这条断言的前提没了"
