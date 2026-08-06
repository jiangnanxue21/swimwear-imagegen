"""代码评审修复的回归测试。

每一条都对应评审里的一个编号。写成测试而不是改完就算,是因为这些问题有一个共同点:
**它们都不会报错**。幂等键漏字段表现为\u300c提交成功但拿到旧图\u300d,
CSV 注入表现为\u300c文件能打开\u300d,重定向绕过表现为\u300c下载成功\u300d。
没有断言钉住,下一次重构会原样把它们放回来。

只覆盖能在无数据库环境跑的部分。涉及真实事务的(原子认领的并发语义、
成品图唯一索引)由 tests/test_*.py 在真库上覆盖。
"""
from __future__ import annotations

from app.core.net_safety import UnsafeDownloadURL, stream_checked
from app.services.export_format import escape_formula, row_for
from app.workflows.idempotency import build_idempotency_key
from app.workflows.state_machine import CLAIM_TARGET, CLAIMABLE_STATUSES, assert_can
from tests.pure._helpers import expect_raises


def _key(**overrides):
    base = dict(
        product_id="p1",
        mode="virtual_try_on",
        provider="fashn",
        asset_ids=["a1"],
        candidate_count=4,
    )
    base.update(overrides)
    return build_idempotency_key(**base)


# ---------------------------------------------------------------- 评审 #4 幂等键


def test_changing_the_prompt_produces_a_different_task():
    """漏掉提示词的后果:改了提示词重新提交,静默命中旧任务,拿回上一版的图。"""
    assert _key(prompt="强调面料光泽") != _key(prompt="强调剪裁轮廓")


def test_every_result_affecting_input_changes_the_key():
    baseline = _key()
    for field, value in (
        ("prompt", "换个说法"),
        ("negative_prompt", "不要水印"),
        ("output_width", 1024),
        ("output_height", 1536),
        ("base_seed", 12345),
        ("max_rounds", 5),
        ("routing_mode", "PRIORITY"),
        ("provider_params", {"segmentation_free": True}),
    ):
        assert _key(**{field: value}) != baseline, f"{field} 改了却算出同一个幂等键"


def test_provider_params_key_order_does_not_matter():
    """同一份参数换个书写顺序还是同一份参数,否则幂等形同虚设。"""
    assert _key(provider_params={"a": 1, "b": 2}) == _key(provider_params={"b": 2, "a": 1})


def test_nested_provider_params_are_also_order_insensitive():
    left = {"mock_evaluator": {"outcome": "a", "round_number": 1}}
    right = {"mock_evaluator": {"round_number": 1, "outcome": "a"}}
    assert _key(provider_params=left) == _key(provider_params=right)


def test_same_inputs_still_deduplicate():
    """收紧之后不能矫枉过正 —— 完全相同的输入必须仍然命中同一个任务。"""
    assert _key() == _key()


def test_explicit_key_still_wins():
    assert _key(explicit_key="ops-2026-07-28") == "ops-2026-07-28"


# ---------------------------------------------------------------- 评审 #26 CSV 注入


def test_formula_prefixes_are_neutralised():
    for dangerous in ("=1+1", "+1", "-1", "@SUM(A1)", "=HYPERLINK(\"http://x\")"):
        assert escape_formula(dangerous).startswith("'"), f"{dangerous} 没有被转义"


def test_ordinary_text_is_untouched():
    for safe in ("SW-001-BLK-S", "黑色连体泳衣", "1+1", ""):
        assert escape_formula(safe) == safe


def test_product_name_from_a_row_is_escaped():
    row = row_for(
        {
            "product": {"sku": "=cmd|'/c calc'!A1", "spu": "S", "name": "正常名字"},
            "images": {},
        }
    )
    assert row[0].startswith("'"), "SKU 里的公式没有被转义"
    assert row[2] == "正常名字"


# ---------------------------------------------------------------- 评审 #2 原子认领


def test_every_claimable_status_can_legally_reach_the_claim_target():
    """认领用一条 UPDATE 直接写状态,绕过了 assert_can。

    绕过是必要的(判定必须和 UPDATE 同属一条语句),但合法性不能因此没人管 ——
    这条测试就是那个管的人。往 CLAIMABLE_STATUSES 里加一个状态机不认的状态,
    这里会立刻红。
    """
    assert CLAIMABLE_STATUSES, "认领状态清单不能为空"
    for status in CLAIMABLE_STATUSES:
        assert_can(status, CLAIM_TARGET)


def test_created_is_not_directly_claimable():
    """CREATED 必须先经过 QUEUED。直接认领会跳过状态机里那条边。"""
    assert "CREATED" not in CLAIMABLE_STATUSES


# ---------------------------------------------------------------- 评审 #10 重定向


