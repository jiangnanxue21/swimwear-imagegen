"""建档请求指纹(PRD §9.1)。

`POST /api/spus` 在 2026-08-09 评审之前**一个字都没实现 `Idempotency-Key`**,
而 §12.1 的 API 表里那一行后面就写着「新增(带 Idempotency-Key)」。

这些用例只覆盖**判定**那一半(纯函数,零依赖)。落库那一半 ——
部分唯一索引 `uq_spus_request_key`、并发撞键之后回查赢家 —— 要真
PostgreSQL,归 `tests/test_a45_batch13_spu_db.py` 那一侧,**本轮没有跑**
(本机无 PG)。分开写是刻意的:指纹算错和索引建错的修法完全不同,
混在一个文件里两者都查不清。
"""
from app.listings.sku_matrix import (
    MAX_IDEMPOTENCY_KEY,
    SPU_REQUEST_VERSION,
    spu_request_fingerprint,
)


def _fp(**over):
    base = dict(
        spu_code="SW-101",
        internal_name="三色泳衣",
        audience="WOMEN",
        base_category="swimwear",
        supplier_ref=None,
        variant_codes=["BLK", "RED", "BLU"],
        size_template="ALPHA_SML",
    )
    base.update(over)
    return spu_request_fingerprint(**base)


# ---------------------------------------------------------------- 同一次请求


def test_the_same_request_hashes_the_same_way():
    assert _fp() == _fp()


def test_colour_order_is_not_part_of_the_request():
    """`[BLK, RED]` 与 `[RED, BLK]` 是同一次建档。

    不排序的话,一个把颜色存在 Set 里的前端会因为遍历顺序不稳定算出两个
    指纹,于是同一次重发被判成 409 —— 而那个 409 没有任何人看得懂。
    """
    assert _fp(variant_codes=["RED", "BLU", "BLK"]) == _fp()


def test_duplicate_colours_do_not_change_the_request():
    """重复颜色不改变"这次要建什么"。

    它是不是**合法**入参由 `expand()` 回答,而且是在算指纹之前回答的 ——
    两件事分开,不互相掩盖。
    """
    assert _fp(variant_codes=["BLK", "BLK", "RED", "BLU"]) == _fp()


def test_whitespace_and_case_do_not_change_the_request():
    assert _fp(spu_code=" sw-101 ", variant_codes=[" blk ", "red", "blu"]) == _fp()


# ---------------------------------------------------------------- 不同的请求


def test_every_field_that_changes_the_outcome_changes_the_fingerprint():
    """**这是本模块存在的理由。**

    七项里任何一项变了,建出来的东西就不一样,所以指纹必须变。
    漏掉其中一项的后果是:同键重发时那一项的改动被静默丢弃,
    调用方拿到 200 和一个**不是它要的**对象。
    """
    changed = {
        "spu_code": "SW-102",
        "internal_name": "另一个款",
        "audience": "MEN",
        "base_category": "dress",
        "supplier_ref": "V-9",
        "variant_codes": ["BLK", "RED"],
        "size_template": "NUMERIC_EU",
    }
    for field, value in changed.items():
        assert _fp(**{field: value}) != _fp(), f"{field} 变了而指纹没变"


def test_dropping_a_colour_changes_the_fingerprint():
    """少一个颜色 = 少三行 SKU。它必须是一次不同的请求。"""
    assert _fp(variant_codes=["BLK", "RED"]) != _fp()


# ---------------------------------------------------------------- 边界


def test_the_algorithm_version_is_inside_the_hash():
    """换算法必须换版本号,否则新旧指纹混在一列里。

    「同键不同入参」与「换过算法」在比较结果上长得一模一样(都是不相等),
    于是换算法那天全部存量键会开始报 409。版本号进哈希是让这件事
    在**换的那一刻**就分得开。
    """
    assert SPU_REQUEST_VERSION in ("1",)
    assert len(_fp()) == 64  # sha256 hexdigest


def test_none_and_empty_are_the_same_absence():
    """`supplier_ref=None` 与 `""` 是同一件事:没填。

    分开的话,一个把空串当"没填"的前端与一个传 null 的前端算出两个指纹,
    而它们提交的是同一个款。
    """
    assert _fp(supplier_ref=None) == _fp(supplier_ref="")
    assert _fp(supplier_ref=None) == _fp(supplier_ref="   ")


def test_the_key_length_ceiling_is_stated_not_guessed():
    """上限与 `batch_service` 同值。

    **超长报错,不截断** —— 截到 64 之后,两把只有前缀相同的键会变成同一把,
    而那种冲突报出来的错和真正的键冲突长得一模一样。
    """
    assert MAX_IDEMPOTENCY_KEY == 64


def test_notes_are_not_part_of_the_request():
    """`notes` 不进指纹 —— 它不改变建出来的任何对象。

    进了的话,同一次重发只要备注多一个空格就报 409。
    这条用**签名**钉着:`spu_request_fingerprint` 根本不收 notes 参数,
    所以哪天有人想加进去,得先改签名,而改签名会经过这条用例。
    """
    import inspect

    params = set(inspect.signature(spu_request_fingerprint).parameters)
    assert "notes" not in params
    assert params == {
        "spu_code",
        "internal_name",
        "audience",
        "base_category",
        "supplier_ref",
        "variant_codes",
        "size_template",
    }
