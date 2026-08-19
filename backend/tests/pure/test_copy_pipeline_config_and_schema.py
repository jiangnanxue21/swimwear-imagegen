"""文案生成链路的配置生效、严格解析与幂等指纹(评审第 3 / 11 / 13 / 14 条)。

这四条咬在一起,所以放在同一个文件里:

    11  配置生效     选了 llm 就要真的走 llm,不能恒定退回模板
    13  api_style    选了 responses 就要发 /responses
    14  严格解析     结构不对整体拒收,不猜模型想说什么
    3   幂等指纹     上面三样任何一个变了,旧结果就不许再被复用

第 3 条依赖前三条是真的:指纹里写进「模型名」「提示词版本」「生成器」,
前提是这些东西确实决定输出。配置根本没生效的时候,把它们塞进指纹
只是让指纹变长。
"""
from __future__ import annotations

from app.core.errors import ErrorCode, ValidationError
from app.listings import copy_rules
from app.listings.copy_generator import (
    LLM_SYSTEM_PROMPT,
    LLMCopyGenerator,
)
from app.workbench.batch import (
    COPY_GENERATORS,
    TEXT_API_STYLES,
    generator_fingerprint,
)
from tests.pure._helpers import BACKEND_ROOT, expect_raises  # noqa: F401


def _gen(**kw) -> LLMCopyGenerator:
    base = dict(
        api_key="k", base_url="https://example.invalid/v1", model="m-1",
        api_style="chat_completions",
    )
    base.update(kw)
    return LLMCopyGenerator(**base)


# ---------------------------------------------------------------- 11 配置生效
#
# `Settings` 要 pydantic,容器里没有,所以这一节测的是**两边共用的那份白名单**
# 与注册表不许漂移。真正的「拼错的值在启动期被拒」需要 pytest 套件跑,
# 见交接文档里未验证项那一节。


def test_the_generator_registry_matches_the_shared_whitelist():
    """`config.py` 的校验器和 `copy_generator` 的注册表认同一份名单。

    原来 `get_generator()` 读的是 `getattr(settings, "COPY_GENERATOR", …)`,
    而那个字段根本没声明 —— `getattr` 的兜底把「字段不存在」和「用户选了
    template」变成了同一件事。于是设置页上无论选什么,拿到的恒定是模板
    生成器,配好密钥和模型名也没有任何效果,而且没有任何地方会说一句。

    名单一旦两边各写一份,加了新生成器却忘了改校验器的表现是:新生成器
    在启动期被拒,而报错信息指着一份已经过期的白名单。
    """
    from app.listings.copy_generator import GENERATOR_FACTORIES

    assert set(GENERATOR_FACTORIES) == set(COPY_GENERATORS), (
        f"注册表 {sorted(GENERATOR_FACTORIES)} 与白名单 {sorted(COPY_GENERATORS)} 不一致"
    )


def test_get_generator_reads_a_declared_field_not_a_getattr_fallback():
    """源码级断言:`getattr(settings, "COPY_GENERATOR"` 这个写法不许回来。

    这一条是防回归的。行为级的验证要 pydantic,而这个具体的坏写法
    正是第 11 条的根因,值得单独钉住。

    **看代码不看文档字符串。** `get_generator` 的说明里原样引用了那个坏
    写法来解释它错在哪 —— 直接在 `inspect.getsource()` 的结果里搜字符串
    会被自己的解释文字触发。和 `test_pure_suite_is_self_contained` 里
    不用正则用 AST 是同一个理由。
    """
    import ast
    import inspect
    import textwrap

    from app.listings import copy_generator

    tree = ast.parse(textwrap.dedent(inspect.getsource(copy_generator.get_generator)))
    fn = tree.body[0]
    assert isinstance(fn, ast.FunctionDef)
    body = fn.body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]          # 去掉文档字符串
    code = "\n".join(ast.unparse(node) for node in body)

    assert "getattr" not in code, (
        "COPY_GENERATOR 必须当成 Settings 上的真字段读;"
        "getattr 兜底把「字段不存在」和「用户选了 template」变成了同一件事"
    )
    assert "settings.COPY_GENERATOR" in code


def test_both_whitelists_are_non_empty_and_lowercase():
    """校验器会 `.strip().lower()` 之后再比对,名单本身必须已经是小写。"""
    for name, values in (("COPY_GENERATORS", COPY_GENERATORS),
                         ("TEXT_API_STYLES", TEXT_API_STYLES)):
        assert values, f"{name} 不能为空"
        for v in values:
            assert v == v.strip().lower(), f"{name} 里的 {v!r} 不是小写"


# ---------------------------------------------------------------- 13 api_style


