"""A45 batch12-5:回归报告点名的三条 P1。

    NEW-01  同一笔外部生成在恢复后被费用台账记两次。
            **不是重复扣费**(Provider 没有重复 submit),是内部账目失真 ——
            而失真的恰好是"恢复过的那批任务",也就是最需要看清的一批。
    NEW-02  评分重排在**释放阶段租约之前**派发新消息。新 worker 抢不到租约、
            静默退出,而 Outbox 已经标成 DISPATCHED —— 消息已消费、任务没人跑。
    NEW-03  阶段租约(900s)和卡死阈值(1800s)按 4 张图、无内部重试估的,
            而系统允许 8 张 + 2 次重试,合法最坏耗时 2700s 以上。

## 这份守卫分成两半,和上一轮同一个理由

上一轮的教训是"1824 条全绿的同时仍然存在自动重复扣费",因为断言没有覆盖
跨函数组合。所以:

**能真跑的就真跑。** `dispatch_policy` 和 `phase_budget` 都是零依赖的
(后者读配置走 `providers/_config`,那一层对 pydantic 是软依赖),所以
NEW-03 的整个推导在这里是**调用真函数**算出来的,不是比对源码里的数字。

**跑不动的用 AST 钉结构。** NEW-01 / NEW-02 落在 `generation_tasks` 和
`generation_service` 上,它们一 import 就要 sqlalchemy。真正的行为验证在
`tests/test_a45_batch12_5_lease_and_billing_db.py`,那一批需要真实 PostgreSQL。

**AST 断言的定位是"结构塌了要红",不是"行为对了"。**
"""
from __future__ import annotations

import ast

from app.core.field_limits import MAX_CANDIDATE_COUNT
from app.workflows import dispatch_policy as policy
from app.workflows import phase_budget
from tests.pure._helpers import BACKEND_ROOT

GENERATION_TASKS = BACKEND_ROOT / "app" / "tasks" / "generation_tasks.py"
GENERATION_SERVICE = BACKEND_ROOT / "app" / "services" / "generation_service.py"
EVALUATION_SERVICE = BACKEND_ROOT / "app" / "services" / "evaluation_service.py"
GENERATION_SCHEMA = BACKEND_ROOT / "app" / "schemas" / "generation.py"
GENERATION_MODEL = BACKEND_ROOT / "app" / "models" / "generation.py"
MIGRATIONS = BACKEND_ROOT / "migrations" / "versions"


def _tree(path):
    return ast.parse(path.read_text(encoding="utf-8"))


def _fn(path, name: str) -> ast.FunctionDef:
    for node in ast.walk(_tree(path)):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{path.name} 里找不到 {name} —— 改名了就把这份守卫一起改")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    return [
        child
        for child in ast.walk(node)
        if isinstance(child, ast.Call) and name in ast.unparse(child.func)
    ]


def _kwarg(call: ast.Call, name: str) -> ast.expr | None:
    for kw in call.keywords:
        if kw.arg == name:
            return kw.value
    return None


def _first_line(node: ast.AST, predicate) -> int | None:
    """满足 predicate 的第一条语句在第几行。用来断言**顺序**。"""
    hits = [
        child.lineno
        for child in ast.walk(node)
        if getattr(child, "lineno", None) is not None and predicate(child)
    ]
    return min(hits) if hits else None


# ==========================================================
# NEW-01:一条 attempt 只能有一笔生成费用
# ==========================================================


