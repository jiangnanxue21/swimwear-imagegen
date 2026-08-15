"""配置取值白名单。

## 为什么单独一个模块

这三份白名单同时被两处需要：`core/config.py` 的启动期校验器，和实现层
（`workbench/batch.py`、`listings/copy_generator.py`）。两边各写一份字符串的话，
加了新生成器却忘了改校验器，表现是新生成器在启动期被拒——而报错信息指着
一份已经过期的白名单。

它们原来放在 `app/workbench/batch.py`，`config.py` 用**函数体内 import** 去取，
注释里写着「是为了避开模块级的环」。那个做法确实避开了环，但没有解决方向：
`app.core` 仍然依赖 `app.workbench`，只是把依赖藏进了函数体，
连 AST 静态检查都看不见（`test_import_graph.py` 当年就没看见）。

v4.1 Phase 0 接入 import-linter 之后这条被当场抓出来。修法不是给它开豁免，
是把东西放对位置：**这三份东西本来就是配置词汇表，不是批次逻辑。**
放进 core 之后依赖方向自然是对的，函数体内 import 也不再需要。

`workbench/batch.py` 仍然重新导出它们，老的引用路径不变。
"""
from __future__ import annotations

#: 文案生成器。`config` 的校验器与 `listings/copy_generator.py` 的注册表共用这一份。
COPY_GENERATORS: tuple[str, ...] = ("template", "llm")

#: 文本模型的 API 形状。写错的表现是 404，而 404 会被传输层归类成
#: 「厂商不可达」——排查方向从一开始就是错的，所以要在启动期拦下。
TEXT_API_STYLES: tuple[str, ...] = ("responses", "chat_completions")

#: FULL 视觉评分的 JSON 最多包含 11 个维度、问题、不确定项与事实一致性。
#: 1800 在开启轻量推理时已经被真实端点截断。8192 只是允许的上限,
#: 厂商按实际输出计量,不会要求模型把额度写满。
DEFAULT_VISION_MAX_OUTPUT_TOKENS = 8192

#: 这一项在设置页允许填到多大。
#:
#: 它必须和截断建议共用同一个数:输出被截断时,评分器会算一个「建议调到多少」
#: 塞进错误信息里。那个建议如果超过设置页的 maximum,运营照着填会被表单打回,
#: 而错误信息本身看不出任何问题 —— 一条读起来完全合理、却执行不了的建议。
#: 所以两边都从这里取。
#:
#: 注意这条**只管视觉评分**。`EXTRACTOR_MODEL_MAX_OUTPUT_TOKENS` 是识别层
#: 另一个独立的旋钮(默认 1500),数字碰巧一样不等于该共用常量。
MAX_VISION_MAX_OUTPUT_TOKENS = 32000

#: 批次执行模式。
#:
#:     inline   在请求内执行，但**每件一个独立事务**：请求断开时
#:              已完成的条目与回执不随之回滚
#:     celery   创建后立即提交返回，由 worker 领取执行，前端轮询
#:
#: 写错时**不能**静默退回 inline —— 那意味着运维以为批次跑在 worker 上，
#: 实际仍然挤在 HTTP 请求里，而这个差别只有在第一次超时时才暴露。
BATCH_EXECUTION_MODES: tuple[str, ...] = ("inline", "celery")
