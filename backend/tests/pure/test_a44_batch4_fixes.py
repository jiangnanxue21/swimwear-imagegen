"""A44 第四批:十条 P1/P2/P3 毛边。

    A-14  S3 delete 把一切异常吞成"没删掉"
    A-24  批量导出 409 不点名是哪几件
    A-27  drift() 把带消歧后缀的 key 恒判成"改过名"
    A-28  refs_of() 的 docstring 承诺 drift() 会报的那一类,drift() 不报
    A-29  后台 refetch 静默清空未保存的编排
    A-33  前端 30s 超时 vs nginx 320s
    A-38  reject() 不清批准列,一版图既"已批准"又"已退回"
    A-39  自动降级只写日志不进审计
    A-40  两处重置都漏清 target_step
    A-41  should_renew_lease 的默认值写死成字面量

能跑真逻辑的就跑真逻辑(A-27 / A-28 / A-41 走的是纯函数),跑不了的钉 AST。
字符串断言一律先过 `_code_only` 剥注释 —— 前三批在这上面各栽过一次。
"""
from __future__ import annotations

import ast
import re

from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"
FRONTEND = BACKEND_ROOT.parent / "frontend" / "src"


def _read(path) -> str:
    return path.read_text(encoding="utf-8")


def _code_only(source: str) -> str:
    without_block = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
    return re.sub(r"(?<!:)//[^\n]*", "", without_block)


def _fn(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"找不到函数 {name}")


def _calls(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Call):
            if isinstance(sub.func, ast.Name):
                out.add(sub.func.id)
            elif isinstance(sub.func, ast.Attribute):
                out.add(sub.func.attr)
    return out


# ==========================================================
# A-27 / A-28:变体漂移诊断(纯函数,跑真逻辑)
# ==========================================================


def _row(sku: str, color: str | None, key: str | None):
    from app.listings.variant_key import VariantRow

    return VariantRow(sku=sku, color=color, key=key)


def test_a_disambiguated_key_is_not_reported_as_renamed():
    """A-27:`Red~2` 配色 `Red` 不是"改过名"。

    改之前的判据是 `normalize(color) != normalize(key)`,而消歧后缀是
    **铸造时**加的、和改名毫无关系 —— 于是这一行每次诊断都出现在
    renamed 里。诊断噪声的代价不是"多一行",是真正改过名的那几行被淹掉。
    """
    from app.listings import variant_key

    out = variant_key.drift([_row("SW-1", "Red", "Red~2")])
    assert out["renamed"] == [], f"带消歧后缀的 key 被判成改名了:{out['renamed']}"


def test_a_truncated_key_is_not_reported_as_renamed():
    """列宽截断同理:key 是颜色名的前缀,不是另一个名字。

    判据用 `key_matches_color()` 把 key 照铸造规则重算一遍,所以截断
    自动对上 —— 而不是在判定里再写一遍"允许前缀相等"。
    """
    from app.listings import variant_key

    long_color = "深" * (variant_key.MAX_KEY_LENGTH + 10)
    key = variant_key._fit(long_color)
    assert len(key) == variant_key.MAX_KEY_LENGTH, "铸造规则变了,这条断言要跟着改"
    assert variant_key.drift([_row("SW-2", long_color, key)])["renamed"] == []


def test_a_real_rename_is_still_reported():
    """放宽之后不能把真改名一起放过去 —— 否则这条修复是把诊断关掉。"""
    from app.listings import variant_key

    out = variant_key.drift([_row("SW-3", "Crimson", "Red~2")])
    assert out["renamed"] == ["Red~2"], f"真改名没报出来:{out}"


def test_an_empty_colour_is_not_a_rename():
    """颜色为空是"还不知道",不是"改过名"。与 mint_key 的立场一致。"""
    from app.listings import variant_key

    assert variant_key.drift([_row("SW-4", "", "SW-4")])["renamed"] == []


def test_the_same_key_with_two_colours_is_reported():
    """A-28:`refs_of()` 的承诺现在是真的。

    它的 docstring 写着"同 key 不同色由 `drift()` 单独报,不在这里悄悄
    挑一个",而 drift() 那时报三类、一类都不是它。于是"这里挑第一个没关系"
    这个理由不成立,读代码的人还看不出来。
    """
    from app.listings import variant_key

    out = variant_key.drift([_row("SW-5", "Red", "Red"), _row("SW-6", "Blue", "Red")])
    assert out["key_label_conflicts"] == ["Red"], (
        f"同 key 两个颜色没有报出来:{out}"
    )