def test_every_submit_usage_record_carries_a_billing_identity():
    """所有可能记 `operation="submit"` 的流水都必须带计费身份。

    这是 NEW-01 的核心。REG-01 的修复给 `_await_and_collect()` 开了第二条
    进入路径(续跑),而它的公共出口无条件写一条 submit 流水 —— 于是
    "全部下载失败 -> 恢复成功" 会在台账上留下两条、6 个计费单位,
    而 Provider 只跑过一次、只收过一次钱。

    漏掉任何一个调用点,那条路径上的重复记账就会回来,而且**没有任何报错**。

    ## 判据分两种

    字面量 `operation="submit"` 是直接的。`_abandon_attempt` 那一条传的是
    **转发进来的变量** —— 它在 submit 崩掉时确实会是 "submit",所以那一条
    必须自己判断并有条件地传键。其余带变量 `operation` 的调用点(轮询、
    取结果)由下一条测试从反方向守住。
    """
    #: 会转发一个可能是 "submit" 的 operation 变量进来的函数。
    #: 这张表变长就说明又多了一条要自己判断计费身份的路径。
    _FORWARDS_OPERATION = {"_abandon_attempt"}

    checked = 0
    for fn in (
        node for node in ast.walk(_tree(GENERATION_TASKS))
        if isinstance(node, ast.FunctionDef)
    ):
        for call in _calls(fn, "record_usage"):
            operation = _kwarg(call, "operation")
            if operation is None:
                continue
            literal_submit = ast.unparse(operation) == "'submit'"
            forwarded = fn.name in _FORWARDS_OPERATION
            if not (literal_submit or forwarded):
                continue
            checked += 1
            key = _kwarg(call, "billing_key")
            assert key is not None, (
                f"`{fn.name}` 第 {call.lineno} 行的 record_usage 没有传 billing_key —— "
                "这条路径上恢复一次就多记一笔生成费用"
            )
            assert "submit_billing_key" in ast.unparse(key), (
                f"`{fn.name}` 第 {call.lineno} 行的 billing_key 不是 "
                "`submit_billing_key()` 算出来的 —— 键的形状必须只有一个来源,"
                "否则两条路径会算出两个不同的键,幂等失效"
            )

    assert checked >= 3, (
        f"只找到 {checked} 个 submit 计费点,期望至少 3 个"
        "(提交失败、落库之后、意外收尾)—— 少了说明有一条路径改名或消失了,"
        "而这份守卫会安静地不再覆盖它"
    )


def test_polling_and_scoring_calls_stay_out_of_the_billing_key_scheme():
    """轮询、取结果、评分**不能**折叠成一行。

    反方向同样要守:那几类每一次都是真的又调了一次(评分尤其 —— 一张图
    评两次就是两次真实计费),给它们加幂等键会让台账**少记**,
    而少记比多记更难发现。
    """
    for call in _calls(_tree(EVALUATION_SERVICE), "record_usage"):
        assert _kwarg(call, "billing_key") is None, (
            "评分流水被加上了计费身份 —— 同一张图重评一次就会被折叠掉,台账少记"
        )

    for call in _calls(_tree(GENERATION_TASKS), "record_usage"):
        operation = _kwarg(call, "operation")
        if operation is not None and ast.unparse(operation) in {"'status'", "'fetch'"}:
            assert _kwarg(call, "billing_key") is None, (
                f"第 {call.lineno} 行给轮询/取结果加了计费身份 —— 那是真的又调了一次"
            )


def test_record_usage_updates_the_existing_row_instead_of_adding_a_second():
    """`record_usage` 拿到已有键时必须走更新分支,而不是照常 `session.add()`。

    只加一列、调用方也传了键,但函数体里仍然无条件 `add()` 的话,
    唯一约束会把重复记账变成一次 IntegrityError —— 比静默多记好,
    但那是**把生成任务弄崩**,不是修好。
    """
    fn = _fn(GENERATION_SERVICE, "record_usage")
    src = ast.unparse(fn)
    assert "billing_key" in {a.arg for a in fn.args.kwonlyargs}, (
        "`record_usage` 没有 billing_key 参数了"
    )
    assert "ProviderUsageRecord.billing_key ==" in src, (
        "没有按 billing_key 查已有行 —— 那就只剩\"每次都新增\"一条路"
    )
    # 更新分支必须真的改掉结论:恢复成功之后那次调用从失败变成成功,
    # 不改的话台账上会留着一条"成功的调用记成失败"
    assert "record.succeeded = succeeded" in src, (
        "更新分支没有改写 succeeded —— 恢复成功之后失败率统计仍然是错的"
    )
    # 计费单位只增不减:恢复这一遍拿回来的候选可能比第一遍少,
    # 但钱是按第一次实际产出收的
    assert "max(int(record.billable_units or 0), int(units))" in src, (
        "计费单位不是取两次的较大值 —— 台账会少记一笔已经花掉的钱"
    )


def test_the_billing_key_is_unique_in_both_the_model_and_a_migration():
    """应用层认领 + 数据库唯一约束,两道都要有。

    只有应用层的话,下一个写新记账路径的人漏掉键的后果是**一批看不出来的
    重复流水**;有约束的话,后果是一次 IntegrityError。第二种可以在测试里
    被发现,第一种不能。这与迁移 0033 对候选表的处理是同一条理由。
    """
    model_src = GENERATION_MODEL.read_text(encoding="utf-8")
    assert "uq_provider_usage_records_billing_key" in model_src, (
        "ORM 上没有 billing_key 的唯一约束 —— `create_all()` 建出来的表"
        "(测试夹具走的正是那条路)会和迁移建出来的不一致"
    )

    migrations = sorted(MIGRATIONS.glob("00*_*.py"))
    assert any(
        "uq_provider_usage_records_billing_key" in path.read_text(encoding="utf-8")
        for path in migrations
    ), "没有任何一条迁移建这个唯一约束 —— 线上库不会有它"


