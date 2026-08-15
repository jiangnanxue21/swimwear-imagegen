"""A45 batch12-5:费用幂等与阶段租约协议,需要真实 PostgreSQL。

`tests/pure/test_a45_batch12_5_fixes.py` 钉的是**结构**(键传没传、释放排在
派发前面没有、心跳在不在循环里)。这一批钉的是**行为**,而且每一条都对着
报告里那三条 P1 的复现路径:

    NEW-01  全部下载失败 -> 恢复成功。断言 submit 次数 1、submit 计费流水 1 条、
            计费单位等于 Provider 实际产出。上一版这里是 2 条、6 个单位。
    NEW-02  评分重排。断言新消息投出去之后**真的有人把它跑完**。
            上一版新 worker 抢不到租约、静默退出,任务停在 SCORING。
    NEW-03  长阶段的心跳、续期与 fencing。断言正在评分的任务扛得住回收器,
            以及被接管的旧 worker 一个字都写不进去。

## 为什么 NEW-02 的断言是"跑完了",不是"顺序对"

顺序在纯测试里已经用行号钉过了。这里要回答的是另一个问题:顺序对了之后,
那条消息是不是真的被执行了。上一版最难受的地方正是**每一层都认为自己成功了**
——派发成功、消费成功、退出正常——只有任务本身停在原地。所以这里只认终态。

`celery_eager` 让派发在同一个调用栈里内联跑完,于是"新 worker 能不能接手"
在同步测试里就能观察到。
"""
from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import text

from app.core.enums import PROVIDER_RESULT_PENDING_CODE, TaskStatus
from app.models.generation import (
    GenerationAttempt,
    GenerationTask,
    ProviderUsageRecord,
)
from app.providers.errors import ProviderRateLimitError
from app.providers.mock import MockImageGenerationProvider
from app.services import generation_service as gs
from tests.conftest import requires_db

pytestmark = requires_db

SPU_CODE = "SPU-B125"
PRODUCT = {"spu": SPU_CODE, "name": "batch12-5 回归泳衣"}


def _image(w=800, h=1200) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (30, 110, 140)).save(buf, format="JPEG")
    return buf.getvalue()


def _ensure_spu(client) -> None:
    """先建 SPU 档,再往下挂 SKU。理由与 12-4 那份逐字相同。

    A45-batch14-26 起 `create_product` 会解析 `spu` 字符串:查 `spus` 表,
    查不到就 422(「SPU xx 不存在:请先用 POST /spus 建档」)。这批 fixture
    写在那次契约变更之前,于是 11 条(本文件 + 12-4)统一挂在这一处。

    **这两份 helper 是刻意复制的,不抽到 conftest。** 两个文件用的是不同的
    SPU 编码(`SPU-B125` / `SPU-REG`),抽公共 helper 就要多一个参数,
    而它唯一的作用是让两个不相干的批次共享一段夹具 —— 那正是将来改其中一批
    会连带改动另一批的路径。这批用例的价值在于彼此独立地钉住同一条链路。

    每条用例都要建(`conftest.session` 跑在 SAVEPOINT 上,结束回滚);
    同一条用例内重复调用返回 409,那也是"已经有了",一并放行。
    `ONE_SIZE` + 单颜色只展开一行噪音 SKU(`SPU-B125-B125-OS`),
    与用例自建的编码切得开(SPU 段允许横线,颜色与尺码段不允许)。
    """
    resp = client.post(
        "/api/spus",
        json={
            "spu_code": SPU_CODE,
            "internal_name": "batch12-5 回归 SPU",
            "audience": "WOMEN",
            "color_variants": [{"variant_code": "B125", "working_name": "回归色"}],
            "size_template": "ONE_SIZE",
        },
    )
    assert resp.status_code in (201, 409), resp.text


