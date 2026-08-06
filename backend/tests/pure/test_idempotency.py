"""幂等键与每轮 seed。需求第九章。"""
from __future__ import annotations

from app.workflows.idempotency import build_idempotency_key, next_seed
from tests.pure._helpers import BACKEND_ROOT  # noqa: F401

BASE = dict(
    product_id="11111111-1111-1111-1111-111111111111",
    mode="virtual_try_on",
    provider="mock",
    asset_ids=["a1", "a2"],
    model_template_id="t1",
    candidate_count=4,
    prompt_version="v1",
)


def test_same_inputs_produce_same_key():
    assert build_idempotency_key(**BASE) == build_idempotency_key(**BASE)


def test_asset_order_does_not_matter():
    """素材顺序不同不代表任务不同,否则前端排序一变就漏掉去重。"""
    reversed_assets = {**BASE, "asset_ids": ["a2", "a1"]}
    assert build_idempotency_key(**BASE) == build_idempotency_key(**reversed_assets)


def test_changing_any_meaningful_input_changes_the_key():
    baseline = build_idempotency_key(**BASE)
    variants = [
        {"product_id": "22222222-2222-2222-2222-222222222222"},
        {"mode": "product_to_model"},
        {"provider": "comfyui"},
        {"asset_ids": ["a1", "a3"]},
        {"model_template_id": "t2"},
        {"candidate_count": 6},
        {"prompt_version": "v2"},
    ]
    for change in variants:
        assert build_idempotency_key(**{**BASE, **change}) != baseline, f"{change} 应当改变幂等键"


def test_explicit_key_wins():
    assert build_idempotency_key(**BASE, explicit_key="  order-42  ") == "order-42"


def test_explicit_key_is_truncated_at_the_column_width():
    """截断长度**不写字面量**,读那个和列宽同源的常量。

    这里原来写的是 128。迁移 0041 把 `generation_tasks.idempotency_key`
    扩到 256(方案指纹要进键)之后它变红了 —— 而它守的事情没变:
    截断长度必须等于列宽。两个数字分别手写在两处的失效方式很难看:
    列扩了而截断没跟上,长键被截短,**两份不同的输入拿到同一个键**,
    第二次提交命中第一次的任务而参数不同。
    """
    from app.workflows.idempotency import MAX_IDEMPOTENCY_KEY_CHARS

    key = build_idempotency_key(**BASE, explicit_key="x" * 500)
    assert len(key) == MAX_IDEMPOTENCY_KEY_CHARS


def test_blank_explicit_key_falls_back_to_derivation():
    assert build_idempotency_key(**BASE, explicit_key="   ").startswith("auto-")


def test_generated_key_is_bounded():
    key = build_idempotency_key(**BASE)
    assert key.startswith("auto-") and len(key) <= 64


def test_round_and_seed_are_not_part_of_the_key():
    """重生是同一个任务的下一轮,不该被当成新任务。"""
    with_extra = build_idempotency_key(**BASE, extra={})
    assert with_extra == build_idempotency_key(**BASE)


def test_each_round_gets_a_new_seed():
    seeds = [next_seed(1000, r) for r in range(1, 4)]
    assert len(set(seeds)) == 3


def test_seed_derivation_is_reproducible():
    """同任务同轮必须复现同 seed,否则重放和排障都无从谈起。"""
    assert next_seed(1000, 2) == next_seed(1000, 2)


def test_seed_fits_in_signed_int32():
    """seed 必须落在 PostgreSQL INTEGER 能存的范围内。

    原先断言的是无符号 32 位,但两张表的 seed 列都是有符号 INTEGER,
    于是约一半轮次会在 INSERT 时抛 NumericValueOutOfRange —— 第 1 轮常常
    侥幸通过,第 2 轮才炸,表现为"重试就崩",极难定位。
    """
    for r in range(1, 10):
        assert 0 <= next_seed(123456, r) <= 2**31 - 1