def test_refs_of_still_promises_only_what_drift_delivers():
    """docstring 里点名的那个键必须真的在 drift() 的返回里。

    **退化成什么样:** 谁改了键名(比如叫成 `key_colour_conflicts`)而没动
    注释 —— 承诺又变成假的,而没有任何测试会红。
    """
    from app.listings import variant_key

    doc = variant_key.refs_of.__doc__ or ""
    promised = re.findall(r"`drift\(\)` 的 `(\w+)`", doc)
    assert promised, "refs_of 的注释不再点名 drift 的哪个键了"
    delivered = variant_key.drift([_row("SW-7", "Red", "Red")])
    for key in promised:
        assert key in delivered, f"注释承诺了 `{key}`,而 drift() 不返回它"


# ==========================================================
# A-41:租约续期的默认值
# ==========================================================


def test_the_renew_default_is_the_lease_constant_not_a_literal():
    """A-41:默认值必须引用 `ITEM_LEASE_SECONDS`,不许写字面量。

    **退化成什么样:** 有人把租约时长从 1800 调到 600,改了常量、漏了这里。
    续租判据按 1800 算(剩余 900s 才续),而租约只有 600s —— 于是永远
    "还不该续",租约到期被回收,那件事**两边都不报错**。
    """
    from app.workbench import batch as rules

    tree = ast.parse(_read(APP / "workbench" / "batch.py"))
    fn = _fn(tree, "should_renew_lease")
    default = fn.args.defaults[-1] if fn.args.defaults else fn.args.kw_defaults[-1]
    assert isinstance(default, ast.Name) and default.id == "ITEM_LEASE_SECONDS", (
        "should_renew_lease 的默认租约时长又写成字面量了(A-41)"
    )

    # 顺带钉住行为:半程之前不续、之后要续
    now = 1_000_000.0
    half = rules.ITEM_LEASE_SECONDS * rules.LEASE_RENEW_RATIO
    assert not rules.should_renew_lease(lease_until=now + half + 1, now=now)
    assert rules.should_renew_lease(lease_until=now + half - 1, now=now)
    assert rules.should_renew_lease(lease_until=None, now=now)


# ==========================================================
# A-14:存储删除不许吞异常
# ==========================================================


def test_s3_delete_only_swallows_the_not_found_codes():
    """A-14:只有"确实不在"算 False,其余一律抛。

    改之前是 `except ClientError: return False` —— 403(凭据过期)、限流、
    5xx 全被伪装成"没删掉"。调用方据此认为处理完了,于是一次凭据故障的
    表现是**清理任务一直在跑、什么都没清掉、也没有一条错误日志**。

    判据抄的是同文件上方的 `exists()`,这条断言把两处钉在一起。
    """
    tree = ast.parse(_read(APP / "services" / "storage.py"))

    # **必须按类找。** 第一版用了全模块的 `_fn`,而 `LocalObjectStorage.delete`
    # 排在前面 —— 断言拿到的是本地实现,它本来就没有 ClientError,于是
    # 报"delete 里没有异常处理了"。假红,而且指向的是一个没问题的函数。
    def _method(class_name: str, fn_name: str) -> ast.FunctionDef:
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef) and node.name == class_name:
                for sub in node.body:
                    if isinstance(sub, ast.FunctionDef) and sub.name == fn_name:
                        return sub
        raise AssertionError(f"找不到 {class_name}.{fn_name}")

    def _not_found_codes(fn_name: str) -> set[str]:
        codes: set[str] = set()
        for node in ast.walk(_method("S3ObjectStorage", fn_name)):
            if isinstance(node, ast.Set):
                codes |= {
                    e.value for e in node.elts if isinstance(e, ast.Constant)
                }
        return codes

    delete_fn = _method("S3ObjectStorage", "delete")
    handlers = [n for n in ast.walk(delete_fn) if isinstance(n, ast.ExceptHandler)]
    assert handlers, "delete 里没有异常处理了"
    reraises = [
        n
        for n in ast.walk(delete_fn)
        if isinstance(n, ast.Raise) and n.exc is None
    ]
    assert reraises, "delete 又把所有 ClientError 吞掉了(A-14)"
    assert _not_found_codes("delete") == _not_found_codes("exists"), (
        "delete 与 exists 对「不存在」的判据不一致 —— 两处各写一份迟早分叉"
    )


# ==========================================================
# A-24:整批停下时要点名
# ==========================================================