def _product_with_asset(client, sku: str) -> str:
    _ensure_spu(client)
    resp = client.post("/api/products", json={**PRODUCT, "sku": sku})
    # 先断状态码再取 id:原来 `.json()["id"]` 在建品失败时抛 KeyError('id'),
    # 后端那句「请先用 POST /spus 建档」一个字都到不了报告里
    assert resp.status_code == 201, resp.text
    pid = resp.json()["id"]
    client.post(
        f"/api/products/{pid}/assets",
        files={"file": ("front.jpg", _image(), "image/jpeg")},
        data={"asset_type": "GARMENT_FRONT"},
    )
    client.post(
        f"/api/products/{pid}/assets",
        files={"file": ("model.jpg", _image(801, 1200), "image/jpeg")},
        data={"asset_type": "MODEL_REFERENCE"},
    )
    return pid


def _usable_model_template(client) -> str:
    rows = client.get(
        "/api/model-templates", params={"enabled_only": True, "audience": "WOMEN"}
    )
    assert rows.status_code == 200, rows.text
    if rows.json():
        return rows.json()[0]["id"]
    response = client.post(
        "/api/model-templates",
        files={"file": ("billing-model.jpg", _image(801, 1200), "image/jpeg")},
        data={"name": "计费回归模特", "audience": "WOMEN"},
    )
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _create_task(client, pid, **overrides):
    payload = {
        "product_id": pid,
        "mode": "virtual_try_on",
        "provider": "mock",
        "model_template_id": _usable_model_template(client),
        "candidate_count": 3,
        **{"provider_params": {"mock_evaluator": {"outcome": "a"}}, **overrides},
    }
    return client.post("/api/generation-tasks", json=payload)


@pytest.fixture
def pinned_mock_provider(monkeypatch):
    """把 `generation_tasks.get_provider("mock")` 钉在同一个实例上并数 submit。

    与 `test_a45_batch12_4_recovery_db.py` 里那个同名夹具是同一件事:Mock 没有
    服务端,状态全在实例内存里,不钉住的话"续跑"测到的是环境限制而不是修复。
    """
    import app.tasks.generation_tasks as gt
    from app.providers.registry import get_provider as real_get_provider

    instance = MockImageGenerationProvider()
    submit_calls: list[str] = []
    real_submit = instance.submit

    async def _counted_submit(request):
        external_id = await real_submit(request)
        submit_calls.append(external_id)
        return external_id

    monkeypatch.setattr(instance, "submit", _counted_submit)

    def _pinned(name: str):
        if (name or "").strip().lower() == "mock":
            return instance
        return real_get_provider(name)

    monkeypatch.setattr(gt, "get_provider", _pinned)
    return submit_calls


def _usage(session, task_id, operation: str | None = None) -> list[ProviderUsageRecord]:
    query = session.query(ProviderUsageRecord).filter(
        ProviderUsageRecord.task_id == task_id
    )
    if operation is not None:
        query = query.filter(ProviderUsageRecord.operation == operation)
    return list(query.order_by(ProviderUsageRecord.created_at))


class _EvaluatorProxy:
    """真评分器外面套一层钩子。**除了钩子,一切照旧。**

    用代理而不是替身:`_billing()` 会读 `billable` / `usage_provider`,
    决策链路会读 `evaluator_name`,换成假对象等于把这些一起改掉,
    而它们正是这几条用例要保持真实的部分。
    """

    def __init__(self, inner, before=None, raises=None):
        self._inner = inner
        self._before = before
        self._raises = raises

    def __getattr__(self, name):
        return getattr(self._inner, name)

    async def evaluate_structured(self, *args, **kwargs):
        if self._before is not None:
            self._before()
        if self._raises is not None:
            raise self._raises()
        return await self._inner.evaluate_structured(*args, **kwargs)


def _patch_evaluator(monkeypatch, factory):
    import app.services.evaluation_service as es
    from app.evaluators.registry import get_active_evaluator as real

    monkeypatch.setattr(es, "get_active_evaluator", lambda *a, **k: factory(real()))


