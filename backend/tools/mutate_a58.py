#!/usr/bin/env python3
"""a58 变异验证:把每条守卫对应的决定改回去,确认它真的变红。

用法:python3 tools/mutate_a58.py

机制与 `tools/mutate_a57.py` 相同,理由不再重复。本批的特殊之处:

## 被变异的多数是前端源码,而前端在这个环境里跑不动

守卫只能读源码形状(`test_a58_prompt_center.py` 顶部写了它守不住什么)。
**正因为守卫弱,变异才更必要**:一条读源码的断言最容易长成"这个文件里有没有
人这么写过",而不是"这里写没写" —— a57 的 M10 就是这么被抓出来的。M3 / M4
是照着同一个形状造的:把入口条件放宽、把只读页开一个写口。

## M1 是本批的核心

`ui_reachable=True` 的全部依据是"两份提示词走同一段代码"。把 key 写回去之后,
男装那条路径重新变成没人走过的代码,而注册表上那个 True 会从一句真话变成
一句错话 —— **看起来可达、点进去出错,比原来的"显式不可达"更糟。**
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

DETAIL = "../frontend/src/pages/PromptsPage.tsx"
LIST = "../frontend/src/pages/PromptListPage.tsx"
VERSION = "../frontend/src/pages/PromptVersionPage.tsx"
APP = "../frontend/src/App.tsx"
TYPES = "../frontend/src/api/types.ts"
REGISTRY = "app/prompts/registry.py"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    (
        "M1",
        "详情页把 key 写回女装(男装那条路径重新没人走过,而注册表说它可达)",
        DETAIL,
        "  const promptKey = routeKey ?? ''",
        "  const promptKey = 'vision_system_prompt'",
    ),
    (
        "M2",
        "男装退回不可达,而页面已经能到它(注册表与界面反向分叉)",
        REGISTRY,
        "        ui_reachable=True,\n        consumers=(\n            \"app/evaluators/vision_schema.py:prompt_key_for 按受众选它,链路读库,API 可编辑\",",
        "        ui_reachable=False,\n        consumers=(\n            \"app/evaluators/vision_schema.py:prompt_key_for 按受众选它,链路读库,API 可编辑\",",
    ),
    (
        "M3",
        "列表页给不可编辑的项也开入口(保存成功、毫无效果)",
        LIST,
        "        row.editable && row.ui_reachable ? (",
        "        true ? (",
    ),
    (
        "M4",
        "只读页长出一个「切到这版」(把切换藏进一个标题写着只读的页面)",
        VERSION,
        "  const query = useQuery({",
        "  const activate = () => promptsApi.activate(promptKey, version)\n  const query = useQuery({",
    ),
    (
        "M5",
        "版本号不再校验(去请求 /versions/NaN,界面说「读取失败」)",
        VERSION,
        "  const versionValid = Number.isInteger(version) && version >= 1",
        "  const versionValid = true",
    ),
    (
        "M6",
        "分组顺序漏掉一层(那一组整组消失,不报错)",
        LIST,
        "const TIER_ORDER: PromptTier[] = ['FREE', 'TEMPLATE', 'MAPPING']",
        "const TIER_ORDER: PromptTier[] = ['FREE', 'TEMPLATE']",
    ),
    (
        "M7",
        "列表页自己编一份不可编辑的说辞(与注册表两份说辞会分叉)",
        LIST,
        "line.includes('editable=False 的原因')",
        "line.includes('__never__')",
    ),
    (
        "M8",
        "注册表里某处不可编辑的标记改回不同形(前端取原因取到空)",
        REGISTRY,
        '"editable=False 的原因:PRD §14.2 决议本轮只读',
        '"editable=False:PRD §14.2 决议本轮只读',
    ),
    (
        "M9",
        "/prompts 指回详情页(useParams 拿到 undefined,白屏)",
        APP,
        '<Route path="/prompts" element={<PromptListPage />} />',
        '<Route path="/prompts" element={<PromptsPage />} />',
    ),
    (
        "M10",
        "详情与单版本也塞进侧栏(两个指向同一件事的入口,其一还要先知道 key)",
        APP,
        "      { key: '/prompts', label: '提示词', icon: <FileTextOutlined /> },",
        "      { key: '/prompts', label: '提示词', icon: <FileTextOutlined /> },\n      { key: '/prompts/:key', label: '提示词详情', icon: <FileTextOutlined /> },",
    ),
    (
        "M11",
        "前端类型漏掉后端会返回的一列(拿得到但读不出来)",
        TYPES,
        "  ui_reachable: boolean\n  required_slots: string[]",
        "  required_slots: string[]",
    ),
    (
        "M12",
        "版本表的「查看」入口拿掉(想看一眼又只能先让它生效)",
        DETAIL,
        "          <Link to={`/prompts/${promptKey}/versions/${row.version}`}>查看</Link>\n",
        "",
    ),
]

SUITE_FILTER = "test_a58_prompt_center.py"


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
                ignore=shutil.ignore_patterns(".git", "node_modules", "__pycache__", "dist"),
            )
            work = root / "backend"
            target = (work / rel).resolve()
            text = target.read_text(encoding="utf-8")
            if text.count(old) != 1:
                results.append(
                    (ident, f"{description} —— 锚点出现 {text.count(old)} 次,拒绝变异",
                     False, [], False)
                )
                continue
            target.write_text(text.replace(old, new, 1), encoding="utf-8")
            code, output = run_suite(work)
            failed = [
                line.split("::", 1)[1].strip()
                for line in output.splitlines()
                if "FAILED" in line and "::" in line
            ]
            crashed = any(
                marker in output
                for marker in ("NameError", "ImportError", "SyntaxError", "ModuleNotFoundError")
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
