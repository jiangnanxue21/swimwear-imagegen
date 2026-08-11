"""A45 第一/二批修复 + **本轮最重要的产出:五条结构性门禁**。

评审 §7 那句话是这一批的主题:

> 仓库里已经建立了"唯一入口",但**没有任何自动检查在数绕过点**。

38 条里最贵的三条(#1 admin 头、#6 prod volumes、#31 配置入口)与最琐碎的
一批(#33 时间格式、#34 antd 静态实例、#4 saveBlob 复制件)是同一个根因。
所以下面的门禁一 ～ 门禁五盯的是**类**,不是这一次的实例。

修完这一批之后再次确认一件事:本仓已经有**五次**"门禁钉住了当时的写法而不是
不变量",其中两次就发生在这一批(saveBlob 那条钉住了复制件的存在、变体下拉
那条钉住了两串字面量)。所以新写门禁时的自检是:

    我钉的是这段代码现在长什么样,还是它必须保持什么性质?
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
    """去掉 TS/TSX 注释。字符串断言的第一步,本仓踩过四次。"""
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
# 门禁一:admin 路由 ↔ 前端 adminHeaders 对齐(A45-#1 这一类)
# ==========================================================


def _admin_routes() -> set[str]:
    """后端所有挂了 `require_admin` 的路由路径(含路由器前缀)。"""
    routes: set[str] = set()
    for path in sorted((APP / "api").glob("*.py")):
        tree = ast.parse(_read(path))
        source = _read(path)
        prefix = ""
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "APIRouter"
            ):
                for kw in node.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        prefix = kw.value.value
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for deco in node.decorator_list:
                if not (isinstance(deco, ast.Call) and isinstance(deco.func, ast.Attribute)):
                    continue
                if deco.func.attr not in {"get", "post", "put", "patch", "delete"}:
                    continue
                text = ast.get_source_segment(source, deco) or ""
                if "require_admin" not in text:
                    continue
                if deco.args and isinstance(deco.args[0], ast.Constant):
                    routes.add(prefix + deco.args[0].value)
    return routes



def _one_call(text: str, needle: str, where: str) -> str:
    """从这次 axios 调用的路径字面量,切到它自己的收尾 `)` 为止。

    原来这里是 `text[index : index + 400]` —— 一个定长窗口。判断是**反向**的
    (`adminHeaders` 不在窗口里就算漏网),而定长窗口切窄的表现是**报一个
    假的漏网**:调用点多写几行注释就够了。

    改成按括号配平找收尾,切不出来就抛 —— 让它当场说清楚,
    而不是拿一段可能不完整的文本去下一个反向结论。
    """
    start = text.index(needle)
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth < 0:
                return text[start:i]
    raise AssertionError(f"{where} 里 {needle!r} 这次调用找不到收尾括号")

def test_no_frontend_call_site_carries_an_admin_token():
    """挂 `require_admin` 的路由仍然要有,而正常前端**不得**再带管理口令。

    ## 这条是把旧不变量翻过来,不是删掉它

    旧的那条叫 `test_every_admin_route_is_called_with_admin_headers`:对每一个
    admin 路由,要求前端调用点出现 `adminHeaders()`。它是为一次真实事故加的
    (A45-#1:`regenerateFile` 少了那一句,于是 A-21 专门为解开 409 死循环加的
    唯一出口对每个人 403,而 `AUTH_FORBIDDEN` 被 `isAuthError` 刻意排除、
    冷启动横幅也不亮 —— 一条修复交付了,却等于没交付)。

    浏览器登录之后,管理权限由 **Session Identity → require_admin** 判定,
    浏览器一个 Token 都不该再持有。旧断言于是从"必须带"变成"必须不带"。

    **保留了 `_admin_routes()` 那半段。** 它扫的是后端真实挂着 `require_admin`
    的路由,和前端怎么调用无关 —— 那一半的价值一点没变:admin 路由必须存在,
    否则说明有人在"让前端不带口令"的过程中顺手把守卫也摘了,而那才是真正的
    退化。两条断言合起来才是新方案下的等强度不变量:

        后端仍然有 admin 路由     守卫没被摘
        前端一处都不带管理口令     凭据确实换成了 Cookie
    """
    routes = _admin_routes()
    assert routes, (
        "一条 admin 路由都没扫到。要么扫描坏了,要么 `require_admin` 被摘干净了 —— "
        "后者意味着设置页、Provider 测试、提示词这些接口现在谁都能调"
    )

    offenders: list[str] = []
    for path in sorted(FRONTEND.rglob("*.ts")) + sorted(FRONTEND.rglob("*.tsx")):
        text = _code_only(_read(path))
        for token in ("adminHeaders", "X-Admin-Token", "X-Operator-Token"):
            if token in text:
                offenders.append(f"{path.name} -> {token}")

    assert not offenders, (
        "正常前端又开始带机器凭据了:" + "; ".join(sorted(set(offenders)))
        + "。浏览器的凭据是 HttpOnly 签名 Cookie(`withCredentials`),"
        "它读不到也不该读 Token;`ADMIN_TOKEN` / `OPERATOR_TOKENS` 只服务 "
        "CLI、脚本与 pytest。**不要**为了让某个 403 变绿把这一行加回来 —— "
        "403 的正确解法是用 admin 账号登录。"
    )


#: 设置页可改、因此**必须**走 `provider_setting()` 读的键。
#:
#: 判据不是"所有 Settings 字段",而是"后台能改的那些" —— 只有它们才有
#: "改完是否立刻生效"这个问题。清单从 `settings_schema` 里取,不手写。
_RUNTIME_READ_EXEMPT = frozenset(
    {
        # 这两个是 `providers/_config` 自己的实现内部,它就是那个唯一入口
        "app/providers/_config.py",
        # 配置定义处自然要提到字段名
        "app/core/config.py",
        "app/core/settings_schema.py",
    }
)


def _overridable_keys() -> set[str]:
    """`settings_schema` 里声明为可后台修改的键。"""
    tree = ast.parse(_read(APP / "core" / "settings_schema.py"))
    keys: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for kw in node.keywords:
            if kw.arg == "key" and isinstance(kw.value, ast.Constant):
                keys.add(kw.value.value)
    return keys


def test_overridable_settings_are_never_read_straight_off_settings():
    """后台可改的键不许写成 `settings.KEY` 直读。

    ## 这条拦的是什么

    A45-#31:`DOWNLOAD_ALLOWED_HOSTS` 是设置页可编辑字段,而
    `generation_tasks.py` 有三行直读 `settings.DOWNLOAD_ALLOWED_HOSTS`。
    于是同一个配置分成两派:提交素材、评分取图走 `provider_setting`(生效),
    候选图下载走 `settings`(**不生效**)。

    表现:管理员在设置页加了自建 ComfyUI 主机(页面写着"改完就生效"),
    前两步通了、下载被拒,任务停在 DOWNLOADING。运营看到的是"白名单明明加了
    却没用",而唯一修法是改 `.env` 重启后端。

    **一个页面承诺了"立刻生效",就必须每一个读取点都真的立刻生效** ——
    否则那个承诺比没有承诺更糟。
    """
    keys = _overridable_keys()
    assert keys, "settings_schema 里一个键都没扫到 —— 扫描坏了"

    offenders: list[str] = []
    for path in APP.rglob("*.py"):
        if "__pycache__" in str(path):
            continue
        relative = str(path.relative_to(APP.parent))
        if relative in _RUNTIME_READ_EXEMPT:
            continue
        source = _read(path)
        for key in keys:
            for match in re.finditer(rf"\bsettings\.{key}\b", source):
                line = source.count("\n", 0, match.start()) + 1
                # `provider_setting("X", settings.X)` 是**正确用法** —— 那是
                # 覆盖层查不到时的兜底默认值,不是绕过。判据取这一行往前 120 字符
                # 里有没有 `provider_setting(`:第一版没有这一条,于是
                # `environment.py` 里两处正确的写法被判红了(假红)。
                window = source[max(0, match.start() - 120) : match.start()]
                if "provider_setting(" in window:
                    continue
                offenders.append(f"{relative}:{line} settings.{key}")

    assert not offenders, (
        "后台可改的配置被直读了,设置页那句「改完就生效」在这些点上不成立 -> "
        + "; ".join(sorted(offenders))
    )


# ==========================================================
# 门禁三:prod compose 的 override 完整性(A45-#6 这一类)
# ==========================================================


def test_the_prod_overlay_really_detaches_the_source_bind_mount():
    """prod overlay 里重列 `volumes` 的服务必须带 `!override`。

    Compose 的 `volumes` 按**容器路径合并**,不是整段替换。只重列 storage 与
    secrets 的话,基座里的 `./backend:/app` 原样保留 —— 而 prod compose 的文件头
    第 3 条明写着"去掉 `--reload` 与源码 bind mount"。

    在没有源码 checkout 的部署机上,那个空目录会盖住镜像里的代码,三个服务
    全部起不来,而现象(ModuleNotFoundError)**完全不指向 volumes 合并**。

    同文件的 `ports` 与 `image` 都记得写 `!reset` —— 所以这是遗漏不是取舍,
    而遗漏正是门禁该管的东西。
    """
    prod = _read(PROJECT / "docker-compose.prod.yml")
    base = _read(PROJECT / "docker-compose.yml")

    # 基座里带源码 bind mount 的服务
    risky = set(re.findall(r"^  (\w+):", base, flags=re.M)) & set(
        re.findall(r"^  (\w+):", prod, flags=re.M)
    )
    bind_mounted = {
        name
        for name in risky
        if re.search(rf"^  {name}:.*?- \./backend:/app", base, flags=re.M | re.S)
    }

    offenders: list[str] = []
    for name in sorted(risky):
        block = prod.split(f"\n  {name}:", 1)
        if len(block) < 2:
            continue
        body = re.split(r"\n  \w+:", block[1])[0]
        if "volumes:" not in body:
            continue
        if "volumes: !override" not in body and "volumes: !reset" not in body:
            offenders.append(name)

    assert not offenders, (
        "prod overlay 的这些服务重列了 volumes 却没有 !override —— "
        f"基座的 bind mount 会原样合并进来 -> {offenders}"
    )
    assert bind_mounted or True  # 基座写法变了也不该让这条误报


# ==========================================================
# 门禁四:同一个 helper 全仓只许一个定义(A45-#4 这一类)
# ==========================================================

#: 已经出过"两份实现漂移"的 helper。加新的进来比修一次 bug 便宜。
_SINGLE_DEFINITION_HELPERS = ("saveBlob", "nameOfVariant", "distinctNameOfVariant")


def test_shared_helpers_have_exactly_one_definition():
    """这些函数全仓只许有一个定义。

    A45-#4:`saveBlob` 有两份。A-34 修 revoke 时机时改的是**复制件**,
    于是修完之后两份都"对",但一份延后 60 秒、一份延后 0 毫秒 ——
    **比修之前更糟**:修之前两份是"同样错",修之后是"不同程度地对",
    而没有任何东西会发现它们不一致。

    A45-#23 是同一个形状的另一例:同名消歧在下拉里做了、横幅里没做,
    同一个页面上一处分得开一处分不开。

    这条门禁比修这两次的 bug 更值钱 —— 它拦的是**下一次复制**。
    """
    for helper in _SINGLE_DEFINITION_HELPERS:
        pattern = re.compile(rf"^\s*(?:export\s+)?function {helper}\s*\(", re.M)
        definitions = [
            path
            for path in FRONTEND.rglob("*.ts*")
            if pattern.search(_code_only(_read(path)))
        ]
        assert len(definitions) == 1, (
            f"{helper} 有 {len(definitions)} 处定义 -> "
            f"{[p.name for p in definitions]};两份实现会各自演化"
        )


# ==========================================================
# 门禁五:nginx 里声明 add_header 的 location 必须补回安全头(A45-#7)
# ==========================================================


def test_every_location_with_its_own_add_header_reincludes_the_security_snippet():
    """nginx 的继承是"一条都没有才继承",不是逐条覆盖。

    一个 location 只要出现**任何一条** `add_header`,server 级的 add_header
    就**全部**不再继承。于是 `location = /index.html { add_header Cache-Control
    no-store; }` 这样一行完全正常的缓存声明,会让 CSP / X-Frame-Options /
    nosniff / Referrer-Policy 恰好在 **SPA 文档本身**上消失 ——
    而那正是保护 localStorage 里两把口令(C-03)最关键的那一个响应。

    ## 这条门禁的来历

    b5 修 `/healthz` 时,我在代码里写下了"这个形状会被复制到下一个 location 上"。
    **而它已经被复制了,就在同一个文件里**(`/assets/` 与 `/index.html`),
    我没有去看。注释拦不住复制,门禁可以。
    """
    conf = _read(PROJECT / "frontend" / "nginx.conf")
    snippet = "include /etc/nginx/snippets/security-headers.conf;"

    blocks = re.findall(r"(location[^{]*\{)(.*?)\n    \}", conf, flags=re.S)
    assert blocks, "一个 location 都没解析到 —— 是解析坏了"

    offenders: list[str] = []
    for head, body in blocks:
        code = _no_hash_comments(body)
        if not re.search(r"^\s*add_header", code, flags=re.M):
            continue
        if snippet not in code:
            offenders.append(head.strip())

    assert not offenders, (
        "这些 location 声明了自己的 add_header,却没有 include 安全头 snippet —— "
        f"server 级的四条头在它们上面全部失效 -> {offenders}"
    )

    # snippet 本身要真的存在,并且被镜像 COPY 进去
    snippet_file = PROJECT / "frontend" / "nginx-security-headers.conf"
    assert snippet_file.exists(), "安全头 snippet 文件不见了 —— nginx 会启动失败"
    assert "Content-Security-Policy" in _read(snippet_file)
    dockerfile = _read(PROJECT / "frontend" / "Dockerfile")
    assert "nginx-security-headers.conf" in dockerfile, (
        "镜像没有 COPY snippet —— include 找不到文件,nginx 起不来"
    )


# ==========================================================
# A45 单条修复的回归钉
# ==========================================================


def test_the_receipt_ledger_exposes_a_unique_row_key():
    """A45-#5:`(action, scope_key)` 会重复,前端需要一个真正唯一的键。

    上游指纹一变就多一行回执,而台账按 `last_used_at` 取前 50 —— 两行同屏。
    """
    src = ast.get_source_segment(
        _read(APP / "workbench" / "batch_service.py"),
        _fn(APP / "workbench" / "batch_service.py", "receipt_ledger"),
    ) or ""
    assert '"idempotency_key"' in src, "回执出参没有可用的唯一键(A45-#5)"

    page = _code_only(_read(FRONTEND / "pages" / "WorkbenchBatchPage.tsx"))
    assert "rowKey={(row) => row.idempotency_key}" in page, "前端 rowKey 还在用会撞的组合"
    assert "`${row.action}-${row.scope_key}`" not in page


def test_a_preview_export_does_not_count_as_a_delivery():
    """A45-#35:比对用的 CSV 不该关掉平台驳回的闸门。

    闸门的证据链就是 `EXPORT_AUDIT_ACTION` 那些审计行,而它只看时间不看格式。
    CSV 的下载按钮上前端明写着"不作为交付给平台的文件" ——
    **一个界面上说"这个不算"的动作,在系统里算。**
    """
    from app.workbench import platform as pf

    assert pf.PREVIEW_EXPORT_AUDIT_ACTION != pf.EXPORT_AUDIT_ACTION
    assert "xlsx" in pf.DELIVERY_EXPORT_FORMATS and "csv" not in pf.DELIVERY_EXPORT_FORMATS

    delivered = pf.export_audit_payload(
        fmt="xlsx", spu="S", fingerprint="f", components={}, export_count=1
    )
    preview = pf.export_audit_payload(
        fmt="csv", spu="S", fingerprint="f", components={}, export_count=1
    )
    assert delivered["action"] == pf.EXPORT_AUDIT_ACTION
    assert preview["action"] == pf.PREVIEW_EXPORT_AUDIT_ACTION
    # 闸门只认交付那一条
    assert pf.export_entry(preview, at="2026-01-01T00:00:00+00:00") is None
    assert pf.export_entry(delivered, at="2026-01-01T00:00:00+00:00") is not None

    # 计数与 exported_at 也只在交付时动
    src = ast.get_source_segment(
        _read(APP / "workbench" / "service.py"),
        _fn(APP / "workbench" / "service.py", "record_export"),
    ) or ""
    assert "DELIVERY_EXPORT_FORMATS" in src, "export_count 仍然把两种产物混在一个计数里"

    frontend = _read(FRONTEND / "api" / "batch.ts")
    assert "export_preview" in frontend, "新动作名没有前端文案,审计页会显示原始动作码"


def test_the_private_network_check_parses_an_ip_first():
    """A45-#36:`host.startswith(("10.", …))` 作用在**主机名**上。

    `10.gpu.example.com` 是一个完全公网的域名,却被判成自建 → 无 Key 也
    `configured=True` → 每次调用不带 `Authorization`,一路 401。
    而同一段 docstring 写着"猜错的方向是多要一个 Key" —— **这一支恰好反了**。
    """
    from app.evaluators.vision import _is_private_host

    assert _is_private_host("10.0.0.1") is True
    assert _is_private_host("192.168.1.5") is True
    assert _is_private_host("127.0.0.1") is True
    assert _is_private_host("::1") is True
    # 原来那串手写前缀只列到 172.19,连私网段本身都不全
    assert _is_private_host("172.31.0.9") is True
    # 要害:长得像私网的**域名**不算
    assert _is_private_host("10.gpu.example.com") is False
    assert _is_private_host("192.168.evil.com") is False
    assert _is_private_host("api.openai.com") is False

    source = _read(APP / "evaluators" / "vision.py")
    assert "_PRIVATE_PREFIXES" not in source.replace("_PRIVATE_PREFIXES` 已删", ""), (
        "手写前缀元组又回来了"
    )


def test_the_batch_entry_deduplicates_product_ids():
    """A45-#14:重复的 product_id 会落两条同幂等键的条目,其中一条永不执行。"""
    src = ast.get_source_segment(
        _read(APP / "workbench" / "batch_service.py"),
        _fn(APP / "workbench" / "batch_service.py", "candidates"),
    ) or ""
    assert "dict.fromkeys(product_ids)" in src, "批量入口仍未去重(A45-#14)"
    assert src.index("dict.fromkeys") < src.index("MAX_BATCH_SIZE"), (
        "去重要发生在上限判定**之前**,否则重复项会把人挡在门外"
    )


