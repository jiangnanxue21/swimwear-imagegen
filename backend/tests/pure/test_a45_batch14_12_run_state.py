"""A45-batch14-12:识别 run 的终态、取消与幂等键(PRD §4.6 / §9.2)。

## 这一批守卫分两半,因为病灶分两半

**已接线那一半**守的是一个正在漏的东西:「这次识别算成功、部分成功还是失败」
今天由**前端**回答(`AttributeTab.tsx` 拿两个计数判三档),而后端从来没有
派生过这个值 —— 全部失败的 run 与全部成功的 run 在库里长得一模一样:
`error_code` 是 NULL、接口返回 200。

前端那三档还判漏了一种:循环在跑完之前停下(worker 被回收、上限拦截、
将来的 cancel)时 `failed_count` 是 0,那串三元运算落在「识别完成」上 ——
**一次跑了一半的识别显示成功**,而缺的那几张图不会有任何人回来补。
`test_a_run_that_stopped_short_is_never_complete` 就是这一条。

**未接线那一半**(§9.2 幂等)今天一条真实数据都影响不了:五个列还不存在。
它照样在这里验完,理由和 batch14-9 / 14-10 相同 —— 真库用例池里已经压着
60 条没跑过的了,再往里加一条等于把它推给下一台机器。而这一半的失效方式
尤其安静:每一条的后果都是**同一件事付两次钱**,而账单不会告诉你
哪一笔是重复的。

## 三处穷举

    终态判定的整个输入空间        image_count × succeeded × failed × cancel
    每一个 `ExtractionRunStatus`  算不算数 / 复用判定 / 占不占键,都被显式决定过
    转移表                        每一档的出边都在枚举里,终态一条出边都没有

第一条同时钉住**空间规模与结果分布**(batch14-8 的 X1 教训):判定塌成
恒返回一个值时,「没有半截数据能被判成 COMPLETED」会是一句正确的废话。
"""
from __future__ import annotations

import ast
import hashlib
import itertools

from app.attributes import run_state as rs
from app.attributes import scope_fingerprint as fp
from app.core.enums import ExtractionRunStatus
from tests.pure._helpers import BACKEND_ROOT, expect_raises, window

APP = BACKEND_ROOT / "app"
FRONTEND = BACKEND_ROOT.parent / "frontend" / "src"

S = ExtractionRunStatus

#: 终态判定的**宿主**:哪个文件的哪个函数负责把三个计数递给
#: `terminal_status_for`。写成常量而不是写死在断言里,是「点名做法」的
#: 第六次一般化(DECISIONS §3.33)。
#:
#: 前五次的形状一模一样:守卫把「这段代码在哪个函数里」当成了不变式,
#: 而搬家那天它变红,口径其实更严。这一条自己就栽过 —— 14-11 到 14-19
#: 期间宿主是 `models/attribute.py` 的派生属性 `status`,A45-batch14-20
#: 那一列落库之后判定必须挪到写入方,于是原来那条守卫当场变红。
#:
#: **再搬一次只改这两行,断言本身不必动。**
VERDICT_HOST = ("attributes/service.py", "run_extraction")


def _src(rel: str) -> str:
    return (APP / rel).read_text(encoding="utf-8")


def _tree(rel: str) -> ast.Module:
    return ast.parse(_src(rel))


def _wired_entry(module: str) -> tuple[str, ...]:
    """从 `verify_delivery.WIRED_MODULES` 里取出某个模块登记的函数名。

    **用 AST 取,不用字符串切。**第一版是
    `block[block.index(module):][:...index("),")]` —— 而登记项的注释里
    有一个 `(归属外键不存在),`,`),` 当场提前命中,后面的名字被切没了。

    后果不是这条守卫红,是它**变松**:正向断言碰巧还在截断范围内,
    而「这几个不许登记」的反向断言被截断喂成了平凡真。
    §3.26 第四节那句话在这里第六次成立 —— 按字符串在源码里找东西,
    找到的从来不保证是你以为的那个位置。
    """
    source = (BACKEND_ROOT / "tools" / "verify_delivery.py").read_text(encoding="utf-8")
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
            continue
        if node.target.id != "WIRED_MODULES" or node.value is None:
            continue
        for key, value in zip(node.value.keys, node.value.values, strict=False):
            if isinstance(key, ast.Constant) and key.value == module:
                return tuple(
                    e.value for e in value.elts if isinstance(e, ast.Constant)
                )
    raise AssertionError(f"WIRED_MODULES 里找不到 {module}")


