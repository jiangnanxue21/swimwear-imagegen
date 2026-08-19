"""REVIEW III.2:失败提示的统一出口 `<ErrorNotice>`(A12)是全站口径,但仍有大量页面
把 error 拍平成一句话直接渲染,于是那些页面上管理员拿不到请求编号与技术详情。评审的
关键一句是「**且没有棘轮测试防止新增**」—— 没有守卫,新页面照样再写一个,债不降反增。

## 这个文件的第一版是弱的,记在这里

初版只数一种形状:`<Alert … readError( … >`。它数出 17 处,与评审点名的 17 吻合,
于是「吻合」被当成了口径正确的证据。但那条正则只认现存代码恰好长的那个样子:

        <Alert type="error">{readError(e)}</Alert>          初版看不见(写成子元素)
        <Alert description={<span>{readError(e)}</span>} />  初版看不见(属性里套 JSX)
        <Result subTitle={readError(e)} />                   初版看不见(换个组件)

**而棘轮的全部意义就是拦新增的。** 一道只认旧形状的守卫,对新写法一律放行 ——
它看起来在守,实际不守,正是本仓反复记的「绿着的守卫说着一件不一定成立的事」。
下面 `test_the_detector_sees_the_shapes_that_defeated_the_first_version` 用合成样本
把这三种形状钉住,防止检测器再退回去。

## 现在的口径:不看 JSX 形状,只问「这个 readError 是不是 toast」

    宽口径(主基线)   pages/ + components/ 里**非 toast** 的 readError 调用点,合计 18
    窄口径(锚点)     `<Alert…readError(` 这一种,合计 14 —— 保留它是为了不丢失与
                     评审那条记录的对应关系,不是因为它够用

宽口径不解析 JSX,所以换写法绕不过去:只要 `readError` 出现在展示层且不是
`message.error(readError(…))` 这类 toast,就计为债。`message.error(...)` / `notification.*`
是 `readError` 的正当用法(一句话的浮层本就不该展开技术详情),明确排除。

**宽口径 18 是窄口径 14 的超集,不等于「24 处都必须迁成 `<ErrorNotice>`」。** 有些
计入的点(例如把错误串进一句「模特列表没拉到,下面是空的不代表没有模特」的提示)
未必值得整块 `<ErrorNotice>`。这里守的是**这个数只能下降**,不是「每一处都是错的」。

## a67 第一次真的还了六处

在此之前这份基线只涨过一次(a58 新页面被拦下、当轮改掉)、没降过 —— **棘轮让债不涨,
而没有人还**。a67 迁了六处,基线 24 -> 18、窄口径 17 -> 14:

    Dashboard / SystemStatus / Spend    整块 Alert 换 ErrorNotice。Spend 那处顺带
                                        把手写的重试按钮交回组件 —— 连同「用按钮
                                        而不是无 href 的锚点」那条无障碍理由
    ProductList / TaskList / AuditLog   **失败被画成了"没有数据"**:错误句渲染在
                                        表格的 emptyText 里,一次拉取失败在界面上
                                        和「还没有 SKU」长得一模一样,而这两者的
                                        下一步完全相反(重试 vs 去创建)。
                                        改成表上方一条 ErrorNotice,空表文案留给真的空

后面这三处不是机械替换 —— **统一出口顺带把"失败"和"空"分开了**,而那才是 A12
真正在买的东西。

## 还债与限度

迁走一处就把该文件在 `BROAD_BASELINE` 里的数字下调一格,迁到 0 删掉该行。
`current == baseline` 双向锁:多了拦、少了也拦(少了说明有人迁了却忘了调基线,
留着虚高的基线等于给未来的新债留缝)。

**限度要说清:** 这仍是读源码的结构守卫,不是行为验证。它证明不了迁移后的页面
真的渲染出了请求编号 —— 那属于 Vitest / Playwright,在只有 python3 的机器上跑不了。
先跑 Vitest 那才是行为;这里守的是不再长新债。而且任何基于文本的检测器都有边界:
把 `readError` 先赋给一个变量再渲染,它同样看不见。它抬高的是门槛,不是不可绕过。
"""
from __future__ import annotations

import re

from tests.pure._helpers import PROJECT_ROOT

FRONTEND_SRC = PROJECT_ROOT / "frontend" / "src"

#: 宽口径:每文件冻结的「非 toast 的 readError 渲染点」数,合计 24。
#: 迁走一处就把对应数字下调;迁到 0 删掉该行。新文件不许出现在这份名单之外。
BROAD_BASELINE: dict[str, int] = {
    "components/GenerationPlanPanel.tsx": 2,
    "components/workbench/BatchActionBar.tsx": 1,
    "pages/MediaLibraryPage.tsx": 1,
    "pages/ProductDetailPage.tsx": 1,
    "pages/PromptsPage.tsx": 2,
    "pages/ReviewDetailPage.tsx": 1,
    "pages/ReviewQueuePage.tsx": 1,
    "pages/SettingsPage.tsx": 2,
    "pages/TaskDetailPage.tsx": 1,
    "pages/WorkbenchBatchPage.tsx": 2,
    "pages/WorkbenchReviewPage.tsx": 4,
}

