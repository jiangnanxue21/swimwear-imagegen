"""商品字段的长度上限 —— 一份定义,三处使用。

以前这几个数字散在三个地方,而且互相不一致:

    数据库列    spu/sku VARCHAR(64)、name VARCHAR(255)、material VARCHAR(128)
    API 出入参  Field(max_length=...) 各写各的
    批量导入    一律截断到 255

于是批量导入里一个 200 字符的 SKU 在解析阶段被判为合法,一直到 flush 才撞上
VARCHAR(64) 报错 —— 报出来的还是数据库异常,行号也丢了。

现在三处都读这一份。只用标准库,因此没装 pydantic 的纯测试环境也能导入。
"""
from __future__ import annotations

#: 字段名 -> 最大字符数。与 app/models/product.py 的列宽一一对应,
#: 由 tests/pure/test_review_fixes.py 静态比对守住。
PRODUCT_FIELD_LIMITS: dict[str, int] = {
    "spu": 64,
    "sku": 64,
    "name": 255,
    "category": 64,
    "primary_color": 64,
    "material": 128,
}

#: 辅助颜色:最多几个、每个多长
MAX_SECONDARY_COLORS = 10
MAX_SECONDARY_COLOR_LENGTH = 64

# ---------------------------------------------------------------- 集合上限
#
# BE-GLOBAL-07:自由集合与自由 JSON 没有业务上限。
#
# 没有上限的代价不是"内存爆了"——在这些接口上它长这样:
#
#   一次识别传 5000 个素材 id      同步跑真实模型链路,请求挂死,而运营看到的是超时
#   一版图片集 2000 项            批准时逐项校验,详情页 N+1 变成 2000 次查询
#   provider_params 塞 1MB        进幂等指纹,于是每次都是新指纹,幂等彻底失效
#
# 数字取"业务上明显不可能达到"的量级,而不是"系统扛得住"的量级:
# 上限的作用是把手滑和脚本失控挡在门外,不是做容量规划。
MAX_IMAGE_SET_ITEMS = 200
#: 一次识别最多喂几张素材 / 认领几个字段
MAX_EXTRACT_MEDIA_IDS = 50
MAX_EXTRACT_FIELDS = 50
#: 一次属性确认最多几项
MAX_CONFIRM_ITEMS = 100
#: 一个生成任务最多几张输入素材
MAX_INPUT_ASSET_IDS = 20

#: 一轮最多出几张候选图。**这个数不只是接口校验,它同时是耗时预算的输入。**
#:
#: A45-batch12-5 / NEW-03:它原来只写在 `schemas/generation.py` 的
#: `Field(4, ge=1, le=8)` 里,而 `workflows/dispatch_policy` 那边按 **4 张**
#: 手写死了"一轮最长要跑多久"。两个数字都对得起自己那一行,合起来却是错的:
#: 接口放行 8 张,阈值按 4 张算,于是一个完全合法的 8 张任务会被回收器
#: 判成卡死 —— 而它正在花钱评分。
#:
#: 放进这里之后两边读同一份:接口上限一改,租约与卡死阈值跟着改。
MIN_CANDIDATE_COUNT = 1
MAX_CANDIDATE_COUNT = 8
#: provider_params 的键数与序列化后的字节数(它会进幂等指纹)
MAX_PROVIDER_PARAM_KEYS = 50
MAX_PROVIDER_PARAM_BYTES = 8 * 1024


def limit_for(field: str) -> int:
    """取某个字段的上限。没登记的字段按 255 处理(与最宽的文本列一致)。"""
    return PRODUCT_FIELD_LIMITS.get(field, 255)


def too_long(field: str, value: str) -> bool:
    return len(value) > limit_for(field)