def _func(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


# ================================================================ 一、终态派生


def test_a_run_that_stopped_short_is_never_complete():
    """**本批的病灶复现。**

    循环没跑完(`succeeded + failed < image_count`)而一张都没失败时,
    前端那串三元运算说「识别完成」。它不是。五张图跑了三张,
    剩下两张一条证据都没有,而没有任何人会回来补 —— 因为界面说完成了。
    """
    assert rs.terminal_status_for(image_count=5, succeeded=3, failed=0) is S.PARTIAL_SUCCESS
    assert rs.terminal_status_for(image_count=5, succeeded=0, failed=0) is S.FAILED
    assert rs.terminal_status_for(image_count=5, succeeded=1, failed=1) is S.PARTIAL_SUCCESS


def test_a_run_with_no_images_is_not_a_success():
    """「零张图全部成功」逻辑上是真的,业务上是一个**没有输入的 run**。

    往 COMPLETED 倒的话,这种 run 会带着「成功」的标签让下游认为
    事实已经识别过了。今天 `run_extraction` 在这之前就抛了,
    所以这一档是给异步路径留的 —— 那时素材可能在排队期间被隔离光。
    """
    assert rs.terminal_status_for(image_count=0, succeeded=0, failed=0) is S.FAILED
    assert (
        rs.terminal_status_for(
            image_count=0, succeeded=0, failed=0, cancel_requested=True
        )
        is S.FAILED
    )


def test_a_run_where_every_image_failed_is_failed():
    """一条证据都没有的 run 不是「部分成功」。

    判成 PARTIAL_SUCCESS 的后果是它进 `AUTHORITATIVE_STATUSES`,
    于是一次彻底失败的识别会去参与事实合并 —— 合并零条证据。
    """
    assert rs.terminal_status_for(image_count=3, succeeded=0, failed=3) is S.FAILED
    assert rs.terminal_status_for(image_count=1, succeeded=0, failed=1) is S.FAILED


def test_a_run_with_any_failure_is_partial_not_complete():
    """有一张失败就不是 COMPLETED。

    差别不在措辞:COMPLETED 的下一步是「去确认事实」,PARTIAL_SUCCESS 的
    下一步是「把失败那几张补上」。说成完整,那几张永远补不上。
    """
    assert rs.terminal_status_for(image_count=3, succeeded=2, failed=1) is S.PARTIAL_SUCCESS
    assert rs.terminal_status_for(image_count=3, succeeded=3, failed=0) is S.COMPLETED


def test_impossible_counts_fail_closed():
    """计数自相矛盾时倒向 FAILED,不倒向 COMPLETED。

    负数、处理过的比总数还多 —— 这些是 bug 的征兆,不是业务情况。
    而 bug 的征兆最不该做的事,就是带着「成功」的标签把半截证据送进事实。
    """
    assert rs.terminal_status_for(image_count=3, succeeded=5, failed=0) is S.FAILED
    assert rs.terminal_status_for(image_count=3, succeeded=-1, failed=1) is S.FAILED
    assert rs.terminal_status_for(image_count=-1, succeeded=0, failed=0) is S.FAILED
    assert rs.terminal_status_for(image_count=2, succeeded=2, failed=2) is S.FAILED


def test_a_cancel_that_arrived_after_the_last_image_is_not_a_cancel():
    """喊得太晚不算取消。**这一条是钱的问题,不是措辞问题。**

    最后一张跑完之后才收到取消:那批调用一次不少地付过了,而 CANCELLED
    的证据不进合并 —— 判成取消等于**把已经买到手的结果扔掉**,
    换来的只是界面上一个更符合运营预期的词。

    反过来,停在做完之前必须是 CANCELLED 而不是 PARTIAL_SUCCESS:
    两者的下一步动作完全不同。
    """
    # 全部跑完之后才喊停 -> 该是什么就是什么
    assert rs.terminal_status_for(
        image_count=3, succeeded=3, failed=0, cancel_requested=True
    ) is S.COMPLETED
    assert rs.terminal_status_for(
        image_count=3, succeeded=2, failed=1, cancel_requested=True
    ) is S.PARTIAL_SUCCESS
    # 停在做完之前 -> CANCELLED,而不是 PARTIAL_SUCCESS
    assert rs.terminal_status_for(
        image_count=5, succeeded=2, failed=0, cancel_requested=True
    ) is S.CANCELLED
    assert rs.terminal_status_for(
        image_count=5, succeeded=0, failed=0, cancel_requested=True
    ) is S.CANCELLED


def test_the_whole_verdict_space_is_enumerated_and_nothing_collapses():
    """整个输入空间穷举,并且**钉住空间规模与结果分布**。

    后一半是 batch14-8 的 X1 教训:判定塌成恒返回一个值时,
    「没有半截数据会被判成 COMPLETED」是一句正确的废话。四种结果
    每一种都必须真的出现过,COMPLETED 必须只出现在「跑满且零失败」上。
    """
    space = list(itertools.product(range(0, 5), range(0, 5), range(0, 5), (False, True)))
    assert len(space) == 250, "输入空间的形状变了,先确认下面的分布断言还成立"

    seen: dict[ExtractionRunStatus, int] = {}
    for images, ok, bad, cancelled in space:
        verdict = rs.terminal_status_for(
            image_count=images, succeeded=ok, failed=bad, cancel_requested=cancelled
        )
        seen[verdict] = seen.get(verdict, 0) + 1
        if verdict is S.COMPLETED:
            # COMPLETED 的充要条件,写成断言而不是注释
            assert images > 0 and bad == 0 and ok == images, (
                f"({images},{ok},{bad},{cancelled}) 被判成 COMPLETED,但它不是「跑满且零失败」"
            )
        if verdict is S.CANCELLED:
            assert cancelled and ok + bad < images, (
                f"({images},{ok},{bad},{cancelled}) 被判成 CANCELLED,但它没有停在做完之前"
            )

    assert set(seen) == {S.COMPLETED, S.PARTIAL_SUCCESS, S.FAILED, S.CANCELLED}, (
        f"四种结果没有全部出现过,判定可能塌了:{ {k.value: v for k, v in seen.items()} }"
    )
    assert seen[S.COMPLETED] == 8, f"COMPLETED 的格子数变了:{seen[S.COMPLETED]}"


# ================================================================ 二、哪几档算数


def test_every_status_is_explicitly_decided_for_the_merge():
    """每一个取值都被显式决定过「证据算不算数」。

    新增一档时这条会红 —— 那正是要的:新的一档默认算数还是默认不算数,
    是一个必须有人做的决定,不是一个可以顺手继承的默认值。
    """
    decided = {s: rs.run_is_authoritative(s) for s in ExtractionRunStatus}
    assert decided == {
        S.QUEUED: False,
        S.RUNNING: False,
        S.COMPLETED: True,
        S.PARTIAL_SUCCESS: True,
        S.FAILED: False,
        S.CANCELLED: False,
    }, f"算数集合变了:{ {k.value: v for k, v in decided.items()} }"


def test_a_cancelled_run_does_not_feed_the_facts():
    """被人叫停的那次识别不改写事实,**尽管它的证据同样付过钱**。

    区别在于「为什么停」:部分失败是模型答不上来,取消是人喊停,
    而人喊停多半正是因为这一次跑错了(选错了图、选错了字段)。

    注意这条只管合并,不管删除:被取消的 run 已经写下的证据行照样留着,
    那是「这笔钱花在哪了」的唯一记录。
    """
    assert rs.run_is_authoritative(S.CANCELLED) is False
    assert S.CANCELLED not in rs.AUTHORITATIVE_STATUSES


def test_a_partially_successful_run_still_feeds_the_facts():
    """部分成功**算数**。成功那几张的证据是真的。

    排除它的代价很具体:五张图坏了一张,另外四张的钱照付,
    而证据一条都不用。少几张图的表现应该是置信度低、进 CANDIDATE
    而不是 SUGGESTED —— 那是合并层的职责,不是这里的。
    """
    assert rs.run_is_authoritative(S.PARTIAL_SUCCESS) is True


def test_no_status_means_not_authoritative():
    """没有状态 = 证明不了它算数。与 `facts_stale` 的「算不出来就算过期」同一条口径。"""
    assert rs.run_is_authoritative(None) is False


# ================================================================ 三、转移与取消


def test_the_transition_table_covers_every_status_and_terminals_have_no_way_out():
    """转移表是封闭集合:每一档都有一行,终态一条出边都没有。

    终态还留着出边的后果与 `state_machine` 那条一样:后台任务能把一个
    已经跑完、已经合并进事实的 run 重新拉起来。
    """
    assert set(rs.TRANSITIONS) == set(ExtractionRunStatus), "转移表漏了某一档"
    for status, targets in rs.TRANSITIONS.items():
        assert targets <= set(ExtractionRunStatus), f"{status} 的出边里有不存在的取值"
        if status in rs.TERMINAL_STATUSES:
            assert targets == frozenset(), f"终态 {status.value} 还留着出边 {targets}"
        else:
            assert targets, f"非终态 {status.value} 一条出边都没有,它会永远卡住"


def test_partial_success_is_terminal():
    """`PARTIAL_SUCCESS` 是**终态不是中间态**。

    放进非终态的话,「重试失败那几张」会变成在同一行上继续写,
    而那一行的输入快照是第一次跑时的 —— 于是重试之后的证据挂在一个
    过期的输入上,而 §13 要的是「按颜色重试」建一个新 run。
    """
    assert S.PARTIAL_SUCCESS in rs.TERMINAL_STATUSES
    assert rs.is_terminal(S.PARTIAL_SUCCESS) is True
    assert rs.TRANSITIONS[S.PARTIAL_SUCCESS] == frozenset()


def test_a_finished_run_cannot_be_cancelled():
    """跑完了就取消不了。

    取消一个已经合并进事实的 run,只会让一份已经付过钱的结果变成
    「不算数」,而钱不会回来。可取消的恰好是两档还没结束的。
    """
    assert rs.can_cancel(S.QUEUED) is True
    assert rs.can_cancel(S.RUNNING) is True
    for status in (S.COMPLETED, S.PARTIAL_SUCCESS, S.FAILED, S.CANCELLED):
        assert rs.can_cancel(status) is False, f"{status.value} 居然还能取消"


# ================================================================ 四、复用与占键


def test_every_status_is_explicitly_decided_for_reuse():
    """§9.2「同意图」的判定逐档钉死。

    `PARTIAL_SUCCESS` 判新建是本表里唯一一处和「复用已有结果」的直觉相反的:
    复用等于宣布失败的那几张永远不再重试。
    """
    decided = {s: rs.reuse_verdict(s) for s in ExtractionRunStatus}
    assert decided == {
        S.QUEUED: rs.ReuseVerdict.RETURN_EXISTING,
        S.RUNNING: rs.ReuseVerdict.RETURN_EXISTING,
        S.COMPLETED: rs.ReuseVerdict.REUSE_RESULT,
        S.PARTIAL_SUCCESS: rs.ReuseVerdict.NEW_RUN,
        S.FAILED: rs.ReuseVerdict.NEW_RUN,
        S.CANCELLED: rs.ReuseVerdict.NEW_RUN,
    }, f"复用判定变了:{ {k.value: v for k, v in decided.items()} }"
    assert rs.reuse_verdict(None) == rs.ReuseVerdict.NEW_RUN


def test_a_running_run_is_returned_not_duplicated():
    """双击 / 网络重发 / worker 重投都落在这一条上:返回原 run。

    判成新建的后果不是多一行,是**同一批图付两次钱**。
    """
    assert rs.reuse_verdict(S.RUNNING) == rs.ReuseVerdict.RETURN_EXISTING
    assert rs.reuse_verdict(S.QUEUED) == rs.ReuseVerdict.RETURN_EXISTING


def test_the_key_occupying_set_is_derived_from_the_reuse_verdict():
    """占键名单**从判定派生,不是第二张名单**。

    手写的那一版会和判定漂移,而漂移的表现极隐蔽:代码说该新建一个 run,
    库说这个键被占了,于是接口报一个和用户动作毫无关系的 409。
    """
    assert rs.KEY_OCCUPYING_STATUSES == frozenset(
        s for s in ExtractionRunStatus if rs.reuse_verdict(s) != rs.ReuseVerdict.NEW_RUN
    )
    assert {s.value for s in rs.KEY_OCCUPYING_STATUSES} == {
        "QUEUED",
        "RUNNING",
        "COMPLETED",
    }, "占键的档位变了 —— 这是一个要改迁移里那条索引谓词的决定"


def test_the_unique_index_is_partial_and_names_exactly_the_occupying_statuses():
    """§9.2 的唯一约束**必须是部分唯一索引**,谓词由占键名单生成。

    按字面做成全表唯一的后果:一次 FAILED 之后,同样的输入再也建不出
    第二个 run —— 输入没变、模型没变、字段没变,而那正是重试的定义。
    运营看到的是一个再也识别不了的商品,唯一的解法是去改点什么来骗过约束。
    """
    predicate = rs.unique_index_predicate()
    assert predicate.startswith("status IN ("), f"谓词形状不对:{predicate}"
    for status in ExtractionRunStatus:
        quoted = f"'{status.value}'"
        if status in rs.KEY_OCCUPYING_STATUSES:
            assert quoted in predicate, f"{status.value} 占键却不在谓词里"
        else:
            assert quoted not in predicate, f"{status.value} 不占键却进了谓词"
    assert rs.unique_index_predicate("run_status").startswith("run_status IN (")


# ================================================================ 五、作用域


def test_the_two_scope_words_mean_the_same_thing_as_in_the_fingerprint_module():
    """「共享」在两个模块里必须是同一个东西,编码器也是同一个。

    抄一份编码器过来的代价不是重复,是**两个编码器会漂移** ——
    改了其中一个的人不会知道另一个也在,而两边都不报错。
    """
    assert rs.length_prefixed is fp.length_prefixed
    assert rs.SHARED is fp.SHARED is None
    tree = _tree("attributes/run_state.py")
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef):
            assert node.name not in {"length_prefixed", "_field"}, (
                "run_state 里又长出了一个自己的长度前缀编码器 —— 用 scope_fingerprint 那个"
            )


