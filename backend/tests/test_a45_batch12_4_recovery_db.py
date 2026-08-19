"""A45 batch12-4:跨函数故障注入,需要真实 PostgreSQL。

`tests/pure/` 那批守卫钉的是**结构**(保存点在不在、判据读的是哪个变量)。
报告对上一轮的批评是准确的:结构对了不等于行为对,「1824 条纯测试全绿
同时仍然存在自动重复扣费」正是因为没有一条测试真的走完
"故障发生 -> reaper/续跑 -> 下一次生成" 这整条链路。

这一批补的就是那条链路,而且每条用例最后都断言同一件事:
**Provider `submit()` 的调用次数,全程保持 1。**

## Mock Provider 的一个环境限制,以及这里怎么绕开它

`get_provider("mock")` 每次调用都构造一个**新实例**——真实 Provider 靠自己
服务端持久化任务状态,`external_task_id` 是一把能查到对方数据的钥匙;
Mock 没有服务端,状态全在进程内存的 `_jobs` 字典里,新实例查不到旧任务。

这对真实 Provider 不是问题,但会让"续跑"在测试里失真:如果每次都构造新
Mock 实例,`get_status`/`fetch_results` 会返回"找不到这个任务",那验证的
是环境限制,不是这份修复。

所以这批用例用 `_pinned_mock_provider` 把 `generation_tasks.get_provider`
钉在同一个实例上——这正是真实 Provider 天然具备、Mock 需要模拟出来的那件事
(服务端记得住自己收过的任务)。这是测试基础设施的调整,不是被测代码的改动。
"""
from __future__ import annotations

import io

import pytest
from PIL import Image
from sqlalchemy import text

from app.core.enums import (
    PROVIDER_RESULT_PENDING_CODE,
    SUBMIT_UNKNOWN_CODE,
    AttemptStatus,
    TaskStatus,
)
from app.models.generation import GenerationAttempt, GenerationCandidate, GenerationTask
from app.providers.mock import MockImageGenerationProvider
from app.services import generation_service as gs
from tests.conftest import requires_db

pytestmark = requires_db

SPU_CODE = "SPU-REG"
PRODUCT = {"spu": SPU_CODE, "name": "回归测试泳衣"}


def _image(w=800, h=1200) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (w, h), (40, 90, 120)).save(buf, format="JPEG")
    return buf.getvalue()


def _ensure_spu(client) -> None:
    """先建 SPU 档,再往下挂 SKU。

    ## 为什么这一步在这批用例写好之后才需要

    A45-batch14-26 关掉了 §4.2 那条 NULL 兼容缝:`product_service.create_product`
    现在**解析** `spu` 字符串 —— 查 `spus` 表,查不到就 422
    (「款号 xx 还没有建过:请先在款式列表里新建这个款…」)。
    在那之前这个函数从不写 `spu_id`,于是走 `POST /api/products` 建出来的商品
    不属于任何 SPU、`audience` 可空,绕过了「受众必填」。

    这批 fixture 写在那次契约变更之前,11 条(本文件 + 12-5)全挂在同一处,
    统一返回 422。**这是老 fixture 没跟上新契约,不是基础设施问题** ——
    同批次里不碰 `/api/products` 的 `test_batch_lease_concurrency_db.py` 是绿的。

    ## 为什么每条用例都要建,不能提到模块级

    `conftest.session` 跑在 SAVEPOINT 上(A42),teardown 回滚整个外层事务,
    用例之间不留数据。模块级建一次的话,第二条用例开始就又是 422 了。

    重复调用返回 409(`create_spu` 的 `_code_taken` 快速路径),那也是
    "已经有了",一并放行 —— 同一条用例里建两个商品时会走到这条路。

    ## 为什么是 ONE_SIZE + 单颜色

    建档会按尺码模板展开 SKU。`ONE_SIZE` × 一个颜色只展开一行
    (`SPU-REG-REG-OS`),噪音最小。它和用例自建的 `SKU-REG01-A` 那些不撞:
    SPU 段允许横线、颜色与尺码段不允许(`sku_matrix.normalize_code`),
    所以从右边数两段切得开,两套编码不会拼出同一个 SKU。
    换成 ALPHA_3 只是让每条用例凭空多三行商品。
    """
    resp = client.post(
        "/api/spus",
        json={
            "spu_code": SPU_CODE,
            "internal_name": "回归测试 SPU",
            # 受众在 SPU 层必填(§4.2),而且 `create_product` 会把它抄进
            # 商品行 —— 商品那边的 `garment_type` 默认 OTHER,对三个受众恒许,
            # 所以这里选哪个都不会撞上 C-03 那道受众 × 品类闸
            "audience": "WOMEN",
            "color_variants": [{"variant_code": "REG", "working_name": "回归色"}],
            "size_template": "ONE_SIZE",
        },
    )
    assert resp.status_code in (201, 409), resp.text