def _fail_every_download(monkeypatch):
    import app.tasks.generation_tasks as gt
    from app.providers.errors import ResultDownloadError

    def _boom(item):
        raise ResultDownloadError("模拟下载失败(batch12-5 回归测试)")

    monkeypatch.setattr(gt, "_load_bytes", _boom)


# ==========================================================
# NEW-01:一笔生成 = 一条计费流水
# ==========================================================


def test_recovering_a_failed_download_bills_the_generation_exactly_once(
    client, celery_eager, session, monkeypatch, pinned_mock_provider
):
    """报告第四节点名要求的那组断言,一条不少。

        Provider submit 次数      = 1
        submit 类型费用记录数      = 1
        billable_units 总计       = candidate_count
        attempt 数                = 1

    上一版这里是 2 条流水、6 个计费单位 —— 而 Provider 只跑过一次。
    """
    import app.tasks.generation_tasks as gt
    from app.providers.errors import ResultDownloadError

    fail = {"on": True}
    real_load_bytes = gt._load_bytes

    def _conditional_boom(item):
        if fail["on"]:
            raise ResultDownloadError("模拟下载失败(batch12-5 回归测试)")
        return real_load_bytes(item)

    monkeypatch.setattr(gt, "_load_bytes", _conditional_boom)

    pid = _product_with_asset(client, "SKU-B125-BILL")
    task_id = _create_task(client, pid, candidate_count=3).json()["task"]["id"]

    assert client.get(f"/api/generation-tasks/{task_id}").json()["error_code"] == (
        PROVIDER_RESULT_PENDING_CODE
    ), "前提不成立:全部下载失败没有落成可续跑的码"

    first_pass = _usage(session, task_id, "submit")
    assert len(first_pass) == 1, "第一遍就记了不止一条 submit 流水"
    assert first_pass[0].succeeded is False
    assert first_pass[0].billable_units == 3, (
        "第一遍的计费单位不是 Provider 实际产出的张数 —— 钱是按 3 张收的"
    )

    # 修好下载,续跑
    fail["on"] = False
    assert client.post(f"/api/generation-tasks/{task_id}/retry").status_code == 200

    detail = client.get(f"/api/generation-tasks/{task_id}").json()
    assert detail["status"] == "COMPLETED", f"续跑没跑完:{detail.get('error_code')}"

    assert len(pinned_mock_provider) == 1, (
        f"submit() 被调用了 {len(pinned_mock_provider)} 次 —— 外部重复提交复发了"
    )
    assert len(detail["attempts"]) == 1, "续跑开了第二条 attempt"

    rows = _usage(session, task_id, "submit")
    assert len(rows) == 1, (
        f"submit 类型的费用流水有 {len(rows)} 条 —— 同一笔外部生成被记了两次账"
        f"(NEW-01)。计费单位合计 {sum(r.billable_units for r in rows)}"
    )
    row = rows[0]
    assert row.billable_units == 3, (
        f"计费单位是 {row.billable_units},期望 3(Provider 实际产出的张数)"
    )
    assert row.succeeded is True, (
        "恢复成功之后那条流水还写着失败 —— 失败率统计会一直把它算成一次失败调用"
    )
    assert row.error_code is None, "恢复成功了,错误码却还留在费用流水上"
    assert row.candidate_count == 3, "候选数没有跟着恢复后的结果更新"


