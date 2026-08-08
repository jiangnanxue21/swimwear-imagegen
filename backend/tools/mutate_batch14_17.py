#!/usr/bin/env python3
"""A45-batch14-17 变异验证:筛选状态住在 URL 里(GAP-033)。

用法:python3 tools/mutate_batch14_17.py

## 这一批变异改的全是**前端**源码

前几份变异脚本改的是 `app/` 下的判定。本批的被测对象是四个 `.tsx` 页面
和一个 hook —— 而守卫是 Python 写的、读前端源码。变异照样改真实文件,
只是文件在 `../frontend/` 下。

## 为什么这批变异格外要紧

本批的行为覆盖(Vitest)**在这台机器上一次都没跑过**,装不了 node_modules。
所以这十一条守卫是本批唯一被执行过的验证,而**没验过红的守卫等于没有守卫**
——「它今天跑绿」是可假绿守卫的定义,不是它有效的证据。

每一条变异都对着一条守卫,并且描述里写的是**这个改动在生产上的表现**,
不是"哪条断言会不满足"。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

HOOK = "../frontend/src/hooks/useUrlFilters.ts"
LIST = "../frontend/src/pages/WorkbenchListPage.tsx"
TASKS = "../frontend/src/pages/TaskListPage.tsx"
REVIEW = "../frontend/src/pages/WorkbenchReviewPage.tsx"
AUDIT = "../frontend/src/pages/AuditLogPage.tsx"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # -------------------------------------------------- 默认值不进 URL
    (
        "U1",
        "页码的默认值也写进 URL(进一次页面就攒出一串参数,地址没人会贴)",
        HOOK,
        "    write: (value) => (value === fallback ? null : String(value)),\n  }\n}\n\n/**\n * 只有几档",
        "    write: (value) => String(value),\n  }\n}\n\n/**\n * 只有几档",
    ),
    (
        "U2",
        "开关的默认值也写进 URL(同上,而且 `?only_blocked=0` 读起来像筛过)",
        HOOK,
        "    write: (value) => (value === fallback ? null : value ? '1' : '0'),",
        "    write: (value) => (value ? '1' : '0'),",
    ),
    (
        "U3",
        "搜索词写成空串留在 URL 上(清除筛选之后地址栏还挂着 `?search=`)",
        HOOK,
        "    write: (value) => (value === '' ? null : value),",
        "    write: (value) => value,",
    ),
    # -------------------------------------------------- 读的那条路不许写 URL
    (
        "U4",
        "读到非法参数顺手把它从地址栏删掉(渲染期发起导航,手敲进后退栈)",
        HOOK,
        "  const values = useMemo(() => {\n    const current = new URLSearchParams(search)",
        "  const values = useMemo(() => {\n    setParams(new URLSearchParams(search))\n"
        "    const current = new URLSearchParams(search)",
    ),
    # -------------------------------------------------- 非法值的退回方向
    (
        "U5",
        "认不出来的开关取值一律退回 false(审计页的降噪被悄悄关掉,界面还显示开着)",
        HOOK,
        "    read: (raw) => (raw === '1' ? true : raw === '0' ? false : fallback),",
        "    read: (raw) => raw === '1',",
    ),
    # -------------------------------------------------- BLOCK-09 的新门
    (
        "U6",
        "清空勾选挂回某个 setter(后退换筛选时动作条仍说「已选 20 件」)",
        LIST,
        "  }, [filters.signature])\n\n  /**\n   * 搜索框的字跟着 URL 走。",
        "  }, [])\n\n  /**\n   * 搜索框的字跟着 URL 走。",
    ),
    (
        "U7",
        "快审游标不跟着筛选复位(后退换类别后停在上一条队列的第 7 位)",
        REVIEW,
        "  useEffect(() => {\n    setIndex(0)\n  }, [urlFilters.signature])",
        "  useEffect(() => {\n    setIndex(0)\n  }, [])",
    ),
    (
        "U8",
        "游标写进 URL(贴给同事的地址指着另一件商品 —— 看起来精确、实际指错)",
        REVIEW,
        "  const urlFilters = useUrlFilters({ filter: enumParam<Filter>(FILTERS, 'ALL') })",
        "  const urlFilters = useUrlFilters({\n"
        "    filter: enumParam<Filter>(FILTERS, 'ALL'),\n"
        "    index: intParam(0, { min: 0 }),\n  })",
    ),
    # -------------------------------------------------- 一次写完
    (
        "U9",
        "换筛选不回第一页(第 5 页上改条件,停在新结果的第 5 页,像被清空了)",
        AUDIT,
        "            onChange={(value) => filters.patch({ entity_type: value, page: 1 })}",
        "            onChange={(value) => filters.patch({ entity_type: value })}",
    ),
    # -------------------------------------------------- 排序只搬一半
    (
        "U10",
        "排序退回组件内 state(刷新之后筛选还在、排序回到默认,而箭头也跟着回去)",
        TASKS,
        "    sort: enumParam<string>(TASK_SORT_KEYS),\n"
        "    order: enumParam<'asc' | 'desc'>(['asc', 'desc']),\n",
        "",
    ),
    # -------------------------------------------------- 跨语言
    (
        "U11",
        "每页条数加一档 500(后端 le=100,选中它整张列表变成一个 422)",
        LIST,
        "const PAGE_SIZES = [10, 20, 50, 100] as const",
        "const PAGE_SIZES = [10, 20, 50, 100, 500] as const",
    ),
    (
        "U12",
        "排序白名单多一个后端不认的键(后端静默退回默认排序,箭头还指着那一列)",
        TASKS,
        "const TASK_SORT_KEYS = ['created_at', 'provider', 'current_round', 'status'] as const",
        "const TASK_SORT_KEYS = ['created_at', 'provider', 'current_round', 'eta'] as const",
    ),
    # -------------------------------------------------- 旧 hook 复活
    (
        "U13",
        "页面自己动 URL(绕开唯一口子,又有了第二处真相)",
        AUDIT,
        "  const filters = useUrlFilters({",
        "  const [, rawSetParams] = useSearchParams()\n  void rawSetParams\n"
        "  const filters = useUrlFilters({",
    ),
    (
        "U14",
        "分页用 as 绕过运行期白名单(URL 写一档、刷新后查询退回另一档)",
        AUDIT,
        "page_size: pageSizeParam.narrow(nextSize)",
        "page_size: nextSize as (typeof PAGE_SIZES)[number]",
    ),
]

SUITE_FILTER = "test_a45_batch14_17_url_filters.py"


def run_suite(cwd: Path, name_filter: str = SUITE_FILTER) -> tuple[int, str]:
    proc = subprocess.run(
        [sys.executable, "tools/run_pure_tests.py", name_filter],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=1800,
        env={"PYTHONDONTWRITEBYTECODE": "1", "PATH": "/usr/bin:/bin", "HOME": str(cwd)},
    )
    return proc.returncode, proc.stdout + proc.stderr


def main() -> int:
    results = []
    for ident, description, rel, old, new in MUTATIONS:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "project"
            shutil.copytree(
                PROJECT,
                root,
                symlinks=True,
                ignore=shutil.ignore_patterns(
                    ".git", "node_modules", "__pycache__", "dist"
                ),
            )
            work = root / "backend"
            target = (work / rel).resolve()
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                results.append(
                    (
                        ident,
                        f"{description} —— 锚点出现 {text.count(old)} 次,拒绝变异",
                        False,
                        [],
                        False,
                    )
                )
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            code, output = run_suite(work)
            failed = [
                line.split("::", 1)[1].strip()
                for line in output.splitlines()
                if "FAILED" in line and "::" in line
            ]
            # 「导入期就炸了」的检测:**不能**只在整段输出里找异常名字。
            #
            # S6 那条变异(把 getattr 换成属性访问)的**正确**失败方式就是
            # 一条 AttributeError —— 它由守卫在用例内部触发,是一次真红。
            # 原来那版按子串扫全段输出,把它误报成「不算数」。
            #
            # 判据改成结构性的:套件正常跑完(打印了统计行)且点得出是哪条
            # 用例红的,就说明它没有炸在导入期。
            ran = "passed" in output and any(
                "FAILED" in line and "::" in line for line in output.splitlines()
            )
            crashed = not ran and any(
                marker in output
                for marker in (
                    "NameError",
                    "ImportError",
                    "SyntaxError",
                    "ModuleNotFoundError",
                    "AttributeError",
                )
            )
            results.append((ident, description, code != 0, failed, crashed))

    ok = True
    for ident, description, went_red, failed, crashed in results:
        mark = "RED " if went_red else "GREEN"
        note = ""
        if crashed:
            note = "  <- 导入期就炸了,这条变异不算数"
            ok = False
        elif went_red and failed:
            note = "  " + ", ".join(failed[:2])
        print(f"{ident:<4} {description}  {mark}{note}")
        ok = ok and went_red
    reds = sum(1 for r in results if r[2] and not r[4])
    print()
    print(f"{reds}/{len(results)} 条变异验红")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
