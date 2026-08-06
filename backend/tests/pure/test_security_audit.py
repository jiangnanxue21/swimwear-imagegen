"""安全检查(需求第十九章 / 阶段 7)。

分两类:

    行为检查   SSRF 校验这类有逻辑的东西,直接调函数验
    静态扫描   "不许硬编码密钥""错误不许泄露数据库原文"这类,用 ast/正则扫全仓

静态扫描写成测试而不是一次性脚本,是因为安全属性会**退化**:
今天扫干净了,下个月有人为了调试写死一个 key,只有测试会拦住他。
"""
from __future__ import annotations

import ast
import re

from app.core.net_safety import UnsafeDownloadURL, check_download_url
from tests.pure._helpers import BACKEND_ROOT, PROJECT_ROOT, expect_raises

APP_DIR = BACKEND_ROOT / "app"


def _python_files():
    return [p for p in APP_DIR.rglob("*.py") if "__pycache__" not in str(p)]


# ---------------------------------------------------------------- SSRF

def test_only_http_and_https_are_allowed():
    for url in ("file:///etc/passwd", "ftp://example.com/x.jpg", "gopher://x"):
        expect_raises(UnsafeDownloadURL, check_download_url, url, resolve=False)


def test_cloud_metadata_endpoint_is_always_rejected():
    """169.254.169.254 是云环境里一把读到实例凭据的地址,没有任何正当用途。"""
    err = expect_raises(
        UnsafeDownloadURL, check_download_url, "http://169.254.169.254/latest/meta-data/"
    )
    assert "链路本地" in err.args[0]


def test_metadata_endpoint_stays_rejected_even_if_allowlisted():
    """允许清单不能用来放行元数据端点 —— 那等于给自己留一个后门开关。"""
    expect_raises(
        UnsafeDownloadURL,
        check_download_url,
        "http://169.254.169.254/x",
        allowed_hosts="169.254.169.254",
    )


def test_loopback_and_private_ranges_are_rejected_by_default():
    for url in (
        "http://127.0.0.1:8000/files/x.jpg",
        "http://10.0.0.5/x.jpg",
        "http://192.168.1.10/x.jpg",
        "http://172.16.0.9/x.jpg",
        "http://[::1]/x.jpg",
    ):
        expect_raises(UnsafeDownloadURL, check_download_url, url)


def test_private_address_is_allowed_when_explicitly_listed():
    """自建 ComfyUI 在内网是正当场景,但必须是一个显式决定。"""
    url = "http://10.0.0.5:8188/view?filename=out.png"
    expect_raises(UnsafeDownloadURL, check_download_url, url)
    assert check_download_url(url, allowed_hosts="10.0.0.5") == url


def test_allowlist_accepts_hostnames_too():
    url = "http://comfyui:8188/view?filename=out.png"
    assert check_download_url(url, allowed_hosts="comfyui", resolve=False) == url


def test_allowlist_parsing_is_forgiving():
    url = "http://comfyui:8188/x.png"
    for raw in ("comfyui", " comfyui ,other", "other;comfyui", "COMFYUI"):
        assert check_download_url(url, allowed_hosts=raw, resolve=False) == url


def test_public_cdn_url_passes():
    assert check_download_url(
        "https://cdn.fashn.ai/abc/output_0.png", resolve=False
    ).startswith("https://")


def test_empty_or_hostless_url_is_rejected():
    expect_raises(UnsafeDownloadURL, check_download_url, "", resolve=False)
    expect_raises(UnsafeDownloadURL, check_download_url, "http:///x.jpg", resolve=False)


def test_download_paths_actually_call_the_guard():
    """光有校验函数不算数,得确认下载路径真的**调用**了它。

    这条最初写成 `"check_download_url" in source`,变异测试当场证明它没用 ——
    把调用删掉、只留 import,那种写法照样绿。改成用 AST 找真实调用节点。
    """
    for relative in ("tasks/generation_tasks.py", "providers/fashn.py"):
        tree = ast.parse((APP_DIR / relative).read_text(encoding="utf-8"))
        called = any(
            isinstance(node, ast.Call)
            and (
                getattr(node.func, "id", None) == "check_download_url"
                or getattr(node.func, "attr", None) == "check_download_url"
            )
            for node in ast.walk(tree)
        )
        assert called, f"{relative} 没有实际调用 check_download_url"