def test_the_variant_ids_in_a_scope_are_sorted_and_deduped():
    """颜色 id 排序去重。**不排序的后果是同一个请求算出两个键。**

    集合迭代序每个进程都不一样(字符串哈希带随机种子),于是唯一约束
    挡不住任何东西:双击照样跑两次、付两次钱。用八个元素,
    乱序碰巧等于有序的概率是 1/8!。这是同一个教训在本仓库的第四次出现
    (batch14-9 的 H3、batch14-10 的 D1、D2)。
    """
    ids = ["v8", "v3", "v6", "v1", "v7", "v2", "v5", "v4"]
    scope = rs.canonical_scope(variant_ids=ids)
    assert scope.variant_ids == tuple(sorted(ids))
    expected = rs.length_prefixed(rs.SCOPE_VARIANTS) + "".join(
        rs.length_prefixed(i) for i in sorted(ids)
    )
    assert scope.token == expected, f"token 不是按排序后的 id 拼的:{scope.token}"
    # 顺序不同、有重复的同一批 id 必须给出同一个作用域
    assert rs.canonical_scope(variant_ids=["v1", "v2", "v1"]) == rs.canonical_scope(
        variant_ids=["v2", "v1"]
    )


def test_all_is_not_the_same_intent_as_listing_every_colour():
    """ALL 与「把当前全部颜色列出来」不是同一个意图,所以不能是同一个键。

    ALL 说的是「现在有哪些颜色就跑哪些」,列表说的是「就这几个」。
    归一成同一个的写法会让「加了新颜色之后再点识别」命中旧 run 并直接复用,
    而新颜色一张证据都没有。
    """
    every = rs.canonical_scope(all_variants=True)
    listed = rs.canonical_scope(variant_ids=["a", "b"])
    shared = rs.canonical_scope()
    assert every.kind == rs.SCOPE_ALL
    assert shared.kind == rs.SCOPE_SHARED
    assert len({every.token, listed.token, shared.token}) == 3, (
        "三种作用域里有两种编码成了同一个 token"
    )


