"""A45-batch14-20:识别 run 的身份落库与 §9.2 幂等接线(PRD §4.6 / §5.3 / §9.2)。

## 这一批守的是什么

`run_state.py`(14-12)与 `scope_fingerprint.py`(14-9)两个判定模块早就
写完并穷举验过了,**却一直接不上线** —— 每一批都写着同一句话:没有列可写。
迁移 0040 把五列落下去,本批接线。

判定本身**一个字没动**,所以这里不重验判定。这一批的全部风险在接线,
而接线的坏法有它自己的形状:

    算出来了但没用      键算了不写进列、终态算了不写回 `status`
    喂错了入参          ORM 行不经 `views()` 直接进指纹;取响应里的模型版本
    凑一个假的          没有 spu_id 时拿 product_id 顶上
    查重与索引各说各话  查询的状态集合手写一份,和索引谓词漂移

四种全部**不报错**。前三种的后果是同一件事付两次钱,第四种的后果是
一个和用户动作毫无关系的 409。所以本批守卫几乎全是 AST 结构守卫 ——
它们盯的不是「算得对不对」(那由 14-9 / 14-12 的穷举负责),
而是「算出来的东西有没有真的落到该落的地方」。

## 边界:什么验不到

真库上才成立的三件事本批一条都验不到 —— 部分唯一索引到底建没建起来、
`IntegrityError` 那一路捞不捞得到赢家、`server_default` 对存量行生不生效。
它们写在 `tests/test_a45_batch14_20_run_identity_db.py`,**在这台机器上
一次都没跑过**,理由与前几批相同(没有 PostgreSQL)。

不把它们写成纯层守卫来凑数:一条只验了「代码里出现过 IntegrityError」
的守卫会让 19/19 这个数字变成一句谎话。
"""
from __future__ import annotations

import ast

from app.attributes import run_state as rs
from app.core.enums import ExtractionRunStatus
from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"

S = ExtractionRunStatus


