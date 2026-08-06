# a28 前后端联合检视报告 · 整改跟踪表

> 基线:`swimwear-imagegen a28 前后端联合代码检视报告`
> 本次复核:A35(代码状态 = a35 交付包)
>
> 报告第 10 节要求「以本报告为基线建立统一整改表」并规定了字段。
> 这就是那张表。**它放在 `docs/` 而不是 HANDOVER 里,因为 HANDOVER 每轮
> 整份替换** —— 跟踪表跟着被替换掉的话,下一轮就得从头再核一遍 20 条。

## 怎么读这张表

- **已关闭**:代码里查得到证据,且有一条测试盯着它不被改回去
- **半场**:一半做了另一半没接,差的那一半写在备注里
- **待处理**:没做,理由写在备注里 —— 两条都有具体理由,不是排期没排到

「自动测试」一列填的是**改回去时会红的那条**。填不出来的地方就写「无」,
不写"有测试覆盖"这种查不到的话。

---

## 一、20 条阻断项

| 编号 | 严重 | 模块 | 状态 | 修复版本 | 自动测试 | 备注 |
|---|---|---|---|---|---|---|
| BLOCK-01 | 高 | 图片集 | 已关闭 | ≤a33 | `test_image_set_creation_checks_asset_ownership` | `_assert_items_belong_to_spu` + `_assert_spu_exists` |
| BLOCK-02 | 高 | 图片集 | **待处理** | — | `test_variant_coverage_gap_is_visible_even_though_it_does_not_block` | 规则本身是对的,**没有任何写入方给 `variant_id` 赋值**,所以恒不触发。后端 `variant_coverage` 已算出并回在详情响应与批准审计里,**前端零消费**。**A43 更正:这一条不卡业务决定。** `generic._image_bucket()` 里已经写死了优先级(变体专属图优先、通用图兜底、**绝不回退其他变体**),所以"以哪个为准"在代码里早就有答案了。真正欠的是四件工程活:① 稳定的 variant ID(今天是 `primary_color or sku`,改颜色文案就换身份);② 图片绑定 UI(前端新增图片仍硬编码 `variant_id: null`);③ 批准阻断规则;④ 存量通用图的迁移策略。把它继续挂成"等业务"会让一个可执行的任务长期停摆 |
| BLOCK-03 | 高 | 批次 | 已关闭 | ≤a33 | `test_batch_retry_respects_the_execution_mode` | 重试与创建走同一套 `BATCH_EXECUTION_MODE` 判定 |
| BLOCK-04 | 高 | 批次 | 已关闭 | a33 + **a34** | `test_a_finished_status_with_unfinished_items_is_not_settled` 等 8 条 | a33 接上了轮询与 `dispatched`;**a34 才补上重试路径那个洞** —— 终态 `job.status` 配未完成条目时曾判成 SETTLED,轮询彻底停止 |
| BLOCK-05 | 高 | 写请求 | **半场** | ≤a33 | `client.test.ts` / `error-and-cold-start.test.tsx`(行为) | `describeError` 会把超时表达成「结果未知」,主链路 14 个文件已接 `useWriteError`。差的是:仍有约 17 处 `Alert + readError`,**文案对但技术层没有落点**(错误码/请求编号显示不出来),且**没有任何一条测试去数调用点**,加第 18 处不会有东西报错 |
| BLOCK-06 | 高 | 任务 | 已关闭 | ≤a33 | `test_force_retry_sends_every_query_param_the_backend_declares` | `ForceRetryModal` + `force=true` + 对账结论必填并写审计 |
| BLOCK-07 | 高 | 任务详情 | 已关闭 | a32 + **a34** | `test_frontend_terminal_task_statuses_match_the_state_machine`、`test_frontend_awaiting_human_statuses_match_the_state_machine` | 终态清单不再含 `AUTO_APPROVED` / `MANUALLY_APPROVED`;a34 把中间那档(`AWAITING_HUMAN`)也钉到后端并改成**逐值正向比对** |
| BLOCK-08 | 高 | 审核 | 已关闭 | ≤a33 | 无(前端行为,`ReviewDetailPage` 的 `decisionBusy`) | 三个决策按钮共用一把锁;`loading` 仍各归各的,转圈要转在真正被点的那个上 |
| BLOCK-09 | 高 | 工作台 | 已关闭 | ≤a33 | 无(`WorkbenchListPage` 单一出口 `changeFilter()`) | 六个筛选入口全部走同一个函数并清选择 —— 靠只有一个口子,不靠每处都记得写 |
| BLOCK-10 | 高 | 属性 | 已关闭 | ≤a33 | 无 | 只清**这次提交的那几个字段** |
| BLOCK-11 | 高 | 属性 | **半场** | a31 / a43 | `tests/pure/test_attribute_validation.py`(19 条) | **A43 更正:上一版描述过于乐观。** 原文写的"只差一个对象编辑器"会让下一位复核者以为前端补个控件就完了,而真正的缺口在后端:接口收 `Any`、不验枚举、不验单值/多值、不按注册表 owner_type 写入、VARIANT 契约未组装。**a43 已修**:`attributes/validation.py` 挂在唯一写入点 `set_value()` 上,`owner_for()` 让层级只从注册表取,`CanonicalVariant.attrs` 第一次被真正填充。**仍差**:结构化对象编辑器(注册表今天没有这类字段);以及全部改动**未经真库 API 测试** |
| BLOCK-12 | 高 | 图片集前端 | 已关闭 | ≤a33 | `test_every_query_handle_is_checked_for_failure`(a35 起) | `ImageSetTab` 三条 query 失败不退化成空集 |
| BLOCK-13 | 高 | 导出回流 | 已关闭 | ≤a33 | 同上 | `ExportTab` 驳回查询失败不按「无驳回」计算 |
| BLOCK-14 | 高 | 提示词 | 已关闭 | ≤a33 | 无 | 预检失败有独立错误态;`updated_by` 取 `actor`,不再从请求体拿 |
| BLOCK-15 | 高 | 安全 | 已关闭 | ≤a33 | `test_error_redaction.py` | URL、查询串、Authorization、token、key 一律不落库 |
| BLOCK-16 | 高 | 发布 | 已关闭 | ≤a33 | `test_publish_renews_the_lease_before_each_call` | 每次调用前续租 |
| BLOCK-17 | 高 | 导出 | 已关闭 | ≤a33 | `test_export_filters_before_paging` | 先过滤再分页 |
| BLOCK-18 | 高 | 统计 | 已关闭 | ≤a33 | `test_dashboard_does_not_count_completed_as_auto_approved` | |
| BLOCK-19 | 高 | 费用 | 已关闭 | a32 + **a34** | `test_daily_spend_is_grouped_by_currency`、`test_a_currency_with_only_unpriced_calls_does_not_disappear` | 不跨币种求和;a34 补上**只有未配价调用的币种不再从提示里消失**(金额是 0,而那个 0 的意思是"未知") |
| BLOCK-20 | 高 | 工作台后端 | **待处理** | — | 无 | `list_products` 对全量商品逐件 `wb.collect()` 后在内存里筛选排序。**这是 `flow.py` 明写的取舍**:筛选在判定结果上做,不翻译成 SQL,换来的是筛选结果与界面显示状态永远一致。要改就得把判定逻辑翻译成 SQL,而那正是 flow.py 拒绝的那件事。属报告的**阶段 C**(500 件规模),首期量级撑得住。**a38 收掉的是 `collect()` 内部的逐行取数(另一件事),本行主题「列表页全量扫描 + 内存筛选」未变,勿据 a38 划掉** |