def _product_with_asset(client, sku: str) -> str:
    _ensure_spu(client)
    resp = client.post("/api/products", json={**PRODUCT, "sku": sku})
    # 状态码先断言再取 id:原来直接 `.json()["id"]`,建品失败时抛的是
    # KeyError('id'),既不点名是哪一步、也看不到后端给的那句话。
    # 这批用例这次全红了一轮,报文就在响应体里,而堆栈里一个字都没有
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
        files={"file": ("recovery-model.jpg", _image(801, 1200), "image/jpeg")},
        data={"name": "恢复回归模特", "audience": "WOMEN"},
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
    """把 `generation_tasks.get_provider("mock")` 钉在同一个实例上。

    见模块 docstring:这是在补 Mock 相对真实 Provider 缺的那一层
    "服务端记得住任务"，不是在放宽被测代码的行为。
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


def _fail_every_download(monkeypatch):
    """让这一轮的每一张候选图都在"下载"这一步失败。

    Mock 直接内联字节、不走 HTTP,所以要让下载失败得在 `_load_bytes` 这层动手——
    这与真实 Provider 的 S3 抖动、签名过期落在同一个位置。
    """
    import app.tasks.generation_tasks as gt
    from app.providers.errors import ResultDownloadError

    def _boom(item):
        raise ResultDownloadError("模拟下载失败(REG-01 回归测试)")

    monkeypatch.setattr(gt, "_load_bytes", _boom)


# ==========================================================
# REG-01:全部下载失败 -> 不许自动买第二次
# ==========================================================


def test_all_downloads_failing_keeps_the_external_id_and_does_not_resubmit(
    client, celery_eager, session, monkeypatch, pinned_mock_provider
):
    """核心断言:全部下载失败之后,`submit()` 只被调用过一次。

    旧行为下这条用例应该失败:落库返回空列表会被当成"没有候选图",
    自动转 REGENERATING 并派发下一轮,`pinned_mock_provider` 记录的
    submit 次数会变成 2。
    """
    _fail_every_download(monkeypatch)

    pid = _product_with_asset(client, "SKU-REG01-A")
    task_id = _create_task(client, pid, candidate_count=3).json()["task"]["id"]

    detail = client.get(f"/api/generation-tasks/{task_id}").json()
    assert detail["status"] == "FAILED"
    assert detail["error_code"] == PROVIDER_RESULT_PENDING_CODE, (
        f"落成了 {detail['error_code']} —— 全部下载失败被当成\"没有候选图\"了"
    )
    assert detail["external_task_id"], "外部 ID 被清空了 —— 续取的凭证丢了"
    assert len(pinned_mock_provider) == 1, (
        f"submit() 被调用了 {len(pinned_mock_provider)} 次 —— "
        "全部下载失败之后系统自己买了第二次生成"
    )

    # 候选行必须落 DOWNLOAD_FAILED,而不是干脆不存在 —— 运营要能看到"图生成了、
    # 只是没下下来",而不是一片空白
    assert len(detail["candidates"]) == 3
    assert all(c["status"] == "DOWNLOAD_FAILED" for c in detail["candidates"])


def test_resuming_after_all_downloads_failed_reuses_the_same_attempt(
    client, celery_eager, session, monkeypatch, pinned_mock_provider
):
    """续跑成功之后:还是那一条 attempt,候选数不多不少,submit 仍然是 1 次。

    这条钉的是 REG-01 与 REG-03 的组合:候选行按 `candidate_index` 认领,
    续跑不会在原来 3 条 DOWNLOAD_FAILED 之外再插 3 条新的。
    """
    import app.tasks.generation_tasks as gt
    from app.providers.errors import ResultDownloadError

    fail = {"on": True}
    real_load_bytes = gt._load_bytes

    def _conditional_boom(item):
        if fail["on"]:
            raise ResultDownloadError("模拟下载失败(REG-01 回归测试)")
        return real_load_bytes(item)

    monkeypatch.setattr(gt, "_load_bytes", _conditional_boom)

    pid = _product_with_asset(client, "SKU-REG01-B")
    task_id = _create_task(client, pid, candidate_count=3).json()["task"]["id"]
    assert client.get(f"/api/generation-tasks/{task_id}").json()["error_code"] == (
        PROVIDER_RESULT_PENDING_CODE
    )

    # 修好下载,续跑。`pinned_mock_provider` 那把 monkeypatch 不受影响 ——
    # 只切换下载这一个开关
    fail["on"] = False

    r = client.post(f"/api/generation-tasks/{task_id}/retry")
    assert r.status_code == 200, r.text

    detail = client.get(f"/api/generation-tasks/{task_id}").json()
    assert len(detail["attempts"]) == 1, (
        f"续跑之后 attempt 变成了 {len(detail['attempts'])} 条 —— "
        "应该是同一条 attempt 接着走完,不是新开一条"
    )
    assert detail["status"] == "COMPLETED"
    total = (
        session.query(GenerationCandidate)
        .filter(GenerationCandidate.task_id == task_id)
        .count()
    )
    assert total == 3, (
        f"续跑之后候选总数是 {total},期望恰好 3 —— 多出来的就是重复候选"
    )


# ==========================================================
# REG-02:reaper 分类 + retry_task + worker,submit 次数保持 1
# ==========================================================


def test_reaper_gives_a_resumable_task_its_id_back_and_worker_finishes_with_one_submit(
    client, celery_eager, session, monkeypatch, pinned_mock_provider
):
    """完整链路:submit 成功 -> worker 死于 PROVIDER_RUNNING -> reap_stalled
    -> retry_task(force=False) -> worker 续跑 -> COMPLETED,全程一次 submit。

    这是报告第八节点名要求的那条:"reaper -> retry_task -> worker,
    断言 Provider submit 总次数仍为 1"。

    ## 怎么让 worker "死"在 PROVIDER_RUNNING

    真的 kill 一个进程在同步的 `celery_eager` 里做不到,这里用等价的手法:
    把 `_await_and_collect` 换成一个只做**它前半段**(转移到 PROVIDER_RUNNING
    并提交)就返回的版本,不碰 `get_status` / `fetch_results`。

    这不是在放宽被测代码 —— `_run()` 走到调用 `_await_and_collect` 之前的
    全部代码(claim、建 attempt、submit、落 external_task_id、commit)都是
    真实路径原样跑的;被换掉的只是"接下来查状态、下结果"这一段,而**这正是
    worker 进程真的被 kill 时不会执行到的那一段**。库里留下的形状——
    `PROVIDER_RUNNING` + `external_task_id` 已落库 + attempt 仍是 `SUBMITTED`——
    和真实死亡完全一致。

    `resuming=True` 时这个替身让位给真实实现,所以续跑那一半测的是货真价实的
    `get_status` / `fetch_results` 对着共享的 mock 实例。
    """
    import app.tasks.generation_tasks as gt

    real_await_and_collect = gt._await_and_collect

    def _die_after_committing_provider_running(
        session, task, attempt, provider, external_id, *, started, resuming=False,
        lease=None,
    ):
        # `lease` 是 A45-batch12-5 / NEW-03 给真实函数加的形参(阶段租约要能
        # 一路传到评分那一层)。替身必须跟着收下它并原样转发,否则续跑那一半
        # 测到的就不是真实签名了 —— 而这份用例的价值全在"续跑走的是真实路径"。
        if resuming:
            return real_await_and_collect(
                session, task, attempt, provider, external_id,
                started=started, resuming=resuming, lease=lease,
            )
        gs.transition(session, task, TaskStatus.PROVIDER_RUNNING)
        session.commit()
        return {
            "task_id": str(task.id),
            "status": task.status,
            "simulated_worker_death": True,
        }

    monkeypatch.setattr(gt, "_await_and_collect", _die_after_committing_provider_running)

    pid = _product_with_asset(client, "SKU-REG02-A")
    task_id = _create_task(client, pid, candidate_count=2).json()["task"]["id"]

    task = session.get(GenerationTask, task_id)
    session.refresh(task)
    assert task.status == TaskStatus.PROVIDER_RUNNING.value, (
        "没能摆出「死于 PROVIDER_RUNNING」的现场 —— 这份用例的前提不成立"
    )
    assert task.external_task_id, "提交阶段没有落 external_task_id,前提不成立"
    attempt = (
        session.query(GenerationAttempt)
        .filter(GenerationAttempt.task_id == task.id)
        .one()
    )
    assert attempt.status == AttemptStatus.SUBMITTED.value, (
        "attempt 已经被收尾过了 —— 这不是「死掉」该有的样子"
    )
    submits_so_far = len(pinned_mock_provider)
    assert submits_so_far == 1

    # 把 updated_at 往回拨,落进 reaper 的回收窗口——真实场景里这是
    # "过了 STALL_THRESHOLD_SECONDS 没有心跳",这里直接跳过等待
    session.execute(
        text(
            "UPDATE generation_tasks SET updated_at = now() - interval '1 hour' "
            "WHERE id = :id"
        ),
        {"id": str(task.id)},
    )
    session.commit()

    # reaper
    reaped = gs.reap_stalled(session, older_than_seconds=60)
    session.commit()
    assert task_id in reaped

    session.refresh(task)
    assert task.status == TaskStatus.FAILED.value
    assert task.error_code == PROVIDER_RESULT_PENDING_CODE, (
        f"reaper 把它落成了 {task.error_code} —— "
        "有外部 ID 的等结果任务不该走 SUBMIT_RESULT_UNKNOWN 那条对账路径,"
        "那条路的出口(强制重试)会清掉 ID 重新 submit"
    )
    assert task.external_task_id, "reaper 把外部 ID 清掉了"

    # retry_task。**不带 force** —— PROVIDER_RESULT_PENDING 不要求它,
    # 这正是与 SUBMIT_RESULT_UNKNOWN 的区别所在
    r = client.post(f"/api/generation-tasks/{task_id}/retry")
    assert r.status_code == 200, (
        f"retry 被拒绝:{r.text} —— PROVIDER_RESULT_PENDING 不该要求 force"
    )

    # worker 续跑:这一次 resuming=True,走的是真实的 _await_and_collect,
    # 对着共享的 mock 实例把 get_status / fetch_results / 下载 / 评分走完
    final = client.get(f"/api/generation-tasks/{task_id}").json()
    assert final["status"] == "COMPLETED", (
        f"续跑没有跑完,停在 {final['status']}(错误码 {final.get('error_code')})"
    )
    assert final["external_task_id"] == task.external_task_id, (
        "续跑之后外部 ID 变了 —— 说明中途又清空重提交过一次"
    )
    assert len(final["attempts"]) == 1, (
        f"attempt 变成了 {len(final['attempts'])} 条 —— reaper/retry 链路里"
        "多开了一条新的 attempt,而新 attempt 只可能来自新一次 submit"
    )

    # 核心断言
    submits_after = len(pinned_mock_provider)
    assert submits_after == 1, (
        f"submit() 总共被调用了 {submits_after} 次 —— "
        "reap_stalled -> retry_task -> worker 这条链路里 REG-02 复发了"
    )


def test_reaper_still_sends_a_missing_id_task_through_reconciliation(
    client, session
):
    """反面对照:没有外部 ID 的等结果任务，仍然必须走对账，不能被当成可续跑。

    这一批不该存在（成因是落 ID 的那次 commit 崩了），但**它出现的时候**
    不能被误判成"可以安全续取"——那条任务没有 ID 可续。
    """
    # 这条用例不用 `_product_with_asset`(它不需要素材),但建档这一步
    # 是一样的 —— 新契约要求 SPU 先存在,漏掉这里就只有这一条还是 422
    _ensure_spu(client)
    pid_resp = client.post(
        "/api/products", json={**PRODUCT, "sku": "SKU-REG02-BLIND"}
    )
    assert pid_resp.status_code == 201, pid_resp.text
    pid = pid_resp.json()["id"]
    from app.models.product import Product

    product = session.get(Product, pid)
    task = GenerationTask(
        product_id=product.id,
        mode="virtual_try_on",
        provider="mock",
        candidate_count=2,
        max_rounds=3,
        status=TaskStatus.DOWNLOADING.value,
        external_task_id=None,
        idempotency_key=f"reg02-blind-{pid}",
    )
    session.add(task)
    session.flush()
    attempt = GenerationAttempt(
        task_id=task.id,
        round_number=1,
        attempt_number=1,
        provider="mock",
        status=AttemptStatus.SUBMITTED.value,
    )
    session.add(attempt)
    session.commit()
    session.execute(
        text(
            "UPDATE generation_tasks SET updated_at = now() - interval '1 hour' "
            "WHERE id = :id"
        ),
        {"id": str(task.id)},
    )
    session.commit()

    reaped = gs.reap_stalled(session, older_than_seconds=60)
    session.commit()
    assert str(task.id) in reaped

    session.refresh(task)
    assert task.error_code == SUBMIT_UNKNOWN_CODE, (
        f"没有外部 ID 却被判成 {task.error_code} —— 这一批连 ID 都没有，"
        "没有资格被当成可安全续取"
    )

    r = client.post(f"/api/generation-tasks/{task.id}/retry")
    assert r.status_code == 409, "SUBMIT_RESULT_UNKNOWN 不带 force 应该被拒绝"
    r = client.post(
        f"/api/generation-tasks/{task.id}/retry",
        params={"force": "true", "reconciliation_note": "已在 Provider 后台核对，无对应任务"},
    )
    assert r.status_code == 200


# ==========================================================
# REG-03:候选落库崩溃不许提交半截数据
# ==========================================================


def test_a_crash_mid_persist_leaves_zero_orphaned_candidates(
    client, celery_eager, session, monkeypatch, pinned_mock_provider
):
    """落库崩在候选已经 flush 之后时，候选表必须是 0 行，不是 3 行。
    留下的那几行就是 REG-03 复现的那个"半截数据"。

    ## 注入点为什么必须是影子写，不能是 `_load_bytes`

    `_load_bytes` 的调用点在 `_persist_candidates` 每张图那个
    `try / except Exception` **里面**：在那里抛异常会被就地吞掉、把该行标成
    `DOWNLOAD_FAILED`，根本冒不出 `_persist_candidates`，保存点一次都不会回滚。
    那样测到的是"单张下载失败不拖垮整轮"(REG-01 的地盘)，不是 REG-03。

    `shadow_from_candidate` 在逐张 `try` 之外、且发生在**所有候选行 `flush()`
    之后** —— 正好是"候选已经写进事务、然后崩了"的形状，也就是 REG-03 里
    `_abandon_attempt()` 那句会话级 `commit()` 会把它们一起提交的那批行。
    """
    import app.media.service as media_service

    calls = {"n": 0}
    real_shadow = media_service.shadow_from_candidate

    def _boom_on_second(session_, product, row):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟落库崩溃(REG-03 回归测试):影子写失败")
        return real_shadow(session_, product, row)

    monkeypatch.setattr(media_service, "shadow_from_candidate", _boom_on_second)

    pid = _product_with_asset(client, "SKU-REG03-A")
    task_id = _create_task(client, pid, candidate_count=3).json()["task"]["id"]

    detail = client.get(f"/api/generation-tasks/{task_id}").json()
    assert detail["status"] == "FAILED"
    assert detail["error_code"] == PROVIDER_RESULT_PENDING_CODE
    assert detail["external_task_id"]

    # 核心断言:数据库里这一条 attempt 下必须是 0 行候选，不是"第一张已提交"
    remaining = (
        session.query(GenerationCandidate)
        .filter(GenerationCandidate.task_id == task_id)
        .count()
    )
    assert remaining == 0, (
        f"崩溃之后库里留下了 {remaining} 条候选 —— 保存点没有把它们回滚掉，"
        "这正是 REG-03 报的\"半截数据\""
    )


def test_resuming_after_a_persist_crash_does_not_duplicate_candidates(
    client, celery_eager, session, monkeypatch, pinned_mock_provider
):
    """崩溃 -> 续跑成功之后，候选数必须恰好等于 candidate_count，不是它的两倍。

    注入点同上一条:影子写，不是 `_load_bytes`。
    """
    import app.media.service as media_service

    calls = {"n": 0}
    real_shadow = media_service.shadow_from_candidate

    def _boom_once_on_second(session_, product, row):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("模拟落库崩溃(REG-03 回归测试)")
        return real_shadow(session_, product, row)

    monkeypatch.setattr(media_service, "shadow_from_candidate", _boom_once_on_second)

    pid = _product_with_asset(client, "SKU-REG03-B")
    task_id = _create_task(client, pid, candidate_count=3).json()["task"]["id"]
    assert client.get(f"/api/generation-tasks/{task_id}").json()["error_code"] == (
        PROVIDER_RESULT_PENDING_CODE
    )

    # 修好落库，续跑
    monkeypatch.setattr(media_service, "shadow_from_candidate", real_shadow)
    r = client.post(f"/api/generation-tasks/{task_id}/retry")
    assert r.status_code == 200, r.text

    final = client.get(f"/api/generation-tasks/{task_id}").json()
    assert final["status"] == "COMPLETED"
    total = (
        session.query(GenerationCandidate)
        .filter(GenerationCandidate.task_id == task_id)
        .count()
    )
    assert total == 3, (
        f"续跑之后候选总数是 {total}，期望恰好 3 —— 多出来的就是 REG-03 的重复候选"
    )
    assert len(final["attempts"]) == 1
    # 续跑成功的 attempt 不许留着上一次的失败字段(本轮复核第 2 条)。
    # 界面直接读这三列，留着的话运营会看到一条"成功但写着落库失败"的记录
    attempt = final["attempts"][0]
    assert attempt["status"] == AttemptStatus.SUCCEEDED.value
    assert attempt["error_code"] is None, (
        f"续跑成功之后 attempt 上还留着 error_code={attempt['error_code']}"
    )
    assert attempt["error_message"] is None
    assert attempt["regeneration_reason"] is None
