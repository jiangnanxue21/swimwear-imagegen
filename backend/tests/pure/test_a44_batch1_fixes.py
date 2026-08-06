"""A44 第一批修复的契约测试:A-16 / A-21 / A-17 / C-16 / A-18。

和 `test_batch_lifecycle_and_readonly_reads.py` 同一套办法:能穷举的取值就
穷举,穷举不了的钉 **AST 节点**而不是字符串包含 —— 阶段 7 的教训是
`"xxx" in source` 式的断言,把代码删掉只留注释照样绿。

这五条各自的失效方式都很安静,所以每条都配一句"退化成什么样":

    A-16   锁被谁顺手删掉    -> 双击生成两份,export_count 双计,sha 后写覆盖先写
    A-21   端点被删/改成幂等  -> 409 文案又指向一个不存在的动作,运营在里面打转
    A-17   判据退回 paid_calls -> 每个多图商品都报 P0,真的重复付费淹掉
    C-16   写回约束名         -> 迁移改名后所有付费动作被拒,日志不指向根因
    A-18   心跳退回 job 行     -> 健康批次每个 beat 被重投,"stalled" 语义失真
"""
from __future__ import annotations

import ast

from tests.pure._helpers import BACKEND_ROOT

APP = BACKEND_ROOT / "app"
BATCH_SERVICE = APP / "workbench" / "batch_service.py"
BATCH_API = APP / "api" / "workbench_batch.py"


def _read(path):
    return path.read_text(encoding="utf-8")


def _func(path, name: str) -> ast.FunctionDef:
    for node in ast.walk(ast.parse(_read(path))):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到函数 {name}")


def _src(path, name: str) -> str:
    return ast.get_source_segment(_read(path), _func(path, name))


def _code(path, name: str) -> str:
    """函数体的**代码**,注释和 docstring 都不算。

    直接对源码文本做包含判断会被注释骗到两次,而且是**两个方向**都会骗:

        假绿   注释里写着 `_lock_job_row`,锁其实被删了
        假红   注释里解释"原来写的是 `BatchJob.updated_at <= cutoff`",
               而断言正好在找这个字符串

    第二种就是这一批测试第一次跑出来的样子 —— 修复本身是对的,是断言错了。
    `ast.unparse` 重新打印语法树,注释天然消失,docstring 单独摘掉。
    """
    fn = _func(path, name)
    body = list(fn.body)
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(node) for node in body)


def _call_names(node: ast.AST) -> set[str]:
    out: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        if isinstance(func, ast.Name):
            out.add(func.id)
        elif isinstance(func, ast.Attribute):
            if isinstance(func.value, ast.Name):
                out.add(f"{func.value.id}.{func.attr}")
            out.add(func.attr)
    return out


def _keywords_of(node: ast.AST, call_name: str) -> set[str]:
    """某个调用用到的关键字参数名。"""
    out: set[str] = set()
    for sub in ast.walk(node):
        if not isinstance(sub, ast.Call):
            continue
        func = sub.func
        name = (
            func.id
            if isinstance(func, ast.Name)
            else func.attr
            if isinstance(func, ast.Attribute)
            else None
        )
        if name == call_name:
            out |= {kw.arg for kw in sub.keywords if kw.arg}
    return out


# ==========================================================
# A-16:生成的幂等靠锁,不靠那句 if
# ==========================================================


def test_generation_locks_the_job_row_before_it_checks():
    """先锁后判。顺序反过来 = 回到无锁 check-then-act。"""
    src = _code(BATCH_SERVICE, "ensure_batch_export")
    assert "_lock_job_row" in src, (
        "生成路径又回到无锁 check-then-act 了。两个并发 POST 会各自跑完整个 "
        "batch_file():export_count 双计、file_sha256 后写覆盖先写"
    )
    assert src.index("_lock_job_row") < src.index("if job.file_path:"), (
        "锁上在幂等检查之后 —— 那两个请求已经都读到过空的 file_path 了,锁白上"
    )