def _src(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


def _tree(rel: str) -> ast.Module:
    return ast.parse(_src(rel))


def _func(rel: str, name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(rel)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{rel} 里找不到函数 {name}")


def _code(fn: ast.FunctionDef) -> str:
    """函数体的源码,**剥掉 docstring**。

    不剥的下场本批当场撞到了:`VisionAttributeExtractor.declared_versions`
    的文档里写着「不取 `result.model_name`」,而一条查 `result.` 的断言
    把那句**解释**当成了代码,守卫红在一个完全正确的实现上。

    这是 §3.26 第四节那句话的第七次成立 —— 按字符串在源码里找东西,
    找到的从来不保证是你以为的那个位置。方向反过来同样成立:
    注释里恰好出现了被禁的写法时,守卫会**变松**而不是变红。
    """
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(n) for n in body)


# ================================================================ 一、键要落到列里


def test_the_key_is_computed_and_actually_written_to_the_row():
    """键算了必须**写进那一列**。

    「算出来了但没用」是接线最常见、也最安静的坏法:`_run_identity` 返回
    一个正确的键,建行时不带上它,于是 `idempotency_key` 全是 NULL ——
    NULL 在唯一索引里互不相等,双击照样建两行、付两次钱,
    而每一条纯层判定测试都是绿的。
    """
    fn = _func("attributes/service.py", "run_extraction")
    body = _code(fn)
    assert "idempotency_key=identity.key" in body, (
        "建行时没有带上算好的键 —— 双击会照样付两次钱,而没有任何测试会红"
    )
    for column in ("spu_id=identity.spu_id", "requested_scope=identity.scope_token",
                   "input_fingerprint=identity.input_fingerprint"):
        assert column in body, f"身份四件套少写了一件:{column}"


def test_the_reuse_check_happens_before_any_paid_call():
    """查重必须排在逐图循环**之前**。

    排在后面等于先把钱花完再问「这次是不是重复请求」—— 而键要挡的正是
    双击和网络重发,两件事都发生在付钱之前。

    这条用**位置**断言而不是看调用有没有出现:调用出现了但排在循环后面,
    是一个所有其他守卫都看不见的形状。
    """
    fn = _func("attributes/service.py", "run_extraction")
    reuse_line = min(
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "reuse_verdict"
    )
    loop_line = min(
        n.lineno
        for n in ast.walk(fn)
        if isinstance(n, ast.For)
        and isinstance(n.iter, ast.Name)
        and n.iter.id == "assets"
    )
    assert reuse_line < loop_line, (
        f"查重在第 {reuse_line} 行、逐图循环在第 {loop_line} 行 —— "
        "先花钱再问是不是重复请求"
    )


def test_the_reuse_verdict_is_asked_not_re_implemented():
    """复用与否问 `reuse_verdict`,不在服务层重写一遍状态判断。

    重写一份的表现极隐蔽:两份判定对 PARTIAL_SUCCESS 的处理最容易分歧 ——
    判定说新建(失败的那几张要重试),而顺手写的 `in ("QUEUED","RUNNING",
    "COMPLETED","PARTIAL_SUCCESS")` 说复用,于是「按颜色重试」永远跑不起来。
    """
    body = _code(_func("attributes/service.py", "run_extraction"))
    assert "run_state.reuse_verdict(" in body, "服务层没有问 `reuse_verdict`"
    assert "ReuseVerdict.NEW_RUN" in body, "复用判据不是拿判定的常量比的"
    for banned in ('== "COMPLETED"', "== 'COMPLETED'", '== "RUNNING"', "== 'RUNNING'"):
        assert banned not in body, f"服务层在自己判状态:{banned}"
    # **判定的结果必须真的用来 return。** 算了却往下跑是「算出来了但没用」
    # 换到分支上的形状:日志里会写「复用了一次 run」,而下面照样打了一轮
    # 付费调用并建出第二行 —— 两条记录、两笔钱、一句自相矛盾的日志
    fn = _func("attributes/service.py", "run_extraction")
    reuse_if = next(
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.If) and "ReuseVerdict.NEW_RUN" in ast.unparse(n.test)
    )
    assert any(
        isinstance(n, ast.Return) and n.value is not None for n in reuse_if.body
    ), "复用分支里没有 return —— 判定算了却照样往下跑,钱照付"


# ================================================================ 二、查重与索引同源


def test_the_lookup_and_the_index_read_the_same_status_set():
    """查重的状态集合来自 `KEY_OCCUPYING_STATUSES`,不是手写的一串字面量。

    手写一份的漂移方式很具体:库拦下了插入(键被占),而这个查询说没人占,
    于是 `IntegrityError` 那一路找不到赢家 —— 一次正常的并发双击变成 500。
    """
    fn = _func("attributes/service.py", "_run_occupying_key")
    body = _code(fn)
    assert "run_state.KEY_OCCUPYING_STATUSES" in body, (
        "查重的状态集合是手写的 —— 它会和索引谓词漂移"
    )
    for banned in ("'QUEUED'", '"QUEUED"', "'RUNNING'", '"RUNNING"'):
        assert banned not in body, f"查重里出现了字面量状态:{banned}"


def test_the_occupying_set_still_excludes_the_statuses_that_must_be_retryable():
    """占键的那几档里**不许**有 FAILED / PARTIAL_SUCCESS / CANCELLED。

    这三档占键的后果是同一句话:同样的输入再也建不出第二个 run ——
    而那正是重试的定义。运营看到的是一个再也识别不了的商品,
    唯一的解法是去改点什么来骗过约束。

    这条与 14-12 里那条 `KEY_OCCUPYING_STATUSES` 的派生守卫不重复:
    那一条钉的是「名单由判定派生」,这一条钉的是**派生出来的结果本身**
    仍然把三档可重试的排除在外。判定改坏时前者可能仍然绿。
    """
    for retryable in (S.FAILED, S.PARTIAL_SUCCESS, S.CANCELLED):
        assert retryable not in rs.KEY_OCCUPYING_STATUSES, (
            f"{retryable.value} 占着幂等键 —— 这个输入再也建不出第二个 run"
        )
    for occupying in (S.QUEUED, S.RUNNING, S.COMPLETED):
        assert occupying in rs.KEY_OCCUPYING_STATUSES, (
            f"{occupying.value} 不占键了 —— 双击会建出第二个 run"
        )


