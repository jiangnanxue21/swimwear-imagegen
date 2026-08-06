"""环境真实性判定(REVIEW 任务 5)。

这一组穷举 `facet_fidelity` 的三个布尔输入(8 种组合),因为它的顺序
是这个模块唯一真正的判断,而顺序错了不会报错 —— 只会让一个纯 Mock 环境
被标成"真实"。
"""
from __future__ import annotations

from itertools import product

from app.core.environment import (
    Facet,
    Fidelity,
    build_facet,
    facet_fidelity,
    is_trustworthy,
    overall,
    pick_active,
)


def _diff(expected: dict, actual: dict) -> dict:
    """真值表对不上时,只列出不同的那几格。"""
    return {k: (expected[k], actual[k]) for k in expected if expected[k] is not actual[k]}


def _f(key: str, fidelity: Fidelity) -> Facet:
    return Facet(key=key, label=key, fidelity=fidelity, backend="x", detail="")


# ---------------------------------------------------------------- 单环节


def test_a_simulator_is_reported_as_simulated_whatever_configured_says():
    """**这一条是整个模块存在的理由,而且必须两个方向都钉住。**

    Mock 的 `is_configured()` 永远返回 True —— 它不需要任何配置。所以只看
    configured 的话,一个纯 Mock 环境每一项都是"已配置",判定会说 REAL,
    而运营会照着一批假图去批准上架。

    `configured=False` 那一半同样要钉:把 `is_simulator` 和 `configured`
    两个分支对调之后,**只有这一半会变**(configured=True 时对调前后都是
    SIMULATED)。第一次写这条时只测了 True 那一半,于是对调分支的变异
    没有被它抓住 —— 抓住它的是另一条讲 `implemented` 的用例,纯属侥幸:
    那条用例的夹具恰好没带 configured 键。夹具补一个键,这个洞就回来了。
    """
    for configured in (True, False):
        assert facet_fidelity(
            implemented=True, configured=configured, is_simulator=True
        ) is Fidelity.SIMULATED, f"configured={configured}"


def test_unimplemented_beats_everything_else():
    """骨架在、Key 也填了,但请求映射没写 —— 那不是"配好了"。

    ComfyUI / fal.ai 今天正是这个状态。报成 UNCONFIGURED 会让人去检查 Key,
    而 Key 是对的;报成 REAL 更糟。
    """
    for configured, sim in product([True, False], repeat=2):
        assert facet_fidelity(
            implemented=False, configured=configured, is_simulator=sim
        ) is Fidelity.UNAVAILABLE


def test_real_requires_all_three_conditions():
    assert facet_fidelity(implemented=True, configured=True, is_simulator=False) is (
        Fidelity.REAL
    )
    assert facet_fidelity(implemented=True, configured=False, is_simulator=False) is (
        Fidelity.UNCONFIGURED
    )


def test_every_input_combination_lands_somewhere_deterministic():
    """8 种组合全覆盖,且没有一种落进 REAL 之外的意外档。

    穷举而不是抽样:输入空间只有 8 个点,抽样在这里没有任何好处,
    而漏掉的那个点正是将来出问题的那个。
    """
    U, S, R, N = (
        Fidelity.UNAVAILABLE,
        Fidelity.SIMULATED,
        Fidelity.REAL,
        Fidelity.UNCONFIGURED,
    )
    #        (implemented, configured, is_simulator) -> 档位
    expected = {
        (False, False, False): U,
        (False, False, True): U,
        (False, True, False): U,
        (False, True, True): U,
        (True, False, False): N,
        (True, False, True): S,   # <- 分支顺序一对调,只有这一格会变
        (True, True, False): R,
        (True, True, True): S,
    }
    actual = {
        k: facet_fidelity(implemented=k[0], configured=k[1], is_simulator=k[2])
        for k in product([True, False], repeat=3)
    }
    assert actual == expected, (
        "真值表变了。逐格核对 —— 这张表里每一格都有一条具体后果,"
        f"不同的是:{_diff(expected, actual)}"
    )


# ---------------------------------------------------------------- 整体


def test_simulated_outshouts_unconfigured():
    """一个 Mock 环节 + 一个没配好的环节 -> 报 SIMULATED。

    两者的失败方式不同:没配好会当场抛错,人一定会发现;Mock 会安静地
    产出一批合理的假数据,然后那批数据会被批准、写进草稿、导出成上架文件。
    **会静默产生错误结论的那一个更需要常驻提示。**
    """
    assert overall(
        [_f("a", Fidelity.UNCONFIGURED), _f("b", Fidelity.SIMULATED)]
    ) is Fidelity.SIMULATED


def test_one_simulated_facet_is_enough_to_taint_the_whole_thing():
    """真 Provider + 真评分 + Mock 属性 = 产出仍然不能当真。

    属性会被写进商品、进文案宣称、进上架草稿。一个环节假,链路末端就是假的。
    """
    facets = [
        _f("image", Fidelity.REAL),
        _f("score", Fidelity.REAL),
        _f("attribute", Fidelity.SIMULATED),
    ]
    assert overall(facets) is Fidelity.SIMULATED
    assert not is_trustworthy(overall(facets))