def test_a_scope_cannot_be_all_and_a_list_at_the_same_time():
    """两种意图同时给出来 = 调用方自己也没想清楚。猜一个的代价是猜错时
    键是错的,而键错了不报错、只是多付一次钱。"""
    expect_raises(
        ValueError, rs.canonical_scope, all_variants=True, variant_ids=["a"]
    )


def test_a_colour_scoped_key_does_not_move_when_a_generic_photo_is_added():
    """**这是 AC-21 在幂等键这道门上的样子。**

    颜色作用域的键只带那几个颜色的指纹,不带共享指纹。带上的话,
    补一张通用图会让每一个颜色的键都变,于是每个颜色都要重跑一次识别 ——
    D1(作用域过宽)从后门原样回来。
    """
    scope = rs.canonical_scope(variant_ids=["a"])
    before = rs.idempotency_key(
        spu_id="spu-1",
        scope=scope,
        fingerprints={None: "shared-1", "a": "fa", "b": "fb"},
        model_version="m1",
        prompt_version="p1",
        requested_fields=["neckline"],
    )
    after = rs.idempotency_key(
        spu_id="spu-1",
        scope=scope,
        fingerprints={None: "shared-2", "a": "fa", "b": "fb"},
        model_version="m1",
        prompt_version="p1",
        requested_fields=["neckline"],
    )
    assert before == after, "补一张通用图挪动了颜色作用域的幂等键"

    # 但那个颜色自己的指纹一变,键必须变
    moved = rs.idempotency_key(
        spu_id="spu-1",
        scope=scope,
        fingerprints={None: "shared-1", "a": "fa2", "b": "fb"},
        model_version="m1",
        prompt_version="p1",
        requested_fields=["neckline"],
    )
    assert moved != before, "这个颜色补了图,键却没变 —— 新图一条证据都不会有"