def test_the_race_is_arbitrated_by_the_database_not_by_the_lookup():
    """先查后插挡不住并发,所以必须有保存点 + `IntegrityError` 兜底。

    没有它的话,两个同时到达的请求都查到「没人占键」,然后一个插入成功、
    另一个撞约束抛 500 —— 而用户看到的是「识别失败」,顺手的动作是再点一次。

    保存点不是可有可无的写法:约束冲突会让整个事务进入 aborted 状态,
    不回滚就没法继续往下跑这次识别。
    """
    fn = _func("attributes/service.py", "run_extraction")
    body = _code(fn)
    assert "session.begin_nested()" in body, "没有保存点 —— 约束冲突会毁掉整个事务"
    assert "IntegrityError" in body, "没有接住约束冲突 —— 并发双击会变成 500"
    assert "savepoint.rollback()" in body, "撞了约束却没回滚保存点"
    # **不许吞**:捞不到赢家时要把异常放出去。把别的约束冲突翻译成
    # 「复用了一次识别」会让一个真实的写入缺陷表现成一次安静的成功
    handler = next(n for n in ast.walk(fn) if isinstance(n, ast.ExceptHandler))
    guarded = [
        n
        for n in ast.walk(handler)
        if isinstance(n, ast.If)
        and "winner" in ast.unparse(n.test)
        and any(isinstance(x, ast.Raise) for x in n.body)
    ]
    assert guarded, (
        "`IntegrityError` 被无条件吞掉了(或者 raise 被一个恒假的条件挡住了)"
        " —— 别的约束冲突会伪装成一次成功的复用"
    )
    # 条件本身不许被短路成恒假:`if False:` 会让 raise 变成不可达代码,
    # 而 AST 里它还在 —— 只看「有没有 raise」的守卫抓不到这一种
    assert "winner is None" in ast.unparse(guarded[0].test), (
        f"兜底条件变成了 {ast.unparse(guarded[0].test)!r} —— raise 到不了"
    )


# ================================================================ 三、不许凑一个假的


def test_a_product_without_an_spu_gets_no_key_instead_of_a_fake_one():
    """没有 `spu_id` 就**不建键**,不拿 `product_id` 顶上。

    顶上去的后果是两个不同 SPU 的同型请求算出同一个键 —— 第二个 SPU 的
    识别会直接复用第一个的结果,而那不报错:接口 200,属性也真的填上了,
    填的是**别的商品**的属性。

    这是欠账守卫当初点名的那条捷径(14-12 第八节),现在换成正向守卫盯着。
    """
    fn = _func("attributes/service.py", "_run_identity")
    body = _code(fn)
    assert "PRODUCT_HAS_NO_SPU" in body, "没有 spu_id 时没有走「不建键」这一路"
    key_calls = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "idempotency_key"
    ]
    assert len(key_calls) == 1, "键不是只算一处"
    passed = {kw.arg for kw in key_calls[0].keywords}
    assert passed == {
        "spu_id", "scope", "fingerprints", "model_version", "prompt_version",
        "requested_fields",
    }, f"递给键的入参变了:{sorted(passed)}"
    spu_arg = next(kw.value for kw in key_calls[0].keywords if kw.arg == "spu_id")
    assert not (
        isinstance(spu_arg, ast.Attribute) and spu_arg.attr == "id"
    ), "键的第一维递的是 `product.id` —— 两个不同 SPU 会算出同一个键"