# ------------------------------------------------------- DNS Rebinding

def test_resolve_pinned_returns_a_literal_ip_unchanged():
    from app.core.net_safety import resolve_pinned

    assert resolve_pinned("93.184.216.34") == "93.184.216.34"


def test_resolve_pinned_rejects_the_metadata_endpoint():
    from app.core.net_safety import UnsafeDownloadURL, resolve_pinned

    expect_raises(UnsafeDownloadURL, resolve_pinned, "169.254.169.254")
    # 加进允许清单也不行
    expect_raises(
        UnsafeDownloadURL, resolve_pinned, "169.254.169.254",
        allowed_hosts="169.254.169.254",
    )


def test_resolve_pinned_rejects_private_literals_unless_allowlisted():
    from app.core.net_safety import UnsafeDownloadURL, resolve_pinned

    expect_raises(UnsafeDownloadURL, resolve_pinned, "127.0.0.1")
    assert resolve_pinned("10.0.0.5", allowed_hosts="10.0.0.5") == "10.0.0.5"


def test_resolve_pinned_rejects_a_host_that_resolves_to_loopback():
    """localhost 一定解析到环回。这条不需要网络。"""
    from app.core.net_safety import UnsafeDownloadURL, resolve_pinned

    expect_raises(UnsafeDownloadURL, resolve_pinned, "localhost")


def test_download_paths_pin_the_connection_to_a_validated_ip():
    """只做请求前检查堵不住 DNS Rebinding —— 必须真的连到校验过的那个 IP。

    这条和上面 `test_download_paths_actually_call_the_guard` 是两件事:
    那条确认"有没有检查",这条确认"检查完之后连的是不是同一个地址"。
    两次 DNS 解析之间那道缝,只有把连接钉住才能关掉。
    """
    # 图片下载的实现在 M2 搬到了 app/llm/images.py(识别层与评分层共用)。
    # 断言跟着文件走,而不是放松要求 —— 共用之后这一处的漏洞会同时
    # 影响两层,钉得比原来更紧才对。
    for relative in ("tasks/generation_tasks.py", "llm/images.py"):
        tree = ast.parse((APP_DIR / relative).read_text(encoding="utf-8"))
        pinned = any(
            isinstance(node, ast.Call)
            and (
                getattr(node.func, "id", None) == "pinned_transport"
                or getattr(node.func, "attr", None) == "pinned_transport"
            )
            for node in ast.walk(tree)
        )
        assert pinned, f"{relative} 的下载没有把连接钉在已校验的 IP 上"


