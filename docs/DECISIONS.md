# 长期有效的决定

这份文档收的是**至今仍然约束着代码的决定**：迁移编号、架构约束、升级须知。

它按**主题**组织，不按阶段或评审轮次。原因很简单：来查「为什么迁移从 0011 起」
的人不知道那是 M0 定的，来查「阻断数为什么变小了」的人不知道那是 stage12 改的。
按轮次归档等于要求读者先知道答案才能找到答案。

来源是 26 份已删除的过程文档（阶段验收报告、M0–M6 里程碑、5 轮评审记录、
逐轮 CHANGES）。那些文档记的是**当时做了什么**；这里只留**现在仍然是什么**。

---

## 一、迁移编号

`backend/migrations/versions/0011_media_assets.py` 与 `0012_attribute_extraction.py`
的模块注释指向本节。

### 1.1 为什么从 0011 起，不是 0008

需求文档 §14.1 给自动上架链路排的迁移号是 0008 起。照抄会直接撞车 ——
0008 / 0009 / 0010 当时已被 Outbox、评分尝试、阶段执行租约占用
（0010 是上一轮代码评审第 4 条的产物）。全部顺延：

| 需求文档 §14.1 | 实际编号 | 内容 | 里程碑 |
| --- | --- | --- | --- |
| 0008 | **0011** | `media_assets`、`media_derivatives`、`media_consents`；改造 `generation_candidates` | M1 |
| 0009 | — | 影子写回填 | M1 · **不占迁移号**，见 1.2 |
| 0010 | **0012** | `product_attribute_extractions`、`attribute_evidence`、`product_attribute_values`、`attribute_calibrations` | M3 |
| 0011 | **0013** | `listing_image_sets`、`listing_image_items` | M5 |
| 0012 | **0014** | `content_plans`、`listing_copies` | M6 |
| 0013 | **0015** | `listing_drafts`；`products` 增列 `size` / `size_group` | M6 |
| 0014 | ~~0016~~ **待定** | `channel_applications`、`channel_accounts`、`channel_listings` | M8 |
| 0015 | ~~0017~~ **待定** | `publish_tasks`、`publish_attempts`、`publish_outbox`、`channel_webhook_events` | M8 |

> ⚠️ **M8 那两行的号已经不作数了。** 计划表写的 0016 / 0017 在计划之后被
> 0016（图片集兜底唯一索引）、0017（草稿导出留痕）、0018（批量任务与幂等回执）、
> 0019（平台侧状态与驳回台账）占满。**M8 从 0020 起，落地时以
> `ls backend/migrations/versions/` 的实际结果为准，不要照抄上表。**
> 这正是「照抄需求文档的编号会撞车」在同一个项目里发生的第二次。

### 1.2 回填不占迁移号

需求文档把历史数据回填排成一次数据迁移，M1 实现时改成了脚本
（`app/scripts/backfill_media_assets.py`）。理由：回填量按图算，而 Alembic
迁移跑在一个事务里，几十万行的 UPDATE 会长时间持锁。脚本可以分批提交、
中断续跑、先干跑。因此 0012 顺延给了属性表。

### 1.3 `generation_candidates` 是改造，不是新建

需求 §4.2 把它写成 `CREATE TABLE`，但这张表在 `0002_generation_tasks` 就建好了，
且当时直接持有 `storage_path` / `file_hash` / `width` / `height` / `mime_type`。
目标是让它退化成「任务 ↔ 素材」的关联表：

```
现状：generation_candidates.storage_path    -> 图片直接挂在候选上
目标：generation_candidates.media_asset_id  -> 指向 media_assets
```

**0011 不删旧列**，只加可空的 `media_asset_id`。回填之后置非空、删旧列都是
独立的清理任务 —— 迁移的每一步都要能单独回滚，不必恢复备份。

---

## 二、架构约束与守卫

每条约束都有一个测试盯着。**改这些行为时测试会红，那不是测试过时，是提醒你
回到这一节确认这个决定还成不成立。**

| # | 约束 | 为什么 | 守卫 |
| --- | --- | --- | --- |
| 1 | 影子写与业务写在**同一个事务**里 | 分两个事务的话，业务提交成功而影子写失败时两边永久不一致，而且没有任何报错 | `test_media_layer.py::test_shadow_write_happens_in_the_business_transaction`、`::test_all_three_write_paths_are_wired` |
| 2 | 素材去重键是 `(product_id, sha256)`，不是全局 `sha256` | 两个商品用同一张图是正常的，全局去重会让第二个商品的素材凭空消失 | `test_media_layer.py::test_dedupe_key_is_product_scoped` |
| 3 | `/media`（素材库，要口令）与 `/media-files`（签名文件，匿名可读）必须是两个前缀 | 前缀嵌套时匿名白名单会顺带把素材库放出去 | `test_media_layer.py::test_signed_file_endpoint_is_not_under_the_media_library_prefix` |
| 4 | 属性的**证据与值分表** | 一图一字段一条证据。合并成一张表就没法回答「这个值是凭哪几张图定的」 | `test_attribute_extraction.py` 全域、`test_m0_contracts.py::test_registry_enum_fields_align_with_core_enums` |
| 5 | 没有校准 = 什么都不自动确认。置信度是 `None`，不是 `0` | `0` 会被下游当成「算过了，很低」；`None` 才是「没算过」。前者会让未校准品类静默走自动确认 | `test_attribute_extraction.py::test_uncalibrated_returns_none_not_zero`、`test_m0_contracts.py::test_uncalibrated_confidence_is_none_not_zero` |
| 6 | 属性来源是**分层**，不是加权求和 | 供应商数据压过任意多条模型证据。求和会让「20 张图都这么说」盖掉一条权威事实 | `test_attribute_merge.py::test_supplier_data_beats_more_numerous_model_evidence`、`::test_ai_only_evidence_cannot_decide_a_value` |
| 7 | 已批准的图片集**不可原地修改**，重排走 derive 出新版本 | 驳回回流第一件事是定位到被驳回的那一版。原地改等于把证据擦掉 | `test_image_set_rules.py::test_approved_sets_cannot_be_edited_in_place` |
| 8 | 两条 COALESCE 唯一索引必须**同时**存在于 ORM 与迁移 | 只在迁移里建，ORM 侧的并发批准就绕得过去；只在 ORM 里声明，库上根本没有约束 | `test_migration_consistency.py::test_both_coalesced_indexes_exist_on_orm_and_in_migrations` |
| 9 | 文案规则**默认全禁**，不是默认全放 | 新增一个禁词库条目应当立刻生效；默认放行意味着漏配一处就等于没有规则 | `test_listing_copy.py::test_banned_words_survive_unicode_evasion`、`::test_banned_words_do_not_hit_inside_longer_words` |
| 10 | 生产环境评分器 **fail closed**，配错转人工，不回退 Mock | 回退 Mock 意味着配错的部署会**自动通过并发布**。这是唯一一条会直接发错图的约束 | `test_evaluators.py::test_production_never_falls_back_to_mock_when_fail_closed`、`::test_unreadable_config_defaults_to_fail_closed` |
| 11 | 字段来源 DSL 是**受限**的：白名单变换、禁止属性遍历 | 它读的是运营可编辑的渠道 spec。留一个通用表达式求值等于给 spec 一条任意代码执行的路 | `test_source_expr.py::test_attribute_traversal_is_rejected`、`::test_transform_whitelist_rejects_unknown_names` |
| 12 | LLM 层不许 import 上层；图片预处理与重试循环各只有一份实现 | 抽公共层的意义就是消掉重复。留两份实现，改一份不改另一份的缺陷不会被任何测试发现 | `test_llm_layer.py::test_llm_never_imports_an_upper_layer`、`::test_image_preparation_has_exactly_one_implementation`、`::test_retry_loop_has_exactly_one_implementation` |
| 13 | 过期口径只有一份：一律走 `service.refresh_draft` | 批次页自己凭指纹推断一遍，就会出现批次页说「3 件过期」、草稿页说「5 件过期」 | `test_workbench_batch.py` 全域、`test_stale_matrix.py::test_451_export_gate_refreshes_staleness_before_anything_else` |
| 14 | 只读接口的兜底 `session.rollback()` 必须是**返回前最后一件事**，且只有一次 | rollback 会 expire 会话里全部 ORM 对象，而 expired 对象的下一次属性访问是一条隐式 SELECT。放在出参组装之前 = 逐行往返；最贵的是 `sort_in_memory` 的 tiebreak，它跑在全量结果上而不是当前页 | `test_a41_fixes.py::test_read_only_endpoints_roll_back_after_the_last_orm_read` |
| 15 | 「把批次条目打回 PENDING」两处必须清同一组列 | `reset_items_for_retry`（人点重试）与 `reap_expired_leases` 的 REQUEUE 分支是同一个状态转换。漏掉 `finished_at` 不只是脏数据：`_liveness_out` 拿全部行的 `max(finished_at)` 当心跳，刚点完重试的批次会立刻被报成 STALLED | `test_a41_fixes.py::test_retry_reset_clears_the_same_columns_as_lease_reclaim` |
| 16 | 模糊搜索走 `core/search.like_pattern()`，且 `ilike` 必须带 `escape=` | `%` 与 `_` 是 LIKE 元字符。不转义时搜 `SW-001_BLK` 会命中 `SW-001XBLK`，搜一个 `%` 返回全表 —— 失败是安静的，只表现为「搜出来的东西不对」 | `test_a41_fixes.py::test_every_ilike_call_passes_the_escape_char` |
| 17 | 批量导出的 SKU 表必须带「所属 SPU」列 | 单件导出里两张表的对应关系是隐式的（SPU 表就一行）；批量把 N 个 SPU 的 SKU 行拼进同一张表，而 `row_fields` 里没有任何字段指向父 SPU。少了它，400 行 SKU 没有一列能说明归属，`manifest.csv` 也补不上（粒度在 SPU，而且平台拿不到它） | `test_a41_fixes.py::test_batch_sku_rows_can_name_their_parent_spu`、`::test_single_export_shape_is_untouched` |
| 18 | 投递未落地时，轮询不改本地状态（`PLATFORM_REJECTED` 除外） | `publish_policy._status_for` 已经写下：下架成功时平台通常仍返回那条商品、带着 `LISTED` 字样。投递路径知道自己发的是什么操作，轮询路径不知道。判据用「有没有未结算的 outbox 行」而不是状态集合——后者会在投递以 DEAD 收场时把行永久钉死 | `test_a41_fixes.py::test_polling_defers_to_delivery_while_an_outbox_row_is_open`、`::test_the_guard_is_self_limiting_not_a_status_whitelist` |
| 19 | 清理计划给人看的数字必须是**实际会执行**的数量 | `occupying` 只数真实商品，而 `run_delist` 遍历全部行（含 SIM-）。人工测试批次几乎全是模拟行，两个口径能差一个数量级——而这个模块的整套安全设计就是「限定作用域 + 动手前把数量给人看」 | `test_a41_fixes.py::test_plan_counts_match_what_delist_will_actually_queue`、`::test_the_cleanup_script_prints_the_execution_count` |

### 2.1 测试套件自身的两条纪律

**`tests/pure/` 全域不许 `import pytest`，也不许 `@pytest.mark.parametrize`。**
这个目录被两个 runner 跑：真实 pytest（CI、容器内）和
`backend/tools/run_pure_tests.py`（只要有 python3 就能跑）。后者直接以 `fn()`
无参调用发现的 `test_*`，加参数化会让 `make test-pure` 全线 TypeError。
需要合并同类用例时写**函数内表驱动循环**，并在断言消息里带上当前那一行的说明。

**CI 必须真的起 PostgreSQL。** 静态迁移对照测试已在测试套件清理时删除
（AST 认不出辅助函数、重命名和条件迁移，比真跑一遍弱）。现在
`tests/test_migrations.py` 是模型与迁移一致性的**唯一**防线，而它整个模块挂着
`requires_db` —— 没有库时它整体跳过，静默失去这道防线。

---

## 三、升级须知

按主题归并。原文散在 5 轮评审记录里，同一个主题往往被改过两三次，
这里只写**最终状态**。

### 3.1 必须做的人工动作

- **主密钥视为已泄露，必须轮换。** 早期交付压缩包里带出过
  `.secrets/.settings.key`（运行时生成的 Fernet 主密钥）。现在打包已排除该目录，
  但已经发出去的那把钥匙不能再用：配置 `SETTINGS_SECRET_KEY`
  （`openssl rand -base64 32`，或 `make secret-key`），或删除各节点 secrets 卷里的
  `.settings.key` 让系统重新生成；然后到设置页**重新录入所有 Provider Key**，
  使其用新钥重新加密。
- **生产环境 `ADMIN_TOKEN` 必须填。** 设置页的读接口现在也要口令，空着的话
  页面直接打不开。`APP_ENV=test` 的环境同样必须配。
- **`CORS_ORIGINS` 不要填 `*`**，改成具体来源。
- **必须部署 `beat` 进程**，且**只能起一个实例**。没有它，Outbox 里的意图只在
  快路径失败时躺着没人投；多个实例会重复排产同一个节拍。不想用 beat 的话，
  把 `make requeue APPLY=1` 挂 cron 每 5 分钟一次，效果等价。
- **升级前备份 `output_assets`。** 迁移 `0006` 会先清理历史数据里重复启用的记录
  （每个商品每个用途只留最新一条），然后才建唯一索引。

### 3.2 原本能跑通、现在会被挡下的操作

- **未实现的 Provider 不能再提交任务。** 以前 fal / ComfyUI 填了 Key 就能选，
  跑到 worker 才失败；现在创建任务时就挡下。
- **非开发环境下评分器配错会转人工审核**，而不是用 Mock 打分自动通过。
  部署前确认 `EVALUATOR_BACKEND` 与 `APP_ENV`。
- **创建任务的素材要求变严。** 必须有合格的正面图或抠图；虚拟试穿必须有启用中的
  模特模板或模特参考图。以前会退化成「随便拿第一张」。
- **没有候选图的审核不能直接通过**，只能重新生成或驳回。
- **提交阶段超时的任务不能直接重试。** 先去 Provider 后台核对确认没产生结果，
  再用 `POST /api/generation-tasks/{id}/retry?force=true`。这是为了避免
  「一键重试」变成一键再买一次。
- **批量导入超长字段报错，不再截断。** 以前 200 字符的 SKU 会被截到 255
  （实际列宽 64）然后在写库时抛数据库异常；现在解析阶段就报行级错误，
  带行号和字段名。历史上靠截断「导入成功」的数据不受影响，但重新导入同一个
  文件会报错。
- **模特模板图片检查未通过的默认停用**，不是启用。
- **平台状态 `REJECTED` 不能手工设置**，只能由「记录驳回」产生；有未解决驳回时
  平台状态哪儿也去不了，先标记解决，状态自动退回 SUBMITTED。

### 3.3 行为变了但不报错的（看板口径切换点）

这几条不会让任何东西失败，但依赖这些数字做趋势对比的话，注意切换点。

- **发布新成品图会停用同商品同用途的旧图。** 网站缓存里的旧 URL 仍能打开
  （旧记录停用而非删除）。
- **卡在进行中状态超过 30 分钟的任务会自动落成 FAILED。** 以前会永远停着。
  其中提交过请求的那些带 `SUBMIT_RESULT_UNKNOWN`，需要先对账再 force 重试。
- **取消现在真的会生效。** 以前 `SUBMITTING` 之后点取消没有任何效果；现在会在
  下一个阶段边界停下。如果有依赖「取消不生效」的运维习惯，需要调整。
- **阻断数变小了。** 上游未就绪（BLOCKED）的步骤不再产出字段级问题。以前一件
  刚建好的商品显示「阻断 5」（素材 2 + 每个必填属性 1），数字随品类属性数虚高；
  现在只数运营当下能处理的，同例为 2。
- **待确认数变大了。** 「草稿校验通过、等人点导出」的商品现在计入待确认
  （同时仍计入可导出）。这是修口径，不是多了新问题。
- **完成度计分改为任务书 §3.2 口径**：过期 0.0、进行中 0.3。以前 STALE 计 0.6、
  进行中 0.5，导致一件草稿过期的商品显示 92% —— 按完成度排序它沉底，
  而它恰恰是导不出去、最该被看的那件。
- **`mark_dispatch_failed()` 已删除。** 派发失败改由 Outbox 的 `attempts` /
  `last_error` 记录。

### 3.4 部署形态变更

- **Postgres / Redis 端口改绑 `127.0.0.1`。** Redis 是无认证的 Celery broker，
  发布到 `0.0.0.0` 等于把 API 层口令整个绕开。宿主机外要直连调试的话自行改回，
  或走 `make psql`。
- **worker / beat 对 backend 的依赖是 `service_healthy`**，不是 `service_started`。
  迁移在 backend 的启动命令里跑，以前 worker 会在迁移完成前起来，拿旧 schema
  消费任务。
- **新增 `secrets` 卷**，`docker compose up -d` 会自动创建。密钥目录
  （`SETTINGS_KEY_DIR`，默认 `.secrets/`）**独立于存储目录** —— 存储目录由
  `/files` 直接托管，密钥放进去等于对外可下载。启动时会复查这一点。
- **`OPERATOR_TOKENS` 有字符限制**：口令不能含逗号/分号，名字不能含冒号。

### 3.5 发布链路的三条取舍(任务 14 / 15 / 16)

- **轮询的退避封顶的是频率,不是次数。** 投递有 `MAX_ATTEMPTS`(每次重试都花钱、
  占配额);轮询没有放弃这个出口,只有 `MAX_POLL_INTERVAL_SECONDS`。理由是轮询
  是**读**:停下来的代价是本地看板与平台永久分叉,而且分叉之后没有任何机制会发现。
- **`next_poll_at` 与 `last_polled_at` 是两列。** 一次失败的轮询必须推进前者
  (否则同一行被反复领)、不许碰后者(它的含义是「我们对平台的了解是什么时候的」)。
  合成一列的表现是界面显示「刚刚同步过」而实际上已经断联数小时 —— 它让人不去查。
- **平台返回 404 永远不写 `DELISTED`。** 查不到可能是 ID 错了、可能是拿着 A 店铺
  的 ID 问了 B 店铺。猜成已下架的代价是一个仍挂在平台上的商品从清理清单里消失,
  而那正是 4.1 节 H 要防的结局本身。
- **API 上架的驳回不走 `locate_export()`。** 那个函数的前提(没导出过就谈不上被
  驳回)对手工 Excel 成立,对 API 自动上架不成立。改走 `record_api_rejection()`,
  换的是版本定位依据(提交尝试)不是表。**遗留缺口**:`resolve_gate()` 仍只认
  「驳回后有新导出」为修复证据,API 驳回目前关不掉,由任务 20 补齐。

### 3.6 a25 评审修复合并时确立的几条(A27)

- **`core/clock.utc_now()` 是全仓唯一的「现在几点」。** 不要再在别处写
  `datetime.now(UTC).replace(tzinfo=None)`。同时 `db/session.py` 在连接上钉死
  `-c timezone=utc`:业务时间列都是 `timestamptz` 而写进去的是 naive,
  「这个时间是几点」以前取决于数据库怎么配,现在取决于代码。
- **存储类不读配置,配置由工厂传入。** `LocalObjectStorage` 的 `api_prefix`
  是构造参数。在类里 import `app.core.config` 会把 pydantic 拖进
  `tests/pure/`(零三方依赖),一签 URL 就 ModuleNotFoundError。
- **`AUTH_FORBIDDEN` 与 `AUTH_FAILED` 分开。** 两者都是 4xx,但一个意思是
  「口令不对,去核对」,另一个是「口令没问题,这件事你做不了」。混用同一个码时,
  前端横幅会对着一把完全正确的口令说「后端不认」,诱导运营反复去改一个本来就对的东西。
- **口令写入返回是否**持久化**成功。** 隐私模式下只落内存,界面必须如实说
  「仅本次会话有效」。注意两把口令要各自求值再合并,`a() && b()` 会短路,
  第一把失败时第二把连内存都不会进。
- **预算耗尽日用 `ceil` 不用 `int(x)+1`。** 后者在整除时多报一天,而预算与
  日耗都是人定的整数,整除是常态。「第几天用完」没人会去验算,错了不会被发现。

### 3.7 发布接口层确立的几条(A28)
- **发布状态的派生全仓只有一份,在 `workflows/publish_view.py`。** 硬规则 4
  (前端不推断状态)在发布链路上最难守,因为「现在怎么样了」散在四处:
  listing 的当前事实、attempt 的历史结局、outbox 还投不投、驳回关没关。
  让前端拼这四样,拼错的方式很多。判定放在纯模块里而不是接口函数里,
  是为了让它能在 `tests/pure/` 被穷举 —— 写在接口里的话,覆盖「某个状态
  组合下按钮不该亮」要起一个 FastAPI 加一个库,那种测试没人会写。
- **`STALLED` 是一个组合状态,`PublishStatus` 里没有它。** listing 说
  SUBMITTING、attempt 说 IN_FLIGHT、outbox 已经 DEAD —— 四个来源里没有
  任何一个单独知道「这件事死了」。这也是 `EnqueueResult.reused_terminal`
  的消费点:幂等键含草稿指纹,草稿不变键就不变,于是一条退避耗尽的提交,
  运营再点「提交」仍然 `reused=True`,只看这一位的界面会说「正在处理」。
  接口层返回 `notice.will_deliver`,明确回答「这次点击之后会不会真的发一次」。
- **`rejection_auto_closeable` 用 `None` 表示「不适用」,不与 `True` 合并。**
  「没有驳回要关」和「有驳回且系统能自证已修复」是两件事,合并之后界面
  分不出来。判据是 `PlatformRejection.located_by == "publish_attempt"` ——
  那一列本来就是为「这条驳回从哪条路径来」存在的。
- **未解决的驳回不阻断提交。** 改完内容重新提交**正是**解决驳回的方式,
  挡住它等于告诉运营「你得先关掉驳回才能修驳回」。驳回只改变推荐动作
  (`next_action`),不改变按钮可用性(`allowed_actions`)。
- **下架走 `enqueue(DELIST)`,不开直连平台的近路。** 走同一条 Outbox 意味着
  它同样有幂等键、有退避、在异常队列里可见。开近路会让 4.1 节 H 的清理
  成为全链路上唯一没有重试、没有留痕的一步,而那恰恰是最不该出错的一步。
- **`safe_preview()` 是脱敏报文唯一的对外出口。** 接口层不直接调
  `_safe_snapshot`(跨包的私有名一改,调用点静态查不出来),也不另写一套
  脱敏 —— 两套迟早漂移,而漂移的方向一定是新的那套漏掉一个字段,
  漏掉的那次没有任何征兆,直到某个密钥出现在界面上。

### 3.8 「还会不会自己变」的三档口径(A32)

前端凡是要决定「还要不要继续轮询」的地方,以前都是**二档**:终态就停,
非终态就按固定节拍问。两条链路上各自出了同一个形状的缺陷,所以这一轮
把两边都改成三档,并把两份清单都钉到后端。

- **生成任务:终态清单必须等于 `state_machine.TERMINAL_STATES`,一个不多。**
  前端曾经多列了 `FAILED` 与 `MANUAL_REVIEW`,而后端这两个都有出边
  (`FAILED → QUEUED / FORMATTING / SCORING`,`MANUAL_REVIEW → MANUALLY_APPROVED
  / MANUALLY_REJECTED / REGENERATING`)。多一个值的后果**不是多刷几次**,
  是那个状态下页面永远停在旧数据上 —— 而「多出来的那个值」的定义恰恰是
  「后端还会改它」。运营 A 在审核台批准之后,运营 B 的列表到下班都显示
  待审核:两个人、两份事实,谁也不会觉得是 bug,只会觉得对方记错了。
- **停止轮询和降低频率是两个结论。** 中间那一档(`AWAITING_HUMAN`)是
  「机器不再写它了,但有人点一下就会继续走」。归档的判据不靠感觉:
  `STALLABLE_STATUSES` 的定义就是「worker 正持有、且不等外部输入」,
  它的注释里明写不含 `MANUAL_REVIEW`。契约测试据此断言两份清单不相交。
- **批次的 `liveness` 由后端算,前端只读(硬规则 4)。** 批次刻意不写 outbox
  (`send_batch` 顶部:条目本身就是意图记录),所以投递失败的那个 False
  只活在创建那一次的响应里、不落库 —— 刷新之后前端只看得到一个 QUEUED
  批次,而「非终态就轮询」会让它转到天荒地老。判定改从 `status` /
  `created_at` / `started_at` 和条目的 `finished_at` 现算,这四样都是真实列。
- **批次停滞用两个阈值,不是一个。** 认领那一档(300 秒)**没有误杀风险**:
  `started_at` 仍是 NULL 意味着没有任何 worker 碰过它。进度那一档
  (1800 秒)确实会误报 —— 一件跑得慢的付费调用和一个死掉的 worker
  在库里长得一模一样。两个数写成同一个的话,下一个人调其中一个时会连带
  把另一个也调了,而它们的误报代价根本不同。
- **STALLED 不停轮询,而是放慢到 30 秒。** 卡住的成因(Broker 挂了、worker
  没了)都会自己恢复,恢复之后批次继续跑。停掉的话运营要手动刷新才知道,
  而页面没有任何地方告诉他需要这么做 —— 那正是 FE-BATCH-01 修过的坑。
- **文案写「可能没派出去」,不写「已失败」。** 队列真的积压时它确实只是在
  排队;一句猜错的「已失败」会让运营去建第二个批次。

### 3.9 多币种:说出「这个数不是全部」,但不折算(A32)

`/spend` 的「本月已花费」和预算进度条**只统计主币种**。这个取舍本身早就
定了(汇率是又一个「看起来精确、实际是估的」数字,而这一页是用来提前发现
异常的,不是用来对账的),但在此之前界面上没有任何地方说那个数不是全部——
顶层只有一个 `daily_currencies` 币种代码清单,回答不了「少了多少」。

- **求和与金额格式化都留在后端。** `aggregate_by_currency` 放在
  `core/pricing.py` 而不是 `services/spend.py`,是因为后者顶层 import 了
  SQLAlchemy,进不了 `tests/pure/` —— 而这个函数正是需要被逐个取值验的那种。
  前端自己拼金额字符串就是第二份货币格式化,两份分叉的表现是同一笔钱在
  两个位置上写法不同,那比不显示更让人不敢信。
- **主币种那一行也在 `by_currency` 里,并带 `is_main`。** 只返回「其他币种」
  的话,前端要自己判断哪个是主的才能把两边对上,而那正是硬规则 4 禁止的
  那类判断。
- **币种代码两侧都要归一大小写。** `main_currency` 那一侧漏了归一时,
  `is_main` 会全是 False,于是页面说「另有 3.00 CNY 没有计入已花费」——
  而那 3 块钱正是已花费本身。
++ docs/DECISIONS.md	2026-08-01 06:54:28.380795704 +0000

### 3.11 发布接口补编号为任务 25(A40)

`REVIEW.md` 12.1 的任务表里 **18 是「Batch Outbox 与异常恢复」**(P3,依赖 17),
表里根本没有「发布 API」这一项。A28 做的六个发布端点因此一直没有号,
被口头叫作「任务 18」,和表里的 18 撞上了。

**这件事在 A35 之前只是口径混乱,A35 之后成了事实冲突** —— 18 真的被做完了。
于是同一个号在 `CLAUDE.md` 里同时指两件已完成的事,读的人无从分辨。

结论:**发布接口 = 任务 25,原任务 18 不动。**

挑新号而不是给 Batch Outbox 挪号,理由按分量排:

1. **`REVIEW.md` 开头那句「正文一字未改」是它能当验收口径的全部依据。**
   给 18 挪号就是改正文。补号写在 12.1 表末的标注行里,与已有的删除线、
   状态标记同属**标注层**。
2. 18 已完成并有迁移(`0025`)、门禁(`test_batch_lease.py`)、决策记录(§3.12)
   三处引用,挪号要同步改三处,每一处都可能漏。
3. 25 是表末 24 之后的下一个空号,不与任何既有任务冲突。

同步改到的地方:`REVIEW.md` 进度快照与 12.1 表末、`CLAUDE.md` 两处正文加
编号说明、`STATUS.md`「需要你决策」第一条勾掉。

**关于 §3.10 的空号:** 本文件从 3.9 直接跳到 3.11。3.10 是 a35 那条分支上
分配的号,那条决定没有随任何一个补丁进到这条线上。留空不补,**因为不知道
它是什么** —— 编一条填进来比留一个空号危险得多。

### 3.12 任务 18 的「Batch Outbox」不建表(A35)

任务表里 18 的标题是「Batch Outbox 与异常恢复」。**本轮没有新建 outbox 表**,
而是把同一个保证用既有结构实现。理由不是省事,是仓库里已经有一条相反方向的
决定,照标题字面去建表会和它撞上:

`dispatch_service.send_batch` 与 `workbench/batch.py` 都写明**批次刻意不写
`TaskDispatch`** —— 批次的条目本身就是意图记录,`batch_job_items` 里那批
PENDING 行在业务事务里落库,消息投不出去它们照样在。再叠一张表是把同一件事
记两遍,而两份记录迟早会不一致(而且不一致时没人知道该信哪一份)。

所以 outbox 的三个保证逐条对照下来,缺的**只有第三条**:

| Outbox 要保证的 | 批次靠什么 |
|---|---|
| 意图与业务数据同一个事务 | PENDING 条目本身,已有 |
| 意图不会丢 | 同上,已有 |
| **有人重新投递** | **本轮补:`redispatch_stalled_batches()`** |

「异常恢复」那一半同理,缺的是**回收**:worker 猝死后它领走的条目停在
RUNNING,而 `run_batch` 只捞 PENDING、`reset_items_for_retry` 只挑 FAILED,
两条路径都绕开它。本轮补 `reap_expired_leases()`。

三条附带决定:

- **重投的前提是任务 17,不是可选项。** 没有 `FOR UPDATE SKIP LOCKED` + 租约
  的话,「重投一次」等于「再跑一遍」,而那意味着重复的付费调用。任务表把 18
  排在 17 后面不是排版,是安全依赖。**先做 18 会得到一个花钱的 bug。**
- **自动重排有上限(`MAX_ITEM_ATTEMPTS = 3`),超过落 `WORKER_LOST`。**
  一件能打死 worker 的条目(超大图 OOM)会被无限回收,每一轮都可能是一次
  真金白银的调用,而没有人在看。有上限之后,同一个故障的表现从「预算悄悄
  见底」变成「界面上一条可解释的失败」。
- **`WORKER_LOST` 登记成 `retryable=True`。** 回收器放弃的是**自动**重排,
  不是这件事本身。写成不可重试的话它连重试按钮都拿不到,而这一类恰恰是
  重试就能好的。

### 3.13 租约到期判定里的 NULL 取「已过期」(A35)

`batch_job_items.lease_until` 可空,而可空列参与比较是这个仓库栽过的地方:
`publish_service.claim_due` 里 `next_attempt_at IS NULL` 必须显式放行,
否则 `NULL <= now` 求值为 NULL 而不是真,那一行**永远领不到**。

这一条是同一个坑的另一面,方向相反:

    publish outbox   漏掉 NULL -> 行永远领不到
    batch item       把 NULL 当「永不过期」-> 0025 之前卡在 RUNNING 的
                     存量残骸永远回收不了

两个方向都不报错,都表现为「有一行安静地不动」。判定口径写在
`batch.lease_expired()` 一处,SQL 侧(`claim_items` / `reap_expired_leases`)
两处都必须与它同向 —— 三处写岔的表现是「只看时说有 3 条,--apply 之后
回收了 5 条」,而这种不一致恰恰会在出事、有人盯着屏幕对数字的时候出现。

<!-- 合入说明:a42-review-fixes 与 a42-task19 各自新增了 §3.14/§3.15,编号撞车。
     保留 review-fixes 的 3.14–3.18(其中 §3.14 / §3.16 被 test_batch_lease_concurrency_db.py
     的 docstring 硬引用),task19 的两节顺延为 §3.19 / §3.20,引用点已同步改。 -->

### 3.14 领取批量收成 1,并搬进判定模块(A42)

`CLAIM_CHUNK` 从 10 改成 1,位置从 `batch_service.py` 搬到 `batch.py`,
租约不变量从「> 单件最长合法耗时」改成「> `CLAIM_CHUNK` × 单件最长合法耗时」。

**为什么改:那条防线在 A35～A41 期间从未生效。** 三个数放在一起就看得出来:

```text
LONGEST_LEGAL_ITEM_SECONDS   1080 秒 = 18 分钟   单件最长合法耗时
CLAIM_CHUNK                  10 件               一次领多少
ITEM_LEASE_SECONDS           1800 秒 = 30 分钟   整批共用的租约
                             ↑ 要盖住 10 × 1080 = 10800 秒 = 180 分钟
```

`claim_items` 算**一次** deadline 盖给全部 10 行,执行期间**从不续期**,
而 `run_batch` 是顺序跑的。所以第 3 件开跑时租约就到期了,第 3～10 件
全程在一个过期租约下执行。`reap_expired_leases` 每 60 秒全表扫一遍,
它不认识「owner 还活着」这件事(系统里没有活体名单,`lease_owner`
明写只用于排查、不参与判定),于是它会从一个**正在正常工作**的 worker
手里把条目抢走放回 PENDING。后果:重叠执行(= 重复付费调用)、
`attempts` 虚增、`CONCURRENT_IN_FLIGHT` 假失败、`_apply_outcome` 无条件
覆写 status 造成的状态互踩、提前触发 `WORKER_LOST`、批次短暂错误结算。

原注释写的是「10 件按单件最长合法耗时算是半小时的活,与 `ITEM_LEASE_SECONDS`
同量级 —— 再大就会出现『最后一件还没开跑,第一件的租约先到期了』」。
**那句话算错了 6 倍,而它描述的失效在 10 这个值上已经发生了。**

**为什么是 1 而不是续租。** 续租(每件开跑前把 `lease_until` 往后推)
是更好的终局,但它是**第三种设计**:现有的两版任务 17 实现都没有它,
两边的测试也都没覆盖过它。首期取 1,租约与执行一一对应,不变量退化成
原来那条单件断言,不需要新机制。代价是每件多一次 `SELECT ... FOR UPDATE`,
和一次付费出图调用比可以忽略。

**为什么常量必须搬家。** 它参与不变量,而 assert 在判定模块里。
常量留在服务层的话 assert 看不见它,只能退化成单件断言 ——
**而单件断言正是这个缺陷活下来的机制**,它在整个期间都是绿的。
服务层保留 `CLAIM_CHUNK = rules.CLAIM_CHUNK` 一行转引,
`tests/pure/test_batch_lease.py` 钉着这一行不许变回字面量。

**这条改动不消除超时窗口,只是把它从必然降成偶然。** 单件真的跑超
30 分钟时,`reap_expired_leases` 仍会从活着的 worker 手里抢走它,
而 `_apply_outcome` 仍然不校验归属。真库双 session 用例必须显式覆盖
「租约过期但 worker 还活着」,不能只覆盖「worker 真的死了」。

### 3.15 `is_simulator` 问实现类,不查名单,默认 True(A42)

`core/environment.build_facet()` 读三列,而 `providers` / `evaluators`
两个注册表从来没报过 `is_simulator`。缺列不报错:`row.get()` 给 `None`,
`bool(None)` 是 False,而 Mock 的 `is_configured()` 恒为 True ——
判定一路走到 REAL。默认 Mock 环境下的实际输出是:

```text
出图 mock     REAL          ← 状态条对运营说「真的在调外部出图服务,会产生费用」
评分 mock     REAL          ← 「真的在调视觉大模型评分」
属性 mock     SIMULATED     ← 只有这一档对,因为 extractors 报了这一列
渠道 generic  UNAVAILABLE   ← 「还没实现,只有骨架」,而它正在用 Simulator 正常工作
```

渠道那一档是另一个原因:`describe_channels()` 的行没有 `active` 键
(它不是一张「选一个」的表),调用方又传了 `None` 把 `pick_active` 的
第二条线索掐掉,于是只能返回 None,而 `build_facet` 把 None 判成 UNAVAILABLE。
**`pick_active` 的两条线索必须至少留一条能用。**

三个决定:

- **问实现类自己,不查注册表里的名单。** 名单式写法在
  `describe_extractors` 里,而它的注释自己承认「接真后端时这一行必须改成
  问它自己」。名单和实现类会分开演化,分开之后没有任何东西会报错。
- **基类默认 `True`(先假设是假的)。** 两个方向的错误代价不对称:
  漏标一个真 Provider → 多喊一次「这是模拟环境」,一眼看得见,当场就会来改;
  漏标一个 Mock → 安静地说「会产生费用」,运营照着假图批准上架。
  默认值必须站在吵闹的那一边。这与 `core/environment._LOUDNESS` 把
  SIMULATED 排在 UNCONFIGURED 前面是同一条理由。
- **接缝要有自己的用例。** 这个洞能活四轮,是因为两侧各自都有测试:
  判定被 8 种组合穷举钉死,注册表也有形状测试,**没有一条跨过中间那道缝**。
  `tests/pure/test_environment.py` 新增「注册表接缝」一节,真的调注册表、
  真的跑判定,验收口径是「默认 Mock 环境四档全部 SIMULATED」。

### 3.16 租约的真库用例把一个**已知缺口**也钉了进去(A42)

`tests/test_batch_lease_concurrency_db.py` 里有一条
`test_the_reaper_does_not_ask_whether_the_owner_is_still_alive`,
它断言的是**当前行为**,而当前行为是有缺口的:

    reap_expired_leases   只看 lease_until,不问 owner 死活
    _apply_outcome        不校验本 worker 是否仍持有租约就覆写 status

于是"租约过期但 worker 还活着"时,回收器会从活人手里抢走条目,
而原 worker 跑完仍会把结果盖上去。A42 把 `CLAIM_CHUNK` 收成 1 之后,
这件事从**必然**(第 3 件起就发生)降成**偶然**(单件真跑超 30 分钟),
但没有消失。

**为什么把缺口写成一条会绿的用例,而不是留一行 TODO。** TODO 不会在
行为变化时告诉任何人。这条用例的 docstring 明写:它变红意味着有人补上了
续租或归属校验,**那时该改的是断言,不是删掉用例**。缺口于是有了一个
带地址的落点 —— 而不是散在三份文档里的一句话。

同样的道理,那条用例给第二个 session 装了 `lock_timeout = '3s'`:
没有它,`skip_locked` 被删掉时用例会**挂住**,CI 报出来的是"作业超时"
而不是"锁语义错了"。**一条只会挂起的守卫等于没有守卫** ——
出事时没人能从超时日志里读出原因。

### 3.17 第一次真库全绿，在路上撞出两个生产缺陷(A42)

装了 PostgreSQL 16 + Redis 才跑起来的 1652 条，一路碰到两个**真实生产 bug**：

**（1）`POST /api/reviews/{id}/approve|reject|regenerate`**
人工审核的三个动作全部无条件 500。根因是  `_basic_review_out` 在「报告 6.5」那轮
被改成批量形态 `(item, *, products, task_statuses)`，调用方只改了列表页，
审核写接口的三处调用还在用老签名 `_basic_review_out(session, item)`。

修法是抽 `_single_review_out(session, item)`，让写接口和列表页都经过它。
这样做不是为了节省三行代码，是为了**让下一次签名变化时编译期报错，
而不是运行期 500**。

**（2）`celery_eager` 夹具把 `commit` 换成 `flush`**
被测代码到处是真的 `session.commit()` 与 `session.rollback()`，它们有语义：
`_claim()` 抢不到时 `rollback()` 丢掉这一次的 UPDATE 尝试。不开 savepoint 时，
那个 `rollback()` 会一路回到**用例开头**，于是派发意图全没了，第二轮派发
看到的还是同一条 PENDING，同一个任务被投两次。第二次必然抢不到、必然
rollback、必然全清。eager 模式下 `deliver_pending` 嵌套到深度 2。

修法是 `session` 夹具改用 `join_transaction_mode="create_savepoint"`：
应用代码的事务语义因此是真的，不是被夹具改写过的。

这两条缺陷一直活着，是因为唯一会碰到它们的集成测试整个模块 skip——
没库时"跳过"和"通过"在汇总行里长得一模一样。这是为什么
`make test-pure` 绿不代表"接近就绪"的一个例证。

### 3.18 「生产缺陷」的定义边界(A42)

上面那两条都是真的 bug，会在生产环境造成可观测的影响：
审核接口 500；派发重复(如果生产也跑 eager 或有类似 eager 的夹具)。

但评讲的时候要有信心：这两条缺陷的存在不是"测试没做好"或"代码没写好"，
而是"测试路径从未被用过"——审核那一块因为离线测试只能 skip，
派发那一块因为一路过的都是 mock 单元夹具。

所以它们活这么久，是结构决定的，不是谁的疏忽。同样地，下一个从未被跑过的
路径暴露的 bug，也不应该冲着任何一个人来；应该改的是**让路径可以定期跑起来**。
A42 为止，真库集成已经是绿的了。下一步就是让 CI 也用真库。
### 3.19 请求的事务边界归接口所有,批次那条长事务是署名例外(A42)

任务 19 的后半。§7.8 的第一条禁止项是「请求级自动 commit 与 Service commit
混用」,而 `db/session.py::get_session()` 一直在 `yield` 之后无条件提交。

**害处不在多提交一次**(那无害),在于事务边界没有主人:

    接口层写着 commit          38 处,发布链路还刻意分三段提交
    依赖层又替所有人 commit    包括 13 个一行 commit 都没写的写端点

读路径受的伤更实:自动提交意味着**任何一处误写都会落库**,哪怕那个端点
从头到尾没打算写。评审第 19 条把五个 GET 改成只读、a38 又给 `collect()`
加了形状门禁盯着写必须挡在 `dry_run` 后面 —— 而这一行在下面兜着,
让那些努力只差一次手滑就白做。

**处置:** 摘掉那一行(回滚保留),13 个端点里 12 个各自补上显式 commit,
`preview_import` 进白名单 —— 它是只读的预览 POST,用写方法只因为要收文件体。

摘之前逐个端点走过调用图,确认**没有任何 GET 在依赖它**:可达的会话写
只有两类,一是全部挡在 `dry_run` 后面的 `refresh_draft`,二是
`download_batch_file` 那条只增不改的审计流水(它自己就 commit)。

#### 为什么这件事只能靠门禁

`tests/conftest.py` 的 `client` 夹具把 `db_session` 覆盖成一个**不提交**的
session(整个用例一个事务,结束回滚)。同一个 session 里读得到未提交的写,
于是「真的提交了」和「只是 flush 了」在 API 测试里**完全等价** ——
那 13 个端点漏不漏提交,现有测试一条都答不上来。

这不是夹具写错了:用例之间要隔离就只能这么写。代价是提交这件事落在测试的
射程之外。**守它的是 `tests/pure/test_transaction_boundaries.py` 的
「HTTP 边界」一节,不是集成测试。** 那五条都做过变异验证。

#### 批次执行的长事务:是例外,不是漏网

§7.8 还禁止「一个数据库事务跨越长时间外部调用」。`batch_service._execute`
确实跨了 —— 它在事务里调付费模型。**这一条不改,因为它是防重复付费的前提:**

`try_advisory_xact_lock` 是**事务级**锁,而它必须活到「回执对别人可见」
那一刻,也就是提交。提前 commit 会把锁放掉,第二个请求拿到锁、查不到回执、
照样调一次模型 —— 竞态原样回来(理由写在 `db/locks.py` 那个函数里)。

发布链路能做三段事务,是因为它的幂等靠**库里的唯一键**,不靠锁;
批次靠的是锁 + 回执。两条链路的幂等机制不同,事务形状因此不同,
这是取舍不是不一致。边界由每件一个事务兜着(`run_batch` 的 `commit_each`),
所以长事务的跨度是**一件**,不是一批。

### 3.20 系统范围是「服装」,泳装是目前唯一已校准的品类(A42)

文档口径统一为**服装**系统。品类在代码里本来就是参数:
`field_spec(category_id=...)` 读 `spec/{category_id}.yaml`,属性注册表按品类校准。
`CATEGORY_ID = "swimwear"` 是默认值,不是硬编码的边界。

**三类「泳装 / swimwear」字样不动**,因为它们不是旧称,是在如实描述:

| 不动的 | 为什么 |
|---|---|
| `swimwear-imagegen` 这个名字 | 它是标识符,接在 `tools/pack.sh` 的产物名、`ci.yml` 的镜像名、`.env.example` 的 `APP_NAME` 上。改名是一次重命名,不是文档改动,要单独排期并同步改门禁 |
| `spec/swimwear.yaml`、样例数据、属性注册表的校准 | 这些描述的**就是**泳装那个品类。改掉它们等于让文档与数据对不上 |
| `evaluators/vision_schema.py` 的评分提示词 | **最不能顺手改的一处。** 提示词是按当前分档阈值校准过的,改一个字都可能整体平移分数分布,而阈值不动就是大批误判(`docs/VISION-EVALUATOR.md` 第八节)。换品类要连着重新校准,不是改文案 |

`docs/REVIEW.md` 的正文同样不动 —— 那份仓内副本开头写着「正文一字未改」,
它是这份文档能当验收口径的全部依据。范围口径写在它的**标注层**,
与 §3.11 的补号同一个位置。要引用范围口径,以 `CLAUDE.md` 开头与 `README.md` 为准。

---

## §3.15 A43:执行归属由令牌决定,不由时钟决定(BLOCK-02)

批次条目落库的条件更新是四条件:

```sql
WHERE id = :id AND status = 'RUNNING'
  AND lease_owner = :owner AND lease_token = :token
```

**刻意没有第五条 `AND lease_until > now`。** 复查报告建议的写法里也没有,
这里把理由钉下来,免得下一个人"顺手补全":

真正的交接发生在回收器把 `lease_token` 吊销的那一刻,不是时钟走过某一秒
的那一刻。把到期时刻写进归属判定,后果是一件**跑超了但还没被抢走**的条目
连自己的结果都落不了库 —— 钱已经花掉,结果却被自己丢掉,而且库里看不出
这件事发生过。

于是分工是:

    时间    决定**谁可以来抢**(`lease_expired` / `reap_expired_leases`)
    令牌    决定**谁能写**(`lease_still_held` / `apply_outcome`)

这也是 fencing token 与单纯超时租约的全部区别。两件事的取向不同:
抢活宁可早(漏回收没有兜底),写入宁可晚(多写一次要花钱)。

**这不能防止重叠执行。** A 跑超时、B 接管,两次付费调用都真的发生了。
令牌防的是**状态互踩**,不是重复花钱;后者由租约时长与续租控制,
两件事不要混在一起看。


## §3.16 A43:属性读取的回落链是临时的(BLOCK-03)

`effective_map()` 同时读四个位置:

```text
(SPU,     product.spu)          新
(VARIANT, variant_id_for())     新
(SKU,     product.sku)          新
(SPU,     product.id)           legacy —— A43 之前所有值都写在这里
```

最后一行是存量兼容。直接切口径会让已确认的属性在界面上凭空消失,
那比不分层更糟。

**删除条件**:属性表完成一次数据迁移(把 `(SPU, product.id)` 上的行按注册表
改写到正确的 owner)。删的时候要连同这一条一起删,并把 `effective_map()`
的 `product` 参数改成必填。

回落链里有一条约束容易被误删:**一个字段只认它在注册表里声明的那一层**。
少了它,存量 `(SPU, product.id)` 里那份 `primary_color` 会盖掉新写入的
变体级值 —— 回落链会变成覆盖链。


## §3.17 A43 引入的已知遗留:变体分层挂在一个不稳定的 ID 上

`variant_id_for()` 今天仍是 `primary_color or sku`。A43 把属性的 `owner_id`
接到了它上面,于是**运营改一次颜色文案,已确认的颜色属性就会"消失"**
(旧值留在旧 owner_id 上,新读取按新 id 查不到)。

改之前:这个不稳定 ID 只影响图片标签(变成 `unknown_tags`,有诊断能看见)。
改之后:它还影响属性归属,而属性那一侧**没有等价的诊断**。

**下一轮必须先解决稳定 variant ID,再动其他多颜色功能。** 图片绑定和属性
分层用的是同一个函数,只改一侧会让两边对不齐,而那种不一致两边都不报错。

> **A44 已关闭这一条,见 §3.18。** 本节保留原文不改 —— 它记录的是当时
> 的判断和理由,改写它等于抹掉「这个问题是怎么被发现的」。
> 另外:动手修它的过程中发现了一个更严重的洞,见 §3.19。


## §3.18 A44:变体的身份与名字分开(BLOCK-04 第一件)

§3.17 记的那条遗留在本轮关闭。做法只有一句话:

    variant_key    身份。创建时分配一次,**此后不由任何字段推导**
    primary_color  名字。运营随便改,改名不动身份

`variant_id_for()` 现在读 `products.variant_key`(0027),不再是
`primary_color or sku`。于是图片标签、属性 owner_id、导出变体列
三者在改名之后继续指着同一个东西。

### 回填取值是旧表达式,不是 UUID

这是 0027 唯一需要想清楚的地方。给 UUID 的话,那一句 UPDATE 跑完的瞬间,
库里所有图片标签(自由文本,写的是颜色名)和所有 `(VARIANT, 颜色名)` 上的
属性值全部指向不存在的 key —— 一次迁移把 §3.17 那个 bug 在**每一个** SPU
上同时引爆一遍。

回填写成 `COALESCE(NULLIF(BTRIM(primary_color), ''), sku)`,也就是旧表达式
本身。升级完成那一刻所有 id 与升级前逐字节相同,存量数据一条不用动。

代价是 key 看起来像颜色名,会有人想去解析它、或者直接显示它。
对策是 `label_of()` 与响应里的 `labels` 字段:**要显示的字符串有一个
正确的来源**。直接把 key 打到界面上的地方,在第一次改名之后就会显示旧名字。

### 新 SKU 归到哪个变体:先找同色兄弟,再用种子

`mint_key()` 第一步是「同 SPU 下已有同色的变体 → 复用它的 key」。
少了这一步,红色 S 和红色 M 会拿到两个 key,而「红色缺图」这件事
就永远检查不出来。空颜色**不复用**任何 key:空是「还不知道」,
不是一种颜色,把两个未知并成一个变体等于宣称它们同色。

### 改名换来的两种新异常

身份不再跟着名字走,于是出现了 A43 不可能有的状态,由 `drift()` 报告:

    renamed             key 与当前颜色对不上 —— 正常,只是说明改过名
    label_collisions    两个变体的当前颜色一样了。**这是真问题**:
                        界面上同名,绑图分不出给谁,`resolve_ref()` 会拒绝翻译。
                        成因通常是把「正红」和「大红」都改成「红色」——
                        那是一次合并,应当显式做,不该由改名顺手完成
    unassigned          还没分到 key 的行。它们仍走种子表达式,即仍会被改名带偏


## §3.19 A44:VARIANT 属性的 owner_id 必须带 SPU 命名空间

改 §3.17 的过程中发现的,比它更严重。

`product_attribute_values` 的唯一索引是
`(owner_type, owner_id, field_name) WHERE is_current` —— **里面没有 SPU**。
A43 把 VARIANT 层的 owner_id 直接写成变体 id(取值是颜色名),于是:

> 全库只要有两个 SPU 都有「黑色」,它们就共用同一行属性。
> 给 SPU-SW-001 的黑色确认一次 `primary_color`,SPU-SW-002 的黑色跟着变,
> 没有任何提示。

比「属性不见了」更糟:值还在,只是属于别人。而它在单 SPU 的测试里永远
复现不出来 —— 要两个 SPU 同色才会出现,而样例数据每个 SPU 一个颜色。

owner_id 现在是 `<len>:<spu>/<variant_id>`,列宽 64 → 160。

### 为什么带长度前缀

直接拼 `spu + "/" + variant_id` 有一个真实的歧义:

    spu="A"    variant="B/C"   ->  "A/B/C"
    spu="A/B"  variant="C"     ->  "A/B/C"

SPU 是自由文本(`schemas/product.py` 只校验长度),含斜杠不违法。
两个不同的变体拼出同一个 owner_id,后果和上面那条一模一样,而且更难查。
长度前缀让这种歧义在结构上不可能存在,代价是 2~4 个字符。

### 存量行只改写「能唯一定位到一个 SPU」的那些

0027 的改写带 `HAVING COUNT(DISTINCT spu) = 1`。一个裸 owner_id 对应多个
SPU 时,它本来就是被共用的那一行 —— 归给谁都是猜,而猜错等于把别人的颜色
写进这个 SPU。这类行原样留着,由 `orphaned_variant_owners()` 的
`legacy_bare` 报出来,人工决定。

**不提供 `--apply`。** 重指归属要靠人判断「这个黑色是哪个 SPU 的黑色」,
而本轮拿不到真库,一个能改数据的脚本不该在这种状态下交出去。

---

## §3.21 A45:受众是一根独立的轴,不是品类的子分类

PRD v2(男女泳衣版)的全部改造围绕一句话:**受众与品类正交**。
`core/audience.py` 与 `workbench/audience_rules.py` 的模块注释指向本节。

### 为什么不能把男装做成 `category="men_swimwear"`

这是最省事的做法,也是错的。受众会在六处被用到:模特筛选、提示词选择、
槽位表、检查项、必填属性、平台字段。做成品类值之后,这六处每一处都要
自己从品类码里抠出受众 —— 而 `unisex_` 与 `women_` 的前缀匹配迟早有人写错,
**且错的那一处不报错**:它只是安静地用了另一个受众的规则。

所以派生方向是单向的:`audience + garment_family -> category_code`,
派生函数只有一个(`core/audience.category_code_for`)。反向抠受众的函数
(`embedded_audience_of`)存在,但它的唯一用途是**校验一致性**,
不是当作受众的来源。两者不一致时以 audience 为准**并报错** ——
静默取其一的两个方向各有各的坏处:取 audience 会让草稿指纹和 spec 文件名
对不上,取前缀会让「AI 不得静默改写受众」变成空话。

### 为什么 `Audience` 里没有 UNCONFIRMED

UNISEX 是一个明确的业务判断(两种受众的模特都能穿),"没填"是另一回事。
给"没填"一个枚举值,它就会被当成合法受众流进模特筛选和规则包派生。
未确认的状态由 `products.audience IS NULL` 表达 —— 数据库层面就区分得开,
不需要在应用层记住"UNCONFIRMED 要特殊对待"。

`ModelTemplate.audience` 反过来是 NOT NULL,而且不允许 UNISEX:
模特本人总有受众,入库的人当场就知道;可空只会让硬过滤多一个
"没填怎么算"的分支,而那个分支没有正确答案。

### 0030 的回填取向:两张表相反,各有理由

| 列 | 回填 | 理由 |
|---|---|---|
| `products.audience` | **不回填**(NULL) | 按"库里只有泳装所以都是女装"回填,正是 PRD §9.1 明令禁止的「按类目猜一个默认值填进去」。存量商品进"待确认受众",由运营逐件确认 |
| `model_templates.audience` | WOMEN | 存量模板全部来自女装校准期(a44 之前唯一开放的组合),这是**记录事实**不是猜测。且错误方向安全:被标成 WOMEN 的男性模特最坏是"少一个候选",不是"错配生成" |
| 授权字段组 | UNVERIFIED / false / 空 | 标成 LICENSED、`age_verified=true` 等于替不存在的授权文件签字。UNVERIFIED **不阻断**新任务(否则存量全停摆),只进管理员待补清单;EXPIRED / REVOKED / 过期才硬阻断 |

### 为什么两张表的列放在同一次迁移里

它们服务同一条规则(受众硬约束需要两边都有受众才判得了)。拆成两次会
出现"商品有受众、模特还没有"的中间版本,那个版本里硬约束恒放行 ——
**半上线的保护比没上线更糟,因为它看起来在**。

### 提示词为什么是两份而不是一份加分支

女装那份是**按当前分档阈值校准过的**。在里面加一段"如果是男装则检查腰头
和裤长",会同时改变女装图片的评分分布:提示词长度、检查项数量、注意力分配
都变了,而阈值不动。后果是一批本来 B 档的女装图变成 C 档,**而且两侧测试
都是绿的**——没有任何断言在看"女装的分档分布有没有平移"。

所以男装是另一份提示词常量、另一个提示词键、将来另一次校准。
交付时用脚本核对过:`DEFAULT_SYSTEM_PROMPT` 相对 a44 基线**字节级不变**
(1343 字符,前后一致)。`tests/pure/test_audience_rules.py` 守的是结构层面
(两份必须是独立字符串、女装那份里不许出现男装的检查项词);
字节级门禁需要真实历史样本重跑,属于阶段 4。

### UNISEX 商品目前走女装提示词 —— 这是显式决定,不是遗漏

中性款(防晒衣、水母衣)的检查项按 PRD §14.3 是两组的交集,而交集里的
结构项目前只有女装那份覆盖得到。等中性款攒够样本单独校准时,
`prompt_key_for()` 里加一个分支 —— **而不是往任何一份提示词里加条件语句**。

### 已知缺口:§17 第 2 条未接线

PRD §17 要求提交前重新验证三条。第 1、3 条(商品受众有值、草稿规则包受众
与商品一致)已接进 `export_gate`,单件与批量共用。

**第 2 条(图片使用的模特受众与商品受众一致)只有判定函数,没有接线。**
判定逻辑在 `workbench/audience_rules.model_audience_problems`,有穷举测试;
缺的是数据:`media_assets` 没有生成溯源列(只有迁移期的
`legacy_kind` / `legacy_id`),查不到某张已批准图出自哪个模特模板。

补法是下一次迁移给 `media_assets` 加 `generation_task_id`
(`generation_tasks.model_template_id` 已经有了,一跳可达)。
在那之前,PRD §12.4 的**生成前硬阻断**保证错配的图根本产生不出来 ——
第 2 条是第三道网,不是唯一一道。但它确实是一道没合拢的网,
记在这里而不是让它隐身。

### 受众有两条写入路径,两条都要收口

`products` 是 SKU 级表,受众却挂在 SPU 上。只在一条路径上校验是不够的:

| 路径 | 措施 |
|---|---|
| 导入(CSV / JSON 两个入口) | 同 SPU 各行受众必须一致,**留空也算一种取值**;不一致当场报行号并指出与哪一行冲突 |
| 编辑接口 PATCH | 改受众时**整个 SPU 一起改写**,传播条数记进审计 |

不做这两条的后果不是"数据有点乱":草稿是 SPU 级的,同 SPU 两行受众不同时,
草稿的规则包取决于**查到哪一行** —— 运营从红色 SKU 进去看到男装草稿、
从蓝色 SKU 进去看到女装草稿,两边写的是同一行草稿。这个不确定性最终会被
§17 第 3 条在导出闸口拦下,但那时错误指向草稿,而根因在几天前那次导入。

编辑接口选择「整体改写」而不是「拒绝并要求逐行改」:后者把一个本该原子的
操作交给运营手工保持一致,而中途关掉页面就留下一个半改的 SPU。

### 启用码不能只写在 spec 里 —— 它必须走到请求体

`enabled_hard_fail_codes` 的第一版只是一个**声明**:两份 spec 写对了、
读 YAML 的测试全绿,而**没有任何代码读它**。男装的评分请求体里照样摆着
`STRAP_CHANGED`。

这一条值得单独记下来,因为它的失效方式很隐蔽:scoring 层只丢弃**枚举外**
的码,而 STRAP_CHANGED 是合法枚举值 —— 它会一路落库、进修复动作、
触发"换 seed 重出",没有任何一层会因为"男装不该有肩带问题"把它挡回去。

接线走 `rule_set` 字典,与 `PROMPT_KEY` / `DEPTH_KEY` 同一条通道
(`AUDIENCE_KEY` / `ENABLED_CODES_KEY`):`evaluate()` 的签名是所有评分器
共用的公开接口,不该为受众改动它;而且评分器跑在 worker 里,必须能被
无库测试,不能自己连库去查商品受众。

降级语义与提示词一致:取不到就退回受众前时代的行为(全集码 + 女装那份
**已校准**的提示词)。注意兜底方向 —— 受众未知时落到女装那份,不是男装
那份未校准的。

`tests/pure/test_audience_rules.py` 第 12 组守的正是"声明与接线之间的缝":
它构造真实请求体断言码枚举,而不是读 YAML。这一组做过变异验证 ——
把 `build_response_schema(depth, allowed_hard_fail_codes)` 的第二个参数拿掉,
它会变红。

### 中性款规则包是一次复核的产物

UNISEX 一直是合法受众:枚举里有、导入模板里有、前端下拉里有、
`model_matches_product` 也专门为它写了分支。但它派生出的
`unisex_swimwear.yaml` **此前不存在** —— 一件中性款商品会在
`_current_draft_data` 抛 `ChannelSpecError`,把整个工作台列表打挂。

没被发现是因为测试只断言了**派生出的字符串**(`category_code_for(UNISEX,
"swimwear") == "unisex_swimwear"`),从没真正加载过那个 category_id。
现在有 `test_every_audience_value_resolves_to_a_loadable_rule_pack`
逐个受众取值加载一次 —— 加一个受众而不加规则包,那个受众的商品会整片打不开。

中性款的字段集按注册表的 `applies_to={MEN, UNISEX}` 取(§18.4),
**但必填只有三条通用字段** —— 注册表里 waistband / inseam / fit 的
`required_for` 是 `{MEN}`。两边不一致会造成一个死锁:属性页按注册表出
必填清单,运营照着填完、流程显示完成,然后导出闸口说缺字段,
而界面上没有任何地方能补它。`test_spec_required_fields_never_exceed_what_the_registry_requires`
守这一条。

### 授权字段:哪些执行、哪些只记录

存了 12 个字段,第一版只有 2 个被读。存下来却从不检查,
等于把一份合规记录做成装饰。现在的分界:

| 字段 | 处置 |
|---|---|
| `license_status` = EXPIRED / REVOKED | **阻断新任务** |
| `license_expires_at` 已过 | **阻断新任务**(状态没人改也拦得住) |
| `allow_ai_dressing` = false | **阻断**,但仅当 status == LICENSED |
| `prohibited_categories` 命中规则包键 | **阻断**,但仅当 status == LICENSED |
| `allowed_platforms` / `allowed_regions` | 记录。执行点应在发布环节(渠道 + 站点),本期未做 |
| `commercial_scope` / `source` / `allow_derivative_images` | 记录,供人工核对 |
| `age_verified` | 记录 + 界面标记。**不阻断** —— 见下 |

后两条"仅当 LICENSED"不是漏判:默认 `allow_ai_dressing=False` 的含义是
"没人核实过",不是"授权禁止"。对 UNVERIFIED 执行它会让存量模板全部停摆,
与迁移 0030 记的取向直接矛盾。一旦有人把状态改成 LICENSED,
就意味着他核对过条款,那份条款的限制从那一刻起是权威的。

`age_verified` 默认 false 同理 —— 它是"没人核实过"。不阻断而是在模特卡片上
打标记,因为存量模板全部未核实,阻断等于停摆;但泳衣 + AI 生成人像是所有
服饰品类里这一条最敏感的组合,所以它必须**可见**,不能只躺在库里。

判定逻辑拆在 `app/services/model_license.py`:纯逻辑、零 ORM 依赖,
与 `core/audience.py`、`workbench/audience_rules.py` 同一个惯例 ——
规则不依赖 SQLAlchemy 才测得动。

### 跨语言的"下发即消费"

后端 `_product_out` 下发 `audience` / `audience_label` / `review_focus`,
而前端一处都没读 —— 这是"声明但没接线"在跨语言边界上的版本,
比后端内部那两处更难发现:**两边各自的测试都是绿的**。

第 15 组测试同时从两侧断言:后端必须产出这三个键,前端类型里必须有它们,
并且必须有组件真的读 `product.review_focus`。只加类型不算接线 ——
类型是给编译器看的,运营看的是界面。

### 批量导出新增一条不变量

批次内商品分属不同规则包时**拒绝合并导出**,不自动拆成多 sheet。
理由与"模板混版拒绝合并"同一条:男装与女装的导出列头不同(领型 vs 腰头),
合并进一个工作簿等于半份列头对不上值。自动拆 sheet 会让
"一个批次一个文件"的对账口径悄悄变掉。

---

## §3.22 A45-batch13:身份从字符串约定改成外键(阶段 1 第一批)

PRD v3.1 §4.2~4.4。新增 `spus` / `color_variants` 两张表,`products` 增六列
(`spu_id` / `color_variant_id` / `barcode` / `price` / `cost` / `inventory`),
迁移 `0035`。

### 它解决的是 §3.17 / §3.19 那两条的**根因**

§3.17(变体身份挂在一个不稳定的 ID 上)与 §3.19(owner_id 必须带 SPU 命名空间)
是同一个原因的两个症状:**没有一个地方可以挂"这个款/这个颜色自己的东西"**。
于是受众只能复制到每一行 SKU(所以要有一致性检查),颜色事实的 owner_id 只能
靠 `<len>:<spu>/<variant>` 拼出来(所以要有一个解析器),而"这个颜色停售了"
根本无处存放。

有了 `color_variants.id` 之后,§3.19 那个长度前缀**没有工作可做了** ——
它当初存在的唯一理由是"那张表的唯一索引里没有 SPU,得让 VARIANT 的 owner_id
自己全局唯一",而一个 UUID 主键天生全局唯一。切换在阶段 1 第二批做,
**前提是 products 先带上 `color_variant_id`**:owner_id 要用变体 UUID,
就得先有地方拿到它。顺序反不过来,这也是本批次的边界为什么划在这里。

### 为什么这一版外键可空

§3.1 允许一次性切换(系统尚未正式使用),但"允许"的是**数据**层面。代码层面
老建档路径(`product_service.create_product`、CSV 导入)还不知道这两张表,
把外键立刻设成 NOT NULL 会让它们在 flush 阶段整体失败 —— 而整改环境没有
PostgreSQL,一个改不动就没法验的改动不该一次做完。

代价是"新路径一定写外键"这件事没有数据库替我们保证,所以它由守卫顶着
(`test_the_archiving_path_sets_both_foreign_keys_on_every_row`)。
收紧成 NOT NULL 的前提写在 `docs/STATUS.md` 阶段 1 那一节。

### SKU 编码:后两段禁用分隔符,第一段允许

`SPU-SW-001-BLK-S` 里第一段自己就带横线(样例数据与供应商单据都这么印)。
禁掉全部分隔符等于要求运营改掉一个印在样品袋上的编码;全都允许则回到 §3.19
那个歧义(两个不同的三元组拼出同一个字符串)。

取中间:**颜色与尺码禁用,SPU 段允许**。于是从右边数,最后两段一定是尺码和
颜色,剩下的全是 SPU —— 切法唯一,两个不同的三元组拼不出同一个 SKU。
和 §3.19 的长度前缀是同一个不变式的两种实现,区别在于这里能收输入,
所以不用带解析器。`test_two_different_triples_can_never_collide` 钉的正是
这条不变式本身,而不是某个函数的返回值 —— 它是允许第一段带分隔符的全部理由。

### 均码是模板 `ONE_SIZE`,不是"尺码留空"

`UNIQUE(color_variant_id, size)` 在 PostgreSQL 下对 NULL 不生效(NULL 彼此
不相等),于是尺码留空的行可以在同一个颜色下重复任意多次,而唯一约束一声不吭。
给它一个真实取值 `"OS"`,那条约束才管得住它。

## §3.23 A45-batch13-2:变体身份收成三级,以及回填不是一次数据迁移

> **历史口径提示（2026-08-08）:** 本节记录的是 0046 之前的过渡态。
> 0046 已完成身份改写；运行时身份现为 `color_variant_id`，`variant_key` 只为
> downgrade 保留，不再是第二级，也不再产生 `identity_shadowed`。当前口径见
> §3.44 与 §3.45。

对 batch13(§3.22)的走读修复。五条,F1 是唯一一条**已经随交付包发出去**的。

### F1:三颜色九 SKU 的 SPU,在读取侧是九个颜色

§13 阶段 1 的第一条验收在新表里成立,在读取侧不成立。三个各自正确的决定
叠在一起造成的:

    建档路径写 `color_variant_id`      batch13 的交付项
    建档路径**不写** `variant_key`     退役方向,守卫明令禁止
    建档接口上**没有**视觉属性         阶段 1 验收项,`primary_color` 因此为空

于是 `variant_id_for()` 的两级回落全部落空,掉到种子表达式
`primary_color or sku`,而 sku 全库唯一。属性层把颜色事实按 SKU 分桶
(S 码确认的主色 M 码读不到)、图片集要求九个变体各有一张图、导出铺九行 ——
**三处都不报错**。

值得单独记一句:那条禁止读 `variant_key` 的守卫本身是对的,它守的是退役方向。
问题是**没有任何东西测量它打开的缺口**。「本批次只保证新代码不再长出对
variant_key 的依赖」这句话字面为真,略掉的是"于是那些行现在没有身份了"。

**修法是加一级,不是在建档路径上补 `assign_variant_key`** —— 后者是往退役的
方向再加一个依赖。身份现在是三级,唯一定义在 `variant_key.identity_of()`:

    1. `products.color_variant_id`   外键。新建档路径写的就是它
    2. `products.variant_key`        0027 分配的稳定 key,退役中
    3. 种子表达式                    会被改名带偏,`drift().unassigned` 点名

顺带收掉一处重复:"key 或种子"这句话原来在 `ids_of` / `refs_of` /
`variants.variant_id_for` 里各写了一份。两级时三份恰好等价,所以一直没人发现;
加进外键这一级就会分叉,而分叉正是 §3.17 那个"导出铺三行、属性写一个变体"。

### 外键排在 key 前面,以及它埋着的雷

退役方向决定了顺序只能这样。代价是:**同时有外键和 key 的行,身份会从 key
翻成外键**。今天不存在这种行(新路径写外键不写 key、老路径写 key 不写外键,
两个集合不相交),但老建档路径切过来、或者给存量行回填 `color_variant_id`
的那一刻,库里每一行都会同时有两个。

**那一刻不是一次数据迁移,是一次身份变更。** 已确认的颜色属性挂在
`<len>:<spu>/<key>` 上,图片标签写的也是 key;身份一翻,两者同时指向不存在
的变体 —— 这正是 §3.17 那个 bug 在全库同时引爆一遍,和当初否掉"key 用 UUID"
的理由一模一样。

所以回填必须和属性 owner_id、图片标签的改写**在同一个动作里**做。在那之前,
`drift()` 的 `identity_shadowed` 负责让这种行不隐身:这一级打开的缺口由它
测量,而不是靠"应该不会有人这么干"。这是 F1 的教训直接换来的一条。

### 顺序问题:§3.22 的前提说的是"行带上值",不是"列存在"

`HANDOVER.md` 把下一阶段的顺序记成「owner_id 切 UUID → 老建档路径切到新表 →
两个外键收 NOT NULL → variant_key 退役」。**第一步和第二步的依赖是反的。**

`owner_for()` 要用 `color_variants.id`,而今天只有新建档路径写这个外键。先切
owner_id,老路径建的每一行商品都会在 `owner_for()` 那里拿到空坐标 —— 而
`apply_evidence` 捕获 `AttributeValueError` 之后是 `logger.warning + continue`,
**识别侧会静默跳过颜色字段**,运营看到的是"识别完成"加一个空的主色。

但这不是一次新的决策:§3.22 原文写的就是「前提是 **products 先带上
`color_variant_id`**」—— 说的是**行带上值**,交接把它转述成了"列存在"。
决策记录本来是对的,漂的是那份副本。**正确顺序即 §3.22 原文,不需要重新拍板。**

### F2 / F3 / F4 / F5

    F2  `GET /spus` 的 `sku_count` 恒为 0 —— `_to_out(spu, sku_count=0)`,列表
        路径一个参数都没传。硬规则 4 点名的形状:为凑接口形状填的常量。
        修法是**去掉默认值**,让忘记数 SKU 变成 TypeError 而不是安静的 0。
    F3  `_translate()` 的注释说保留 `field`,代码丢了。`AppError.to_payload()`
        因此新增 `fields` —— **形状抄的是 `main.py` 里 422 处理器已经在发的那份**
        (`{loc, msg}`),不因为上游属性叫 `field` 就造第二种形状。
    F4  `spu_service` 把事务边界记到 `api/deps.get_session` 头上,而那个依赖的
        文档明写着它不提交(§7.8 第一条禁止项)。真正提交的是路由,**那是对的**,
        错的是注释,而这句错话被一条守卫的 docstring 抄了一份。危险在于:照着
        它删掉路由的 commit,建档什么都不落库而**测试不会红** —— conftest 的
        session 夹具跑在外层事务里,同一个 session 内看不出提交与否。
        因此补了 `test_the_archiving_route_commits_because_nothing_else_will`:
        "服务不提交"必须配一条"路由必须提交",只有前者会被读成"没人需要提交"。
    F5  `GET /spus` 序列化 `color_variants` 时按行惰性加载,一页上限 200 行就是
        200 次补查。这个端点不在查询预算那条棘轮的覆盖范围里(它盯的是
        `api.workbench_batch`)。

### 这一批真正的发现是一个形状,不是五条缺陷

F1、F3、F4 与那个顺序问题是同一件事的四次重演:**上游有一份正确的定义,
下游抄了一份副本,副本漂了,而没有任何东西测量副本。**

    上游                                  漂掉的副本
    禁读 variant_key 的守卫(对的)        没人测量它打开的缺口          → F1
    `SkuPlanError.field`(带着位置)       `_translate()` 的注释          → F3
    §7.8 / `publish_service` 的事务规矩    `spu_service` 的模块文档       → F4
    §3.22「products 先带上外键」           `HANDOVER.md` 的步骤顺序       → 顺序

所以这一批加的守卫里有两条不是钉某个函数的返回值,而是钉**两份副本必须同源**:
`test_every_drift_key_is_declared_in_the_frontend_union`(后端 `drift()` 的键 vs
前端联合类型)与 `test_no_module_still_blames_the_session_dependency_for_the_boundary`
(那句错话不许留在树里)。它们防的是这个形状本身。

---

## §3.24 A45-batch14:识别的判定与落库分家(阶段 3 第一批)

任务 7 接的是真实抽取器,但这一批真正值得记住的决定有四个,都不在
「怎么发那个 HTTP 请求」上。

### 一、判定必须住在零依赖模块里,否则它的分支永远测不到

`run_extraction` 里那段「这条观察要不要落证据」是 §6.3 与 §4.5 两条硬规则的
执行点。它原本长在服务层,而服务层 import 了 ORM —— 于是它的分支只有在
装了 sqlalchemy 的机器上才跑得到,而**整改环境没有**。

后果不是「少跑几条用例」。变异验证当场证明:把「模型说看不清就不落证据」
这条决定改回去,守卫**不红**——因为验它的那几条用例全部处在 skip 状态。
一条 skip 的守卫和一条不存在的守卫,防护力完全相同,但前者会出现在
「N/N 通过」那行数字里。

判定因此搬进 `extractors/evidence_plan.py` 与 `extractors/call_budget.py`,
两个模块零三方依赖。服务层只剩两件事:读配置、把计划物化成 ORM 行。
这条规矩本身不是新的 —— `workflows/publish_view.py` 顶部写着同一句话
(「判定必须留在 workflows/,那里零依赖,所以能在 tests/pure/ 被穷举」)。
新的是它的第二个理由:**在缺依赖的机器上,零依赖是"能不能被验证"的分界线,
不只是"好不好测"。**

### 二、编造的判据是「这次问了什么」,不是「注册表里有没有」

原来的检查是 `observation.name not in REGISTRY`。它挡得住模型自造的名字,
挡不住**注册过但这次没问它的字段** —— 而增量识别(`fields=` 只点几个字段)
恰恰是最常见的调用形态:模型顺手多答一个 `pattern_type`,它会被当成
本次识别的产物写进证据表,带着一个没人要过的值参与合并。

改成按 `targets` 判之后,还多了一条不那么显然的规则:**同一个字段既给了值、
又出现在 `missing` 里时,「没有值」胜出,并计入编造。** 这不是随手取一边 ——
模型自己承认这个字段判断不了却仍然给出了值,那就是「不可见属性编造」
这个指标的字面定义。让值胜出的话,一个模型亲口说看不清的值会进确认队列。

### 三、编造要计数落列,因为一个恒为 0 的指标比没有指标更糟

§14.4 要统计「不可见属性编造率」。过滤动作如果只写日志,那个指标永远是 0,
而 0 看起来是在说「模型从不编造」。所以 `fabricated_field_count` 是一列真实列
(迁移 0036),不是 JSON 里的一个键 —— 它要被 SUM。

**字段名不入库,只进日志且截断到 64 字符。** 名字可能是模型自造的任意字符串,
存清单等于让模型往我们的库里写自由文本。

### 四、识别配置独立于评分配置,即便填的是同一个端点

`EXTRACTOR_MODEL_*` 不回退到 `VISION_MODEL_*`。共用一个端点是常态,但那要
运维显式填两遍。

理由不是洁癖:识别的置信度校准按 (字段 × 模型 × Prompt) 分箱存
(§6.3:未校准的组合一律不自动确认)。静默共享的后果是**换一次评分模型,
识别的全部校准分箱同时作废**,置信度集体退回「未校准」,而界面上不会有
任何提示 —— 运营看到的是「今天开始所有属性都需要人工确认了」,
而原因在另一个页面的另一个下拉框里。

`TEXT_MODEL_*` 独立成组是同一条先例。

共用的是**判定**而不是配置:「这个端点要不要 Key」与「这笔账记在谁名下」
抽进 `llm/endpoint_trust.py`,两层调同一份。A45-#36 那个缺陷(对主机名做
前缀匹配,把 `10.gpu.example.com` 判成私网,于是缺 Key 也报「已配置」)
如果让两层各写一份,它会在第二份里原样重生,而且两份都有测试。

### 顺带修掉的:注册表的 `configured` 从名单改成问实现自己

`describe_extractors()` 的 a37 版按一张 `_SIMULATED_BACKENDS` 名单填
`configured`,注释自己承认「接真后端时这一行必须改成问它自己」。
真后端接上了,照办 —— 名单式写法会说「vision 已配置」,而它可能连
base_url 都没填,状态条据此告诉运营「识别已就绪」,然后每一次识别都报未配置。
这是硬规则 4 的第二次落地(第一次在 providers)。
## §3.25 A45-batch13-3:ORM 不许软化数据库约束;副本编辑必须向上同步权威

### 一、`passive_deletes="all"` 是 RESTRICT 的一部分,不是调优项

R1 的病灶:数据库层写了 RESTRICT,ORM 层却在删父行前**替你**把子行外键
置 NULL —— 约束没被违反,因为约束想保护的关系先被拆掉了。规矩:

    凡是 ondelete=RESTRICT 的外键,它在 ORM 侧的反向关系必须带
    passive_deletes="all"。True 不够(已加载的子行仍会被置 NULL)。

判断口径:问"这个删除该由谁说不"。答案是数据库的,ORM 就一个字节不许碰。
新表照此办理;守卫按 AST 钉 kwarg,行为进真库池子。

### 二、改副本的入口,必须在同一个事务里改权威

R2 的病灶:§4.2 把权威立在 `spus.audience`,而改受众的**标准入口**
(商品编辑)只改九份副本。权威与副本分叉没有任何诊断,直到读取方切换
那天被静默撤销 —— §3.17 的形状,引信更长。规矩:

    立一个权威列,就要在**当天**盘点所有会改它副本的入口,逐个补上
    同步(或拒绝)。"读取方还没切过去"不是缓期的理由 —— 恰恰因为读取方
    还在副本上,分叉才没有症状。

两条推论:权威 NOT NULL 的值,副本入口不许清空(拒绝,不是替它选);
凡带外键的行,传播类查询用"字符串命中 ∪ 外键命中",直到字符串列退役。

### 三、指定的看门狗必须有人渲染

R4 与 batch13 的 M9 是同一条:恒不触发的上限、无人渲染的诊断、
只存在于类型联合里的"真问题",都是**看起来在保护**。文档里写下
"由 X 盯着"的那一刻,X 的渲染点(或告警点)要能被指出来;指不出来,
那句话删掉比留着诚实。

## §3.26 A45-batch14-2:写在注释里的前提不是前提;规矩要么是不变式,要么是散文

### 一、"一张图一票"这种前提,必须有一处代码保证它

F1 的病灶:`merge.py` 的阈值注释写着「三张图里有一张不同意就够了」——
那是一个**前提**,不是结论,而全链路没有任何一处保证它。解析层明说
"只解析不过滤",判定层逐项 append,服务层照单建行,合并层
`scores[value] += weight`。四层各自都对得起自己那一行,合起来是错的。规矩:

    凡是某个阈值/公式的注释里出现"每 X 一次"这类计量前提,就必须能指出
    **哪一行代码**保证了它。指不出来,那个阈值调的是一个没人定义的量。

判断口径:把注释里那句话当成断言,问"什么输入会让它假"。F1 的答案是
"模型把一个字段答两遍",而那个输入今天就合法(Schema 没有 uniqueItems,
两个降级档位连 Schema 都没有)。

### 二、全称句的规矩要做成不变式,不是点名单

F4 与 §3.25 第一节是同一条的两面。R1 写下"凡是 RESTRICT 外键……",
守卫却点名了一个关系 —— 于是规矩活在散文里,而散文不会在下一个人
加表时说话。这正是 `_is_identity_column()` 自己骂过的"第二张名单"。规矩:

    写下带"凡是/每一个/所有"的规矩时,当场问一句:新加的那一个会不会
    自动进入覆盖?答案是否,就把守卫改成遍历,而不是把规矩改成个例。

配套的第二条:遍历型不变式必须同时钉住它**够得着**(至少命中一条已知的),
否则它是绿着装样子 —— batch13 的 M9、batch13-3 的 M11、本批的 F4,
三次同一个教训。

### 三、兜底逻辑不许长在判定内部

F3 的病灶:判定按 12 拦人,提示语按 0 说话。规矩与 `field_limits`
同源 —— **一个数字有第二个消费者的那一刻,它就该有名字**。
`effective_ceiling()` 不是抽象,是"闸和提示语必须是同一个数"的载体。

### 四、"文件里出现过这串字"从来不等于"这行代码在生效"

第三次了(batch13-3 的 M2、M11,本批的 N15)。三次的形状完全一样:
守卫用子串/无锚正则,而变异只需要把那行**注释掉**或**包进 `{false && }`**。
规矩:

    文本型守卫一律整行锚定(行首只允许空白),或锚在语法结构的开括号上。
    写断言之前先造变异 —— 反过来做的那一版,三次里有三次是绿的。

## §3.27 A45-batch14-11:队列成员资格是一张穷举表;拦截作用域必须自己论证,不许沿用去重键

三件事,两条来自 PRD §11 的两行,一条是修它们时撞出来的。

### 一、「哪些事实进确认队列」是一张必须逐个归档的表,不是一句排除法

`AttributeStatus` 的文档字符串早就写着 `CANDIDATE`(留证据不采信)与
`SUGGESTED`(够格进队列)不能混,PRD §11 又把它写成明文规则,
而 `workbench/flow.py` 一直写的是 `name in suggested or name in candidate_only`。
全仓 `candidate_only` 在 `tests/` 里出现次数为**零** —— 它不是一个被权衡过的
决定,是一处没人看过的地方。

判定收进零依赖的 `attributes/queue_policy.py`,而且是**每个枚举成员逐个归档**,
不是 `!= CANDIDATE`。两种写法今天等价,分歧在将来:排除法之下新增任何状态
取值都默认进队列,而新增取值的那个人不会想起这道口径。
与 §6.2 的 `CONFIRMED_ROLE_SOURCES` 挑白名单不挑排除法是同一条理由,
也和硬规则 4 第二次事故同型:**缺一档不报错,判定一路走到最宽的一档。**

兜底方向也是一个决定:认不出的状态归「产出一条待办」,不归「静默消失」。
反过来兜的话,库里一个拼错的状态会让那个字段从确认队列和阻断清单里同时消失,
而商品照样导不出去 —— 运营看到的是一件卡住但没有任何理由的商品。

**这条今天不改变任何一件商品的判定**,因为 Mock 抽取器给的置信度够高。
它要到真实模型接上、校准分箱还是空的那天才生效 —— 而那时
`decide_status` 对每一个字段都返回 CANDIDATE,代码和现在逐字相同。

### 二、拦截的作用域不能沿用去重键,哪怕两者看起来应该一样

`ingest()` 的去重键是 `(product_id, sha256)`,而 §11 的溯源冲突写的是
**同 SPU**。差出来的那一块不是边角:一个卖三色的款,把 A 色生成好的图
下载下来当样品传给 B 色的 SKU —— `product_id` 不同,去重不命中,
于是新建一条 `source=MANUAL_UPLOAD` 的行,派生成 `PRODUCT_EVIDENCE`,
**直接进识别输入,每张一次真实付费调用**。

规矩:**一道拦截的作用域必须自己论证一次,不许从旁边那个查询抄。**
「反正它们查的是同一张表」不是论证。

同一条的另一半:空值不是「所有值」。`MediaAsset.spu == None` 会被渲染成
`spu IS NULL`,于是全库没有 SPU 的素材成了同一个 SPU 的兄弟。
写取数条件时,可空列必须显式决定空值那一档去哪里。

### 三、两个闸不重叠时,两个都要;而修洞不许砸掉正常动线

`verdict()` 管新建行那一路,`may_fill_role()` 管去重命中那一路。
只做前者,把图传回原 SKU 仍会给 AI 行盖上 `role_source=HUMAN`;
只做后者,传给兄弟 SKU 会新建一条干净的 `PRODUCT_EVIDENCE`。

但去重命中那一路**刻意不改状态**:命中的很可能就是生成链路自己的候选行
(`candidate.media_asset_id` 指着它),隔离它等于把一张合法候选图从图片集里
拿掉。**修一个洞,砸一条正常动线,是净亏。**那一路靠补角色闸挡住,
另记一条 warning 日志。

### 四、AST 守卫要打在「那个结构问的是对的问题」上

本批变异第一轮 30/32,两条漏网的都是 AST 守卫:

    assert "IfExp" in dumped              -> 变异换成 `if True`,照样是 IfExp
    assert "Not()" in d and "quarantine_reason" in d
                                          -> 函数本来就有 `not deduped`、
                                             本来就要给那一列赋值,两个各自都真的
                                             东西凑在一起,证明不了它们在同一处

与 batch14 的 M10(`if False: 记流水(...)`)、batch13-3 的 M2 同型。补一条规矩:

    结构型守卫要断言到**节点的位置关系**(这个 IfExp 的 test 是哪个表达式、
    这个 If 的条件里有没有那一问),不是「函数体里出现过某个记号」。
    读源码的守卫一律先剥掉文档字符串 —— 按整段源码找字符串在这个仓库
    栽过三次,最后一次比较的是一段说明文字(batch14 的 M30)。

## §3.28 A45-batch14-12:守卫要钉性质,不钉做法;派生状态可以先于列落地

### 〇、先说号:同一个批次号被三条线同时占用

`14-11` 有三份:门禁批(在装齐依赖的机器上跑门禁)、§11 两条新场景批、
以及本批。三者互不重叠,**硬冲突只有一处** —— `tools/mutate_batch14_11.py`
两份内容不同,同名不同物。

按 §3.11 的老办法处理:先到的两批留在原号,后到的补号。本批改成 `14-12`,
守卫(`test_a45_batch14_12_run_state.py`)与变异脚本(`mutate_batch14_12.py`)
一并改名,`SUITE_FILTER` 跟着改。**正文没有为了让号连续而改写别人的批次记录** ——
STATUS 里三个块各自保留它们当时报的数字,合树之后的数字单独记一行。

一般化:批次号是**文件名的一部分**,而文件名冲突是唯一真正会出事的那种冲突。
并发的几条线各自打包时,号可以撞;合树的人负责补号,并且补的是**自己那一批**,
不是别人已经交付出去的那一批。

### 一、守卫钉住「用哪一行代码做到」的那一刻,它就把凑合办法变成了规格

A2 有一条守卫断言前端源码里出现 `succeeded_count ?? 0) === 0` —— 那段代码
正是**硬规则 4 禁止的东西**(前端自己判三档)。守卫想守的性质是
「全失败不能显示成功」,守到的却是当时实现它的那一行。

于是判定搬到后端(硬规则 4 要求的方向)之后,那条守卫变红,
**而它变红的原因是病治好了**。规矩:

    写守卫之前先把那句性质说出来,再问「有没有第二种实现方式也满足它」。
    答案是有,而断言会把另一种实现判红,那就是钉错了层。

判断口径很简单:守卫失败时,修的人第一反应是「改代码」还是「改守卫」。
第二种反应出现,通常说明守卫钉的是做法。

配套的第二条:钉性质的守卫要**两头一起钉**。这次改成「后端算好的那一档
真的被读了」+「error 语气真的还在」——只写前一半的话,一个什么都不显示的
前端也能通过。

### 二、「文件里出现过这串字」第五次,这次是重构撞上的

`test_single_item_extract_skips_apply_evidence_when_all_failed` 断言源码里
出现过 `succeeded_count`。判据换成 `run_is_authoritative` 之后它**照样绿**,
因为换判据时留下的注释里写着「判据从 `succeeded_count > 0` 换成……」。

前四次(batch13-3 的 M2 / M11、batch14-2 的 N15、batch14-4)都是**变异
把代码注释掉**而守卫照样匹配;这次是**重构把代码移走**而守卫匹配到了注释。
方向相反,根因同一句话。§3.26 第四节那条规矩因此加一句:

    文本型守卫不只要防「代码被注释掉」,还要防「只剩注释」。
    锚在语法结构上(AST 取那个 `if` 的条件)时两种都防住了 ——
    注释在 AST 里根本不存在。

### 三、变异脚本天生看不见「本批打破了别人的守卫」

`mutate_batch14_*.py` 都只跑**本批那一份套件**(`SUITE_FILTER`),
这是刻意的:跑全量要几十分钟。代价是它**结构上**验不到一类回归 ——
本批改动让别人的守卫变红或变假绿。

上面那两条正是这一类,它们是 `make check-offline` 跑全量时掉出来的,
不是 34 条变异跑出来的。所以规矩不是「把过滤器去掉」,而是:

    改了别的模块共用的东西(枚举、编码器、判据)之后,除了本批变异,
    还要跑一次全量纯层,并**重跑被改模块那一批的变异**。
    本批因此重跑了 batch14-9 的 16 条(它的 `scope_fingerprint` 被改过)。

### 四、状态可以先于列落地,只要它派生自真实的列

§4.6 要五个新列,要迁移,要真库。但「这次识别算哪一档」这个值**今天就有人
在算**——前端,而且算错了。等那一列意味着这个错再多活一批。

做法是先落成派生属性(输入是 `image_count` / `succeeded_count` /
`failed_count` 三个真实的列),它满足硬规则 4 的追溯要求。判定住在零依赖
模块里,那一列落库时判定一个字不用改,只是调用点从读取方挪到写入方。

**这条有边界**:派生属性只适用于「输入已经都是真实列」的情况。
输入里但凡有一项要靠猜(比如拿 `variant_hint` 当归属),就不能这么做 ——
那是 batch14-9 / 14-10 两次拒绝接线的同一条理由。

### 五、幂等键要挡的是付钱之前的事,所以它必须在付钱之前算得出来

§9.2 的键含 `model_version`。本仓有两个来源:配置与模型响应。
取响应的实现**编译得过、测试也绿**,只是键只有在付过钱之后才存在 ——
而这个键要挡的正是双击与网络重发,两件事都发生在付钱之前。

一般化:

    幂等键的每一项都必须在**它要挡的那件事发生之前**就是已知的。
    有一项要等结果才知道,那个键挡不住任何重复。

判定层因此拒绝用空的模型/Prompt 版本建键,逼调用方去配置里取。

### 六、唯一约束按字面落码会把重试锁死

§9.2 说「落 idempotency_key 唯一约束,数据库裁决」。全表唯一之下,
一次 FAILED 之后同样的输入再也建不出第二个 run —— 而那正是重试的定义。

占键的只该是**还会被复用**的那几档,索引写成部分唯一索引。
名单从 `reuse_verdict` 派生而不是手写第二张:漂移的表现是
「代码说该新建,库说键被占了」,接口报一个和用户动作毫无关系的 409。
谓词由孪生函数生成,与 `media/evidence_rules.py` 的 CHECK 孪生同源。


## §3.29 A45-batch14-13:不可达的防御代码等于没有;并发的几条线不许各自动迁移链

### 一、一段没有任何用例够得着的判断,和删掉它没有区别

「没有图的颜色不算失败」这条规则的载体是 `total > 0`。它写在循环里,
而循环里的 `total` 恒大于 0 —— 于是删掉它不会有任何东西变红。

这与 batch14-9 的 S5(指纹里冗余的 `status`)是同一型,而两次的修法应当一致:

    发现某个条件/字段「今天不可能为假」时,不要留着它当注释用。
    要么删,要么**把它挪到能穷举的地方**并起个名字 —— 让它的两个边界
    各自有一条用例。留在原地的下场是下一个人顺手清理掉它,而那时它已经
    可达了(本例:等到 §4.6 开始把「没有图的颜色」也列进成绩单那天)。

判断口径:对每一个防御性条件问一句「什么输入会让它为假」。答不上来,
它现在就不在任何守卫的射程里。

### 二、并发的几条线共用的单写者资源:迁移链

本仓有先例在没有真库的机器上加迁移(batch14 的 0036,「已写、未执行」)。
本批**不照做**,理由不是谨慎:

    三条线同时在同一个基线上开工时,迁移链是**单写者资源** ——
    `verify_delivery` 盯着「单一 head」,而各自加一条 0037 之后,
    合树的人**无法在本地解决**这个冲突:改谁的 revision 都要动
    对方已经交付出去的文件与它的 down_revision。

一般化:并发开工时,凡是「全局唯一 + 有序」的东西(迁移链、端口号、
枚举里的排序位、任务编号)都该由**一条线**统一带走,其余线把需求写成规格。
批次号可以撞(合树时补号,§3.28),迁移链不行。

### 三、守卫读源码时,字符串切是一种慢性假绿

本批一次撞见两条:`entry.index("),")` 被登记项注释里的一个 `(...)` 提前命中;
`[:200]` 定长窗口在登记项变长之后切在半路上。

两条的共同后果**不是变红,是变松**:正向断言碰巧还在残段里,而**反向**断言
(「这几个不许出现」)被残段喂成平凡真。守卫因此在某次无关改动的那一刻
悄悄不设防,并且照样显示 PASS。

    读结构化的东西(dict / 调用 / 条件)一律走 AST。
    改完之后要**验它咬得住**:临时把不该出现的那个东西塞进去,看它红不红。

这是 §3.26 第四节那条规矩的第六次成立,前五次分别是 batch13-3 的 M2 / M11、
batch14 的 M30、batch14-2 的 N15、batch14-12 撞见的注释假绿。


## §3.30 A45-batch14-14:守卫也要被审;规矩要挑那条不响的失效

### 一、判定有变异兜底,守卫没有

每一批的判定都被 20~34 条变异验过,而**守卫本身只有「它今天跑绿」这一条
证据** —— 而假绿守卫的定义正是「它跑绿」。14-12 与 14-13 连着两批,
真正值钱的发现全是副产品(四条守卫钉错了层或已经假绿),一条都不在计划里。

规矩:

    每隔几批,拿一次**守卫本身**当审计对象。判据要做成门禁,
    不是走读 —— 走读发现的那四条,恰恰是前面每一轮评审都走读过的文件。

### 二、一条规矩要挑那条**不响**的失效

按字符串定位切源码,全树 87 处。禁掉全部会把绝大多数正当用法一起判红。
而它们的失效方向不同:

    正向断言碰上窄窗口   断言变假 -> 红 -> 有人会看见
    反向断言碰上窄窗口   断言变真 -> 绿 -> 没有人会看见

只管反向那一侧之后,全树只剩 10 处,零容忍做得到。一般化:

    要禁一种写法之前,先问它坏掉的时候**响不响**。响的那一类可以先放着
    (它自己会喊);不响的那一类必须零容忍 —— 而把两类捆在一起禁,
    结果是门禁误伤,然后被加白名单,然后两类都不管了。

### 三、「这一段到哪为止」有三种,别拿一种去框另一种

    地标      两端各自唯一           `window()`
    分隔符    在文件里必然出现很多次   `braced_block()`,按配平找
    行        终点是换行             `only_line()`,行首锚定

拿地标去框分隔符的表现是「工具挡我」——**而挡得对**:nginx 的 location
按「第一个 `}`」切在今天恰好对,只因为那个块没有嵌套,而那不是一条能靠的
性质。写本批时连着被挡两次才想明白第三种存在。

行首锚定还有一条附带好处:`#` 开头的注释天生不算命中,而子串计数算 ——
14-13 那条被注释里的 `),` 抢先命中的守卫,根子就在这里。

### 四、门禁的三处登记要实测,不是照着写

硬规则 3 说加一条门禁要改 Makefile、ci.yml、`check_ci_runs_every_gate()` 的表。
本批三处各有一条变异,并且**实测过第三处真的在盯着**:临时删掉 ci.yml 里
那一行,`verify_delivery` 当场 12/13 并点名。

照着写而不实测的下场,`mutate_contract_tests.py` 演过一次:它报「18 条全被
抓住」,而它点名的 15 个测试一个都不存在(batch14-3 退役了它)。


## §3.31 A45-batch14-17:点名做法的守卫会因为进步而变红;两处真相修不成一个补丁

### 一、守卫点名做法,第三次

三批之内同一个形状撞了三次,都是**守卫钉的是"怎么做的",不是"要成立什么"**:

| 批 | 守卫点名了 | 变红的真实原因 |
|---|---|---|
| 14-16 | 迁移链 `heads == {"0037"}` | 有人正常地加了一条迁移 |
| 14-17 | 页面 `import { useUrlSeed }` | 换了一个更好的 hook |
| 14-17 | `"setAudience" in page` | setter 换成了 `filters.patch` |

三次红的原因都是「有人把这件事做得更好了」。这类守卫的成本不在改它,
在于**它把一次正当的改造报成一次故障**,而下一个人的第一反应是"我是不是改坏了"。

`CLAUDE.md` 早写着「规矩要么是不变式,要么是散文」。它被违反三次说明
那句话还不够操作 —— 补一条判据:

> **写守卫之前先问:如果有人用另一种更好的做法达成同一件事,这条会不会红?**
> 会红,就说明钉的是做法。把断言改到"要成立什么"那一层,
> 或者干脆写成注释(散文不假装是门禁)。

对应到三条的改法:不查 `import` 哪个 hook,查**这一页的筛选声明表里有没有那个参数名**;
不查有没有 `setAudience` 这个名字,查**这一页认不认 audience 筛选**。

**三次都是红不是静默过期**,这一点值得记在旁边:正向断言切错了会变红、有人看得见;
真正贵的是反向断言切窄之后变成一句永远为真的话(14-14 那条门禁管的就是它)。

### 二、两处真相修不成一个补丁

`useUrlSeed` 的口径是「URL 参数是初值,不是真相」,页面接手后擦掉参数。
FE-GLOBAL-06 报的是「URL 筛选被消费后清除」,看起来像是"别擦就行了"。

不行。病根不是那一行 `clear()`:

    只要 URL 是初值,组件内那份 state 就是第二处真相
    两处真相之间,任何一次重新同步都要在「谁顶掉谁」上做选择
    而擦参数,正是当时为了不让 URL 顶掉运营的手选而做的那个选择

也就是说 `clear()` 是**上一个选择的结论**,不是笔误。删掉它而不改口径,
换来的是另一个方向的 bug(运营手选之后被 URL 顶回去)。

一般化:**看到一个"顺手擦掉/顺手同步/顺手兜一下"的补丁时,先找它在替哪一次
选择买单。** 如果那个选择的前提是"这件事有两份",那么正确的改动是把它变成一份,
而不是把补丁修得更聪明。

### 三、把状态搬进 URL 会开一扇新门

这条是上一节的直接后果,单独记是因为它**不是回归,是新增的失效路径**:

    URL 化之前:改筛选的入口都是 JS,收成一个口子就守得住(BLOCK-09 的解法)
    URL 化之后:后退、前进、直接编辑地址栏都会改筛选,而它们一行 JS 都不经过

于是「换筛选就清空勾选」写在任何 setter 里都是漏的。判据要挂在**状态本身**上
(一个规范化的 signature),不挂在"谁改了它"上。

规范化那一步同样不能省:`?page=1` 和不带 page 是同一个状态,
拿原始查询串当依赖会白白清一次勾选 —— 而「勾选莫名其妙没了」
只要发生过一次,运营下次就不敢用批量了。

### 四、会话位置不进 URL

筛选是**条件**,在两个人那里意思一样;游标和勾选是**位置**,不是。
`?index=7` 贴给同事,他打开看到的是另一件商品 ——
**看起来精确、实际上指错**的地址比不带它更贵。

判据:一个值能不能进 URL,看的是"把这个地址发给别人,他看到的还是不是同一件事"。

---

## §3.32 A45-batch14-18:算出来没人读的数,和没算是一回事;点名做法第四次

### 一、本批修的缺陷,形状是「解析了、抄下了、没人读」

`providers/fashn.py` 从 `x-fashn-credits-used` 响应头解析出厂商实际扣的额度,
抄进候选图的 `metadata["credits_used"]` —— 然后**全仓再没有第二处读它**,
grep 只剩测试。台账那边记的一直是 `max(provider_count, 1)`,也就是"我们收到几张图"。

两个数不是同一个量。官方参考表(`docs/vendor/fashn-skill/reference.md`「Credits」):

    tryon-max  balanced  1k:2  2k:3  4k:4     (× num_images)
               quality   1k:3  2k:4  4k:5

一张图 2 到 5 个额度,取决于 `FASHN_RESOLUTION` 与 `generation_mode` ——
**两个旋钮运营在设置页都能改**。

所以这不是"记得不够精确",是**记错**,而且:

    倍数不固定    2 到 5,跟着配置浮动,没有一个常数能把它折算回去
    方向恒定      永远是少记
    没有征兆      预算横幅一路绿着,账单是它的好几倍

一般化:**一个算好了、存下了、但没有任何读者的值,和没算是一回事 ——
差别只是它让人以为这件事已经做了。** `verify_delivery.py` 的 `WIRED_MODULES`
本来就是为这个形状立的(`record_cost` 零调用那次),本批把两个新函数登记了进去。

### 二、`units_source`:一个数字必须带着"是谁说的"

§10.2 第 5 条准入是「用量记录与 Provider 后台账单条数一致」。对账的人发现
某一行对不上时,第一个要回答的问题是:

    这个数是厂商说的,还是我们猜的?

前者对不上要去查厂商,后者对不上是我们算错了 —— **两条路完全相反**。
不落这一列的话每一行看起来同样权威,而在本批之前**每一行都是猜的**。

判据一般化:**一个会被拿去和外部事实核对的数字,必须同时存下它的来源档次。**
否则"对不上"这个信号无法定位,而无法定位的信号最后都会被当成噪音关掉。

默认值方向也是刻意的 —— `record_usage(units_source=...)` 默认 `inferred`:

    忘了接线   台账诚实地说"这是估的",对账时看得见
    默认权威   一张全是猜的表冒充和账单同源,而没有任何地方会说它不是

和 `is_simulator = True` 同一个取舍:**两个方向的错误代价不对称时,
默认值站在"承认自己不知道"那一边。**

### 三、同一个数抄在 N 张图上,读的时候必须去重

`fetch_results` 把每条 prediction 的额度抄在**它产出的每一张**候选图上
(那是给排查用的现场)。于是 `num_images=4` 那一次,4 张候选各自带着同一个
per-prediction 总额,直接 `sum()` 就是 **4 倍高估**。

这个坑本批之前没人踩进去,只是因为**没有人读过这个字段**。它会在第一个
读者出现时立刻生效 —— 而第一个读者通常不知道那份数据是怎么写进去的。

一般化:**为排查而冗余写入的数据,不能直接当成可聚合的数据用。**
读法要收在一处,并且那一处必须知道冗余的粒度(这里是 `prediction_id`)。
本批把它收进 `usage_from_candidates()`,变异 F1 验的就是这条。

### 四、点名做法的守卫,第四次

§3.31 记了三次,本批第四次:

| 批 | 守卫点名了 | 变红的真实原因 |
|---|---|---|
| 14-16 | 迁移链 `heads == {"0037"}` | 有人正常地加了一条迁移 |
| 14-17 | 页面 `import { useUrlSeed }` | 换了一个更好的 hook |
| 14-17 | `"setAudience" in page` | setter 换成了 `filters.patch` |
| **14-18** | **`billable_units=` 实参里有没有 `provider_count` 这几个字** | **换成了「先问厂商、问不到再退回 `provider_count`」** |

第四次尤其说明问题:那条守卫叫
`test_the_ledger_bills_what_the_provider_produced_not_what_we_saved` ——
**名字说的是不变式,断言查的是字面量**。名字对、断言错,于是没人怀疑过它。

改法:把实参连同它引用的局部变量的赋值一起摊平,再问"这笔账追得到 Provider 吗",
两条合法路径(厂商自述 / `provider_count`)取并;另加一条反向断言禁止追到 `stored`。

补一条比 §3.31 更具操作性的判据:

> **守卫的名字和它的断言必须说同一句话。** 名字里写的是不变式而断言查的是
> 某个字面量时,以名字为准去改断言 —— 不是反过来把名字改窄。

### 五、反向断言第一版又切了文本窗口,又被挡回来

本批那条「今天还报不出来的付费路径」守卫,第一版是
`window(source, "record_usage(\n", "\n    )")`,被 `_helpers.window()` 当场挡回:
终点在文件里出现 21 次。

这是 14-14 那道门禁第二次在写的时候就拦住人(不是事后审计发现)。值得记的是
**正确解法不是把窗口调准,是根本不用窗口**:调用的实参名单本来就是 AST 上的
一个精确集合。退回文本匹配是因为顺手,而顺手的代价是给自己留一条
"切窄之后静默变真"的路。

一般化:**要对代码结构下断言时,先问这件事在 AST 上是不是已经是一个精确对象。**
是的话,任何文本窗口都只是把一个确定问题换成一个概率问题。

### 六、存量回填成 `inferred`,和迁移 0034 拒绝回填不矛盾

0034 的说明写着「不回填历史行的 `billing_key`:回填等于替过去的账做一个
今天才定下来的判断」。看起来本批违反了它 —— 没有。

区别在于**存量的正确取值今天可不可知**:

    0034 的 billing_key   不可知。哪一行才是真的取决于厂商账单
    本批的 units_source   可知。读厂商额度的代码本批之前不存在,
                          所以每一条历史流水都出自 max(provider_count, 1)

`inferred` 不是一个猜测,是对**我们自己那条代码路径**的陈述。

判据:**回填之前先问"这个值是关于外部世界的,还是关于我们代码历史的"。**
前者不许猜,后者是事实。变异 M4 钉的是这条的边界 —— 一条按
`billable_units > 1` 挑行的 UPDATE 会把它变回猜测。

---

## §3.33 A45-batch14-19:防的是明天的调用点;守卫点名做法第五次;门禁自己有盲点

### 一、"判定对了"和"入口收了"是两件事

§5.1 白名单的判定从 A45-batch14-7 起就是对的,AI 图进不来。但交付原文那半句
**「白名单查询助手成为唯一取数入口」**一直没做,而它防的根本不是同一件事:

    判定对了   保护的是**今天写好的那一个调用点**
    入口收了   保护的是**明天照着抄的那一个调用点**

搬家之前的形状是"未过滤取数谁都能调 + 一处调用点自己过滤"。新写一个调用点
照着 `usable_assets()` 抄一行,AI 图就进了付费抽取器,而没有任何地方会红。

一般化:**一条规则如果只写在使用点上,它的强度等于每一个使用者的记性。**
要让它有强度,得让"绕过它"这件事**抄不出来** —— 把未过滤的入口从这条路径上
拿掉,而不是在这条路径上多写一次检查。

`usable_asset_count()` 是同一条思路的第二次应用:识别路径需要的是**条数**,
那就只给它条数。**拿不到行,就不会有人用错行。**

### 二、粗筛必须是超集,而且判定只许有一处

`evidence_assets_for()` 的分工是「SQL 粗筛 + Python 判定」,不是"SQL 全包"。
后者等于给 `evidence_class` 造第二个判定点,而 `media/evidence_rules.py`
顶部整段在说这件事:两个判定点漂移时没有人会发现。

粗筛的正确性条件只有一条:**判定为 True 的行一定通过粗筛**。所以粗筛用到的
列被收进一张显式的表(`_COARSE_FILTER_COLUMNS`),加一列必须先去补蕴含证明。

值得单记的是**两个方向的失效代价不对称**:

    粗筛多放一行   Python 判定兜住了,只是多读几行 —— 可接受
    粗筛多挡一行   那张图静静不进识别输入,而**"本该识别却没识别"没有任何
                   地方会报**:运营看到一次成功的识别,只是少了几个字段,
                   而字段少了会被当成"这张图上看不出来"

判据:**加一条过滤条件之前,先问它错了会不会有人发现。**不会的话,它要么
不加,要么加了就得有一条测试专门盯着它。

### 三、点名做法的守卫,第五次

§3.31 记了三次、§3.32 第四次,本批第五次:

| 批 | 守卫点名了 | 变红的真实原因 |
|---|---|---|
| 14-16 | 迁移链 `heads == {"0037"}` | 有人正常地加了一条迁移 |
| 14-17 | 页面 `import { useUrlSeed }` | 换了一个更好的 hook |
| 14-17 | `"setAudience" in page` | setter 换成了 `filters.patch` |
| 14-18 | `billable_units=` 实参里的 `provider_count` 字样 | 换成了"先问厂商" |
| **14-19** | **过滤写在 `run_extraction` 的函数体里** | **过滤搬进了取数入口** |

改法这次是**参数化宿主**:`WHITELIST_HOST = (模块, 函数名)` 写在文件顶部,
两条守卫都对它下断言。搬家再发生一次时改两行,断言本身不必动。

补一条判据(§3.32 第四节那条的延伸):

> **守卫里凡是出现"某段代码在哪个函数里"的假设,都把那个位置提成常量。**
> 位置是会变的,性质不会 —— 而把位置写死在断言里,等于让每一次搬家都
> 报成一次故障。

### 四、退役一条变异,并说清楚为什么不是漏了

14-7 的变异 P1(「过滤结果算出来了但没赋回 `assets`」)本批之后跑出 GREEN。
**原因不是守卫漏了,是被建模的那个缺陷已经不可能发生**:旧形状里 `assets`
先被绑成未过滤结果,删掉赋值之后代码照跑、用的是未过滤那份(静默致命);
现在 `assets` 只剩一个绑定点,删掉它是 NameError(第一次调用就炸)。

留着它只会让"24/24 全红"变成一句谎话。所以退役,并在正面补一条守卫钉住
"绑定点唯一"这条性质 —— 那条性质一旦不成立,P1 那个洞就重新打开。

一般化:**变异跑绿时先分清是"守卫漏了"还是"缺陷不存在了"。**
两者的处理相反:前者补守卫,后者退役变异**并把使它不存在的那条性质钉住** ——
只退役不钉,等于把洞交给下一次重构。

### 五、门禁自己也会有盲点,而它的失败方向是假红

`verify_delivery.py` 的接线门禁按**文件名**判断"是不是模块内部互调"。
于是 `app/media/service.py` 的函数被 `app/attributes/service.py` 调用时,
被当成自己调自己 —— 判定未接线。

而 `service.py` 是这个仓库里最常见的文件名(media / attributes / workbench …)。
**这条门禁在最容易发生接线遗漏的那一批模块上恰好是瞎的。**

更要紧的是它的失败方向是**假红**:被拦下来的人最省事的做法是把条目从
`WIRED_MODULES` 里删掉 —— 于是门禁不是被修好,是被静静关掉。

一般化:**假红比假绿更容易让门禁死掉。**假绿至少还在那里,假红会诱导人
去掉规则本身。写门禁时要专门想一遍"它误报时,最省事的解决办法是什么" ——
如果答案是"把这条规则删了",那这条门禁需要先做准。

## §3.34 A45-batch14-20(阶段 3 · 识别 run 身份):欠账守卫是有还款日的;docstring 是源码的一部分

### 一、把"今天做不到"写成一条会自己变红的守卫,是这个仓库最划算的一件事

`run_state.py` 与 `scope_fingerprint.py` 两个判定模块,验完之后**连续八批
接不上线**。八批里没有一次是靠人记住的 —— 每一批都有一条守卫在说同一句话:

    test_the_idempotency_half_cannot_be_wired_yet_and_here_is_exactly_why
    test_this_module_cannot_be_wired_yet_and_here_is_exactly_why

而且它们写明了**还款条件**(那五列落库)和**还款动作**(接线、登记、删掉本条)。
迁移 0040 一落地,两条当场变红,红的信息就是待办清单。

对照一下没有这种守卫的欠账会怎样:`docs/STATUS.md` 里那条"AI 图会进识别
输入并产生真实付费调用"的警报,在 batch14-7 修好之后**又挂了十二批**,
直到 14-19 才有人发现它是过期的。区别不在于谁更用心,在于一个会自己变红、
另一个只是文字。

一般化:**欠账要么写成会变红的守卫,要么就不要写。**写在文档里的欠账
有两种下场 —— 被忘掉,或者在还清之后继续吓唬人,而后者更糟。

配套的一条:欠账守卫的反向断言必须点到**恰好那一件事**。本批 14-13 那条
`for owed in ("failed_scopes", "input_asset_ids", "requested_scope")` 就点宽了 ——
`requested_scope` 落库跟它要的东西(跑完之后哪个作用域全军覆没)没有关系,
于是它红在一个不相干的改动上。红错的守卫会诱导下一个人整条删掉它。

### 二、docstring 是源码的一部分,第七次

§3.26 第四节:"按字符串在源码里找东西,找到的从来不保证是你以为的那个位置。"
本批在同一天被咬了两次,而且是**两个相反的方向**:

    守卫变假红   VisionAttributeExtractor.declared_versions 的文档里写着
                 "不取 result.model_name",一条查 `result.` 的断言把这句
                 **解释**当成了实现,红在一个完全正确的代码上
    守卫变假绿   _run_identity 的文档里出现 imported_url_trusted=True
                 (解释为什么两边口径要一致),把一条查这个关键字的断言
                 喂成了平凡真 —— 口径真改掉时守卫照样绿

第二个方向才是要命的那个,而它只有靠变异跑才发现得了(本批 P2)。

修法固定下来:**读函数体的守卫一律先剥 docstring**,助手写成 `_code(fn)`。
`ast.unparse(fn)` 是不能直接用的 —— 它把文档一起吐出来。

顺带记一条同源的:`ast.unparse` 会**规范化引号**,所以断言里写
`'getattr(x, "y", None)'` 永远匹配不上,得写单引号那一版。

### 三、判定的宿主会搬家,守卫要参数化(点名做法第六次)

`terminal_status_for` 的调用点在 14-11 到 14-19 期间挂在
`models/attribute.py` 的派生属性上;五列落库之后必须挪到写入方,
因为**索引谓词由数据库读列求值,而属性只在 Python 里存在**。

守卫把"这段代码在哪个函数里"当成了不变式,于是搬家那天它变红,而口径
其实更严了。第六次,处理照旧:文件顶部一个 `VERDICT_HOST = (模块, 函数)`,
断言读它。

本批还给这条补了一个前五次没覆盖的方向:搬家会让**旧断言变松**。
`"row.status" in body` 原来只可能命中那一个比较,搬家之后终态自己也写
这一列,于是它变成平凡真(变异 D3)。所以搬家时不只要改宿主常量,
还要回头看一遍原来那些**宽断言**在新宿主里是不是还唯一。

### 四、宽默认值要朝"不放行"那一侧倒,包括 server_default

`status` 这一列不回填,存量行落 `FAILED` 而不是 `COMPLETED`。理由不是
"失败更保守"这种口号,是两个方向的代价不对称:

    落 FAILED     一次真的成功过的旧 run 显示成失败。难看,不花钱
    落 COMPLETED  一次从来没有被判定过的 run 以"算数"的身份参与
                  事实合并与占键 —— 两件都要花钱的事

这与 `ExtractionOut.status` 默认 `FAILED`、`asset_is_extraction_input` 认不出
来源时兜底到不可用,是同一条规矩的第三次应用:**默认值是给"没有人想过
这种情况"准备的,而那种情况下不该有人被放行。**


## §3.35 A45-batch14-20(阶段 4):判定写在测不到的地方等于没写;回落逻辑在维度增加后会变成静默的错

### 一、判定不许落在 import 数据库的模块里

方案指纹第一版写在 `models/generation_plan.py` 的 `plan_fingerprint()` 里。
形状上说得通:指纹由这张表的字段派生,放在表旁边最顺手。

代价是**它一行都测不到** —— 那个文件 import sqlalchemy,而整改环境没有。
而指纹算错不报错:它只会让"换了方案"这件事静默不触发重出图,
或者反过来让每次创建任务都顺带把自己的图片集判成过期。

搬到 `workflows/generation_plan.py`(零依赖)之后,每一条性质都被穷举:
归属不进指纹、每个参数各自改一次、`10` / `10.0` / `"10.00"` 编成一个串、
角度顺序不进键。

**判据:任何一条"改了它,下游会不会失效"的判定,都必须落在
`run_pure_tests.py` 覆盖得到的模块里。**判断标准不是"它属于哪一层",
是"它算错的时候谁会红"。这与 `.importlinter` 的 `grading-stays-pure`
同源 —— 那条契约立的时候讲的也是这件事:回答"这张图为什么判 C"
不该需要先造一套库内数据。

### 二、"看起来更友好"的回落,在维度增加之后会变成静默的错

`image_set_rules.primary_for()` 原来写成:

```python
for want in (variant_id, None):     # 颜色没有专属主图 -> 回落到 SPU 通用
```

单色时代这是对的,含义是"没配主图就用默认那张"。

多色一来,**同一段代码的含义变了**:变成"缺这个颜色的图就用别的颜色的"。
而在颜色绑定入口上线之前,通用图就是第一个颜色的图 —— 于是这条回落的
实际后果是红色 SKU 挂着黑色主图上架。它不会报错,也不会有任何测试变红:
`test_primary_falls_back_to_the_spu_level` 当时是**绿的**,而且它钉的正是
这个错误行为。

§6.5 把 BLOCK-02 挂了几版的那个业务决定定死了:「不得回退使用其他颜色的
图片,缺图就是缺图(BLOCKED)」。缺图必须表现成缺图,由批准这一步拦住,
而不是在发布时用另一张图顶上。

**判据:写回落逻辑时要写清它回落的是哪个维度上的默认;新增一个维度
(颜色、站点、受众)时,逐条重读现存的回落。**回落的危险性和它的
"友好程度"成正比 —— 越是无声顶上,越没有人会发现顶错了。

### 三、把诊断改成门禁,要先有一句业务决定,不是先有决心

`variant_coverage()` 算了几个版本一直没有阻断,注释里写着理由:
硬阻断会让每一个多色 SPU 立刻无法批准,"那不是修复,是停产"。

这一批敢动它,**不是因为想通了,是因为 §6.5 补上了缺的那句话**
(通用图只进附图位 + 候选入集继承生成任务的颜色)。两件事一起才成立:
有了继承,图才有真实的颜色标签;有了标签,覆盖率才不是恒假的。

**判据:一条算出来但不阻断的指标,升级成门禁之前先问"缺的是执行力还是
一句规则"。**缺规则时强行阻断,最省事的解法是把规则调松 ——
门禁不是被修好,是被静静关掉(与 §3.33 第二条同源)。


## §3.36 A45-batch14-20 并线合并:门禁的覆盖面等于有人付过代价的地方;"没写"不许写成"验不到"

### 一、三处同名撞车,只有一处有门禁 —— 而它有,是因为有人付过代价

两条并行线都自称 `A45-batch14-20`、基线都是 14-19,互不知情,撞了三处:

    迁移号                   两份都是 revision = "0040"
    tools/mutate_batch14_20.py   同名不同内容(20 条 vs 41 条)
    docs/DECISIONS.md        两份 §3.34

**第一处当场变红。**「迁移链单一 head」那条交付门禁里有一句 revision 唯一性
检查 —— 它不是为并线写的,是 A24 那轮 `0021` 撞号返工之后补的。那次的根因
甚至和并线无关(查看时用了 `ls | head -20` 而目录里正好 21 个文件)。
一条为「手滑」写的门禁,在三年后的「并线」上救了一次。

**另外两处没有任何东西会响**,而它们的坏法不同:

    同名文件     后写的覆盖先写的。合并工具会拦(文件已存在),
                 但 `cp -r` 式的合并不会 —— 而那是最常用的合并方式
    同号决策     **永远不会炸。**它只是让「§3.34 说了什么」从此有两个
                 都说得通的答案,而这份文档的用法恰恰是被别处**按编号引用**
                 (代码注释、其他决策、评审回复里到处是「见 §3.x」)

**判据:门禁的覆盖面等于「有人为哪些事付过代价」,不等于「哪些事会出问题」。**
并线合并是一个几乎没有人付过代价的方向 —— 单线开发时"名字唯一"这条前提
自动成立,从来不用写下来,于是也没有人为它写门禁。

本次给决策编号补了一条(交付项 13 → 14)。**同名文件那一条没补**:它需要
一个"哪些文件名必须全仓唯一"的清单,而那个清单本身会漂移 ——
写一条要靠维护清单才准的门禁,和没有门禁的差别只在于它会先绿一阵子。
这一处的处理是把合并方式钉住:合并并行线**不许用会静默覆盖的方式**。

### 二、接线欠账不许写进"验不到什么"

阶段 4 原稿把两件事记在「本批验不到什么」里:

    §6.5 两列的写入路径     "接线点在服务层,而服务层要 sqlalchemy"
    前端方案面板            "跑不了 tsc / Vitest"

两句话字面上都不假,但它们描述的是**环境限制**,而实际情况是**代码没写**。
在 `create_set` 里多写两个 kwarg 不需要**运行** sqlalchemy;tsc 也不会因为
一个组件没人 import 而报错。

这两类的处理方式正好相反 —— 一个等机器,一个等有人写一行代码。混进同一节
的后果是:机器到了,东西照样不工作,而那一节已经被划掉了。

**判据:"验不到"这一节只许放"代码写完了但这台机器证明不了它"。
"代码没写"要另开一节,并且配一条会在还款日变红的守卫。**

第二半不能省。只写进文档的话,还清的那天没有任何东西提醒"顺手把这条记录
删掉",于是文档里留下一条已经不成立的欠账 —— 那正是 §3.33 判过死刑的东西
(一条过期的警报比没有警报更糟)。守卫的写法沿用 §3.34:断言**现状**,
接线那天它自己红,红的信息就是待办。

### 三、并线合并时,每一条"理由"都要重新验一遍

阶段 4 写着共享作用域指纹接不了,因为「§4.6 的 `input_fingerprint` 那批列
还不存在」。合并当天那句话就不成立了 —— 另一条线刚把那一列落了。

结论没变(那一列落在**识别 run 行**上,而 `facts_stale()` 要比的是**一条
确认事实**的指纹,`ProductAttributeValue` 上没有它),但**理由变了**,
而理由才是下一个人照着做事的东西。照着"列不存在"去查的人会发现列就在那儿,
然后顺手把它接上 —— 接到的是 run 行,而一次 run 可以产出多条事实、
一条事实也可以跨多次 run 存活。

**判据:合并两条并行线时,逐条重读被合并方写下的"因为 X 不存在所以没做",
X 很可能正好是对方这一批做的。**代码冲突有工具会喊,**理由冲突没有任何
工具会喊** —— 它表现为一份读起来完全通顺、而前提已经消失的文档。


## §3.37 A45-batch14-21:欠账守卫的还款日要有人替它盯;守卫说出那件事的名字不等于它就是那件事

### 一、判据落在别人身上的守卫,守不住自己

§3.34 立的规矩是「欠账要么写成会变红的守卫,要么就不要写」。那条规矩在
`run_state` / `scope_fingerprint` 上连着八批准确记账,一次没漏 —— 然后在
第九批漏了一笔,而且**没有任何东西响过**。

漏掉的那一笔是 `facts_stale`。守卫写的是:

    for owed in ("facts_stale", "changed_scopes"):
        assert owed not in fingerprint, "...而事实侧还没有指纹列 ——
            要等属性值行也带上指纹列,**那是阶段 4 的事**"

阶段 4 落码了。这件事没做。**守卫仍然是绿的** —— 它断言的是"这两个不许被
登记成接线",而"没登记"这件事仍然为真。

区别在哪里:那八批有效的守卫,判据是**那五列存不存在** —— 列落库那天断言
当场翻转,判据长在它自己身上。这一条的判据是**"到了阶段 4 该做"**,
而守卫看不见交付进度。

一般化:**判据落在别人身上的守卫,守不住自己。**

推论有两个方向,都要:

    自翻转    还款**发生**时会红。这是还款的确认
    还款日    还款**该发生而没发生**时会红。这是还款的催促

八批有效的那两条只有前者,而前者在"没人还"的时候永远是绿的。

### 二、所以还款日必须是一个能被机械判定的数字

落法是 `verify_delivery` 的第 15 条门禁:欠账守卫的 docstring 里写
`还款日:阶段 N`,当前阶段从 `docs/STATUS.md` 的
`<!-- DELIVERY_STAGE: N -->` 读,`N <= 当前阶段` 即逾期。

三处刻意的选择:

**当前阶段不写在脚本里。** 写死的常量没有人会记得改,而 STATUS 顶部那张
阶段清点表是每批都要动的东西。标记就放在它旁边。读不到标记时**红**,
不是静静跳过 —— 静静跳过等于这条门禁自己也成了一笔没有还款日的欠账。

**语义定成"阶段 N 之前必须还清"**,不是"阶段 N 期间还"。前者能被一个数字
判定,后者要判"这个阶段做完了没有" —— 那是一句没有人能机械回答的话。

**只留一种写法。** 第一版还有「还款日:条件式」,意思是"判据长在自己身上,
不用外部盯"。写完之后回头核仓库里那四条欠账,**没有一条用得上它** ——
每一条都自称会自翻转,而每一条同时都有真实的阶段死线。留一个永远不被走的
分支,这条门禁的覆盖就有一格是假的。与「别把明知抓不住的变异列进脚本」
是同一条规矩:**数字要能被兑现,否则它是谎话。**

反验过一次:把 `DELIVERY_STAGE` 推到 5,四条欠账当场被逐条点名。而本批还的
那一笔,还款日写的正是"阶段 4",当前阶段也正是 4 —— **这条门禁要是早一批
存在,它会当场红。**

### 三、一条守卫说出它守的那件事的名字,不等于它就是那件事

这条门禁在写出来的当天被自己咬了两次,而且是**同一个方向的两次**:

    第一次   一条**已还清**的守卫被判成欠着的。仓库的习惯是把欠账守卫翻转
             成正向守卫、并留一段"原来记的是欠账,某批把它收了"的叙述 ——
             那段叙述让标记在**过去时**里命中
    第二次   两条**盯着这条门禁自己**的元守卫被判成欠账。它们要讲清自己
             守的是什么,就必须说出那句标记

第二次值得单独记。§3.26 那条「docstring 是源码的一部分」此前记过两个方向
(文档把断言喂成平凡真、喂成假红),这是第三个:**文档把判据喂成假阳。**
而假阳最省事的消法是把整条门禁删掉 —— 所以它比假绿更危险的地方在于,
它会诱导下一个人做一件正好相反的事。

修法不是把正则写得更巧,是两条:

**一、三种状态各有一句话。** 欠着 / 已还清(`欠账已还`)/ 不相干,
不靠时态去猜。

**二、声明只认 docstring 开头那一段。** 这不是绕开,是把一条本来就存在的
规矩写下来 —— 仓库里四条欠账守卫的声明**全都**在第一行。一般化:
**声明写在开头,解释可以出现在任何地方。**

### 四、一条守卫可以瞎十二批,而它一直是绿的

本批加迁移 0042 时,`test_the_migration_chain_has_exactly_one_head` 红了。
红的信息对,理由错:不是有人建了两个 head,是**它一直数不全**。

它的正则只认 `revision: str = "..."`,而 0041 写的是裸赋值,从来没进过链表。
后果不是当时会红,是当时**碰巧绿** —— 0041 是 head,不出现在任何
`down_revision` 里,少了它照样只有一个 head。断点第一次落在链**中间**
(0042 挂在 0041 下面),它才暴露。

一般化:**一条守卫的正确性不该取决于被它漏掉的东西恰好在哪。**

修法不是放宽正则 —— 下一种写法(`revision: Final = ...`、多行赋值)会让
同一件事再来一次,而且同样表现为"碰巧绿"。加的是一条**覆盖率断言**:
解析出的 revision 条数必须等于 `versions/` 下的文件数。少看见一个文件当场红。

同一件事在 `verify_delivery` 那条同名门禁上没有发生 —— 它的正则两种写法都吃。
**同一条不变式,两个实现,瞎了的是纯层那个。** 收成一份的正确做法是收进
被两边 import 的地方,而纯层不许 import `tools/`;所以这里选的是让它
**自己证明自己看全了**,而不是指望两份实现永远同步。

### 五、`facts_stale` 接线时那几个"不报错的坏法"

判定一个字没动(14-9 穷举验过),本批只接线。而接线的失效方式和判定完全
不同:判定错了会算出一个错的答案,接线错了是**没有人问那个问题**。

最要紧的一条:**事实的指纹不许取自 run 行。**
`ProductAttributeExtraction.input_fingerprint`(0040)记的是"这次 run 吃进去
的是哪批素材",是共享作用域那一个;`ProductAttributeValue.input_fingerprint`
(0042)记的是"这条事实按哪批素材立起来"。拿前者填后者是让门禁"最省事变绿"
的做法,而后果是共享事实与各颜色事实共用同一个哈希 —— **D1 从后门原样回来**,
每一条事实都带着一个看起来很像的 64 位指纹。

第二条:**"不参与"与"比不上"必须分两次问。** `fingerprint_for_owner()` 对
两种情况都返回 None:CHANNEL 不参与,以及参与但那个作用域今天没有证据素材。
后者必须判过期(一个颜色的最后一张证据图被删掉,它的事实最该被重新看一眼),
前者必须不判。合成一个的表现是刊登属性跟着补图集体过期。

第三条:**不变式要长在数据结构上,不长在调用点。** `stale ⊆ confirmed` 这条
如果靠"消费侧记得只在 confirmed 里查",那它的判据又落在别人身上了(第一节
那条规矩的第二次应用)。做法是 `AttributeFacts.stale_confirmed` 取交,
两个消费者都读它,裸的 `stale` 谁也不读。

第四条:**过期字段不许说成"尚无可用值"。** 说错话的代价很具体 ——
那句话的建议是"先跑一次属性识别",而识别产出 CANDIDATE,盖不掉已有的
MANUAL 值(§6.2 规则 4),那个字段照样过期。花了钱,什么都没变。
与 §11 修 `ATTR_EVIDENCE_ONLY` 是同一个形状的第二次:**两种处境的下一步
动作不同,就不能共用一个问题码。**

### 六、不回填,而这一次的代价要说清

0042 不回填,理由和 0040 那条同源但不完全相同。0040 是"回填要重写一个判定";
这一条是**没有人知道答案** —— 一条存量事实当初按哪批素材算出来,
`attribute_evidence` 只记到 run,而 run 的素材快照是 §4.6 还没落的一列。

唯一算得出来的是**今天**的指纹,而把今天的指纹写进去等于宣布"每一条存量
事实都仍然成立" —— 那是这一列要防的那件事的反面。

代价照直说:这一列上线之后**存量已确认事实会集体判过期**,每个必填字段出一条
`ATTR_FACT_STALE`,运营要重新点一次头。§3.1 写着系统尚未正式使用,所以这批行
在真实部署里不存在。真有存量时,正确的做法是先做数据盘点再决定回填口径,
**不是回来把这一条改成"默认不过期"**。

## §3.38 A45-batch14-22:"读得到、判得了、写不进"是一个可以点名的类;验收要有对象

### 一、这一批是靠"逐条对着 PRD 核代码"核出来的,不是靠守卫

上一批交完之后被问了一句「阶段 4 之前的验证下,确保都开发完成」。逐条对着
PRD §13 原文核阶段 1-3 的 18 项交付时,核出一处**任何清单里都没有的欠账**:

    `media_assets.color_variant_id` 落库了(0037)、被两处判定读着
    (`sample_completeness._in_variant` / `scope_fingerprint._element`),
    **而全仓没有任何写入路径。**

它躲过了整整五批。躲得掉的原因不是没人上心,是**它不属于任何一条守卫盯的
类别**:接线门禁盯"函数有没有被调用",这一列不是函数;欠账守卫盯"有人写了
一句话说它欠着",而没有人写过 —— 它是在一次迁移里顺手落下的列,当时的
批次文档只说"归属外键落库了",而那句话是真的。

一般化:**一条门禁只抓得住它被设计来抓的形状。** 覆盖面靠的是有人真的
逐条核过,而不是门禁数量。

### 二、"读得到、判得了、写不进"值得当成一个类来点名

这是第三例。前两例在阶段 4(`shared_opt_in` 与 `angle`),本例更靠上游。
三例的共同形状:

    列在库里、判定读它、判定被穷举验过、而没有任何代码写它

共同的失效方式:**不报错、不抛异常、测试全绿**,而那个维度整个塌掉。
本例塌掉的是颜色维 —— 每张图都算通用图,于是「给 A 色补图」和「补一张
通用图」在指纹上完全一样。

后果要说清:上一批刚接的 `facts_stale` 会一路绿着,**而 AC-21 那条全称
命题在真数据上永远平凡成立**。也就是说上一批报的"AC-21 算得出来了",在
没有这条写入路径的前提下是**虚的**。

判据一般化:**一列新增的、被判定读取的列,落库的那一批必须同时回答
「谁写它」。** 答不出来的话,那一批交付的不是一个维度,是一个恒定值。

### 三、链断在中间与完全没接,表现完全相同

写入链有四层(接口 → asset_service → 影子写 → ingest)。任意一层少一个
参数,运行时表现**一模一样**:那一列恒为 NULL。而每一层单看都是对的。

所以守卫必须逐层钉,不能只钉最里面那一层。本批变异 L1-L5 就是这五种断法,
五条的运行时行为不可区分 —— 而守卫要认得出是哪一层断的,否则修的人只能
从头读一遍。

### 四、按字符串找东西的第 N 次,这次是"命中了另一个正确的位置"

守卫原来写的是:

    assert "color_variant_id=color_variant_id" in _code(ingest)

而 `ingest` 里除了 `MediaAsset(...)` 的构造,还有一句
`_fill_missing_colour(existing, color_variant_id=color_variant_id)`。
把构造那一行整个删掉,断言照样成立(变异 L5)。

§3.26 那条的一个**新方向**:前几次是文档喂假了断言,这次是**另一处正确的
代码**喂假了断言。前者剥 docstring 就能修,后者不能 —— 修法是按 AST 找那次
构造,断言它的 keywords。

同一批里 S6/S8 是同一个形状的另一种:`assert "_first_sku(" in body` 被
`_first_sku(variant.id)` 喂真,而被掐掉的是 `_first_sku(None)`。
判据固定下来:**前缀断言只在那个前缀全仓唯一时才成立**,而"唯一"是一件
会随下一次改动失效的事。要钉两个调用点,就写两条断言。

### 五、改归属不许由一次重复上传触发

`_fill_missing_colour` 与 `_fill_missing_role` 同一条口径:去重命中时
**只补空的,不改已定的**。

理由不是对称性,是代价:§5.3 明写「修改素材颜色归属 A→B → 原颜色、新颜色、
共享三个指纹同时变化」——三批事实同时过期。而运营做的动作是"再传一次同一
张图",看到的结果是"另外两个颜色的事实全部退回待确认"。**两件事在界面上
毫无关联**,查起来要跨三个页面。

要改归属,走显式的改归属动作(它会留下审计与 `changed_fingerprint_scopes`)。

反方向留了一条:**空的可以补**。运营的真实动作顺序常常是"先把图传上来,
回头再分颜色",补不上的话那张图永远算通用图,而该颜色的完整度门禁永远缺图。

### 六、跨 SPU 的颜色 id 要在写之前拦,而"没有 spu_id"一律拒绝

§4.3 那条约束的后果被低估了:**颜色名在 SPU 内唯一,跨 SPU 同名是常态**
(几乎每个款都有黑色)。所以光看一个 UUID 分不出它属于谁,而传错的后果
不是报错:

    这张图进不了本商品那个颜色的完整度门禁(传了图还说缺图)
    它却会让**另一个 SPU** 的颜色事实过期(一个没人动过的款突然
    一批字段回到待确认)

两个现象都不指向"上传时选错了颜色",而且发生在两个不同的页面上。

老建档路径建的商品没有 `spu_id`,给不出"本商品所在 SPU"这个答案 ——
**拒绝而不是放行**。放行等于允许挂到一个查不出关系的颜色上,正是上面
第二条的来路。代价是那批商品暂时不能按颜色上传,而它们本来也没有颜色可选。

### 七、验收要有对象:「可构造」和「构造出来了」是两件事

阶段 1 验收原文是「可构造三颜色九 SKU 的 SPU」。后端的展开规则早就算得出
3 × 3 = 9(纯层有穷举守卫),而样例数据里 **10 个 SPU 各 1 个 SKU** ——
于是这条验收在演示时没有对象,真库用例只能自己造夹具。

自己造的夹具证明的是"代码能建",**不是"这个包交出去之后演示得起来"**。
这两件事的差别在交付时才暴露,而那时没有人记得是谁的责任。

所以样例数据也是交付物,而且它的约束要写成会红的检查,不是写在 README 里。
`verify_sample_data` 新增五条,其中三条钉的就是这件事(三颜色九 SKU 存在、
按颜色的图齐全、各颜色的图内容不同)。

第三条值得单独说:**不同颜色的图内容必须不同**。相同的话去重键挡不住
(product_id 不同),但 §5.3 的指纹会把它们算成同一个元素 —— 于是
"给 A 色补图"和"给 B 色补图"算出同一个指纹变化,**AC-21 在样例上验出来的
结论是假的**。这比"图被去重掉"严重一档:前者少一张图,后者让验收说谎。

### 八、样例数据的播种走建档服务,不另写一条

`seed_sample_data` 里不许出现 `sku_matrix.expand(` / `Product(` /
`ColorVariant(`。另写一条的下场是**样例数据能建出接口建不出来的东西**
(比如一个跳过 SKU 展开规则的 SPU),而演示时看不出来,直到有人照着样例的
形状去调接口。

与 §5.1「唯一取数入口」是同一条规矩在写入侧的样子。

---

## §3.39 A45-batch14-23:一条守卫同时钉不变量与现状时,它对现状的那一半是伪装成成绩的欠账

### 一、这条规矩是被一笔"有守卫、守卫还是绿的"欠账换来的

§3.34 立的是「欠账要么写成会变红的守卫,要么就不要写」;§3.37 补上还款日,
治的是「守卫钉的是没接线这个事实,不钉什么时候该接」。两条都假定了同一件事:
**没有守卫的地方才是盲区。**

§4.8 去重键这一笔证明该假定不成立。它有守卫,守卫叫
`test_dedupe_key_is_product_scoped`,内容是:

    assert 'UniqueConstraint("product_id", "sha256"' in source   # 现状
    assert 'UniqueConstraint("sha256"' not in source             # 不变量

第二句是永久成立的判断:全局唯一会让第二个商品根本存不进去。第一句不是 ——
PRD v3.1 §4.8 明文要求它改成 `UNIQUE(spu_id, COALESCE(color_variant_id,''), sha256)`。

两句写在一起的后果有两层:

**第一层,它读起来像成绩。** 一条绿着的、名字里带"is"的守卫,任何人清点时
都会当成"这件事做好了"。阶段 2 交付第一项因此被记成完整落地六批。

**第二层,更贵:它把 PRD 那条要求变成了会变红的东西。** 谁按 §4.8 落新键,
这条当场红。而红的时候最省事的消法**不是**去改 PRD,是把新键改回旧键 ——
守卫的措辞("跨商品不去重是刻意的")还会替这个动作提供理由。
**一条守卫把待办事项钉成了退化路径。**

### 二、判据:这句断言在需求变更时应该红,还是应该继续绿?

    继续绿  →  不变量。它描述的是任何一版都成立的事
    应该红  →  现状。它描述的是今天恰好长这样

两者混在一组断言里时,整条守卫按"继续绿"被对待,而其中一半不该。

拆开的成本很低(多一个函数名),收益是两件事各自可见:不变量留在原处,
现状那一半挂上 §3.37 的还款日,到期不还会被门禁点名。

### 三、和 §3.38 的关系:两条讲的是同一个盲区的两侧

§3.38 说的是「一列新增的、被判定读取的列,落库那批必须同时回答谁写它」——
那是**没有守卫**的盲区,靠补守卫治。

本条说的是**有守卫**的盲区,靠拆守卫治。共同点是:两者在清点表上都显示
"这一项完了",而清点表是唯一有人定期看的东西。

### 四、顺带记一条:改准过期的理由,和还清欠账一样要紧(§3.33 的再证)

同一轮里核出 14-20 那条面板守卫写着"后者等有人写一行 import"。逐条核路由
之后那句话是错的:面板要 SPU 的 UUID,而全前端没有任何一条路径拿得到它 ——
唯一在出参里给 `spu_id` 的 schema 是方案接口自己(要拿 spu_id 得先有方案,
要列方案得先有 spu_id)。

照着错理由去做的人不会发现自己在做错事,他会发现"无处可写",然后多半把
`spu` 字符串码传进 `spuId`。那时接口 422,而错因指向后端。

**一条过期的理由比没有理由更糟** —— 没有理由的人会去查,有错理由的人不会。

---

## §3.40 A45-batch14-24:一条只在有人手工复核时才会发现的规矩,等于没有规矩

### 一、这条是 §3.38 的执行侧

§3.38 立的是「一列新增的、被判定读取的列,落库的那一批必须同时回答
「谁写它」」。它是对的,但它是**一句话**,而这个仓库为同一个类付过两次代价:

    media_assets.color_variant_id     躲了五批
    §4.8 新去重键                     躲了六批

两次都是人逐条对着 PRD §13 核才核出来的。§3.34 立"欠账要么写成会变红的
守卫,要么就不要写"时说过同一句话,这里只是把它用在规矩本身上:
**一条规矩如果它的执行依赖有人记得去核,那它的失效方式就是有人不记得。**

`tools/audit_column_writers.py` 是它的机械落点。首跑当场找出第三例:
`PublishAttempt.provider_request_id` —— 列注释写着"出事时这是唯一能和
平台对账的东西",接口在返、前端在显示,而全仓没有任何写入点。

### 二、判据只留写入侧,不判读取侧

第一版按"有读无写"筛,读那一半噪声压倒信号:

    checked.note          与 ListingImageSet.note 在同一个文件里同名
    GenerationCandidate   在 providers/base.py 是 dataclass,
                          在 models/generation.py 是模型

按名字撞出来的"读"不是读。而**没人读的无写列同样是问题** ——
它是一列永远为默认值的数据,而 schema 里它看起来像是有内容的。
去掉读那一侧,判据从"两个都要对"变成"一个要对",噪声源少一半。

### 三、审计的失效方式与功能代码不同,所以它要自己被守

功能代码坏了会报错。**审计坏了会安静地少查一点,而报告仍然是绿的。**

第一版当场演示了这件事:全仓四处动态 `setattr(obj, name, ...)`,
第一版把它们判成"整个仓库都判不了",于是 40 个模型全部出局,
输出「**0 列都答得出「谁写它」**」—— 而退出码是 0。

那种绿是最坏的一种绿:它和"全都查过了"在报告上长得一模一样。所以
`test_a45_batch14_24_column_writer_audit.py` 钉了一条**覆盖面地板**,
它挡的是塌方不是波动,所以取整数而不是精确值。

### 四、"台账"不是"白名单":区别只有一条,自净

`LEDGER` 里每一条写清"为什么这一列今天可以没有写入点"。它与白名单的
唯一区别是:**某一列一旦有了写入点,它的条目就失效并被点名,必须删掉。**

少了这一条自净,条目会在还清之后继续留着,而那时它不再是"已知欠账",
是"我们不看这一列"。两者在文件里长得一模一样。

### 五、"能算"比"能读"更值得守 —— 判据要能被注入

第一版把判定写在 `main()` 里,于是"台账条目失效会不会被报出来"只能靠
读源码断言。变异验证当场把它打绿了:判定被搬走之后,读源码的断言安静地
继续为真。

这是 §3.37 那条「判据落在别人身上的守卫,守不住自己」的又一个形状。处理
方式是把审计拆成 `report(ledger=...)` + `main()`,守卫注入一个**已知有
写入点**的列,直接看它报不报。

同一轮里 R1 变异也暴露了一条:守卫断言"`verify_delivery.py` 里出现过
审计脚本名",而把它从 `CHECKS` 表上摘掉之后那个名字仍在函数体里 ——
守卫照样绿。改成解析 `CHECKS` 表本身。**断言要落在起作用的那个结构上,
不是落在提到它的那段文字上。**

## §3.41 A45-batch14-26:决定可以由写的人做,但它必须以决定的身份出现

14-23 立过一条规矩,本批的全部工作都绕着它转:

> 分界线是**缺代码**还是**缺一个决定**。前者今天这台机器上写完就能验,
> 后者写得出来也不该由写的人拍板 —— 替人做完的决定不会有人复核,
> 它会以"已完成"的身份进下一轮清点。

那条规矩是对的,而它只有一半。本批拿到显式授权("如果有疑问,按照你的
决定来")之后,补上另一半:

**决定可以由写的人做。它不可以以实现的身份混进去。**

区别是可操作的,不是态度问题。一个决定以决定的身份出现,意味着四件事:

1. 它单独成条,写清**备选方案**——不是"我选了 A",是"A 和 B,选 A";
2. 写清**选错时会怎样**,而且要具体到可观察的现象,不是"可能有问题";
3. 配一条守卫,在它被**悄悄撤回**时变红;
4. 那条守卫钉的是决定的**前提**,不是决定的实现细节。

第 4 条是本批学到的新东西,单独说。

### 一、守卫要钉前提,不是钉实现

§4.8 那个去重键落成了两条局部唯一索引。守卫的第一个想法是断言"有两条
唯一索引、谓词分别是这两句"。那是钉实现。

实际该钉的是**这个落法成立的前提**:两个谓词严格互补,任何一行恰好落在
一条键下。因为退化回去最省事的路径不是删掉一条索引(太显眼),是把某一条
的谓词**改窄**——比如给兜底那条加 `AND legacy_kind IS NULL`。那之后:

    仍然有两条唯一索引 ✓  `spu_id` 仍在 ✓  PRD 那个键仍在 ✓
    而中间裂开一批**两条都不管的行**,它们彻底失去去重,且没有地方会报。

钉实现的守卫在这个变异下是绿的。钉前提的守卫会红。变异 K3 / K4 是这条的
两个方向。

这与 §3.31「点名做法的守卫会因为进步而变红」是一体两面:钉实现细节的守卫
既漏得掉真的退化,又会被无害的重构打红——两种失效都会训练人去改守卫。

### 二、"看起来落了、实际覆盖零行"是一个可以点名的类

§4.8 按字面落是 `UNIQUE(spu_id, COALESCE(color_variant_id,''), sha256)`。
`spu_id` 可空,而唯一索引里 `NULL <> NULL`,于是所有没有 SPU 的行彼此
永不相等——**约束在,只是不拦任何东西**。

14-24 记过同型的一次:审计判不了的模型全部出局,输出「0 列都答得出」,
**而退出码是 0**。那种绿和"全都查过了"在报告上长得一模一样。

这一类的共同形状是:**一个防御机制的存在与它的覆盖面是两件事,而清点
通常只数前者。** 处理方式也一样——断言覆盖面本身,而不是断言机制存在。

### 三、绕开一条老结论,比推翻它便宜

`evidence_class` 落存储列要回填,而迁移不许 import `app.*`,于是回填只能
把四条派生规则冻成 SQL CASE ——那是 D3 描述的第二个判定点。0040 逐条
权衡过并拒绝了,14-23 复述过一次。

本批没有推翻那个结论。它成立,今天仍然成立。**换的是问题的形状**:
回填不一定要在迁移里做。`app/scripts/backfill_media_assets.py` 早就确立
了"大回填走脚本"的先例,而脚本 import 得到派生函数。

一条老结论挡路时,先问它挡的是不是我以为的那件事。0040 拒绝的是
"在迁移里重写一份规则",不是"回填"。

### 四、"守卫太严"这个念头本身是一个信号

第一版回填脚本被单写入点守卫拦下,第一反应是"守卫太严:调派生函数不该
算违规"。**它不严。** 那条守卫禁的是"路由层/脚本直写",而经过一个本地
变量之后,在赋值点上看不出那个值是派生来的还是谁手搓的一段 if。

值得记住的不是这次判断错了,是**这个念头出现时的处境**:守卫红了、
而我手上有一个"它误报了"的解释。那个处境下改守卫和撤回一个真的违规,
在 diff 上长得一模一样,而前者永远更省事。

所以定一条:**守卫红的时候,先假设它对。** 要放宽它,理由必须写成
"它钉的那件事本身不该钉",而不是"我这次是例外"。本批最后没有动那条守卫,
改的是代码。

### 五、锚点靠相邻性时,插一段代码就能让一条变异什么都不验

本批往 `media/service.py` 里插了一段派生调用,`audit_anchors` 当场报两条:

- 14-11 W3 的锚点是 `status = MediaStatus.QUARANTINED\n\n    asset = MediaAsset(`
  ——靠**两行相邻**。插在中间之后它一次都匹配不上。
- 14-16 W5 的锚点是两行 kwargs,而派生调用按同样两行传溯源,从此**不唯一**。

两条的修法不同,分界线值得记住:

    W3 改代码(把派生块移到冲突判定之前,它本来就不依赖 status),
    W5 改锚点(延长一行带上收尾的 `)`,它验的东西一字未改)。

判据是:**锚点断掉时,先问代码的新位置有没有独立理由。** 有就改锚点,
没有就改代码。W3 的派生块放哪儿都行,那就让它避开;W5 的两处传参各有
各的理由,那就把锚点说清楚。

一条什么都没验的变异比没有变异更糟(14-22 S3)。而这两条在本批之后
都会**安静地**什么都不验——`audit_anchors` 是唯一会说话的地方。

---

## §3.42 A45-batch15-merged:一句说缺口已关的话,比一句过期的话贵得多;过程文档删掉的同时,结论要有去处

本批不改任何业务行为。交付的是**四处失实陈述的订正 + 两道让这一类以后会变红的门禁 + docs 目录的一次清账**。

### 一、这四处是同一类,而那一类不是「文档过期」

    `create_product` 注释      「CSV 导入从此要求 SPU 先存在。这是真实的行为变更」
    `README.md`                「make check 离线全部门禁,不需要网络」
    `audit_anchors.py`         「今天只剩 mutate_contract_tests.py 用 CASES 形状」
    四份纯测试 docstring        「真库层的闭环在 tests/test_api_*.py」

逐条核过之后,四句话**全部在宣告一件没有发生的事**,而且宣告方向一致:
**都说某个缺口是关着的。**

- `import_products` 根本不调 `create_product`,它直接 `Product(**row)`——
  不解析 spu 码、不抄 audience、不过 C-03 闸。CSV 那条路今天照旧写 `spu_id = NULL`。
- Makefile 里 `check: check-offline fe-check`,而 `fe-check` 第一步是 `npm ci`。
  离线跑得动的是 `check-offline`。用例数写的 `1270+` 停在很早以前(今天 2459)。
- `mutate_contract_tests.py` 在 batch14-3 就退役了,全仓今天**零个** CASES 表。
- `tests/test_api_attributes.py` / `test_api_media.py` / `test_api_prompts.py` /
  `test_variant_key_db.py` —— 四个文件**树里不存在**。
  (说"从来不存在"我证不了 —— 这棵树没带 git 历史。**能证的是现在指不到**,
  而那已经足够:一句指不到的"那边已覆盖"照样让人不去看那边。)

**分界线值得记住:过期的文档让人多走弯路,这一类文档让人不走。**
读到「那边已经覆盖了」的人不会再去看那边有没有东西;读到「不认它就漏 18 条锚点」
的人不会去删那段死分支。一句失实的理由能把一段没有用户的代码一直养着。

`README` 那条还多一层:照旧文案在没网的机器上敲 `make check`,
得到的是一个装依赖失败的红 —— 于是人会怀疑门禁本身坏了,而不是文档写错了。

### 二、守卫钉一致性,不钉现状

`tests/pure/test_a45_batch16_doc_truth.py` 五条,每一条都能同时容纳
「缺口开着」与「缺口关上」,只拒绝**两份真相对不上**。理由是 §3.31 那条:
钉现状的守卫会因为进步而变红,而那种红会训练人去改守卫。

    CSV 那条    实现走不走 SPU 闸  ==  注释里说走不走
    清册那条    正文里悬空的章节集合 == PRD 开头清册列的集合
    README 那条 Makefile 里 check 依不依赖 fe-check == README 敢不敢说它离线

所以 CSV 那笔债该怎么还,守卫**不表态**。怎么还是一个决定(§3.41):
可以让 SPU 缺席的行计入 `errors`,也可以按 CSV 里的 spu 码自动建最简 SPU ——
两种对运营的可感知行为不一样,要产品侧拍板。守卫只保证:**做了决定的那天,
那几句话得跟着改。**

### 三、变异验证逼出来的一个数:驳斥窗口 = 0 行

本批修法的一部分是**把假话原样引出来再驳掉**(「这里原来写的是……那句话是错的」)。
于是守卫必须放行被引述的假话,否则它会逼人删掉真话 —— 一道让人删真话的门禁比没有门禁贵。

放行靠"驳斥标记在封闭窗口内"。窗口半径最初写 2 行,**变异 M1(把假注释改回去)
不响** —— 那段注释里本来就有一句「原来写的是……那句话是错的」,它在 ±2 行内
**替另一句假话作了担保**。收到 0 行(必须同一行)之后 M1 变红。

    一句驳斥只能管它自己引的那句,管不了邻居。

六条变异现在全红:改回假注释 / 清册删行 / README 说成离线 / PRD 改回旧文件名 /
正文新增一处沿用 v3.0 / `import_products` docstring 退回原版。

### 四、`audit_doc_refs.py`:第 14 道门禁,以及它为什么分两档

`verify_delivery` 盯"门禁有没有人调用",`audit_anchors` 盯"变异锚点还在不在" ——
**没有任何一道门禁盯"这句话指的东西还在不在"**。这份补那个空档。

分两档,只有一档拦:

    ERROR   活文档 + 活代码。指错就是把人送错地方
    WARN    历史台账,只有四份:STATUS / DECISIONS / REVIEW / HANDOVER

分档不是宽严之别,是这两类文本的语义不同。台账是追加写的历史记录:
「batch14-21 的合入说明写在 MERGE-A45-BATCH14-21-FACTS-STALE.md」这句话
在写下那天是真的,那份文档后来按规矩删了,**这句话仍然是那天的事实**。
把它判成错误,只会逼人去改历史,或者把台账整个加进白名单 —— 两条路都比现状糟。

同样的封闭窗口口径也用在这里:带退役标记(`退役` / `已删` / `原来是` / `并入` …)
的引用放行,但标记要落在同一行或紧邻上下一行。**标记要挨着它说的那个名字,
不能挨着这份文件** —— 窗口开宽了,一句文件级免责声明就能豁免全文。

### 五、docs 清账:删 42 份 / 59 份,但删之前先问三个问题

判据用仓库自己的规矩(CLAUDE.md:「过程文档不留档 —— 结论进 docs/DECISIONS.md」)。
**这条规矩的后半句和前半句一样重要**,所以删之前逐份查了三件事:

    一、有没有门禁 `read_text` 它?      —— 一份都没有(唯一被读的是 STATUS.md,3 处)
    二、有没有活代码的 docstring 点名它? —— 有 5 份
    三、它的结论在 DECISIONS.md 里吗?    —— 那 5 份对应的批次查不到

**第二、三条一起成立的那 5 份予以保留。** 删掉它们等于用一个悬空引用
换掉另一个 —— 而那正是本批要修的缺陷。上一轮的清单把它们划进了删除范围,
这一轮撤回:

    MERGE-A45-BATCH14-20-STAGE4-IMAGE-PRODUCTION.md   ← mutate_batch14_20_stage4.py:24
    REVIEW-A44-BATCH7.md                              ← test_a44_batch7_fixes.py:4
    REVIEW-A45-BATCH12-2.md                           ← test_a45_batch12_2_fixes.py:24
    REVIEW-A45-BATCH12-3.md                           ← test_a45_batch12_3_fixes.py:26
    REVIEW-A45-BATCH14-3.md                           ← test_a45_batch14_4_fixes.py:9

另外三份按各自的自述保留:`REVIEW-A28-TRACKING.md`(自述了为什么不能随 HANDOVER
被整份替换)、`REVIEW-A44-A45-MERGED.md`(还有 3 条未处理)、`OPS-REVIEW.md`
(25 处代码注释锚定它的 P1/P4 编号)。

**解锁条件写明**:那 5 份要能删,得先把它们各自被点名的那一节内容搬进
`DECISIONS.md`,再改 docstring 指过来。本批没做,因为搬的是行为验证记录,
搬错了比不搬贵。

### 六、归档台账:42 份文档的结论今天在哪

| 已删文档 | 主题 | 结论今天在哪 |
|---|---|---|
| `MERGE-A42.md` | 四个补丁并成一棵树 | `STATUS.md` 同批条目 |
| `MERGE-A44-BATCH8-PATCH.md` | `a44-batch8-fixes.patch` → batch9 树 | `STATUS.md` 同批条目 |
| `MERGE-A45-BATCH13-3.md` | A45-batch13-3 → batch14 | `DECISIONS.md` §3.25 |
| `MERGE-A45-BATCH14-10-SAMPLE-COMPLETENESS.md` | 样品完整度门禁的单点派生(§6.2) | `STATUS.md` 同批条目 |
| `MERGE-A45-BATCH14-11-GATES.md` | 第一台装得齐后端依赖的机器,把欠的门禁跑了 | `DECISIONS.md` §3.27 |
| `MERGE-A45-BATCH14-11-QUEUE-AND-PROVENANCE.md` | §11 的两条新场景(确认队列口径 + AI 图伪装拦截) | `DECISIONS.md` §3.27 |
| `MERGE-A45-BATCH14-12-RUN-STATE.md` | 识别 run 的终态、取消与幂等键(§4.6 / §9.2) | `DECISIONS.md` §3.28 |
| `MERGE-A45-BATCH14-13-SCOPE-OUTCOME.md` | §11 第一行 —— 按作用域的失败归集 | `DECISIONS.md` §3.29 |
| `MERGE-A45-BATCH14-14-SOURCE-WINDOWS.md` | 守卫的窗口必须是封闭的 | `DECISIONS.md` §3.30 |
| `MERGE-A45-BATCH14-15-ATTRIBUTION.md` | 素材归属外键落库,四批欠账收其二 | `STATUS.md` 同批条目 |
| `MERGE-A45-BATCH14-16-PROVENANCE.md` | 溯源列落库,候选落盘写入(§4.8) | `STATUS.md` 同批条目 |
| `MERGE-A45-BATCH14-17-URL-FILTERS.md` | 筛选状态住进 URL,阶段 0 最后一条代码项收口(GAP-033) | `DECISIONS.md` §3.31 |
| `MERGE-A45-BATCH14-18-PROVIDER-USAGE.md` | 计费量问厂商要,并记下这个数是谁说的 | `DECISIONS.md` §3.32 |
| `MERGE-A45-BATCH14-19-EVIDENCE-QUERY.md` | §5.1 白名单成为取数入口 | `DECISIONS.md` §3.33 |
| `MERGE-A45-BATCH14-20-RUN-IDENTITY.md` | 识别 run 的身份落库与 §9.2 幂等接线 | `DECISIONS.md` §3.34 |
| `MERGE-A45-BATCH14-21-FACTS-STALE.md` | `facts_stale` 派生接线 + 欠账还款日门禁 | `DECISIONS.md` §3.37 |
| `MERGE-A45-BATCH14-22-COLOUR-ATTRIBUTION.md` | 素材颜色归属的写入路径 + 新结构样例数据 | `DECISIONS.md` §3.38 |
| `MERGE-A45-BATCH14-23-ITEM-COLUMNS-AND-DEDUPE.md` | §6.5 两列的写入路径 + §4.8 去重键拆账 | `DECISIONS.md` §3.39 |
| `MERGE-A45-BATCH14-24-COLUMN-WRITER-AUDIT.md` | 「每一列都答得出谁写它」——把 §3.38 变成会红的门禁 | `DECISIONS.md` §3.40 |
| `MERGE-A45-BATCH14-25-WRITE-EVIDENCE-AUDIT.md` | 审计自己的证据链——`height` 那条假阳性不是孤例,是三个缺陷的其中一个出口 | `STATUS.md` 同批条目 |
| `MERGE-A45-BATCH14-26-DECISIONS.md` | 三笔决定类欠账 + 老建档路径切 SPU | `DECISIONS.md` §3.41 |
| `MERGE-A45-BATCH14-7.md` | 两份 patch → batch14-6 | `STATUS.md` 同批条目 |
| `MERGE-A45-BATCH14-8-EVIDENCE-CLASS.md` | 阶段 2 的 `evidence_class` 单点派生 | `STATUS.md` 同批条目 |
| `MERGE-A45-BATCH14-9-SCOPE-FINGERPRINT.md` | 双作用域样品指纹与 `facts_stale` 派生 | `STATUS.md` 同批条目 |
| `REVIEW-A43-RESPONSE.md` | 对 A42-merged 复查意见的逐条答复 | `DECISIONS.md` §3.15 |
| `REVIEW-A44-BATCH3.md` | A44 第三批修复说明（A-01 ～ A-06 · A-19 · A-20 · A-25 … | `STATUS.md` 同批条目 |
| `REVIEW-A44-BATCH4.md` | A44 第四批修复说明（十条 P1/P2/P3 毛边） | `STATUS.md` 同批条目 |
| `REVIEW-A44-BATCH5.md` | A44 第五批修复说明（C-13 · B-07 · 十一条毛边） | `STATUS.md` 同批条目 |
| `REVIEW-A44-BATCH6.md` | A44 第六批修复说明（十条） | `STATUS.md` 同批条目 |
| `REVIEW-A44-FINAL-verified-r3.md` | swimwear-imagegen A44 评审核验报告（最终版 · 修订三 —— 七批修… | `DECISIONS.md` §3.18 |
| `REVIEW-A44-RESPONSE.md` | 稳定 variant ID | `DECISIONS.md` §3.18 |
| `REVIEW-A45-BATCH12-4-RESPONSE.md` | 修 batch12-3 回归报告里的五条 | `STATUS.md` 同批条目 |
| `REVIEW-A45-BATCH12-4-SELFCHECK.md` | 复核上一轮的五处修改 | `STATUS.md` 同批条目 |
| `REVIEW-A45-BATCH12-5-RESPONSE.md` | A45 batch12-5 回归评审 —— 修复报告 | `STATUS.md` 同批条目 |
| `REVIEW-A45-BATCH12.md` | 人工审核与自动流水线的交叉口，以及批量付费的自动重跑上限 | `STATUS.md` 同批条目 |
| `REVIEW-A45-BATCH13-2.md` | A45-batch13-2(阶段 1 第一批 + 走读修复) | `DECISIONS.md` §3.23 |
| `REVIEW-A45-BATCH14-4.md` | A45-batch14-4(把退役 `mutate_contract_tests.py` … | `STATUS.md` 同批条目 |
| `REVIEW-A45-BATCH14-5.md` | A45-batch14-5(收掉 batch14-4 §五的三条遗留) | `STATUS.md` 同批条目 |
| `REVIEW-A45-BATCH14-6.md` | A45-batch14-6(阶段 P0:1/6 -> 4/6,真库 pytest 第一次跑… | `STATUS.md` 同批条目 |
| `REVIEW-A45-BATCH14.md` | A45-batch14(阶段 3 第一批 + 合入 batch13-3 之后的走读) | `DECISIONS.md` §3.24 |
| `REVIEW-A45-BATCH8.md` | A45 第八批修复说明（第一批 4 条 + 第二批 20 条 + **五条结构性门禁**） | `STATUS.md` 同批条目 |
| `REVIEW-A45-BATCH9.md` | Celery 任务的故障恢复语义（自查发现，不在任何一份评审里） | `STATUS.md` 同批条目 |
「`STATUS.md` 同批条目」不是敷衍:`STATUS.md` 是这个仓库的历史台账,
每一批都有自己那一段,合入说明的结论在那里能查到。`STATUS.md` 里指向已删文档的引用共 **23 份 / 26 处**,因此不是断头路 ——
顺着批号能走到这张表,再走到 STATUS 的对应段。

### 七、PRD 的两处

**文件名与版本不符**:文件叫 `..._prd_v3_1.md`,正文第 3 行写 v3.1.1。
差一个小版本号,而这个仓库里「v3.1 说的」和「v3.1.1 说的」是两回事
(§0.4 专门讲两者差别)。已改名为 `..._prd_v3_1_1.md`,守卫钉住文件名与自报版本一致。

**v3.0 原文缺失**:v3.1 用增量写法,凡 v3.0 写对的地方写「沿用 v3.0」而不重述。
这在 v3.0 拿得到时合理,拿不到时**它把 21 个章节号变成了指向空地的引用**(实测 21 处)。
原文补不回来 —— 但可以让这笔债不再是藏在正文里的一行话。PRD 开头加了一张
逐条清册,守卫钉「清册列的章节集合 == 正文里真正悬空的章节集合」。

其中 §14.1 最贵:AC-01~AC-20 是 §14.3 人工测试准入的判据,**判据本身拿不到**。
这与 P0 那 5 项的「未验证」是两种东西 —— P0 缺的是机器,补一台就能推进;
AC-01~20 缺的是判据,补一百台也没用。**AC-02 / AC-06 / AC-07 / AC-20 连
阶段归属都反推不出来**:§13 四条阶段验收行的并集只覆盖 16 条。

### 八、自查发现的四条,以及第一条为什么最值得记

订正完四处失实陈述之后回头审自己写进去的话,抓到四条。**前三条是同一个动作
的两面**:一边指责别人把易变的事实写进散文,一边自己在做同一件事。

**一、README 里那个用例数。** 订正时我写了当时的真值 2459,而本批新增 5 条
守卫当场把它作废成 2464。改法不是改成 2464 —— 下一批照样烂。
**根因是把一个每批都在变的事实复制进了散文。** 真值由 `make test-pure`
自己打印,README 里不留第二份。这一条比它订正的那一条更值得记:
**订正一句失实的话时,最容易犯的错就是用另一句将来会失实的话替换它。**

**二、「四个文件从来没有存在过」。** 我只验证了它们**现在**不存在 ——
这棵树不带 git 历史,"从来"我证不了。已改成"树里不存在"。能证的那一半
已经足够:一句指不到的"那边已覆盖"照样让人不去看那边。

**三、清册写「20 个章节」,实测 21 个章节号。** §7.4 与 §7.5 在正文里合写
一个标题,按标题数是 20,按章节号是 21。守卫比对的是章节号集合,
所以数字必须按章节号写。

**四、`_LEDGERS` 里有三份是我防御性加的,没验过需不需要。**
实测 `AC-VERIFICATION.md` 与 `REVIEW-A28-TRACKING.md` 一处 ERROR 都没有,
`REVIEW-A44-A45-MERGED.md` 只有一处(指向已删的 BATCH8),改成过去时后也干净。
三份全部移出,宽松档从 6 份收到 4 份。**这一条是我自己 docstring 里
写的那句话的现场演示:没有理由的豁免会长大,而我就是那个往里加的人。**

## §3.43 A45-batch14-28:「上游算对了、没人接最后一跳」是一种测试看不见的缺陷

### 形状

本批修了三处同一个形状的东西:

    variant_gate_roles   14-15 算好并递到 MaterialFacts,_evaluate_material 一次没读
    facts_stale          14-21 起逐行在回,frontend/src/ 零引用
    color_variant_id     上传接口收得下,前端表单不发

三处都不是"忘了写代码"。三处的**判定都验过、接线也都在**,断的是中间那一跳。
而这一跳恰好落在两侧测试的**缝**里:上游测判定,下游测渲染,没有人测
「上游的结论有没有到达下游」。

于是它比一个没写的功能更贵 —— 一个没写的功能是空的,谁都看得见;
这个**看起来是一道已经生效的门禁**。

### 为什么清点数不出来

上一版清点把 `facts_stale` 记成「完整落地」。那不算错记:按"这个字段算得对不对"
问,它确实完整。清点的口径是**交付项**,而交付项的完成定义里没有"有人消费它"。

改法不是改清点表的口径 —— 是让每一条跨层的结论都有一条**指名消费点**的守卫:

    test_the_stale_flag_has_a_consumer_and_it_fails_closed
    test_the_upload_form_actually_sends_the_colour
    test_the_variant_buckets_actually_produce_a_verdict

它们读的是对面那一侧的源码。丑,但它是唯一能跨过那道缝的东西。

### 前端类型门禁从未执行,是这三条能活这么久的直接原因

`make check-offline` 里的前端项只有 `tools/syntax-check.mjs` —— **只解析不判型**。
它一直报 86/86,而 `npm run typecheck` 第一次跑就是 3 条错误、Vitest 1 条失败。

其中一条是「后退保留」用例:它用 `MemoryRouter` 却调 `window.history.back()`,
两个后退栈没有连接,**断言恒真**。它是全仓唯一守 §8.2 的用例,两个方向都绿。

一条从来没有执行过的门禁,和一条不存在的门禁没有区别 —— 但它更糟:
它在清点表上占着一行,让人以为那件事有人看着。

## §3.44 A45-batch14-28:切身份之前,先把"静默"拆掉,而不是赌顺序装对了

### 这一批拿到的授权是"按你的理解来",而理解是这样

`owner_id` 切 UUID 的前提(§3.22)是**products 行带上 `color_variant_id` 的值**,
而那要真库回填。授权给到了,库还是验不了 —— 那么"顺序装对了"这件事,
在交出代码的那一刻是**赌**,不是保证。

赌的赔率由后果的形状决定。而这件事的后果形状是最坏的一种:
`owner_for()` 拿到空坐标 → 抛 `AttributeValueError` → `apply_evidence`
`logger.warning + continue` → **整个 VARIANT 层静默消失**,而界面显示
「识别完成」加一个空主色。

所以第一个动作不是切换,是**把那个 `except` 拆开**:

    AttributeValueError   值不合契约 —— 继续吞。模型偶尔吐个不在枚举里的值是
                          常态,炸掉整次识别意味着另外 7 个正常字段也写不进去
    AttributeOwnerError   坐标算不出来 —— 不吞。抛出去,整次识别当场失败并说原因

拆完之后,顺序装错**仍然是错的**,但它的表现从"少了一批字段没人知道"
变成"这次识别失败并指着原因"。**这个类型不让错误变对,只让它不再无声。**

代价诚实地记在这里:一批没回填的存量商品从此跑不了识别。那是对的 ——
它们本来就写不出读得回来的颜色属性,跑完只会留下一批查不到的行。

### 迁移的姿态跟着同一条:回填不上就 `raise`,不跳过

0046 拦的是**名下真的挂着 VARIANT 属性或变体图片标签**的商品。刚导进来
没跑过识别的不拦 —— 它们没有可丢的东西,拦它只会让迁移无法执行。

跳过它们的表现是:迁移绿了、系统起来了,而那批商品的颜色属性从此读不到。
**`raise` 比一批查不到的数据便宜得多。**

### 退役一个 hack,要说清它是"被删掉"还是"被抛弃"

命名空间 `<len>:<spu>/<key>` 的存在理由是:变体 id 取值是**颜色名**,
而那张表的唯一索引里没有 SPU。owner_id 切成 `color_variants.id` 之后
这个理由消失了 —— UUID 全局唯一,跨 SPU 同名撞不上。

**hack 不是被删掉,是被它的存在理由抛弃了。** 这个区别不是修辞:
前者随时会有人"顺手加回来",后者要先把 UUID 换回颜色名。
所以铸造函数留成一个 `raise NotImplementedError` 加一段说明,而不是删除 ——
照着旧文档调用的人应该拿到一句话,不是一个 `AttributeError`。

解析函数(`split_variant_owner_id`)留着,因为库里 0046 之前写下的行还是
那个形状。**铸造退役、解析不退役**,这两件事必须分开说。

### 切身份最深的伤在下游,而它不报错

`fingerprint_scope()` 靠解析命名空间取变体段。切成裸 UUID 之后
`split_variant_owner_id` 对每一行返回 None,于是**每一条颜色事实都落回
共享作用域** —— 给 A 色补一张图会 stale 掉 B 色的事实,也就是 D1
从后门原样回来,而 AC-21 正是为它写的。

它**不会报错**:返回值合法、类型正确、`facts_stale` 照常算。唯一的症状是
一批本不该过期的事实开始过期,而那看起来像"补图了,该复核一下" —— 完全说得通。

这类伤只有一种办法找得到:切完之后**把每一个读 owner_id 的地方数一遍**,
而不是等测试告诉你。本批是守卫先红的,那是运气;变异 R8b 把这份运气固定下来。

### 守卫钉着已退役的形状时,改指新不变式,不删

七条红的守卫,没有一条被删。它们守的不变式(同色跨 SPU 不许共用一行、
存量 owner_id 要拆得开、变体作用域要取得出)一条都没变,变的是实现。

顺带补上一个一直存在的弱点:那几条 round-trip 用例拿**铸造函数的返回值**
当期望值。那本来就是弱断言 —— 铸造和解析一起漂的话它照样绿,而库里
真正躺着的字节谁都读不了。铸造退役之后一律改钉**字面量**。

**一条守卫因为实现变了而变红,是它在工作;因此删掉它,是把它的工作成果扔掉。**

## §3.45 A45-batch14-28 补丁审核:身份迁移必须同时守住作用域与唯一答案

0046 的大方向是对的：商品外键、属性 owner 与图片标签必须在同一迁移动作里
切到 UUID。但补丁审核发现，“同一事务”只保证一起成功或失败，**不保证改的是
正确的行**。因此再加两条迁移不变式：

1. 旧图片 `variant_id` 只在 SPU 内唯一。任何 upgrade/downgrade 改写都必须经
   `listing_image_items -> listing_image_sets.spu` 收窄；按裸 `variant_id` 更新全表
   会把另一个 SPU 的同名颜色改成当前 SPU 的 UUID。
2. 回填必须有唯一答案。`variant_code` / `working_name` 都可能命中候选；候选超过
   一个时迁移必须停止并要求先清理数据，不能依赖 PostgreSQL 的未指定选择顺序。

真库用例用两个都含 `BLK` 的 SPU 验证 upgrade 和 downgrade，并另造同 SPU 两个
相同 `working_name` 的颜色，确认 0046 明确失败。迁移测试清库也改成重建专用测试库
的 `public` schema：Alembic 显式外键名与 ORM 推导名可能不同，拿
`Base.metadata.drop_all()` 清 Alembic 建出的库会在 teardown 中途失败。

本次真库还证伪了两个“判定正确但取数作用域错误”的实现：共享事实指纹与工作台
颜色素材门禁都只查当前 `product_id`，而同一 SPU 的颜色通常分布在不同 SKU 行。
两处现统一按 `spu_id` 取整个 SPU；仅对没有 `spu_id` 的历史行保留按商品隔离。
这不是性能优化，而是 AC-21 与“声明颜色都有必需素材”成立的前提。

最后，付费范围参数必须区分 `None` 与 `[]`：前者表示未限定，后者表示调用方明确
选择了空集合。把两者都写成 `if only_variant_ids:` 会让一次空重试扩大成全 SPU，
因此颜色范围判断必须用 `is not None`，且在 Provider 调用前拒绝空证据。

## §3.46 A45-batch17-1 补丁审核:增量补丁的前置条件也是合入契约

`patch/A45-batch17-1-offline-blind-spots.patch` 不是独立补丁。它的交接明确要求
先应用 A45-batch17，而当前树缺少该批引入的 `GenerationPlansPage`、宿主页组件
用例、`mutate_batch17.py` 与阶段 4 宿主页交接文档。更直接的证据是：补丁准备
新增的 §3.44 在当前决策日志中已经属于“身份切 UUID”，整包应用不仅冲突，
还会让同一个编号同时回答两个问题。

因此这类补丁的审核单位不能是“哪些 hunk 能勉强贴上”，而必须是语义：

1. 当前基线已经用 `narrowOneOf` 修掉两处裸 `number` 的 TS2322，但白名单与
   fallback 仍要在调用点再传一遍。移植来包更强的设计：`oneOfParam` 自带
   `narrow()`，URL 读取与控件回调共用同一个集合和默认值。纯层守卫扫描真实
   `page_size` 写入，U14 反向变异把调用点换成 `as`，证明 tsc 绿时守卫会红。
2. “失败时清空数据，却把 emptyText 写成业务空集”与宿主页是否存在无关。
   该判据独立移植，并把当前三处存量欠账放进只减不增、反向自净的台账。
3. `GenerationPlansPage` 空态与 QueryClient 包装器只存在于前置批次，当前树没有
   可修的对象，也没有可运行的对应组件用例。为它们新造占位文件会把“前置功能
   不存在”伪装成“修复已合入”，所以延期到宿主页真实落地时重新审核。
4. 来包的交接正文和 §3.44 不复制进活文档。前者依赖不存在的历史，后者编号
   冲突；结论只在本节与 `STATUS.md` 留一份当前事实。

这次选择保留 `patch/` 中的原始来包作为审核输入，不把它当运行时代码，也不以
“`git apply` 成功”作为验收。补丁合入完成的定义是：被移植的不变量有正反两面
验证，未移植项仍在状态文档中如实保持未落地。

## §3.47 A45-batch17-2 评审修复:落库端的归属校验是一条独立的防线,幂等键顶不了它

本批修的是 1442 包审阅报告里的 P2-1 与四条文档失实。P2-1 的形状是:
`publish_service._save()` 取三行、无条件写三行,于是一次租约过期后被重领、
随后**迟到返回**的调用可以把较新的结论盖回去 ——
DONE → DEAD、SUCCEEDED → UNKNOWN、LISTED → SUBMIT_RESULT_UNKNOWN。

### 一、幂等三道防线论证的是另一件事

`§3.19` 解释过发布与批次两条链路的幂等机制不同,而那段论证覆盖的是
**「重试不会重复创建」**。它今天仍然成立:幂等键、唯一索引、平台 409 三道
都在,迟到的那次调用不会让平台上多出一件商品。

但它不覆盖**「结果不会被回写」**。这两件事在代码里由完全不同的东西保证:

    不重复创建   由**发出去的报文**保证(同一把键)
    不被回写     由**落库时的 WHERE**保证(执行权还在不在自己手上)

第一条做对了不能推出第二条。P2-1 之所以能在两条链路里活下来一条、
被修掉一条(批次 A43 / BLOCK-02),原因就是当时论证的是第一条,
而第二条没有单独的名字。本节给它一个:**落库端的归属校验**。

### 二、评审给的"最小修法"在本仓是错的,而它错的方向更贵

报告建议的退一步方案是在落库的 UPDATE 里带 `lease_until == 领取值`。
本仓不行:`_renew_lease()` 在发请求**之前**就把库里的 `lease_until` 改成了
`now + LEASE_SECONDS`(BLOCK-16 关的是另一个窗口),而 `_Call.lease_until` 不动。
真按它写,**正常路径也会 rowcount = 0** —— 每一次投递的结论都落不了库。

这一条值得单独记,因为两种坏法的可见度差了一个数量级:

    缺口留着     只在"调用耗时越过 LEASE_SECONDS"的那一次露馅,而那次没人在看
    判据写反     每一次都露馅,五分钟内就会被发现

所以判据必须是一个**续租时不变、重领时必变**的值。那就是令牌,
与 `batch_job_items.lease_token`(迁移 0026)同一个东西、同一套语义。
迁移 0047 把它加到 `publish_outbox` 上。

### 三、`status == LEASED` 不进 WHERE,而这是一个决定

批次那边的四个条件里有 `status == RUNNING`。发布这边只用令牌,因为
`reconcile_unknown()`(人工确认结果未知的提交)走的是一行 **DEAD** 的 outbox,
而它同样是一次真实调用、它的结论必须写得进去。

    备选          让确认路径把 status 改成 LEASED,四个条件与批次对齐
    选错的后果    进程恰好死在确认中间的那一次,租约过期后 `claim_due` 会把
                  这一行重新领走**自动再发一遍** —— 而 DEAD 的语义正是
                  "不再自动投递"。一次人工确认不该把它悄悄改回自动重试
    守卫          `test_the_ownership_test_is_a_token_not_a_timestamp`
                  (反向断言,窗口封闭)

令牌不需要状态配合:它自己就能回答"这次调用还有没有资格落库"。
条件更少反而判得更准 —— 这是本批唯一一处**刻意**与批次链路不对称的地方。

### 四、`_still_holds` 用行锁而不是条件 UPDATE + rowcount

批次那边一次只写一张表,条件 UPDATE 的 rowcount 就是答案。这里
`apply_outcome` 要写三张(outbox / attempt / listing),一条 UPDATE 的 rowcount
**管不到另外两张** —— 校验和写入之间只要有窗口,挡住的就只是三分之一。

`SELECT … FOR UPDATE` 的行锁握到本事务提交为止,而 `claim_due` 用的是
`FOR UPDATE SKIP LOCKED`:想在这中间重领的 worker 会跳过这一行,
不排队、也插不进来。

### 五、失去执行权 ≠ 这次调用没发生过

`_record_stale_outcome()` 把结论写进审计并打 WARNING。理由与
`batch_service._record_stale_outcome` 逐字相同:这次调用的结论在业务表里
**不会**留下任何痕迹(那正是本次修复要的),于是"平台那边到底发生了什么"
没有落脚点。审计是追加的,它是唯一还能回答这个问题的地方。

`run_due` 的统计里 `stale` 与 `lost_lease` 分开计数,因为处置不同:
前者说明真实调用耗时已经越过 `LEASE_SECONDS`(要调参数),
后者说明续租窗口偏紧(没花钱)。

### 六、四条文档失实里,最贵的那条长在"教别人怎么数"的旁边

`core/clock.py` 的收敛台账写着 14 处,实数 16;缺的两处里
`services/model_license.py:53` **原样就是那个被点名禁止的写法**,而台账
同时宣称"全仓唯一一处已由 A34 收掉"。一句**说缺口已关**的话(§3.42)
长在一段**教别人别手数、要用 AST** 的文字旁边 —— 那段补救措施本身是对的,
它只是不是一个会自己执行的东西。规矩要么是不变式,要么是散文(§3.26)。

本批把两处收掉,并加 `tests/pure/test_a45_batch17_2_clock_ledger.py`:
它每次现数一遍,与台账逐文件比对。按 §3.31 钉的是**两份真相一致**而不是
"数字等于 14" —— 收敛一处 + 更新清单 = 绿,收敛一处 + 不更新清单 = 红。

### 七、顺手撞见的一处 aware/naive 混比

收 `model_license.py` 那一处时发现它只归一了**一侧**:`now` 脱了 tzinfo,
而 `template.license_expires_at` 直接拿来比。那一列是 `timestamptz`,
换一个 session 重新查出来是 aware 的,比较当场 `TypeError`。
写入侧的 `_naive_utc` 加上 `expire_on_commit=False` 让这件事在
"写完立刻回读"的路径上一直不暴露。

它落在 §11.3 的硬阻断上,抛出去的表现是「点生成 → 500」而不是「授权已过期」。
本批一并修了(两侧都过 `as_naive_utc` / `utc_now`)。同时它证明了 clock.py
里那两轮 AST 扫描的射程边界是真的:第二轮找的是"未经 replace 就继续使用",
而这一处 **replace 过了** —— 出问题的是另一侧。

### 八、合入复审补的一刀:ORM 的 `== None` 不是 SQL 的 `= NULL`

来包把无令牌调用的安全性建立在一句话上:`lease_token = NULL` 在 SQL 三值逻辑
里判假。这个结论对手写 SQL 成立,对当前实现不成立 —— SQLAlchemy 会把
`PublishOutbox.lease_token == None` 编译成 `lease_token IS NULL`,恰好命中存量
无主行。也就是说,`_Call.lease_token` 的默认值若被某个新调用点漏传,
原实现会把“没有执行权”翻成“持有所有 NULL 行”。

因此 `_renew_lease()` 与 `_still_holds()` 都在发 SQL 前显式拒绝空令牌,
并新增守卫与 M10/M11 两个变异分别摘掉这两个分支。这个决定不依赖 ORM 将来
怎样生成 SQL:**无令牌就是无执行权**,必须在 Python 边界先说清楚。

复审还修正了两处验证基础设施:时钟台账用例把相对路径统一成 POSIX 形状,
避免 Windows 的 `core\\clock.py` 被误判成入口外文件;真库并发用例清理审计时
改用真正写入的 outbox ID,不再把 listing/attempt ID 当作审计实体 ID。

## §3.48 A45-batch18:接线欠账的理由必须准,准不了就别写

阶段 4 的方案面板写完之后连着四批进不去任何路由。四批的处理都没错
(每一批都如实记了账),错的是**账上那句理由**:

    「等有人写一行 import」

14-23 逐条核路由时发现无处可写:面板要 `spuId`(UUID),而当时前端没有任何
一条路径拿得到 SPU 主键。这条已经按 §3.33 改准过一次,本批把它还清。

值得单独记的是**照着错理由去做会发生什么**:那个人会打开 `App.tsx`、
找不到能传主键的地方,然后多半随手把手边有的 `spu` 字符串码传进 `spuId`。
接口收 UUID,于是 422 —— 而错因指向后端,查的人会从 `api/generation_plans.py`
开始查起。**一条过期的理由不只是没用,它会把下一个人送到错的地方。**

### 一、主键的收敛:不一致时不猜(D1)

`SpuGroup` 的分组键是 `spu` 字符串码,主键住在每一行商品上,所以
`SpuGroup.spu_id` 是一次收敛。两行共用字符串码却指向不同主键时(§4.1 的
唯一约束不允许,但约束在库里、这个函数在纯层),给 `None`。

    备选        挑第一个
    选错的后果  运营点进去看到的是另一个款,方案面板照着那个主键列方案、
                启用、把**别人的**图片集判过期 —— 而每一步都不报错
    守卫        test_a_group_whose_rows_disagree_reports_no_key_instead_of_picking_one
                (变异 A1)

**半迁移的一组仍然给主键**,前提是非空的那些一致。备选(一律给 None)
在迁移中间态里会让这一页说"这个 SPU 配不了方案"而不解释为什么,
而中间态可能持续很久。守卫
`test_a_half_migrated_group_still_reports_the_key_the_migrated_rows_carry`,
变异 A2 是反方向。

### 二、宿主页新开一页,不挂在聚合页的展开行里(D2)

聚合页按字符串码分组,一行可能对应"没有主键的老商品"。把面板铺在展开行下面
等于让一个**只在部分行上成立**的功能长在每一行下面,而不成立的那些行没有
任何视觉差别。分法改成:聚合页答"哪个 SPU 该处理",详情页答"对它做什么";
聚合页只在后端真给了主键时给链接,给不出时用 tooltip 说明。

### 三、守卫被注释喂真,第四次 —— 这次改的是输入不是锚点

变异 C2 把宿主页那行 import 注释掉,守卫第一轮绿。前三次
(batch13-3 M11、batch14-2 N15、14-22 L5)的修法都是把断言锚得更死。
本批换了方向:**从输入里把注释去掉**(`_code_only()`)。理由是锚得再死也
挡不住下一段刚好提到那个名字的注释,而解释性文字本来就不该参与判定。

剥掉注释之后 C2 **仍然绿**,这一层同样值得记:JSX 里的
`<GenerationPlanPanel .../>` 接着把断言喂真,而那一刻组件根本不存在
(tsc 会红,纯层看不见)。「出现了某个名字」不等于「那个名字是从这里来的」——
断言最终钉在 import 语句上,渲染另有一条。与 14-11 的 W2 同型。

## §3.49 A45-batch18 评审修复:门禁扫的是工作树,而交付的是版本库

外部评审在本批之前发现了一件既有门禁全部看不见的事:两条关键迁移
(`0046` / `0047`)与对应的并发测试**没有被 Git 跟踪**。本机
`alembic heads` 说 0047、`verify_delivery` 16/16、纯测试全绿,而从 main
做一次 clean checkout 得到的最后一条迁移是 `0045` —— 已提交的代码要读
`publish_outbox.lease_token`(0047 建的列),部署之后报 UndefinedColumn。

### 一、这一整类问题只有问 Git 才答得出来

`check_the_migration_chain_has_a_single_head()`、`alembic heads`、纯测试、
import 检查 —— 全部扫的是**当前文件树**。文件就在那里,链也是通的,
它们没有任何理由报错。**门禁的取数范围和交付的取数范围不是同一个**,
而在这次之前没有任何一条门禁问过这个差。

新增 `check_every_migration_and_db_test_is_tracked_by_git()`。范围只到
迁移与 `*_db.py`,理由是这两类是"漏提交之后**别人的环境**会坏、而本机
一切正常"的那一批;普通源码漏提交会在 CI 的 import 检查里当场炸。
范围写窄是刻意的:一条把整个工作树都算进来的检查会在任何人有一个临时
脚本时变红,而最省事的消法是把整条门禁删掉(§3.37 第一版当场踩过)。

不是 Git 工作树时**直接失败**而不是跳过。这个脚本的定位是"交付动作有没有
做对",而在一个 tarball 解出来的目录里宣布"交付卫生通过",正是上一次
那份 16/16 起的作用。

写完当场验证了它:这条门禁第一次运行就红了,红在**本批自己新增的**
`0048` 和 `test_a45_batch18_lease_visibility_db.py` 上。

### 二、"重试对调用方透明"是设计目标,也是记账的洞

`llm/transport` 对超时/429/5xx 自动重试,`fashn.submit` 把候选拆成多次
POST。两处的共同点是**调用方只看到一个返回值或一个异常**,于是费用台账
按"一次业务调用 = 一个计费单位"记。真实情况是最多 3 个请求(识别)或
最多 N 个 POST(生成),而反方向上,一次在网络调用之前就失败的 preflight
也照样记了一个单位。

两个方向的错误**不对称,但都致命**:少记的是已经花掉的钱,多记的是
从来没花过的钱。叠在一起之后 `/spend` 与供应商账单再也无法逐条核对,
而 §10.2 第 5 条准入要的正是那个一致性。

判定收进两个纯模块(`extractors/call_accounting.py`、
`providers/call_accounting.py`),形状与 `providers/base.settle_billable_units`
一致:返回 `(units, source)`,第二个值回答"这个数是谁说的"。

**"不知道"必须退回 1,不能退回 0。** 抽取器是可插拔的,一个没接上次数
上报的第三方 billable 抽取器如果被记成 0,一整类真实调用会从账上消失 ——
而那个方向的错误没有人会去发现。这与 `ProviderUsage.units is None` 是
同一条规矩,也是这两个模块里被单独测到的那一条。

### 三、`provider_attempts` 为什么是新列而不是复用 `billable_units`

两者在识别链路上恰好相等(一次往返 = 一个单位),在生成链路上不等:
FASHN 一次 POST 可能出多张图并按额度计价。合成一列的话,对账的人拿它去
比供应商后台的调用条数,会在一半的链路上得到一个**必然对不上的数**,
而对不上的原因是口径不是漏账 —— 那是最浪费时间的一种红,也最容易让人
把整张表判成不可信。

列可空,且 NULL ≠ 0:NULL 是"这一类调用还没接上报",0 是"确认一个请求
都没发出去"。给 `server_default="1"` 等于宣称每一条存量流水都恰好发过
一次请求,而那正是这次要修的那个假设。

### 四、顺序对不等于有效:续租必须是**已提交**的事实

A43 加的 `renew_lease()` 调用点在付费调用之前,既有门禁用源码位置证明了
这一点。但那条 UPDATE 留在外层事务里,而外层事务要等 `_execute()` 跑完、
结果保存完才提交。整个付费调用期间,回收器在它自己的会话里读到的仍是
**领取时**那个 `lease_until`。

于是续租挡得住"我在开跑前就已经出局了",挡不住"我正在跑却被判过期"——
而后者才是那条不变量真正要防的。**源码扫描永远看不见这个区别,因为两种
写法的源码顺序完全一样。** 补了双会话真库用例,并附一条反向用例
(不提交时回收器确实读不到)让正向那条的绿有意义。

### 五、一条守了三批的不变量,被除数取的是另一条链路的参数

`LONGEST_LEGAL_ITEM_SECONDS` 写的是 `90 * 3 * 4`,三项全部来自
**VISION_MODEL_\*(评分器)**,而批次里跑的 EXTRACT 读的是
**EXTRACTOR_MODEL_\***。两组键从一开始就是独立的。真实上限是
`60 × 3 × 12 = 2160` 秒,而 `ITEM_LEASE_SECONDS` 是 1800 ——
"租约必须长于单件最长合法耗时"这条不变量**在默认配置下根本不成立**,
而那条模块级 `if` 因为被除数取错,一次都没有红过。

教训不是"算错了",是**一条用常量表达的不变量,它的每一项都要能指回
真实配置项**。改成 `longest_legal_item_seconds()` 纯函数 + 一条读
实际部署配置的启动检查:前者挡"改了常量忘了改租约",后者挡"运维在
设置页把单张超时从 60 调到 120"。

### 六、一个任务号绑两件交付物,文档必然分叉

任务 20 在三份文档里有三个完成状态,而三句话各对了一部分:页面确实做完了,
`resolve_gate` 确实还没有。这不是谁写错了,是**一个号绑着两件依赖与排期
都不同的交付物**,于是每个人按自己关心的那一半陈述。拆成 20-A / 20-B,
每一行只有一个完成定义。

## §3.50 A45-batch19(阶段 5 批次 5-1):快照列的默认值决定了它是防线还是装饰

阶段 5 交付的第三项是 `upstream_versions` 与 `color_sku_image_map` 两列
(PRD §4.10)。本批只落列 + 迁移 + 零依赖判定层,接线在 5-2。

### 一、`{}` 与 NULL 在这两列上不是风格问题,是放行与不放行

`listing_drafts` 上其它 JSONB 列一律 `nullable=False, server_default='{}'`,
照抄很自然。但那几列从第一版起就有写入点,`{}` 的含义是「这份草稿确实
没有手填字段」;而 0049 之前建的每一行草稿都**一次都没算过颜色维**。

同一个 `{}`,在这两列上的含义变成「算过,颜色维是空的」。READY 门禁读到它
会在颜色轴上**静默放行** —— 一份缺了整个颜色的草稿显示「上游全部有效」,
不报错、不告警、不留痕。

所以两列留 NULL,判定层把 None 判成不可证明、不放行。这是 0042(NULL 兜底成
「过期」)、0045(NULL 兜底成「没算过」)、0048(NULL 兜底成「不知道发过
几次请求」)之后同一条规矩的第四次应用,四次的共同点写成一句:
**默认值不许让任何人被静默放行或静默拦下。**

判定层的第一个分支因此不是防御代码,是这条规矩的落点 —— 它拦下的是本批
之前建的**全部**草稿,而那正是正确结论。

### 二、范围口径的反向那一半,没有人会主动去写

§6.7 写的是「只检查 `sellable_status=ACTIVE` 的颜色与其 SKU」。照着它写
测试,写出来的一定是正向那条:ACTIVE 颜色变了要过期。

反向那条(PLANNED 颜色怎么折腾都不许让草稿过期)不在条文的字面上,而它
的失效更贵:一个还没到样的颜色会让整个 SPU 的草稿永远处在过期态,运营
重新生成一次它又过期,**而提示里说的是一个他压根还没开始做的颜色**。
正向失效有人在导出文件里看得见;反向失效没有任何东西会报错,只会让人
不再相信过期提示 —— 然后去关掉它。

实现上的落点是快照分两格:`colors` 只放 ACTIVE 且带全部版本引用,
`inactive` 只记 `{颜色 id: 可售状态}` 且**一个版本都不记**。少了 `inactive`
这一格,「被停用了」和「被删了」在提示里就是同一句话;而它一旦带上版本,
上一段那件事当场发生。这两条约束方向相反,所以各有一条守卫。

### 三、§4.10 与「一个轴只有一个人报」冲突时,落列与比较要拆开

§4.10 点名 `upstream_versions_json` 要含 Copy 版本。而 copy / spec / mapping
三个轴已经在 `canonical_snapshot["components"]` 里,由 `stale.diff_components`
负责解释。两边都采集就是第二个漂移源,两边都比较就是双报。

拆法:值从 `components` **抄**过来(单一写入点、两个读取方),满足 §4.10;
比较仍然只由 `diff_components` 做一次。两条守卫钉两个方向 —— 值真的落进去
了(否则 §4.10 没兑现),以及改了它一条变化都不出(否则改一次文案版本会
出两条过期提示,而运营看到的是「这份草稿有 2 个上游变了」,
**「变了几处」这个数字从此不可信,且不会有任何东西报错**)。

### 四、`DELIVERY_STAGE` 不跟着开工走,跟着还账走

阶段 5 开工了,标记仍是 4。它的含义是「本仓当前落码到第几阶段」,而推到 5
会让三条还款日写「阶段 5」的欠账守卫当场变红,外加 `audit_column_writers`
台账里 13 条同期的账。本批一条都不还。

这看起来像是把标记当成了「还完账才敢动的数字」,而那正是它该有的样子:
STATUS.md 顶部那段注释自己写着「变红时该做的是还上那笔欠账,或者把还款日
往后改并写清为什么改 —— 不是回来把这个数字调回去」。**开工不是还账,
标记记的是后者。**

### 五、第五次:解释性文字又一次喂真了断言

本批「判定层今天没有生产调用点」那条守卫,第一版按文本扫 `app/` 下的源码。
它当场报「已经接线了」,命中的是 `models/listing_copy.py` 的列注释——
那句话写的恰恰是「形状与判定在 `app/workbench/upstream_snapshot.py`」,
也就是**在说它没接**。

同一个坑在本仓是第五次(§3.26 / 14-13 / 14-22 / batch18 的 C2)。前三次的
修法是把锚点钉得更死,第四次改成从输入里把注释去掉。本次沿用第四种,并把
它推到判据层面:**判据换成 `ast` 的 import 节点,而不是任何形式的文本匹配。**
`ast` 天然不看注释,所以这条守卫不再需要「记得剥注释」这个人工步骤。

顺带一条同型的:那条守卫要同时认 `from app.workbench import upstream_snapshot`
与 `from app.workbench.upstream_snapshot import ...`。只认前一种的话,5-2 用
后一种接线时它**不会翻转** —— 一条还清了却不肯变红的欠账,和一条没人记的
欠账等价。

## §3.51 阶段 5 / 5-1 评审复核：自描述要落到运营能读懂的键，接线前先把爆炸半径分开

5-1 的 NULL、防 PLANNED 误报、单轴单报三项决定继续成立。评审复核发现的
问题不是推翻这三项，而是快照虽然机器可比，尚未完全做到「人能解释、接线不漏门」。

### 一、颜色事实快照存映射，不存版本集合

旧形状的 `color_facts` 是版本 UUID 集合。集合足够回答「是否变化」，却无法回答
「哪个字段变化」；UI 最终只能显示 UUID。改为 `{version_id: field_key}`，比较仍以
版本为身份，提示以字段键为名字。这里不引入运行时解析器：解析器会让快照的可解释性
依赖当前注册表，恰好破坏「当时看见了什么」的审计价值。

同理，`inactive` 不只是删除/停用的辅助信息。它还区分「之前 PLANNED、现在转正」
与「快照后新建 ACTIVE」。两者都应过期，但运营动作不同，所以必须是两句文案。

### 二、READY 只有一个合取入口

`map_problems()` 只负责 SKU 映射，不负责主图位；`validate_set()` 只负责图片集规则，
不负责 SKU 全集。两者任何一个单独接到 READY 都会静默漏门。增加 `ready_problems()`
作为 5-2 的唯一入口并用集成守卫证明两边都调用。SKU 全集同时定死：同一颜色下全部
`products.sku`，不按状态、库存、条码、价格再过滤；构建和检查读取同一份采集结果。

### 三、两列的 schema version 必须独立

共用一个版本号意味着任一侧的小改动都会让另一侧也不可比，迫使全量草稿两列一起
重建。拆成 `UPSTREAM_SCHEMA_VERSION` 与 `IMAGE_MAP_SCHEMA_VERSION`。这不是允许
随手 bump：任一版本变化仍必须附存量重建影响，只是爆炸半径被限制在真正变形的列。

### 四、找不到历史 AC 时，签认执行版，不能补写成“原文”

Git 全历史没有 PRD v3.0，AC-10/11/18/19 无法恢复原句。继续把它们记为不可得会让
5-2～5-5 对着未知标准施工；直接写四句又会伪造来源。处理方式是在现行 PRD §14.1
写「仓内执行版」，明确依据现行 §4.9/§4.10/§6.7 和阶段散文验收，明确它不是找回的
v3.0。AC-18/19 目前只有 5-1 部分证据，不能标完整通过。

### 五、阶段切法必须把到期账算进去

原 5-1～5-5 只映射 PRD 五项，没认领 `owner_id` UUID / `variant_key` 退役、三条
欠账守卫和 13 条列写入欠账。新增 5-2A 身份与真库前置、5-2B 草稿接线、5-C 验收
与台账收口；其余欠账按 5-3～5-5 的实际消费链路分配。完整清单在
`docs/REVIEW-STAGE5-5-1-CONCLUSION.md`。

NULL 的上线语义不改：存量草稿全部在颜色轴不可证明。5-2 必须以分批重生成、
READY/导出禁行、失败重试和运营公告做演练，不能用 `{}` 回填把未知伪装成空集。

---

## §3.52 A45-batch20(阶段 5 批次 5-2B):接线是一跳,而这一跳没有任何既有测试走过

5-1 把判定层验干净了 —— 29 条纯守卫、13 条变异全红。本批开工时先做了一件事:
数一遍**有多少测试真的调用过 `build_draft`**。

答案是零。`grep -rn build_draft tests/` 只命中 5-1 那份纯守卫里的文本断言。

这个数字决定了本批的形状。它意味着接线做完之后,整套真库用例(当时 2881 条)
照样全绿 —— 它们一条都不走这条路。§3.43 已经把这一类点过名(「上游算对了、
没人接最后一跳」),而那一次的结论是「加一条门禁看模块有没有被 import」。
本批发现那条门禁不够:**`WIRED_MODULES` 只问「有没有人 import」,一个 import
就够。**只接写入点、不接读取点的话,快照会一直落库而没有任何人比较它,
表现是颜色维永远不过期 —— 和没接线一模一样,却过得了那条门禁。

所以本批的守卫钉的是**两处**:写(`build_draft`)与读(`refresh_draft` 的
颜色轴)各有一个,再加一份真库用例文件从外面走完整条链路。

### 一、图片集这一轴从「被比较」搬到「记下但不比」

5-1 落码时 `_one_color_changes` 会比较每个颜色的 `image_set`。接线一做就暴露:
图片集今天**按 SPU 批准**(`resolve_for_publish` 解析的是 `(spu, channel, site)`),
于是每个 ACTIVE 颜色引用的是同一版。重新批准一次图片集会让判定层报 N 条、
`stale.diff_components` 的 `image_set` 再报 1 条 —— **合计 N+1 条说的是同一件事。**

这正是 §3.50 D3 为 `copies` / `spec_version` / `mapping_version` 三项定过的规矩,
本批是它的第四次应用:**值仍然落进快照(AC-19 要「可解释的上游版本引用」——
「这个颜色的映射是从哪一版图片集铺出来的」在出事之后是第一个要查的东西),
但比较只由一个人做。**

`color_image_set` 因此从 `COMPONENTS` 里删掉,前端 `STALE_COMPONENT_LABEL` 同步。
两者由 `test_frontend_contract` 做集合相等,所以加回来会当场变红。

**两个方向都要钉。** 只钉「不出变化」的话,最省事的退化是把这一格干脆不写进
快照 —— 那样 AC-19 那半就没了,而"不出变化"照样绿(变异 E2)。

### 二、READY 是阻断,而「阻断」在这个仓库有过一次相反的判例

§6.7 / AC-18 的原话是「任一侧失败都不得 READY」。而
`image_set_service.variant_coverage` 的文档字符串里记着上一次有人考虑
硬阻断时的顾虑:「那会让每一个多色 SPU 立刻无法批准 —— 那不是修复,是停产。」

那句话今天**不成立了**,而且是双重不成立:

1. `coverage()` 已经把「有通用图就算覆盖」那条放行删掉了,而
   `image_set_service.validate()`(批准门禁)已经在传真实的 `variant_ids` ——
   也就是说硬阻断在**批准**那一步早就生效了
2. 草稿这一侧的口径比批准更窄:只查 ACTIVE 颜色(§6.7),而批准查全部变体

所以本批把颜色维问题接成 `level="error"`。顺带把 `variant_coverage` 那段
过期的顾虑改掉 —— 它描述的是一个已经被关掉的缺口,而按它去做的人会以为
自己在避免停产,实际是在给一条已经生效的门禁开洞。

单色 SPU 与没有归属外键的存量行各有一条真库用例钉着**不因此被拦**。

### 三、两条被现有守卫当场拦下的写法,都不是"风格问题"

接线的第一版有两处直接被既有纯守卫判红,值得记下来,因为两处都是
「看起来最自然」的写法:

```
select(Spu).where(Spu.spu_code == product.spu)   §3.39 禁掉的形状
scope_fingerprint.fingerprints(views(assets))     §5.1 的唯一取数入口被绕开
```

第一处的代价不是"多一次查询":那个码可能已经因改名而与 SPU 行断开,
反查出来的是**另一个款**的颜色集,而这份草稿会按别人的颜色去铺 SKU。
正确写法是读 `product.spu_id`(§4.4 的权威),空的就如实给空颜色轴。

第二处的代价是写入侧按 A 集合算指纹、读取侧按 B 集合算,两边都不报错。
正确写法是调 `attr_service.scope_fingerprints_for()` —— 本模块是它的
**第四个**消费者,而那个函数的文档字符串里列的是三个。

**两条守卫都不是本批写的。**它们分别是 14-26 与 14-21 留下的,而它们
在本批第一次真的咬到人。这是这套守卫第一次拦下**新代码**而不是拦下回退。

### 四、锚点审计多了第三种表形状

5-2 的守卫分在两个运行器上(纯层 + 真库),所以变异表要多一列说明每行
该由谁跑。`tools/audit_anchors.py` 的 `SHAPES` 按**表名**认宽度,所以
表名改成 `RUNNER_MUTATIONS`(arity 6)而不是沿用 `MUTATIONS`(arity 5)。

沿用旧名的后果具体而安静:审计按 5 列读、脚本按 6 列解包,两边对同一张表
的宽度理解不同,表现是审计说"锚点全对"而脚本在解包时静默取错列。

变异脚本在没有 `TEST_DATABASE_URL` 时**明确跳过并返回非零**,不静默当成通过。

### 五、同一个坑的第六次:注释喂真断言

翻转后的写入点守卫第一版按文本扫 `"row.color_sku_image_map = "`。
变异 B2 把那一行改成 `pass  # row.color_sku_image_map = ...` —— 写入点死了,
而**注释里那几个字仍然喂真断言**,B2 报 GREEN。

§3.26 / 14-13 / 14-22 / batch18 的 C2 / batch19 的 caller 守卫之后第六次。
修法与前几次同向:**从输入里把注释去掉**,判据换成 `ast` 的赋值目标。

### 六、这台机器上第一次跑到了真库

本批之前每一份交接的「仍未执行」里都写着真库 pytest。这次容器里装得上
PostgreSQL 16,所以全量 pytest(含迁移升降级)第一次真的执行:
**2891 passed, 0 skipped**。0049 的 upgrade 与 downgrade 也第一次在真库上跑过。

这不改变验收结论(AC 仍然一条都没在**真环境**验收过 —— 真库 ≠ 真环境),
但它把此前所有交接里那一整类「写好了、没实测」的欠账关掉了一大半。

---

## §3.53 A45-batch21/22/23(阶段 5 收官):三笔账,以及同一个坑的第七到第九次

5-3 / 5-5 / 5-4 三个批次一起收掉 PRD §13 阶段 5 的后三项交付。三笔账的
共同点是**都已经"写好了"很久,只差最后一跳**,而缺的那一跳在测试里看不见。

### 一、5-3:幂等单元不含尺码,而缺口只长在单件入口上

`listing_copies` 从第一版起就是 SPU 粒度的(`spu`+`channel`+`site`+`locale`
唯一),而入口是 SKU 粒度的。一个 S/M/L 三码的 SPU,运营在三行上各点一次
「生成文案」= **三次真实 LLM 调用、三个版本**,输入完全相同。

批量那条路早就对了(`batch.ACTION_SCOPE[GENERATE_COPY] == "spu"`,计划阶段
按 `scope_key` 去重,回执表挡重跑)。所以这个缺口只长在**单件入口**上 ——
而单件入口恰恰是运营日常用的那一个。这类"批量对了、单件没对"的不对称
值得单列一条:批量路径因为要处理 50 件,天然会被设计成幂等的;
单件路径看起来"就一次点击",于是没人给它加。

两个决定:

1. **`REJECTED` 不占幂等槽位。** `save_copy` 会把规则硬失败的输出也落库
   (好让运营看见模型吐了什么)。拿它复用的话这个 SPU 的文案**永远修不好**,
   现象是"点生成没反应"。与 `run_state.unique_index_predicate()` 拒绝全表
   唯一是同一条规矩的第二次应用 —— 也因此 0050 **不加唯一索引**。
2. **存量 NULL 判「重新生成」,不判「复用」。** 与 0049 那两列的 NULL
   方向相反,而两者同源:**默认值的方向按代价选,不按对称选。**
   那边放行的代价是缺了整个颜色的草稿被导出;这边复用的代价是拿一版
   按早就变过的事实生成的文案顶上。

### 二、5-5:预览的正确性不在"算得准",在"算不了"

AC-19 的后半句是「导出预览与最终导出读取**同一份已存映射**,不在预览时
重新推断」。这句话最容易被"改进"掉:重新推断的预览显示"当前该用哪些图",
看起来更准,而导出用的是草稿生成那一刻存下来的那些 —— 上游变过之后
两者不是一回事,运营在预览里看到红色有主图、导出出来那一行是空的,
**两边都不报错**。

所以 `export_preview.image_preview()` 的签名里**只有落库的那份映射**:
它拿不到图片集、拿不到颜色表、拿不到商品行,想重新推断也无从下手。
这不是保守,是把"不许重新推断"变成一件做不到的事 —— 而守卫钉的正是
那个签名(变异 A1:多一个 `image_set=` 入参,当场红)。

缺主图的行**留在表里**标 MISSING,不跳过:跳过的话运营会读成
"这个尺码不在这次导出里",而它在,只是没有图。

### 三、5-4:一笔账靠"一个不存在的字段名"躲了几批

`ColorVariant.display_name` 的列注释从落列那天就写着「唯一写入点是属性
服务在 `standard_color_name` 被确认时」,而:

    前置    「要等 owner_id 切 UUID」—— 迁移 0046 就做完了
    字段名  **全仓没有 `standard_color_name`**,注册表里是 `primary_color`

第二条才是真正的挡箭牌:照那句话去接线的人会先找那个字段、找不到,
然后多半得出"还缺前置"的结论,于是这笔账又躲过去一次。后果是可见的 ——
后端 5 处读、前端 7 处显示,而值恒为空串。

守卫因此**去注册表里查那个字段**,而不只是把名字定死在常量里:
常量也可以写错;注册表把颜色挪到 SPU 层的那天,这条会当场红,
而不是让投影安静地再也不触发。

### 四、同一个坑的第七、第八、第九次:解释性文字喂真断言

`§3.26 / 14-13 / 14-22 / batch18 C2 / batch19 caller / batch20 B2` 之后
又来三次,而且**三次都是本批自己的守卫咬自己**:

```
batch21  `generate_copy` 的 docstring 里写着「`_content_plan_or_raise` 必须
         留在判定之后」,于是按顺序找它的断言拿到了 0(那句话本身)
batch22  按文本扫 `coverage` / `build_color_sku_image_map`,而模块文档里
         正写着「不许出现这两个」
batch23  `_project_colour_name` 的 docstring 里写着「这里不 commit」,
         于是 `assert "commit" not in body` 当场假红
```

修法每次都一样:**从输入里把解释性文字去掉**(AST 的 docstring 剥离 /
调用名节点)。这个坑出现九次说明它不是疏忽,是**这个仓库注释密度高的
必然副作用** —— 凡是读源码的守卫,第一步就该剥注释,而不是等它咬。

### 五、变异证伪了一条自己写的理由(batch23 B3)

`needs_update()` 原来的文档写着「不这么做的话每次确认都会让
`color_variants` 进 UPDATE,`updated_at` 跟着动」。变异把那半个条件拿掉,
**行为完全一样** —— SQLAlchemy 的脏检查本来就会把"赋一个和当前值相同的值"
折叠掉,不发 UPDATE。

那条 B3 无论怎么写都验不红,因为它对应的缺陷不存在。处理方式是
**删掉那条变异并把理由改对**,而不是把断言改松让数字好看。
`needs_update` 承重的只有 `wanted is None` 那一半,现在文档如实这么写。

### 六、`DELIVERY_STAGE` 仍然是 4,而这一次要说清楚为什么

PRD §13 阶段 5 的五项交付**全部落码**(batch19~23)。而标记不能推,
因为它的语义被门禁定死:`还款日:阶段 N` = 「推进到 N 之前必须还清」,
推到 5 会让 11 条列写入欠账 + 3 条欠账守卫**当场逾期变红**。

那 14 条**都不是阶段 5 的交付项** —— 是识别侧 token 计量、原始响应留存、
几个缺入口的 UI 字段等等,当初被随手写上"阶段 5"这个还款日。

所以两件事必须分开说:**五项交付落码完毕** ≠ **标记可以推进**。
把标记推上去再回来改还款日,是 `STATUS.md` 顶部那段注释明令禁止的做法。

---

## §3.54 A45-batch24(三位外部评审的复核):接了一半的三条接缝

三位评审各自独立读了 batch21/22/23 的交付包,给出的清单高度重合。
重合的那几条**不是"没做",是"做了一半"** —— 每一条都有真库用例覆盖着
做了的那一半,于是套件全绿、文档写"已验证",而另一半在生产里持续出错。

这是本仓第十到第十二次撞同一族问题(§3.43「上游算对了、没人接最后一跳」),
但形状比前九次隐蔽:前九次是**没人接**,这三条是**接了一条路**。

### 一、颜色投影只接了人工确认那条路(评审 F-1)

`_project_colour_name` 挂在 `confirm()` 上。而 CONFIRMED 不只由它产生:
`apply_evidence` 经 `decide_status`,在「置信度 ≥ 组阈值且库里为空」那一档
同样写出 CONFIRMED。那条路不投影,于是**识别自动确认出来的颜色**,
`display_name` 仍是空串 —— 正是 batch23 声称关闭的那个现象。

**为什么当时没暴露。** 两层:未校准时 `system_confidence is None` → CANDIDATE,
没跑过 `make calibrate` 的部署根本到不了自动确认那一档;而且即使校准了,
单图证据的一致性因子只有 0.85、没测过质量的图按 0.85 算,0.99 的校准值
乘下来是 0.72,低于 identity 组的 0.95。**要三张清晰的同值证据才触发。**
这是部署状态与数据形状的双重掩盖,不是结构性防线 —— 灌了校准数据、
素材质量正常的生产环境上,缺口立即生效。

**修法:调用点挪进 `set_value`(写入边界),按 CONFIRMED 触发。**
不是"在 `apply_evidence` 里也加一句"。理由:`set_value` 是
`product_attribute_values` 的唯一写入口,挂在这里,「所有产生 CONFIRMED
颜色事实的路径都触发投影」成为结构保证,不再依赖每个调用方记得调一次。
守卫因此写成两条全称命题:构造点只有 `set_value` 一处 + `apply_evidence`
确实经过它 —— 少任何一条,"所有路径"就退化成"我检查过的那几条路径"。

### 二、文案的键比存储细一档(评审 F-6)

`_copy_unit` 的 `color_variant_id` 恒传 None,注释写着「等 5-4 的颜色
结构化字段」。5-4 交付之后没有人回来接。而**键其实早就随颜色变了**:
`fact_versions` 里带着该颜色的 `primary_color` 版本,`primary_color` 又是
`required_for=ALL_AUDIENCES`,所以每个能生成文案的颜色都有一条。

于是:键按颜色分,存储按 SPU 一个槽。红色点生成 → 存 key_R;蓝色点生成 →
既有版本带 key_R ≠ key_B → 判 GENERATE → **付费一次**,且新版本把红色那版
挤成 ARCHIVED;回红色再点 → 又不等 → 又付费一次。在颜色轴上来回点,
每次都花钱,而"当前文案"在两个颜色之间摆动。

**方向:存储下沉到颜色粒度(迁移 0051),不是把颜色从键里去掉。**
去掉键里的颜色会让蓝色行复用一篇写着「Coral Red」的文案 —— 文案内容
**按设计就是颜色相关的**(标题模板第一段是颜色,`REQUIRED_FACTS` 要求
`primary_color` 有 claim)。AC-11 第二句也定死了:「颜色维文案若启用,
幂等粒度为 SPU + 颜色 + 语言」。

**一并改细的还有批量去重键**(`ACTION_SCOPE[GENERATE_COPY]`:`spu` →
`spu_colour`)。这是同一个决定的另一侧,而且方向相反:去重键比槽**粗**
一档的话,三颜色 SPU 的批量生成只给第一个颜色出稿,另外两个记成
「已跳过」—— 界面显示"成功 1 件、跳过 2 件",读起来完全正常,
而那两个颜色到导出时才发现没有文案。**粗一档漏做、细一档白花钱,
两侧都不报错**,所以守卫把键、槽、去重三处写在同一条断言里。

存量行回填哨兵 `""` 而不是真实颜色:哨兵按颜色查永远不命中,也就是
「这个颜色还没有文案」——fail closed,与 0050 对 NULL 键判 GENERATE 同向。
反方向(把 SPU 槽的旧文案指派给某个颜色)等于替当年那次生成补一个
它没有回答过的问题。不用 NULL 的理由是这一列进唯一键,
而 PostgreSQL 把 NULL 当成互不相等,版本号唯一性会在 NULL 槽上失效。

### 三、预览读已存映射,而最终导出仍在自己挑图(评审 AC-19)

`build_draft` 里,`color_sku_image_map`(预览读的那份)按 §6.5 挑:
主图取显式 `is_primary`、通用图只有勾了「可混入」才进附图位、缺图就是缺图;
而 `map_fields` 的行级图片桶自己挑:该变体的图按 `sort_order` 取第一张,
取不到回落到 SPU 通用图。**两套规则各自都合理,合在同一份草稿里就不成立。**

后果:一个合法且 READY 的图片集里,只要显式主图不是排序第一张
(运营换了更好的一张设成主图、排序没动 —— 最常见的形状),
**预览显示 A 图、Excel 与 API 报文用 B 图**,两边都不报错。
第二处分叉更隐蔽:导出会把 SPU 通用图回落成某个颜色的变体图,
而 §6.5 / BLOCK-02 明写「不得回退使用其他颜色的图片,缺图就是缺图」——
于是预览说缺、READY 门禁据此拦下这份草稿,而 Excel 那一行填着一张通用图。

**修法:行级图片只翻译映射里点名的素材 id,`primary` 排在 `extras` 之前。**
`map_fields` 的 `image_map` 做成**必需**关键字参数,不给默认值:
给默认值的写法会让这次修复只在记得传的调用点上生效,漏传的地方继续
静默走老路 —— 表现和修复前一模一样。这与 `export_preview.image_preview`
的签名里只有 `mapping` 是同一手法:把"重新推断"变成一件**做不到**的事。

`build_draft` 的取数顺序因此反过来:先算映射,再把**同一个对象**传给
`map_fields`,最后两者一起落库。一次计算、两个消费者。

**这一改动翻转了两条既有纯用例的期望值**(通用图回落被取消)。
翻转是有代价的,所以记在这里:换的方向是 §6.5 原本就规定的那个,
而旧断言编码的是导出这一侧自己长出来的回落。

### 四、5-5 的运营侧半边不存在(评审 F-7)

后端算出 `preview.images`、前端类型也齐了,而**没有任何组件读它**。
5-5 的存在理由是运营侧的(「运营在预览里看到红色有主图、蓝色缺一张」),
那一半当时不存在。后端守卫钉到 `draft_preview` 的返回值为止,
它的 docstring 自己写着「少这一跳的话本模块就是"算好了没人看"」——
而下一跳没有守卫。batch24 补上渲染与一份组件用例(渲染整个标签页,
不是单独渲染子组件:后者在有人把 `<ImagePreview>` 删掉之后仍然全绿)。

### 五、账目本身:还了没更新、没还也没重新认领(评审 F-8)

- `ColorVariant.display_name`:5-4 认领、batch23 已还,而结论文件 §五
  仍把它列在「仍欠」表里并写「12 条」—— 实际是 11 条。
- `ListingDraft.template_checksum`:5-5 认领、**没还也没重新认领**。
  batch24 改判阶段 6 并写清为什么不还:这一列存的是平台官方 Excel 模板
  的指纹,而今天只有我们自己的 generic spec(版本已由 `spec_version` 落着),
  第一个真实渠道适配器在阶段 6。把 YAML 的哈希写进去会让这一列变成
  另一件东西 —— 列注释仍写着「出事时和平台对账」,而值来自我们自己的文件。
- `test_a45_batch14_9_scope_fingerprint`:四行欠账里「新增/停用 ACTIVE 颜色」
  其实已由 batch20 用快照比对那条路还了,守卫却还在报"欠 4 笔"。
  **一份报出来的欠账比实际多的清单,下一个人核对两轮发现对不上之后,
  会开始怀疑整张清单。** 划到 `settled_elsewhere`,余额三行改判阶段 6。

三条合起来是 §3.34 / §3.37 要防的两种形态各一次:**有还款日而没人盯**,
以及**认领了却不更新认领关系**。到期时的正确动作只有两个:还,或者
重新认领并写清为什么 —— 而不是把日期往后挪一格。

### 六、门禁自己也塌了一角(评审 F-2)

`docs/DECISIONS.md` 里 §3.52 整节**逐字重复了两遍**,于是
`verify_delivery.py` 的「决策日志编号不重复」判负,`make check-offline`
在这一步中止,后面五道门禁(verify-sample-data / verify-imports /
audit-anchors / audit-guards / audit-doc-refs)**一次都没跑**。
一处复制粘贴遮住了五道门禁,而 `STATUS.md` 顶部同时写着「交付 17/17」。

修法是删掉后写的那一节(比对确认逐字相同,不是两节同号的不同内容)。
值得记的是**修它的优先级**:成本极低、遮蔽面积最大,所以它排在任何
需要跑门禁的工作之前 —— 门禁塌着的时候,后面所有"绿"都是没有证据的。

### 七、这一批没有做的事

- **真环境验收仍是 0/22。** 本批的全部证据来自本机 PostgreSQL 16 与
  纯逻辑套件,没有连过任何真实 Provider、渠道或产生费用的调用。
- **AC-11 的真实计费差异未验。** 本地生成器不花钱,"少了几次付费调用"
  是靠 `listing_copies` 行数与 `content_plans` 行数推出来的。
- **Docker / Playwright / 多 worker 未跑。** 与前几批同一口径。
- `DELIVERY_STAGE` **仍是 4**。五项交付落码 ≠ 标记可以推进,
  §3.53 那句话在本批同样成立:到期账里还有 11 条列写入与 3 条守卫。

---

## §3.55 A45-batch25(阶段 6 批次 6-1):先立判据,再动最底下那一层

### 一、开工第一件事不是代码,是判据

PRD §13 写着阶段 6 的验收是 AC-01/05/14~17,而 §14.1 同时写着这六条
「沿用 v3.0 原文,当前不可得」。阶段 5 遇到过同一件事,处理是按仓内条文
重述并签认(AC-10/11/18/19)。这一批沿用,重述表写进 §14.1「阶段 6 仓内
执行版」,签认日期 2026-08-09。

不先做这件事的代价刚刚在 §3.54 付过:5-3 与 5-5 都"交付"了,而两者
各差一半 —— 差的那一半都不是难做的部分,是**没有一条可执行的判据说得清
"完成"是什么**,于是"接了一条路"和"接完了"在验收上没有区别。

七步的取值**不是新定义的**:它就是 §16 最终产品定义那条管线逐行,
一行一步,正好七步。自己另编一套的话,"完成"又会变成一个只存在于
实现里的概念。

### 二、为什么 6-1 只做颜色子态,不碰七步

`flow 增维(七步 + 颜色子态)`是一句话两件事,而两件事的性质相反:

    颜色子态   **新增一层**。输入在 batch19~24 之后全部落地
               (颜色集、颜色→SKU→图片映射、颜色级事实、按颜色的文案、
               按颜色的覆盖率),不动任何既有口径
    七步增维   **改掉一个正在被用的数字**。`STEP_WEIGHT = 20.0`(5×20=100)
               变成七步之后,同一件商品从 80% 变成 71%,而完成度驱动
               列表排序、批量筛选与审阅队列的顺序

合批的后果很具体:完成度一旦出问题,没人分得清是"新增了一层"引起的还是
"改了权重"引起的。所以七步单独作为 6-2,连同它必须同批给出的三样东西
(新权重表、口径变更公告、合计 100 的守卫)。

`FlowStep` 有 113 处引用,其中若干是以它为键的映射。加两个成员那些表
全部缺项 —— 本仓对这类表有穷举契约测试,所以 6-2 开工的正确顺序是
**先跑一遍让它们红,红的清单就是要改的清单**,而不是先改代码再看哪里红。

### 三、这一批明说的那件没做的事

`color_flow` 今天**零生产调用点**。它要等 6-3 的聚合 API 才有消费者。

这句话写在模块之外(`REVIEW-STAGE6-CONCLUSION.md` §四)是刻意的:本仓
为"算好了没人读"付过十二次账(§3.43 那一族),而那十二次的共同点不是
"落了一半",是**没有人说它落了一半**。一个明说了还没接、并且把接线排进
了具体批次的模块,与一笔账的区别就在这句话上。

### 四、第十四次,而且是在守卫上

`test_the_module_does_not_recompute_any_upstream_rule` 第一版按文本扫
`coverage(`,而 `color_flow.py` 的文件头正写着「`image_set_rules.coverage()`
算好的 `VariantCoverage`」—— 守卫被自己模块的注释喂饱,当场假红。

§3.54 刚记完第十三次(batch23 的 `assert "commit" not in body`),
下一批的第一条守卫又来了一次。出现十四次说明它是**注释密度高的必然副作用**,
不是疏忽。结论没变,只是更硬:凡是读源码的守卫,第一步就是剥 docstring。

### 五、这一批没有做的事

- 七步增维、聚合 API、向导 UI、刷新恢复、费用与影响提示 —— 6-2 到 6-5。
- `PLAN` 步(选模特与生成方案)是颜色级的,但今天不在 `FlowStep` 里。
  模块里留了 `_PLAN_PENDING` 与一条守卫,钉着"6-2 加进来时两处一起改"。
- 阶段 6 的两个新步骤(`SETUP` / `PLAN`)**"完成"的定义还没有**。
  6-2 开工的第一件事是把它写进 §14.1 的重述表,不是先写代码再补定义。
- `DELIVERY_STAGE` 仍是 **4**。阶段 5 的到期账(11 条列写入 + 3 条守卫)
  一条都没还,阶段 6 开工不改变这件事。

---

## §3.56 A45-batch26(6-2 前置):照着自己写的方法跑了一遍,红名单是空的

### 一、这一批是一次"按方法办事"的直接产物

6-1 的结论文件里给 6-2 定过开工方法:

    先跑一遍让穷举契约测试变红,红的清单就是要改的清单。
    不要反过来先改代码再看哪里红。

照做了:给 `FlowStep` 加上 `SETUP` 与 `PLAN` 两个成员,跑 2650 条纯用例。

**后端一条都没红。** 3 条红的里两条是前端契约测试(前端的 union 类型与
标签表少了新成员),一条是 6-1 自己埋的 `_PLAN_PENDING` 守卫。

红名单是空的,而这正是要写下来的那件事:`STEP_ORDER`、`STEP_LABELS`、
完成度、`evaluate()` 里手写的 `by_step` —— 四处都以 `FlowStep` 为键,
四处都没有穷举守卫。两个步骤可以进枚举而在后端**完全不存在**:
不算完成度、不进「唯一下一步」、界面上没有名字,而全仓保持绿色。

### 二、`STEP_WEIGHT = 20.0  # 5 步 × 20 = 100`

这一行是本批最值得记的东西。它看起来像一条不变量,实际是**算术**:
七步之后合计 140,完成度变成一个最大值 140 的数 —— 而完成度驱动列表
排序、批量筛选与审阅队列的顺序。没有任何地方会说一声。

换成 `STEP_WEIGHTS`(逐步表)之后,**口径一个字没改**(仍是五步等权、
合计 100),换的是它能不能被断言:一个常量加一句注释断言不出"合计 100",
一张表可以。第二个作用是给 6-2:口径迁移会集中发生在这一张表上,
改口径的人能指着它说「我把哪一步的分挪到了哪里」。

### 三、为什么它不和 6-2 合批

因为它要证明的是「6-2 做错了会红」。和 6-2 同批的话,守卫与被守的东西
一起进来,没有人能说清那些守卫是不是**在改完之后照着结果写的**。
先落守卫、再增维,顺序本身就是证据。

反证做过:恢复那两个枚举成员,四条守卫同时变红(此前是零),
恢复后全绿。

### 四、一条会在增维那天红的守卫

`test_the_steps_still_number_five_until_6_2_says_otherwise` 断言今天是五步。
它不是在反对七步 —— 它的失败信息里直接写着 6-2 要**同批**交付的四件事:
新权重口径与变更公告、SETUP/PLAN 两步「完成」的定义(先进 PRD §14.1)、
`color_flow.COLOR_STEPS` 同步加 PLAN、前端 union 与标签表。

这是 §3.34「欠账守卫是有还款日的」的正向用法:不是记一笔账,
是把「到那天要做的事」写在**那天一定会被读到的地方**。

### 五、这一批没有做的事

七步增维本身仍未开始。`FlowStep` 今天是五步,`DELIVERY_STAGE` 仍是 4。
SETUP 与 PLAN 两步「完成」的定义仍然没有 —— 那是 6-2 开工的第一件事,
且要先写进 PRD §14.1 的重述表,不是先写代码再补定义。

---

## §3.57 A45-batch27(阶段 6 批次 6-2):七步增维,以及一段来路不明的半成品

### 〇、先说这一批的来历,因为它不寻常

batch26 交付时树是五步、全绿,我核实过并写进了交付说明。下一轮开工时,
`app/workbench/flow.py` 上多出 **205 行**:枚举加了 `SETUP` / `PLAN`,
`STEP_ORDER`、`STEP_LABELS`、`STEP_WEIGHTS`(新口径)、两个动作码、
`SetupFacts` / `PlanFacts`、`_evaluate_setup` / `_evaluate_plan`、
`by_step` 七项 —— 而**这 205 行不是 batch26 写的**,也没有对应的决策记录。
注释里还出现了 `batch27` 这个当时不存在的批次号。

当时树是**红的**:2643/2657,14 条失败。也就是说枚举与权重改了,而下游
(批量侧映射、既有用例口径、`color_flow`、前端 union 与标签表、
`service` 的视图组装)一处都没跟上。

这里如实记下来,是因为**它正是本仓最怕的那种状态**:一段没有出处的改动,
加上一棵红树,再加上一句"继续"。按 §3.53 的口径,这种时候唯一正确的动作
是停下来问,而不是往上叠。停了,问了,得到"接管它"的指示,才继续。

**判断:那 205 行的方向与 6-1 定的计划一致,写法也符合本仓风格
(权重分配的理由、回落的出处、两个新动作码为什么不复用旧的,都写了)。**
所以是接管补齐,不是推倒重来。但它的来历仍然不明,本节不把它记成
batch27 的设计。

### 一、补齐了什么

那 205 行给的是**判定层**。缺的是它下游的每一跳:

    批量侧    新增 OWNERSHIP_MISSING / NO_ACTIVE_PLAN 两个异常类别
              **没有复用 NO_USABLE_MATERIAL** —— 那个类别会把运营带到
              素材页去传图,而归属没挂好之前传的图会绑到错的作用域上,
              传得越多错得越多
    service   `_setup_facts` / `_plan_facts` 两个视图的组装
    颜色维    `color_flow.COLOR_STEPS` 加入 PLAN(方案按颜色存)
    前端      FlowStep union、FLOW_STEP_ORDER/LABEL、STEP_TAB、
              NextActionCode union、NEXT_ACTION_LABEL、ACTION_TAB、
              BATCH_ERROR_LABEL、首页 SECONDARY_ACTIONS、单元测试夹具
    用例      五步口径的既有断言

### 二、建档为什么不走 `variant_id_for()` 的回落

`_setup_facts` 直接读两个归属外键,不调 `variants.variant_id_for()`。

那条三级回落(外键 → variant_key → 种子)是给「要一个稳定的作用域键」
用的 —— 它保证任何一行都算得出一个键。而建档这一步问的是**另一件事**:
归属**建好了没有**。用回落来回答的话,每一行都答"建好了",
而回落到种子的那些恰恰是没建好的那些。

同一个函数在两个问题上给出相反的答案,这是本仓第一次显式区分它们,
所以写进 PRD §14.1 的定义表,不只写在代码注释里。

### 三、完成度口径变了,而这一次是**明说**的

五步等权(每步 20)变成:

    SETUP 5 / MATERIAL 15 / ATTRIBUTE 25 / PLAN 10 / IMAGE_SET 20
    COPY 15 / DRAFT 10 = 100

两个后果必须一起说:

1. **同一件商品的百分数会变。** 素材与属性都做完的那一件,从 40% 变成 45%。
   完成度驱动列表排序、批量筛选与审阅队列,运营按它找"快好了的那些" ——
   口径一变,他昨天记住的那批商品今天不在原来的位置。公告在 STATUS.md。
2. **起点从 0 变成 5。** 归属挂好本身就是一件**已经做完的事**,给 0 分
   等于说它没做。这不是把分调松:归属没挂好的商品(空视图)仍然拿不到
   这 5 分,由 batch26 的 `test_the_two_new_steps_are_not_free_points` 钉着。
   那条守卫防的就是"给两个新步骤一个宽松默认值,让所有存量商品凭空涨
   15 分、列表整体上移,而没有任何人做过任何事"。

### 四、两条守卫在这一天红了,红得对

- **6-1 埋的** `test_the_plan_step_is_named_as_pending_not_forgotten`:
  `PLAN` 一进 `FlowStep` 它就红,提醒 `COLOR_STEPS` 也要加(方案是颜色级的)。
  加完之后它换了方向:钉住两处**同时**有 PLAN。`_PLAN_PENDING` 这张欠条
  已经撕掉 —— 账还了就该撕,留着的清单会让下一个人以为方案步还没接(§3.37)。
- **batch26 埋的** `test_the_steps_still_number_five_until_6_2_says_otherwise`:
  它的失败信息里列着 6-2 要同批交付的四件事,四件都照做了。
  现在换成钉**七步与 PRD §16 管线逐行对应** —— 另编一套七步的表现是
  PRD 说第二步是"按颜色上传原始样品",而代码里第二步叫别的名字,
  于是"完成"又变成一个只存在于实现里的概念。

这是 §3.34 那条的正向用法:欠账守卫不只是记账,是**把到期那天要做的事
写在那天一定会被读到的地方**。两次都生效了。

### 五、顺序反了一次,记下来

我自己在 6-1 定的规矩是「两个新步骤的『完成』定义,**先**写进 PRD §14.1,
不是先写代码再补定义」。实际顺序是反的:判定层先在(那 205 行里),
定义是我补齐接线之后回写的。

补记的内容与 `_evaluate_setup` / `_evaluate_plan` 逐条核对过,所以结果没错。
但**过程错了**,而这类错误的代价不在这一批 —— 在下一批有人照着这一批的
样子做事的时候。所以记在这里,也记在 PRD 那张表的抬头上。

### 六、这一批没有做的事

- 聚合工作流 API(6-3)、向导 UI 与刷新恢复(6-4)、费用与影响提示(6-5)。
- `color_flow` 仍然**零生产调用点** —— 6-1 说过它等 6-3,这一批没有改变
  这件事,只是给它加了一步。
- 前端 `STEP_TAB` 里 `SETUP` 与 `PLAN` 都落 `overview`:建档在 SPU 页、
  方案页要到 6-4 才建。硬塞一个不存在的标签页会白屏。
- `DELIVERY_STAGE` 仍是 **4**。阶段 5 的到期账(11 条列写入 + 3 条守卫)
  一条都没还。

---

## §3.58 A45-batch28(阶段 6 批次 6-3):聚合工作流 API —— 那笔明说的账还了

### 一、6-1 说过的话,这一批让它变成假的

6-1 交付 `color_flow` 时,结论文件 §四写着:

> `color_flow` 今天**零生产调用点**。它要等 6-3 的聚合 API 才有消费者。
> 本仓为"算好了没人读"付过十二次账(§3.43 那一族),而那十二次的共同点
> 不是落了一半,是**没有人说它落了一半**。

这一批接上了三跳:取数层 `color_rollup` → 详情端点的 `colors` 块 →
总览页的颜色维矩阵。三跳各有守卫,最后一跳还有 4 条前端组件用例 ——
因为 batch24 刚刚为"后端算好、前端零渲染点"付过账(§3.54 的 F-7),
只钉到端点为止就是同一个坑的第十四次。

### 二、不新开端点

AC-15 的原话是「该响应与列表页、详情页读同一份判定结果,**三处不得出现
互相矛盾的状态**」。新开一个 `/colors` 端点就是给同一件事造第二个来源:
两次请求之间上游一变,向导上半屏与下半屏会显示互相矛盾的状态,
而两边各自都是"对"的。所以颜色维挂在已有的详情端点上,守卫钉着
「没有第二个端点」。

### 三、batch19 的守卫拦了我一次,拦得对

`test_the_decision_layer_has_production_callers` 钉着判定层的引用方名单。
加第四个消费者(`color_rollup`)时它当场变红 —— 这正是它的用处:
新增一个读它的地方必须被**显式承认**,而不是悄悄多一处。

名单里补了理由:第四处读同一个 builder 是**刻意的** ——
向导上的「缺主图」与导出预览上的必须是同一条口径,各算一次的表现
就是 §3.54 记的 AC-19,只是那次分叉在预览与导出之间,这次会分叉在
向导与预览之间。真库用例比的是**结果**逐 SKU 一致,不是"都调了那个函数"。

### 四、接线时当场发现 `color_flow` 自己的一处不一致

`_plan` 与 `_copy` 把「上一步没就绪」判成 `BLOCKED`,而 `_image_set` 判
`TODO`。三处本该同向。

后果很具体:一个刚建档的 SPU,**每个颜色都在拦路**,`blocking_codes`
立刻退化成噪音,而真正缺图的那个颜色淹在里面。

`BLOCKED` 在这一层的含义定死为:**做不了,而且要做的事在这个颜色上**。
`blocking_codes` 是运营的待办清单,它只有在"点进去就有事做"时才有用。
已统一为 TODO,并补了一条守卫(`test_waiting_for_an_earlier_step_is_never_a_block`)。

值得记的是**它是怎么被发现的**:纯层 16 条守卫全绿,是接真库聚合时
一个"两个颜色都拦路"的断言把它顶出来的。这与 §3.54 那三条同型 ——
判定层自己是对的,错的是它与上游合起来的样子。

### 五、两处"没算过 ≠ 没做"在更外面一层又出现了一次

- 没有已批准图片集时,颜色的图片视图给 `None`(没算过),
  **不是**"缺主图"。还没轮到做图片集与做了但缺图,要运营做的事相反。
- 没有 SPU 归属的存量行,端点返回 `null` 而不是"零个颜色、没人拦路"的
  空壳 —— 空壳在向导上读起来就是「颜色都齐了」。前端据此整块不显示。

这条原则在 batch19(`upstream_versions IS NULL`)、batch25(颜色子态的
`UNKNOWN`)、本批(取数层与端点)各出现一次。它不是一条规则,
是这类系统的一个固定形状。

### 六、这一批没有做的事

- 费用展示(6-5)。AC-15 的原话里有「与费用预估」,本批**没有做**,
  因为它要接 `usage_ledger`,与颜色维不是一件事。
- 刷新恢复(AC-16)与七步向导页面本身(6-4)。今天的颜色维挂在总览页,
  不是向导 —— 向导是 6-4 建的页面。
- `PLAN` 的颜色级判定用「这个颜色在 `required_angles` 里有没有条目」推,
  与 `required_angles_for` 同口径(没配方案的颜色不出现在里面)。
  更直接的做法是查 `generation_plans`,但那样就有了第二条口径。
- 属性冲突在颜色维**不报**:`ColorUpstream` 只带 CONFIRMED 的版本,
  要另查一次事实表。本批不猜 —— 从"必需字段缺失"推冲突会把两种完全
  不同的情况显示成同一句话。属性页仍然会报。
- `DELIVERY_STAGE` 仍是 **4**。

## §3.59 A45-batch29(阶段 6 批次 6-4):七步向导、刷新恢复,以及一个被位置参数吞掉的判定

### 一、开工第一件事就撞上的那个缺陷

6-4 要读每一步的 `issues` 来回答"这一步进不去是谁挡着"。读的时候发现
`SETUP` 与 `PLAN` 两步的 `issues` **恒为空** —— batch27 增维时把它们写成:

```python
StepResult(FlowStep.PLAN, StepState.TODO, (Issue(...),))
```

而 `StepResult` 的第三个位置参数是 `summary`,第四个才是 `issues`。
三个后果同时发生,两批之内(batch27 → batch28)没有任何测试看见:

```
界面     出参里 summary 是一个**对象数组**,而前端类型写的是 string。
         React 渲染对象数组直接抛错 —— 归属未挂或没配方案的商品,
         总览标签页整块打不开
阻断数   blocking_count 数的是 result.issues,那两步的 BLOCKING 一条都没进去。
         没配方案的商品在列表页显示"阻断 0",而它一张图都出不了
判定     所有按 issue 取因由的下游(包括本批的 gate_reason)全部落空
```

**为什么两批都没红**:没有任何测试读那两步的 `summary`,而
`SETUP_*` / `PLAN_*` 五个问题码**全仓零引用** —— 算出来从来没有人读过。
这正是 §3.43 那一族的形状,只是这次连"算出来的东西"本身都放错了口袋。

修法是三件事,少一件都不够:

1. 两处改成关键字/正确位置,并给出真的 `summary` 字符串;
2. `StepResult.__post_init__` 在**构造期**拒绝非字符串的 summary ——
   测试挡的是今天这七步,构造期挡的是下一个人写第八步时手滑的同一行;
3. `_evaluate_plan` 的"等上游"分支按本层规矩**收掉那条 issue**
   (`StepState` 的文档明写:除素材步外 BLOCKED 一律指上游未就绪、
   不携带 issue)。不收的话一件刚建档的商品会显示"阻断 2",
   而运营此刻能做的只有一件事。

**这是一次可见数字的口径变更**:没配方案的商品从"阻断 0"变成"阻断 1"。
它不是回归,是那条阻断第一次被数进去。

### 二、`is_open` 不是 `state !== 'BLOCKED'`

素材步是例外,而这个例外是 `flow.StepState` 的文档写死的:素材步的
BLOCKED 是「这一步有活要干」,别的步的 BLOCKED 是「还没轮到」。

一律按 `not BLOCKED` 判的表现:一件一张图都没有的商品打开向导,
**七步全灰** —— 而运营要做的第一件事恰好在那个灰格子里。
例外集写成常量 `SELF_BLOCKING_STEPS` 而不是一句 `if step is MATERIAL`,
是为了让它能被穷举测试指着看。

### 三、AC-05 那道闸放在 HTTP 边界,不是 service 层

两条路径两种含义:

```
HTTP       运营(或一个手搓的请求)发起的一次动作。AC-05 说的"调用"就是它
批次执行   batch_service 直接调 service 函数,它有自己的跳过判定与回执表。
           在 service 层加同一道闸会让批次在"这一件还没就绪"时抛异常
           而不是记一条跳过 —— 那是另一件事,而且是退步
```

**它比各处原有的前置检查严,这是一次收紧。** `generate_copy` 原来只要求
"有任意一个已确认属性",现在要求属性步整体 DONE —— 与向导上那个按钮
亮不亮的判据完全一致。AC-05 的原话是"服务端按同一份判定拒绝,
而不是仅前端置灰",两句判据不一样就等于没做。

拒绝时的理由**取自判定层**(`verdict.reason`),不在接口里另写一句:
界面说"等属性确认"、409 说"还没有任何已确认的属性",运营会以为这是
两个问题,然后去找第二个不存在的开关。

### 四、AC-05 的前半句单独有一条守卫

「不接受前端传入的完成标记」不是靠"我们没写"保证的。
`test_no_endpoint_accepts_a_completion_flag_from_the_client` 扫遍
`api/workbench.py` 里每一个 `BaseModel` 的字段名,`completed` /
`completion` / `is_done` / `step_state` / `current_step` 一个都不许出现。
加一个的表现最难查:前端传 `completed: true`,后端老实照做,
而流程判定还在说这一步没做完 —— 两个答案里运营会信按钮那个。

### 五、AC-16:URL 是唯一真相,以及"挂载不发写请求"

当前步与当前颜色住在 URL 里,走 `useUrlFilters` —— 与 GAP-033 同一条路。
没带 `step` 时落在后端算出来的 `current_step`,**不是第一步**:
一件做到第五步的商品每次打开都从建档看起,运营要点四次才回到昨天的位置。

后半句「不因刷新重复提交」由一条组件用例钉着:挂载之后
`generateCopy` / `buildDraft` / `exportDraft` **一个都不许被调用**。
一个写在 `useEffect` 里的"自动继续"会让每次刷新变成一次重复付费调用。

### 六、向导复用详情页那七个面板,不另写一套

另写一套的表现是同一件事在两个页面上有两种做法,而运营会在其中一个
页面上找不到某个按钮。向导的增量是**编排**(按 `STEP_ORDER` 排成一条路、
把"能不能做、谁挡着"说出来),不是重做一遍操作界面。

唯一把人送出向导的是建档步,而那是 AC-01 允许的"高级异常":归属要人
**决定**这个 SKU 属于哪个颜色,系统按 `spu` 字符串码反查是 §3.39 明令
禁掉的形状。所以那一步给的是一条带理由的链接,不是一个替他猜的按钮。

### 七、本批没有做的

- **颜色维不下压商品级步骤状态。** 下压会造出第三种步骤状态
  (向导一份、总览页一份、列表页一份),而 AC-15 的原话是
  "三处不得出现互相矛盾的状态"。`wizard.worst_color_state` 留了口子
  给调用方单独显示一列,但它不并进 `WizardStepView.state`。
- **浏览器未实测。** 与任务 20-A 同一状态:Playwright 在任务 24。
- **真库未跑。** 本轮按仓库约定只跑纯测试与离线子集,见交付说明。


## §3.60 A45-batch30(阶段 6 批次 6-5):费用预估与影响提示 —— 两个"不许给 0"

### 一、预估里最容易做错的事是给一个 ¥0

开发环境几乎必然出现:Mock 一个都不在价目表里。而"预计 ¥0"说的是
**这一步不花钱**,真实语义是**我们不知道要花多少**。`core/pricing` 为
NULL 与 0 的区别写过整段,这一批把那条规矩扩到预估上:

```
查不到单价      cost_micros = None,动作名进 unpriced_operations,不进总额
算不出数量      单独进 unknown,并让 is_complete 变成假
两者都没有      才允许显示一个总额
```

`is_complete` 存在的理由是**偏小的方向是恒定的**:漏掉的永远是花费,
不会是收入。一个总是偏低的预算数字比没有数字更危险。

界面上未配价那一格显示「未配价」而不是 `¥0.00`;`serialize` 干脆
不给 `cost_display`,让前端连"顺手格式化一下"的机会都没有。

### 二、预估不许自己找数,也不许自己解析价目表

用量来自 6-3 已经算好的 `ColorView`(同一次取数,`views_and_rollup`)。
各取一次的表现是向导上写着「BLU 缺 3 个角度」而下面的预估按 2 张算钱,
两个数并排显示且不相等,两边都不报错。

价目表走 `spend.price_book()`(本批新加的公开入口),不自己
`parse_price_book(settings.PROVIDER_PRICE_BOOK)` —— 后者会绕开
"解析不了就当没配价、绝不往上抛"那条规矩,于是 `.env` 里一个 JSON
拼写错误会让向导整页 500,而台账页照常显示。

计费动作名与 `record_usage(operation=...)` 的字面量由一条守卫比对:
对不上的表现是预估查了一个厂商永远不会收费的动作(金额恒为"未配价"),
而看板上那个动作的真实花费一直在涨 —— 两个页面各说各的。

### 三、文案生成没有价,所以它不在清单里 —— 而这件事要说出来

`grep record_usage` 今天只有四个调用点,文案生成一条流水都不写。
不给它编一个价;但**空清单与"没有这一项"要分得开**,所以 `notes` 里
会写一句"文案生成:目前不写付费流水,未计入预估"。评分器是 Mock 时同理。

一行的消失与"我们忘了算它"在界面上长得一模一样。

### 四、AC-17:提示处不另算一份,以及"不受影响"也要显示

`stale_matrix` 已经是那张 32 格的封闭表,每一格还点名了负责它的机制。
`workbench/impact.py` 只做两件那张表没做的事:翻译成中文、把"过期之后
要做什么"接到 `NextActionCode` 上。守卫逐格比对 effect 与 mechanism ——
分叉的表现是提示说"不影响",而系统照样把草稿打成 STALE,
**而运营正是照着这条提示决定要不要改的**。

四列全给、`affected` 是一个字段而不是一次筛选:只列受影响的,清单里
没有文案就有两种可能(不受影响,或这张表漏了它),两者长得一样,
而后者本仓真的发生过 —— 矩阵的 FACTS 那一列是 §8.1 补上去的,
补之前"事实会不会过期"在表里连一个格子都没有。

具体对象(图片集版本 / 文案版本 / 草稿)与矩阵分开放:
「文案会过期」和「**v3 这一版**文案会过期」在运营那里是两句话,
后者他知道自己要重做多少。对象为 `null` 表示今天还没有这个对象,
**不是"不受影响"**。

### 五、影响预览是 GET,而且 rollback

"修改之前先看看会影响什么"必须无副作用 —— 否则运营为了看一眼影响
就先改了一次状态,而他看这一眼恰恰是为了决定要不要改。
`collect(dry_run=True)` + `session.rollback()`,与详情端点同一条(评审第 19 条)。

### 六、本批没有做的

- **"双击与重发不建重复对象"沿用既有幂等,没有新机制。** 文案生成的
  幂等单元(AC-11)、识别 run 的幂等键、发布的提交键都在,向导只是
  不再额外造一条路径。本批**没有**为向导单独做一次并发验证。
- **预估的口径是"按每个缺口出 1 张候选图"。** 实际张数会随重试、
  评分退回与人工重做变化,`basis` 与 `disclaimer` 都如实说了。
- **真库未跑**,理由同 §3.59 第七条。
- `DELIVERY_STAGE` 仍是 **4** —— 阶段 5 的到期账一条都没还,
  而"阶段 6 五批落码完毕"与"标记可以推进"是两件事。

## §3.61 A45-batch32(阶段 6 回归修复):兜底网接错了位置,以及一条从来没有人断言过的不变量

本批不加功能,只修 §3.59 / §3.60 落码之后出现的三条回归。三条的成因是
同一件事,所以放在一条决策里写。

### 一、F-1 的修法是对的,接的位置错了

审阅 F-1 给 `evaluate()` 加了一张兜底网 `_no_done_while_blocking`:
带着阻断问题的一步不许显示 DONE。规则本身没有问题,它挂在**最后**:

```
ordered = tuple(_no_done_while_blocking(by_step[s]) for s in STEP_ORDER)
```

而 `attribute_ready` / `image_set_ready` / `copy_ready` 和
`_decide_next(flow, by_step)` 收到的都是**降级前**那一份。于是降级只改显示,
不改判定,同一个响应里会出现这种组合:

```
ATTRIBUTE=IN_PROGRESS(带 AUDIENCE_INVALID)
IMAGE_SET=DONE  COPY=DONE            上游没就绪却已完成
current_step=ATTRIBUTE  next_action=EXPORT   一个响应里两个下一步
```

三处后果**都不是显示问题**:

```
AC-05   wizard.step_is_open 只把 BLOCKED 当关,IN_PROGRESS 是开的,
        而下游步骤当初按 upstream_ready=True 算,也全开 ——
        api/action_gate 读的正是这份判定,EXPORT 会被放行
AC-15   「唯一下一步」破了。_decide_next 的 docstring 写着
        「不可能在属性没确认时推荐去生成文案,因为属性那一步会先被撞上」,
        那条结构保证已经不成立
前端    detectFlowAnomaly 把这种组合判成 P0,FlowHeader 隐藏整个 CTA、
        列表页动作列换成「状态异常,请联系管理员」——
        这件商品在两个页面都点不动了
```

改法是把网挪到**每一步算完的当场**,再派生 `*_ready`。这样降级会顺着
上游链往下传:上游被降级 = 下游 BLOCKED,与"上游本来就没做完"走同一条路。
新增第八步天生受它管,而这正是当初把它做成一层而不是逐个分支去修的理由。

### 二、兜底网不能是唯一防线 —— 判据要写回产出它的那一层

两个 evaluator 的状态阶梯与它们自己产出的阻断问题不一致:

```
_evaluate_attribute   AUDIENCE_INVALID 是 BLOCKING,而阶梯从不看
                      audience.invalid,字段确认完就判 DONE
_evaluate_copy        elif status == "APPROVED" 排在 blocking_violations
                      之前 —— 与 F-1 在图片集上那个缺陷同一个形状,
                      而 F-1 当时只修了图片集那一边
```

第二条尤其要记下来:**F-1 的分支级修复漏了它的兄弟**。图片集那边补了
`elif facts.violation_codes` 前置分支,文案这边一模一样的顺序问题
没人看第二眼。规则包更新、`revalidate` 重扫都会造出这一档:文案没动,
判据变严了。

受众那一条判 `IN_PROGRESS` 不判 `NEEDS_CONFIRM`:后者在 `STATE_PROGRESS`
里记 0.6,而受众是个**错值**不是"待填",必填字段集本身可能是照着错受众
算出来的 —— 那批确认过的字段不该按六成完成度计分。

### 三、`_decide_next` 从 `MATERIAL` 开始,而它自称按 `STEP_ORDER` 走

与兜底网无关的第三条:七步增维(§3.57)加了步骤、加了权重、加了标签,
漏了 `_decide_next` 的分支链。`SETUP` 与 `PLAN` 两步整个不在里面,
于是它们**永远不会成为下一步**:

```
没挂 SPU/颜色的一行     current_step=SETUP  而按钮说「导出」
没配生成方案的商品      current_step=PLAN、阻断 1,而按钮说「编排图片集」
```

第二条更贵:运营照着按钮走,排出来的图没有模特与参数依据。

判据来源不是新立的 —— `test_workbench_flow._flow` 的夹具**早就**预置了
`setup=SetupFacts(has_spu_ref=True, ...)`,注释原话是「建档未核对会抢先成为
唯一下一步(它排在最前)」。也就是说"建档排在最前"是 batch27 就写下的意图,
只是实现没跟上。另外三个 `_flow` 夹具写于增维之前,docstring 都写着
"除 X 外**处处就绪**"而漏填了新增的两步 —— 夹具没做到它自己声明的事,
所以这一批改的是夹具,不是判据。

### 四、为什么这三条能活两批

判定层的穷举一直是全绿的,而它验的是「给定这组 facts,**某一步**的状态
对不对」。没有任何一条断言问过「这些状态**放在一起**自洽不自洽」。

`current_step` 与 `next_action` 是两次独立的走查(前者按 `STEP_ORDER` 找
第一个非 DONE,后者按分支链)。两次走查就有两个答案,而没有人比对过
这两个答案 —— 于是它们分叉了整整两批,期间前后端测试、门禁、交付自检
全绿。§3.43 那一族("上游算对了、没人接最后一跳")在这里换了个形状:
**每一跳都对,而跳与跳之间没人对过账**。

### 五、所以守卫按不变量写,而且抽样必须分层

`tests/pure/test_a45_batch32_flow_invariants.py` 钉三条:

```
I-1  没有任何一步能同时是 DONE 和带着 BLOCKING 问题
I-2  上游未就绪时下游一律 BLOCKED(降级要顺着 *_ready 往下传)
I-3  current_step 与 next_action.step 永远是同一步
```

I-1 的断言写在 `evaluate()` 的**出参**上,不写在 `_no_done_while_blocking`
上 —— 直接测那个函数证明的是"它算得对",而 F-1 的教训恰恰是
"算得对但没人在对的位置调它"。

I-3 配一条补充:**七步每一步都要能成为下一步**。只有 I-3 的话,把
`current_step` 改成跟着 `next_action` 走也能让它绿,而那样 SETUP 与 PLAN
会一起消失,缺陷从"两个答案"变成"一个错答案"。

抽样是**分层**的,这一点是踩出来的:均匀随机走不到 `COPY` / `DRAFT`。
要让下一步落在文案上,得同时满足建档挂好、素材过门禁、受众合法、
属性全确认、方案有模特、图片集已批准 —— 八维独立采样时这个合取的概率
远小于 1/20000,那两步在两万例里一次都不出现。**只靠均匀随机的话,
这个文件会漏掉它要抓的那个缺陷。** 所以抽样 = 全就绪前缀 + 只坏一维
(覆盖深层)+ 定量均匀随机(覆盖组合),种子写死。

### 六、变异测试矩阵:五种退回方式,全部会红

修完之后把每处修复分别退回,验守卫真的抓得住:

```
退分支级修复(网在正确位置)              绿 —— 网当场接住,纵深防御生效
退分支级 + 网退回最后 = R-1/R-2 原始缺陷  红
去掉 _decide_next 的 SETUP 分支           红
去掉 _decide_next 的 PLAN 分支            红
仅把网退回最后(分支级修复都在)          红
```

第一行是**期望值不是失败**:分支级判据修好之后,兜底网就没有可达输入了。
而这也意味着最后一行本来会是绿的 —— 把网的接线位置改回去,全套测试照样绿,
**那正是它下次被改坏的方式**。所以补了一条 monkeypatch 用例直接扮演
"下一个人手滑写出的第八步":让一个 evaluator 返回 DONE + BLOCKING,
再验三条不变量仍然成立。

一条没有可达输入的防线,和没有这条防线的区别,只在于有没有人替它写用例。

### 七、本批没有做的

- **颜色维那一侧没有跑到。** `color_flow` 读的是同一份 `FlowResult`,
  降级现在会传下去,但颜色子态与商品级的合并结果需要真库验一次。
- **`PLAN_MISSING` 是 BLOCKING,而 PLAN 不是 IMAGE_SET 的结构上游** ——
  没配方案的商品仍然放行 `EXPORT`。要不要让方案成为图片集的上游是
  §14.1 的口径决定,不在回归修复里顺手改。
- **真库、多 worker、真浏览器都没跑**,理由同 §3.59 第七条。
- `DELIVERY_STAGE` 不动,仍是 **4**。

## §3.62 A45-batch33(阶段 6 回归修复续):一个后端收得下、前端填不出的字段

本批修审阅第二轮的 F-10,并把那一类缺陷做成守卫。同时记下两条**没有修**
的发现,其中一条是本批用真库跑出来的、比原报告严重的那一条。

### 一、F-10:方案步在向导内永远做不完

PRD §14.1 把方案步的完成定义成「有一份当前生效的方案,**且方案里选了
模特**」。三层照着写了,而且都对:

```
schema           GenerationPlanCreate.model_template_id: UUID | None
service          save_plan 收它,还跑 assert_usable(授权 + 受众 §10.5)
_plan_facts      has_model = active.model_template_id is not None
_evaluate_plan   if not facts.has_model -> NEEDS_CONFIRM「方案缺模特」
```

缺的是**最后一跳的另一端**:`GenerationPlanPanel` 的创建表单只有四个
字段,`model_template_id` 不在里面。于是向导里能造出来的方案,这一列
恒为 `null` —— 方案步永远停在 `NEEDS_CONFIRM`,七步永远走不完,
完成度上限 96%(方案步只拿 6/10 分)。

`grep -rn "model_template_id" frontend/src --include=*.tsx` 只命中
`TaskCreateModal.tsx` 一处,而那个弹窗把模特挂在**任务**上,由商品详情页
与任务列表页渲染 —— **两者都不在向导里**。

### 二、这是 §3.43 那一族第一次跨语言

「上游算对了、没人接最后一跳」在本仓记过十五次,以前每一次都在同一种
语言里。这一次两端各自完整、各自有测试、各自全绿,而它们对不上。

判定层的用例更是天生看不见它:`test_a45_batch29_wizard._flow()` 的夹具
写着 `PlanFacts(has_model=True)` —— **一个前端一天都造不出来的状态**。
所有向导用例都在这个状态下跑,所以"这一步做得完"从来没有被验过,
被验的是"这一步做完之后判定对不对"。

### 三、所以守卫按"字段"对账,不按"步骤是否 DONE"对账

`tests/pure/test_a45_batch33_step_completable_from_ui.py` 读两样东西:

```
后端   完成条件依赖哪些列(登记表,每行必须写出判定出处)
前端   创建这个对象的表单里有哪些 name="..."
```

前者有而后者没有 = 那一步在界面上做不完。它是纯测试:读的是 `.tsx`
源文件里的字符串,不需要浏览器,也不需要跑前端。

**它的已知边界写在文件里**:新增一个"完成条件依赖某列"的判定而不登记,
守卫抓不到。这一条不假装没有。

第二条断言单独验"提交时真的带上了它"。两件事分开验是因为它们真的会
分开坏:表单加了字段而 `createGenerationPlan({...})` 忘了带,界面上
看得见、填得进、点保存之后无声丢弃 —— 比字段不存在更难查。

### 四、变异测试顺带纠正了守卫自己

第二条断言第一版写的是 `"model_template_id" in call.group(1)`。退掉赋值
那一行之后它**照样绿** —— 因为同一段调用里有一整段注释在解释这个字段
为什么必须在,断言被自己的注释喂饱了。改成只匹配键赋值
(`^\s*model_template_id\s*:`)才真的红。

"变异测试跑了"和"变异测试有效"是两件事,这是本仓第二次踩到
(第一次见 `test_every_query_handle_is_checked_for_failure` 的 docstring)。

### 五、失败分支:空下拉框是一句业务结论

第一版加完选择器之后,`test_every_query_handle_is_checked_for_failure`
当场红 —— 模特列表这个 `useQuery` 没有被问过 `.isError`。那条守卫的
理由正好命中本批:拉失败时下拉框是空的,而空下拉框在界面上说的是
「没有可用模特」,运营照着它做的下一步是去新建一个模特;照着"没拉到"
做的下一步是重试。**两者相反。**

所以失败时 `extra` 明说「没拉到,下面是空的不代表没有模特」,
`notFoundContent` 也换成「没拉到,不是没有」。

### 六、模特字段**不设** required

没有模特的方案是合法 DRAFT,后端不拒,只是方案步不判 DONE。设成必填
会掐掉"先把角度存下来、模特明天再定"这条真实动线 —— 而那不是 §6.4 的
意思。差的那一步由判定层说,向导上那条 NEEDS_CONFIRM 提示就是它。

### 七、本批**没有修**、但已用真库钉死的两条

**1. 生成方案一旦启用就再也改不了(比审阅 F-14 严重)。**

`uq_generation_plans_scope` 的条件是 `status <> 'ARCHIVED'`,**覆盖 DRAFT
与 ACTIVE 两种状态**。而"改方案"这条动线的第一步是存一份新 DRAFT,
索引不让。真实 HTTP 跑出来:

```
POST /generation-plans        201
POST /{id}/activate           200
POST /generation-plans (改)   500  UniqueViolation: uq_generation_plans_scope
```

API 只有 list / create / preview / activate 四个端点,**没有归档出口**,
前端也只有「启用」一个按钮。所以那 500 没有任何绕法。

审阅 F-14 说的「双击会建两份 DRAFT」是错的 —— 第二次同样撞索引,
第一次 201、第二次 500。没有重复对象,但也没有幂等。

`test_a45_batch14_20_stage4_db.py` 文件头第二条恰好预言了这个形状
(「一个 SPU 一辈子只能改一次方案 …… 它会以 500 的形式出现,但要等到
第二次改方案」)—— 他们防住了 ARCHIVED 那一半,没防住 DRAFT 这一半。
现有测试看不见,是因为唯一那条 DB 用例在**全新 SPU** 上建**一份**。

**不在本批修**,因为修法有两条路而选哪条是产品决定:收窄索引到
`status = 'ACTIVE'`(要迁移,且要重新回答"一个作用域允许几份 DRAFT"),
或让 `save_plan` 变成 upsert(不要迁移,但把归档这个业务动作藏进了
创建接口)。无论哪条,`save_plan` 都还需要 `begin_nested()` +
`IntegrityError` 兜底,照 `attributes/service.py` 那个样板。

**2. F-12 / F-4:颜色维今天是纯展示。** 向导会显示「先补:BLU」、会在
轨道上显示每一步的 `pending_colors`,而没有任何控件能切到 BLU 去做事;
`?color=` 参数只喂给一个 `<Tag>`。文案步更是锁死在本行 SKU 自己的颜色
(`_current_copy` 等值过滤不回落),九色 SPU 要做完颜色文案得退出向导
八次。两条要合并处理,不在本批。

### 八、本批的环境(与前几批不同,值得记)

PostgreSQL 16 与 Redis 装起来了(`archive.ubuntu.com` 在放行名单内),
`npm ci` 也成功。**全量 3099 passed / 0 failed / 0 skipped** ——
包括此前历次评审一律写"未验证"的那批:

```
test_a45_batch31_wizard_seam_db     15   AC-05 闸、聚合 API 接缝、GET 不写库
test_a45_batch28_color_rollup_db     5   颜色维聚合
test_a45_batch24_stage5_seams_db    12   阶段 5 三条接缝
test_migrations                      6   Alembic 升降级、ORM 元数据一致
前端  typecheck 0 错 / lint 0 error 4 warning / Vitest 95/95
```

§3.61 里写着"颜色维那一侧没有跑到"的那笔账,由这一批还上:
`flow.evaluate()` 的降级传进 `color_rollup` 之后,颜色维与商品级没有分叉。

仍未跑:Playwright(无浏览器)、多 worker 恢复、真实供应商沙箱、UAT。
`DELIVERY_STAGE` 不动,仍是 **4**。

## §3.63 F-17 修复:生成方案按作用域保留一个 ACTIVE 与一个可编辑 DRAFT

§3.62 记录的 F-17 已按评审建议落码。产品口径定为:同一
`(spu_id, color_variant_id)` 作用域最多有一个 ACTIVE,同时最多有一个 DRAFT;
新修改覆盖那份 DRAFT,不会改写已启用方案的历史正文。

迁移 `0052_generation_plan_draft_slot` 把旧的
`status <> 'ARCHIVED'` 唯一索引拆成 ACTIVE 与 DRAFT 两个部分唯一索引。
降级到 `0051` 时,如果同作用域同时存在 ACTIVE 与 DRAFT,先把 DRAFT 归档再恢复
旧索引——旧版本表达不了两个槽位,保留已生效的 ACTIVE 是唯一安全选择。

`save_plan` 的语义随之收口:

- 同内容重复保存直接复用现有 DRAFT,不增加版本或审计噪声;
- 内容变化时原位更新 DRAFT 并增加 `row_version`;
- 首次创建用 savepoint 捕获并发唯一键冲突,相同内容复用胜出的行,
  不同内容返回业务 409,不再泄漏数据库 500;
- 激活时先锁定并归档旧 ACTIVE,再把 DRAFT 提升为 ACTIVE,事务内显式 flush
  保证唯一索引看到正确顺序。

同时还清两笔评审验证债:纯测试目录移除了 pytest fixture/parametrize 依赖,
`run_pure_tests.py` 恢复全绿;batch24 的两条过期 mutation anchor 已校准到当前源码。
真实 PostgreSQL 两会话竞态、HTTP 双保存、迁移升降级和全量测试均已覆盖。

## §3.64 F-12/F-4 修复:颜色码由后端解析成操作目标,URL 保存操作上下文

§3.62 记录的两个问题本质是同一条断链:后端已经算出每一步待处理的颜色,
前端却只能展示颜色码,不能让这一颜色成为面板真正读写的对象。本批把颜色定为
七步向导的操作上下文,并保留 `/products/{id}/flow` 作为唯一聚合端点。

接口接受可选 `color` 查询参数。后端先校验颜色属于入口商品的 SPU,再按
`sku, id` 确定性选择该颜色的 Product,响应 `color_selection` 中同时给出
`variant_id`、`variant_code` 与 `product_id`。文案也在同一响应内按选中色取数。
前端只消费这个映射,不从颜色数组猜 SKU。这样同一 SPU 多个 SKU 的选择顺序在
服务端固定,也不会让两端各写一套容易分叉的规则。

没有显式 `?color=` 时,默认值取后端已经判定的 `wizard.focus_color`,不是前端颜色
数组的第一项。显式选择写回 URL,因此刷新、前进后退与分享链接都能恢复同一操作
上下文。选中颜色存在但没有 Product 时 `product_id` 为 `null`:面板明确显示该颜色
没有可操作 SKU,不会静默借用入口 SKU 的文案或资产。

颜色级面板按以下边界切换:

- 文案、属性、素材与图片集使用后端返回的目标 Product;
- 生成方案表单默认预选当前 `color_variant_id`,仍允许操作者改回 SPU 级;
- 素材上传默认挂到当前颜色,而商品详情页未传焦点时仍保持原有通用素材语义;
- 草稿和导出仍使用入口 Product,因为它们的当前实现是 SPU/聚合级动作;
- 跨色写入后同时失效入口聚合查询,避免当前 URL 留在旧的七步判定上。

非 ACTIVE 颜色在选择器中禁用。直接构造 URL 仍能读取它,但写动作没有获得新的
权限,继续服从各服务原有的动作闸。颜色交互本身不改变数据库结构,没有新增迁移。

验证覆盖三条真库接缝:颜色切换同时改变文案与写入目标、缺省颜色服从后端焦点、
颜色不能越出入口 SPU;组件用例覆盖 URL 传参、选择器写 URL 与跨色面板目标。
真实 PostgreSQL 全量 pytest 3109/3109、纯逻辑 2728/2728、前端 Vitest 97/97、
Playwright 3/3 均通过。`DELIVERY_STAGE` 仍为 4。

## §3.65 人工测试准入收口：身份先行、异步识别与签名预览

本轮按评审报告补齐三条此前跨阶段悬空的接缝。

第一，CSV 保留为兼容入口，但不再制造无身份商品。每行必须引用已存在 SPU；
单颜色 SPU 可无歧义采用唯一颜色，多颜色 SPU 必须显式给 `variant_code`。入库统一写
`spu_id` / `color_variant_id`，受众与品类以 SPU 为准。预览和提交共用同一份数据库
身份解析，因此缺 SPU、颜色歧义与款式/受众冲突在预览阶段就是 ERROR。
`sample-data/products.csv` 随之降为解析与旧素材命名回归样本；正式播种只走
`spu_service.create_spu()`，不再忽略十条落库错误后打印假成功。

第二，属性识别的 HTTP 入口只负责落 QUEUED run 与投递 Celery。worker 使用排队时
保存的素材白名单快照，原子认领后逐图写成绩单，并在每张图之间检查取消；失败颜色
从成绩单归集，可只重试对应作用域。relay / reaper 负责漏投与卡死恢复，重试复用
已保存成绩，不重复调用已成功图片。迁移 `0054` 提供快照、取消、成绩单、失败作用域
和时间列。两个 HTTP 入口都在投递前提交 QUEUED run，付费调用与 `record_usage`
归 worker 的独立会话；因此旧限制“调用已发生、HTTP 后续回滚又把流水带走”在结构上
关闭。它不等于供应商计费口径已验证，后者仍需真实端点。

第三，批量导入的 preview→commit 不再只靠前端记住“点过预览”。服务端用独立派生
密钥签 HMAC token，绑定操作者、文件摘要、预览摘要和有效期；文件被改、SKU 被其他
请求抢先创建、数据库身份状态变化或 token 过期都返回 409 要求重预览。该密钥与设置
主密钥用途隔离，凭据不落库、不进仓库。

验证环境由用户明确授权：PostgreSQL + Redis 全量 pytest 3128/3128，真实 Celery
worker ping、Uvicorn HTTP、前端 100/100 与 Chromium 6/6 通过。独立 UAT 库已迁移
并播种，Mock / Simulator 可以进入人工测试。Docker CLI 缺失，所以镜像与 compose
仍是未执行项；真实 Provider / 渠道无凭据，也不在本次通过范围。


## §3.66 浏览器登录:滑动过期,以及浏览器侧具名审计的丧失

本轮把浏览器的认证方式从「localStorage Token」换成「用户名+密码 ->
HttpOnly Session Cookie」。两个固定账号 admin / operator,不建 users 表。
后端 Identity、`require_admin` / `require_operator`、Legacy API Token 全部沿用。

有两条取舍是**能力上的减法**,写在这里,免得下一轮有人把它们当缺陷去"修复"。

### 一、`AUTH_SESSION_MAX_AGE_SECONDS` 是空闲超时,不是绝对存活时长

Starlette 的 `SessionMiddleware` 在**每一个** session 非空的响应上重写
`Set-Cookie`,带上新的 `max_age`。所以那个 12 小时是 idle timeout:
只要页面还在发请求,Session 就不会过期。而本仓前端有多处 `useQuery`,
`useIdentity` 在后端故障时还会 15 秒轮询一次 —— 也就是说,一个开着不动的
标签页可以无限期保持登录。

因此「用一个很短的 TTL 等它过期」这种验证方式在有轮询的页面上**测不出来**,
人工验收要改成直接删 Cookie 或换签名密钥。

需要绝对过期的话,做法是在 session 里写 `login_at`(epoch 秒),
`resolve_identity` 时自行比较。本轮不做:两个固定内部账号、非公网。

同样不做的还有:refresh token、"记住我 30 天"、Session 管理后台、
"管理员强制踢人"、"改密后立即全端失效" —— 后两条需要 Redis Session
或 session version,签名 Cookie 是无状态的,服务端没有一张"哪些 Session
还有效"的表。logout 只让**当前浏览器**丢掉 Cookie。

### 二、浏览器侧的具名审计没有了

改之前 `OPERATOR_TOKENS=alice:tok-a,bob:tok-b` 支持一人一个口令,审计日志里
记的是 `alice` / `bob`(`deps.parse_operator_tokens` 为此专门做了名字形状
约束)。改之后,**所有经浏览器发生的操作,actor 只会是 `admin` 或 `operator`
两个值**,"这件是谁批的"在 UI 侧答不出来了。

这是一次真实的回归,本轮**显式接受**:当前是两人内部使用,且本轮明确不做
用户表。等到需要按人追溯时,正确的下一步是「users 表 + 每人一个账号」,
**不是**回退到 `OPERATOR_TOKENS` 的具名口令 —— 那是机器凭据,不是人的账号。

API 侧不受影响:`OPERATOR_TOKENS` 的具名解析原样保留,CLI 与脚本仍然记真名。

## §3.67 浏览器登录的前端接线:未登录该往哪儿去,由后端说

§3.66 把后端做完了 —— `/auth/login` 发 HttpOnly Cookie、`resolve_identity`
Session 优先、非 local 配置不全直接拒绝启动。**而前端整个仍然跑在
localStorage 口令上**:那三个端点一个界面调用都没有,`useIdentity` 的
`enabled` 还挂在 `hasToken` 上。本节记本轮接线时三个不显然的决定。

### 一、`auth_mode` 挂在 `/health` 上,不让前端按 401 的文本去猜

前端在**未登录**那一刻需要决定把人送到登录页还是设置页,而它手上只有一个
401 —— 两种模式的 401 完全一样:同一个 `AUTH_FAILED`,同一个状态码,
只有 message 不同。按 message 分支是 §3.26 明令禁掉的形状(按字符串在别处
找东西,找到的不保证是你以为的那个),而且那句话将来任何一次措辞调整都会
静默改变跳转行为。

    备选           前端按 message 关键字判断
    选错的后果     后端把"未登录或登录已失效"改成"请先登录",跳转当场失效,
                   而没有任何测试会红 —— 两侧各自的用例都不涉及那个字符串

挂到 `/auth/whoami` 上也不行:那个接口未登录时**就是 401**,正是要回答问题
的那一刻它答不了。`/health` 是唯一一个匿名接口,所以它是唯一的位置。

判据取 `settings.browser_auth_configured` —— 和 `resolve_identity` 同一个属性,
不在 `/health` 里重新推一遍"三项都非空"。分叉的方向是最坏的那种:local 下
只填了密码没填 secret 时,守卫已经按 Session 在拦,而 `/health` 说这是口令模式,
前端把人送去设置页,填完还是进不来,页面上没有任何一句话解释为什么。

不是信息泄露:"这个部署开没开浏览器登录"本来就匿名可观测(往
`/auth/login` POST 一次就知道)。

### 二、登录页是 `AppLayout` 的兄弟,不是它的子路由

挂进布局路由的表现有两层。表层是未登录的人看到一个完整的侧栏,点哪一项都被
弹回来 —— 而他会以为是自己点错了。深一层是自指:`AppLayout` 判断未登录时要
跳登录页,而登录页在它里面,于是那次跳转会再次经过同一个判断。

代价是登录页要自己画一个外框(`Shell`),多了十几行。值得。

### 三、`?next=` 要带,而且要在唯一入口处校验

运营的常态是从聊天窗点一条深链进来(某件商品的向导、某个批次),会话恰好
过期。只把他丢回首页的话,那条链接就白点了 —— 而他多半不会想到回去再点一次。

带上它就引入一个开放重定向:`/login?next=//evil.example` 里那个协议相对 URL
会被浏览器当成外站。react-router 的 `navigate()` 本身不出站,但这个值将来很
容易被谁塞进 `href` 或 `location.assign`,而那时没有人会回来想它的来源是地址栏。
所以在**唯一入口** `safeNext()` 处白名单式收干净(必须以单个 `/` 开头),
比在每个出口处防要可靠。

### 四、跳转判据是 401,不含 403

    401                  未登录 / 登录失效。**送去登录页**
    403 AUTH_FORBIDDEN   身份有效,但 operator 动了管理接口。登录页帮不上忙
    403 CONFIG_INVALID   服务端没配凭据。他登多少次都没用

把 403 也算进去的后果具体而难查:operator 点一下管理页,被踢去登录页,
重新登一次,回来看到同样的 403 —— 一个能无限循环的动线。

### 五、这一轮修的四条僵尸守卫(不是本轮引入的)

打开交付包时纯层有 3 条红、变异脚本有 1 条锚点失效,全部早于本轮:

    test_admin_guarded_pages_are_not_in_the_operator_menu   断言 NAV 里有
        `adminOnly`。菜单改成不按账号隐藏之后它就是错的,而且与
        `nav-and-url-filters.test.tsx` 的「不按账号隐藏」**互为反面** ——
        两条守卫对同一件事给出相反判据时,红的那条会被当成环境问题绕过去
    test_nav_is_grouped_and_every_entry_is_routed           同上,同一句断言
    test_the_frontend_reads_...attribution_column...        `groupByColour` 搬去了
        `materialUtils.ts`,定位串失效,`window()` 因此抛"起点出现 0 次"
    mutate_batch14_4.py Q11                                 锚点里的 `isAdmin`
        早被去掉,这条变异**一次都没造出来过**,而脚本照样报"抓住"

前两条按 `frontend/CLAUDE.md` 第三条删掉(菜单可见性不是跨语言契约,
判定留在会渲染它的那一侧),后两条改定位串。

删第一条时留下了一个洞:变异 Q1「把 /settings 挪进普通运营菜单」原来由它抓,
删完 `mutate_batch14_4.py` 从 20/20 掉到 19/20。补的是"同一个 key 不出现在
两组里"——那是翻转之后仍然成立的那一半,和 Vitest 那条说的是同一句话,
不是相反的话。

### 六、自审补的一条:信号必须有消费端

本节头两版落码之后自审时发现,401 会话失效信号**算出来了、没有人订阅**。
后果是"用着用着会话过期"这条路径整个不生效 —— 身份探测挂着 60 秒
`staleTime`,后端 Cookie 过期后前端手里那份身份还能有效一分多钟,
运营点什么都失败而界面不把他送去登录页。

    备选           拦截器里直接跳登录页(不绕 whoami)
    选错的后果     一次偶发 401(某接口配错守卫、网关抽风)会把登录态完好的人
                   踢出去;他重新登一次、回来撞上同一个接口,再被踢出去 ——
                   一个能无限循环的动线
    定下来的        信号只负责说"去重新问一遍",`useIdentity` 用
                   `resetQueries` 丢掉缓存重探,**由 whoami 的回答决定去留**。
                   "我登没登着"仍然只有一个答案来源

`resetQueries` 不是 `invalidateQueries`:后者在重新请求失败时保留旧数据,
`probe.data` 还在、`needsLogin` 还是假 —— 做了但没生效,比没做更难查。

这次的教训不在缺陷本身,在**它为什么没被看见**:`browser-login.test.tsx`
把 `useIdentity` 整个 mock 掉了,于是它验的是"拿到 `needsLogin` 之后怎么办",
而洞在"`needsLogin` 永远不会变成真"。守卫补在 mock 的**另一侧**:
`tests/unit/client.test.ts` 那 6 条不 mock 任何东西,纯层再钉一条
"信号必须有消费端"。同时删掉两个没有读者的导出
(`SESSION_EXPIRED_EVENT` / `isSessionExpired`)——它们不会让任何东西变红,
只会让下一个人以为这里有一套事件机制,然后照着写第二份订阅。

### 七、这一轮的验证缺口(必须读)

打这一批的机器**没有网络**,`npm ci` 装不上,`fastapi` 也没有。

    跑过     纯测试 2747/2747、verify_delivery 18/19(唯一 FAIL 是"不是 Git
             工作树",解包目录的预期)、mutate_batch14_4 20/20、
             syntax-check 96/96、pack.sh 正反两次
    没跑     npm typecheck / lint / Vitest / build、pytest、Playwright

也就是说**本轮新增的 21 条 Vitest(13 组件 + 6 单元 + 2 冷启动)与 3 条
Playwright 一次都没有执行过**,后端新增的 2 条 pytest 同样。纯层新增的 8 条
跑过并逐条做过变异验证,不在这份缺口里。

本轮主体是前端,这个缺口不小,清单与复跑命令见 `HANDOVER.md` 顶部那一节。

## §3.68 浏览器登录的可部署性:一个必填到"不填就起不来"的配置,全仓 0 份文档

§3.66 落了后端、§3.67 落了前端,而这一节修的是**它们合起来仍然部署不了**。

### 一、事实

`config._check_browser_auth` 规定:`APP_ENV` 不属于 local/dev/development 时,
`ADMIN_PASSWORD` / `OPERATOR_PASSWORD` / `AUTH_SESSION_SECRET` 三项配不全就
**抛错、进程起不来**。而落码两轮之后:

    docker-compose.prod.yml   一个字都没提这三项
    全仓 .md                  `ADMIN_PASSWORD` 出现 0 次

也就是说,任何人按 README 部一次生产,得到的是一个**起不来的后端**,
而他能拿到的全部线索是容器日志里被几十行启动输出淹掉的一句「ADMIN_PASSWORD 为空」。
`docker compose up -d` 会打印成功,`docker compose ps` 只显示 Restarting。

### 二、`:?` 而不是 `:-`

    备选           `${ADMIN_PASSWORD:-}`(给个空默认,让它起来再说)
    选错的后果     容器起来又退出、退出又重启。**问题从部署那一刻推迟到了
                   看日志那一刻**,而中间隔着一个"看起来部署成功了"的假象
    定下来的        `:?` —— 变量没设,compose 在**创建容器之前**就退出,
                   消息直接打在部署者的终端上

这条不是本轮发明的,是这份文件自己的哲学:同文件 `POSTGRES_PASSWORD` 那几行
写着「没配就起不来……启动失败是个好错误 —— 它发生在部署那一刻、由部署的人看到,
而不是三个月后由别人发现」。上一版只是没把这条哲学应用到新加的三项上。

### 三、三个服务都要,不只 backend

worker 与 beat 同样构造 Settings,同样被那条检查拦住。只给 backend 加的表现是:
**页面能打开,而任务队列在悄悄重启** —— 用户看到的是"任务建好了就不动",
而那和登录看不出任何关系。

### 四、守卫钉的是"两处必须一起改",不是"现在写对了"

    test_every_key_that_blocks_startup_is_demanded_by_the_production_compose
        从 `_check_browser_auth` **本身**解析出"哪些键为空就起不来",
        再要求 prod compose 用 `:?` 逐个要求,并且 backend/worker/beat 都引到。
        加第四项必填配置时,这条当场红
    test_the_startup_blocking_keys_are_documented_somewhere_a_deployer_reads
        同一份键必须在 README 里出现过。一个必填到"不填就起不来"的配置项,
        唯一的说明在 `.env.example` 注释和一个会抛错的函数里 —— 那等于没有

判据只取「XXX 为空」那一支。同一个函数里还有 `AUTH_SESSION_MAX_AGE_SECONDS
必须为正`、`仍然是占位值` 之类,那些键**有合理默认值**,拿 `:?` 要求它们
反而会让一个本来能跑的部署起不来。「不给就起不来」和「给错了会起不来」
是两件事,守卫只管前者。

### 五、人工验收的入口也补了

`LOCAL_MANUAL_TEST.md` 新增 §4.5:本机怎么开(填任意一项就会真的走登录)、
怎么确认(`curl /health` 看 `auth_mode`)、要走的六步(深链跳转带 `?next=`、
错密码不区分原因、登录后回原页、退出后仍被弹回、operator 看 403、admin 正常)。

其中一条专门写了**容易误判的现象**:会话过期后页面不会立刻跳登录页,
要等下一次请求撞上 401、前端重新探一次身份才跳(§3.67 第六节的设计)。
不写的话,验收的人会干等着,然后报一个"登出没生效"的假缺陷。
## §3.69 打包复验的假阴性:grep -q 提前退出 × SIGPIPE × pipefail

### 一、现象回放,与当时就成立的三条观察

§3.68 交接留了一桩没查出根因的事:`tools/pack.sh` 的必备文件复验偶发报
`!! 交付包缺少必备文件`,两轮共四次,受害者是 `.gitattributes`、
`.github/workflows/ci.yml`(两次)与 `backend/tools/verify_delivery.py`,
复现率约 3/12。诊断三条当时就都在:清单条目数与成功那次完全相同、
`unzip -t` 通过、该文件就在清单里。也就是说包是好的、清单是全的、
要找的那一行就在那,而 `grep -qxF` 说没有。上一轮还因为用 `tail -3`
截了输出,把它误判成"包里有禁品" —— 假阴性的代价从来不止重打一次。

### 二、根因

出错的是这一条(修前原文):

    if ! printf '%s\n' "$LISTING" | grep -qxF "$path"; then

四个各自无害的东西叠在一起:

1. `grep -q` 的契约是**命中即退**,不读完输入;
2. 清单共 836 条、约 31.7 KB,bash 内建 printf 经 stdio 分多次 write 写入管道;
3. grep 先退、printf 还有残余没写完时,内核给 printf 发 SIGPIPE,退出码 141;
4. `set -o pipefail` 取管道里**最右边的非零码**,141 压过 grep 的 0;
   `if !` 再把"管道失败"翻译成"没找到"。

于是:grep 明明命中了(它自己退出码 0),整条判定却走进"缺少必备文件"。
三条旧观察逐一有了解释 —— 清单当然是全的(问题不在清单),包当然是好的
(问题不在 zip),文件当然在清单里(grep 也真的找到了)。受害者分布也对上:
命中越靠前,grep 越可能在 printf 写完前退出。四个历史受害者在清单里的
字节偏移是 0、52、28215(总长 31692)—— 全部落在竞态窗口内,最靠前的
两个被打中的次数也最多。

### 三、复现(同形最小化 + 真实脚本,全部在本机测得)

    同形最小化:836 行清单,目标放第 1 行,
    `printf | grep -qxF` 循环 400 次        38 次假阴性,退出码全是 141
    逐进程状态(PIPESTATUS)                 printf=141,grep=0 —— grep 命中了
    对照 A:目标挪到清单最后一行,400 次     0 次(grep 必须读完才能命中,
                                            printf 必然先写完,无竞态)
    对照 B:grep 改读文件,400 次            0 次
    真实 tools/pack.sh,修前 15 连打         3 次失败,受害者与历史一致
    真实 tools/pack.sh,修后 40 连打         0 次失败

对照 A 同时解释了为什么 `FORBIDDEN` 循环从来没炸过:那边的 grep 不带
`-q`,要打印全部命中就必须读完输入,结构上就没有提前退出。

### 四、修法:比对全部读文件,清单顺带升级成常规留档

复验一节开头把清单一次性落盘,此后**所有 grep 都读文件,不吃管道**:

    LISTING_FILE="$OUT.listing.txt"
    unzip -Z1 "$OUT" | sed 's|^\./||' > "$LISTING_FILE"

必备文件复验改为 `grep -qxF "$path" "$LISTING_FILE"`;env 豁免视图经同一份
文件生成(mktemp + trap 清理);条目计数与最终统计同样读文件。读普通文件的
grep 提前退出无人可"杀" —— 竞态不是变小了,是没有了。

两个顺带的收益:清单从"失败才落盘"变成每次都落盘 —— 包发出去之后,它是
唯一能回答"当时打进去了什么"的东西;`report_listing_health` 保留(它当时
把范围收窄到比对路径上,是这回定位的第一功),措辞改为指向已落盘的清单。

`tools/pack.ps1` 不受此病影响(.NET 内存 List + `-notcontains`,无管道),
只同步"清单每次落盘"这一条,证据口径两侧一致。

### 五、守卫与变异验证

`verify_delivery.py::check_the_pack_script_excludes_and_then_verifies` 增两条:

    正向   pack.sh 必须含 `grep -qxF "$path" "$LISTING_FILE"`
    反向   pack.sh 里不许再出现把内容从管道喂给 `grep -q` 的写法

变异验证两个方向都做过:把必备文件复验改回管道 —— 第一条红;在脚本里
塞一行 `true | grep -q x` —— 第二条红;还原后 19/19 绿。

### 六、这件事一般化之后是什么

`grep -q`、`head`、`read` 一行 —— 任何**允许不读完输入**的消费者,接在
`set -o pipefail` 的管道右侧,生产者就可能以 141 收场,而那 141 会被当成
"没找到/没读到"。要么让消费者读文件,要么别让这类管道的退出码参与判定。
本仓库的口径取前者:**判定用的比对,一律读落盘文件** —— 顺带每次都留下
一份可回看的证据,这两件事在同一个改动里互为副产品。


## §3.70 文档审核:四类缺陷,一个共同点 —— 两份真相分叉,而没有东西在看

### 一、事实

a46-phase4 交出的包,离线门禁**全绿且数字属实**(纯测试 2751、锚点 553/553、
守卫窗口 638、导入解析 483、样例数据 10/10、前端语法 96/96)。本轮不是去推翻
那些数字,而是去查它们**管不到**的那一层 —— `audit_doc_refs.py` 自己在输出里
写着的那句:「这只管路径存不存在,一句指得到但说错内容的话,它看不出来」。

查出来 15 处,按后果排:

    照着做会当场失败
      docs/MANUAL-ACCEPTANCE.md §3.1  UAT 基线 env 缺浏览器登录三项,而 §5.1
                                      让人跑生产 overlay(`${KEY:?}`)—— 变量没设
                                      连容器都不会创建;就算绕开 compose,
                                      `APP_ENV=uat` 也过不了 `_check_browser_auth`
      docs/MANUAL-ACCEPTANCE.md §5.3  鉴权矩阵仍按 Header 口令写,配了密码之后
                                      部署处于 session 模式,浏览器走 `/login`
      docs/DEPLOYMENT.md              全文「登录」0 次;§九仍写「MVP 没有账号体系
                                      —— 当前最大的安全缺口」「唯一的防线是网络层」

    说反了
      frontend/src/App.tsx            文件头「按角色收敛」「只有管理员看得见」,
                                      而同一文件往下 60 行写「路由和菜单都不按角色
                                      裁剪」;`NAV` 里没有 `adminOnly`
      frontend/src/components/AppLayout.tsx  文件头「始终可见」,行内注释「整组对
                                      非管理员隐藏」—— 一个文件里两句话互为反面
      frontend/src/hooks/useIdentity.ts      整段设计说明建立在「菜单收敛」上
      backend/app/api/auth.py         「菜单按角色收敛需要知道是不是管理员」
      docs/SETTINGS.md §三            「所以这里没有登录」「不新建 app/api/auth.py:
                                      一个 auth.py 会把一道运维口令看起来变成账号
                                      体系的开端」—— 而那个文件现在就在旁边

    宣告一个不存在的守卫
      backend/app/api/health.py       「`test_browser_login_frontend.py` 钉着这一点」
      frontend/src/api/client.ts      「它有一条源码级守卫(同上)」
                                      —— 全仓没有这个文件,真正在钉的是
                                      `test_a46_phase2_browser_login_seam.py`

    冻住的数
      docs/STATUS.md §七              「一共 13 份」而表里 15 行;「眼下 29 份」
                                      过程文档实际只剩 9 份;地图漏收四份活文档
      docs/STATUS.md 能力表           「新增的 12 条 Vitest」—— 那是 phase2 自审时
                                      当场改掉的第一版数(真值 21 条 Vitest +
                                      3 条 Playwright + 2 条 pytest),订正只落在
                                      HANDOVER,没有回流到 README 指定的第一份文档
      README.md                       「15 个硬错误代码」实际 21;「30 张素材」实际 51
      CLAUDE.md / AGENTS.md 等五处    同一句「30 张」各冻一份,而
                                      `LOCAL_MANUAL_TEST.md` 里**已经写着**这个数
                                      过期过一次、并改成让人跑 verify_sample_data
      三份 AGENTS.md                  是 CLAUDE.md 的旧副本(根那份差 113 行),
                                      正文首行都写着「# CLAUDE.md — 仓库总纲」,
                                      全仓 0 处引用、0 道门禁

另有一处是门禁自己:`docs/DECISIONS.md` 的 §3.63~§3.69 用的是 `## 3.6x` 而不是
`## §3.6x`,而「决策日志编号不重复」那条检查的正则是 `^##\s+§` —— **最新七节
撞号了它也看不见**。本轮统一成 `## §N`。

### 二、共同形状

十五处分属四类,坏法却是同一个:**一件事有两份记载,其中一份被改了,而没有
任何机制要求另一份跟着改。** 与 §3.33 那一族是同一个东西,区别只在这一轮的
「另一份」不在代码里,在文档和注释里。

三个次级观察值得单独留着:

**一、教训只在踩到它的那份文件里生效。** 「30 张」这个数,`LOCAL_MANUAL_TEST.md`
不但改对了,还专门写了一段说它过期过、以后跑脚本取当前口径 —— 而隔壁四份文档
原样冻着。一条只写在散文里的规矩,作用范围就是那一页。

**二、订正会停在它被发现的那一层。** phase2 自审时发现「12 条」是错的,改在
HANDOVER 里;而 STATUS —— README 指定的「想知道某项能不能用,从那份开始」——
留着旧数。订正的传播方向默认是「往下游」,而下游是谁没人列过。

**三、守卫的窗口就是它的射程,不多一寸。** 见下一节。

### 三、「单一归属」在部署文档上是错的,本轮翻掉

phase3 补「三把启动键必须被文档说明」这条守卫时,只钉了 README 一份,理由写的是
「单一归属」。它当时是对的:那一刻全仓 0 份文档提过这三个键,钉住一份就止了血。

但它掩护了一件事:README 自己写着「部署与运维见 `docs/DEPLOYMENT.md`」,
`STATUS.md` 的文档地图也把 DEPLOYMENT 标成「要把它部署起来时看」的那一份 ——
**读者根本不会读 README 的那一节**,他按地图走。于是守卫绿着,而部署的人拿到的
仍然是 phase3 想根治的那个「反复重启的后端」。

单一归属的前提是「读者会去那一份」。这个前提在部署这件事上不成立,因为入口不止
一个。判据因此改成:**每一条入口路径上都要有**(README / DEPLOYMENT /
MANUAL-ACCEPTANCE 三份)。`.env.example` 仍然不算 —— 那是给已经知道有这回事的人
看的,而这条守的是「根本不知道有这回事」。

### 四、修法上的两条纪律

**钉一致性,不钉现状(§3.31 / §3.33)。** 新增的每一条守卫都能同时容纳两种世界:
菜单那条在「裁剪了」和「没裁剪」下都能绿,只拒绝「代码一个样、注释另一个样」;
文档地图那条不关心地图有几份,只要求那句话和表格行数是同一个数。

**引用一个不在的文件不算错,说它「正在钉着」才算。** 所以判的是**时态**:
同段里带「原先 / 已并入 / 当年 / 退役 / 不存在」这类标记的一律放行 —— 这个仓库
的注释密度本来就是为了保留历史。全仓有五处这样的合法引用(`test_import_graph.py`
「当年就没看见」、`test_attribute_projection.py`「原先在……已并入 merge」等),
一条都不该被这条守卫误伤。

### 五、守卫与变异验证

`tests/pure/test_a46_phase5_doc_truth.py` 新增 7 条,`test_a46_phase2_browser_login_seam.py`
那条扩窗口。变异逐条验红,**11/11**,分两份脚本 —— 第一版想用 `SUITE_FILTER = "a46"` 一个
子串盖住两个套件,`audit_anchors.py` 当场拦下(子串会让变异被别的套件抓住,
归因变假)。`tools/mutate_a46_phase5.py` 9 条:

    C1  AGENTS.md 分叉一行                      RED
    C2  「只有管理员看得见」放回 App.tsx          RED
    C3  反方向:NAV 真按角色裁剪而注释说没落地    RED
    G1  注释改回引用不存在的守卫文件              RED
    N1  文档地图那句话与表格行数分叉              RED
    N2  过程文档条数写回旧值                      RED
    N3  README 又把示例条数写死                   RED
    N4  反方向:sample-data/README 的数改成假的   RED
    N5  硬错误代码条数冻回 15                     RED

`tools/mutate_a46_phase5_deploy_docs.py` 2 条,钉 phase2 那条被扩过窗口的守卫,
新旧窗口各一条:

    D1  三把键从 UAT 验收手册里拿掉(phase3 的旧窗口看不见)  RED
    D2  三把键从 README 拿掉(扩窗口不能把原来钉住的弄丢)    RED

N 组那两条方向相反是刻意的:「不许写死」与「写死了就必须是真的」是两条不同的
规矩,对应两种读者 —— 入口文档的读者要的是当前值,贴着数据的那份 README 的
读者要的是一个具体的数。

### 六、没做的

前端四条(typecheck / lint / Vitest / build)与 Playwright **仍然没跑**,
机器没有网络,`npm ci` 装不上。本轮改动含 4 个 `.tsx`/`.ts` 文件,但**改的全是
注释与文档字符串,没有一行可执行代码**;`frontend/tools/syntax-check.mjs` 96/96
通过,能证明它们仍然解析得通,不能证明类型与用例。phase2 欠的那份验证缺口原样
欠着,清单见 `HANDOVER.md` 的 a46-phase2 一节。

## §3.71 浏览器登录收尾:Token UI 退役、菜单按角色收敛,以及八条为 Token 写的门禁

§3.66~§3.68 落了后端、前端与可部署性,而浏览器**仍然同时持有两套凭据**:
HttpOnly Session Cookie 和 localStorage 里那两把口令。本节记的是把后者拆掉,
以及拆的过程中撞上的八条门禁。

### 一、拆掉的是什么

    localStorage 双口令 + memory 兜底 + TOKEN_CHANGED_EVENT 广播与订阅
    自动带 X-Operator-Token 并回落 X-Admin-Token 的请求拦截器
    adminHeaders() 与它在 settings / prompts / generation / batch 四处的调用
    设置页顶部的「操作口令 / 管理口令 / 记住」录入卡
    ColdStartBanner 的三支口令话术
    顶栏那条「共用口令,审计追不到人」的告警(operator 账号会让它常亮)

`ADMIN_TOKEN` / `OPERATOR_TOKENS` **在后端原样保留**,只服务 CLI、脚本、pytest
与服务间调用。`deps.resolve_identity` 的三级(Session → Legacy Token → ROLE_DEV)
一行没动。

有人会问:`/health` 回 `auth_mode: token` 的部署,浏览器岂不是没凭据了?
不会 —— `_check_browser_auth` 规定非 local 环境三项必填、配不全起不来,
所以 `token` 模式**只可能出现在 local/dev**,而那里 `ROLE_DEV` 回落本来就
不要任何凭据。「口令模式的浏览器」这个组合在今天的配置空间里不存在。

### 二、菜单按角色收敛:一条被显式撤销的旧约束

「系统管理」组现在标了 `adminOnly`,`AppLayout` 按 `isAdmin` 过滤。

**这是新功能,不是恢复。** 在此之前那一行是 `const visible = groups`,而且有
一条**反向门禁**明令禁止 `NavGroup` 上出现 `adminOnly`。当初"始终可见"的理由
写在 `useIdentity.ts` 里:「新人第一次打开系统时既不是管理员、又必须进设置页
填口令」。浏览器登录上线之后这个前提**消失了** —— 密码在 `.env` 里,没有任何人
需要先把自己变成管理员。所以撤,而且连同它的门禁和三段注释一起撤。

**路由不跟着裁。** operator 手输 `/settings` 打得开,页面上是一句 403。
把路由也删掉会把一个说得清楚的 403 变成一个看不懂的 404,而真正的边界从来
只在后端 `require_admin`。菜单收敛是可发现性,不是权限。

### 三、八条为 Token 方案写的门禁,逐条改写而不是删

本轮真正的工作量在这里。八条里有四条用 `.index()` 切窗口,删掉被测代码之后
它们**抛 ValueError 而不是断言失败**;而其中两条的失败信息会主动把人引向错误
的修法 —— 最露骨的一条原文是:

    "回落被整个删掉了 —— 只配 admin 的部署会整站 401,那不是修复"

那句话在口令时代是对的。今天整站 401 的正确解法是**登录**,不是往 localStorage
里塞一把能改 API Key 的口令。所以处理原则是:**改写成新方案下等强度的不变量,
不是删了了事,更不是为了让它变绿把 Token 加回来。**

    改写(4)
      admin 路由必须带 adminHeaders     -> 反转:后端仍要有 require_admin 路由,
                                          而前端一处都不许带 X-Admin-Token
      匿名成功不清鉴权横幅              -> 主语从 authRejected 换成 sessionExpired,
                                          形状一模一样;前后端白名单集合相等保留
      横幅要带口令探测                  -> 横幅只在 backendDown 时出声,
                                          并反向禁止四个口令时代标识符回流
      探测不再挂本地口令                -> enabled 收敛成只依赖 !health.isError,
                                          并禁止 hasToken / read*Token 回到 hook 里
    删除并留墓碑(3)
      请求拦截器回落顺序、口令变更响应式、回落对运营可见
    翻转(1)
      「系统管理」不按账号隐藏          -> operator 看不到管理入口,admin 看得到,
                                          而 declaredPaths() 仍含 /settings

墓碑不是客套:三条都在原地留了注释说明"被守的那件事今天由谁接住",
否则下一轮会有人照着 git 历史把它们恢复。

顺带修了两条**过期的变异锚点**(`mutate_a46_phase5.py` 的 C2、
`mutate_batch14_4.py` 的 Q11)—— 它们指向的源码行被本轮改掉了。
PRD §41.14 点名要求改完跑一次 `make audit-anchors`,那一步真的抓到了东西。

### 四、没做的,和为什么

**没有引入 `RequireAuth` 组件。** PRD §23 要求登录检查在 `AppLayout` 之外,
而现有实现是 `AppLayout` 内部按 `needsLogin` 走 `<Navigate>` —— 行为上已经满足
§23 的每一条(检查期不渲染、401 跳登录、带 `?next=`、不闪管理员页面),
差的只是组件名与位置。搬动它要重写 13 条 phase2 的 Vitest,而**那 13 条在这台
机器上一次都没跑过**(没有网络)。对一段从未被执行过的代码连改两次,
风险大于收益。这一条留给有网络的机器,连同下面那份验证缺口一起还。

**`PASSWORD` 没有加进 `SECRET_NAME_SEGMENTS`。** PRD §5.2 说是"可选但建议",
加了会让 `POSTGRES_PASSWORD=imagegen` 这一行当场变红,而那是本地默认库密码,
处理它属于另一件事。按 PRD 自己的要求,这里显式记一句:**判据的这个洞还在,
不是许可。**

### 五、交付后自审补上的七处(同轮)

被要求"反思本轮修改"之后过了一遍,七处,其中最有教益的两处:

**一条为新方案写的测试,在它唯一能跑的环境里必红。** `client.test.ts` 的
401 用例被我改成 `toContain('重新登录')` —— 而单测环境里 `/health` 一次都不会
返回,`authMode` 停在默认 `token`,那条断言够不到会话分支。写它的人(我)
没法跑 Vitest,红灯要等下一台机器才炸,和 §41 里被点名的那批旧门禁**恰好是
镜像**:它们是"为旧方案写的、新方案下必红",这条是"为新方案写的、在测试环境
里必红"。修法是承认单测的射程:那条用例改钉免登录分支的负向不变量
(不许再指设置页),会话分支的文案钉到纯层
(`test_the_session_401_copy_sends_people_to_login_not_the_settings_page`,
剥注释后判 —— 历史引用放行,活代码零容忍),变异 D3 验红。

**解释文案的注释没跟上文案。** client.ts 三段、SettingsPage 一段,还在讲
"口令模式下该说到设置页核对口令""口令是对的,只是……"。这正是 §3.70 修的
那一类病(说法在 A 处被订正,B 处继续说旧的),phase6 在自己身上又犯了一次 ——
区别只是这次两处相隔二十行而不是两个文件。教训同一句:**改一句话之前,
先搜谁在解释它。**

其余五处:我新增的横幅用例引用了两个不存在的测试帮手(没读文件先写了
`Wrapped`);browser-login 的 mock 揣着已删的 `usingAdminFallback` 字段;
两处死 import(`Space` / `brandVars`,typecheck 会红而 syntax-check 看不见);
以及 SettingsPage 那行 identity 注释描述的是一个已删除的用途。

---

## §3.72 界面收口 a47:方案真正控制出图,菜单 13 → 7,款级聚合不编下一步

上游 PRD:`PRD-ui-ia-consolidation-v2_1-self-reviewed.md`。本节记四件**结论**,
过程不留档。

### 一、撤销两条旧约定

**「工作台的生成任务页签是只读观察窗」作废。** 那条分工在工作台还不是唯一
生产入口时说得通;§4 把 `/products` 与 `/tasks` 一起撤出运营菜单之后,
它会让运营看得见"这件商品没有任何生成任务",却没有任何一颗按钮能开始出图。
现在那一页可发起(复用 `TaskCreateModal`,不重新实现业务逻辑),并且显示
状态、轮次与失败原因三样。

**「`/tasks` 是运营的任务排障入口」作废。** 它移进「系统管理」组,而那一组
对 operator 整组不可见 —— 所以这不是降级,是消失。**顺序是硬的**:
上面那一条必须先落地。反过来做,运营会有一段时间两头落空(老入口没了,
新入口答不上)。

### 二、`plan_fingerprint` 那一列的存量遗留(不清洗,但要写下来)

a47 之前,`generation_tasks` 上已经写着 `generation_plan_id` /
`plan_fingerprint` 的历史行,**其参数与它记着的那份方案并不一致** ——
当时六个方案参数里只有 `budget_cap` 生效,`provider` / `model_template_id`
取的是调用方传的,`scene` / `pose` / `angles_json` 一次都没被读过。

**结论:不回填、不清洗。** 理由是这一列今天没有任何读者(全仓零处读
`GenerationTask.plan_fingerprint`;上游快照里那个同名字段是
`gp.effective_fingerprints()` 从方案直接算的,不经过任务),清洗的风险
大于收益。

**但这是一笔已知遗留,不是一件没发生过的事。** 本轮之前的行不保证与方案
一致,将来第一个要读它的人必须先看这一条 —— 按 `plan_fingerprint` 做对账、
去重或复现之前,先按 `created_at` 把 a47 之前的行排除掉,或者接受它们
可能记着一份并没有被执行的方案。不写下来,这就是下一轮的"沉默的坑"。

### 三、`override_plan` 绕过方案,**但不绕过预算**

`budget_cap` 是花钱的闸,不是出图参数。绕过方案不等于绕过预算 ——
否则 `override_plan` 会成为超预算出图的后门,而它恰恰只有管理员能用,
查起来最没人怀疑。所以 `create_task` 里预算检查排在 `plan_applied` 判定
**之前**,`tests/pure/test_a47_plan_governs_generation.py` 有一条守卫钉着
这个顺序。

绕过之后 `generation_plan_id` 与 `plan_fingerprint` 两列必须留空:
绕过了方案就不许再记这份方案。记着方案而按别的参数跑,正是本轮要修的那个
错位的镜像版本,而且更隐蔽 —— 那一行任务看起来完全正常。

### 四、款级聚合给分布,**不给 `next_action`**

上游 PRD v2.0 要求后端聚合出款级 `next_action_label`,规则是"取阻断数最大
的那只 SKU 的下一步"。它与明令禁止的"拿第一个 SKU 的 next_action 当款级"
**只差在任意性的形式**,本质一样:把一个没有真实语义的推导从前端搬到后端,
并不会让它变成事实。

款级"下一步"在这套状态机里**没有定义**:不同 SKU 卡在不同步骤时,任何单选
都是编的。所以 `SpuGroup` 给的是 `blocked_steps` 分布(`{步骤: 卡在该步的
SKU 数}`),让运营自己看"这款有 3 只卡在图片集、1 只卡在文案"。

口径提醒(容易被拿去对账):`blocking_count` 数的是**问题条数**,
`blocked_steps` 数的是 **SKU 个数**,一只 SKU 在一步里可以有三条阻断。
两者不相等,这不是缺陷。

**纪律的对象不是"哪一侧写代码",是"这个值有没有真实定义"。**

### 五、本轮验不到什么

`tests/test_a47_plan_governs_db.py`(§5.5 四条等式 + override 分支 + 403)
**写了、一次都没跑过** —— 没有 PostgreSQL。前端 tsc / ESLint / Vitest /
build 四条同样没跑 —— 没有 node_modules 且无网络。这两句话不许被读成
"已验证",纯层各有一条守卫钉着那两份文件必须存在。

第三条边界更细:§5.5 第三式「实际出图覆盖的角度集合」在这个基线上落在
**提示词**上,不在数据上 —— `GenerationTask` 没有角度列(本轮禁止加列),
`GenerationRequest` 也没有角度参数,出图 Provider 的接口里根本没有这个概念。
要验到"生成出来的图真的是那几个角度",需要给候选图加角度标注,那是另一轮的事。

## §3.73 a48 收口:离线看得见死 import,方案接管的语义补齐两个调用方

本节记三件**结论**,过程不留档。上游是 a47 交付后的自审复核 ——
不是新需求,是把 a47 与 a46-phase6 各自漏下的一条缝焊上。

### 一、离线门禁看不见死 import,这一条已经漏过两轮

事实先摆清楚:**a47 交付出去的树跑不过 `npm run typecheck`。**
`components/AppLayout.tsx` 里 `WarningOutlined` 是死 import,而 `tsconfig.json`
开着 `noUnusedLocals`。

来历比这一行本身值钱:

    a46-phase6  自审时**专门去找**死 import,找到两处(ColdStartBanner 的
                `Space`、SettingsPage 的 `brandVars`),漏了第三处
    a47         没动那个文件。离线门禁全绿 —— 而它本来就看不见这一类

也就是说,这条缝的唯一发现者一直是「有人恰好在联网机器上跑了一次前端门禁」,
而这棵树上大多数轮次都没有那样一台机器。**这不是自审不认真,是自审在
结构上补不了的位**:同一个人在同一轮里带着这个清单去找,仍然漏。

结论按硬规则 4 第二段那条既有的写:**靠人记住的防线会漏,得有机械落点。**
`frontend/tools/syntax-check.mjs` 从本轮起是两遍 —— 语法 + 死 import,
并且覆盖面从 `src` 扩到 `src` + `tests`(`tsconfig` 的 `include` 本来就是两棵,
原来那句「离线门禁绿了」在 tests 上从来没成立过)。

三个选型理由,改它之前先读:

1. **走 TS 的 AST,不走正则。** 正则在三处会**误报**:`...AUDIENCES` 展开
   (前面那个点让单词边界失效)、JSX 标签名、只在类型位置出现的名字。
   而误报比漏报贵得多 —— 一条会冤枉正常文件的门禁,活不过第一次拦住人的那天。
2. **属性名位置的 Identifier 要排掉**(`obj.Foo`、`{Foo: 1}`、限定名右半边),
   不排的话 `theme.Space` 会让一个真死了的 `Space` 看起来还活着。
   简写属性 `{Foo}` **是**引用,不能一起排。
3. **不新增门禁,只加宽已有的那一条。** `syntax-check` 早就在 `check-offline`
   与 ci.yml 里,加宽它不触发硬规则 3 的「三个地方」。同时它仍是
   `frontend/CLAUDE.md` 第三条的合规做法:判定交给 TypeScript 自己的编译器,
   不是又写一个 Python 扫前端源码的断言。

**它仍然验不到**死变量、死参数、类型与渲染。别把它读成 typecheck 可以不跑了 ——
它只让「离线全绿」这句话少骗人一点。

### 二、a47 改的是一个函数的入参语义,而受影响的是它全部调用方

a47 §5 让方案真正接管出图参数,这是对的。但它同时把 `create_task` 的
`provider` / `model_template_id` / 一轮张数 / 提示词从「调用方说了算」
改成了「方案在的时候方案说了算」——**改的是一个函数,受影响的是全部调用方**,
而 a47 只跟了一个(HTTP 接口)。

漏掉的那个是 `app/scripts/provider_baseline.py`。它整个脚本的用途写在
函数文档第一行:「固定 seed,让两边的差异只来自 Provider 本身」。
SPU 上有一份 ACTIVE 方案时,两条腿的 `provider=` 会**双双**被换成方案里
那一个,而脚本照旧打印一张对比表 —— **不报错,答案是假的**,
假在「上面写着 mock、下面写着 fashn」的那一栏里,而人是拿它决定接哪家的。

这不是一个要等很久才会撞上的场景:`LOCAL_MANUAL_TEST.md` §4.6 的 §5 验收
第一步正是「给一个 SPU 配一份 ACTIVE 方案」。照着文档走一遍,`make baseline`
就废了。

**修法:`provider_baseline` 显式 `override_plan=True`。** 它走 service 层,
而 `override_plan` 的管理员闸在 HTTP 那一侧 —— 基线脚本本来也只有能进库的人
跑得动。绕过方案**不绕过预算**(§3.72 三),也不绕过 §10.5 / §11 那两道闸。

`app/scripts/smoke_test.py` 是同一件事的另一面,但**结论相反**:它显式传一个
已授权模特,是为了让 §10.5 / §11 的生成前硬阻断真的在冒烟里跑一遍;
a47 之后那句话不再自动成立。冒烟**不绕过方案** —— 它要测的就是运营真实走的
那条路,而那条路本来就该由方案接管。改成回读出参(`TaskOut` 的 `provider` /
`model_template_id` 是解析**之后**的值)与请求体比对,不一致时打一条 note。
不 fail:被接管是正确行为,会骗人的是「报告说验过已授权模特那条闸,
而实际验的是别的模板」这句话。

### 三、守卫钉的是「有没有表态」,不是「表成哪个值」

`tests/pure/test_a48_plan_governs_every_caller.py` 要求 `app/` 下每一个
`<生成服务>.create_task(...)` 都显式写出 `override_plan=`。

**为什么不判值:两种调用方都合法,而且答案相反。** 线上出图不许绕
(那正是 a47 修的东西),基线对比必须绕(被接管的对比不是对比)。
没有一个「正确的默认值」可判。能判的是**作者有没有意识到这个问题存在** ——
默认值 `False` 是安全的,但它安静;而安静的默认值配上一个语义刚改过的参数,
就是下一个 `provider_baseline`。

**唯一钉死值的是基线脚本那一条**(必须 `True`),因为那里「差异只来自
Provider」是脚本成立的前提,不是一个偏好。

本文件同时带一条**反平凡**用例:调用点数归零时,另外两条会平凡通过并照样
打印 PASS —— 那正是 `audit_source_guards.py` 顶部讲的那种失效,
只不过这里的空窗口不是切出来的,是找出来的。

### 三之二、同一条缝的第三面:断 import(a48 第二批)

后端 `tools/verify_imports.py` 早就在 `check-offline` 里,做的是「`app.*` 的
import 是否都指向真实存在的东西」。**前端一直没有对侧。**于是「改名或删掉一个
导出、漏了某个调用点」这类改动,离线时同样无人看管 —— 而 a46-phase6 与 a47
恰恰各做过一次横跨九个文件的删除(Token 存储链、`adminHeaders`、
`usingAdminFallback`、`sharedActor`)。

**这一次扫下来是干净的**,一处都没有。加它的理由不是"发现了缺陷",是
**"这一次没漏"和"有东西在看"是两件事** —— 与第一节那条同源,只不过第一节
是先被咬了一口才补,这一节是补在被咬之前。判据:变异「把 `types.ts` 的
`AUDIENCES` 改个名」当场点名三个调用点。

两条刻意的放宽,改之前先读:

1. **只管本地模块**(`./` `../` `@/`)。第三方包要 node_modules 才知道它导出
   什么,那正是这个脚本不做的事;越界去猜会得到一堆误报。
2. **`export * from '...'` 直接放行。**再往下追要建完整模块图,收益是几个边角。
   **宁可漏,不可冤** —— 一条会冤枉正常文件的门禁活不过第一次拦住人的那天。

`@/` 的映射重复了 `tsconfig.json` 的 `paths`,这是本文件唯一一处重复配置,
改 tsconfig 要一起改它。

### 三之三、a48 自己改出来的第二份真相,当场补上了

`tests/pure/test_a45_batch11_fixes.py::test_smoke_exercises_the_license_gate...`
的文档字符串说的是「冒烟真的执行了 §10.5/§11 那四道检查」,而它的判据只有
**请求体里传没传 `model_template_id`**。a47 之后这句话变成**有条件成立**:
条件是那只 SKU 所属 SPU 上没有 ACTIVE 方案。

本轮给 `smoke_test` 加了回读之后,如果不同时把判据加上去,就会留下一条
**绿着的守卫说着一件不一定成立的事** —— 正是 §3.70 点名的那个形状。
所以那条守卫同批改写:传参那半边不变(不传就是走回绕行缝),另加两条 ——
必须回读 `task.get("model_template_id")`,被接管时必须出声。
变异验红:删掉回读、删掉那句话,各红一次。

### 四、本轮验不到什么

前端 tsc / ESLint / Vitest / build 四条**仍然一次都没跑过**(无 node_modules
且无网络),Playwright 同样。本节修的那一行死 import 是**推断**会让 typecheck
变红 —— 依据是 `tsconfig.json` 的 `noUnusedLocals: true` 与 TS 的成文行为,
不是一次真实的红。真库 pytest 与 `test_a47_plan_governs_db.py` 照旧未执行。

`provider_baseline` 与 `smoke_test` 两个脚本**都要真库 + 真 worker 才跑得起来**,
所以本轮改的两处只有纯层守卫与人眼,没有一次真实运行。
接第一台有库的机器时,这两条应当排在前面 —— 它们改的是「排障工具会不会
给出假答案」,而假答案正是在排障时最贵。

## §3.74 a49 评审整改:十二条,加上三份从来没有跑过的门禁

外部评审按「人工测试前是否会踩坑」收敛出 12 条(P1 八条、P2 四条)。这一节记
**为什么这么修**,以及修的过程里发现的三件评审看不到的事 —— 它看不到是因为
那台机器没有 `node_modules`、没有 PostgreSQL,而这台有前者。

### 一、两条 CI job 在改之前就是红的,而本地门禁看不见

`make check-offline` 不含前端类型、lint、Vitest 与构建(它自己会打印这句话)。
于是:

    npm run typecheck   `TasksTab.tsx:177` TS2352,一行强转两个类型不够重叠
    npm run test        5 条红,而且**每一条钉的都是一个不存在的 DOM**

第二条尤其值得记:三条登录用例写着
`getByRole('button', { name: '登录' })`,而 antd 会在两个汉字之间插空格
(`Button` 的 `autoInsertSpace`),真实可及名字是「登 录」。另两条断言
`更换口令`(随 localStorage 口令链一起退役)与 `provider: 'mock'`
(那个字段长在"仅管理员"的高级区里,非管理员的请求体里本来就没有它)。

**共同点是同一个:它们从来没有被执行过。** 一个从未失败过的门禁与一个不存在的
门禁,可靠性相同 —— 这句话本来就写在 `smoke.spec.ts` 的文件头上。

Playwright 同理。本轮第一次在真浏览器里跑完 9 条(镜像预装 Chromium,
版本与仓库 pin 不同,用一次性 `executablePath` 覆盖跑,**没有把那份覆盖留在仓库里**
—— 那个路径只在这台机器上存在)。第一跑抓到 6 条假绿:除了上面那个空格,
还有三条断言 `getByText('商品展示图生产台')`,而那是 `useDocumentTitle` 的
`BASE_TITLE`,也就是**标签页标题**,它从来没有出现在页面里。页面上那行字是
「服装上架平台」。

### 二、十二条里,判据换了方向的三条

**1. `MODEL_REFERENCE` 绕行缝(C-10)以拒绝的方式关闭,不是接通闭环。**
评审给的方向是 `provenance -> 授权主体 -> 同等校验`,并注明解析不了就
fail-closed。今天解析不了,而且缺的不是一句代码:`ProductAsset` 上没有指向
ModelTemplate 或授权记录的列;`MediaAsset.consent_id` 列在,而**全仓没有一个
写入点**;`MediaConsent` 又没有受众字段,连 §10.5 都判不了。在这三样齐之前写
一条"解析成功就放行"的分支,等于把一个永远走不到的放行路径摆在闸门上 ——
硬规则第 4 条点名的正是这种形状。所以落点是拒绝,并把运营指向"登记成
ModelTemplate 再选",那是唯一能执行四道检查的路径。

同批删掉了**第二道门**:`_build_request` 里"拿不到模板图就退回自由上传模特图"
的兜底。它比第一道隐蔽 —— 创建任务时模板是好的、四道检查全过了,而执行时
模板可能已被停用,于是 worker 静默换一张没有授权记录的图把钱花掉。

**2. 预算的"估不出来"分两种,只有一种该 fail-closed。**
`budget_verdict` 一直收 `estimated`,而唯一的调用方从来没传 —— 于是那道闸
判的是"已经超了没有",不是"这一次会不会超"。补上预估之后,`None`(估不出来)
与 `0`(免费)必须分开,前者走 UNKNOWN 阻断。

但"没配单价"直接一律 None 是不对的:默认部署(`DEFAULT_PROVIDER=mock`、
`PROVIDER_PRICE_BOOK` 空)下,任何一份设了预算上限的方案都会**永久无法创建
任务** —— 而 Mock 出的是本地假图,一分钱不花。那不是 fail-closed,是把演示
环境锁死。所以判据换成**这一家会不会真的花钱**(`is_simulator`,问实现类自己,
不查名单):没配价 + 模拟器 = 0 元(台账对这类调用记的确实是 0,两边一致);
没配价 + 真 Provider = None(它会收钱而我们不知道收多少)。

TOCTOU 那一半靠两样合起来解:按方案 id 的事务级 advisory lock(持有到提交)
+ 把"已经建好但一分钱还没记账"的任务算成预占。真正的预占要一张表,本轮不加列;
锁 + 在途统计能挡住"两个请求各自读到同一个 spent"那条竞态。

**3. `auth_mode` 从两档变三档。** `dev`(本机免登录)与 `token`(只配 Header
口令)原来合报成 `token`,于是"一切正常"和"浏览器彻底进不来"在前端是同一个值。
后者没人接手:`needsLogin` 要求 `sessionAuth`,`AppLayout` 不会跳登录页;而
浏览器不发 Header 口令,设置页那个输入框随口令链一起删了。于是界面给出的
每一句指引都指向一个不存在的输入框。

`resolve_identity` 那四个条件因此提成 `deps.dev_fallback_active()` ——
`/health` 与守卫必须由**同一个函数**回答,抄一份过去的表现是界面说"免登录"
而后端在 401。`client.ts` 的默认值同时从 `token` 改成 `unknown`:`token` 现在
有了自己那句很重的话,拿它当默认会让 `/health` 还没回来时的任何一个 401 都
声称服务器配错了。

### 三、角度:方案按角度验收,而生成不按角度执行

方案写 `FRONT×2 / BACK×1` 时,改之前的全部动作是把 `candidate_count` 设成 3、
往提示词里拼一句「拍摄角度:FRONT×2、BACK×1」,然后**一次**请求出 3 张。
请求里没有角度这个概念,候选图上也没有记角度 —— 模型完全可以回
FRONT/FRONT/FRONT,而 §6.5 严格要求两个角度各有覆盖。

修法是让角度成为**工作单元的维度**:

    workflows/generation_plan.angle_units / angle_assignments   纯判定,可穷举
    providers/base.AngleWorkUnit + work_units()                 Provider 只收"发什么请求"
    fashn.submit                                                 先按角度拆,再按单次上限拆
    mock.fetch_results                                           按角度分段,并把角度画到图上
    _persist_candidates                                          candidate_metadata.target_angle
    image_set_service._inherit_generated_angles                  入集时缺省继承

三条边界写在代码里,这里只记最容易写反的那一条:**先按角度拆,再按每家模型的
单次上限拆,顺序不能反。** 反过来会让一次 `num_images=3` 横跨两个角度,而那
一次只能带一句提示词 —— 于是"前两张正面、第三张背面"变成一句我们自己相信、
模型没听见的话。

候选图靠**提交顺序**对回角度。这个假设写在 `angle_assignments` 的文档里:
将来接一家不保序的 Provider,要么让它在候选图 metadata 里回带角度
(Mock 已经这么做,而且优先于按位置推),要么这个函数就不能用。

### 四、本轮仍然验不到什么

真库 pytest、Alembic 升降级、Redis / Celery 集成一次都没跑(按仓库约定,
本地协作默认不跑真实基础设施验证,需用户明确指令)。`docker build` 无等价物。
因此下面这些**只有纯层与人眼**:

    预算的 advisory lock 与在途预占     要真库才能验"两个并发请求真的被串起来"
    `_inherit_generated_angles`         要一行真实 GenerationCandidate 穿过去
    合规字段的 403                      要起 FastAPI + 真身份
    LOCAL 半配拒绝启动                  已用构造 `Settings()` 逐组合验过(六种),
                                        但没验"真的起不来的那个进程"


## §3.75 评审整改(REVIEW II.8 / III.2 / III.6 / II.1):四条能在离线门禁里真跑的,加上两条只能记下来的

这一节整理的是**一次外部代码评审**驱动的整改。评审把缺口分成两类:真跑得动的
(接真实 FASHN / 视觉模型 / 真实渠道)与今天就能改的。本轮只碰第二类,且每一条都
配了能进 `make check-offline` 的验证 —— 沿用本仓的铁律:**从未执行过的门禁 =
不存在的门禁**(§3.74 一)。真跑那一类留给有真实凭据/网络的机器,不在这里假装做过。

### 一、个人设置文件随交付包出去了(III.6)

`\.claude/settings.local.json` 被打进过交付包。它是 Claude Code 的个人权限配置,
里面是**开发机的绝对路径**(实测带出 `/c/Users/<user>/.claude/…` 这样的 Windows
用户名与本地路径)。它不像凭据那样致命,但和那张 5.8MB 的 `data/s1.jpg`(pack.sh
顶部记的那次)同类:**没有任何机制拦着它** —— 既不在 `.gitignore`、也没有代码或
文档引用,而它泄露的是打包者的机器信息。

处理走本仓既有的**两道拦截**(pack.sh 顶部的口径:`.gitignore` 与 `tools/pack.sh`
是两道):

    第一道  `.gitignore` 加 `\.claude/settings.local.json` —— 从源头不跟踪
    第二道  pack.sh 的 `FORBIDDEN_FILES` 与 pack.ps1 的 `$ForbiddenFiles` 各加
            `settings.local.json`(basename,任意层级都拦),打包后 FORBIDDEN 复验
            命中即删包退出

用 basename 而不是整棵 `.claude/`:约定上 `settings.local.json` 恒为个人文件,共享
设置走 `settings.json`,后者该跟着交付走。

**踩到一个坑,值得记下来给下一个加数组条目的人:** verify_delivery 有一条
「Linux/Windows 打包规则不许分叉」,它逐元素比较两个脚本的数组。pack.sh 侧用
`shlex.split(..., comments=True)` 解析,注释被整行剥掉;但 pack.ps1 侧用的是朴素
正则 `@\(...\)` **非贪婪**,遇到第一个 ASCII 右括号就截断 —— 于是在 `$ForbiddenFiles`
的注释里写了一对 ASCII 括号,就把括号后面的条目全漏在数组之外,两侧比较不相等、
门禁当场红,而错误信息只说「分叉」,不会告诉你是注释里的括号干的。**结论:
pack.ps1 的数组内注释不能出现 ASCII 括号。** 已在该处留了一行注释说明。

### 二、上传闸 20MB 与 FASHN 内联上限 10MB 的落差(II.8)

`MAX_UPLOAD_SIZE_MB`(默认 20)与 `FASHN_MAX_IMAGE_MB`(默认 10)是两道**不同且
不等**的门。一张 15MB 的商品图能通过上传、却在建任务后于 FASHN 侧才失败。原文案
四处各写一句「素材超过 10 MB」,运营无从判断:哪个环节的限制?上传明明放行了、
为什么现在不行?怎么修?

**为什么是改文案,不是把两个数字对齐:** 两道门服务不同目的 —— 上传闸是通用的,
Provider 内联上限是 FASHN 特有的,换个 Provider 可能允许 20MB。把它们钉成相等是错的。

**为什么没做「建任务期前置校验」:** 那才是评审说的「前置校验的时机」的最优解,
但它需要**建任务时就拿到确切的源图文件列表**,而这份列表是在 worker 里按 plan
解析出来的(`generation_service` 建任务时只定 provider,不定具体送哪几张图)。
在建任务路径上做校验会拿不到数据。所以本轮的取舍是:

    收敛四个 oversize 现场到同一条**运营可操作**的文案(`_oversize_error`)
    文案点名是 FASHN 的限制、说明上传闸其实更宽、给出修复动作(压到 10MB 以内再重试)
    失败仍发生在 provider 层、但在计费/提交之前 —— 时机没变差,可读性变好

**这条文案的初版自己踩了一次「给不出的建议」,记下来:** 初版结尾还写了「**或改用
不受此限的渠道**」。但 `providers/registry.py` 里注册的换装 Provider 只有 `mock` 与
`fashn`(`backend/app/providers/comfyui.py` 等有文件,没进那张表),运营照着去找会找到一片空白。
**一句办不到的建议比一句简略的报错更糟** —— 简略报错只是让人困惑,办不到的建议是
让人去做一件做不到的事,还会让他以为是自己没找到那个设置。写文案时想的是「要给修复
路径」,漏了回头问一句「这条路径存在吗」。已删掉该半句,并加了一条反向断言
(`test_message_does_not_advise_switching_to_a_channel_that_does_not_exist`)钉住它;
等真有第二个可用 Provider,由**调用方**把它作为参数传进来,而不是在文案里假设它存在。

文案逻辑落在 `app/services/upload_validation.py::provider_inline_size_message`,
它不读配置、不碰网络,所以有**能真跑的纯单测**(给字节数直接断言产出的句子)。
四处接线是否都改到、有没有一处还留着简略文案,压在读源码的
`tests/pure/test_provider_inline_size_message.py` 上(FASHN 的行为测试需要 httpx,
离线跳过,跑不到)。

### 三、`<ErrorNotice>` 迁移的棘轮(III.2)

失败提示的统一出口 `<ErrorNotice>`(A12)是全站口径,但仍有 **17 处**页面直接把
error 拍平成一句话喂给 antd 的 `<Alert description={readError(…)}>`,于是那些页面上
管理员拿不到请求编号与技术详情。评审的关键一句是「**且没有棘轮测试防止新增**」——
没有守卫,新页面照样会再写一个,债务不降反增。

本轮**不盲改这 17 处**(迁移是行为改动,而 tsc / Vitest / Playwright 在只有 python3
的机器上跑不了,盲改等于发一个没跑过的改动)。改为补上那道棘轮:
`tests/pure/test_error_notice_ratchet.py` 读前端源码冻结债务,只减不增。

**这道棘轮的第一版是弱的,而它弱在一个值得记的地方。** 初版只数
`<Alert…readError(…>` 这一种形状,数出 17 处、与评审点名的 17 吻合 —— 于是「吻合」
被当成了口径正确的证据。但那条正则只认现存代码恰好长的那个样子:

    <Alert type="error">{readError(e)}</Alert>            初版看不见(写成子元素)
    <Alert description={<span>{readError(e)}</span>} />    初版看不见(属性里套 JSX)
    <Result subTitle={readError(e)} />                     初版看不见(换个组件)

**而棘轮的全部意义就是拦新增的** —— 一道只认旧形状的守卫对新写法一律放行,
它看起来在守、实际不守,正是 §3.70 点名的「绿着的守卫说着一件不一定成立的事」。
数字对得上不等于口径对,这次是自己把巧合当成了验证。

现在的口径不解析 JSX,只问「这个 `readError` 是不是 toast」:

    宽口径(主基线)   pages/ + components/ 里**非 toast** 的 readError 调用点,合计 24
    窄口径(锚点)     `<Alert…readError(` 那一种,合计 17 —— 保留是为了不丢与评审
                     那条记录的对应,不是因为它够用

换写法绕不过宽口径。`message.error(readError())` / `notification.*` 是 `readError` 的
正当用法(一句话的浮层本就不展开技术详情),明确排除。另加一条**检测器自检**
(`test_the_detector_sees_the_shapes_that_defeated_the_first_version`),用合成样本钉住
上面三种形状,防止检测器再退回只认一种。

宽口径 24 是窄口径 17 的**超集,不等于「24 处都必须迁成 `<ErrorNotice>`」** —— 有些
计入的点(例如把错误串进「模特列表没拉到,下面是空的不代表没有模特」这类提示)未必
值得整块 `<ErrorNotice>`。这里守的是**这个数只能下降**,不是「每一处都是错的」。

**限度也要写下来:** 它仍是读源码的结构守卫,证明不了迁移后的页面真的渲染出了请求
编号(那属于 Vitest / Playwright,离线跑不了);而且任何基于文本的检测器都有边界 ——
把 `readError` 先赋给变量再渲染,它同样看不见。**它抬高门槛,不是不可绕过。**

17 处的迁移本身(改成 `<ErrorNotice error={原始error}>`)留给有前端依赖的机器,
清单在 `frontend/tests/ratchet-error-notice.test-notes.md`。

### 四、发布传输层的一条不变量:客户端超时必须钳在 LEASE_SECONDS 之下(II.1,预防)

评审把「发布重叠投递窗口」列为 P0 里的最高一条:worker A 领走租约 180 秒、发出真实
外部调用,调用挂住超过 180 秒,`claim_due()` 把它当成 A 崩了重新发出,于是同一份
报文发两遍(§3.19 论证的是「不会重复创建」,不覆盖「结果不会被回写」;A45-batch17-2
关掉了回写那一面)。

关键事实:**这条今天不可达。** `channels/registry.py` 里唯一的传输层是 Simulator,
没有任何真实 HTTP transport,也就**没有一个可以钳的客户端超时常量**。为一个尚不
存在的 transport 造一个投机守卫,是在钳一个还没有的东西 —— 那是过早的。

所以本轮把它落成**改动点上的不变量**,而不是守卫:在 `channels/registry.py` 传输层
注册处写清楚「第一个真实 transport 接入时,客户端总超时必须 < `publish_policy.LEASE_SECONDS`
(=180s),并确认平台幂等语义」。下一个加 transport 的人会在他正要改的那一行看到它。
等 transport 真的落地、有了那个常量,再把这条从文档升级成守卫(那时它才有对象),
并真跑已写未跑的 `test_publish_lease_concurrency_db.py`(7 条,需真库)。

### 五、本轮验不到什么(以及 a50/a51 的交接仍缺)

按仓库约定,本地协作默认不跑真实基础设施验证,需用户明确指令。因此本轮的验证是
**离线子集**:`test-pure`(2824/2824,含本轮新增 11 条)、`verify-delivery`
(18/19,唯一 FAIL 是「非 Git 工作树」——从 tarball 解出来的目录本就跑不了那条,
这正是它该有的作用)、`verify-imports` / `audit-*` 全绿。下面这些**没有**在本轮跑过:

    前端 tsc / Vitest / Playwright     装不了 node_modules —— III.2 的棘轮只守结构,
                                        迁移后的行为、以及任何前端改动都要在联网机器复跑
    真库 pytest / Alembic / Redis      发布并发那 7 条、FASHN 计费口径都要真库或真端点
    真实 FASHN / 视觉模型 / 真实渠道    评审 P0 的另一半,本轮一个字都没碰

**另外,交付一致性本身有一处欠账(评审 II.5,本轮未消除):** 包内时间戳显示 08-12
有一批改动(a50/a51:登录限流 `login_throttle.py`、`client_ip.py`、路由级代码分割、
nginx 安全头、`celery_app.py`、`db/session.py` 等),但 HANDOVER 停在 a48、STATUS 的
验证记录停在 08-09、本决策日志此前停在 §3.74(a49)。本节补上的是**评审整改**这一批
的交接;a50/a51 的完整交接 + 在有网络 + 真库的机器上重跑一遍完整门禁,仍是冻结交付
前的必办项。冻结前请补齐,别让「改了但没有交接记录」的批次带着一片离线绿出门。

## §3.76 视觉评分的限制对象是整份请求,不是单张图片

真实千问 Responses 兼容端点返回过一条明确的 400:
`Exceeded limit on max bytes to request body : 6291456`。当时一张候选 JPEG 本身约
5.8MB,没有超过 `VISION_MODEL_MAX_IMAGE_MB=8`,所以单图安全闸正常放行;但它转成
data URL 后会膨胀到约 7.7MB,还没算参考图、提示词和 JSON Schema,整份请求必然超过
端点的 6MiB 硬限制。

因此保留两种含义不同的上限:

    VISION_MODEL_MAX_IMAGE_MB      原始输入安全上限;在解码巨图之前拒绝
    VISION_MODEL_MAX_REQUEST_MB    整份模型请求上限;默认 5MB,低于端点硬限制留余量

评分器先用原图构造请求,放得下就不压缩。超限后用占位请求精确扣除提示词与 Schema,
只给真正内联的图片分配剩余空间;公网 URL 不参与。候选图优先取得 55% 图片预算,
参考图共享 45%,小图没用完的份额会归还。发送副本按「先缩像素、后小幅降质量」的
保真档位处理,候选图档位高于参考图;透明图用 WebP 保留 alpha,不合成背景。
请求构造后按真实 JSON 字节数复核,必要时始终从原始副本重新编码做比例收敛,
不反复压上一轮 JPEG。仍超限才在本地给出「减少参考图或启用公网 URL」的输入错误,
不再发一次已知会失败的付费请求。原始素材始终不改。

这不是把原有「单张超限直接拒绝」改成无条件压缩:超过原始输入安全上限仍会拒绝。
新增自动压缩只处理此前完全没有建模的**多图总请求预算**。

### 输出侧的第二个真实限制:1800 Token 装不下 FULL 评分

图片请求体修好后,同一个真实端点返回 HTTP 200,但状态是
`incomplete/max_output_tokens`:模型 `qwen3.8-max` 在旧默认 1800 Token 内没有完成
结构化 JSON。FULL 输出最多包含 11 个分数、问题、不确定项与事实一致性,再叠加
推理 Token,1800 没有可靠余量。

默认 `VISION_MODEL_MAX_OUTPUT_TOKENS` 因此升到 8192。它是允许上限,不是固定消费量;
通常按实际输出计量。**不对截断自动重试**:HTTP 200 说明第一次调用已经发生且可能
已经计费,后台无权把一次付费确认扩大成第二次。截断异常改为携带当前值、建议值、
`automatic_retry=false`,并保留响应 ID、usage、finish reason 与实际模型。数据库里若有
旧的 1800 覆盖值,仍尊重显式配置,由设置页改为 8192 或清除覆盖;不能在运行时偷偷钳高,
否则设置页显示 1800、实际发 8192,又制造一份假状态。


## A46 外部审查修复(P0-1 / P0-2 / P0-3 / P1-4 / P1-5 / P1-6 / F7 / F8)

**P0-1 交付包里的运行期日志。** `backend/.api-stdout.log` / `.api-stderr.log` 随包出去过两次,
里面是 uvicorn 的完整请求行与异常栈,含图片绝对路径与上游 URL 查询串。它和那张 5.8MB 的
`data/` 图同类:没有任何机制拦着它,既不在 `.gitignore` 也没有代码引用。三处同时补 ——
`.gitignore` 加通配加具名,`pack.sh` 与 `pack.ps1` 的禁品数组同位置各加 `'*.log'`。
ps1 侧的注释避开了 ASCII 右括号与单引号:`verify_delivery` 的解析正则非贪婪,
遇到第一个右括号即截断,而 `re.findall(r"'([^']*)'")` 会把注释里的引号内容也当成数组项。

**P0-2 生产评分丢弃已计费调用的元数据。** 截断与内容安全拒绝是**已经计费的成功 HTTP 调用**,
评分器在 `_extract_or_fail` 那一层已把响应 ID、模型名、usage、finish reason 挂到异常上。
`evaluation_service` 的 `ProviderError` 分支以前不取,于是最该对账的一类失败在
`evaluation_attempts` 里反而是空的 —— 而同一份数据在诊断路径上是落库的,两条路径就此分叉。
取值走 `getattr`:限流与超时是传输层抛的,请求根本没成功,身上没有这两个字段。

**P0-3 同步诊断调用无界。** 诊断跑在 HTTP 请求处理函数里(`asyncio.run`),人在页面上等。
按默认 `TIMEOUT=90 / MAX_RETRIES=2` 算最坏约 272 秒,链路上任何一个反代先超时,
连接就断了 —— 用户看到 502,而那几次调用已经计费且没有留痕。现在入口处封顶到 240 秒、
重试归零。**封的是副本不是原对象**:传输层每次调用现读 `self.config`,就地改会让这次诊断的
封顶永久留在调用方持有的那个实例上。`_transport.config` 必须一起换,否则封顶静默失效。

**P1-4 一条执行不了的截断建议。** 建议值以前恒为 8192,而 8192 正是默认值 ——
最常见的那次截断(默认配置跑 FULL 被截)给出的指示是「当前 8192,建议设为 8192」。
现在低于默认先抬到默认,之后翻倍递进并封在 `MAX_VISION_MAX_OUTPUT_TOKENS=32000`;
到顶后文案改为「已是上限」,不再说一句表单会打回的话。`reasoning_effort` 已是 low/none 时
不再追加「降到 low」。同时识别裸 `"incomplete"`(Responses API 在 `incomplete_details`
缺失时的形状)—— 漏掉它的表现是截断被报成「上游返回空输出」,把人引去查网络和鉴权。
设置页的 `maximum` 改为从同一个常量取,两边不再各写一个 32000。

> **测试改动一处,是有意的。** `test_vision_evaluator.py` 原本断言
> `recommended_max_output_tokens == 8192`,而 `configured` 也是 8192 —— 那一行固化的
> 正是上面这条空建议。它和「建议值必须严格大于当前值」直接矛盾,不存在同时成立的公式。
> 已改为 16384 并补一条 `recommended > configured` 的不变量断言。

**P1-5 压缩阶梯吃不下发给它的预算。** 最宽一档是 4096,一张 6000×4000 的原图哪怕预算
有 5MB 富余也会先被缩到 4096 再谈质量,而缩放不可逆、判断纹理与走线靠的正是那些像素。
两条阶梯各补原分辨率首档,用 `MAX_IMAGE_EDGE_PX` 当哨兵使 `scale` 恒为 1.0。
参考侧刻意只加一档且质量停在 90:要守的不变量是「同预算下候选图恒不小于参考图」。

**P1-6 预算算的和上线发的不是同一串字节。** 预算检查自己 `json.dumps`,发送交给 httpx 的
`json=` —— 两者序列化参数不同,长度不同。于是 pre-flight 说通过、线上回 413,而 413 被归类成
上游错误,排查方向从一开始就错。现在只有 `canonical_json_bytes` 一个序列化点,
`_send_once` 用 `content=` 把它的返回值原样发出去。`Content-Type` 由适配器显式写在 headers 里,
不依赖 `json=` 代设。连接探针也经 `_send_once`,一并受益。

**F7 日志编码与脱敏。** `ensure_ascii=False` 写进裸 `sys.stdout`,在中文 Windows(GBK)下
轻则日志不是 UTF-8,重则遇到编不出的字符抛 `UnicodeEncodeError` 被 logging 吞掉、**整条记录消失**
—— 而那往往正是出问题的那一条。现在优先 `reconfigure(encoding="utf-8", errors="replace")`,
退回 `TextIOWrapper`,再退回裸 stdout(编码不对是缺陷,起不来是事故)。
`message` 与 `exc` 补上值级脱敏:`redact` 按键名判定,管不到自由文本,而异常堆栈正是
密钥最常出现的地方。`httpcore` / `httpx` / `PIL` 单独钉到 WARNING。

**F8** `safe_request_summary` 的 `url` 过 `_safe_url`。这条摘要是默认一直记的,
不像完整 payload 要 `LLM_LOG_PAYLOADS=true` 才开;而自建端点把签名或 token 放查询串是常见做法。

---

## §3.77 a51:建档的四类失败提前到"下一步",以及 SPU 停用补上一个到不了的状态

两件事一起做,因为它们是同一个形状 ——**后端做完了,动线断在最后一跳**。
这棵树上这个形状出现过至少四次(`GenerationPlanPanel` 缺一个知道 SPU 主键的页面、
`facts_stale`、`variant_gate_roles`、a48 那两处死 import),每一次都不是缺功能,
是缺最后一根线。

### 第一节:校验的位置对了,时机错了

建档表单三步走完点「建档」才提交,而这四类失败**全部**只在那一刻报得出来:

    编码字符集     `sku_matrix.normalize_code`    错在第二步的输入框上
    重复颜色       `sku_matrix.expand`            错在第二步的输入框上
    行数上限       `sku_matrix.expand`            要选完模板才算得出,第三步
    编码已存在     `uq_spus_spu_code`             错在第一步的输入框上

四条里三条错在人已经离开的那一页上。而第三步那条 Alert 写着
「编码字符集、重复颜色、行数上限这些规则由后端校验,**上面的提示里有具体原因**」
—— "上面的提示"是一条 `message.error` 的 toast,它会自己消失。
**一句页面自己做不到的话**,和 §3.70 点名的那一族(算好了没人读)是近亲。

更值钱的一条:后端**早就**点名了是哪一行。`spu_service._api_loc()` 专门把纯层的
`variant_codes[1]` 翻成 `color_variants[1].variant_code`,而那个函数的 docstring 里
记着 A45-batch14-6 修它时的表现 ——「前端高亮不到任何一行」。翻它的唯一理由就是
让表单定位到行,而前端把这个结构丢在了 `describeError` 的 `` `${loc}: ${msg}` ``
字符串拼接里。**后端付了钱,前端没接**,而这件事不会有任何东西报错。

#### 解法不是把规则复制到前端

复制的下场是两个版本,先过期的那一个会让**合法输入在界面上被拒**,而运营无从申诉
(`schemas/spu.py` 顶部记着同类事故的原样:同一个字段 POST 有校验、PATCH 没有)。
所以方向是**把同一份规则提前问一次**:

    POST /spus/preview       试算。不写库、不要幂等键、非法入参走 **422 与建档同
                             一条 `_translate()` 路径** —— `loc` 逐字相同,前端一套
                             高亮两处都能用。做成 `{ok:false,problems:[]}` 的话,
                             同一件事就有了两种说法(AC-05 那条注释里的同一句)
    GET  /spus/code-rules    字符集、分隔符、各段上限。**只喂提示文案与输入归一化**,
                             判定仍然只有 `listings/sku_matrix` 一处

试算里没有一条新写的判定:四类失败各自对应一个既有函数,新写的只有"什么时候问"。
为了让第二步(还没选尺码模板)也问得动,把 `expand()` 里的颜色三条抽成
`normalize_variant_codes()`,`expand()` 改调它 —— **抽出来而不是抄一份**,
所以两条路径连 `field` 的下标口径都是同一份。

#### 三个刻意做成这样的取舍

**`sku_count` 可空。** 没给尺码模板时是 `null` 不是 `0`。界面上"算不出"和"零行"
长得一样,而后者会被读成"这次建档一行 SKU 都不会产生"。

**`code_taken` 是标志位不是错误。** 运营边打字边触发试算,`SW-0` 打到一半就红一次的话,
他会先学会忽略这个提示,然后连真的那次也一起忽略。

**第三步显示的 SKU 换成后端算的那份。** 上一版只显示 `颜色数 × 尺码数`,页面里那段
长注释论证的是"编码怎么拼住在 `sku_matrix`,前端拼一份的话界面和库里就是两套编码,
而运营会拿界面上那个去平台后台搜、搜不到、然后怀疑没建成功"。**那个论证是对的,
而它当时的结论(所以只预览数量)是当时唯一的选项** —— 没有任何端点能在不写库的
前提下回答"这次会建出哪几行"。试算端点补上之后顾虑本身就没了,列表走的是 `expand()`,
前端仍然一个字符都没拼。

### 第二节:`SpuStatus.DISABLED` 从落枚举那天起就到不了

那个取值的注释写着「停掉了。**不删** —— 素材、事实、图片集都挂在它下面」,
而全仓**没有任何一条路径迁得到它**。一个定义了却到不了的状态,和没有这个状态是一样的。

同时 `spusApi` 的四个写方法(`update` / `addColorVariant` / `updateColorVariant` /
`createSkus`)**全树零调用点** —— 后端端点齐了、前端客户端也写了、UI 没接。

补的是 `POST /spus/{id}/disable` + `/restore`,口径照抄 `product_service.archive_product`
(它的 docstring 已经把"为什么不是 DELETE"那笔账算完了:`products.id` 被九张表引着,
RESTRICT 撞 500 / CASCADE 清空整条证据链)。全仓仍然只有一个 `DELETE` 路由,在生成任务上。

#### 停用**不**连带归档底下的 SKU

刻意的,而且是这一节最容易被改错的一条。归档一行 SKU 有它自己的闸(平台还挂着就拒)、
自己的理由、自己的审计记录;一个按钮背后连带迁移十几行的话,那十几条审计记录的理由
会全是同一句,而其中某一行可能正卡在平台驳回回流上。

所以停用的语义窄而清楚:**这个款不再接受新的颜色与 SKU,并从建档侧的列表里消失**。
已经在生产动线上的那些行按自己的节奏走完。`_assert_open_for_building()` 在
`add_color_variant` / `create_skus` 上拒绝停用中的 SPU —— 不拒的话「停用」就只是一个标签。
闸放在 service 层而不是接口层:批量导入也会落 SKU,那条路径不经过 `api/spus.py`。

#### 停用要理由、要平台闸、**不要**版本号

理由必填进审计(判据与 `ProductArchiveIn` 逐字相同:三个月后复盘"这个款当初为什么
不做了"时,只有它答得出来)。平台闸走 `product_service.live_listings_for()` ——
**「还挂着」全仓只有那一个定义**,在 `spu_service` 里再写一遍 `notin_(...)` 的话,
`_PUBLISH_SETTLED` 就有了两个读者而只有一个会跟着改(它已经被改过一次,
DELISTED 是后补进去的)。

不要 `expected_version`,与 `archive_product` 同一条:它是**带闸的状态迁移**,
不是字段编辑。要版本号的话一次双击的第二下会撞 409「已被其他人更新」,
而那句话在双击这个语境下是假的。字段编辑那一侧(`update_spu` / `update_color_variant`)
仍然要,详情页把 409 翻成一颗刷新按钮。

#### 列表默认过滤,而这对存量数据是恒等的

`list_spus` / `count_spus` 加 `include_disabled=False`,两者共用 `_visible()` ——
各写一遍 `where` 的下场是分页错位:总数把停用的算进去、这一页没有,于是最后一页是空的,
而空态写着"还没有商品"(`api/workbench.py` 的归档过滤踩过同一个形状)。
默认过滤**不改变任何存量行的可见性**:停用端点存在之前没有任何路径写得出 DISABLED。

#### 写操作挂详情页,不挂 SPU 聚合页

理由与 `GenerationPlanPanel` 当初的逐字相同:聚合页按 `products.spu` 字符串分组,
一行可能对应"没有主键的老商品"(`spu_id` 为 `null`),而写操作必须有主键。
聚合页只在后端真给了主键时给出通往详情页的链接。

`SpuOut` 顺带补了 `notes` 一列 —— `SpuPatch` 收它,而出参里读不到就改,那不是编辑是覆盖:
表单打开时是空的,保存一次就把别人写的备注抹掉了,而没有任何地方会报错。

### 第三节:这一轮的守卫自己骗过我两次

变异验红 11 条,第一遍**三条没红**,而其中两条是我写的守卫在说假话。这两条值得记下来,
因为它们是 `tools/audit_source_guards.py` 管不到的方向:

    A 没红   `"max_variant_code": MAX_VARIANT_CODE` 改成 `16`,守卫照旧绿 ——
             它比的是**值**,而两者的值恰好相等。**值证明不了出处**,
             而"把常量抄成字面量"正是这个函数要防的那件事本身。
             改成 AST 守卫:返回的字典里不许有裸 `Constant`,唯一例外是
             `normalizes_to_uppercase: True`(它描述行为,没有对应的常量可指)
    E 没红   `live = product_service.live_listings_for(...)` 整句换成 `live = []`,
             守卫照旧绿 —— **正向断言命中的是那个函数 docstring 里的同一串字**。
             `audit_source_guards.py` 只管反向断言(「不许吃切窄的源码」),
             正向这个方向全仓没人管,而本仓库的注释密度恰恰让它高发
    F 没做   锚点命中 0 次。我的行尾探测按整个文件猜一种,而 `spu_service.py` 是
             **混合**的(老代码 CRLF、后补的段 LF)。这是 a48 那次 `sed` 翻车的
             变体 —— 幸好"命中数必须为 1"那条断言在,否则它会被读成"守卫不设防"

结论有三条:**反向断言要吃去注释的源码,正向断言也要**;比值比不出出处,
要出处就得走 AST;混合行尾的树上,锚点必须两种形式都试。

## §3.78 界面用语归一:后端枚举不许直接摆在界面上,英文名一律给中文对照

走查起因是一句很短的抱怨:「很多地方中英文混杂很奇怪」。查下去是**三件不同的事**,
它们表现相似而成因完全不同,分开记,因为修法与再犯的方式都不一样。

### 第一件:标签表漏了,枚举值直接进了下拉框

`ProductFormModal` 的品类下拉写的是 `options.map((v) => ({ value: v, label: v }))` ——
运营在录商品时看到的是 `SWIM_BRIEFS`、`TIE_DYE`、`THREE_QUARTER`,而不是
「三角泳裤」「扎染」「四分之三侧身」。同样的形状散在六处:品类、图案、复杂度、
模特姿势、体型、素材状态。

**这不是遗漏了几行文案,是漏了一整类表。** `test_frontend_contract.py` 的
`_label_contracts()` 逐张比对「后端枚举 ↔ 前端标签表」,而它比的是**表里已有的那些**:
一张压根没建的表不会让任何一条断言变红。那份清单是白名单,不是全集扫描 ——
它拦得住「后端加了一个取值而前端忘了翻译」,拦不住「这个枚举从来就没有过标签表」。

本轮补的六张(`GARMENT_TYPE_LABEL` / `PATTERN_TYPE_LABEL` / `COMPLEXITY_LABEL` /
`POSE_LABEL` / `BODY_TYPE_LABEL` / `CANDIDATE_STATUS_LABEL`,以及
`SPU_STATUS_LABEL`、发布域那四张)**刻意没有加进那份契约清单**。加进去要连着
改后端那张表的进出口,而本轮是文案整改不是契约变更;它们眼下由 tsc 的
`Record<string, string>` 与调用点的 `?? v` 兜底。**下一个动这块的人应当把它们补进
`_label_contracts()`** —— 那才是让它们不再漂移的地方。

品类里有三个值没有硬翻:`TANKINI` 写成「坦基尼(TANKINI)」。中文行业里
它就叫坦基尼,硬造一个「分体背心式泳衣」搜不到货也对不上供应商的说法 ——
**「翻成中文」的目的是让人看懂,不是让页面上没有英文字母。**

### 第二件:同一个概念在界面上有两个名字

`Provider` 在界面上出现 30 余处:菜单项、表头、表单标签、错误提示、
Tooltip。而同一个东西在别处叫「出图服务」「厂商」。归一成两个词:

    出图服务商   特指出图那一家(创建任务、生成方案、任务详情、审核页)
    服务商       跨出图与评分两类的场合(花费页、服务商列表页)

同一类归一还有:`Simulator` → 渠道模拟器,`seed` → 随机种子,
`worker` → 后台执行进程,`claims` → 卖点声明,`Session` → 会话,
`Mock 演示` → 模拟演示,`admin`/`operator` → 管理员/运营。

**留英文的三类,是判据不是例外:**

    环境变量名与命令   `ADMIN_PASSWORD`、`docker compose ps`、`.env`
                       —— 它们是要被照抄进终端的字面量,翻译等于写错
    要填进提示词的值   主颜色 `black`、背景 `studio`
                       —— 这两个字段原样拼进出图提示词,填中文出来的图是错的。
                       所以不是留英文就完事,得**说明白为什么**:两处都补了
                       `extra="填英文,会直接进出图提示词"`
    诊断码             `UNMARKED_SHARED_IMAGE`、`CLAIM_NOT_IN_FACTS`
                       —— 中文说人话的那句在前,码在后面跟着,给转述用

「中文(英文)」只在**两者都要**时用:`泳装(swimwear)` 里中文给人看、
键给人照着去找 `spec/swimwear.yaml`;`运营(operator)` 里中文给人看、
英文对得上配置项。只留一边都会丢掉一半用处。

### 第三件:标点两套,而全仓早有事实标准

`src/` 下中文半角逗号 3012 处、全角 43 处 —— 后者集中在三四个文件里,
是分批交付时带进来的。没有人写下过这条约定,所以每一批都自己选了一次。
本轮把那 43 处按多数归一(连带 `“”` 归成 `「」`),**记在这里就是为了让下一批
不用再选一次**。

### 一处顺带修掉的重复

`DraftTab` 的图片映射告警原来是「图片映射未算过」后面再挂一个印着 `UNPROVEN`
的 Tag,而卡片标题上已经有一个同款中文 Tag —— 同一件事在同一屏说了三遍,
其中一遍是英文。删掉英文那一遍之后 `draft-image-preview.test.tsx` 当场变红,
因为它断言的正是 `getByText('UNPROVEN')`:**那条用例守的是「两档分得开」,
而它一直是靠界面上那个英文枚举值来分的。** 断言改成读中文文案,守的事没变。

同样形状的还有 `tests/unit/workbench.test.ts` 那条 `detail` 断言 ——
`detectFlowAnomaly` 的异常详情原来直接拼 `COPY` / `DRAFT` / `BLOCKED`,
而那段话最终显示在工作台的「状态异常」浮层里,唯一用处是被运营转述给开发。
转述不出去的字符串等于没写。

## §3.79 A52:界面上的英文标识符、一条走不通的复活路径,以及 a51 走查的九条

三件互不相干的事,合在一批里,因为它们的共同点是**代码各自正确、接缝处不对**。

### 一、`重点检查:cup_shape · shoulder_strap` —— 词表在,没人翻

界面上有三处直接把标识符摆给运营:商品头部的 `重点检查`(规则包的
`review_checks` 键)、颜色维表头(`MATERIAL / ATTRIBUTE / PLAN / …`)、
颜色维明细里的 `缺:primary_color`。

**中文名放在哪里,取决于键定义在哪里**,而不是取决于哪里显示:

    属性字段    注册表 `AttributeField.label`。**没有默认值** ——
                新注册一个字段忘了给中文名会在导入期 TypeError,
                而不是让 `waistband_type` 悄悄出现在四个显示位上
    重点检查项  spec 的 `review_check_labels`,和 `review_checks` 同一屏。
                少一条**加载期直接拒**(`ChannelSpecError`)——
                与本模块顶部那句"解析不了的 source 在运行期只是
                '这个字段是空的'"同一条理由
    流程步骤    前端已有 `FLOW_STEP_LABEL`,颜色维表头只是没去读它

判定层不许自己去查词表:`color_flow` 被
`test_a45_batch25_color_substate` 钉死只能 import `workbench.flow`,
所以词表由调用方(`color_rollup`,它本来就拿着 `REGISTRY`)递进 `ColorView`。
那条守卫拦对了 —— 我第一版直接 import 注册表,当场变红。

界面上**两个都给**:中文名在主位,字段名压成第二行小字或 tooltip。
只留中文的话,运营描述得出问题却指不出是哪个字段;只留英文就是现在这样。

### 二、删掉再传回来:一条承诺了却走不通的路

`delete_asset` 的文档写着「误删的补救是重新上传或重新出图」。**那句话是假的。**

删除是软删,`MediaAsset` 那行还在;去重键不看状态;老的 `ProductAsset`
压根没有软删这回事。三条合起来:

    删掉一张素材 -> 再传同一份字节
    -> `asset_service._find_same` 在 ProductAsset 上命中,**直接 return**
       (影子写都没跑到)
    -> 界面说「这张图已经在该商品下了,沿用已有素材」
    -> 素材列表里一张图都不多

运营看到一句"成功"和一个没有变化的页面,而且**没有任何出路**。

修在两处,缺一不可:`ingest` 命中去重时复活 DELETED 的行(判定抽进
`media/revive.py`,零依赖、可穷举);`upload_asset` 命中去重时**照样跑影子写**
—— 后者是整条修复里唯一没法用纯判定表达的一跳,由一条 AST 守卫钉着。

复活只对 DELETED 生效。放宽到 QUARANTINED 的话,重复上传就成了一条绕过
人工复核的路 —— 那条穷举断言(`test_every_media_status_is_covered_by_exactly_one_branch`)
守的正是这一格。

### 三、a51 走查:九条,而两条 P0 是"32 条守卫全绿"的那一类

a51 给建档试算写了 32 条守卫,**全是读源码的**。漏掉的两条恰好是源码看着
完全正确、跑起来才错的:

    第一步的试算从来没通过    `ColorVariantCreate.variant_code` 带
                             `min_length=1`,而第一步的颜色表是一行空编码 ——
                             **pydantic 先于服务层拒**。四类失败里唯一能被
                             提前的 `code_taken`,一次都没回来过。
                             页面注释还写着"试算必然报『至少要有一个颜色
                             变体』",而实际报的是另一条,英文原文
    `loc` 有两种方言          `_api_loc()` 产出 `a[0].b`,`main.py` 的
                             `validation_handler` 产出 `body.a.0.b`。
                             前端只认前者,而**最常见**的那几类错
                             (长度超限、颜色数超上限)都被 schema 先接走

两条都不是"某一行写错了",是"两段各自正确的代码接不上"。读源码的守卫
看不见接缝 —— 这一批的 11 条断言因此全是行为断言。

其余七条:`msg` 里的内部形参名(`normalize_code` 现在分开收 `field` 与
`label`,而不是在服务层做"把开头那个词摘掉"的兜底 —— 兜底会让下一条按老
样子写的消息静默被修好);详情页没接 `codeRules` 与 `onFields`
(硬编码的 `16` 和后端常量恰好相等,所以任何比值的守卫都是绿的 ——
变异 R1 教的"值证明不了出处"在前端复现了一遍);停用的平台闸用
`skus_of()`(按外键查,而 `Product.spu_id` 可空、老导入只写字符串码 ——
**安全闸和权威口径对"错"的容忍方向相反**,所以是新函数
`rows_touching_spu`,不是改 `skus_of`);停用/恢复无锁却写 `row_version`;
对话框承诺的「显示已停用」勾选框不存在;`code_rules_out` 的 docstring 把
`model_validate` 挡得住的方向说反了;以及尺码数用除法算(变异 F2 挡的
"前端自己乘"的另一半)。

### 一条守卫因为这批被放宽,记在这里

`test_a45_batch14_2_fixes` 的挂载守卫原来钉的是**整行**
`<MissingEvidenceNotice evidence={...} />` —— 给组件多传一个 prop 就会变红,
而多传一个 prop 不是回归。放宽成"行首的标签",它仍然挡得住
`{false && ...}`(那会让行首锚定不成立),这一点当场反向验过。
`mutate_batch14_2.py` 的 N10 锚点跟着改,16 条变异仍然全红。

## §3.80 a53 运行日志控制台:归类、展示、原文,以及一条从来没有人读过的日志

设计文档在 `docs/LOG-CONSOLE.md`,原型在 `docs/log-console-prototype.html`。
这一节只记**取舍**与**代价**,不重复设计。

### 一、病灶不在采集面,在采集之后

采集是健康的:210 个调用点、190 个带结构化字段、脱敏有测试钉着、
编码问题有注释记着教训。不满意的是三件事:

```
归类   唯一的机器可读分类是 logger(代码结构)和 message(自由英文句子)
展示   唯一的查看工具是一条只看 AI 调用的命令行
原文   模型的请求与响应正文默认不留;留了也只能在那条命令行里看
```

所以这一轮**一个采集点的写法都没改**(除了补 `event=`),动的全是采集之后。

### 二、message 不能既当人话又当键

`tools/watch_ai_logs.py` 的过滤集是 9 条硬编码的消息原文。措辞一改,
查看器**安静漏事件** —— 不报错、不提示,就是少了。而这个仓库的注释文化
天然鼓励人去改措辞:一句话说得不够准就该改。

与此同时,新代码已经自发长出了另一种写法 —— `batch.stale_outcomes`、
`publish.poll_status_changed`、`spu.create_reused_request_key` 拿短码当 message。
**写日志的人已经在要一套分类法,只是没立起来。**

现在立起来了:`app/core/log_events.py` 是唯一事实来源,`event` 是键,
`message` 退回它该干的事。原来那些短码全部挪进 `event=`,message 补回人话。

### 三、双向守卫,不是单向

守卫最容易写成单向的:"写了的必须登记"。那样漏掉的是反面 ——
一个登记了却没人写的码,会在筛选下拉里摆出一个**永远筛不出东西的选项**,
而运营会把它读成「这段时间没发生」,不是「这个码是假的」。

所以 `test_a53_log_console.py` 两个方向都断言,而且事件码是从
**字典字面量**里扫的,不是从 `logger.xxx(...)` 调用点里扫的 ——
传输层把字段先攒进一个变量再传给 logger,按调用点扫会漏掉那两条,
而漏掉的表现是"这个码没人写",于是反向断言会反过来冤枉注册表。

### 四、载荷与事件分路:两个去向、两个开关、两套寿命

`LLM_LOG_PAYLOADS` 默认关,那个决定**今天依然成立**,这一轮一个字没动:
它管的是归档面,而归档面会被采集、被复制到别处,不该躺着一份受授权约束的
商品数据。

错的从来不是"默认关"这个决定,是**"要么进归档面、要么不留"这个二选一**。
新增的 `OPS_LLM_PAYLOAD_CAPTURE` 默认开,因为它的去向完全不同:
有 TTL(24h)、有管理员闸、不出本机 Redis。风险面不同,默认值因此可以不同。

这条必须默认开,理由是"打开开关重跑一遍"对模型问题基本无效 ——
值得排查的失败恰恰是**不可复现**的那些:偶发的格式跑偏、偶发的限流、
某张图触发的拒答。等你开了开关,它不来了。

脱敏**没有第二套**:旁挂库复用 `safe_payload_for_log`,只把字符串上限做成
参数(12k → 40k,因为系统提示词截在 12k 会把"输出要求"那段切掉)。
图片正文两边都永不留存。新开一个去向最容易漏的就是在新去向上把老规矩忘了,
所以那一条有 AST 守卫 + 一条真的构造 base64 与明文密钥的运行时用例。

### 五、日志绝不反噬业务

环形缓冲挂的是**第二个** handler,stdout 那条链路一个字节没动。四条硬要求:

```
超时 0.2 秒          Redis 卡住不能让一次出图调用跟着卡住
异常静默吞掉         但**不瞎** —— dropped_since_boot 随 /meta 暴露
失败后进冷却期       少了它,Redis 挂掉时每一条日志都要付 0.2 秒,
                     而那正是"日志系统把应用拖垮"的经典形状
handler 内不打日志   redis-py 的 logger 钉到 CRITICAL 且 propagate=False,防递归
```

`handleError` 也覆盖掉了:标准库默认往 stderr 打堆栈,而那会在 Redis 挂掉时
给每一条日志配一份堆栈,把 stdout 采集面淹掉 —— 淹掉的正是出事时唯一还
靠得住的那一面。

### 六、界面上说不出来的事,不许画成空的

三处都是同一条规矩(硬规则第 4 条)的具体形状:

```
环形读不到      界面说「读不到」,不画空列表 —— 空列表的意思是"这段时间没有日志"
载荷取不到      404 分两种措辞:「没开捕获」去改配置,「已过期」下次能查到。
                一个空面板说不清是哪一种,而它们的下一步完全相反
窗口边界        oldest_ts 明说 —— 查不到更早的不是没发生,是滚出窗口了
```

`routine`(能不能折叠)由**后端**判,而且 ERROR 永不折叠。这一条不能挪到
前端:"什么时候可以藏一条日志"是业务规则,而前端藏错了没有任何人会发现。

### 七、捡到的缺陷:14 个调用点的字段从来没进过日志

接线时撞出来的,不在设计范围内:

```python
logger.warning("could not write the in-flight marker", extra={"key": ..., "error": ...})
                                                              ↑ 少了 extra_fields 那一层
```

`JsonFormatter` 只读 `record.extra_fields`。少了包裹,`logging` 会把这些键挂到
record 上然后**没有任何人去看**。不报错、不提示,那条日志只是比作者以为的
少了一半 —— 而作者是在出事时才会去读它的。

14 处里最贵的是 `batch.billed_result_unknown_refusing_paid_retry`
(已计费但结果未知,拒绝付费重试):`key` / `action` / `status` 一个都没落地,
于是"到底是哪一件被拒了"这个问题在日志里查不到答案。

这一类缺陷的特征和 §3.43 那一族一样:**两端各自都对,缝在中间**。
调用点写得很认真(字段挑得准、截了长度),formatter 也没错(它只答应读
`extra_fields`),错在没有任何东西在看两者对不对得上。现在有了(守卫四),
而且配了一条**真的跑一遍 formatter** 的用例钉住理由 —— 免得下一个人
把守卫读成风格洁癖然后加个白名单绕过去。

### 八、顺手还的一笔:`DECISIONS.md` 里 §3.78 被整节贴了两遍

`verify_delivery.py` 的「决策日志编号不重复」这一项在本轮之前就是红的:
§3.78 出现在第 5689 与第 5855 行,而且后者是前者**逐字的副本**。
删掉了重复的那一份。它不是本轮引入的,但一个长期红着的交付自检项
会让人习惯"这一项本来就红",而那是所有门禁失效的第一步。

### 九、没做的

浏览器一次都没实测(Playwright 在任务 24);真 Redis 一次都没连过
(环形写入、旁挂库、TTL 到期都只有假客户端覆盖);`pytest` / `make test-nodb`
在本机跑不了(没装 fastapi / sqlalchemy),所以三个新端点**没有被
TestClient 打过**。逐条也记在 `docs/LOG-CONSOLE.md` 第十一章。

## §3.81 a54:运行日志控制台的十处修复,以及一条锚在会变的数上的变异

a53 把控制台立起来了,走查发现十件事**没有任何东西守着**。它们的共同形状:
不报错、不变红,只在某个具体时刻让控制台少说一句真话。逐条记在
`docs/LOG-CONSOLE.md` 第十二章,这里只记三条需要点头的取舍。

### 一、worker 的日志:接 `worker_process_init`,不接 `setup_logging` 信号

a53 只在 `app/main.py` 顶层调过 `setup_logging()`,而 worker 的入口是
`celery -A app.tasks.celery_app.celery_app worker` —— 它不 import `app.main`。
于是**环形里一条 worker 日志都没有**,而"为什么是 Redis 而不是进程内环形"
的全部理由就是那 59 个住在 worker 里的调用点。

两个候选钩子,取前者:

    worker_process_init   prefork 的每个子进程各装一次。`seq` 是进程内计数 +
                          `os.getpid()`,父进程装一次再 fork 会让几个 worker
                          共享同一个进程标识,跟随模式的去重会把不同 worker
                          的同序号日志当成同一条丢掉
    setup_logging 信号    接上它等于告诉 Celery「日志我全包了」,连它自己的
                          启动横幅和任务生命周期行都要接管。这里只要 root
                          handler 是我们的,不需要那么大的责任面

### 二、控制台自己的访问日志不进环形

中间件对每个请求写一条 `http.request_completed`,包括 `/api/ops/logs` 自己;
跟随模式 3 秒一拍 = 1200 条/小时,cap 5000。**四小时后环形里全是"某人在看
运行日志"**,而这些行标了 routine、在流视角里折进计数条,`held/cap` 显示
5000/5000 —— 排障的人一边盯着页面,一边把自己要找的证据顶出窗口。

边界两条:只挡 2xx/3xx(这一页自己 500 了是要看见的),只挡环形不挡 stdout
(归档面一个字节没动,采集端仍然收到全部访问日志)。

### 三、变异的锚点不许包含被守卫读的那个数

`tools/mutate_a46_phase5.py` 的 N1 锚在「一共 19 份」上,a53 往文档地图里加了
一份文档,表格变 20 行、那句话跟着变,锚点从此找不到 —— 而失锚的变异
**什么都没验**,它只是在报告里占一行。这类锚点的寿命等于「下一次有人加一份
文档」,所以重新对准成 20 是把同一颗雷埋回去。

改成锚在不含数字的那一段上,把过期的数插到前面:守卫读的是第一个正则命中,
「说的数」与「表格行数」照样分叉,而锚点不再随文档增删过期。N2 是同一个病灶,
一并改掉。

顺带一条自我验证:`tools/mutate_a54.py` 的 P1 第一次跑出来是 GREEN —— 守卫
写的是 `assert "_matches" in text`,而变异把调用改名成 `_matches_removed`,
子串照样在。**一条按子串判的断言挡不住把函数换掉,而换掉正是它要挡的**。
改成按 AST 的调用名比,并按行号比先后。这一条就是变异验证存在的理由。