def test_all_real_is_trustworthy():
    facets = [_f("a", Fidelity.REAL), _f("b", Fidelity.REAL)]
    assert overall(facets) is Fidelity.REAL
    assert is_trustworthy(overall(facets))


def test_no_facets_does_not_blow_up():
    """算不出来时不抛错。调用点是一条横幅,整页报错比少一条横幅糟得多。"""
    assert overall([]) is Fidelity.REAL


def test_only_real_counts_as_trustworthy():
    """四档里只有一档算可信 —— 包括 UNCONFIGURED 和 UNAVAILABLE。

    「跑不起来」不该被当成「可信」,哪怕它确实不会产出假数据:
    这个判定的消费方是一句给运营看的话,而"环境正常"对着一个跑不通的
    环境说出来,下一次它说真话时就没人信了。
    """
    for f in Fidelity:
        assert is_trustworthy(f) is (f is Fidelity.REAL), f


# ---------------------------------------------------------------- 挑出生效的那一个


def test_the_registry_row_marked_active_wins():
    rows = [{"name": "a"}, {"name": "b", "active": True}]
    assert pick_active(rows, "a")["name"] == "b", "注册表自己报的 active 优先于配置名"


def test_falling_back_to_the_configured_name():
    rows = [{"name": "mock"}, {"name": "fashn"}]
    assert pick_active(rows, "fashn")["name"] == "fashn"


def test_nothing_matches_means_nothing_not_the_first_row():
    """**挑不到时不拿第一行顶上。**

    顶一行的后果是:注册表的顺序变一次,状态条的结论就变一次。而这一档
    回答的是"运营现在看到的东西是不是真的",它不该取决于一个 sorted 的稳定性。
    """
    assert pick_active([{"name": "mock"}, {"name": "fashn"}], "ghost") is None
    assert pick_active([], "mock") is None
    assert pick_active([{"name": "mock"}], None) is None


def test_channels_are_matched_by_their_own_key():
    """渠道注册表用 `channel` 而不是 `name`。两个键都要认。"""
    assert pick_active([{"channel": "generic"}], "generic")["channel"] == "generic"


# ---------------------------------------------------------------- 翻译成档位


def test_a_name_that_is_not_in_the_registry_is_unavailable_not_unconfigured():
    """配了一个注册表里没有的名字,那不是"没配好",是配错了。

    两句话指向不同的动作:「没配好」让人去填 Key,「找不到」让人去查拼写。
    """
    facet = build_facet(
        key="image", label="出图", row=None, wanted="ghost",
        real_detail="真", simulated_detail="假",
    )
    assert facet.fidelity is Fidelity.UNAVAILABLE
    assert "ghost" in facet.detail, "说不出是哪个名字的话,这条提示没法照着查"


def test_a_registry_that_omits_implemented_is_not_treated_as_unimplemented():
    """四个注册表里有两个不报 `implemented` —— 它们只登记已实现的后端。

    默认 False 的话,属性识别和渠道会**恒被判成 UNAVAILABLE**,
    于是状态条永远在喊"跑不通",而它其实在正常工作。
    """
    facet = build_facet(
        key="attribute", label="属性", row={"name": "mock", "is_simulator": True},
        wanted="mock", real_detail="真", simulated_detail="假",
    )
    assert facet.fidelity is Fidelity.SIMULATED


def test_the_detail_line_differs_per_verdict():
    """每一档说的话必须不同 —— 四档共用一句话等于没有这一列。"""
    row_real = {"name": "fashn", "configured": True, "implemented": True}
    row_sim = {"name": "mock", "configured": True, "implemented": True, "is_simulator": True}
    row_unconf = {"name": "fashn", "configured": False, "implemented": True}
    details = {
        build_facet(key="k", label="l", row=r, wanted="fashn",
                    real_detail="真的在调外部服务", simulated_detail="出的是本地假图").detail
        for r in (row_real, row_sim, row_unconf)
    }
    assert len(details) == 3


def test_the_backend_name_is_reported_so_a_screenshot_is_evidence():
    """报出生效的那个后端叫什么。运营截图时这一格就是证据。"""
    facet = build_facet(
        key="image", label="出图",
        row={"name": "fashn", "configured": True, "implemented": True},
        wanted="fashn", real_detail="真", simulated_detail="假",
    )
    assert facet.backend == "fashn"


# ---------------------------------------------------------------- 注册表接缝
#
# **这一节是补的(A42),它守的是上面那些用例全部守不到的地方。**
#
# 上面每一条都用手写的字典喂 `build_facet`,所以判定被穷举钉死了;而各注册表
# 也各自有自己的形状测试。缺的是中间那道缝:**注册表到底报不报得出判定要读的
# 那三列。** 缺一列的表现不是报错,是 `row.get("is_simulator")` 取到 `None`,
# `bool(None)` 是 False,而 Mock 的 `is_configured()` 恒为 True ——
# 判定于是一路走到 REAL,状态条对运营说「真的在调外部出图服务,会产生费用」。
#
# 这个缺陷在 a37～a41 之间一直活着,四档里错了三档(出图 REAL、评分 REAL、
# 渠道 UNAVAILABLE),而两侧的测试全绿。切错测试边界的代价就是这样。