**小计:已关闭 16,半场 2,待处理 2。**

---

## 二、阶段 A 验收标准(报告 §8)

| # | 验收标准 | 状态 | 依据 |
|---|---|---|---|
| 1 | 所有活动任务和批次最终自动刷新到终态 | ✅ | 三档轮询钉在后端;a34 补掉重试路径那个洞 |
| 2 | 所有写请求超时均进入"结果未知"而非普通失败 | **半场** | 判定齐了,17 处展示层没落点。见 BLOCK-05 |
| 3 | 无法通过其他 SPU 素材创建图片集 | ✅ | BLOCK-01 |
| 4 | 多变体 SPU 缺图时无法批准 | ❌ | 卡在 BLOCK-02。**a43 更正:不是等业务拍板,是四件工程活没做**,见 BLOCK-02 行 |
| 5 | 属性数组和对象不会被转换为字符串 | **半场** | 数组 ✅;a43 起后端会拒绝字符串形式的列表(422)。结构化对象仍只读。见 BLOCK-11 |
| 6 | 查询失败时不会展示"无数据"或"无驳回" | ✅ | **a35 补齐**,见第三节 |
| 7 | `SUBMIT_RESULT_UNKNOWN` 可完成可审计的人工恢复 | ✅ | BLOCK-06 |
| 8 | 同一审核对象不能同时批准与退回 | ✅ | BLOCK-08 |

**进入受控真实 Provider 小流量测试的前提是八条全绿。今天卡在第 4 条(需要一次业务决策)与第 2、5 条(半场)。**

---

## 三、a35 补齐的第 6 条:查询失败不许显示成"无数据"

报告 FE-GLOBAL-03 说的是「查询失败经常被包装成空数据」。这一类的共同形状是
**空数据在界面上是一句业务结论**,而运营照着结论做的下一步,和照着"没拉到"
做的下一步是相反的。