def test_a_second_record_with_the_same_identity_updates_the_first_one(session):
    """直接对着 `record_usage` 验幂等语义,不绕整条流水线。

    整条链路那条用例证明"这次修好了",这一条证明**机制本身**是对的:
    同一个键第二次进来必须更新,而且计费单位只增不减。
    """
    from app.models.product import Product

    product = Product(spu="SPU-KEY", sku="SKU-KEY", name="计费键")
    session.add(product)
    session.flush()
    task = GenerationTask(
        product_id=product.id, mode="virtual_try_on", provider="mock",
        candidate_count=3, max_rounds=3, status=TaskStatus.PROVIDER_RUNNING.value,
        idempotency_key="billing-key-unit",
    )
    session.add(task)
    session.flush()
    attempt = GenerationAttempt(
        task_id=task.id, round_number=1, attempt_number=1, provider="mock"
    )
    session.add(attempt)
    session.flush()

    key = gs.submit_billing_key(attempt.id)
    gs.record_usage(
        session, provider="mock", task_id=task.id, attempt_id=attempt.id,
        operation="submit", succeeded=False, candidate_count=0,
        error_code=PROVIDER_RESULT_PENDING_CODE, billable_units=3, billing_key=key,
    )
    session.flush()
    gs.record_usage(
        session, provider="mock", task_id=task.id, attempt_id=attempt.id,
        operation="submit", succeeded=True, candidate_count=3,
        error_code=None, billable_units=2, billing_key=key,
    )
    session.flush()

    rows = _usage(session, task.id, "submit")
    assert len(rows) == 1, f"同一个计费身份记出了 {len(rows)} 行"
    assert rows[0].succeeded is True, "结论没有被更新"
    assert rows[0].billable_units == 3, (
        "第二次传了更小的单位数,把已经记上的账改小了 —— "
        "钱是按第一次实际产出收的,只能增不能减"
    )


def test_the_database_refuses_a_second_row_with_the_same_billing_identity(session):
    """应用层认领之外,库里那条唯一约束必须真的存在。

    没有它,下一个写新记账路径的人漏掉键的后果是一批看不出来的重复流水;
    有它,后果是一次 IntegrityError —— 后者能在测试里被发现。

    用 ORM 而不是手写 INSERT:那张表的非空列和默认值会变,手抄一份列清单
    等于给这条用例埋一个"和模型漂移之后才炸"的雷,而它炸的时候看起来像是
    约束坏了。
    """
    from sqlalchemy.exc import IntegrityError

    def _row() -> ProviderUsageRecord:
        return ProviderUsageRecord(
            provider="mock", operation="submit", succeeded=True,
            candidate_count=0, billable_units=1, billing_key="dup-key-probe",
        )

    session.add(_row())
    session.flush()
    session.add(_row())
    with pytest.raises(IntegrityError):
        session.flush()
    session.rollback()


def test_calls_without_a_billing_identity_still_get_one_row_each(
    client, celery_eager, session, monkeypatch, pinned_mock_provider
):
    """反方向:轮询、取结果、评分**不能**被折叠。

    那几类每一次都是真的又调了一次 —— 给它们加幂等键会让台账少记,
    而少记比多记更难发现。这里用两次全部下载失败(不恢复)来制造
    "同一条 attempt 上有两遍 download 收尾"的形状。
    """
    _fail_every_download(monkeypatch)

    pid = _product_with_asset(client, "SKU-B125-NOKEY")
    task_id = _create_task(client, pid, candidate_count=2).json()["task"]["id"]
    assert client.post(f"/api/generation-tasks/{task_id}/retry").status_code == 200

    submit_rows = _usage(session, task_id, "submit")
    assert len(submit_rows) == 1, (
        f"两遍下载失败记了 {len(submit_rows)} 条 submit 流水 —— 幂等没生效"
    )
    assert submit_rows[0].billable_units == 2, "两遍失败把计费单位累加了"

    # 评分那一类没有键:一张图评两次就是两次真实计费,必须是两行。
    # 这一轮没有走到评分(下载全败),所以只断言"没有被键折叠"这条契约
    # 由纯测试守着;这里确认的是 submit 之外的行不受幂等影响。
    assert all(
        row.billing_key is None
        for row in _usage(session, task_id)
        if row.operation != "submit"
    ), "submit 之外的流水被加上了计费身份"


# ==========================================================
# NEW-02:派发出去的评分消息必须真的有人跑完
# ==========================================================


