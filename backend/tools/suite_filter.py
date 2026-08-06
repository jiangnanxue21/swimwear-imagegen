#!/usr/bin/env python3
"""「这个过滤器挑中哪些用例文件」—— 只有这一处定义。

## 为什么要单独一个模块

`run_pure_tests.py` 按过滤器挑文件,`audit_anchors.py` 要判断变异脚本的
`SUITE_FILTER` 挑不挑得中 —— 两处必须给出**完全一样**的答案,否则审计
说"挑得中"而运行器挑不中(或者反过来),而两种分叉都表现为"变异全报 GREEN"。

原来的做法是 `audit_anchors.py` 里抄一份 `suite in name`,并在文档字符串里
写「所以这里也只能照着它判,不能自己发明一套」。**用注释维持两份实现同步,
正是这个仓库反复踩的那个坑**(`test_in_flight_task_count_comes_from_the_state_machine`
守的是同一件事:"哪些状态算运行中"不许两处各抄一份)。注释拦不住重构。

所以把判定挪到这里,两边都 import 它。这是结构性的,不靠人记得。

## 为什么不让 `audit_anchors.py` 直接 import `run_pure_tests`

`run_pure_tests.py` **在模块顶层就有副作用**:它 `mkdtemp()` 建临时目录、
写两个环境变量(见那个文件顶部关于主密钥的整段说明)。审计工具跑一次
就在 /tmp 下多一个目录,而审计的卖点恰恰是"只读文件、零子进程、不到一秒"。

这个模块没有任何副作用,import 它是安全的。

## 判定规则

    ""              挑全部
    "xxx.py"        **精确**匹配文件名
    其他             子串匹配(与历史行为一致)

以 `.py` 结尾 = 精确,是因为在调用点上一眼能看出意图:
`SUITE_FILTER = "test_a45_batch14_fixes.py"` 明摆着只指一个文件,
而 `"a45_batch14"` 明摆着是个前缀。不需要额外的开关参数,也不会
悄悄改变任何已有调用的含义。

## 精确匹配解决的不只是耗时

batch14-4 §五 记的是耗时:`"a45_batch14"` 同时匹配 `_2` `_3` `_4`
与 `stage3_extractor` 共 5 个套件,每条变异都白跑四份。

但更要紧的是**归因**:`mutate_batch14.py` 的某条变异可以因为
`test_a45_batch14_4_fixes.py` 里的某条守卫而变红,报告却记在
`mutate_batch14.py` 自己头上 —— 于是"这条变异被我的守卫抓住了"是假的,
而删掉那条真正抓住它的守卫时,变异仍然是红的,没人会发现。
这与 F7(`AttributeError` 的非零退出码被读成"被抓住")是同一种病:
**红是真的,红的原因不是。**
"""
from __future__ import annotations

from pathlib import Path

__all__ = ["select", "select_paths", "describe"]


def select(filenames: list[str] | tuple[str, ...], spec: str) -> list[str]:
    """`spec` 挑中的文件名,按名字排序。

    只做纯字符串判定,不碰文件系统 —— 这样测试可以直接喂一张假清单,
    而不必在磁盘上摆出一棵目录树。
    """
    names = sorted(filenames)
    if not spec:
        return names
    if spec.endswith(".py"):
        return [name for name in names if name == spec]
    return [name for name in names if spec in name]


def select_paths(directory: Path, spec: str, pattern: str = "test_*.py") -> list[Path]:
    """`directory` 下被 `spec` 挑中的文件,按名字排序。"""
    by_name = {p.name: p for p in directory.glob(pattern)}
    return [by_name[name] for name in select(tuple(by_name), spec)]


def describe(spec: str) -> str:
    """给报错文案用的一句话,说明这个 spec 是按哪种方式判的。"""
    if not spec:
        return "空过滤器(全部文件)"
    if spec.endswith(".py"):
        return f"精确文件名 {spec!r}"
    return f"子串 {spec!r}"
