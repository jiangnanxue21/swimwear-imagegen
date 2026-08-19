"""a68 守卫:前端零依赖门禁的**运行时前提**必须写下来,而且三处写的是同一个数。

## 来路:同一份代码,两台都装 Node 22 的机器一红一绿

外部审计在 a67 实测:

    Node 22.16.0    node --test x.ts    ERR_UNKNOWN_FILE_EXTENSION    0/2
    Node 22.22.2    node --test x.ts    26/26 passed

默认剥 TypeScript 类型是 **22.18.0** 才落地的(22.6.0 起有
`--experimental-strip-types`)。而 `run-pure-tests.mjs` 的注释写着「Node 22 原生
剥类型」,spawn 出去的却是光秃秃的 `node --test` —— 前提写在注释里,**指望它成立**。

`test_a61_frontend_pure_tests.py` 当时 11/11 全绿。它证明的是 runner 没有第三方
依赖、Makefile/CI 接上了它、用例文件被列进去了 —— 也就是**接线**。它没有执行过
runner,所以「11/11 绿」和「make fe-test-pure 0/2」可以同时成立。

## 这条守卫的射程,以及它**不**替代什么

这里守的是三处声明同向:

    frontend/tools/run-pure-tests.mjs   MIN_NODE 版本闸 + strip-types 能力探测
    frontend/package.json               engines.node 同一个下限
    .github/workflows/ci.yml            gates job 显式 setup-node,且钉到小版本

**它仍然是一条读源码的守卫**,和它要修的那类问题是同一个形状。真正的证明是
CI 里那次真实执行 —— gates job 跑 `node tools/run-pure-tests.mjs` 并要求退出码 0,
在一个**由 setup-node 钉死**的 runtime 上。审计那句「不要再只加 grep」说的就是
这件事:下面这些断言的价值在于"声明不许悄悄漂移",而不在于"它跑得起来"。
"""
from __future__ import annotations

import json
import re
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[3]
RUNNER = PROJECT / "frontend" / "tools" / "run-pure-tests.mjs"
PACKAGE_JSON = PROJECT / "frontend" / "package.json"
CI = PROJECT / ".github" / "workflows" / "ci.yml"

#: `--experimental-strip-types` 落地的版本。低于它 `.ts` 进不了 `node:test`
_STRIP_TYPES_SINCE = (22, 6, 0)
#: 默认剥类型落地的版本。CI 该钉在这里或更高:那样连 flag 都不必依赖
_DEFAULT_STRIP_SINCE = (22, 18, 0)


def _runner() -> str:
    return RUNNER.read_text(encoding="utf-8")


def _version(raw: str) -> tuple[int, int, int]:
    m = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    assert m, f"解析不出版本号:{raw!r}"
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def test_the_runner_declares_a_minimum_node_version():
    """runner 自己带版本闸。

    没有它的话,低版本上跑出来的是一句关于文件扩展名的报错,而那句话和
    测试内容毫无关系 —— 下一个人会去查用例,查上半天。
    """
    source = _runner()
    m = re.search(r"MIN_NODE\s*=\s*\[(\d+),\s*(\d+),\s*(\d+)\]", source)
    assert m, "run-pure-tests.mjs 里找不到 MIN_NODE —— 版本前提又回到注释里了"
    assert (int(m.group(1)), int(m.group(2)), int(m.group(3))) >= _STRIP_TYPES_SINCE


def test_the_runner_asks_for_type_stripping_instead_of_hoping_for_it():
    """显式带 `--experimental-strip-types`,而不是指望默认行为。

    它在 22.18+ 是多余的、无害的;在 22.6~22.17 是必需的。带上之后,
    「跑不跑得起来」不再取决于小版本号。
    """
    source = _runner()
    assert "--experimental-strip-types" in source, (
        "runner 没有显式要求剥类型 —— 22.6~22.17 上它会以 0/2 的形状失败,"
        "而那个形状看起来像是用例挂了"
    )
    assert "allowedNodeEnvironmentFlags" in source, (
        "flag 是硬写死的。将来这个 flag 被移除时会变成 bad option —— "
        "探测一下,认不出就不带(那时默认剥类型早已落地)"
    )


