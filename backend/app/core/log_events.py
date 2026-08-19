"""运行日志的事件分类法(`docs/LOG-CONSOLE.md` §3)。**唯一事实来源。**

## 这个模块回答什么

在它之前,日志里唯一的机器可读分类是两样:

    logger    代码结构。`app.services.poll_service` 说不出"这是发布轮询"
    message   一句自由英文。措辞一改,过滤器**安静漏事件**

`tools/watch_ai_logs.py`(已删,改名成 `tools/watch_logs.py`)硬编码了 9 条
消息原文当过滤集,而消息是给人读的 —— 改一个词,查看器就少一类事件,
不报错、不提示。这正是本仓最忌讳的失效方式。

同时,新代码已经自发长出了另一种写法:`batch.stale_outcomes`、
`publish.poll_status_changed`、`spu.create_reused_request_key` —— 拿短码当 message。
写日志的人已经在要一套分类法,只是没立起来,于是 message 既要当人话又要当键,
两个都当不好。

所以这里立三张表:

    DOMAINS                 十五个业务域 -> 中文标签
    EVENTS                  事件码 -> 中文标签 + 例行标记
    LOGGER_DOMAIN_FALLBACK  logger 前缀 -> 域(没写 event 的调用点的兜底)

Web 页的筛选下拉、CLI 的 `--domain` 取值、formatter 的推导,**全部 import 这里**。
中文标签只在这一处 —— `AuditLogPage` 前端持有两张翻译表是"拉到数据之前就要能
列出取值"的历史妥协,ops 页不重复它,用 `/api/ops/logs/meta` 拿(硬规则第 4 条)。

## 为什么放在 `core`

零依赖:不 import sqlalchemy / fastapi / redis,连 `app.core.config` 都不碰。
formatter 在任何异常处理路径上都会跑,多一条跨层导入就多一次循环导入风险;
而且 `tests/pure/` 要能穷举它。

## 域的判据是**业务领域**,不是模块路径

`poll_service` 里的每一条日志都在讲发布轮询,所以它归 `publish` 而不是
`gen` —— 排障的人问的是「上架怎么了」,不是「poll_service 怎么了」。

**这一条与 `docs/LOG-CONSOLE.md` §3.2 的表格不一致,是刻意的。** 那张表把
`poll_service` 划给了 `gen`,多半是把它当成了"生成轮询";而这个文件里五条
调用点的 message 全部以 `publish.` 开头(`publish.poll_status_changed` 等),
`CLAUDE.md` 也写明它是发布链路七模块之一。文档写错的那一半按代码订正,
理由记在 `docs/DECISIONS.md` §3.80。

## 加一个事件要做什么

1. 在 `EVENTS` 里登记(域前缀必须在 `DOMAINS` 里);
2. 在调用点写 `extra_fields={"event": "...", ...}`。

两条都做才过得了守卫 —— `tests/pure/test_a53_log_console.py` **双向**比对:
写了没登记会红,登记了没人写**也会红**。后者是刻意的:一个没有任何调用点
产出的事件码,会在筛选下拉里摆出一个永远筛不出东西的选项,
而运营会以为是"这段时间没发生",不是"这个码是假的"。
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# ============================================================ 域

#: 十五个域。**从 210 个调用点的实际分布归并而来,不是凭空设计。**
#:
#: 顺序有意义:界面左侧轨道按这个顺序排,而它大致是一件商品从请求进来
#: 到上架出去的顺序。改顺序会改运营每天扫的那一列的形状,别顺手排字母序。
DOMAINS: dict[str, str] = {
    "http": "请求",
    "auth": "登录与鉴权",
    "app": "进程生命周期",
    "gen": "生成流水线",
    "llm": "模型调用",
    "eval": "评分与审核",
    "attr": "属性提取",
    "media": "素材",
    "listing": "图片集与文案",
    "spu": "建档",
    "batch": "批量任务",
    "publish": "发布上架",
    "output": "产出与导出",
    "settings": "设置与密钥",
    "ops": "维护与清理",
}


# ============================================================ 例行分组

#: 例行事件在流视角里折叠成一根计数条,而计数条要说得出**折的是什么**:
#: 「例行 ×12 · 租约让位 ×9 · 幂等复用 ×3」。所以 routine 不是一个布尔,
#: 是一个分组名 —— 只写 True 的话,那根条上只能显示一个总数,
#: 而"9 次租约让位"和"9 次幂等复用"要采取的行动完全不同。
ROUTINE_GROUPS: dict[str, str] = {
    "lease": "租约让位",
    "idempotent": "幂等复用",
    "lifecycle": "生命周期",
    "access": "访问日志",
}


#: 级别序。**API、CLI、折叠判定共用这一张。**
#:
#: 上一版没有它:`api/ops_logs.py` 用 `level == 精确值` 过滤,而
#: `tools/watch_logs.py` 的 `--level` 是「最低级别」。同一个词在两个入口
#: 是两种意思,而设计文档写的是「一个人在终端学会的过滤,换到页面上不用重学」。
#: 更糟的是精确匹配把 §1.4 那个病放了回来:运营选 WARNING 想找问题,
#: ERROR **被过滤掉了**。
LEVEL_ORDER: dict[str, int] = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}

#: 认不出的级别按 INFO 算 —— 第三方 logger 偶尔会写自定义级别名,
#: 把它们当成"最低级"会让筛选悄悄漏掉,当成"最高级"会让它们永远不被折叠。
_UNKNOWN_LEVEL_RANK = 20

#: 不折叠的下限。**ERROR 与 CRITICAL 都在线上。**
#:
#: 上一版 API 写的是 `level != "ERROR"`,于是一条 CRITICAL 的例行事件照样被
#: 折进计数条 —— 而 CLI 写的是 `< 40`,两边对 CRITICAL 的结论相反。
NEVER_FOLD_FROM = LEVEL_ORDER["ERROR"]

#: 访问日志的事件码。环形的自指过滤(`core/log_ring.py`)要按它认人,
#: 而那里不许再抄一份字面量 —— 抄了就是第二张分类表。
ACCESS_EVENT = "http.request_completed"

#: 任务链路里一轮的里程碑。链路模式按 `round` 字段分段,段头取这条事件的摘要
#: (`docs/LOG-CONSOLE.md` §5.2)。
#:
#: **它必须住在这里,不能住在前端。** 硬规则第 4 条:ops 页源码里不许出现
#: 任何事件码字面量。所以 `api/ops_logs.py` 在每一条上给出一个布尔
#: (`round_summary`),前端只认那个布尔 —— 哪条事件当段头是分类法的一部分,
#: 而分类法只有一份。
ROUND_SUMMARY_EVENT = "gen.round_evaluated"


@dataclass(frozen=True)
class LogEvent:
    """一个事件码的元数据。

    ``label`` 是界面上显示的中文;``message`` 由调用点自由书写,想怎么改就怎么改。
    这正是分类法要换来的东西:**措辞与键分家**。

    ``routine`` 标在**事件**上,不是调用点参数 —— 判据是"单独一条不承载决策
    信息、成批出现的过程事件"。它是注册表里的一次判断,不是每个调用点各自
    再判一次;后者的结果一定是同一类事件在两个文件里标得不一样。
    """

    key: str
    label: str
    routine_group: str | None = None

    @property
    def routine(self) -> bool:
        return self.routine_group is not None

    @property
    def domain(self) -> str:
        return self.key.split(".", 1)[0]


def _e(key: str, label: str, routine_group: str | None = None) -> tuple[str, LogEvent]:
    return key, LogEvent(key=key, label=label, routine_group=routine_group)


#: 事件码 -> 元数据。码的形状:`域.动作[_限定]`,小写下划线,英文。
#:
#: **这张表不是全量 210 条。** 迁移是"渐进补精度"而不是"一次换血":没写
#: `event=` 的调用点由 `LOGGER_DOMAIN_FALLBACK` 兜底,照样有域可归,
#: 只是没有中文标签、不能按事件精筛(§3.4)。所以这里只登记真的接了线的。
EVENTS: dict[str, LogEvent] = dict(
    (
        # ---------------- http:每请求一条 ----------------
        #
        # 例行,但**只对 2xx/3xx 有意义** —— 折叠规则对 ERROR 不生效,
        # 计数条内含 WARN 时单独点名(§3.5)。所以这里标 routine 是安全的:
        # 一次 500 不会被折进"例行 ×12"里消失。
        _e("http.request_completed", "请求完成", "access"),
        _e("http.unauthenticated_rejected", "未通过身份校验"),
        _e("http.business_error", "业务错误"),
        _e("http.validation_failed", "请求参数不合法"),
        _e("http.unhandled_error", "未处理异常"),
        _e("http.dotfile_blocked", "拦下了一次点文件请求"),
        # ---------------- auth ----------------
        _e("auth.login_accepted", "登录成功"),
        _e("auth.login_rejected", "登录失败"),
        _e("auth.login_throttled", "登录被限流"),
        _e("auth.logout", "退出登录"),
        _e("auth.media_signature_rejected", "素材签名无效或已过期"),
        # ---------------- app:进程生命周期 ----------------
        _e("app.started", "进程启动"),
        _e("app.stopped", "进程停止"),
        _e("app.secrets_dir_exposed", "密钥目录暴露在对外目录内"),
        _e("app.price_book_unparseable", "价目表无法解析"),
        _e("app.lease_budget_shortfall", "批次租约短于识别预算"),
        _e("app.pool_capacity_low", "连接池小于请求并发上限"),
        # ---------------- llm:模型调用 ----------------
        #
        # 这一组是 `docs/LOG-CONSOLE.md` §7 那张样板映射表,逐条落地。
        # 三条 routine 是一次调用的生命周期:单看没有信息量,合起来看是噪音,
        # 但在**链路模式里全展** —— 排一次具体调用时,"发出去了几次"正是答案。
        _e("llm.request_prepared", "请求已准备"),
        _e("llm.attempt_started", "开始一次尝试", "lifecycle"),
        _e("llm.response_received", "收到响应", "lifecycle"),
        _e("llm.request_completed", "调用完成"),
        _e("llm.attempt_failed", "调用尝试失败"),
        _e("llm.retrying", "准备重试", "lifecycle"),
        _e("llm.image_compacted", "图片被压缩以适配请求预算"),
        _e("llm.request_fitted", "请求被裁剪以适配端点预算"),
        _e("llm.image_inline_fallback", "图片改为内联发送"),
        _e("llm.exif_rotation_failed", "无法应用 EXIF 方向,原样发送"),
        # 这一条不在 §7 的表里 —— 那张表写着「llm 域的完整映射」,却漏了
        # `images.py` 的第四个调用点,同时收进了一条其实住在 `evaluators/`
        # 的(`llm.request_fitted`)。两处都按代码订正。
        _e("llm.image_url_unverifiable", "公开地址核不通,回落内联"),
        # ---------------- gen:生成流水线 ----------------
        #
        # 五条 routine 全是并发让位。它们是**健康并发的正常表现**,
        # 而它们和"这一轮白花钱了"曾经同为 WARNING 各占一行 —— 看日志的人
        # 很快学会跳过前一种,然后连后一种一起跳过(§1.4)。
        _e("gen.task_already_claimed", "任务已被别的 worker 领走", "lease"),
        _e("gen.formatting_already_leased", "成片阶段已被领走", "lease"),
        _e("gen.provider_results_already_leased", "回收阶段已被领走", "lease"),
        _e("gen.scoring_already_leased", "评分阶段已被领走", "lease"),
        _e("gen.phase_lease_taken_over", "阶段租约被接管", "lease"),
        _e("gen.scoring_lease_unavailable", "拿不到评分租约", "lease"),
        _e("gen.idempotency_race_resolved", "幂等竞争已裁决", "idempotent"),
        _e("gen.provider_status", "出图服务商状态", "lifecycle"),
        _e("gen.dispatch_relay_pass", "补投巡检完成", "lifecycle"),
        _e("gen.phase_committed", "阶段已提交", "lifecycle"),
        _e("gen.round_evaluated", "本轮评分完成"),
        _e("gen.task_auto_approved", "任务自动通过"),
        _e("gen.task_escalated", "任务转人工复核"),
        _e("gen.task_stopped", "任务停止"),
        _e("gen.task_reactivated", "搁浅任务已复活"),
        _e("gen.task_not_stranded", "任务并未搁浅,跳过复活"),
        _e("gen.stalled_tasks_reaped", "回收了搁浅任务"),
        _e("gen.resuming_phase", "断点续跑"),
        _e("gen.all_candidate_downloads_failed", "本轮候选图一张都没下来"),
        _e("gen.candidate_download_failed", "一张候选图下载失败"),
        _e("gen.candidates_persist_failed", "候选图落库失败"),
        _e("gen.provider_result_not_ready", "出图结果还取不到"),
        _e("gen.provider_result_missing", "已下载的候选图从结果集里消失"),
        _e("gen.provider_fewer_candidates", "出图服务商返回的候选比上一轮少"),
        _e("gen.provider_billing_mismatch", "服务商报的计费量与推算不一致"),
        _e("gen.provider_option_dropped", "丢弃了不支持的出图选项"),
        _e("gen.submit_incomplete", "提交未干净收尾"),
        _e("gen.formatting_aborted", "成片中止"),
        _e("gen.formatting_failed", "成片失败"),
        _e("gen.formatting_source_missing", "成片续跑时选中的候选图已不存在"),
        _e("gen.candidate_unverifiable", "成片前无法核对选中的候选图"),
        _e("gen.repair_switch_provider", "按修复规则切换出图服务商"),
        _e("gen.repair_unrecordable", "修复策略记不下来,改为转人工"),
        _e("gen.scoring_rescheduled", "评分暂时失败,已重排"),
        _e("gen.scoring_escalated", "评分重试仍失败,转人工"),
        _e("gen.escalated_without_regeneration", "转人工且不再重生成"),
        _e("gen.pipeline_crashed", "生成流水线异常"),
        _e("gen.db_unavailable", "数据库不可用,稍后重试"),
        _e("gen.failure_state_unpersisted", "失败状态写不下去"),
        _e("gen.lease_release_failed", "阶段租约释放失败"),
        _e("gen.dispatch_abandoned", "反复失败后放弃投递"),
        _e("gen.dispatch_attempt_failed", "投递失败,转由补投接手"),
        _e("gen.batch_dispatch_failed", "批次投递失败,条目留在待跑"),
        _e("gen.usage_record_updated", "更新既有用量记录"),
        _e("gen.sample_fingerprint_unavailable", "样例指纹取不到"),
        _e("gen.model_template_missing", "任务没有可用的模特模板"),
        _e("gen.license_subject_missing", "试穿任务缺少可核对的授权主体"),
        _e("gen.plan_row_missing", "生成方案行已不存在"),
        _e("gen.plan_angle_mismatch", "方案机位数与候选数不再匹配"),
        _e("gen.stray_pending_rows", "收尾时发现多余的待处理行"),
        _e("gen.candidate_savepoint_rollback_failed", "候选保存点回滚失败"),
        _e("gen.attempt_close_failed", "尝试收尾失败"),
        # ---------------- eval:评分与审核 ----------------
        _e("eval.completed", "视觉评分完成"),
        _e("eval.round_failed", "整轮评分失败"),
        _e("eval.partial_failure", "部分候选图没评上"),
        _e("eval.incomplete_scoreboard", "记分板不完整,拒绝定档"),
        _e("eval.prescreen_failed", "预筛失败,退回全量评分"),
        _e("eval.response_parse_failed", "评分响应解析失败"),
        _e("eval.payload_unparseable", "评分结果无法解析"),
        _e("eval.call_failed", "评分调用失败"),
        _e("eval.prompt_missing", "取不到生效的评分提示词"),
        _e("eval.prompt_fallback", "回落到内置评分提示词"),
        _e("eval.reference_images_truncated", "参考图超限已截断"),
        _e("eval.mock_fallback", "评分器不可用,回落 Mock"),
        # AI 测试留档(PRD §12.5)。归 eval 而不是 settings:运营查它的
        # 场景是「这次测试跑出了什么」,而不是「谁改了设置」。
        # 文案测试也走这两个码 —— 域按**业务领域**分,不按哪条链路产生的
        _e("eval.ai_test_recorded", "AI 测试已留档"),

        _e("eval.ai_test_record_failed", "AI 测试留档失败"),
        _e("eval.unusable", "评分器不可用且不允许回落"),
        _e("eval.test_connection_failed", "评分连通性自检异常"),
        _e("eval.outputs_rebuilt", "换图后已重出成品"),
        _e("eval.rebuild_source_unreadable", "换上的候选图读不到,拒绝重出"),
        _e("eval.rebuild_failed", "重出成品失败"),
        _e("eval.approved_source_unreadable", "已批准的候选图读不到,拒绝成片"),
        _e("eval.candidate_unverifiable", "成片前无法核对候选图"),
        _e("eval.formatting_failed", "人工批准后成片失败"),
        # ---------------- attr:属性提取 ----------------
        _e("attr.extraction_reused", "复用了已有的识别运行", "idempotent"),
        _e("attr.force_rerun", "明确重跑并替换已完成识别"),
        _e("attr.idempotency_race_resolved", "识别幂等竞争已裁决", "idempotent"),
        _e("attr.no_idempotency_key", "本次识别没有幂等键"),
        _e("attr.extraction_finished", "识别完成"),
        _e("attr.extraction_image_failed", "一张图识别失败"),
        _e("attr.scope_all_images_failed", "某个颜色范围的图全部失败"),
        _e("attr.call_completed", "识别调用完成"),
        _e("attr.field_out_of_scope", "模型返回了范围外的字段"),
        _e("attr.merge_no_value", "合并没有产出可用值"),
        _e("attr.merge_conflict_manual_wins", "与人工值冲突,保留人工值"),
        _e("attr.merge_rejected_by_contract", "合并值被字段契约拒绝"),
        _e("attr.divergence_detected", "多轮识别结论分歧"),
        _e("attr.projections_repaired", "已修复属性投影"),
        _e("attr.dispatch_failed", "识别投递失败,转由补投接手"),
        _e("attr.worker_failed", "识别 worker 异常"),
        # ---------------- media:素材 ----------------
        _e("media.provenance_conflict_quarantined", "来源冲突,已隔离"),
        _e("media.provenance_conflict_deduped", "来源冲突,按去重处理"),
        _e("media.lost_dedupe_race", "去重竞争让位", "idempotent"),
        _e("media.migration_mismatch", "素材迁移前后不一致"),
        _e("media.shadow_write_skipped", "产出没有源素材,跳过影子写"),
        # ---------------- listing:图片集与文案 ----------------
        _e("listing.copy_rejected_quick_review", "文案快审退回"),
        _e("listing.copy_validation_failed", "文案未通过校验"),
        _e("listing.copy_rules_changed", "文案规则已变,重新校验"),
        _e("listing.copy_version_collision", "文案版本号相撞,重试", "idempotent"),
        _e("listing.image_set_rejected_quick_review", "图片集快审退回"),
        _e("listing.image_set_coverage_gap", "图片集带着变体缺口通过"),
        _e("listing.image_set_downgraded_audience", "受众变了,图片集降级"),
        _e("listing.image_set_downgraded_quarantine", "素材被隔离,图片集降级"),
        _e("listing.image_set_version_collision", "图片集版本号相撞,重试", "idempotent"),
        _e("listing.item_angle_ambiguous", "素材来源机位不一致,留空机位"),
        # ---------------- spu:建档 ----------------
        _e("spu.create_reused_request_key", "复用了已有的建档请求", "idempotent"),
        # ---------------- batch:批量任务 ----------------
        _e("batch.create_reused_request_key", "复用了已有的批次请求", "idempotent"),
        _e("batch.create_lost_idempotency_race", "批次幂等竞争让位", "idempotent"),
        _e("batch.lease_lost_before_execution", "执行前租约已丢失", "lease"),
        _e("batch.async_finished", "异步批次跑完"),
        _e("batch.run_failed", "批次执行异常"),
        _e("batch.db_unavailable", "数据库不可用,批次稍后重试"),
        _e("batch.stale_outcome_discarded", "丢弃了一条过期回执"),
        _e("batch.stale_outcomes", "本次结算里有过期回执"),
        _e("batch.unexplained_failures", "有失败条目没有失败原因"),
        _e("batch.leases_reaped", "回收了过期的条目租约"),
        _e("batch.redispatched", "重投了停滞的批次"),
        _e("batch.in_flight_marker_failed", "在途标记写入失败"),
        _e("batch.in_flight_claim_failed_refusing_paid_call", "抢不到在途标记,拒绝付费调用"),
        _e("batch.provider_call_marker_missing_row", "调用标记找不到对应行"),
        _e("batch.provider_call_marker_failed", "调用标记写入失败"),
        _e("batch.provider_call_marker_failed_refusing_paid_call", "调用标记写不下,拒绝付费调用"),
        _e("batch.billed_result_unknown_refusing_paid_retry", "已计费但结果未知,拒绝付费重试"),
        _e("batch.receipt_requires_paid_retry", "回执要求一次付费重试"),
        _e("batch.lease_reaper_failed", "租约回收任务异常"),
        # ---------------- publish:发布上架 ----------------
        _e("publish.enqueue_reused", "复用了已有的提交", "idempotent"),
        _e("publish.rejection_already_recorded", "这条驳回已经记过", "idempotent"),
        _e("publish.lease_lost_before_dispatch", "投递前租约已丢失", "lease"),
        _e("publish.stale_outcome", "丢弃了一条过期的投递结果"),
        _e("publish.delivery_dead", "投递放弃"),
        _e("publish.redeliver_dead", "重投仍然放弃"),
        _e("publish.external_id_mismatch", "平台侧外部 ID 对不上"),
        _e("publish.poll_status_changed", "平台侧状态变化"),
        _e("publish.poll_needs_attention", "轮询结果需要人看"),
        _e("publish.poll_deferred_to_delivery", "轮询让位给投递"),
        _e("publish.poll_transport_failed", "轮询请求失败"),
        _e("publish.rejection_reflow_skipped", "驳回回流被跳过"),
        _e("publish.outbox_delivery_failed", "发布投递任务异常"),
        _e("publish.poll_failed", "发布轮询任务异常"),
        # ---------------- output:产出与导出 ----------------
        _e("output.built", "成品已产出"),
        _e("output.orphans_cleaned", "清理了失败发布留下的孤儿对象"),
        _e("output.orphan_kept_referenced", "对象仍被别的记录引用,保留"),
        _e("output.orphan_delete_failed", "孤儿对象删不掉"),
        _e("output.delete_unsupported", "存储后端不支持删除"),
        _e("output.reference_check_failed", "清理前核对引用失败"),
        # ---------------- settings:设置与密钥 ----------------
        _e("settings.updated", "设置已更新"),
        _e("settings.overrides_unreadable", "读不到设置覆盖,回落环境变量"),
        _e("settings.prompt_saved", "提示词已保存"),
        _e("settings.prompt_activated", "提示词已启用"),
        _e("settings.prompt_reset", "提示词已恢复默认"),
        _e("settings.prompt_version_collision", "提示词版本号相撞,重试", "idempotent"),
        # FE-309 / FE-310(a60)。两条都不是"失败":前者是有人在你之前动过,
        # 后者是一次带理由的退回 —— 而运营查"上周谁把口径改回去了"时,
        # 靠的正是能按事件码筛出这一条
        _e("settings.prompt_save_conflict", "提示词保存版本冲突"),
        _e("settings.prompt_rolled_back", "提示词已回滚"),
        _e("settings.key_generated", "生成了设置加密密钥文件"),
        _e("settings.key_migrated", "密钥已迁出对外目录"),
        _e("settings.legacy_key_readable", "旧密钥文件仍可被下载"),
        _e("settings.key_migration_failed", "密钥迁移失败"),
        _e("settings.dir_permissions_unchanged", "密钥目录权限收紧失败"),
        _e("settings.encryption_unavailable", "设置加密不可用"),
        _e("settings.ciphertext_unrecognised", "存储的设置不是可识别的密文"),
        _e("settings.decrypt_failed", "设置解密失败"),
        _e("settings.price_book_unparseable", "价目表无法解析,调用将记为未计价"),
        # ---------------- ops:维护与清理 ----------------
        _e("ops.delist_queued", "已排入下架"),
        _e("ops.orphan_export_delete_failed", "孤儿导出文件删不掉"),
        _e("ops.stale_draft_refresh_failed", "过期草稿刷新失败"),
        _e("ops.stale_draft_refresh_pass_failed", "草稿刷新巡检异常"),
        _e("ops.dispatch_relay_failed", "补投巡检异常"),
        # 留档清理(a62)。归 ops 而不是 eval:运营查它的场景是
        # 「这张表为什么这么大」,那是运维视角 —— 与 spend 归 ops 同一条判据
        _e("ops.ai_test_purge_pass", "AI 测试留档清理巡检"),
        _e("ops.ai_test_purge_failed", "AI 测试留档清理失败"),
        _e("ops.stalled_task_reaper_failed", "搁浅任务回收异常"),
        _e("ops.backfill_progress", "回填进度"),
        _e("ops.backfill_skipped_orphan", "回填跳过一条孤儿记录"),
        _e("ops.requeue_failed", "重新入队失败"),
        # 启动时一条,记下这个进程的诊断窗口收不收 2xx/3xx 访问日志。
        # 少了它,"http 域怎么这么空"只能靠去读 `.env` 才答得出 ——
        # 而读 `.env` 的前提是先想到这里有个旋钮。
        _e("ops.log_ring_access_filtered", "访问日志进环形的模式"),
    )
)


# ============================================================ 兜底推导

#: logger 前缀 -> 域。**最长前缀匹配。**
#:
#: 210 个调用点不可能一次迁完,也不该为了迁移冻结其他开发。所以没写 `event=`
#: 的调用点走这张表:它覆盖全部持 `get_logger(__name__)` 的模块,于是
#: 迁移期间每一条日志都有域可归 —— 差别只是没有中文事件标签、不能按事件精筛。
#:
#: **新模块不进这张表就红**(守卫二)。这条是"查看器里永远不出现未分类"
#: 的全部保障:少一行不会报错,只会让那个模块的日志静静地落进兜底域。
LOGGER_DOMAIN_FALLBACK: tuple[tuple[str, str], ...] = (
    ("app.prompts", "settings"),  # 提示词注册表与版本机制(PRD §11.2 v2.0)
    # app.main 同时产出访问日志(http)与启动/停止(app)。兜底给 `app`,
    # 访问日志那三个调用点自己写了 `event="http.request_completed"` ——
    # **event 优先于 logger**,这正是它要解决的形状:一个模块两个域。
    ("app.main", "app"),
    ("app.api.auth", "auth"),
    ("app.api.media_files", "auth"),
    ("app.api.generation_tasks", "gen"),
    ("app.api.publish", "publish"),
    ("app.api.reviews", "eval"),
    ("app.attributes", "attr"),
    ("app.extractors", "attr"),
    ("app.tasks.attribute_tasks", "attr"),
    ("app.core.secrets", "settings"),
    ("app.evaluators", "eval"),
    ("app.listings", "listing"),
    ("app.llm", "llm"),
    ("app.media", "media"),
    ("app.providers", "gen"),
    ("app.scripts", "ops"),
    ("app.services.cleanup_service", "ops"),
    ("app.services.dispatch_service", "gen"),
    ("app.services.ai_test_archive", "eval"),
    ("app.services.evaluation_service", "eval"),
    ("app.services.generation_service", "gen"),
    ("app.services.output_service", "output"),
    # 发布轮询,不是生成轮询。见模块文档里那一段。
    ("app.services.poll_service", "publish"),
    ("app.services.prompt_service", "settings"),
    ("app.services.publish_service", "publish"),
    ("app.services.review_service", "eval"),
    ("app.services.settings_runtime", "settings"),
    ("app.services.settings_service", "settings"),
    # 花费台账。它既不是"生成"也不是"设置",而运营查它的场景是
    # 「这个月为什么这么贵」—— 那是运维视角,归 ops。
    ("app.services.spend", "ops"),
    ("app.services.spu_service", "spu"),
    ("app.tasks.batch_tasks", "batch"),
    ("app.tasks.generation_tasks", "gen"),
    ("app.tasks.maintenance_tasks", "ops"),
    ("app.tasks.publish_tasks", "publish"),
    ("app.workbench.batch_service", "batch"),
    ("app.workbench.platform_service", "publish"),
    # 工作台聚合:文案、草稿、导出。放在两条更长的前缀之后,靠最长匹配区分。
    ("app.workbench", "listing"),
)

#: 一条都匹配不上时落在哪。**只有第三方 logger 会走到这里**
#: (celery / uvicorn / alembic / sqlalchemy —— 它们跟着 root handler 一起进环形)。
#:
#: 选 `app` 而不是新造一个「未分类」:框架自己的日志本来就是进程级的,
#: 而"查看器里永远不出现未分类"(§3.4)是这一页的承诺。守卫读的是
#: `LOGGER_DOMAIN_FALLBACK` 本身,不经过这个兜底,所以新模块漏登记照样会红。
UNMATCHED_LOGGER_DOMAIN = "app"


def domain_of_event(event: str | None) -> str | None:
    """事件码的域。没写 event、或写了一个没登记的码,返回 None。

    **没登记的码不猜。** 按 `.` 前缀硬取会让一个打错的码
    (`llmm.attempt_failed`)长出一个不存在的域,而界面会老实地把它画出来。
    守卫已经保证登记过的码才会被写出来,这里返回 None 是给"守卫还没跑到"
    的那一刻用的。
    """
    if not event:
        return None
    known = EVENTS.get(event)
    return known.domain if known else None


def domain_for_logger(logger_name: str | None) -> str:
    """按 logger 前缀推导域。最长匹配优先。"""
    name = logger_name or ""
    best = ""
    domain = UNMATCHED_LOGGER_DOMAIN
    for prefix, candidate in LOGGER_DOMAIN_FALLBACK:
        if (name == prefix or name.startswith(prefix + ".")) and len(prefix) > len(best):
            best, domain = prefix, candidate
    return domain


def resolve_domain(event: str | None, logger_name: str | None) -> str:
    """一条日志属于哪个域。**event 优先,logger 兜底。**"""
    return domain_of_event(event) or domain_for_logger(logger_name)


def label_of(event: str | None) -> str | None:
    """事件码的中文标签。没登记返回 None —— 界面此时显示 message 原文。"""
    known = EVENTS.get(event or "")
    return known.label if known else None


def is_routine(event: str | None) -> bool:
    known = EVENTS.get(event or "")
    return known.routine if known else False


def level_rank(level: str | None) -> int:
    return LEVEL_ORDER.get(str(level or "").upper(), _UNKNOWN_LEVEL_RANK)


def level_at_least(level: str | None, minimum: str | None) -> bool:
    """这条日志够不够 ``minimum`` 这一档。**级别筛选一律走它。**

    `minimum` 为空表示不筛。判据是"及以上"而不是"恰好等于":选 WARNING
    的人要找的是问题,而 ERROR 是更严重的问题 —— 把它挡掉是把 §1.4 那个
    病换了个地方复发。
    """
    if not minimum:
        return True
    return level_rank(level) >= level_rank(minimum)


def normalize_level(name: str | None) -> str | None:
    """把**筛选条件**里的级别名规整成注册档位。空返回 None(不筛),打错抛 ValueError。

    与 `level_rank` 的未知兜底是两回事,方向相反,不许混用:那条兜底服务的
    是**日志行自己的级别** —— 第三方 logger 会写自定义级别名,按 INFO 算是
    合理的容错;筛选条件走同一条兜底的话,`level=WARN` 静默变成 rank 20,
    等于"不筛",整窗全回来,而打错的人以为自己看的是 WARNING 以上。

    CLI 对 `--domain` 与 `--event` 早就写明了口径:打错一个名字不该表现成
    "这段时间没有日志" —— 它的反面同样成立:也不该表现成"全都是日志"。
    API 与 CLI 的级别校验都调这一份。
    """
    if name is None or not str(name).strip():
        return None
    upper = str(name).strip().upper()
    if upper not in LEVEL_ORDER:
        ordered = "/".join(sorted(LEVEL_ORDER, key=LEVEL_ORDER.__getitem__))
        raise ValueError(f"未知的级别:{name!r};可用(不分大小写):{ordered}")
    return upper


def seq_sort_key(seq: object) -> tuple[str, int]:
    """``seq`` 的排序键。**数值序,不是字符串序。**

    `seq` 形如 ``{pid:x}-{n}``,n 是进程内单调计数。日志的 ts 只有秒级精度
    (`_TS_FORMAT` 里没有毫秒),同一秒内的先后**全靠它** —— 而字符串比较里
    ``"10" < "7"``,同一进程一秒内发第 7~10 条时展示顺序会倒置,链路模式的
    横幅上正写着「按时间顺读」。`RingHandler` 的文档说 seq 管"同一毫秒内的
    稳定排序",而 ts 根本没有毫秒:这个键要扛的其实是整整一秒。

    解析不出来的(空、没有 ``-``、计数不是数字)排在同秒最前(计数 -1),
    并保留原文本作次序键 —— 不猜、不炸,与 `parse_ts` 同一个姿势。
    """
    text = str(seq or "")
    proc, _, counter = text.rpartition("-")
    try:
        return (proc, int(counter))
    except (TypeError, ValueError):
        return (text, -1)


def folds_away(event: str | None, level: str | None) -> bool:
    """这条日志在流视角里能不能折进计数条。

    **判定只有这一份**,API 的 `_shape` 与 CLI 的 `Filter.accepts` 都调它。
    上一版两边各写各的,对 CRITICAL 的结论正好相反 —— 而"什么时候可以藏一条
    日志"是一条业务规则,规则有两份就等于没有。
    """
    return is_routine(event) and level_rank(level) < NEVER_FOLD_FROM


#: `JsonFormatter` 写出来的时间格式(`%Y-%m-%dT%H:%M:%S%z`)。
#: 解析放在这里而不是各自的调用点,和 `level_at_least` 同一个理由 ——
#: 「什么算一条日志的时间」有两份实现就等于没有。
_TS_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def parse_ts(value: str | None) -> datetime | None:
    """一条日志的时间。解析不出来返回 None,**不猜、不抛**。

    ## 为什么需要它(a55)

    环形是多个进程 LPUSH 进同一个键的,列表顺序 = **到达顺序**,不是
    时间顺序:worker 与 API 各自 fire-and-forget 写入(0.2 秒超时),
    排队与网络抖动都会打乱先后。而链路模式的横幅上写着
    「按时间顺读,旧在上」—— 一条 API 领取、worker 执行、API 回写的任务链路,
    展示顺序可能是错的,**而界面正在向你保证它是对的**。

    解析失败的返回 None 而不是一个兜底时间:兜底会让那条日志安静地
    落在某个位置上,而"它到底该排哪"恰恰是答不出来的。调用方把它们
    单独标出来 —— 不静默丢,也不假装它在某个位置。
    """
    if not value:
        return None
    try:
        return datetime.strptime(value, _TS_FORMAT)  # `%z` 匹配到才成功,恒为 aware
    except (TypeError, ValueError):
        pass
    # 容一手 ISO 8601 的其他写法(第三方 handler 偶尔会写别的格式)
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        # `astimezone()` 对极端时间(平台 time_t 范围之外)可能抛 OSError/
        # OverflowError —— 契约是"不猜、不抛",所以补不上时区就当解析失败:
        # 返回 None 的意思正是"不知道它在哪段时间里"。
        # **返回值永远带时区。** 上一版这条兜底路径把无时区输入原样返回
        # naive,而调用方拿它和主格式解析出的 aware 做比较与排序 —— Python
        # 对 naive×aware 直接抛 TypeError:`?since=2026-08-18T09:30:00`
        # 让 `/ops/logs` 整个 500;第三方 handler 往环形写一条无时区 ts,
        # **整页挂掉**。也就是说"容一手第三方写法"的那条路径,恰恰是让
        # 调用方崩溃的路径 —— 容忍的姿势自己成了事故源。
        #
        # 无时区按**本机时区**解释,不按 UTC、也不拒绝:写入端 `formatTime`
        # 用的就是本机墙上钟,在 URL 里手打 `since` 不带时区的人指的也是它;
        # 按 UTC 解释会让东八区的查询悄悄偏 8 小时 —— 偏了没人报错,
        # 而 500 至少会被看见,这里两个都不要。
        try:
            parsed = parsed.astimezone()
        except (OSError, OverflowError, ValueError):
            return None
    return parsed


def routine_group_of(event: str | None) -> str | None:
    """例行分组的**中文名**,给折叠计数条用(「租约让位 ×9」)。"""
    known = EVENTS.get(event or "")
    if known is None or known.routine_group is None:
        return None
    return ROUTINE_GROUPS.get(known.routine_group)


def events_of_domain(domain: str) -> list[LogEvent]:
    return [e for e in EVENTS.values() if e.domain == domain]