# ==========================================================
# NEW-02:派发之前先释放阶段租约
# ==========================================================


def test_the_scoring_reschedule_releases_the_lease_before_it_enqueues():
    """`commit -> release -> enqueue`,顺序不能反。

    反过来的话 Outbox 行一提交 relay 就可能立刻投递,而租约还挂在旧 worker
    身上:新 worker `_claim_phase()` 失败、静默退出、消息被 ack,
    Outbox 却已经是 DISPATCHED。任务停在 SCORING 等回收器,
    而**每一层都认为自己成功了**。
    """
    fn = _fn(GENERATION_TASKS, "_reschedule_scoring")
    release = _first_line(
        fn,
        lambda n: isinstance(n, ast.Call) and "_release_lease" in ast.unparse(n.func),
    )
    enqueue = _first_line(
        fn,
        lambda n: isinstance(n, ast.Call) and "enqueue" in ast.unparse(n.func),
    )
    assert release is not None, (
        "`_reschedule_scoring` 没有释放阶段租约 —— 新消息会投给一个抢不到租约的 worker"
    )
    assert enqueue is not None, "重排评分不再登记派发意图了?"
    assert release < enqueue, (
        f"释放在第 {release} 行、登记意图在第 {enqueue} 行 —— "
        "顺序反了,relay 可能在租约还挂着的时候就把消息投出去"
    )


def test_the_task_entrypoint_releases_the_lease_before_dispatching():
    """`run_generation_task` 也一样:release 必须排在 `_dispatch_next_round` 前面。

    报告里那条注入记录是 `['commit', 'dispatch', 'release', 'close']` ——
    释放落在 `finally` 上,也就是**派发之后**。
    """
    fn = _fn(GENERATION_TASKS, "run_generation_task")
    release = _first_line(
        fn,
        lambda n: isinstance(n, ast.Call)
        and "_release_lease_before_dispatch" in ast.unparse(n.func),
    )
    dispatch = _first_line(
        fn,
        lambda n: isinstance(n, ast.Call) and "_dispatch_next_round" in ast.unparse(n.func),
    )
    assert release is not None, "入口没有在派发前释放租约"
    assert dispatch is not None, "派发下一轮的调用不见了?"
    assert release < dispatch, (
        f"释放在第 {release} 行、派发在第 {dispatch} 行 —— 顺序反了"
    )


def test_releasing_before_dispatch_takes_the_owner_out_of_the_lease_dict():
    """释放之后要把 owner 摘掉,否则 `finally` 会再还一次。

    重复释放本身无害(条件里带着 owner),但"谁还持有租约"这件事在代码里
    有两个答案时,下一个人一定会读错其中一个。
    """
    src = ast.unparse(_fn(GENERATION_TASKS, "_release_lease_before_dispatch"))
    assert ".pop('owner'" in src or '.pop("owner"' in src, (
        "释放之后没有从 lease 里摘掉 owner"
    )


# ==========================================================
# NEW-03:预算按配置推导,长阶段有心跳、有续期、有 fencing
# ==========================================================


def test_the_pause_budget_is_derived_from_the_real_candidate_ceiling():
    """不许再手写 4。接口允许几张,预算就得按几张算。

    上一版是 `300 + 4 * 30 + 4 * 90`,而 `TaskCreate.candidate_count` 是
    `le=8` —— 一个完全合法的 8 张任务的评分耗时超出阈值一倍以上。
    """
    schema_src = GENERATION_SCHEMA.read_text(encoding="utf-8")
    assert "MAX_CANDIDATE_COUNT" in schema_src, (
        "接口上限又变回硬编码了 —— 它和耗时预算必须读同一份"
    )
    assert phase_budget.budget_inputs()["candidate_count"] == MAX_CANDIDATE_COUNT, (
        "预算用的候选张数不是接口上限"
    )

    # 判据看 **AST**,不看源码文本:那段修复说明里引用了旧公式
    # (`300 + 4 * 30 + 4 * 90`),按文本匹配会命中自己的注释。
    # 注释根本不进 AST,文档字符串这里显式跳过。
    policy_tree = ast.parse(
        (BACKEND_ROOT / "app" / "workflows" / "dispatch_policy.py").read_text(
            encoding="utf-8"
        )
    )
    docstrings = {
        id(node.value)
        for node in ast.walk(policy_tree)
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant)
    }
    hardcoded = [
        node
        for node in ast.walk(policy_tree)
        if isinstance(node, ast.Constant)
        and id(node) not in docstrings
        and isinstance(node.value, int)
        and not isinstance(node.value, bool)
        and node.value == 4
    ]
    assert not hardcoded, (
        f"`dispatch_policy` 的可执行代码里还有硬编码的 4"
        f"(第 {[n.lineno for n in hardcoded]} 行)—— "
        "候选张数必须来自 `MAX_CANDIDATE_COUNT`"
    )