def test_a_forged_variant_id_cannot_borrow_another_colours_fingerprint():
    """作用域成员判定读真实的 id 元组,**不在 token 里找子串**。

    子串写法可以被伪造:一个 id 恰好长成 `x1:a` 的颜色,会让 `1:a`
    (颜色 a 的编码)成为它的子串,于是 a 的指纹被算进一个根本不含 a 的键。

    这是「文件里出现过这串字 ≠ 这件事成立」换到数据编码上的样子 ——
    同一个形状在本仓库已经栽过三次(batch13-3 的 M2 / M11、batch14-2 的 N15)。
    """
    forged = rs.canonical_scope(variant_ids=["x1:a"])
    assert forged.variant_ids == ("x1:a",)
    expect_raises(
        ValueError,
        rs.idempotency_key,
        spu_id="spu-1",
        scope=forged,
        fingerprints={None: "s", "a": "fa"},
        model_version="m1",
        prompt_version="p1",
        requested_fields=["neckline"],
    )


def test_a_scope_without_a_fingerprint_refuses_to_build_a_key():
    """指纹缺了就建不出键,不许少一维凑合。

    少一维的后果是两个输入不同的 run 算出同一个键,而第二个会被当成
    重复请求直接复用 —— 拿 A 色的结果当 B 色的事实。
    """
    expect_raises(
        ValueError,
        rs.idempotency_key,
        spu_id="spu-1",
        scope=rs.canonical_scope(variant_ids=["b"]),
        fingerprints={None: "s", "a": "fa"},
        model_version="m1",
        prompt_version="p1",
        requested_fields=["neckline"],
    )


# ================================================================ 六、幂等键本体


def _expected_key(
    *, spu, scope, pairs, model, prompt, fields
) -> str:
    """守卫这一侧独立把 payload 拼一遍。

    独立重算而不是调用被测函数,是这条守卫的全部意义:它同时钉住
    键的**组成清单**与**编码方式**。少一项、换一种拼法都会对不上。
    """
    lp = fp.length_prefixed
    parts = [lp(rs.KEY_VERSION), lp(spu), scope.token, lp(model), lp(prompt)]
    for name, digest in pairs:
        parts.append(lp(name))
        parts.append(lp(digest))
    for name in sorted(fields):
        parts.append(lp(name))
    return hashlib.sha256("".join(parts).encode("utf-8")).hexdigest()


def test_the_key_is_exactly_this_payload_and_nothing_else():
    """键的组成逐项对着 §9.2 原文钉死,连拼法一起。

    这一条同时守四件事:版本进键、作用域进键、指纹进键、字段**排序后**进键。
    其中「排序」用八个字段验 —— 集合迭代序碰巧等于字典序的概率是 1/8!。
    """
    fields = ["waistband", "neckline", "pattern", "fit", "closure", "liner", "back", "strap"]
    scope = rs.canonical_scope()
    key = rs.idempotency_key(
        spu_id="spu-1",
        scope=scope,
        fingerprints={None: "shared-1", "a": "fa"},
        model_version="m1",
        prompt_version="p1",
        requested_fields=fields,
    )
    assert key == _expected_key(
        spu="spu-1",
        scope=scope,
        pairs=[("", "shared-1")],
        model="m1",
        prompt="p1",
        fields=fields,
    )
    # 传进来的顺序不改变键
    assert key == rs.idempotency_key(
        spu_id="spu-1",
        scope=scope,
        fingerprints={None: "shared-1", "a": "fa"},
        model_version="m1",
        prompt_version="p1",
        requested_fields=list(reversed(fields)),
    )
    # 同一个字段传两遍不改变键(重复不是新意图)
    assert key == rs.idempotency_key(
        spu_id="spu-1",
        scope=scope,
        fingerprints={None: "shared-1", "a": "fa"},
        model_version="m1",
        prompt_version="p1",
        requested_fields=[*fields, "neckline"],
    )


def test_the_field_list_encoding_cannot_be_forged_by_concatenation():
    """字段清单按长度前缀编码,**不是用分隔符拼起来**。

    这一对是真的会碰撞的一对:分隔符写法下 `("a|b",)` 与 `("a","b")`
    拼出来一模一样。「构造了一对不同的输入」不等于「构造了一对碰撞」——
    batch14-9 的 H1 就栽在这一步上,所以这里挑的是真碰撞。
    """
    common = dict(
        spu_id="spu-1",
        scope=rs.canonical_scope(),
        fingerprints={None: "shared-1"},
        model_version="m1",
        prompt_version="p1",
    )
    one = rs.idempotency_key(requested_fields=["a|b"], **common)
    two = rs.idempotency_key(requested_fields=["a", "b"], **common)
    assert one != two, "两个不同的字段清单算出了同一个幂等键"