class _FakeResponse:
    def __init__(self, location: str | None = None):
        self.headers = {"location": location} if location else {}
        self.is_redirect = location is not None
        self.closed = False

    def close(self):
        self.closed = True


class _FakeClient:
    """记录每一次实际请求的地址。真正要断言的是\u300c它去访问了哪里\u300d。"""

    def __init__(self, hops: dict[str, str | None]):
        self.hops = hops
        self.visited: list[str] = []

    def build_request(self, method, url):  # noqa: ARG002
        return url

    def send(self, request, stream=False, follow_redirects=False):  # noqa: ARG002
        self.visited.append(request)
        return _FakeResponse(self.hops.get(request))


def test_redirect_to_the_metadata_endpoint_is_blocked():
    """公网地址 302 到云元数据端点,是 SSRF 最经典的一条绕过路径。

    只校验最初那个 URL 等于没校验 —— 这条测试直接断言\u300c它没有去访问那里\u300d。
    """
    client = _FakeClient({
        "https://cdn.example.com/a.png": "http://169.254.169.254/latest/meta-data/",
    })
    expect_raises(
        UnsafeDownloadURL, stream_checked, client,
        "https://cdn.example.com/a.png", resolve=False,
    )
    assert "http://169.254.169.254/latest/meta-data/" not in client.visited


def test_redirect_to_a_private_address_is_blocked():
    client = _FakeClient({"https://cdn.example.com/a.png": "http://10.0.0.5/secret"})
    expect_raises(
        UnsafeDownloadURL, stream_checked, client,
        "https://cdn.example.com/a.png", resolve=False,
    )
    assert "http://10.0.0.5/secret" not in client.visited


def test_relative_redirect_is_resolved_before_checking():
    """相对跳转不补全就去校验,校验的是一个没有主机名的串,必然放行。"""
    client = _FakeClient({
        "https://cdn.example.com/a.png": "/redirected/b.png",
        "https://cdn.example.com/redirected/b.png": None,
    })
    stream_checked(client, "https://cdn.example.com/a.png", resolve=False)
    assert client.visited[-1] == "https://cdn.example.com/redirected/b.png"


def test_normal_public_redirect_still_works():
    client = _FakeClient({
        "https://cdn.example.com/a.png": "https://files.example.com/a.png",
        "https://files.example.com/a.png": None,
    })
    response = stream_checked(client, "https://cdn.example.com/a.png", resolve=False)
    assert not response.is_redirect
    assert len(client.visited) == 2


def test_redirect_loop_is_stopped():
    client = _FakeClient({"https://a.example.com/x": "https://a.example.com/x"})
    err = expect_raises(
        UnsafeDownloadURL, stream_checked, client, "https://a.example.com/x", resolve=False
    )
    assert "重定向" in str(err)


def test_redirect_without_location_is_rejected():
    class _Broken(_FakeClient):
        def send(self, request, stream=False, follow_redirects=False):  # noqa: ARG002
            self.visited.append(request)
            response = _FakeResponse()
            response.is_redirect = True
            return response

    expect_raises(
        UnsafeDownloadURL, stream_checked, _Broken({}),
        "https://cdn.example.com/a.png", resolve=False,
    )


# ---------------------------------------------------------------- 评审 #5/#14 未实现


def test_implemented_sets_are_a_subset_of_what_is_registered():
    """\u300c已实现\u300d清单里出现一个没注册的名字,等于开了一道谁也走不通的门。"""
    from app.evaluators.registry import EVALUATOR_FACTORIES, IMPLEMENTED_EVALUATORS
    from app.providers.registry import IMPLEMENTED_PROVIDERS, PROVIDER_FACTORIES

    assert IMPLEMENTED_PROVIDERS <= set(PROVIDER_FACTORIES)
    assert IMPLEMENTED_EVALUATORS <= set(EVALUATOR_FACTORIES)


def test_describe_reports_implemented_consistently_with_the_registry():
    from app.evaluators.registry import IMPLEMENTED_EVALUATORS
    from app.evaluators.registry import describe_all as describe_evaluators
    from app.providers.registry import IMPLEMENTED_PROVIDERS, describe_all

    for entry in describe_all():
        assert entry["implemented"] == (entry["name"] in IMPLEMENTED_PROVIDERS)
    for entry in describe_evaluators():
        assert entry["implemented"] == (entry["name"] in IMPLEMENTED_EVALUATORS)


# ---------------------------------------------------------------- 评审 #23 字段上限