def test_the_batch_export_names_every_blocked_product():
    """A-24:导出闸拦住时,报错要说是哪几件、各自卡在什么上。

    改之前 `export_gate` 的异常直接穿出去:整包 409,detail 里只有第一件
    的原因、**不带 SPU/SKU**。而条目页同时还是一排绿色 SUCCEEDED
    (导出条目确实成功了,过期的是之后才检查的草稿)—— 运营看到的是
    "全绿 + 一句没有主语的报错",只能一件件点开找。

    闸门没有松:仍然是任何一件不合格就整批拒绝(§4.5.1)。
    """
    tree = ast.parse(_read(APP / "workbench" / "batch_service.py"))
    fn = _fn(tree, "batch_file")

    guarded = [
        n
        for n in ast.walk(fn)
        if isinstance(n, ast.Try)
        and "export_gate" in _calls(n.body[0] if n.body else n)
    ]
    assert guarded, "export_gate 又是裸调用了 —— 第一件不合格就整批抛,不点名(A-24)"

    caught: set[str] = set()
    for handler in guarded[0].handlers:
        for node in ast.walk(handler.type) if handler.type else []:
            if isinstance(node, ast.Name):
                caught.add(node.id)
    assert {"ValidationError", "NotFoundError"} <= caught, (
        f"只接住了 {sorted(caught)} —— 缺草稿(NotFoundError)那一支会绕过点名"
    )

    source = ast.get_source_segment(_read(APP / "workbench" / "batch_service.py"), fn) or ""
    assert '"blocked"' in source, "报错的 detail 里没有 blocked 名单"
    assert '"spu"' in source and '"sku"' in source, (
        "名单里没有 spu / sku —— 点名点了个寂寞"
    )


# ==========================================================
# A-38 / A-39:图片集状态的对称与留痕
# ==========================================================


def test_reject_clears_the_approval_columns():
    """A-38:退回要清批准列,和 `approve()` 清退回列对称。

    不清的话同一行同时留着"批准于 x 由 y"和"已退回",而**哪一个是当前
    结论**要靠读 status 才知道。回滚会重新批准同一行,所以两组列同时有值
    不是理论情况。
    """
    tree = ast.parse(_read(APP / "listings" / "image_set_service.py"))
    source = ast.get_source_segment(
        _read(APP / "listings" / "image_set_service.py"), _fn(tree, "reject")
    ) or ""
    for column in ("approved_by", "approved_at"):
        assert f"row.{column} = None" in source, (
            f"reject() 没清 {column} —— 一版图会既是已批准又是已退回(A-38)"
        )
    assert "cleared_approved_by" in source, (
        "清掉的批准记录没有进审计 —— 清列是为了结论唯一,不是为了忘掉它"
    )

    approve_source = ast.get_source_segment(
        _read(APP / "listings" / "image_set_service.py"), _fn(tree, "approve")
    ) or ""
    for column in ("reject_reason", "rejected_at"):
        assert f"row.{column} = None" in approve_source, (
            f"approve() 不再清 {column} 了 —— 对称性是双向的"
        )


def test_the_automatic_downgrade_is_audited():
    """A-39:系统自动降级要进审计,不能只写日志。

    日志运营看不到。表现是一版**已经批准过**的图片集突然回到待复核、
    没有任何解释 —— 运营第一反应是"谁把它退回了",而审计里查不到任何人
    做过这件事。`reject()` / `approve()` 都记审计,唯独这条系统路径不记。
    """
    path = APP / "listings" / "image_set_service.py"
    fn = _fn(ast.parse(_read(path)), "downgrade_sets_using")
    source = ast.get_source_segment(_read(path), fn) or ""
    assert "audit.record(" in source, "自动降级仍然只有 logger,没有审计(A-39)"
    assert "downgrade_on_quarantine" in source, "审计里没有可筛的业务动作名"

    kwargs = {a.arg for a in fn.args.kwonlyargs}
    assert "actor" in kwargs, "降级不接收操作者 —— 只能记成 system"

    caller = _read(APP / "media" / "service.py")
    assert "downgrade_sets_using(\n        session, asset.id, actor=actor\n    )" in caller, (
        "隔离没有把操作者传下去 —— 隔离是人点的,降级是它的后果"
    )


# ==========================================================
# A-40:两处重置都要清 target_step
# ==========================================================