def test_the_key_version_participates():
    """键版本进键。不进的话,改了算法之后新旧键会被直接比较,
    而比较的结果是「同意图」判定全体错乱:老 run 一个都认不出来,
    于是每个商品的下一次识别都新建一个 run 并重新付费。"""
    scope = rs.canonical_scope()
    args = dict(
        spu_id="spu-1",
        scope=scope,
        fingerprints={None: "shared-1"},
        model_version="m1",
        prompt_version="p1",
        requested_fields=["neckline"],
    )
    assert rs.idempotency_key(**args) == _expected_key(
        spu="spu-1", scope=scope, pairs=[("", "shared-1")],
        model="m1", prompt="p1", fields=["neckline"],
    )
    assert rs.KEY_VERSION, "键版本不能是空的"


def test_a_key_without_a_model_or_prompt_version_is_refused():
    """**这一条是「幂等键必须在第一次付费调用之前算得出来」的落点。**

    §9.2 的键含 model_version。有两个地方能拿到它:配置与模型响应。
    取后者的话,键只有在**付过钱之后**才算得出来 —— 而幂等键要挡的
    正是双击和网络重发,那两件事都发生在付钱之前。

    照那个来源写的实现会编译得过、测试也绿,只是每一次双击都买两次。
    所以这里拒绝空版本,逼调用方去配置里取。
    """
    common = dict(
        spu_id="spu-1",
        scope=rs.canonical_scope(),
        fingerprints={None: "shared-1"},
        requested_fields=["neckline"],
    )
    expect_raises(
        ValueError, rs.idempotency_key, model_version=None, prompt_version="p1", **common
    )
    expect_raises(
        ValueError, rs.idempotency_key, model_version="  ", prompt_version="p1", **common
    )
    expect_raises(
        ValueError, rs.idempotency_key, model_version="m1", prompt_version=None, **common
    )


def test_a_key_without_a_spu_or_any_field_is_refused():
    """没有归属、或者什么都不问的 run 拿不到键。

    「一个什么都不问的 run」不是边界情况,它是调用方漏传了字段清单 ——
    而放行的表现是所有漏传的请求共用同一个键,于是第二个人的识别
    直接复用了第一个人的结果。
    """
    common = dict(
        scope=rs.canonical_scope(),
        fingerprints={None: "shared-1"},
        model_version="m1",
        prompt_version="p1",
    )
    expect_raises(
        ValueError, rs.idempotency_key, spu_id=None, requested_fields=["neckline"], **common
    )
    expect_raises(
        ValueError, rs.idempotency_key, spu_id="spu-1", requested_fields=[], **common
    )
    expect_raises(
        ValueError, rs.idempotency_key, spu_id="spu-1", requested_fields=["", "  "], **common
    )


def test_different_scopes_of_the_same_spu_get_different_keys():
    """同一个 SPU 的共享作用域与颜色作用域必须是两个键。

    合成一个的后果是拿某个颜色的结果当共享事实 —— 而共享事实是
    全部颜色都要用的那一份。
    """
    fingerprints = {None: "shared-1", "a": "fa", "b": "fb"}
    args = dict(
        spu_id="spu-1",
        fingerprints=fingerprints,
        model_version="m1",
        prompt_version="p1",
        requested_fields=["neckline"],
    )
    keys = {
        rs.idempotency_key(scope=rs.canonical_scope(), **args),
        rs.idempotency_key(scope=rs.canonical_scope(variant_ids=["a"]), **args),
        rs.idempotency_key(scope=rs.canonical_scope(variant_ids=["b"]), **args),
        rs.idempotency_key(scope=rs.canonical_scope(all_variants=True), **args),
    }
    assert len(keys) == 4, "四种作用域里有两种算出了同一个键"


# ================================================================ 七、接线


def test_the_terminal_verdict_is_asked_once_at_its_host_and_not_re_derived():
    """终态的宿主只做翻译:把三个计数递给判定,不自己写第二份三元运算。

    自己写一份的后果不是重复,是**两份判定各说各话** —— 而它们不一致时,
    接口返回的和界面显示的是两个结论。

    宿主在 `VERDICT_HOST`。A45-batch14-20 之前它是 `models/attribute.py`
    的派生属性,那一列落库之后挪到了写入方;搬家只改那个常量。
    """
    module, function = VERDICT_HOST
    host = _func(_tree(module), function)
    calls = [
        n
        for n in ast.walk(host)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == "terminal_status_for")
            or (isinstance(n.func, ast.Attribute) and n.func.attr == "terminal_status_for")
        )
    ]
    assert len(calls) == 1, (
        f"{module}::{function} 调用 `terminal_status_for` {len(calls)} 次 —— "
        "宿主只该问一次"
    )
    passed = {kw.arg for kw in calls[0].keywords}
    assert passed == {"image_count", "succeeded", "failed", "cancel_requested"}, (
        f"递给判定的入参变了:{sorted(passed)}"
    )
    # 判定结果必须**落到那一列**,不是算完丢掉。「算出来了但没用」是接线
    # 最常见的坏法,而它不报错:`status` 停在建行时那一档(RUNNING),
    # 于是每一次跑完的 run 都还占着幂等键 —— 同一个 SPU 再也识别不了第二次。
    #
    # 断言要点到**那次赋值的右边**,不是「函数里出现过 row.status =」:
    # 手写一份三元、再把判定的返回值丢给一个 `_unused`,前一种写法照样
    # 满足宽断言,而那正是本条变异 W1 的形状。
    assigns = [
        n
        for n in ast.walk(host)
        if isinstance(n, ast.Assign)
        and any(ast.unparse(t) == "row.status" for t in n.targets)
    ]
    assert len(assigns) == 1, f"`row.status` 被赋值 {len(assigns)} 次 —— 该只有一次"
    assert isinstance(assigns[0].value, (ast.Call, ast.Attribute)) and (
        "terminal_status_for" in ast.unparse(assigns[0].value)
    ), (
        f"`row.status` 赋的是 {ast.unparse(assigns[0].value)!r} —— "
        "判定算了却丢掉了,那一列会停在 RUNNING 并永远占着幂等键"
    )


