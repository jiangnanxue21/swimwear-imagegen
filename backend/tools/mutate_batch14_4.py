#!/usr/bin/env python3
"""A45-batch14-4 变异验证:把 A8/A9 那 18 条变异照原样造回来。

用法:python3 tools/mutate_batch14_4.py

## 它和被退役的那份是什么关系

变异内容一条不改地来自 `tools/mutate_contract_tests.py`(batch14-3 退役)。
换掉的只有两件事:

    形状   `CASES` -> `MUTATIONS`,与在用的另外三份一致
    机制   改真实工作树 -> 复制到临时目录再改

后者不是顺手改的。老脚本把变异循环写在**模块顶层**、直接改工作树里的
`App.tsx` / `flow.py`,靠 `try/finally` 之外的一句 `write_text(original)` 还原 ——
中途 Ctrl-C 或者断言抛出,工作树就留在被改坏的状态里。而它没有 `__main__`
保护,任何人 import 它都会触发一整轮。

## 为什么派发到整个套件而不是点名一条测试

老脚本用 `getattr(m, test_name)()` 点名派发,而**测试改名/删除时
`AttributeError` 的退出码和断言失败一样是非零** —— 于是 18 条全部失效
却报告"全部被抓住",这正是它被退役的原因(REVIEW-A45-BATCH14-3 F7)。
按套件跑没有这个洞:套件不存在时 `run_pure_tests.py` 报 0 个用例,
`audit_anchors.py` 的 `SUITE_FILTER` 检查也会提前拦住。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BACKEND = Path(__file__).resolve().parent.parent
PROJECT = BACKEND.parent

APP_TSX = "../frontend/src/App.tsx"
TODAY = "../frontend/src/pages/TodayPage.tsx"
LAYOUT = "../frontend/src/components/AppLayout.tsx"
GUARD = "../frontend/src/components/UnsavedGuard.tsx"

#: (编号, 一句话, 相对 backend/ 的路径, 原文, 替换成)
MUTATIONS: list[tuple[str, str, str, str, str]] = [
    # -------------------------------------------------- 菜单与路由
    # a47 §4 把两条锚点改没了,原样记在这里:
    #   Q1 锚在「素材管理」那一行 —— 该项本轮改名「素材库」
    #   Q2 锚在 `/workbench-spus` 那一项 —— 该项本轮整条撤出菜单(路由保留)
    # 变异**意图**一字未改(往运营组塞管理入口 / 把菜单 key 拼错),
    # 换的只是它们各自钉在哪一行。改锚点而不是删变异:失锚的变异不报错、
    # 不变红,只是安静地什么都没验 —— 那正是 `audit_anchors.py` 存在的理由。
    (
        "Q1",
        "把 /settings 挪进普通运营菜单",
        APP_TSX,
        "      { key: '/media', label: '素材库', icon: <FolderOpenOutlined /> },",
        "      { key: '/media', label: '素材库', icon: <FolderOpenOutlined /> },\n"
        "      { key: '/settings', label: '设置', icon: <SettingOutlined /> },",
    ),
    (
        "Q2",
        "菜单项路由拼错一个字符",
        APP_TSX,
        "{ key: '/workbench', label: '商品工作台'",
        "{ key: '/workbenc', label: '商品工作台'",
    ),
    (
        "Q3",
        "默认路由退回指标仪表盘",
        APP_TSX,
        '<Route path="/" element={<Navigate to="/today" replace />} />',
        '<Route path="/" element={<Navigate to="/dashboard" replace />} />',
    ),
    (
        "Q4",
        "按角色裁剪路由(运营看到 NotFound 而不是「需要管理员」)",
        APP_TSX,
        '<Route path="/settings" element={<SettingsPage />} />',
        '{isAdmin && <Route path="/settings" element={<SettingsPage />} />}',
    ),
    # -------------------------------------------------- 首页
    (
        "Q5",
        "首页自己把两个计数加起来(待办口径出现第二处定义)",
        TODAY,
        "  const secondary = SECONDARY_ACTIONS.filter",
        "  const grand = SECONDARY_ACTIONS.reduce((a, c) => a + (byAction?.[c] ?? 0), 0)\n"
        "  void grand\n  const secondary = SECONDARY_ACTIONS.filter",
    ),
    (
        "Q6",
        "首页卡片写错一个动作码(那张卡永远显示 0)",
        TODAY,
        "to: '/workbench?next_action=RESOLVE_REJECTION'",
        "to: '/workbench?next_action=RESOLVE_REJECTIONS'",
    ),
    (
        "Q7",
        "「其余待办」漏掉一个动作码(整整一步对运营隐形)",
        TODAY,
        "  'RELEASE_QUARANTINE',\n",
        "",
    ),
    (
        "Q8",
        "首页自己抄一份运行中状态清单",
        TODAY,
        "        hint: '在等 Provider 出图,不用做什么。点开是全部任务列表',",
        "        hint: '在等 Provider 出图(PROVIDER_RUNNING 等),点开是全部任务列表',",
    ),
    (
        "Q9",
        "首页卡片跳到一条没注册的路由",
        TODAY,
        "        to: '/tasks?status=FAILED',",
        "        to: '/task?status=FAILED',",
    ),
    # -------------------------------------------------- 落地页 / 身份
    (
        "Q10",
        "落地页不再读 URL 筛选(点进去看到的是全部)",
        "../frontend/src/pages/TaskListPage.tsx",
        "import { enumParam, intParam, useUrlFilters } from '../hooks/useUrlFilters'",
        "// import removed",
    ),
    (
        # 锚点在 a46-phase2 订正过一次。原文钉的是
        # `const { isAdmin, who, loading: identityLoading } = useIdentity()`,
        # 而菜单不再按角色裁剪之后 `isAdmin` 早就从这行解构里去掉了 ——
        # 也就是说这条变异在交付包里**一次都没造出来过**,而脚本照样报"抓住"。
        # `audit_anchors.py` 正是为这种情况存在的,这次由它叫出来。
        "Q11",
        "菜单自己又开一份 whoami 查询(enabled 条件分叉)",
        LAYOUT,
        "  const { who, isAdmin, loading: identityLoading, sessionAuth, needsLogin } = useIdentity()",
        "  const { who, isAdmin, loading: identityLoading, sessionAuth, needsLogin } = useIdentity()\n"
        "  useQuery({ queryKey: ['auth-probe'] })",
    ),
    # -------------------------------------------------- 后端
    (
        "Q12",
        "by_next_action 少填一个码(首页那张卡整张消失)",
        "app/workbench/flow.py",
        "    by_action: dict[str, int] = {code.value: 0 for code in NextActionCode}",
        "    by_action: dict[str, int] = {\n"
        "        code.value: 0 for code in NextActionCode if code is not NextActionCode.FIX_COPY\n"
        "    }",
    ),
    (
        "Q13",
        "in_flight 自己抄一份状态清单",
        "app/services/dashboard_service.py",
        "                task_by_status.get(status.value, 0) for status in sm.IN_FLIGHT_STATES",
        '                task_by_status.get(s, 0) for s in ("QUEUED", "PROVIDER_RUNNING")',
    ),
    # -------------------------------------------------- 离开保护
    (
        "Q14",
        "换回非数据路由(useBlocker 会静默失效)",
        "../frontend/src/main.tsx",
        "<RouterProvider router={router} />",
        "<BrowserRouter><App /></BrowserRouter>",
    ),
    (
        "Q15",
        "布局路由不渲染 Outlet",
        LAYOUT,
        "              <Outlet />",
        "              {null}",
    ),
    (
        "Q16",
        "只挡站内导航、不挡刷新",
        GUARD,
        "    window.addEventListener('beforeunload', onBeforeUnload)",
        "    void onBeforeUnload",
    ),
    (
        "Q17",
        "保存后不放行挂起的导航",
        GUARD,
        "    if (blocker.state === 'blocked' && !dirty) blocker.proceed()",
        "    void blocker",
    ),
    (
        "Q18",
        "某个编辑面漏挂离开保护",
        "../frontend/src/components/workbench/CopyTab.tsx",
        '      <UnsavedGuard dirty={dirty} what="文案" />\n',
        "",
    ),
    (
        "Q19",
        "离开保护传了写死的 true(训练运营无脑点确定)",
        "../frontend/src/pages/SettingsPage.tsx",
        '<UnsavedGuard dirty={dirty} what="设置" />',
        '<UnsavedGuard dirty={true} what="设置" />',
    ),
    (
        # batch14-4 §五之三:守卫两种都拦,而变异只造了恒真那一种。
        # 两种的表现是**相反**的,而恒假这一种更难发现:
        #
        #     恒真   每次离开都弹窗 -> 运营学会无脑点确定 -> 真有改动那次也被点掉
        #     恒假   一次都不弹 -> 组件挂着、看起来有保护 -> 改动被静默吞掉
        #
        # 恒真至少还有人抱怨"老弹窗"。恒假没有任何征兆:代码里
        # `<UnsavedGuard ... />` 好端端挂着,`test_every_editing_surface_is_guarded`
        # 的第一条断言(有没有挂)也照样满足 —— 只有第二条(不许写死)拦得住它。
        # 只造恒真那一种,等于第二条断言里的 `"false"` 那半边从没被验过。
        #
        # 换一个面(提示词页)是刻意的:和 Q19 用同一个锚点会撞
        # `text.count(old) != 1`,而两条变异挑不同的面顺带多验一个挂载点。
        "Q20",
        "离开保护传了写死的 false(等于没挂,而且一点征兆都没有)",
        "../frontend/src/pages/PromptsPage.tsx",
        '<UnsavedGuard dirty={dirty} what="提示词" />',
        '<UnsavedGuard dirty={false} what="提示词" />',
    ),
]

SUITE_FILTER = "test_a45_batch14_4_fixes.py"


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
