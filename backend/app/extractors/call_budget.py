"""付费识别调用的预算闸。**纯判定,零三方依赖。**

## 为什么它不能只是服务层里的一个 if

识别现在还是同步路径(§4.6 的异步化在阶段 3 第二批),一次请求里逐图串行
调付费模型。没有上限,"给一件传了 200 张图的商品点一次识别"就是 200 次
付费调用外加一个必然超时的 HTTP 请求 —— 而点的人在界面上看到的只是一个按钮。

判定放在这里,是因为它的失败模式全部是**边界**:上限之内、正好等于、
超出一张、Mock 不受限。这些分支只有在零依赖模块里才穷举得起来;
留在服务层的话,覆盖"正好等于上限时放行"就要起一个数据库,
而那种测试没人会为一个比较运算去写。
"""
from __future__ import annotations

#: 默认上限。**不是判据** —— 真正的取值由 `EXTRACTOR_MAX_IMAGES_PER_RUN`
#: 配置项决定,运维改了就该生效。写在这里只是给没配过的部署一个起点。
#:
#: 12 的来历:一件商品的样品图通常是正面 / 背面 / 细节 / 吊牌各几张,
#: 十来张覆盖得住;再多的多半是把整批商品的图传到了一件上。
DEFAULT_MAX_IMAGES_PER_RUN = 12


def effective_ceiling(ceiling: int) -> int:
    """真正生效的上限。**每一个关心上限的人都必须问它,不许自己兜底。**

    非正数上限当作"没配好",按默认值兜底而不是放行:把 0 理解成"不限"
    会让一次手滑的配置变成一张巨额账单。设置页对这一项写了
    `minimum=1`,但配置有三条来源(数据库覆盖 → Settings → 环境变量),
    只有第一条经过那张表单 —— `.env` 里写个 0 谁都拦不住。

    A45-batch14-2 / F3:兜底原来长在 `over_call_budget()` 内部,于是
    **判定用 12、提示语用 0**:闸按默认值拦人,拒绝信息却告诉运营
    "一次识别最多 0 张图"。那句话没有任何批量满足得了,运营照着做
    只会一直撞墙,而真正生效的那个数字在代码里谁也看不见。
    兜底抽成函数之后两边读同一份 —— 与 `field_limits` 是同一条路子。
    """
    return ceiling if ceiling > 0 else DEFAULT_MAX_IMAGES_PER_RUN


def over_call_budget(*, image_count: int, ceiling: int, billable: bool) -> bool:
    """这次识别会不会超出单次付费调用预算。

    `billable` 为假时恒不超:Mock 不花钱,而它是 CI 与演练要跑的那条路径,
    给它设上限只会让演练数据集的规模变成一个需要记住的限制。

    问的是抽取器自己的 `billable`,不维护"哪些抽取器要花钱"的名单 ——
    名单会和注册表分叉,分叉的表现是新接的付费抽取器不受限。
    """
    if not billable:
        return False
    return image_count > effective_ceiling(ceiling)


def over_budget_message(*, image_count: int, ceiling: int) -> str:
    """超限时给运营看的话。**说清怎么继续**,不只是拒绝。

    只说"太多了"的话,运营唯一能做的是删素材 —— 而素材本身没有问题。

    报的是 `effective_ceiling()`,不是入参:运营要照着这句话分批,
    那句话里的数字就必须是闸真正在用的那个(F3)。
    """
    limit = effective_ceiling(ceiling)
    return (
        f"一次识别最多 {limit} 张图(本次 {image_count} 张)。"
        f"用 media_asset_ids 分批提交,或等异步识别上线后整批跑 —— "
        f"这道上限拦的是'一次点击 = 一整批付费调用'"
    )