def test_field_limits_match_the_database_columns():
    """上限表与真实列宽对不上,就会出现「解析放行、写库报错」的行为。

    用 AST 读 models/product.py 的 String(n),不 import SQLAlchemy。
    """
    import ast
    import re

    from app.core.field_limits import PRODUCT_FIELD_LIMITS
    from tests.pure._helpers import BACKEND_ROOT

    source = (BACKEND_ROOT / "app" / "models" / "product.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    columns: dict[str, int] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name)):
            continue
        rendered = ast.unparse(node.value) if node.value else ""
        match = re.search(r"String\((\d+)\)", rendered)
        if match:
            columns[node.target.id] = int(match.group(1))

    for field, limit in PRODUCT_FIELD_LIMITS.items():
        assert field in columns, f"{field} 在模型里找不到对应列"
        assert limit == columns[field], (
            f"{field}: 上限表写 {limit},数据库列是 {columns[field]}"
        )


def test_overlong_sku_is_a_row_error_not_a_silent_truncation():
    """截断的后果是「导入成功」但 SKU 少了半截 —— 那条记录从此对不上任何东西。"""
    from app.services.product_import import parse_row

    data, errors = parse_row(
        {"spu": "S1", "sku": "X" * 200, "name": "泳衣"}, row_number=3
    )
    assert data is None
    assert any(e.field == "sku" and "64" in e.message for e in errors)


def test_too_many_secondary_colors_is_a_row_error():
    from app.services.product_import import parse_row

    data, errors = parse_row(
        {
            "spu": "S1",
            "sku": "SW-1",
            "name": "泳衣",
            "secondary_colors": "|".join(f"c{i}" for i in range(30)),
        },
        row_number=4,
    )
    assert data is None
    assert any(e.field == "secondary_colors" for e in errors)


def test_normal_row_still_parses():
    """收紧之后不能把正常数据也挡在外面。"""
    from app.services.product_import import parse_row

    data, errors = parse_row(
        {"spu": "SPU-1", "sku": "SW-001-BLK-S", "name": "黑色连体泳衣",
         "secondary_colors": "白|金"},
        row_number=1,
    )
    assert not errors
    assert data["sku"] == "SW-001-BLK-S"
    assert data["secondary_colors"] == ["白", "金"]


# ---------------------------------------------------------------- 评审 #8 提交超时


def test_submit_timeout_has_its_own_error_code():
    """POST 超时不等于没提交成功 —— 它必须和普通失败区分开。

    共用一个错误码的后果:界面上一个「重试」按钮就再买一次,
    而第一次那张图永远没人认领。
    """
    from app.core.enums import SUBMIT_UNKNOWN_CODE

    assert SUBMIT_UNKNOWN_CODE
    assert SUBMIT_UNKNOWN_CODE != "GENERATION_FAILED"


def test_retry_refuses_unknown_submissions_without_force():
    """静态检查:retry_task 必须先看错误码再决定放不放行。"""
    import ast

    from tests.pure._helpers import BACKEND_ROOT

    source = (BACKEND_ROOT / "app" / "services" / "generation_service.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "retry_task"
    )
    body = ast.unparse(fn)
    assert "SUBMIT_UNKNOWN_CODE" in body, "重试路径没有区分「提交结果未知」"
    assert "force" in body, "没有留出人工确认后强制重试的口子"


def test_timeout_attempt_is_not_recorded_as_a_plain_failure():
    """attempt 上要留下 TIMEOUT,事后对账时才分得清「没发出去」和「不知道」。"""
    import ast

    from tests.pure._helpers import BACKEND_ROOT

    source = (BACKEND_ROOT / "app" / "tasks" / "generation_tasks.py").read_text(
        encoding="utf-8"
    )
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.FunctionDef) and n.name == "_run"
    )
    body = ast.unparse(fn)
    assert "AttemptStatus.TIMEOUT if unknown else AttemptStatus.FAILED" in body


# ---------------------------------------------------------------- 评审 #12 素材角色


def test_only_garment_roles_can_be_used_as_the_product_image():
    """详情图、背景图、模特图都不能当商品图 —— 拿一张吊牌特写去试穿,
    Provider 会认认真真生成一个穿着吊牌的模特,而且钱已经花了。"""
    from app.core.enums import GARMENT_ROLES

    assert set(GARMENT_ROLES) == {"GARMENT_CUTOUT", "GARMENT_FRONT"}
    for forbidden in ("GARMENT_DETAIL", "BACKGROUND_REFERENCE", "MODEL_REFERENCE"):
        assert forbidden not in GARMENT_ROLES


def test_worker_has_no_first_asset_fallback():
    """兜底取 assets[0] 是这条问题的根源,不能留。"""
    from tests.pure._helpers import BACKEND_ROOT

    source = (BACKEND_ROOT / "app" / "tasks" / "generation_tasks.py").read_text(
        encoding="utf-8"
    )
    assert "assets[0] if assets else None" not in source
