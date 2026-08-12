"""ComfyUI 的每个配置项都答得出「谁读它」(a51)。

和 `audit-columns`(每一列都答得出谁写它)是同一条规矩,只是发生在配置上。

被咬的形状:配置从 yaml 一路解析进 dataclass,然后**停在那里**。改它不会有
任何反应,也不报错;而 `comfyui/` 下的配置样例里写着它,所以看起来是生效的。
阶段 5 接线的人多半会重新发明一遍超时,然后样例与实现永远对不上。

两条路让它变绿:接线(把字段真的读起来),或者在 `UNWIRED_CONFIG_FIELDS`
里写一句它该由谁读。两条都要求写的人**做一次决定**。
"""
from __future__ import annotations

import ast
from pathlib import Path

_MODULE = Path(__file__).resolve().parents[2] / "app/providers/comfyui.py"


def _source() -> str:
    return _MODULE.read_text(encoding="utf-8")


def _config_fields() -> list[str]:
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "ComfyUIConfig":
            return [
                stmt.target.id
                for stmt in node.body
                if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name)
            ]
    raise AssertionError("找不到 ComfyUIConfig —— 这条测试的锚点没了")


def _declared_unwired() -> dict[str, str]:
    tree = ast.parse(_source())
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign) or not isinstance(node.target, ast.Name):
            continue
        if node.target.id != "UNWIRED_CONFIG_FIELDS":
            continue
        assert isinstance(node.value, ast.Dict)
        return {
            k.value: v.value
            for k, v in zip(node.value.keys, node.value.values, strict=True)
            if isinstance(k, ast.Constant) and isinstance(v, ast.Constant)
        }
    raise AssertionError("找不到 UNWIRED_CONFIG_FIELDS")


def _reads_of(field: str) -> int:
    """`config.<field>` 这样的读取出现了几次(不含 dataclass 上的定义本身)。"""
    tree = ast.parse(_source())
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute) and node.attr == field
    )


def test_every_config_field_is_either_read_or_declared_unwired():
    """**本批的核心断言。**

    新增一个配置项而没人读它、也没写理由 -> 当场红。
    """
    fields = _config_fields()
    declared = _declared_unwired()
    undecided = [f for f in fields if _reads_of(f) == 0 and f not in declared]
    assert not undecided, (
        "这些 ComfyUI 配置项没有任何读者,也没在 UNWIRED_CONFIG_FIELDS 里说明原因:"
        f"{undecided}"
    )


def test_the_unwired_table_does_not_outlive_the_wiring():
    """接线之后要把对应的行删掉。

    反向的那一半。少了它,`UNWIRED_CONFIG_FIELDS` 会变成一份只增不减的名单 ——
    某一项明明已经读起来了,表里还写着"没人读",而下一个人会信那张表。
    """
    stale = [f for f in _declared_unwired() if _reads_of(f) > 0]
    assert not stale, f"这些字段已经有读者了,请从 UNWIRED_CONFIG_FIELDS 里删掉:{stale}"


def test_every_declared_field_actually_exists_on_the_config():
    """表里不许有 dataclass 上不存在的字段(改名之后忘了同步)。"""
    fields = set(_config_fields())
    ghosts = [f for f in _declared_unwired() if f not in fields]
    assert not ghosts, f"UNWIRED_CONFIG_FIELDS 里这些字段在 ComfyUIConfig 上不存在:{ghosts}"


def test_every_reason_says_who_should_read_it():
    """理由不许是空字符串 —— 一个没有理由的豁免下次会被当成"本来就没用"。"""
    empty = [f for f, why in _declared_unwired().items() if not why.strip()]
    assert not empty, f"这些豁免没有写理由:{empty}"