def test_no_http_client_is_built_without_a_pinned_transport():
    """新加一处 httpx client 却忘了钉 IP,SSRF 防护就只剩一半。

    只扫真正会去下载**外部输入指定的地址**的模块。Provider 客户端连的是
    我们自己配的 base_url,不在这条规则的射程内。
    """
    for relative in ("tasks/generation_tasks.py", "llm/images.py"):
        tree = ast.parse((APP_DIR / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (isinstance(node, ast.Call) and getattr(node.func, "attr", "") in
                    {"Client", "AsyncClient"}):
                continue
            keywords = {k.arg for k in node.keywords}
            assert "transport" in keywords, (
                f"{relative} 第 {node.lineno} 行构造了 httpx client 但没传 transport"
            )


# ---------------------------------------------------------------- 密钥

#: 看起来像"把密钥写死在代码里"的赋值
SECRET_NAMES = re.compile(
    r"\b(api_key|apikey|secret|password|token|access_key|private_key)\b", re.I
)


def test_no_hardcoded_secrets_in_source():
    """禁止硬编码 API Key(需求第十九章)。

    判定标准:名字像密钥的变量被赋了一个**非空字符串字面量**。
    空串是安全默认值,占位符也放行。
    """
    offenders: list[str] = []
    placeholders = {"", "todo", "changeme", "test-key", "k", "your-key-here"}

    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            targets = []
            if isinstance(node, ast.Assign):
                targets = node.targets
                value = node.value
            elif isinstance(node, ast.AnnAssign) and node.value is not None:
                targets = [node.target]
                value = node.value
            else:
                continue

            if not isinstance(value, ast.Constant) or not isinstance(value.value, str):
                continue
            literal = value.value.strip()
            if literal.lower() in placeholders:
                continue

            for target in targets:
                name = getattr(target, "id", None) or getattr(target, "attr", "")
                if name and SECRET_NAMES.search(name):
                    offenders.append(f"{path.relative_to(BACKEND_ROOT)}:{node.lineno} {name}")

    assert not offenders, "疑似硬编码密钥 -> " + "; ".join(offenders)


def test_env_example_has_no_real_looking_keys():
    """.env.example 里的值必须是空的或明显的占位符。"""
    text = (PROJECT_ROOT / ".env.example").read_text(encoding="utf-8")
    suspicious = []
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if SECRET_NAMES.search(key) and value.strip():
            suspicious.append(line)
    assert not suspicious, f".env.example 里疑似写了真实密钥: {suspicious}"


# ---------------------------------------------------------------- 错误泄露

def test_unhandled_errors_do_not_leak_internals():
    """兜底异常处理只能返回固定文案,不许把异常原文塞给客户端(需求第十九章)。"""
    source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    tree = ast.parse(source)

    handler = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "unhandled_handler":
            handler = node
    assert handler is not None, "找不到兜底异常处理器"

    body = ast.unparse(handler)
    for leak in ("str(exc)", "repr(exc)", "exc.args", "traceback"):
        assert leak not in body, f"兜底处理器可能泄露内部信息: {leak}"


def test_app_error_payload_only_exposes_code_and_message():
    """AppError.to_payload 是唯一会被返回给客户端的结构,detail 只进日志。"""
    from app.core.errors import AppError, ErrorCode

    err = AppError("出事了", detail={"sql": "SELECT * FROM users", "provider": "x"})
    payload = err.to_payload()
    blob = str(payload)
    assert "SELECT" not in blob, "detail 不该出现在返回体里"
    assert payload["error"]["code"] == str(ErrorCode.INTERNAL_ERROR)


# ---------------------------------------------------------------- 审计

def test_every_write_service_records_audit():
    """写操作必须记录审计日志(需求第十九章)。

    判定放在服务层而不是接口层:接口可能只是转发,真正改数据的是服务。
    """
    required = {
        "product_service.py",
        "asset_service.py",
        "generation_service.py",
        "model_template_service.py",
        "review_service.py",
        "output_service.py",
        "settings_service.py",
    }
    missing = []
    for name in sorted(required):
        source = (APP_DIR / "services" / name).read_text(encoding="utf-8")
        if "audit.record" not in source:
            missing.append(name)
    assert not missing, f"这些服务有写操作却没有审计: {missing}"


def test_import_service_is_parse_only():
    """product_import 不在审计清单里,是因为它只解析不落库 —— 把这个理由钉住。

    哪天有人在里面直接 session.add,这条会先红,提醒他补审计。
    """
    source = (APP_DIR / "services" / "product_import.py").read_text(encoding="utf-8")
    assert "session.add" not in source, "导入服务开始落库了,必须补审计并加进上面的清单"
    assert "Session" not in source, "导入服务不该拿到数据库会话"


def test_audit_payload_is_redacted_before_persisting():
    source = (APP_DIR / "services" / "audit.py").read_text(encoding="utf-8")
    assert "redact(" in source, "审计 payload 必须先脱敏再落库"


# ---------------------------------------------------------------- 上传与路径

def test_upload_limits_are_injected_not_hardcoded():
    """大小与尺寸下限必须由调用方传进来,而不是写死在校验函数里。

    写死意味着改配置得改代码;更糟的是测试会用另一套值,
    于是"配置生效了吗"这件事永远没被验证过。
    """
    source = (APP_DIR / "services" / "upload_validation.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    functions = [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]
    validators = [f for f in functions if "validate" in f.name]
    assert validators, "找不到上传校验函数"

    params = {arg.arg for f in validators for arg in f.args.args + f.args.kwonlyargs}
    assert "max_bytes" in params, f"大小上限必须是参数,实际参数: {sorted(params)}"


def test_upload_limits_come_from_settings_at_the_api_layer():
    """接口层必须把配置里的上限传下去,否则参数化等于白做。

    ## 这条断言自己改过一次(C-11)

    原来钉的是"`products.py` 的源码里出现 `MAX_UPLOAD_SIZE_MB` 这个词" ——
    而那个词只出现在**错误文案**里。C-11 把读取与判定抽到
    `deps.read_within_limit()` 之后,文案跟着走了,这条当场红,
    而上限依然来自配置、依然被强制执行。

    这是本仓第三次撞见"门禁钉的是写法不是不变量"(前两次:租约不变量被
    要求写成 `assert`、逐图计费被钉成一整串字面量)。现在钉两件事:
    调用方从 settings 取上限,执行方按它拒绝。
    """
    caller = (APP_DIR / "api" / "products.py").read_text(encoding="utf-8")
    assert "settings.max_upload_bytes" in caller, (
        "上传接口没有从配置取大小上限"
    )
    enforcer = (APP_DIR / "api" / "deps.py").read_text(encoding="utf-8")
    assert "MAX_UPLOAD_SIZE_MB" in enforcer and "FILE_TOO_LARGE" in enforcer, (
        "上限没有在任何地方被真正强制执行"
    )


def test_storage_writes_always_go_through_the_path_guard():
    """所有落盘都必须经过 resolve_within,杜绝路径穿越。"""
    source = (APP_DIR / "services" / "storage.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    local = next(
        n for n in ast.walk(tree)
        if isinstance(n, ast.ClassDef) and n.name == "LocalObjectStorage"
    )
    for method in ("save", "exists", "read"):
        func = next(n for n in local.body if isinstance(n, ast.FunctionDef) and n.name == method)
        assert "resolve_within" in ast.unparse(func), f"{method} 没有过路径校验"


# ---------------------------------------------------------------- 评审修复的不变量


def test_downloads_never_follow_redirects_blindly():
    """``follow_redirects=True`` + 只校验首个 URL = 完整的 SSRF 绕过。

    用 AST 找实参,不用字符串包含 —— 后者在注释里写一句就能骗过去。
    """
    for relative in ("tasks/generation_tasks.py", "providers/fashn.py"):
        tree = ast.parse((APP_DIR / relative).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "attr", None) or getattr(func, "id", None)
            if name not in {"Client", "AsyncClient"}:
                continue
            for keyword in node.keywords:
                if keyword.arg == "follow_redirects" and isinstance(keyword.value, ast.Constant):
                    if keyword.value.value is True and relative.startswith("tasks/"):
                        raise AssertionError(
                            f"{relative}: 候选图下载不得让 httpx 自动跟随重定向,"
                            "请走 net_safety.stream_checked"
                        )


def test_candidate_download_uses_the_per_hop_guard():
    source = (APP_DIR / "tasks" / "generation_tasks.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = any(
        isinstance(node, ast.Call)
        and (
            getattr(node.func, "id", None) == "stream_checked"
            or getattr(node.func, "attr", None) == "stream_checked"
        )
        for node in ast.walk(tree)
    )
    assert called, "下载路径没有实际调用 stream_checked"


def test_csv_export_escapes_formulas():
    source = (APP_DIR / "services" / "export_format.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    row_fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "row_for"
    )
    assert "escape_formula" in ast.unparse(row_fn), "导出行没有做公式转义"


def test_json_import_is_size_limited():
    """CSV 有上限而 JSON 没有,等于把限制留了一扇后门。"""
    source = (APP_DIR / "api" / "products.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "import_products"
    )
    body = ast.unparse(fn)
    assert "request.stream()" in body, "JSON 导入仍在一次性读取整个请求体"
    assert body.count("MAX_IMPORT_BYTES") >= 3, "JSON 路径没有复用 CSV 的大小上限"


def test_settings_write_endpoints_stay_guarded():
    source = (APP_DIR / "api" / "settings.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    for name in ("update_settings", "reset_settings"):
        fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == name)
        assert "require_admin" in ast.unparse(fn)


# ---------------------------------------------------------------- 主密钥不得对外

def test_master_key_does_not_live_in_the_public_storage_directory():
    """加密落库防的是数据库泄露;主密钥若跟着 /files 走 HTTP 出去,这层防护就是零。

    这是最安静的一类回归:功能全对,测试全绿,只是密钥可以被 curl 下来。
    """
    source = (APP_DIR / "core" / "secrets.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "_key_file")
    body = ast.unparse(fn)
    assert "secrets_dir" in body, "主密钥没有放进独立的密钥目录"
    assert "storage_dir" not in body, "主密钥又回到了对外托管的存储目录里"


def test_default_key_directory_is_outside_the_storage_directory():
    """默认值本身必须是安全的 —— 靠部署时记得改的安全等于没有。"""
    source = (APP_DIR / "core" / "config.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "secrets_dir"
    )
    body = ast.unparse(fn)
    assert "STORAGE_LOCAL_DIR" not in body, "密钥目录的默认值是从存储目录推导的"
    assert "PROJECT_ROOT" in body, "密钥目录的默认值没有落在项目根下"
    assert "secrets_dir_is_exposed" in source, "缺少启动时的密钥目录复查"


def test_static_mount_refuses_hidden_files():
    """存储目录是公开目录,但公开不该包括某天被谁放进去的点文件。"""
    source = (APP_DIR / "main.py").read_text(encoding="utf-8")
    assert "class SafeStaticFiles" in source, "静态挂载没有做隐藏文件防护"
    assert "is_hidden_path" in source, "隐藏文件判定没有走 paths 里的纯函数"
    tree = ast.parse(source)
    mounts = [
        ast.unparse(n)
        for n in ast.walk(tree)
        if isinstance(n, ast.Call) and "mount" in ast.unparse(n.func)
    ]
    assert mounts, "找不到静态挂载"
    for call in mounts:
        assert "StaticFiles(" not in call.replace("SafeStaticFiles(", ""), (
            f"挂载用的是裸 StaticFiles: {call}"
        )


# ---------------------------------------------------------------- 口令覆盖面

def test_settings_read_endpoint_is_guarded_too():
    """读接口会吐出每把密钥的末四位和内网下载白名单,同样不该开放。"""
    source = (APP_DIR / "api" / "settings.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "read_settings"
    )
    assert "require_admin" in ast.unparse(fn), "read_settings 没有守卫"


def test_provider_connection_test_is_guarded():
    """它会拿真 Key 去打厂商接口 —— 不设门槛就是把别人的额度开放给全网。"""
    source = (APP_DIR / "api" / "providers.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    fn = next(
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.AsyncFunctionDef) and n.name == "test_provider"
    )
    assert "require_admin" in ast.unparse(fn), "test_provider 没有守卫"


def test_local_bypass_does_not_cover_a_test_environment():
    """local/dev 只可能是某人的开发机;\"测试环境\"通常是连着真 Key 的真机器。"""
    # deps.py 依赖 fastapi,纯测试环境里装不了,所以静态取这份常量
    source = (APP_DIR / "api" / "deps.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    node = next(
        n.value
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        and any(getattr(t, "id", "") == "LOCAL_ENVS" for t in n.targets)
    )
    envs = {c.value for c in ast.walk(node) if isinstance(c, ast.Constant)}
    assert "test" not in envs, "APP_ENV=test 仍然可以免口令改配置"
    assert "local" in envs, "本机开发被误伤了"


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _fn(tree, name: str):
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


# 以下来自已删除的 test_code_review_round3.py(第三轮评审的阶段性容器文件)。
# 按被测模块归位,而不是按评审轮次分组 —— 否则同一个模块的测试会散落在多个文件里。

# ---------------------------------------------------------------- 16 白名单


def test_the_public_whitelist_matches_whole_path_segments():
    """评审第 16 条:`startswith` 没有边界。

    这类问题的特征是加接口的人不会发现 —— 他新加的路由看起来工作正常,
    只是恰好不需要口令。
    """
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "_main_public_path", APP_DIR / "main.py"
    )
    # 直接 import main 会拉起 fastapi 依赖链,这里只把那一个纯函数抠出来跑
    tree = _tree(APP_DIR / "main.py")
    fn = _fn(tree, "is_public_api_path")
    namespace: dict = {"PUBLIC_PREFIXES": ("/health", "/media-files")}
    exec(compile(ast.Module(body=[fn], type_ignores=[]), "<main>", "exec"), namespace)
    is_public = namespace["is_public_api_path"]

    assert is_public("/api/health", "/api")
    assert is_public("/api/health/live", "/api")
    assert is_public("/api/media-files/abc.jpg", "/api")
    for sneaky in (
        "/api/health-admin",
        "/api/healthcheck-internal",
        "/api/media-files-export",
        "/api/media",
    ):
        assert not is_public(sneaky, "/api"), f"{sneaky} 被意外放行"
    assert spec is not None