def test_the_lock_helper_both_locks_and_refreshes():
    """只 FOR UPDATE 不 refresh,身份映射里还是进门时的旧快照。"""
    fn = _func(BATCH_SERVICE, "_lock_job_row")
    called = _call_names(fn)
    assert "with_for_update" in called, "_lock_job_row 不再上行锁了"
    assert "session.refresh" in called, (
        "拿到锁之后没有 refresh。ORM 身份映射里仍是旧快照,"
        "第二个请求照样看到 file_path 为空 —— 锁等于没上"
    )


def test_regeneration_takes_the_same_lock():
    """再生成同样会写身份四列,不锁就是第二条竞态路径。"""
    assert "_lock_job_row" in _code(BATCH_SERVICE, "regenerate_batch_export"), (
        "再生成没上锁 —— 它和生成写同一批列,竞态形状完全一样"
    )


# ==========================================================
# A-21:409 文案指向的动作必须真的存在
# ==========================================================


def test_a_regenerate_entry_point_exists():
    assert "regenerate_batch_export" in _read(BATCH_SERVICE), (
        "再生成函数没了。read_batch_file 的两处 409 又变回指向一个不存在的动作"
    )
    assert '"/batches/{batch_id}/file/regenerate"' in _read(BATCH_API), (
        "再生成端点没了 —— 运营照着 409 提示做,只会撞回同一个 409"
    )


def test_regeneration_is_admin_only_and_demands_a_reason():
    """换掉 sha 就是换掉驳回定位的物证,不该和"点一下下载"共用门槛。"""
    fn = _func(BATCH_API, "regenerate_batch_file")
    decorators = "\n".join(ast.unparse(d) for d in fn.decorator_list)
    assert "require_admin" in decorators, (
        "再生成端点没挂管理员守卫。它换掉的是平台驳回定位的物证,"
        "不该和「点一下下载」共用门槛"
    )
    src = _code(BATCH_SERVICE, "regenerate_batch_export")
    assert "reason" in src, "再生成不要理由了 —— 事后没人答得出为什么换过一版"


def test_regeneration_records_both_shas_in_one_audit_row():
    """分两条记的话,中间插进别的记录就再也串不起来。"""
    src = _code(BATCH_SERVICE, "regenerate_batch_export")
    assert "old_sha256" in src and "new_sha256" in src, (
        "审计里没有同时记旧 sha 和新 sha —— 平台驳回定位要问的第一个问题就没答案了"
    )


def test_regeneration_clears_the_identity_before_it_generates():
    """反过来的话,中途抛异常会留下"旧身份 + 新字节"。"""
    src = _code(BATCH_SERVICE, "regenerate_batch_export")
    assert src.index("job.file_path = None") < src.index("batch_file("), (
        "先生成后清身份 —— 中途失败会留下旧 sha 配新字节,"
        "而那正是 read_batch_file 的校验和检查要拦的那种不一致"
    )


def test_generation_stays_idempotent():
    """再生成是**另一个动词**。把 POST /file 改成不幂等等于拆掉第 6 条那道闸。"""
    src = _code(BATCH_SERVICE, "ensure_batch_export")
    assert "if job.file_path:" in src, "生成不再幂等了"
    assert src.index("if job.file_path:") < src.index("batch_file("), (
        "幂等检查跑在重新生成之后"
    )


def test_the_regenerate_audit_action_has_a_label_on_both_sides():
    from app.workbench.audit_view import PAYLOAD_ACTION_LABELS

    assert "regenerate_export_file" in PAYLOAD_ACTION_LABELS
    assert (
        "regenerate_export_file"
        in (BACKEND_ROOT.parent / "frontend/src/api/batch.ts").read_text(
            encoding="utf-8"
        )
    ), "前端文案表少这一个词,审计页会显示成英文"


# ==========================================================
# A-17:异常判的是次数,不是钱
# ==========================================================


def test_the_anomaly_signal_reads_executions_not_paid_calls():
    """逐图计费下 `paid_calls > 1` 把每个多图商品都标成 P0。"""
    src = _code(BATCH_SERVICE, "receipt_ledger")
    assert "r.executions > 1" in src, (
        "异常判据退回 paid_calls 了。一件 4 图商品首次执行 paid_calls 就是 4,"
        "台账会把每个多图商品都报成 §7.3 的那个 P0"
    )
    assert "r.paid_calls > 1" not in src, "旧判据还在,噪声原样回来"