def test_rescheduling_scoring_hands_the_lease_over_so_the_next_worker_can_run(
    client, celery_eager, session, monkeypatch, pinned_mock_provider
):
    """第一次评分整轮限流 -> 重排 -> 新 worker 接手 -> 跑完。

    这是 NEW-02 的复现路径。上一版的结果是任务停在 SCORING:新 worker 收到
    消息、`_claim_phase()` 撞上旧租约、返回 claimed=False,而 Outbox 已经
    标成 DISPATCHED 不会再投 —— 三十分钟后由回收器判成卡死。

    这里只认终态:COMPLETED。中间那些"看起来成功"的信号正是上一版的问题。
    """
    calls = {"n": 0}

    def _rate_limited_first_round(inner):
        def _before():
            calls["n"] += 1

        # 第一轮的每一张都限流(可重试的瞬时故障),之后放行
        return _EvaluatorProxy(
            inner,
            before=_before,
            raises=(
                (lambda: ProviderRateLimitError("模拟评分器限流"))
                if calls["n"] < 2
                else None
            ),
        )

    _patch_evaluator(monkeypatch, _rate_limited_first_round)

    pid = _product_with_asset(client, "SKU-B125-RESCHED")
    task_id = _create_task(client, pid, candidate_count=2).json()["task"]["id"]

    detail = client.get(f"/api/generation-tasks/{task_id}").json()
    assert detail["status"] != TaskStatus.SCORING.value, (
        "任务停在 SCORING —— 评分消息投出去了却没有人能执行它(NEW-02)。"
        "新 worker 多半是被旧 worker 还没还回去的阶段租约挡住了"
    )
    assert detail["status"] == "COMPLETED", (
        f"重排之后没有跑完,停在 {detail['status']}({detail.get('error_code')})"
    )
    assert len(pinned_mock_provider) == 1, (
        "评分重排把图重新生成了一遍 —— 评分失败绝不允许重新花钱"
    )

    task = session.get(GenerationTask, task_id)
    session.refresh(task)
    assert task.phase_lease_owner is None, "跑完了租约还挂着"


# ==========================================================
# NEW-03:心跳、续期、fencing
# ==========================================================


def test_a_scoring_worker_heartbeats_and_survives_the_reaper(
    client, celery_eager, session, monkeypatch, pinned_mock_provider
):
    """正在评分的任务:每张图之间有心跳、租约在续,回收器碰不了它。

    ## 这条用例同时钉住三件事

    1. **首轮评分也持有租约**(上一版只有续跑分支有,首轮整段裸奔);
    2. 每张图之间 `updated_at` 真的在推进 —— 没有它,8 张图会有 2160 秒静默,
       而阈值是 1800;
    3. 回收器看到活着的租约就不动手 —— 这是心跳之外的第二个独立信号,
       专治"库抖一下导致心跳丢了一拍"。

    第 3 条的注入方式是把 `updated_at` 拨回一小时,也就是**假装心跳全丢了**。
    上一版在这个现场下会把任务落成 FAILED。
    """
    seen: list[tuple] = []

    def _probe(inner):
        def _before():
            row = session.execute(
                text(
                    "SELECT status, updated_at, phase_lease_owner, phase_lease_until "
                    "FROM generation_tasks WHERE status = 'SCORING'"
                )
            ).first()
            if row is not None:
                seen.append(tuple(row))

        return _EvaluatorProxy(inner, before=_before)

    _patch_evaluator(monkeypatch, _probe)

    pid = _product_with_asset(client, "SKU-B125-BEAT")
    task_id = _create_task(client, pid, candidate_count=3).json()["task"]["id"]
    assert client.get(f"/api/generation-tasks/{task_id}").json()["status"] == "COMPLETED"

    assert len(seen) >= 3, f"只观察到 {len(seen)} 次评分现场,期望每张图一次"
    owners = {row[2] for row in seen}
    assert None not in owners, (
        "首轮评分期间没有人持有阶段租约 —— 一条重复消息就能让两个 worker "
        "同时把这批图送进视觉模型"
    )
    assert len(owners) == 1, f"评分中途换过持有者:{owners}"

    beats = [row[1] for row in seen]
    assert beats == sorted(beats) and beats[0] < beats[-1], (
        "逐张之间 updated_at 没有推进 —— 回收器眼里这一整段是「没有动静」"
    )
    leases = [row[3] for row in seen]
    assert leases[0] < leases[-1], "租约没有在续期 —— 长阶段跑到一半就会被接管"

    # 假装心跳全丢了:把 updated_at 拨回一小时,但租约还活着
    task = session.get(GenerationTask, task_id)
    session.execute(
        text(
            "UPDATE generation_tasks SET status = 'SCORING', "
            "updated_at = now() - interval '1 hour', "
            "phase_lease_owner = 'still-working', "
            "phase_lease_until = now() + interval '10 minutes' WHERE id = :id"
        ),
        {"id": str(task.id)},
    )
    session.commit()

    reaped = gs.reap_stalled(session, older_than_seconds=60)
    session.commit()
    assert str(task.id) not in reaped, (
        "回收器把一个还持有活租约的任务判成了卡死 —— 而它可能正在花钱评分。"
        "误杀的代价是重试时再买一次"
    )

    # 对照:租约过期之后它必须能被正常回收,否则这道护栏会变成"永远收不掉"
    session.execute(
        text(
            "UPDATE generation_tasks SET phase_lease_until = now() - interval '1 minute' "
            "WHERE id = :id"
        ),
        {"id": str(task.id)},
    )
    session.commit()
    assert str(task.id) in gs.reap_stalled(session, older_than_seconds=60), (
        "租约都过期了还收不掉 —— 那这条护栏会把死掉的任务永远挡在回收之外"
    )
    session.commit()


