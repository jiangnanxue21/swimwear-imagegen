"""这套部署产出的东西**能不能当真**(REVIEW 任务 5)。

## 它回答的问题

运营在界面上看到一张图、一个属性值、一条上架回执,没有任何地方告诉他
这些是真的调了外部服务得来的,还是本地假数据。而这两者在界面上**长得一模一样**
—— Mock 出图是真图片文件,Mock 属性是合理的中文取值,Simulator 上架会返回
一个像模像样的平台单号。

报告 §10 的人工测试准入标准第 4 条就是这件事:「Mock/真实可见 —— 界面截图;
切到 Mock 后状态条改变」。

## 为什么判定放在纯模块里

各注册表(`providers` / `evaluators` / `extractors` / `channels`)已经各自
报得出 `configured` / `implemented` / `is_simulator`。缺的不是事实,是**把四份
事实合成一句话**的那条规则,而那条规则有真正的取舍(见 `overall`),
应该能被穷举验证,不该藏在一个要起 FastAPI 才能跑的地方。

所以这里只收普通字典,不 import 任何注册表 —— 组装在 `services/environment.py`。

## 一个刻意不做的事

不判"存储后端"和"批次执行模式"的真假。它们影响**可靠性**(本地存储不适合
生产、inline 执行会阻塞请求),不影响**产出的真实性**。把它们混进同一个判定,
结果是一套接了真 Provider 的开发环境被标成"不可信",而那句警告一旦出现在
不该出现的地方,下一次它真该出现时就没人看了。它们照样报出去,只是分在
`deployment` 里,不参与 `overall`。
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class Fidelity(StrEnum):
    """一个环节的产出**能不能当真**。

    用 `StrEnum` 而不是 `(str, Enum)`:全仓另外几个枚举
    (`BatchLiveness` / `ReclaimAction` …)都是 `StrEnum`,而这里混用会让
    `ruff` 的 UP042 常红 —— 一条常红的规则等于没有规则,下一条真问题
    会淹在同一行输出里。行为上唯一的差别是 `str(Fidelity.REAL)`
    从 `"Fidelity.REAL"` 变成 `"REAL"`,而本模块的出口一律走 `.value`,
    没有任何调用点依赖前者(改之前 grep 过)。
    """

    #: 真的在调外部服务。产出可以当真
    REAL = "REAL"
    #: 本地假数据。产出**看起来是真的,但不是**
    SIMULATED = "SIMULATED"
    #: 选的是真的,但没配好。它会在运行时抛错 —— 不是"能用",也不是"假的"
    UNCONFIGURED = "UNCONFIGURED"
    #: 这一环压根没实现。骨架在,请求映射没写
    UNAVAILABLE = "UNAVAILABLE"


#: 严重程度排序,`overall` 取最"响"的那一个。
#:
#: SIMULATED 排在 UNCONFIGURED 前面,理由是**它们的失败方式不同**:
#: 没配好的环节会当场抛错,人一定会发现;而 Mock 会安静地产出一批合理的
#: 假数据,然后那批数据会被批准、被写进草稿、被导出成上架文件。
#: 「会静默产生错误结论」比「跑不起来」更需要一条常驻提示。
_LOUDNESS = {
    Fidelity.SIMULATED: 3,
    Fidelity.UNAVAILABLE: 2,
    Fidelity.UNCONFIGURED: 1,
    Fidelity.REAL: 0,
}


@dataclass(frozen=True)
class Facet:
    """一个会影响产出真实性的环节。"""

    key: str
    label: str
    fidelity: Fidelity
    #: 当前生效的那个后端叫什么。运营截图时这一格就是证据
    backend: str
    #: 一句给人看的话。**说后果,不说状态名**
    detail: str


def facet_fidelity(
    *, implemented: bool, configured: bool, is_simulator: bool
) -> Fidelity:
    """单个环节的判定。三个输入都是注册表已经报出来的事实。

    顺序不能换:

        没实现   -> UNAVAILABLE   还没写完,配不配都轮不到它
        是模拟器 -> SIMULATED     实现了、也"配好了",但产出是假的
        没配好   -> UNCONFIGURED  真的,但缺 Key

    `is_simulator` 必须排在 `configured` 前面:Mock 的 `is_configured()`
    永远返回 True(它不需要任何配置),所以先看 configured 的话,
    一个纯 Mock 环境会被判成 REAL —— 这正是这个模块要防的那件事。
    """
    if not implemented:
        return Fidelity.UNAVAILABLE
    if is_simulator:
        return Fidelity.SIMULATED
    if not configured:
        return Fidelity.UNCONFIGURED
    return Fidelity.REAL


def overall(facets: list[Facet]) -> Fidelity:
    """整套环境的判定:取最响的那一个。

    没有任何环节时返回 REAL 而不是抛错 —— 这个函数的调用点是一条横幅,
    而"因为算不出来所以整页报错"比"少一条横幅"糟得多。
    """
    if not facets:
        return Fidelity.REAL
    return max((f.fidelity for f in facets), key=lambda f: _LOUDNESS[f])


def is_trustworthy(fidelity: Fidelity) -> bool:
    """产出能不能当真。只有 REAL 算数。

    单独一个函数而不是 `== REAL`,是因为调用点会有好几个,而"哪些档算可信"
    这件事将来可能变(例如加一档"真 Provider + Mock 评分")——
    那时改这里一处,不是去找所有 `== REAL`。
    """
    return fidelity is Fidelity.REAL


def pick_active(rows: list[dict], name: str | None) -> dict | None:
    """从注册表的一堆行里挑出**实际会被用到的那一个**。

    先认注册表自己报的 `active`,再按名字找。两条都落空时返回 None ——
    **不拿第一行顶上**。

    拿第一行顶的后果是:表的顺序变一次,状态条的结论就变一次,而没有人
    会想到去查表的顺序。而这一档的结论是"运营现在看到的东西是不是真的",
    它不该取决于一个 `sorted()` 的稳定性。
    """
    for row in rows:
        if row.get("active"):
            return row
    if name:
        for row in rows:
            if row.get("name") == name or row.get("channel") == name:
                return row
    return None


def build_facet(
    *,
    key: str,
    label: str,
    row: dict | None,
    wanted: str,
    real_detail: str,
    simulated_detail: str,
) -> Facet:
    """把一行注册表事实翻译成一个档位 + 一句给人看的话。

    `row is None` 判成 UNAVAILABLE 而不是 UNCONFIGURED:配置里写了一个
    注册表里没有的名字,那不是"没配好",是配错了 —— 两句话指向不同的动作。

    缺 `implemented` 键时按 True 算。四个注册表里只有 providers 和
    evaluators 报这一列,而不报的那两个(extractors / channels)的行
    本来就只登记已实现的后端。默认 False 的话它们会全部被判成 UNAVAILABLE。
    """
    if row is None:
        return Facet(
            key=key,
            label=label,
            fidelity=Fidelity.UNAVAILABLE,
            backend=wanted,
            detail=f"配置里选的是 {wanted},但注册表里找不到它。这一步现在跑不通。",
        )
    fidelity = facet_fidelity(
        implemented=bool(row.get("implemented", True)),
        configured=bool(row.get("configured")),
        is_simulator=bool(row.get("is_simulator")),
    )
    backend = str(row.get("name") or row.get("channel") or wanted)
    detail = {
        Fidelity.REAL: real_detail,
        Fidelity.SIMULATED: simulated_detail,
        Fidelity.UNCONFIGURED: f"{backend} 是真的,但没配好(缺 Key 之类)。这一步会在运行时报错。",
        Fidelity.UNAVAILABLE: f"{backend} 还没实现,只有骨架。这一步跑不通。",
    }[fidelity]
    return Facet(key=key, label=label, fidelity=fidelity, backend=backend, detail=detail)