def test_a_single_candidate_may_be_scored_three_times_not_once():
    """单张评分的预算必须含模型内部重试:1 次初调 + `VISION_MODEL_MAX_RETRIES`。

    少算这一项正是 NEW-03 的形状:90 秒 vs 270 秒,差三倍。
    """
    assert phase_budget.budget_inputs()["score_attempts"] == 3, (
        "单张评分的总次数不是 3(默认 VISION_MODEL_MAX_RETRIES=2)—— "
        "传的多半是重试次数而不是总次数,那会正好少算一次"
    )


def test_the_whole_legal_phase_is_longer_than_the_lease_so_renewal_is_mandatory():
    """一整段合法耗时**必然**超过租约长度 —— 所以续期不是优化,是前提。

    这条断言的作用是把那个前提钉住:哪天有人把续期删掉,或者把租约调到
    "反正够长"的量级,这里会红。8 张图的合法评分耗时是 2160 秒,
    而租约按设计只覆盖一格。
    """
    assert phase_budget.longest_legal_phase_seconds() > phase_budget.phase_lease_seconds(), (
        "整段耗时居然短于租约 —— 要么预算算错了,要么租约被调成了'反正够长'"
    )
    # 一格必须能被租约覆盖,否则 worker 会在一次正常的外部调用中途丢掉租约
    assert phase_budget.phase_lease_seconds() > phase_budget.longest_legal_pause_seconds()


def test_the_stall_threshold_exceeds_the_longest_gap_between_heartbeats():
    """回收阈值 vs **心跳间隔**,不是 vs 整段耗时。

    判据换成心跳之后两边同时变好:8 张图的任务不再被误杀,而一个真死掉的
    worker 也不用等 45 分钟(那是"阈值 > 整段耗时"必然导致的数字)。
    """
    pause = phase_budget.longest_legal_pause_seconds()
    threshold = phase_budget.stall_threshold_seconds()
    assert threshold > pause, f"阈值 {threshold}s 小于最长心跳间隔 {pause}s,会误杀"
    assert threshold >= policy.STALL_MARGIN * pause, "余量不足"


def test_the_budget_tracks_configuration_instead_of_freezing_at_import():
    """把评分超时调大,阈值和租约必须跟着变。

    这三个值都在设置页上(`core/settings_schema.py`),而页面写着"改完就生效"。
    冻在 import 那一刻的话,那句话对回收器不成立 —— 而"调大超时"正是最需要
    阈值跟着变的方向。

    ## 这条用例原来是**假绿**(A45-batch14-5 修)

    第一版改的是 `os.environ["VISION_MODEL_TIMEOUT_SECONDS"]`。而
    `provider_setting()` 的读取顺序是:

        1. `_override(name)`      后台设置页写进数据库的覆盖值,每次重读
        2. `app.core.config.settings`   pydantic BaseSettings,**import 时读一次**
        3. `os.environ`          仅在第 2 步 ImportError 时

    也就是说改环境变量只有走到第 3 步才看得见,而第 3 步的前提是
    `pydantic_settings` **装不上**。`_config.py` 自己的文档字符串写着
    "生产环境里永远走第一条分支;退化分支只在被裁剪过的运行环境生效"。

    于是这条用例在离线容器里绿了很久 —— 绿的原因是**依赖缺失**,
    与它声称守的那件事无关;而在 CI、评审机、生产镜像上它是红的。
    这是本仓库反复出现的那个形状的又一次:红/绿是真的,原因不是。
    (batch14-3 F7 是同一句话的另一半。)

    现在改成推**第 1 层** —— 设置页真正走的那条路。它同时把原来的意图
    守得更准:被测的是"预算每次重新求值",而不是"环境变量读得到"。
    """
    from app.providers import _config

    before = phase_budget.describe()

    original = _config._override
    _config._override = lambda name: "600" if name == "VISION_MODEL_TIMEOUT_SECONDS" else None
    try:
        after = phase_budget.describe()
    finally:
        _config._override = original

    assert after["longest_legal_pause_seconds"] > before["longest_legal_pause_seconds"], (
        "调大视觉超时之后最长停顿没变 —— 预算被冻在 import 那一刻了"
    )
    assert after["stall_threshold_seconds"] > before["stall_threshold_seconds"], (
        "最长停顿变了但卡死阈值没跟着变 —— 回收器会开始误杀"
    )
    assert after["phase_lease_seconds"] > before["phase_lease_seconds"], (
        "租约没跟着变 —— worker 会在一次正常调用中途丢掉租约"
    )
    assert phase_budget.describe() == before, "撤掉覆盖之后没有回到原值"


