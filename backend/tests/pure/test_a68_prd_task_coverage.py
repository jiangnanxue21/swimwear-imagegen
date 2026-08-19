"""a68 守卫:§12 里的每一项任务,必须在 §16 的某一格里出现过。

## 来路:FE-305 从 v1.0 起就没被排进任何一格

§12.3 写着「防注入段抽取(BE-308 / FE-305)」,而 §16 的 S4 那格写的是
「FE-306/307/309/310 页面整改 + BE-308 防注入 | 5 项」—— **5 项正好是那四个 FE 加
BE-308,FE-305 不在里面**。于是 BE-308 a56 就落了,而它的前端那一半七轮里没有任何人
认领,也没有出现在任何一条欠账里。

**值得记的不是漏了一项,是漏的方式。** §12 按功能分组、§16 按阶段分组,**两张表各自
都自洽** —— 只有把它们对起来才看得出少了谁,而这件事此前没有任何东西在做。
a66 那条守卫钉的是「状态标记不许过期」,管不到「一项任务压根没进过表」。

## 射程

只判**覆盖**,不判排得对不对:一项任务被排进 S1 还是 S4,这里看不出来。
阶段划分靠 §16 的依赖列与人的判断,这条守卫替代不了。
"""
from __future__ import annotations

import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
PRD = PROJECT / "docs" / "PRD-A55-PROMPT-REGISTRY-AND-LOG-CONSOLE.md"

#: `BE-301~304` / `FE-312~315` / `LG-409~412` 这种区间写法
_RANGE = re.compile(r"\b(BE|FE|LG)-(\d+)\s*~\s*(?:(?:BE|FE|LG)-)?(\d+)")
_SINGLE = re.compile(r"\b((?:BE|FE|LG)-\d+)\b")


def _spec_tasks() -> set[str]:
    """§12 各小节标题里声明的任务号。

    读标题而不是读条目 —— 标题是那一节的**契约**(「12.3 防注入段抽取(BE-308 / FE-305)」),
    而条目可能漏写。FE-305 恰好两边都有,但下一个漏掉的未必。
    """
    text = PRD.read_text(encoding="utf-8")
    section = text[text.index("## 12. 功能规格") : text.index("## 13. 验收标准")]
    tasks: set[str] = set()
    for line in section.splitlines():
        if not line.startswith("### 12."):
            continue
        tasks |= _expand(line)
    return tasks


def _expand(line: str) -> set[str]:
    """把一段文字里的任务号摊平。

    三种写法都要认,漏一种就是**漏报**:
        BE-301 ~ BE-304     区间(端点可能各自带前缀)
        LG-401/402/406      同前缀的斜杠串
        FE-305~307/309/310  两种混着(S4 现在就是)

    这个解析器改了两次,两次都是它自己报的假红:
    第一版只认区间 -> L0/L1/L2 的清单各只数出 1 项(计数写 3/3/2);
    第二版把区间与斜杠揉进一个正则 -> `BE-301 ~ BE-304 / FE-301 ~ FE-304`
    被从斜杠处劈开,FE-302/303 掉了。**分三遍走比一个大正则可靠**。
    """
    out: set[str] = set()
    # 一、区间。端点可能写成 `BE-304`,也可能只写 `304`
    for prefix, low, high in re.findall(
        r"\b(BE|FE|LG)-(\d+)\s*~\s*(?:(?:BE|FE|LG)-)?(\d+)", line
    ):
        for n in range(int(low), int(high) + 1):
            out.add(f"{prefix}-{n}")
    # 二、斜杠串。`/` 后面的裸数字沿用最近一次出现的前缀
    for match in re.finditer(r"\b(BE|FE|LG)-(\d+(?:\s*[~/]\s*\d+)*)", line):
        prefix = match.group(1)
        for part in re.split(r"\s*/\s*", match.group(2)):
            span = re.match(r"(\d+)\s*~\s*(\d+)$", part)
            if span:
                for n in range(int(span.group(1)), int(span.group(2)) + 1):
                    out.add(f"{prefix}-{n}")
            elif part.strip().isdigit():
                out.add(f"{prefix}-{part.strip()}")
    # 三、单个
    out |= set(_SINGLE.findall(line))
    return out


def _staged_tasks() -> set[str]:
    text = PRD.read_text(encoding="utf-8")
    tasks: set[str] = set()
    for line in text.splitlines():
        if re.match(r"^\| \*\*[LS]\d\*\* \|", line):
            cells = [c.strip() for c in line.strip().strip("|").split("|")]
            if len(cells) >= 3:
                tasks |= _expand(cells[2])
    return tasks


def test_both_tables_are_still_parseable():
    """两边解析不出东西就当场红,而不是安静地"零个漏项"。"""
    spec, staged = _spec_tasks(), _staged_tasks()
    assert len(spec) >= 15, f"只从 §12 的小节标题解析出 {len(spec)} 项 —— 标题格式多半变了"
    assert len(staged) >= 15, f"只从 §16 解析出 {len(staged)} 项 —— 阶段表格式多半变了"


def test_every_specified_task_is_scheduled_somewhere():
    """**这一条就是 a68 的来路。**

    一项写在功能规格里、却不在任何阶段里的任务,不会有人做,也不会有人发现没做 ——
    它两边都自洽。
    """
    missing = sorted(_spec_tasks() - _staged_tasks())
    assert not missing, (
        f"这些任务写在 §12 里,而 §16 的任何一格都没排它们:{missing}"
    )


def test_no_stage_schedules_a_task_that_no_section_specifies():
    """反向:排了一项而没有任何小节说它要做什么,那一格的计数是虚的。

    **只查 BLOCK-30 的 BE-/FE-。** `LG-4xx` 是 BLOCK-31 的任务,规格写在第一部分
    (§3~§5),不在 §12 里 —— 把它们算进来会得到十六条假红,而那十六条一条都不是缺陷。
    这条射程限制写在这里,不是留个后门:BLOCK-31 的规格与阶段表同样该对齐,
    只是那要读另一组小节,值得单做而不是塞进这一条。
    """
    stray = sorted(
        t for t in _staged_tasks() - _spec_tasks() if not t.startswith("LG-")
    )
    assert not stray, f"§16 排了这些任务,而 §12 的小节标题里没有:{stray}"


def test_the_stage_counts_match_what_each_stage_lists():
    """**S4 那格的「5 项」正是漏掉 FE-305 的地方。**

    数字和清单对不上时,读的人多半信数字 —— 而数字是手写的。
    """
    text = PRD.read_text(encoding="utf-8")
    bad: list[str] = []
    for line in text.splitlines():
        if not re.match(r"^\| \*\*[LS]\d\*\* \|", line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 4 or not cells[3].isdigit():
            continue
        listed = len(_expand(cells[2]))
        if listed and listed != int(cells[3]):
            bad.append(f"{cells[0]}:清单 {listed} 项,计数写 {cells[3]}")
    assert not bad, f"阶段计数与清单对不上:{bad}"
