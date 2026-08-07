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
