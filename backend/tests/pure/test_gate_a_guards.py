"""Gate A 修复的源码级不变量。

## 为什么是源码级

这几条真正的验收在数据库集成测试里(方案 §3.3),那批用例需要 PostgreSQL。
但其中几条不变量的性质是"**这行代码不该出现**":

    GET 审计接口不该有 commit
    批量导出的执行器不该调 record_export
    resolve 接口不该无条件按 id 取驳回记录

这类不变量集成测试测不到 —— 集成测试只能验证当前行为对不对,
验证不了"下一个人顺手加回来"的那次改动。它们和 `test_frontend_contract`
是同一类:钉住一个**没有其他防线**的跨文件约定。

集成测试补齐之后,这个文件仍然保留:两者验的不是同一件事。
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP = BACKEND_ROOT / "app"
FRONTEND = BACKEND_ROOT.parent / "frontend" / "src"


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _func_source(path: Path, name: str) -> str:
    """取一个顶层函数的源码。按 AST 定位,不靠正则数缩进。"""
    source = _read(path)
    tree = ast.parse(source)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.get_source_segment(source, node) or ""
    raise AssertionError(f"{path.name} 里找不到函数 {name}")


# ==========================================================
# A1 跨商品驳回
# ==========================================================


def test_resolve_endpoint_binds_rejection_to_the_current_draft():
    """A1:驳回记录必须按 rejection_id + draft_id 联合取。

    `session.get(PlatformRejection, rejection_id)` 是全局取:用商品 A 的
    rejection_id 请求商品 B 的这个接口会同时改到两件商品 —— 关掉 A 的驳回,
    再按 B 的草稿重算平台状态,而 A 留下"REJECTED 但已无 OPEN 驳回"的死态。
    """
    source = _func_source(APP / "api" / "workbench.py", "resolve_rejection")
    assert "PlatformRejection.draft_id" in source, "resolve 接口没有按 draft_id 约束驳回记录"
    assert "session.get(PlatformRejection" not in source, (
        "resolve 接口又在按 id 全局取驳回记录了"
    )


def test_resolve_service_rejects_a_foreign_rejection_before_writing():
    """A1:service 层也要有一份归属校验,而且抛在任何写入之前。

    校验只放在 API 层的问题是:`platform_service` 是唯一能写
    `platform_rejections` 的地方,而写库的守卫应该和写库放在一起 ——
    下一个调用方(批量、脚本、定时任务)不会经过那个 API。
    """
    source = _func_source(APP / "workbench" / "platform_service.py", "resolve_rejection")
    assert "rejection.draft_id != draft.id" in source, "service 层没有校验驳回归属"

    guard_at = source.index("rejection.draft_id != draft.id")
    # 第一处状态写入。守卫必须在它之前,否则事务里已经留下痕迹
    first_write = source.index("rejection.status = ")
    assert guard_at < first_write, "归属校验出现在写入之后,事务已经改过东西了"


# ==========================================================
# A2 全失败不得标记成功
# ==========================================================


def test_batch_extract_fails_when_nothing_succeeded():
    """A2:成功数为 0 时执行器必须返回 ok=False。

    `ok=True` 不只是界面上多一句"识别完成" —— 它会写幂等回执,
    此后普通重试直接命中回执、复用这次全失败的空结果,连重跑的机会都没有。
    """
    source = _func_source(APP / "workbench" / "batch_service.py", "_exec_extract")
    assert "succeeded_count" in source, "_exec_extract 没有检查成功数"
    assert "ok=False" in source, "_exec_extract 没有失败分支"
    assert "EXTRACTION_FAILED" in source, "全失败没有映射到预定义异常码"

    # 全失败分支必须早于 apply_evidence:没有证据可合并,跑一遍只会在审计里
    # 留下一次"什么都没改"的属性写入。
    # 比的是**调用**的位置,不是文字 —— 注释里提一句 apply_evidence 不算数
    assert source.index("ok=False") < source.index("attr_service.apply_evidence("), (
        "全失败时仍然会走到 apply_evidence"
    )


def test_failed_actions_still_do_not_write_a_receipt():
    """A2 的另一半:失败不写回执这条既有约定不能被绕过。

    这是"重试能真正重跑"的地基。`_exec_extract` 改成 ok=False 之所以有效,
    全靠 `_execute` 里这个 `if result.ok`。
    """
    source = _func_source(APP / "workbench" / "batch_service.py", "_execute")
    assert "if result.ok:" in source, "回执写入不再以成功为条件了"
    assert source.index("if result.ok:") < source.index("_write_receipt"), (
        "_write_receipt 跑在成功判断之外"
    )


def test_single_item_extract_skips_apply_evidence_when_all_failed():
    """A2:单件路径同样不能把空结果当成识别过了。

    ## 这条守卫此前是**假绿**的(A45-batch14-11 发现)

    它原来断言源码里出现过 `succeeded_count`,并且它出现在 `apply_evidence`
    之前。batch14-11 把判据换成单点派生的 `run_is_authoritative` 之后,
    这条守卫**照样绿** —— 因为换判据时留下的那句注释里写着
    「判据从 `succeeded_count > 0` 换成……」,而注释也是源码里的字。

    这是本仓库第五次栽在同一件事上(batch13-3 的 M2 / M11、batch14-2 的
    N15、batch14-4 那次),只是方向反过来:前四次是变异**注释掉**代码
    而守卫照样匹配,这次是重构**移走**了代码而守卫匹配到了注释。
    两次的根因是同一句话 —— **「文件里出现过这串字」从来不等于
    「这件事成立」**。

    改法与 §3.26 第四节一致:锚在语法结构上。这里用 AST 取那个 `if` 的
    条件本身,而注释在 AST 里根本不存在。
    """
    tree = ast.parse((APP / "tasks" / "attribute_tasks.py").read_text(encoding="utf-8"))
    endpoint = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef)
        and node.name == "run_attribute_extraction"
    )
    guarded = [
        node
        for node in ast.walk(endpoint)
        if isinstance(node, ast.If)
        and any(
            isinstance(call.func, ast.Attribute) and call.func.attr == "apply_evidence"
            for call in ast.walk(node)
            if isinstance(call, ast.Call)
        )
    ]
    assert guarded, "单件路径无条件合并证据 —— apply_evidence 不在任何条件里"
    checked = {
        sub.attr
        for node in guarded
        for sub in ast.walk(node.test)
        if isinstance(sub, ast.Attribute)
    }
    assert "run_is_authoritative" in checked, (
        f"合并证据的条件读的是 {sorted(checked)},而不是那一个单点判定"
    )


def test_frontend_does_not_report_a_total_failure_as_success():
    """A2:界面上"识别完成"不能出现在一张都没成功的时候。

    ## 这条守卫原来把一个临时办法钉成了规格(A45-batch14-11 改)

    它原来断言前端源码里出现 `succeeded_count ?? 0) === 0` ——
    也就是**前端自己判三档**那段代码。A2 要的性质是「全失败不能显示成功」,
    而这条守卫钉的是「用哪一行代码做到它」。

    于是判定搬到后端(硬规则 4 要求的那个方向)之后,这条守卫会变红,
    而它变红的原因是**病治好了**。守卫钉实现不钉性质时就会这样:
    它把当时那个凑合办法变成了以后不许动的规格。

    现在钉的是性质本身,分两头:后端算好的那一档**真的被读了**,
    而 error 语气**真的还在**(全失败用 success 弹一下,运营的结论是
    「识别过了」——那正是 A2 要防的结局)。
    """
    source = _read(FRONTEND / "components" / "workbench" / "AttributeTab.tsx")
    assert "RUN_STATUS_NOTICE[result.status]" in source, (
        "前端没有读后端给的那一档 —— 它要么又自己判了,要么根本不分档"
    )
    assert re.search(r"^\s*FAILED: \{$", _read(FRONTEND / "api" / "attributes.ts"), re.M), (
        "提示语表里没有 FAILED 那一档"
    )
    assert "tone: 'error'" in _read(FRONTEND / "api" / "attributes.ts"), (
        "全失败没有走 error 语气 —— success 两秒就消失,而 error 会等人关掉"
    )


# ==========================================================
# A3 导出状态与真实动作一致
# ==========================================================


def test_batch_export_executor_does_not_record_an_export():
    """A3:校验通过 ≠ 文件已生成。

    `_exec_export` 在 export_gate 过闸时就 export_count +1 并落导出审计,
    而此时没有任何文件存在 —— 文件要等运营点下载时才现场生成,
    届时草稿若已过期还会 409,审计里却已经记着"导出成功一次"。
    """
    source = _func_source(APP / "workbench" / "batch_service.py", "_exec_export")
    # 比的是调用形状。注释里解释"留痕为什么搬走了"是应该留着的
    assert "wb.record_export(" not in source, "批量执行器又在文件生成之前记导出留痕了"
    assert "file_generated" in source, "批次明细没有区分\"已过闸\"和\"已生成文件\""


def test_batch_file_records_the_export_after_the_bytes_exist():
    """A3:留痕补记在 zip 字节产出之后。文件生成失败就走不到那里。"""
    source = _func_source(APP / "workbench" / "batch_service.py", "batch_file")
    assert "wb.record_export(" in source, "批量路径不再记导出留痕了"
    assert source.index("buffer.getvalue()") < source.index("wb.record_export"), (
        "留痕补记跑在字节产出之前"
    )


def test_single_item_export_path_is_unchanged():
    """A3 的范围收窄:单件 `export_draft` 本来就是对的(先生成字节、后留痕),
    这次不动它。这条钉住"顺手统一一下"那种改动。
    """
    source = _func_source(APP / "workbench" / "service.py", "export_draft")
    assert "export_gate" in source and "record_export" in source
    assert source.index("export_gate") < source.index("record_export")


# ==========================================================
# A6 GET 不产生写入
# ==========================================================


def test_file_audit_get_does_not_commit():
    """A6:浏览器预取、重试或刷新不能改变业务状态。"""
    source = _func_source(APP / "api" / "workbench_batch.py", "batch_file_audit")
    assert "session.commit()" not in source, "GET file-audit 又在提交事务了"
    assert "dry_run=True" in source, "GET file-audit 没有走 dry-run"


def test_staleness_is_still_decided_in_exactly_one_place():
    """A6 的约束(来自 P4):过期口径只有 `refresh_draft` 一份。

    修法必须是给它加一个不落库的模式,**不是**在批次页复制一遍判定 ——
    复制出来的那份迟早和草稿页给出两个数字(方案 §3.4 禁止的那种)。
    """
    source = _read(APP / "workbench" / "batch_service.py")
    assert "draft_rules.is_stale" not in source, "批次页自己推断过期了"
    assert "is_stale(" not in source.replace("draft_stale", ""), (
        "批次页出现了第二份过期判定"
    )

    facts = _func_source(APP / "workbench" / "service.py", "draft_file_facts")
    assert "refresh_draft(session, product, row, dry_run=dry_run)" in facts


def test_refresh_draft_dry_run_writes_nothing():
    """A6:dry-run 模式下不许有落库动作。

    判据:函数里每一处 `row.status = ` 都必须被 `not dry_run` 守着。
    """
    source = _func_source(APP / "workbench" / "service.py", "refresh_draft")
    assert "dry_run" in source, "refresh_draft 没有 dry-run 模式"
    for line_no, line in enumerate(source.splitlines()):
        if "row.status = DraftStatus.STALE.value" in line:
            # 往上找最近的 if:它必须带 not dry_run
            preceding = source.splitlines()[max(0, line_no - 1)]
            assert "not dry_run" in preceding, (
                f"第 {line_no} 行的状态写入没有被 dry_run 守住"
            )


# ==========================================================
# A5 Admin / Operator 认证行为统一(前端三处)
# ==========================================================


def test_request_interceptor_falls_back_to_the_admin_token():
    """A5-1:只配了管理口令时,业务请求也要带口令。

    后端早就允许 admin 访问 operator 接口(`deps.resolve_identity`),
    是前端把这件事浪费掉了:没有 operator 口令时它一个头都不带,于是 401。
    """
    source = _read(FRONTEND / "api" / "client.ts")
    interceptor = source[source.index("interceptors.request.use") :]
    interceptor = interceptor[: interceptor.index("interceptors.response.use")]
    assert "readAdminToken()" in interceptor, "请求拦截器没有回落到管理口令"
    # 顺序不能反:两把都配了还是走 operator,免得拉个列表也把管理口令送出去
    assert interceptor.index("readOperatorToken()") < interceptor.index(
        "readAdminToken()"
    ), "回落顺序反了,日常请求会带上管理口令"


def test_anonymous_success_does_not_clear_the_auth_banner():
    """A5-3:匿名接口成功不能证明口令是好的。

    `/health` 走同一个 apiClient,而响应拦截器对**任何**成功响应执行
    `setAuthRejected(false)` —— 横幅刚被 401 点亮,下一次探活成功就把它清掉,
    口令错的时候提示反而看不见。
    """
    source = _read(FRONTEND / "api" / "client.ts")
    assert "ANONYMOUS_PATHS" in source, "没有匿名路径白名单"
    assert "if (!isAnonymousPath(response.config?.url)) setAuthRejected(false)" in source

    # 白名单要和后端 main.PUBLIC_PREFIXES 对齐,否则两边各说各话
    main_src = _read(APP / "main.py")
    match = re.search(r"PUBLIC_PREFIXES:[^=]*=\s*\(([^)]*)\)", main_src)
    assert match, "main.py 里找不到 PUBLIC_PREFIXES"
    backend_public = set(re.findall(r'"([^"]+)"', match.group(1)))
    frontend_public = set(
        re.findall(r"'([^']+)'", source[source.index("ANONYMOUS_PATHS") :].split("]")[0])
    )
    assert frontend_public == backend_public, (
        f"前后端匿名白名单不一致:前端 {sorted(frontend_public)} / "
        f"后端 {sorted(backend_public)}"
    )


def test_cold_start_banner_probes_with_a_token():
    """A5-2:"口令填对了没有"只能由一次带口令的请求回答。

    上一版用 `readOperatorToken() || readAdminToken()` 判断"已配置"来隐藏引导,
    于是口令复制时多带一个空格,它的结论仍然是"没问题"。

    A8 之后这次探测搬进了 `useIdentity` —— 菜单要问同一件事,两处各写一份
    `useQuery` 会让 `enabled` 条件分叉。要守的东西一个没变,只是换了地方:
    探测仍然存在、401 仍然算口令被拒、"填了没有"仍然由 localStorage 单独回答。
    """
    hook = _read(FRONTEND / "hooks" / "useIdentity.ts")
    assert "authApi.whoami()" in hook, "没有做带口令的探测"
    assert "isAuthError(probe.error)" in hook, "没有把探测的 401 算成口令被拒"

    banner = _read(FRONTEND / "components" / "ColdStartBanner.tsx")
    assert "useIdentity()" in banner, "横幅没有用那次探测的结论"
    # "填了没有"仍然由 localStorage 回答,这两件事分开
    assert "readOperatorToken()" in banner


def test_whoami_is_guarded_and_not_public():
    """A5-2 的地基:探测接口必须**要口令**,否则它探不出任何东西。"""
    source = _read(APP / "api" / "auth.py")
    assert "require_operator" in source, "whoami 没有挂守卫"

    main_src = _read(APP / "main.py")
    match = re.search(r"PUBLIC_PREFIXES:[^=]*=\s*\(([^)]*)\)", main_src)
    public = set(re.findall(r'"([^"]+)"', match.group(1)))
    assert not any(
        "/auth" == p or "/auth".startswith(p + "/") for p in public
    ), "/auth 被放进匿名白名单了,探测会永远成功"


# ==========================================================
# A10 快审退回
# ==========================================================


def test_copy_endpoints_bind_the_copy_to_the_product():
    """A10:文案的批准与退回都必须把商品的 SPU 递给服务层。

    这两个端点是 A1 那条缺陷的**同形**:路径是
    `/products/{product_id}/copy/{copy_id}/...`,而 `product_id` 以前只用来
    确认商品存在,`copy_id` 是全局取的 —— 两个参数从不互相印证。用 A 的
    copy_id 请求 B 的路径,动的是 A 的文案,而审计和界面都会记成 B。

    A1 修完之后这一条补上了守卫,但没有测试盯着。把 `expect_spu=` 删掉
    今天不会有任何东西变红,而缺陷的表现是静默串改 —— 那正是这个文件的用途。
    """
    for name in ("approve_copy", "reject_copy"):
        source = _func_source(APP / "api" / "workbench.py", name)
        assert "expect_spu=product.spu" in source, (
            f"{name} 没有把商品的 SPU 递下去,copy_id 又变成全局取的了"
        )


def test_copy_service_rejects_a_foreign_copy_before_writing():
    """A10:归属校验在服务层,而且在任何写入之前。

    理由同 A1:`copy_service` 是唯一能写 `listing_copies` 的地方,而下一个
    调用方(批量、脚本、以后的 worker)不会经过那个 API。守卫放在接口层
    就只保护了今天这一个入口。
    """
    for name in ("approve_copy", "reject_copy"):
        source = _func_source(APP / "listings" / "copy_service.py", name)
        assert "_load_for_spu" in source, f"{name} 没走归属校验,又在全局取文案了"

        guard_at = source.index("_load_for_spu")
        # 第一处状态写入。守卫必须在它之前,否则事务里已经留下痕迹
        first_write = source.index("row.status = ")
        assert guard_at < first_write, (
            f"{name} 的归属校验出现在写入之后,事务已经改过东西了"
        )


def test_a_reject_is_validated_before_anything_is_written():
    """A10:三条入参校验(原因认不认 / 对不对象 / 「其他」有没有说明)
    必须跑在写库之前,而且在**服务层**跑。

    放在接口层的问题和 A1 一样:那三条校验决定的是"这条退回记录三周后
    还看不看得懂",而它要保护的是数据,不是某一个 HTTP 入口。写库的守卫
    和写库放在一起。
    """
    cases = (
        (APP / "listings" / "image_set_service.py", "reject"),
        (APP / "listings" / "copy_service.py", "reject_copy"),
    )
    for path, name in cases:
        source = _func_source(path, name)
        assert "validate_reject" in source, f"{path.name}:{name} 没有校验退回入参"

        guard_at = source.index("validate_reject")
        first_write = source.index("row.status = ")
        assert guard_at < first_write, (
            f"{path.name}:{name} 先写库再校验,不合法的退回已经落进事务了"
        )


def test_the_reject_reason_list_is_not_copied_into_the_frontend():
    """A10:九个原因的中文与「退回后去哪一步」只有后端一份。

    前端抄一份的后果不是显示错字:运营选了「商品结构改变」,界面说
    "接下来去重排图片",而系统实际把他送去改属性 —— 两个说法都出自本系统,
    他会信界面那一个。§3.2.1 的纪律是唯一下一步由后端算、前端只翻译。

    判据是原因**码**不出现在前端源码里(中文可能出现在注释里解释为什么
    不抄,那不构成第二份实现)。
    """
    codes = ("STRUCTURE_CHANGED", "COLOR_MISMATCH", "PRINT_CHANGED",
             "LOW_IMAGE_QUALITY", "COPY_FACT_ERROR", "UNNATURAL_PT",
             "IRRELEVANT_KEYWORDS", "NEEDS_MATERIAL")
    for path in FRONTEND.rglob("*.ts*"):
        source = _read(path)
        # 注释行不算:前端确实在注释里引用过原因名来解释这条纪律
        code_lines = [
            line for line in source.splitlines()
            if not line.lstrip().startswith(("*", "//", "/*"))
        ]
        body = "\n".join(code_lines)
        for code in codes:
            assert code not in body, (
                f"{path.name} 里出现了退回原因码 {code!r} —— 前端又抄了一份原因表"
            )