def test_responses_style_targets_the_responses_endpoint():
    """原来无论配什么都发 /chat/completions。

    配成 responses 的自建端点上,那个请求会 404,而 404 在传输层被归类成
    「厂商不可达」—— 排查方向从第一步就是错的。
    """
    sent = {}

    def fake_send(request):
        sent["url"] = request.url
        sent["body"] = request.body
        return {"output": [{"content": [{"type": "output_text", "text": "{}"}]}]}

    _run_capture(_gen(api_style="responses"), fake_send)
    assert sent["url"].endswith("/responses"), sent["url"]
    assert "input" in sent["body"], "responses 形状的请求体用 input,不是 messages"
    assert "messages" not in sent["body"]


def test_chat_completions_style_targets_the_chat_endpoint():
    sent = {}

    def fake_send(request):
        sent["url"] = request.url
        sent["body"] = request.body
        return {"choices": [{"message": {"content": "{}"}}]}

    _run_capture(_gen(api_style="chat_completions"), fake_send)
    assert sent["url"].endswith("/chat/completions"), sent["url"]
    assert "messages" in sent["body"]
    assert "input" not in sent["body"]


def _run_capture(generator, fake_send):
    """拦掉 HTTP,只看请求长什么样、响应被哪个解析器解。

    不碰网络:容器无网络,而这条断言要的本来也只是请求形状。
    """
    import app.llm.transport as transport

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def send(self, request, *, on_attempt=None):
            if on_attempt is not None:
                on_attempt(1)
            payload = fake_send(request)
            payload.setdefault("id", "copy-test-response")
            payload.setdefault("model", "m-1")
            payload.setdefault(
                "usage",
                {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            )
            return payload, 200

    original = transport.MultimodalClient
    transport.MultimodalClient = _FakeClient
    try:
        return generator._call("sys", "user")
    finally:
        transport.MultimodalClient = original


# ---------------------------------------------------------------- 14 严格解析


def test_a_string_bullet_points_is_rejected_not_split_into_characters():
    """**这条是第 14 条里最要命的一个。**

    `tuple(str(b) for b in "柔软面料")` 是合法的 Python,结果是四条
    单字卖点。类型对、长度检查也过,一路写进 Excel 才会被人看见。
    """
    exc = expect_raises(
        ValidationError,
        _gen().parse_response,
        '{"title":"t","bullet_points":"柔软面料","description":"d"}',
    )
    assert exc.code is ErrorCode.COPY_COMPLIANCE_FAILED
    assert "bullet_points" in exc.message


def test_a_non_string_title_is_rejected_not_stringified():
    """`str({"en": "..."})` 会把一个字典变成合法字符串进标题列。"""
    exc = expect_raises(
        ValidationError,
        _gen().parse_response,
        '{"title":{"en":"hi"},"description":"d"}',
    )
    assert "title" in exc.message


def test_a_non_string_bullet_item_is_rejected():
    exc = expect_raises(
        ValidationError,
        _gen().parse_response,
        '{"title":"t","description":"d","bullet_points":["ok",123]}',
    )
    assert "bullet_points[1]" in exc.message


def test_a_malformed_claim_is_rejected_not_silently_dropped():
    """claims 是 §8.4 里唯一能被后端逐字验证的东西。

    静默丢掉一条等于少验一条,而少验的那条恰恰最可能是模型编出来的。
    """
    exc = expect_raises(
        ValidationError,
        _gen().parse_response,
        '{"title":"t","description":"d","claims":["not-an-object"]}',
    )
    assert "claims[0]" in exc.message


def test_a_claim_without_a_field_name_is_rejected():
    exc = expect_raises(
        ValidationError,
        _gen().parse_response,
        '{"title":"t","description":"d","claims":[{"text_span":"x","location":"title"}]}',
    )
    assert "field_name" in exc.message


def test_a_well_formed_response_still_parses():
    """防误伤。收紧解析最容易的失败方式是把合法响应也拦下来。"""
    draft = _gen().parse_response(
        '{"title":"Navy Swimsuit","description":"d",'
        '"bullet_points":["soft","stretchy"],"keywords":["swim"],'
        '"claims":[{"field_name":"primary_color","value":"NAVY_BLUE",'
        '"text_span":"Navy","location":"title"}]}'
    )
    assert draft.title == "Navy Swimsuit"
    assert draft.bullet_points == ("soft", "stretchy")
    assert draft.claims[0]["field_name"] == "primary_color"
    # value 刻意不限类型:facts 里本来就有数字和布尔
    assert draft.claims[0]["value"] == "NAVY_BLUE"


def test_generate_keeps_only_safe_call_trace_not_prompt_or_response_text():
    generator = _gen()

    def fake_send(request):
        return {
            "id": "resp-safe",
            "model": "m-safe",
            "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            "choices": [{"message": {"content": '{"title":"Navy Swimwear","description":"d"}'}}],
        }

    draft = _run_capture_generate(generator, fake_send)
    assert draft.trace["response_id"] == "resp-safe"
    assert draft.trace["total_tokens"] == 20
    assert draft.trace["provider_attempts"] == 1
    assert "prompt" not in draft.trace
    assert "response" not in draft.trace


def _run_capture_generate(generator, fake_send):
    import app.llm.transport as transport

    class _FakeClient:
        def __init__(self, *a, **kw):
            pass

        async def send(self, request, *, on_attempt=None):
            if on_attempt is not None:
                on_attempt(1)
            return fake_send(request), 200

    original = transport.MultimodalClient
    transport.MultimodalClient = _FakeClient
    try:
        return generator.generate(facts={}, selling_points=(), forbidden_claims=(), locale="en")
    finally:
        transport.MultimodalClient = original


def test_missing_optional_fields_are_still_allowed():
    """没给 bullet_points / keywords / claims 不是结构错误。"""
    draft = _gen().parse_response('{"title":"t","description":"d"}')
    assert draft.bullet_points == ()
    assert draft.keywords == ()
    assert draft.claims == ()


def test_a_code_fenced_response_still_parses():
    draft = _gen().parse_response('```json\n{"title":"t","description":"d"}\n```')
    assert draft.title == "t"


def test_the_prompt_version_is_not_a_hardcoded_one():
    """原来落库的是字面量 `1` —— 改了 build_prompt 的措辞,版本号一动不动,

    「这一版为什么和上一版不一样」就没法回答了,幂等也认不出提示词变过。
    """
    draft = _gen(model="m-9").parse_response('{"title":"t","description":"d"}')
    from app.prompts.versioning import content_version

    assert draft.prompt_version == f"m-9:{content_version(LLM_SYSTEM_PROMPT)}"
    assert not draft.prompt_version.endswith(":1")


# ---------------------------------------------------------------- 3 幂等指纹


def test_the_rules_version_is_a_real_constant():
    """指纹里引用它。原来我写成 `getattr(..., '?')` 兜底,而 `copy_rules`
    上根本没有这个常量 —— 那样规则改了指纹也不会变,等于白加。"""
    assert isinstance(copy_rules.RULES_VERSION, str)
    assert copy_rules.RULES_VERSION.strip()


#: (用例名, 相对基线改动的参数)。基线是一套 llm 配置。
FINGERPRINT_VARIANTS: tuple[tuple[str, dict], ...] = (
    ("换模型", {"model": "another-model"}),
    ("换 api 形状", {"api_style": "responses"}),
    ("换提示词版本", {"prompt_version": "llm-2"}),
    ("换规则版本", {"rules_version": "rules-2"}),
    ("换生成器", {"generator": "template"}),
)


def test_the_copy_fingerprint_changes_with_every_output_deciding_input():
    """评审第 3 条的正题。

    换模型、换 api 形状、换提示词、换规则版本之后,上游属性一个字没动 ——
    旧指纹完全命中,系统安静地复用上一版文案,而运营看到的是「已复用」。
    「不重复触发付费调用」这条本意是省钱,在这里变成了拿旧结果冒充
    新配置的产物。
    """
    base = dict(
        generator="llm",
        model="model-1",
        api_style="chat_completions",
        prompt_version="llm-1",
        rules_version="rules-1",
    )
    baseline = generator_fingerprint(**base)
    seen = {baseline: "基线"}

    for name, override in FINGERPRINT_VARIANTS:
        value = generator_fingerprint(**{**base, **override})
        assert value != baseline, (
            f"[{name}] 改了会决定输出的配置,指纹却没变 —— "
            f"旧的付费结果会被当成新配置的产物复用"
        )
        assert value not in seen, f"[{name}] 和 [{seen[value]}] 算出了同一个指纹"
        seen[value] = name


def test_the_copy_fingerprint_is_stable_for_the_same_configuration():
    """同一份配置必须给同一个值,否则重算一次就 STALE 一次,这个机制变成噪音。"""
    kw = dict(generator="llm", model="m", api_style="responses",
              prompt_version="p", rules_version="r")
    assert generator_fingerprint(**kw) == generator_fingerprint(**kw)


def test_the_copy_fingerprint_ignores_the_api_key():
    """密钥不改变输出,而指纹会进日志和接口响应。

    签名里根本没有 api_key 这个参数 —— 这条断言钉的是「别哪天加进来」。
    """
    import inspect

    params = set(inspect.signature(generator_fingerprint).parameters)
    assert "api_key" not in params
    assert not any("key" in p for p in params)


def test_template_mode_does_not_smuggle_in_a_model_name():
    """选了本地模板却把模型名算进指纹的话,换一个用不到的模型名
    会让全批文案凭空重跑一遍 —— 那是反过来的浪费。"""
    with_model = generator_fingerprint(
        generator="template", model="m-1", api_style="responses",
        prompt_version="template-1", rules_version="rules-1",
    )
    without = generator_fingerprint(
        generator="template", prompt_version="template-1", rules_version="rules-1",
    )
    assert with_model == without


def test_an_empty_model_name_is_not_the_same_as_a_missing_one():
    """空值折成显式的 '?' 而不是空串:空串会和「配置为空」撞在一起。"""
    value = generator_fingerprint(
        generator="llm", model="", api_style="", 
        prompt_version="p", rules_version="r",
    )
    assert "model:?" in value and "style:?" in value
