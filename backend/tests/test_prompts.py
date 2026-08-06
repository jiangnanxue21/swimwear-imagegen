"""提示词的读写与版本切换(需要数据库)。

纯策略部分(体检规则、默认值来源、评分器怎么拿提示词)在
``tests/pure/test_prompts_and_settings_coverage.py``,那批不需要任何依赖。
这里只测**必须有库才能测错的东西**:版本号怎么涨、active 会不会出现两条、
回滚之后取到的是哪一版。
"""
from __future__ import annotations

import pytest

from app.services import prompt_service
from app.services.prompt_rules import VISION_SYSTEM_PROMPT
from tests.conftest import requires_db

# 跟项目其余 DB 测试一致:没有可连接的库时**跳过**,而不是失败
pytestmark = requires_db

KEY = VISION_SYSTEM_PROMPT


def test_fresh_install_uses_the_built_in_default(session):
    """库里没记录时不该是空字符串,也不该报错。"""
    content, version = prompt_service.get_active_content(session, KEY)
    assert version is None
    assert content == prompt_service.default_content(KEY)


def test_first_save_becomes_version_one_and_takes_effect(session):
    prompt_service.save_version(session, KEY, "第一版口径", note="初始")
    content, version = prompt_service.get_active_content(session, KEY)
    assert (content, version) == ("第一版口径", 1)


def test_versions_increment_and_history_is_kept(session):
    """旧版本的 content 是解释历史判定的唯一依据,绝不能被覆盖。"""
    for i in range(1, 4):
        prompt_service.save_version(session, KEY, f"第 {i} 版")

    versions = prompt_service.list_versions(session, KEY)
    assert [v.version for v in versions] == [3, 2, 1]
    assert versions[-1].content == "第 1 版"  # 最早那版原文还在


def test_only_one_version_is_ever_active(session):
    """两条 active 时评分器只会取到其中一条,取到哪条看排序 —— 必须从数据库层面杜绝。"""
    for i in range(1, 4):
        prompt_service.save_version(session, KEY, f"v{i}")
    session.flush()

    active = [v for v in prompt_service.list_versions(session, KEY) if v.is_active]
    assert len(active) == 1
    assert active[0].version == 3


def test_rollback_switches_the_effective_content(session):
    prompt_service.save_version(session, KEY, "老口径")
    prompt_service.save_version(session, KEY, "新口径")
    assert prompt_service.get_active_content(session, KEY)[0] == "新口径"

    prompt_service.activate_version(session, KEY, 1)
    content, version = prompt_service.get_active_content(session, KEY)
    assert (content, version) == ("老口径", 1)


def test_rollback_to_a_missing_version_is_an_explicit_error(session):
    with pytest.raises(LookupError):
        prompt_service.activate_version(session, KEY, 99)


def test_reset_returns_to_the_built_in_default_without_deleting_history(session):
    """"当前用的是内置默认"应该是个可判定的状态,不必拿内容去和常量比对。"""
    prompt_service.save_version(session, KEY, "自定义")
    prompt_service.reset_to_default(session, KEY)

    content, version = prompt_service.get_active_content(session, KEY)
    assert version is None
    assert content == prompt_service.default_content(KEY)
    assert len(prompt_service.list_versions(session, KEY)) == 1  # 历史还在


def test_saving_without_activating_leaves_the_current_one_in_effect(session):
    prompt_service.save_version(session, KEY, "生效版")
    prompt_service.save_version(session, KEY, "草稿版", activate=False)
    assert prompt_service.get_active_content(session, KEY)[0] == "生效版"


def test_unknown_key_is_rejected(session):
    with pytest.raises(ValueError):
        prompt_service.save_version(session, "nope", "x")


def test_describe_gives_the_page_everything_it_needs(session):
    prompt_service.save_version(session, KEY, "只输出 JSON,判断 hard_fail", note="收紧")
    payload = prompt_service.describe(session, KEY)

    assert payload["version"] == 1
    assert payload["is_default"] is False
    assert payload["default_content"] == prompt_service.default_content(KEY)
    assert payload["versions"][0]["note"] == "收紧"
    assert isinstance(payload["warnings"], list)


# ---------------------------------------------------------------- 接口层


def test_prompt_endpoints_require_the_admin_token(guarded_client):
    """提示词决定评分口径,能改它等于能改「什么图算合格」。

    GET **不带 body**:新版 starlette 的 TestClient.get() 直接拒收 ``json=``
    (TypeError),而旧版是默默接受。以前这里对三个方法一视同仁地塞 body,
    在旧版上跑得通,靠的是被忽略掉 —— 换个版本就变成测试本身抛异常,
    而不是守卫没守住。守卫的判据与请求体无关,GET 本来也不该带。
    """
    for method, path in (
        ("get", f"/api/prompts/{KEY}"),
        ("put", f"/api/prompts/{KEY}"),
        ("post", f"/api/prompts/{KEY}/reset"),
    ):
        kwargs = {} if method == "get" else {"json": {"content": "x"}}
        response = getattr(guarded_client, method)(path, **kwargs)
        assert response.status_code in (401, 403), f"{method} {path} 没有守卫"


def test_unknown_prompt_key_returns_404(admin_client):
    assert admin_client.get("/api/prompts/nope").status_code == 404


def test_preview_does_not_write_anything(admin_client, session):
    response = admin_client.post(
        f"/api/prompts/{KEY}/preview", json={"content": "随便写点什么"}
    )
    assert response.status_code == 200
    assert response.json()["warnings"]  # 这段确实有问题
    assert prompt_service.get_active_content(session, KEY)[1] is None  # 但没落库