a35 修的 11 处:

| 编号 | 失败时界面原本说的话 |
|---|---|
| FE-PROVIDER-01 | 空表 = 「一家 Provider 都没配」 |
| FE-TEMPLATE-01 | 空态 = 「还没建模板」,运营会去建一个已经存在的 |
| FE-REVIEW-QUEUE-02 | 「没有规则集,后端将使用代码默认值」—— 一句**关于后端行为的事实陈述**,而前端此时并不知道后端在用什么 |
| FE-REVIEW-QUEUE-03 | 空标签 = 「一个评分器都没有」 |
| FE-REVIEW-DETAIL-05 | 空下拉 = 「没有可用 Provider」;更要紧的是 `isProviderSelectable` 这道拦截跟着失效 —— 列表为空时它一条都拦不到,不是"拦住了" |
| FE-TASK-DETAIL-04 | 「没有评分」—— 那是个业务结论 |
| FE-TASK-CREATE-04 | 空选项,而**建任务是付费动作** |
| FE-COPY-04 | 「无证据」,会让运营改掉一条其实合规的宣称 |
| FE-STATUS-02 | 空的依赖表,而这一页存在的唯一理由就是回答"数据库通没通" |
| FE-BATCH-05 | 「还没有回执」—— 回执是"这批调了几次付费接口"的唯一对账依据 |
| FE-IMPORT-01 | 整张列名对照表连标题一起消失 |
| FE-MEDIA-02 | `?? 0` 把接口失败变成「零处不一致,可以切读路径」 |

**豁免两处,理由写在测试里**:`FlowBits`(拉不到时原样显示 code,信息量更低
但仍然正确,且格子里装不下提示)、`SpendAlertBanner`(全站横幅,给它加失败
提示等于让一个抖动的接口在顶部常驻红条;`/spend` 页自己有完整的失败分支)。

### 棘轮

`test_every_query_handle_is_checked_for_failure` —— **每一个** `useQuery`
句柄都必须在同文件里被问过 `.isError`,配一条安全网
`test_the_query_declaration_forms_stay_within_what_the_scanner_understands`
(出现扫描器不认识的声明写法时它红,而不是让主判据静默漏掉)。

这条棘轮的第一版判据是「文件里有没有 `isError`」,**被 mutation 的 `onError`
里那个 `.error` 骗过去了** —— 拆掉整个失败分支仍然全绿。经过说明写在
HANDOVER 的变异验证一节,值得下次写门禁前读一遍。

---

## 四、报告其余部分的处置

| 部分 | 处置 |
|---|---|
| §3 全站前端公共问题(FE-GLOBAL-01~10) | 01/02/03/05 已闭环;**06(URL 筛选被消费后清除)已修(A45-batch14-17)** —— 筛选改为住在 URL 里,四页搬完、`useUrlSeed` 删除,**阶段 0 的代码侧到此清空**(剩下的三项都要真 runner:Playwright 浏览器、`docker build`、连续两次全绿),自动测试见 `tests/pure/test_a45_batch14_17_url_filters.py`(12 条,13 次变异全红);07/08/09/10 见 STATUS |
| §4 逐页面(约 200 条) | 阻断级别的已收进第一节;其余按阶段 B/C/D 分布,未逐条建卡 —— **这是一次自觉的取舍**:200 张卡片没人维护,会变成一份没人读的清单 |
| §5/§6 后端(BE-GLOBAL-01~08、逐接口) | 08(时间源未完全收敛)进度记在 `core/clock.py`「收敛没有做完」一节:18 处 -> 17 处,唯一一处被点名禁止的原样写法已消除。其余见 STATUS |
| §8 阶段 B/C/D | 未开工。阶段 C 的入口是 BLOCK-20,阶段 D 的入口是「无账号体系」 |
| §9 人工测试重点 | **一条都没做过** —— 这里列的全是需要真库、真 Broker、真 Provider 的场景,当前交付环境跑不了。合并后按 §9 的五组场景各走一遍 |

---

## 五、下次复核怎么做

不要重读 2206 行报告。按顺序:

1. 跑 `run_pure_tests.py`,`test_blocking_fix_contracts.py` 与
   `test_frontend_contract.py` 是这张表大部分「已关闭」的依据
2. 只核对本表里**状态不是"已关闭"的 4 条**(BLOCK-02、05、11、20)
3. 新一轮如果动了报告涉及的模块,在对应行补一句「修复版本」

第 2 步之所以只有 4 条,是因为「已关闭」那 16 条每条都绑了一条会红的测试。
**没绑测试的那几条(BLOCK-08/09/10/11/14)在「自动测试」列写的是「无」——
它们的状态靠人读代码,下次复核要重新读。** 这一列不许填含糊的话。