def test_the_derived_status_property_does_not_come_back():
    """`ProductAttributeExtraction` 上不许再有名为 `status` 的属性/方法。

    列与属性并存不是重复,是**两个答案**:唯一索引的谓词 `status IN (...)`
    由数据库读**列**求值,而接口和界面读属性。两者漂移时的现象是
    「代码说该新建一个 run,库说这个键被占了」,报出来是一个和用户动作
    毫无关系的 409 —— 而没有任何测试会红。
    """
    tree = _tree("models/attribute.py")
    klass = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "ProductAttributeExtraction"
    )
    defined = {
        n.name for n in klass.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    assert "status" not in defined, (
        "`status` 又变回属性了 —— 它是 A45-batch14-20 起的真列,"
        "并存会让索引谓词和接口各说各话"
    )
    # **类体之外也要看。** `ProductAttributeExtraction.status = property(...)`
    # 写在模块底部同样能盖住那一列,而只扫类体的守卫一个字都不会说 ——
    # 这条是本批变异 S2 撞出来的洞,不是想出来的。
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        for target in node.targets:
            rendered = ast.unparse(target)
            assert not rendered.endswith(".status"), (
                f"模块级又把 `status` 盖掉了:{rendered} = ... —— "
                "索引谓词读列、接口读属性,两个答案"
            )
    # 反向:确认那一列确实还在,别让这条守卫在列被删掉之后变成平凡真
    columns = {
        n.target.id
        for n in klass.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    }
    assert "status" in columns, "`status` 列不见了"


def test_the_api_asks_the_single_verdict_before_merging_evidence():
    """要不要合并证据,判据是单点派生的 `run_is_authoritative`,
    不是在接口里重新数一遍 `succeeded_count`。

    今天两者的结论逐条相同,换的是将来:cancel 落地之后,被叫停的
    那次 run 同样会有 `succeeded_count > 0`(前几张已经跑完并付过钱),
    而它的证据不该改写事实。
    """
    src = _src("api/attributes.py")
    tree = ast.parse(src)
    fn = _func(tree, "extract_attributes")
    calls = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr == "run_is_authoritative"
    ]
    assert len(calls) == 1, "`extract_attributes` 没有问 `run_is_authoritative`"
    assert "succeeded_count" not in ast.unparse(fn), (
        "接口里又出现了 `succeeded_count` —— 判据要么在判定里,要么不存在"
    )


def test_a_run_where_everything_failed_gets_an_error_code():
    """全部失败时补 `error_code`。

    在这之前,**全部失败的 run 和全部成功的 run 在库里长得一模一样**:
    `error_code` 是 NULL、接口返回 200,差别只有两个计数。于是
    「最近有哪些识别是失败的」这个问题在 SQL 里问不出来,而按计数反推
    等于把判定又抄一份(前端已经抄过一份,还抄错了)。
    """
    tree = _tree("attributes/service.py")
    fn = _func(tree, "run_extraction")
    body = ast.unparse(fn)
    assert "EXTRACTION_ALL_FAILED" in body, "跑完之后没有补 error_code"
    # 断言要点到**那个比较**,不是「函数里出现过 row.status」——
    # A45-batch14-20 之后终态自己也写这一列,于是宽断言变成了平凡真
    # (本批变异 D3 撞出来的)
    assert "row.status == ExtractionRunStatus.FAILED.value" in body, (
        "补 error_code 的判据不是那一列 —— 它在自己重新数计数"
    )
    # 常量不许改成散在两处的字面量
    module_src = _src("attributes/service.py")
    assert 'EXTRACTION_ALL_FAILED = "EXTRACTION_ALL_IMAGES_FAILED"' in module_src


def test_the_response_default_status_is_the_closed_one():
    """出参的默认值必须是**不放行**的那一档。

    宽松默认值的漏法是 batch14-10 的 C2、也是硬规则 4 第二次事故的形状:
    字段没填上时悄悄放行,而两侧的测试全绿。这里读源码而不是 import ——
    `schemas/attribute.py` 顶层 import pydantic,这批用例刻意零三方依赖。
    """
    tree = ast.parse(_src("schemas/attribute.py"))
    klass = next(
        n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == "ExtractionOut"
    )
    defaults = [
        n.value.value
        for n in klass.body
        if isinstance(n, ast.AnnAssign)
        and isinstance(n.target, ast.Name)
        and n.target.id == "status"
        and isinstance(n.value, ast.Constant)
    ]
    assert defaults, "`ExtractionOut.status` 不见了,或者它没有默认值"
    assert defaults[0] in {s.value for s in ExtractionRunStatus}, (
        f"默认值 {defaults[0]!r} 不是一个合法的 run 状态"
    )
    assert rs.run_is_authoritative(defaults[0]) is False, (
        f"默认值 {defaults[0]!r} 是一档「算数」的状态 —— 漏字段时会悄悄放行"
    )