def test_both_reset_paths_clear_the_target_step():
    """A-40:清 `error_code` / `error_message` / `target_ref` 的地方也要清它。

    漏清的表现是一件 PENDING 的条目仍然带着上一次失败的"卡在哪一步",
    而详情页的下一步文案正是照它渲染的 —— 重试之后界面还停在旧卡点上。

    断言的是**成组**:凡是把这三个之一置 None 的语句块,同块里必须有
    `target_step = None`。这样以后新增第三处重置路径也会被这条钉住。
    """
    tree = ast.parse(_read(APP / "workbench" / "batch_service.py"))
    trio = {"error_code", "error_message", "target_ref"}

    def _cleared(body: list[ast.stmt]) -> set[str]:
        out: set[str] = set()
        for stmt in body:
            if (
                isinstance(stmt, ast.Assign)
                and isinstance(stmt.value, ast.Constant)
                and stmt.value.value is None
                and isinstance(stmt.targets[0], ast.Attribute)
            ):
                out.add(stmt.targets[0].attr)
        return out

    checked = 0
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list):
            continue
        cleared = _cleared(body)
        if not trio <= cleared:
            continue
        checked += 1
        assert "target_step" in cleared, (
            f"第 {body[0].lineno} 行附近的重置清了 {sorted(trio & cleared)} "
            "却漏了 target_step(A-40)"
        )
    assert checked >= 2, f"只找到 {checked} 处重置路径,预期至少 2 处 —— 断言认不出来了"


# ==========================================================
# A-29 / A-33:前端那两条
# ==========================================================


def test_a_background_refetch_no_longer_wipes_unsaved_edits():
    """A-29:版本变化不再静默清空本地草稿。

    改之前那个 effect 的依赖里有 `current.data?.version`:别人派生了新版,
    一次后台 refetch 就把 `draft` 置回 null —— 运营刚拖好的十几行**没有
    任何提示地**变回服务端那一版。`UnsavedGuard` 挡的是离开页面。
    """
    tab = _code_only(_read(FRONTEND / "components" / "workbench" / "ImageSetTab.tsx"))
    assert "}, [imageSetId, current.data?.version])" not in tab, (
        "版本变化又会静默清空草稿了(A-29)"
    )
    assert "const applyEdit = (next: Draft[] | null)" in tab, (
        "编辑没有收敛到单一入口 —— 记不住草稿是基于哪一版开始改的"
    )
    assert "const supersededBy =" in tab, "没有「你改的那一版已经被换掉了」的提示"

    # 所有编辑必须走 applyEdit:直接 setDraft 会绕过 draftBase 的记录
    raw = [
        line.strip()
        for line in tab.splitlines()
        if "setDraft(" in line and "const [draft" not in line
    ]
    assert len(raw) == 3, (
        f"有 {len(raw)} 处直接 setDraft(预期 3 处:applyEdit 内部 2 处 + 切换集 1 处)"
        f" -> {raw}"
    )


def test_the_browser_gives_up_later_than_the_gateway_does():
    """A-33:前端超时不该比网关短一个数量级。

    30s 的时候,后端还在跑、网关还在等,只有浏览器提前放弃了 ——
    运营看到"请求超时",而那次写很可能已经成功。接下来他会再点一次。

    长动作单独放宽,且**略短于** nginx 的 `proxy_read_timeout`:让超时由
    浏览器先报,错误里才有我们自己的文案;网关先断的话前端拿到的是一张
    504 HTML,`describeError` 认不出来。
    """
    client = _code_only(_read(FRONTEND / "api" / "client.ts"))
    # 先断常量在,再断数值。第一版直接 `re.search(...).group(1)`,在改动前的
    # 代码上抛的是 `AttributeError: 'NoneType' has no attribute 'group'` ——
    # 红得看不出是哪条 bug。第三批已经栽过一次,这里不再栽第二次。
    for const in ("DEFAULT_TIMEOUT_MS", "LONG_TIMEOUT_MS"):
        assert const in client, f"client.ts 里没有 {const} —— 超时又写成裸字面量了(A-33)"
    default_ms = int(
        re.search(r"DEFAULT_TIMEOUT_MS = ([\d_]+)", client).group(1).replace("_", "")
    )
    long_ms = int(
        re.search(r"LONG_TIMEOUT_MS = ([\d_]+)", client).group(1).replace("_", "")
    )
    assert default_ms >= 60_000, f"默认超时仍是 {default_ms}ms(A-33)"
    assert long_ms > default_ms, "长动作的超时没有比默认值长"

    nginx = _read(BACKEND_ROOT.parent / "frontend" / "nginx.conf")
    gateway_s = int(re.search(r"proxy_read_timeout\s+(\d+)s;", nginx).group(1))
    assert long_ms < gateway_s * 1000, (
        f"长动作超时 {long_ms}ms 不短于网关的 {gateway_s}s —— "
        "网关会先断,前端拿到的是一张认不出来的 504 HTML"
    )

    batch = _code_only(_read(FRONTEND / "api" / "batch.ts"))
    assert batch.count("LONG_TIMEOUT_MS") >= 3, (
        "生成 / 重生成 / 下载三条长动作没有全部放宽超时"
    )
