"""REVIEW II.8:上传闸(20MB)与 Provider 内联上限(FASHN 10MB)不等 —— 一张
15MB 的商品图能通过上传、却在建任务后于 FASHN 侧才失败,而原文案只说「素材超过
10 MB」,运营无从判断是哪个环节、为什么上传放行了、怎么修。

本批把这三件事一次说清,并把 FASHN 四个 oversize 现场统一收敛到同一条文案。

## 两半各守什么

- **文案逻辑**(`provider_inline_size_message`)不依赖配置/网络,所以这里能**真跑**:
  给字节数直接断言产出的句子。这是行为验证。
- **接线**(FASHN 四处 oversize 是否都走了这条文案)压在**源码断言**上 ——
  `test_fashn_provider.py` 需要 httpx,在只有 python3 的机器上被跳过,跑不到。
  所以「四处都改过、没有一处还留着简略文案」这件事只能读源码来守。这不是理想
  分工,是如实分工:下一台有依赖的机器请先跑 `test_fashn_http.py` / provider 用例。

反向断言(「不许再出现简略文案」)按 14-14 的教训先剥注释再比对 —— 注释里出现
的字样不是代码在做的事,这个仓库栽过四次。
"""
from __future__ import annotations

import re

from app.services.upload_validation import provider_inline_size_message
from tests.pure._helpers import BACKEND_ROOT

MB = 1024 * 1024
FASHN = BACKEND_ROOT / "app" / "providers" / "fashn.py"


def _code_only(src: str) -> str:
    """去掉注释与文档字符串。反向断言必须只看代码在做的事。"""
    src = re.sub(r'"""(?:.|\n)*?"""', "", src)
    src = re.sub(r"'''(?:.|\n)*?'''", "", src)
    return re.sub(r"#.*$", "", src, flags=re.M)


# ------------------------------------------------------------ 文案:真跑


def test_message_names_the_provider_and_its_limit():
    msg = provider_inline_size_message(
        15 * MB, provider_name="fashn", provider_limit_bytes=10 * MB
    )
    assert "fashn" in msg
    assert "10 MB" in msg  # Provider 上限,整数 MB
    assert "15" in msg  # 素材实际大小


def test_message_flags_the_upload_gate_when_it_is_wider():
    """核心诉求:告诉运营「上传放行了、这里更严」,否则他会以为哪里坏了。"""
    msg = provider_inline_size_message(
        15 * MB,
        provider_name="fashn",
        provider_limit_bytes=10 * MB,
        upload_limit_bytes=20 * MB,
    )
    assert "20 MB" in msg
    assert "上传" in msg


def test_message_does_not_claim_the_gate_is_wider_when_it_is_not():
    """上传闸不比 Provider 宽时,不能凭空说「上传放行到更大」——那是假信息。"""
    msg = provider_inline_size_message(
        12 * MB,
        provider_name="fashn",
        provider_limit_bytes=10 * MB,
        upload_limit_bytes=8 * MB,
    )
    assert "8 MB" not in msg
    assert "上传闸放行" not in msg


def test_message_gives_an_actionable_fix():
    msg = provider_inline_size_message(
        15 * MB, provider_name="fashn", provider_limit_bytes=10 * MB
    )
    assert "压" in msg and "重试" in msg  # 有明确的修复动作


def test_message_does_not_advise_switching_to_a_channel_that_does_not_exist():
    """初版这条文案写了「或改用不受此限的渠道」,而 `providers/registry.py` 里注册的
    换装 Provider 只有 `mock` 与 `fashn` —— 运营照着去找会找到一片空白。

    **一句办不到的建议比一句简略的报错更糟:** 简略报错只是让人困惑,办不到的建议
    是让人去做一件做不到的事,还会让人以为是自己没找到那个设置。这条反向断言把它钉住,
    等真有第二个可用 Provider 了,让调用方作为参数传进来,而不是在文案里假设它存在。
    """
    msg = provider_inline_size_message(
        15 * MB,
        provider_name="fashn",
        provider_limit_bytes=10 * MB,
        upload_limit_bytes=20 * MB,
    )
    assert "渠道" not in msg
    assert "换" not in msg


def test_size_is_shown_with_a_decimal_to_avoid_equal_looking_values():
    """10.5MB 超过 10MB 时,若两边都取整会显示「10 MB 超过 10 MB」,把人绕晕。"""
    msg = provider_inline_size_message(
        int(10.5 * MB), provider_name="fashn", provider_limit_bytes=10 * MB
    )
    assert "10.5 MB" in msg


# ------------------------------------------------------------ 接线:读源码守


def test_fashn_routes_every_oversize_site_through_the_shared_message():
    code = _code_only(FASHN.read_text(encoding="utf-8"))

    # 收敛口:helper 定义在,且真的调用了共享文案函数
    assert "def _oversize_error(" in code
    assert "provider_inline_size_message(" in code

    # 四个 oversize 现场:声明大小、边读边算、本地 stat、内联后复核
    assert code.count("self._oversize_error(") >= 4


def test_fashn_no_longer_carries_the_terse_oversize_copy():
    """反向断言:简略文案一处都不许再留 —— 留一处,那一处就退回「超过 N MB」,
    而运营又回到看不懂的原点。这些字样若只出现在注释里不算(已剥注释)。"""
    code = _code_only(FASHN.read_text(encoding="utf-8"))
    for terse in ("无法内联提交", "素材声明大小超过", "已中止读取"):
        assert terse not in code, f"简略 oversize 文案未清除:{terse}"
    # 「素材超过 {...} MB」这种 f-string 拼接也不许再出现
    assert not re.search(r"素材超过 \{", code)