def test_the_attribute_tab_does_not_recompute_the_run_verdict():
    """前端不许自己判这次识别算哪一档(硬规则 4)。

    与 `test_the_environment_banner_does_not_recompute_the_verdict` 同型。
    这里判的原来是三档,而且判漏了「没跑完但一张都没失败」那一种。

    断言整行锚定,不用子串 —— 子串守卫挡不住「把那行注释掉」,
    本仓库栽过三次(batch13-3 的 M2 / M11、batch14-2 的 N15)。
    """
    tab = (FRONTEND / "components" / "workbench" / "AttributeTab.tsx").read_text(
        encoding="utf-8"
    )
    block = window(
        tab,
        "const extract = useMutation(",
        "const confirm = useMutation(",
        what="识别那个 mutation",
    )
    assert "RUN_STATUS_NOTICE[result.status]" in block, (
        "识别结果的提示不是按后端给的 status 挑的"
    )
    for banned in ("succeeded_count ?? 0) === 0", "failed_count ?? 0) > 0"):
        assert banned not in block, f"前端又在自己算三档了:{banned}"


# ================================================================ 八、欠账已还


def test_the_five_columns_landed_and_the_key_is_built_from_all_of_them():
    """§4.6 的五列到齐了(迁移 0040 / A45-batch14-20)。

    **这条守卫是上一版欠账守卫的正面。** 那一条写着「五列一个都没有,所以
    §9.2 接不上线」;列到齐之后它变红,按它自己的约定删掉,换成这一条。

    反向也钉:少一列不会有任何征兆 —— `idempotency_key()` 对缺维的入参
    抛 ValueError,而服务层把 ValueError 翻译成「这次没有键」,于是表现是
    **幂等静静地失效**,不是报错。
    """
    klass = next(
        n
        for n in ast.walk(_tree("models/attribute.py"))
        if isinstance(n, ast.ClassDef) and n.name == "ProductAttributeExtraction"
    )
    columns = {
        n.target.id
        for n in klass.body
        if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name)
    }
    for owed in ("status", "spu_id", "input_fingerprint", "requested_scope", "idempotency_key"):
        assert owed in columns, f"§4.6 的 `{owed}` 列不见了 —— 幂等会静静失效"


def test_the_partial_unique_index_is_declared_with_the_twin_predicate():
    """ORM 上那条部分唯一索引的谓词**由判定生成,不是手打的**。

    手打的后果是它和 `KEY_OCCUPYING_STATUSES` 漂移:代码说该新建一个 run,
    库说这个键被占了 —— 报出来是一个和用户动作毫无关系的 409。
    """
    klass = next(
        n
        for n in ast.walk(_tree("models/attribute.py"))
        if isinstance(n, ast.ClassDef) and n.name == "ProductAttributeExtraction"
    )
    args = next(
        n
        for n in klass.body
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "__table_args__" for t in n.targets)
    )
    rendered = ast.unparse(args)
    assert "uq_attr_extractions_idempotency_key" in rendered, "部分唯一索引不见了"
    assert "unique=True" in rendered, "那条索引不是唯一索引 —— 它挡不住任何东西"
    assert "run_state.unique_index_predicate()" in rendered, (
        "谓词是手打的字面量 —— 它会和 KEY_OCCUPYING_STATUSES 漂移"
    )


def test_the_migration_predicate_is_the_twin_of_the_pure_one():
    """迁移 0040 里那句字面量谓词,今天必须与判定生成的一字不差。

    迁移是**冻结在时间里**的脚本,所以它写字面量而不是调函数(理由在
    0040 的文档第四节)。正因为冻结,这条守卫才必须存在:
    `KEY_OCCUPYING_STATUSES` 哪天变了,它会红 —— 那时该做的是**再写一条
    迁移重建索引**,不是回来改那一行字面量。

    不检查这一条的后果很安静:新库按新判定建索引、老库按老索引跑,
    两边对「这个键被占了吗」给出不同答案,而只有其中一边会多付钱。
    """
    src = (BACKEND_ROOT / "migrations" / "versions" / "0040_extraction_run_identity.py").read_text(
        encoding="utf-8"
    )
    literal = next(
        n.value.value
        for n in ast.walk(ast.parse(src))
        if isinstance(n, ast.Assign)
        and any(isinstance(t, ast.Name) and t.id == "_KEY_OCCUPYING_PREDICATE" for t in n.targets)
        and isinstance(n.value, ast.Constant)
    )
    assert literal == rs.unique_index_predicate(), (
        f"迁移里的谓词 {literal!r} 和判定生成的 {rs.unique_index_predicate()!r} 不一样了 —— "
        "该新写一条迁移重建索引,不是改这一行"
    )


def test_the_terminal_verdict_can_only_land_on_edges_the_transition_table_allows():
    """`terminal_status_for` 的值域必须落在 `TRANSITIONS[RUNNING]` 里面。

    写入方建行即 RUNNING,跑完直接写终态,**中间不做 `can_transition` 检查**
    —— 理由在 `run_extraction` 的注释里:那个检查今天恒真,而它一旦不恒真,
    在那里抛异常会连着整次识别的证据一起回滚,钱已经花了。

    恒真这件事由这条守卫在纯层保证,不由运行期的一句 if 保证。
    """
    reachable = set()
    for image_count in range(0, 4):
        for succeeded in range(-1, 4):
            for failed in range(-1, 4):
                for cancel in (False, True):
                    reachable.add(
                        rs.terminal_status_for(
                            image_count=image_count,
                            succeeded=succeeded,
                            failed=failed,
                            cancel_requested=cancel,
                        )
                    )
    assert reachable <= rs.TRANSITIONS[S.RUNNING], (
        "判定能落到 RUNNING 出边之外的档:"
        f"{sorted(s.value for s in reachable - rs.TRANSITIONS[S.RUNNING])}"
    )
    # 反向:别让这条守卫在判定塌成恒返回一个值时还是绿的(batch14-8 的 X1)
    assert len(reachable) >= 3, f"整个输入空间只落出 {len(reachable)} 档,判定塌了"