def test_the_versions_in_the_key_come_from_the_extractor_not_the_response():
    """模型/提示词版本取**调用前**报出来的那两个,不取响应里的。

    取响应里的实现编译得过、测试也绿,只是键要付过钱才算得出来 ——
    而键要挡的正是付钱前那两下。这条是 §9.2 与 `run_state` 模块文档
    第三节反复点名的那个洞。
    """
    body = _code(_func("attributes/service.py", "_run_identity"))
    assert "declared_versions" in body, "版本不是从抽取器的调用前钩子取的"
    for banned in ("result.model_name", "result.prompt_version"):
        assert banned not in body, (
            f"键里用了响应字段 {banned} —— 那要付过钱才知道"
        )


def test_an_extractor_without_the_hook_gets_no_key_instead_of_a_default():
    """抽取器报不出版本时**不建键**,不拿 `name` 或空串凑一个。

    凑出来的键不报错,只是把两个不同模型的同型请求判成同一次 ——
    换了模型还在用旧结果,而属性值看起来完全正常。
    """
    body = _code(_func("attributes/service.py", "_run_identity"))
    assert "EXTRACTOR_DECLARES_NO_VERSIONS" in body, "没有走「报不出版本就不建键」这一路"
    assert "getattr(extractor, 'declared_versions', None)" in body, (
        "钩子不是用 getattr 取的 —— 没实现它的抽取器会直接 AttributeError"
    )


def test_the_two_shipped_extractors_answer_the_hook_before_any_call():
    """Mock 与 Vision 都实现 `declared_versions()`,而且都不读响应。

    只在 vision 上接的话,`make smoke` 与 CI 走的都是 Mock ——
    **这条路径在门禁里一次都不会被走到**。
    """
    for module, cls in (("extractors/mock.py", "MockAttributeExtractor"),
                        ("extractors/vision.py", "VisionAttributeExtractor")):
        klass = next(
            n for n in ast.walk(_tree(module))
            if isinstance(n, ast.ClassDef) and n.name == cls
        )
        method = next(
            (n for n in klass.body
             if isinstance(n, ast.FunctionDef) and n.name == "declared_versions"),
            None,
        )
        assert method is not None, f"{cls} 没有实现 `declared_versions()`"
        body = _code(method)
        assert "self.extract" not in body and "result." not in body, (
            f"{cls}.declared_versions 读了响应 —— 那要付过钱才知道"
        )
    # **报出来的值不许是空的。** 空版本会让 `idempotency_key()` 抛 ValueError,
    # 而服务层把 ValueError 翻译成「这次没有键」—— 于是幂等**静静失效**,
    # 不报错、不告警,只是每次双击都付两次钱。
    #
    # Mock 在这里被真的实例化并调用:它零外部依赖,而 `make smoke` 与 CI
    # 走的都是它 —— 只读源码的话,一个返回空串的实现会一路绿到生产
    from app.extractors.mock import MockAttributeExtractor

    model_version, prompt_version = MockAttributeExtractor().declared_versions()
    assert model_version and prompt_version, (
        f"Mock 报出来的版本有空值:{(model_version, prompt_version)!r} —— 幂等会静静失效"
    )
    # Vision 不实例化(要配置、要网络),改用源码断言它取的是配置而不是常量
    vision = _code(
        next(
            n for n in ast.walk(_tree("extractors/vision.py"))
            if isinstance(n, ast.FunctionDef) and n.name == "declared_versions"
        )
    )
    assert "self.config.model" in vision, "Vision 的模型版本不是从配置取的"


