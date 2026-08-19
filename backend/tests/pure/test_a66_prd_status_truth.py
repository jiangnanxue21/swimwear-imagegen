"""a66 守卫:PRD 的状态标记与代码不许分叉。

## 来路:同一种错我犯了两次,而两次都是我自己在"强调状态不许过期"的那几轮

    a62   PRD 的 S3 那格写 ✅「AC-24 达成」,而 §13 的 AC-24 行写 🟡「浏览器未实测」
          —— 同一份文档、同一次改动、互相矛盾。a59 订正,并立了一条
          「阶段格的状态不许比它依赖的验收项更乐观」
    a60   两条事件码落了码,而 §6 那张表到 a66 仍写着「⬜ 待 S3 前端半」

第二次尤其说明问题:**规矩已经立了,而立规矩的人接着又犯了一次。** 这份 PRD 从 v3.0
起加状态列,理由就是「计划表不带状态,过期时不会有任何东西变红」—— 而加了状态列之后,
**状态本身过期时同样没有任何东西变红**。文档拦不住这一类:回填状态靠人记得,
而人记得的东西迟早会漏。

## 能机械化的只有一部分,如实说清

§6 的事件码表是**机器可读**的:每一行有一个事件码,而 `log_events.EVENTS` 是代码里
唯一的登记处。两边可以双向比对 —— 这一条钉得住。

§13 的验收表与 §16 的阶段表**钉不住**:「浏览器未实测」「真库并发未跑」这类状态没有
任何代码可以佐证。那两张表仍然靠人维护,这里不假装守住了它们。**一个被说大的守卫比
没有守卫更贵**(a61 那条同样的判据)。
"""
from __future__ import annotations

import re
from pathlib import Path

from app.core.log_events import EVENTS

PROJECT = Path(__file__).resolve().parents[3]
PRD = PROJECT / "docs" / "PRD-A55-PROMPT-REGISTRY-AND-LOG-CONSOLE.md"

#: §6 那张表的行:`| \`域.事件码\` | 标签 | 块 | 调用点 | 状态 |`
_ROW = re.compile(r"^\| `([a-z_]+\.[a-z_]+)` \|(.+)\|\s*(✅|🟡|⬜)([^|]*)\|\s*$")


def _rows() -> list[tuple[str, str]]:
    out = []
    for line in PRD.read_text(encoding="utf-8").splitlines():
        m = _ROW.match(line)
        if m:
            out.append((m.group(1), m.group(3)))
    return out


def test_the_event_table_is_still_parseable():
    """表的形状变了就当场红,而不是安静地一行都匹配不上。

    一条匹配不到任何行的守卫,和一条全都过了的守卫,在 CI 上长得一模一样 ——
    本仓记过多次,这里给它一个下限。
    """
    rows = _rows()
    assert len(rows) >= 5, f"只从 §6 解析出 {len(rows)} 行 —— 表的形状多半变了"


def test_every_event_marked_done_is_really_registered():
    """标 ✅ 而代码里没有 —— 表在说一件没发生的事。"""
    missing = [e for e, mark in _rows() if mark == "✅" and e not in EVENTS]
    assert not missing, f"这些事件码在 PRD 里标着已完成,而 EVENTS 里没有:{missing}"


def test_no_event_is_still_marked_pending_after_it_landed():
    """**这一条就是 a66 的来路。**

    `settings.prompt_save_conflict` / `settings.prompt_rolled_back` 在 a60 就登记了,
    而 §6 那张表到 a66 仍写着「⬜ 待 S3 前端半」。一份说"还没做"的表,读的人会
    照着它去做一遍已经做完的事 —— 而 §3.70 点名的那类分叉,正是这么长出来的。
    """
    stale = [e for e, mark in _rows() if mark == "⬜" and e in EVENTS]
    assert not stale, (
        f"这些事件码已经登记进 EVENTS,而 PRD 里还标着未完成:{stale} —— 回填状态"
    )


def test_the_pending_ones_really_are_pending():
    """反过来:标 🟡 的不许两边都齐了。

    🟡 在这张表里的含义是"落了一部分" —— 而事件码只有登记/没登记两种状态,
    没有中间态。一个标 🟡 的事件码要么该是 ✅,要么该是 ⬜。
    """
    half = [e for e, mark in _rows() if mark == "🟡"]
    assert not half, (
        f"事件码没有中间态,这几行的 🟡 说不清是哪一边:{half}"
    )


def test_the_guard_says_what_it_cannot_cover():
    """§13 的验收表与 §16 的阶段表钉不住 —— 那些状态没有代码可以佐证。

    这一条读的是**这个文件自己的模块文档**:射程要写下来,而写下来的东西
    要能被查。不写的话,下一个人会以为 PRD 的所有状态列都有守卫。
    """
    # 读**模块 docstring**,不读整个文件 —— 读整个文件的话,断言自己那两行
    # 也算命中,于是把 docstring 里那段删掉守卫照样绿。a66 的门禁当场抓到了
    # 这一条(`audit_guard_windows` 报"窗口不唯一"),而它是这一轮第三次
    # 被自己的工具挡回来
    import ast as _ast

    doc = _ast.get_docstring(_ast.parse(Path(__file__).read_text(encoding="utf-8"))) or ""
    assert "钉不住" in doc, "模块文档没写清哪些表钉不住"
    assert "靠人维护" in doc, "模块文档没写清那两张表仍然靠人"