#: 窄口径:评审点名的 `<Alert…readError(` 那一种。保留作锚点。
NARROW_ALERT_TOTAL = 14

_READ_ERROR = re.compile(r"readError\(")
#: toast 是正当用法:一句话的浮层本就不展开技术详情
_TOAST = re.compile(
    r"(?:message|notification)\s*\.\s*(?:error|warning|info|success)\s*\(\s*readError\("
)
_NARROW_ALERT = re.compile(r"<Alert\b[^>]*?readError\(", re.S)


def _code_only(src: str) -> str:
    """去掉块注释、JSX 注释与行注释。反向断言必须只看代码在做的事。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"\{/\*.*?\*/\}", "", src, flags=re.S)
    return re.sub(r"^\s*//.*$", "", src, flags=re.M)


def _broad_count(code: str) -> int:
    """非 toast 的 readError 调用点数。不解析 JSX —— 换写法绕不过去。"""
    return len(_READ_ERROR.findall(code)) - len(_TOAST.findall(code))


def _scan() -> dict[str, int]:
    counts: dict[str, int] = {}
    for folder in ("pages", "components"):
        for path in sorted((FRONTEND_SRC / folder).rglob("*.tsx")):
            code = _code_only(path.read_text(encoding="utf-8"))
            n = _broad_count(code)
            if n > 0:
                counts[path.relative_to(FRONTEND_SRC).as_posix()] = n
    return counts


def _scan_narrow() -> int:
    total = 0
    for folder in ("pages", "components"):
        for path in sorted((FRONTEND_SRC / folder).rglob("*.tsx")):
            code = _code_only(path.read_text(encoding="utf-8"))
            total += len(_NARROW_ALERT.findall(code))
    return total


def test_the_detector_sees_the_shapes_that_defeated_the_first_version():
    """检测器自检:初版正则漏掉的三种写法,现在必须都算得进去。

    这条不读仓库,只喂合成样本 —— 它守的是**检测器本身**没有退回只认一种形状。
    一并钉住 toast 仍被排除,免得把口径收得过宽、误伤合法用法。
    """
    seen_by_narrow_only = [
        '<Alert type="error">{readError(e)}</Alert>',
        "<Alert description={<span>{readError(e)}</span>} />",
        "<Result subTitle={readError(e)} />",
    ]
    for sample in seen_by_narrow_only:
        assert _broad_count(sample) == 1, f"宽口径漏掉了这种写法:{sample}"

    # toast 是正当用法,不许被计成债
    assert _broad_count("message.error(readError(err))") == 0
    assert _broad_count("notification.error(readError(err))") == 0


def test_broad_baseline_total_is_twentyfour():
    """守住基线总数本身,口径不能悄悄漂走。"""
    assert sum(BROAD_BASELINE.values()) == 18


def test_the_narrow_alert_shape_still_matches_the_review():
    """窄口径锚点:评审当时点名 17 处 `<Alert…readError()>`。

    它变了不一定是坏事(迁移会让它降),但**它变了而宽口径没动**说明有人只是换了个
    写法绕过 `<Alert>`,债一处没少。这条失败时先去看宽口径有没有同步下降。
    """
    assert _scan_narrow() == NARROW_ALERT_TOTAL


def test_no_new_file_grows_the_debt():
    current = _scan()
    new_files = sorted(set(current) - set(BROAD_BASELINE))
    assert not new_files, (
        "这些文件新写了「非 toast 的 readError 渲染点」—— 请改用 "
        "`<ErrorNotice error={原始error}>`(原始 error 进去,请求编号/技术详情才有落点):\n  "
        + "\n  ".join(new_files)
    )


def test_no_file_exceeds_its_frozen_debt():
    current = _scan()
    regressed = {
        f: (current[f], BROAD_BASELINE[f])
        for f in BROAD_BASELINE
        if current.get(f, 0) > BROAD_BASELINE[f]
    }
    assert not regressed, (
        "这些文件的债多于冻结基线(债只能减不能增):\n  "
        + "\n  ".join(f"{f}: {now} > {base}" for f, (now, base) in regressed.items())
    )


def test_migrated_files_must_lower_the_baseline():
    """实际计数低于基线 = 有人迁了却没调基线。留着虚高的基线等于给新债留缝。"""
    current = _scan()
    stale = {
        f: (current.get(f, 0), BROAD_BASELINE[f])
        for f in BROAD_BASELINE
        if current.get(f, 0) < BROAD_BASELINE[f]
    }
    assert not stale, (
        "这些文件已迁移(实际低于基线),请把 BROAD_BASELINE 下调到实际值"
        "(迁到 0 就删掉该行):\n  "
        + "\n  ".join(f"{f}: 实际 {now} < 基线 {base}" for f, (now, base) in stale.items())
    )