def test_a_partial_media_selection_does_not_pretend_to_be_a_whole_scope():
    """只识别指定素材时**不建键、也不写作用域**。

    `canonical_scope` 只有共享 / 指定颜色 / 全部三种形状,「任意素材子集」
    不是其中之一。硬塞成 ALL 的话,`requested_scope` 这一列会说一句不真的话
    —— 而它是键的一部分,别的调用点照那句话复算会得到另一个键。
    """
    fn = _func("attributes/service.py", "_run_identity")
    body = _code(fn)
    assert "PARTIAL_MEDIA_SELECTION" in body, "增量识别没有走「不建键」这一路"
    call = next(
        n for n in ast.walk(_func("attributes/service.py", "run_extraction"))
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Name)
        and n.func.id == "_run_identity"
    )
    whole = next(kw.value for kw in call.keywords if kw.arg == "whole_product")
    assert "only_media_ids" in ast.unparse(whole), (
        "「是不是整商品识别」的判据不是 `only_media_ids`"
    )


def test_every_no_key_path_says_why():
    """建不出键的每一条路都要留下**原因**。

    不说的话,「双击产生了两个 run」这件事在排查时只剩一个猜:
    键算错了,还是根本没算?两者的修法完全不同 —— 前者去看编码,
    后者去看这个商品有没有 `spu_id`。
    """
    fn = _func("attributes/service.py", "_run_identity")
    returns = [n for n in ast.walk(fn) if isinstance(n, ast.Return)]
    assert len(returns) == 5, f"`_run_identity` 有 {len(returns)} 条 return,分支数变了"
    no_key = [ast.unparse(n) for n in returns if "None, None)" not in ast.unparse(n)]
    for rendered in (ast.unparse(n) for n in returns):
        # 每一条 return 要么带键、要么带原因,不许两个都空
        has_key = "key" in rendered or rendered.rstrip(")").rsplit(",", 2)[-2].strip() != "None"
        assert has_key or "None, None)" not in rendered, (
            f"有一条 return 既没有键也没有原因:{rendered}"
        )
    assert len(no_key) >= 4, "建不出键的分支少了 —— 有一条在静静地返回空键"


# ================================================================ 四、终态落列


def test_the_row_starts_queued_and_both_active_states_occupy_the_key():
    """异步入口建行即 QUEUED；QUEUED 与 RUNNING 都必须占键。

    建成别的档(比如直接建成 COMPLETED,或者留空让 server_default 兜)的
    后果分两种,都不报错:

        建成 FAILED/留空   不占键 —— 跑的过程中第二个请求查不到占键的行,
                           于是又打一轮付费调用
        建成 COMPLETED     占键了,但那是一句假话 —— 这次还没跑完,
                           而 `reuse_verdict(COMPLETED)` 是「直接复用结果」,
                           第二个请求会拿到一份还没写完的证据
    """
    body = _code(_func("attributes/service.py", "run_extraction"))
    assert "ExtractionRunStatus.QUEUED.value if prepare_only" in body, (
        "排队入口没有把新 run 建成 QUEUED"
    )
    assert S.QUEUED in rs.KEY_OCCUPYING_STATUSES, "QUEUED 不占键了"
    assert S.RUNNING in rs.KEY_OCCUPYING_STATUSES, "RUNNING 不占键了"
    assert rs.reuse_verdict(S.QUEUED) == rs.ReuseVerdict.RETURN_EXISTING
    assert rs.reuse_verdict(S.RUNNING) == rs.ReuseVerdict.RETURN_EXISTING, (
        "撞上正在跑的那一行时不再返回原 run"
    )


def test_the_error_code_still_keys_off_the_status_column():
    """全部失败补 `error_code` 的判据仍然是那个单点判定的结果。

    这条在 14-12 就有,本批把它重钉一次:判定的调用点搬了家(从模型属性
    挪到写入方),而搬家最容易顺手带坏的就是它下游这一行 ——
    改成按 `succeeded == 0` 判的话,今天结论相同,cancel 落地那天就不同了。
    """
    body = _code(_func("attributes/service.py", "run_extraction"))
    assert "EXTRACTION_ALL_FAILED" in body, "跑完之后没有补 error_code"
    assert "row.status == ExtractionRunStatus.FAILED.value" in body, (
        "补 error_code 的判据不是那一列 —— 它在自己重新数计数"
    )