def _registry_rows():
    """四个注册表各自的行 + 判定要读的那几列。**真的调注册表,不是夹具。**"""
    from app.channels.registry import describe_channels
    from app.evaluators.registry import describe_all as describe_evaluators
    from app.extractors.registry import describe_extractors
    from app.providers.registry import describe_all as describe_providers

    return {
        "providers": describe_providers(),
        "evaluators": describe_evaluators(),
        "extractors": describe_extractors(),
        "channels": describe_channels(),
    }


def test_every_registry_reports_the_column_the_verdict_reads():
    """`is_simulator` 是 `facet_fidelity` 的第一个分支,四个注册表都必须报。

    `implemented` 与 `configured` 不在这条里:`build_facet` 对缺席的
    `implemented` 按 True 兜底(有意的,extractors / channels 只登记已实现的),
    而 `configured` 缺席时判成"没配好",那是安全的方向。
    **只有 `is_simulator` 缺席会往"这是真的"那个方向错**,所以只钉它。
    """
    for name, rows in _registry_rows().items():
        assert rows, f"{name} 注册表是空的,状态条会少一整档"
        for row in rows:
            assert "is_simulator" in row, (
                f"{name} 的行少了 is_simulator。判定会 bool(None) -> False,"
                f"于是一个纯 Mock 环境被报成 REAL —— 这正是这个模块要防的事"
            )
            assert isinstance(row["is_simulator"], bool), (
                f"{name} 的 is_simulator 必须是 bool,不能是 None 或字符串"
            )


def test_every_registry_row_can_be_located_by_name():
    """`pick_active` 的两条线索至少要有一条能用,否则那一档恒为 UNAVAILABLE。

    渠道那一档就是这么错的:行里没有 `active` 键(它不是一张"选一个"的表),
    调用方又传了 `None` 把第二条线索掐掉,`pick_active` 只能返回 None,
    而 `build_facet` 把 None 判成 UNAVAILABLE —— 状态条常年说
    「generic 还没实现,只有骨架」,而它正在用 Simulator 正常工作。
    """
    for name, rows in _registry_rows().items():
        for row in rows:
            assert row.get("name") or row.get("channel"), (
                f"{name} 的行既没有 name 也没有 channel,pick_active 按名字找不到它"
            )


def test_the_default_mock_deployment_reports_every_facet_as_simulated():
    """**这一条是整个接缝的验收口径。**

    默认配置 = 出图 mock + 评分 mock + 属性 mock + 渠道 Simulator。
    四档必须全部 SIMULATED,整体判定必须 SIMULATED 且不可信。

    这一条曾经是红的(错三档),而 `make test-pure` 全绿 —— 因为当时没有
    任何一条用例真的去调注册表。REVIEW §10 的人工测试准入标准第 4 条
    (「Mock/真实可见 —— 界面截图;切到 Mock 后状态条改变」)靠的就是它。
    """
    from app.services.environment import describe

    result = describe()
    by_key = {f["key"]: f for f in result["facets"]}
    assert set(by_key) == {"image", "scoring", "attribute", "channel"}
    for key, facet in by_key.items():
        assert facet["fidelity"] == Fidelity.SIMULATED.value, (
            f"默认 Mock 环境下 {key} 被判成 {facet['fidelity']};"
            f"当前生效后端是 {facet['backend']}"
        )
    assert result["fidelity"] == Fidelity.SIMULATED.value
    assert result["trustworthy"] is False


def test_a_provider_that_forgets_to_declare_itself_is_assumed_fake():
    """基类默认 `is_simulator = True`。**两个方向的错误代价不对称。**

    忘了在新的真 Provider 上写 False -> 多喊一次"这是模拟环境",一眼看得见;
    忘了在新的 Mock 上写 True -> 安静地说"会产生费用",运营照着假图批准上架。
    默认值必须站在吵闹的那一边。
    """
    from app.evaluators.base import ImageQualityEvaluator
    from app.providers.base import ImageGenerationProvider

    assert ImageGenerationProvider.is_simulator is True
    assert ImageQualityEvaluator.is_simulator is True


def test_the_simulator_column_traces_to_the_implementation_not_a_list():
    """硬规则 4:每个状态字段都要能追溯到真实来源。

    这一列问的是实现类自己(`provider.is_simulator`),不是注册表里的一张名单。
    名单式的写法在 `describe_extractors` 里,而它的注释自己就承认
    「接真后端时这一行必须改成问它自己」。
    """
    from app.providers.registry import describe_all, get_provider

    for row in describe_all():
        assert row["is_simulator"] == get_provider(row["name"]).is_simulator