def test_the_settings_page_promise_goes_through_the_override_layer_not_the_environment():
    """"改完就生效"靠的是**每次重读的覆盖层**,不是环境变量。

    这条是上一条的配套:没有它,把上一条的 `_override` 换回 `os.environ`
    再在离线容器里跑,又是一次假绿,而且没有任何东西会指出原因。

    钉两件事:覆盖层在最前面且每次都读;环境变量这条路**允许**被
    import 期快照住(容器启动前就定好,本来就不该运行期改)。
    """
    from app.providers import _config

    seen: list[str] = []
    original = _config._override

    def spy(name: str) -> str | None:
        seen.append(name)
        return None

    _config._override = spy
    try:
        _config.provider_setting("VISION_MODEL_TIMEOUT_SECONDS", "90")
        _config.provider_setting("VISION_MODEL_TIMEOUT_SECONDS", "90")
    finally:
        _config._override = original

    assert seen == ["VISION_MODEL_TIMEOUT_SECONDS"] * 2, (
        f"覆盖层不是每次调用都读({seen})—— 设置页改完不会立刻生效"
    )


def test_the_download_timeout_in_the_budget_matches_the_one_actually_used():
    """预算里的单张下载超时必须等于 httpx 真正用的那个数。

    两处各写各的是这一整轮 bug 的通用形状(接口 8 张 / 预算 4 张)。
    这里不能 import `generation_tasks`(要 sqlalchemy),所以静态比对。
    """
    tree = _tree(GENERATION_TASKS)
    actual = next(
        node.value.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(t, ast.Name) and t.id == "DOWNLOAD_TIMEOUT_SECONDS"
            for t in node.targets
        )
        and isinstance(node.value, ast.Constant)
    )
    assert float(actual) == phase_budget.DOWNLOAD_TIMEOUT_SECONDS, (
        f"下载超时:实际用 {actual}s,预算按 {phase_budget.DOWNLOAD_TIMEOUT_SECONDS}s 算"
    )


def test_scoring_beats_once_per_candidate_inside_the_loop():
    """心跳必须在**循环里面**。

    放在循环外面(哪怕前后各一次)修不了 NEW-03:间隔本来就出在两张图之间,
    一张图最多 270 秒,8 张就是 2160 秒的静默。
    """
    fn = _fn(EVALUATION_SERVICE, "evaluate_round")
    assert "heartbeat" in {a.arg for a in fn.args.kwonlyargs}, (
        "`evaluate_round` 没有 heartbeat 参数 —— 编排层没有办法在评分中途续命"
    )
    loop = next(
        node
        for node in ast.walk(fn)
        if isinstance(node, ast.For) and "candidates" in ast.unparse(node.iter)
    )
    assert _calls(loop, "heartbeat"), (
        "逐张评分的循环里没有调用 heartbeat —— 8 张图会有 2160 秒完全没有心跳"
    )


def test_the_heartbeat_renews_the_lease_commits_and_fences():
    """一次心跳要同时做三件事,少一件就有一种失败模式没被覆盖。

        updated_at         对回收器说"我还活着"          少了 -> 误杀
        phase_lease_until  对其它 worker 说"这里有人"    少了 -> 重复评分
        rowcount 检查      对自己说"你已经不是持有者了"   少了 -> 旧 worker 盖掉新结论
    """
    fn = _fn(GENERATION_TASKS, "_heartbeat")
    src = ast.unparse(fn)
    assert "updated_at" in src, "心跳没有推进 updated_at —— 回收器仍然会误杀"
    assert "phase_lease_until" in src, "心跳没有给租约续期"
    assert "phase_lease_owner ==" in src, (
        "续期没有带 owner 条件 —— 那不是续自己的租约,是把别人的抢过来"
    )
    assert "commit" in src, (
        "心跳没有提交 —— 这一行只存在于自己的事务里,回收器看到的还是旧值"
    )
    assert "_LeaseLost" in src, "租约已经易主时没有抛信号,旧 worker 会继续写"