def test_a_worker_that_lost_its_lease_writes_nothing_further(
    client, celery_eager, session, monkeypatch, pinned_mock_provider
):
    """租约被接管之后,旧 worker 必须立刻停手 —— 不落 FAILED、不改状态。

    这是报告要的那条:"如果 B 已经接管,A 的后续写入必须因 fencing token
    失效而失败"。上一版对这种情况没有任何防御:owner 只在**还**租约的时候
    被检查,业务写入一律不检查,于是旧 worker 会把自己那份结论照常提交上去,
    盖掉新持有者正在写的东西。
    """
    stolen = {"done": False}

    def _steal_after_first_candidate(inner):
        def _before():
            if stolen["done"]:
                return
            stolen["done"] = True
            # 模拟"另一个 worker 接管了这个阶段"。真实场景里这发生在租约过期
            # 之后,这里直接改 owner —— 对被测代码来说两者完全一样:
            # 它下一次续期时会发现 owner 已经不是自己
            session.execute(
                text(
                    "UPDATE generation_tasks SET phase_lease_owner = 'takeover-worker', "
                    "phase_lease_until = now() + interval '15 minutes' "
                    "WHERE status = 'SCORING'"
                )
            )
            session.commit()

        return _EvaluatorProxy(inner, before=_before)

    _patch_evaluator(monkeypatch, _steal_after_first_candidate)

    pid = _product_with_asset(client, "SKU-B125-FENCE")
    task_id = _create_task(client, pid, candidate_count=3).json()["task"]["id"]

    assert stolen["done"], "没能摆出「评分中途被接管」的现场,这条用例的前提不成立"

    task = session.get(GenerationTask, task_id)
    session.refresh(task)
    assert task.status == TaskStatus.SCORING.value, (
        f"被接管之后旧 worker 仍然把任务写成了 {task.status} —— "
        "fencing 没生效,它盖掉的可能是新持有者刚写下的结论"
    )
    assert task.error_code is None, (
        f"旧 worker 落了错误码 {task.error_code} —— 租约丢失不是失败,"
        "现在做主的是新持有者,这个码会让运营去处理一个并不存在的故障"
    )
    assert task.phase_lease_owner == "takeover-worker", (
        "旧 worker 把新持有者的租约释放掉了 —— 释放必须带 owner 条件"
    )