def test_per_image_billing_semantics_are_untouched():
    """`paid_calls` 没有错,错的是拿它当次数用。改回一件一次会让费用少算。

    ## 这条断言自己改过一次(R1-39)

    原来钉的是一整串字面量 `attempted = int(extraction.image_count or 0)`。
    它守的其实是**语义**(按图片数计费),钉的却是**写法** —— 于是 R1-39
    把那行拆成可读的两句时,这条当场红了,而计费口径一个字都没变。

    这是本仓第二次出现"门禁钉住了写法而不是不变量"(第一次是租约不变量
    被要求写成 `assert`,见第五批)。现在改成钉 AST:`attempted` 必须
    从 `image_count` 推出来,且 `billable_calls` 必须等于它。
    """

    fn = _func(BATCH_SERVICE, "_exec_extract")
    derived = [
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id in {"attempted", "images"}
            for t in node.targets
        )
        and "image_count" in ast.dump(node.value)
    ]
    assert derived, (
        "逐图计费被改回去了 —— `attempted` 不再从 image_count 推出来,"
        "一件 8 张图的商品会少算 7 次费用"
    )
    src = _code(BATCH_SERVICE, "_exec_extract")
    assert "billable_calls=attempted" in src


def test_claiming_an_execution_counts_it():
    """`executions` 只有在这里 +1,别处都不该碰它。"""
    src = _code(BATCH_SERVICE, "_claim_in_flight")
    assert "executions=1" in src, "认领不再记执行次数,异常判据永远为假"
    assert "executions" in src and "+ 1" in src, (
        "冲突分支没有累计 executions —— 第二次真的开跑不会被记下来"
    )


def test_the_receipt_model_and_migration_agree_on_the_default():
    """老回执存在即至少跑过一次。默认 0 会让历史行全部显示"付过钱没跑过"。"""
    model = _read(APP.parent / "app" / "models" / "batch_job.py")
    assert 'server_default="1"' in model
    migration = _read(
        BACKEND_ROOT / "migrations" / "versions" / "0028_receipt_executions.py"
    )
    assert 'server_default="1"' in migration, "迁移与模型的默认值分叉了"
    assert 'down_revision: Union[str, None] = "0027"' in migration


# ==========================================================
# C-16:按列名冲突,不按约束名
# ==========================================================


def test_the_upsert_does_not_hardcode_a_constraint_name():
    """迁移里改个名,这条 upsert 就恒抛 -> 被 except 吞成 False
    -> **所有付费动作被拒**,而日志只报异常类型不指向根因。"""
    fn = _func(BATCH_SERVICE, "_claim_in_flight")
    kwargs = _keywords_of(fn, "on_conflict_do_update")
    assert "index_elements" in kwargs, "回执 upsert 没按列名冲突"
    assert "constraint" not in kwargs, (
        "又写死约束名了。改名后 upsert 恒抛、被吞成 False,"
        "结果是所有付费动作被拒 —— fail-safe 但功能瘫"
    )
    assert "uq_batch_action_receipts_key" not in _code(BATCH_SERVICE, "_claim_in_flight")


# ==========================================================
# A-18:心跳取条目,不取 job 行
# ==========================================================


def test_stall_detection_looks_at_item_progress():
    """`job.updated_at` 只在 job 行真被 UPDATE 时推进,而 run_batch 只在
    第一轮 chunk 写它 —— 任何健康运行超 5 分钟且仍有 PENDING 的批次,
    每个 beat 都会被重投。"""
    src = _code(BATCH_SERVICE, "redispatch_stalled_batches")
    assert "BatchJobItem.finished_at" in src, (
        "停滞判据又只看 job 行了。批次自己没有心跳列,它的条目才有 —— "
        "健康批次会被每拍重投,而 'stalled' 这个词在告警里就此失真"
    )
    assert "greatest" in src, "没有把 job 行与条目进度取最大值"
    assert "BatchJob.updated_at <= cutoff" not in src, "旧判据还在,等于没改"


def test_the_heartbeat_subquery_is_correlated():
    """不 correlate 的话它会退化成"全表最新的那件",与批次无关。"""
    src = _code(BATCH_SERVICE, "redispatch_stalled_batches")
    assert "correlate(BatchJob)" in src
    assert "BatchJobItem.batch_id == BatchJob.id" in src