def test_the_runner_translates_the_extension_error_instead_of_passing_it_through():
    """撞上 `ERR_UNKNOWN_FILE_EXTENSION` 时要说人话。

    版本闸已经拦掉了已知的那一种。这里认领的是没预料到的那一种:某个 runtime
    既没被闸拦下,又确实剥不了类型。**它不能以「0/2 passed」的形状出现在
    门禁摘要里** —— 那会让人去查用例,而用例一个字都没错。
    """
    source = _runner()
    assert "ERR_UNKNOWN_FILE_EXTENSION" in source
    # **不能只看第一处。** 文件头的说明里也写着这个错误码(那是来路记录),
    # 而要守的是代码里那一处。逐个看,只要有一处后面跟着让门禁变红的动作就算数
    hits = [m.start() for m in re.finditer("ERR_UNKNOWN_FILE_EXTENSION", source)]
    assert any("process.exit(1)" in source[i : i + 800] for i in hits), (
        "认出来了却没让门禁变红 —— 那这句翻译只是打印了一行更好看的字"
    )


def test_package_json_and_the_runner_agree_on_the_same_floor():
    """`engines.node` 和 `MIN_NODE` 是同一个数。

    两处各写一个的话,这条守卫要防的东西就在守卫自己身上重演了。
    """
    package = json.loads(PACKAGE_JSON.read_text(encoding="utf-8"))
    engines = package.get("engines", {})
    assert "node" in engines, "package.json 没有声明 engines.node"

    declared = _version(engines["node"])
    m = re.search(r"MIN_NODE\s*=\s*\[(\d+),\s*(\d+),\s*(\d+)\]", _runner())
    runner_floor = (int(m.group(1)), int(m.group(2)), int(m.group(3)))
    assert declared == runner_floor, (
        f"package.json 写 {declared},runner 写 {runner_floor} —— "
        "同一个前提的两个副本已经开始各说各话"
    )


def test_the_gates_job_sets_up_node_instead_of_taking_what_the_runner_image_has():
    """跑这个 runner 的 CI job 必须自己 setup-node。

    它原来没有 —— 于是门禁行为取决于 hosted runner 镜像**当天**预装的是哪个
    小版本。一次镜像滚动就能让 gate 由绿转红,而 diff 里一个字都没改。
    """
    ci = CI.read_text(encoding="utf-8")
    gates = ci[ci.index("  gates:") :]
    # 下一个顶层 job 之前的那一段
    nxt = re.search(r"\n  [a-z][a-z0-9_-]*:\n", gates[len("  gates:") :])
    if nxt:
        gates = gates[: len("  gates:") + nxt.start()]

    assert "run-pure-tests.mjs" in gates, (
        "gates job 不再跑前端 runner 了 —— 这条守卫该跟着搬,不是删掉"
    )
    assert "actions/setup-node" in gates, (
        "gates job 跑着 node 却没有 setup-node,Node 版本听凭镜像"
    )


def test_the_gates_job_pins_a_minor_not_just_the_major():
    """而且要钉到小版本。

    钉 `"22"` 只保证「是 22」,而这条门禁分不清的恰好是 22 的内部:
    22.16 与 22.18 在剥类型这件事上结论相反。
    """
    ci = CI.read_text(encoding="utf-8")
    gates = ci[ci.index("  gates:") :]
    nxt = re.search(r"\n  [a-z][a-z0-9_-]*:\n", gates[len("  gates:") :])
    if nxt:
        gates = gates[: len("  gates:") + nxt.start()]

    m = re.search(r'node-version:\s*"([^"]+)"', gates)
    assert m, "gates job 的 setup-node 没写 node-version"
    pinned = m.group(1)
    assert pinned.count(".") >= 2, (
        f"node-version 写的是 {pinned!r} —— 只钉了 major,"
        "而这条门禁分不清的正是 22 的内部"
    )
    assert _version(pinned) >= _DEFAULT_STRIP_SINCE, (
        f"钉在 {pinned},低于默认剥类型落地的版本 —— 能跑,但要靠 flag 兜着"
    )