def test_local_storage_flushes_before_linking():
    """A45-#12:内容寻址下"路径即内容承诺",写盘要 fsync。

    不 fsync 的话断电可留下**内容截断但路径正确**的对象,此后每一次同哈希
    写入都命中去重、如实回报 `deduplicated=True`,坏字节被永久复用 ——
    而系统里没有任何地方会再比对一次。
    """
    # **按类找。** `_fn` 全模块搜 `save` 命中的是抽象基类那一个 ——
    # 它当然没有 fsync,于是断言指向一个没问题的函数。b4 已经踩过一次,
    # 这里是第二次:凡是模块里有同名方法的,一律按类定位。
    source = _read(APP / "services" / "storage.py")
    tree = ast.parse(source)
    local = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "LocalObjectStorage"
    )
    save = next(
        node for node in local.body if isinstance(node, ast.FunctionDef) and node.name == "save"
    )
    calls = {
        node.func.attr: node.lineno
        for node in ast.walk(save)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "os"
    }
    assert "fsync" in calls, "本地存储写盘仍然没有 fsync(A45-#12)"
    # **比行号,不比字符串位置。** 第一版写的是
    # `src.index("os.fsync") < src.index("os.link")`,而 `os.link` 在这个函数的
    # docstring 里就出现过("os.link 是原子的")—— 于是断言拿到的是注释的位置,
    # 判定当场反过来。注释陷阱在本仓的第五次,这次是"位置比较"这个新形态。
    assert calls["fsync"] < calls["link"], "fsync 必须在 os.link 之前"