def test_losing_the_lease_stops_the_worker_without_writing_a_conclusion():
    """拿到 `_LeaseLost` 的唯一正确动作是**安静退出**。

    落 FAILED 是最坏的选择:现在做主的是新持有者,它可能正要提交一个成功
    结论,而这个 FAILED 会把它盖掉 —— 那正是 fencing 要防的事情本身。
    """
    fn = _fn(GENERATION_TASKS, "run_generation_task")
    handler = next(
        (
            node
            for node in ast.walk(fn)
            if isinstance(node, ast.ExceptHandler)
            and node.type is not None
            and "_LeaseLost" in ast.unparse(node.type)
        ),
        None,
    )
    assert handler is not None, (
        "入口没有单独处理 `_LeaseLost` —— 它会掉进通用异常分支被落成 FAILED,"
        "而那会盖掉新持有者的结论"
    )
    body = ast.unparse(handler)
    assert "_fail" not in body, "租约丢失时落了 FAILED —— 会盖掉新 worker 的结论"
    assert "transition" not in body, "租约丢失时还在改状态"


def test_the_first_round_scoring_also_runs_under_a_lease():
    """首轮评分也要有租约,而且要在**转 SCORING 之前**抢。

    上一版只有续跑分支抢租约,首轮从 DOWNLOADING 直接转 SCORING,整段裸奔:
    一条重复消息就能让 worker B 走续跑分支、抢到那把没人持有的租约,
    然后同一批图被两个 worker 同时送进视觉模型。

    抢在转移**之后**的话仍然留一道缝,所以这里断言的是行号顺序。
    """
    fn = _fn(GENERATION_TASKS, "_await_and_collect")
    claim = _first_line(
        fn,
        lambda n: isinstance(n, ast.Call)
        and "_claim_phase" in ast.unparse(n.func)
        and "DOWNLOADING" in ast.unparse(n),
    )
    to_scoring = _first_line(
        fn,
        lambda n: isinstance(n, ast.Call)
        and "transition" in ast.unparse(n.func)
        and "SCORING" in ast.unparse(n),
    )
    assert claim is not None, (
        "首轮进 SCORING 之前没有抢阶段租约 —— 重复消息可以并发评同一批图"
    )
    assert to_scoring is not None, "转 SCORING 的调用不见了?"
    assert claim < to_scoring, (
        f"抢租约在第 {claim} 行、转 SCORING 在第 {to_scoring} 行 —— "
        "顺序反了就留下一道缝,而重复投递是随时会发生的事"
    )


def test_the_reaper_leaves_tasks_that_still_hold_a_live_lease_alone():
    """心跳之外的第二个独立信号:租约还活着就别回收。

    心跳判据有个前提 —— worker 活着就一定推得动 `updated_at`。库抖一下时
    这个前提不成立,而回收器接着就会把一个正在花钱评分的任务落成 FAILED。
    """
    src = GENERATION_SERVICE.read_text(encoding="utf-8")
    assert "_lease_is_not_alive" in src, "回收器没有看阶段租约"
    reap = ast.unparse(_fn(GENERATION_SERVICE, "_reap_batch"))
    assert "_lease_is_not_alive" in reap, (
        "回收的那条 UPDATE 没有排除还持有租约的行 —— 正在跑的任务仍然会被误杀"
    )


def test_the_stall_window_is_resolved_when_the_reaper_runs_not_when_it_imports():
    """默认参数不能再写成 `= STALL_THRESHOLD_SECONDS`。

    Python 的默认参数在 import 那一刻求值。运营在设置页把超时调大之后,
    worker 进程会一直按旧值回收,直到有人重启它。
    """
    for name in ("find_stalled", "reap_stalled"):
        fn = _fn(GENERATION_SERVICE, name)
        for default in fn.args.kw_defaults:
            if default is None:
                continue
            assert isinstance(default, ast.Constant) and default.value is None, (
                f"`{name}` 的阈值默认值是 {ast.unparse(default)} —— "
                "它在 import 时就被冻住了,设置页改了也不生效"
            )
        assert "_resolve_stall_window" in ast.unparse(fn), (
            f"`{name}` 没有在运行时解析阈值"
        )
