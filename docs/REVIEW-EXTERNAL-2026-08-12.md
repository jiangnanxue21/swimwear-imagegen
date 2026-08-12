# 2026-08-12 外部评审整改落档(REVIEW I / II / III)

> 评审意见一份,本批整改分两段提交:`§3.74 a49 评审整改`(12 条 + 三份从未跑过的门禁)
> 与 `§3.75 评审整改(REVIEW II.8 / III.2 / III.6 / II.1)`(四条能在离线门禁里真跑的,
> 加上两条只能记下来的)。完整交接见 `HANDOVER.md` 末节 a50/a51,验证快照见
> `STATUS.md`「2026-08-12 离线子集复验」一节。本节是评审原意见归档 —— 把"评审怎么说"
> 与"整改怎么改"分开存,前者在这一份,后者在 DECISIONS。

## 一、评审范围与评审时点的仓库状态

评审时间:2026-08-12 上午(凭据由用户在评审文档中提供)。
评审范围:仓库 `HANDOVER.md` 当时的最新交接(a48,2026-08-11),未包括 §3.74 / §3.75。

评审时点仓库未跑真库、未跑前端四条(typecheck / lint / Vitest / build)、
未跑 Playwright、未跑 Docker build。仓库根有 `.env` 与 `.secrets/.settings.key`,
`verify_delivery` 因此为 18/19。

## 二、评审给出的三处总体结论(原话压缩)

1. **试穿 + 发布未真正闭环**:发布链路 `channels/registry.py` 里**唯一的传输层是
   Simulator**,SHEIN 目录下只有字段 spec 没有发送端。运营今天能走通的"发布"
   是**导出 CSV/XLSX → 人工上传平台后台 → 回系统手工录入平台状态**。
2. **FASHN 从未连过真实端点**:计费口径(超时重发、`x-fashn-credits-used` 头、
   失败时是否带该头)、`EXTRACTOR_MODEL_SEND_IDEMPOTENCY_KEY` 是否接受
   `Idempotency-Key` —— 全部未验证。
3. **评分阈值必须重新校准**才可信:Mock 评分器按文件指纹给分,只能演练;接真实
   视觉模型后,分数分布会整体平移,A/B/C/D 阈值不动会大批误判。

## 三、评审给出的 P0(人工测试 / 上线前必须解决)

| # | 评审原文(摘) | 整改位置 |
|---|---|---|
| P0-1 | 发布链路缺真实渠道对接;重叠投递窗口今天不可达,接第一个真实 HTTP transport 的那一刻变为可达 | 留作 P0,**未在本轮落码** —— `app/channels/registry.py` 仍只有 Simulator;§3.75 D-II.1 仅记注释 |
| P0-2 | FASHN 从未用真实 Key 验证过;`EXTRACTOR_MODEL_SEND_IDEMPOTENCY_KEY` 默认关闭 | 留作 P0;`PROVIDER-FASHN.md` §8 的首验清单仍按字面执行 |
| P0-3 | 评分阈值必须重校准才可信 | 留作 P0;Mock 评分器仍是按文件指纹给分 |
| P0-4 | 主密钥轮换(2026-08-09 验收快照发现仓库树曾出现 `.env`) | **本轮把 `.env` 与 `.secrets/` 移出仓库树**,备份在 `%USERPROFILE%\swimwear-imagegen-secret-backup\`;**密钥轮换未做** —— 这是人工动作,见 §四 |

## 四、本轮真正做了的(与 §3.74 / §3.75 对应)

### 4.1 凭据移出仓库树(评审 II.5 + §3.74 一致)

仓库根的 `.env`(含 PG/Redis 真实密码)与 `.secrets/.settings.key` 移到仓库外
`$env:USERPROFILE\swimwear-imagegen-secret-backup\`,**文件内容不动**。

- 这两个文件**未被 Git 跟踪**(`git ls-files | grep` 无结果),所以只是工作树临时文件,移除不影响历史
- `.gitignore` 已含这两条,所以重新创建不会被跟踪
- **`verify_delivery` 从 18/19 → 19/19**;`test_environment.py::test_the_default_mock_deployment_reports_every_facet_as_simulated` 从红转绿(原因:移走 .env 后 `EVALUATOR_BACKEND` 走模型默认 `mock`,默认部署回到全 SIMULATED)
- **未做**:主密钥轮换。`SETTINGS_SECRET_KEY` 与 `.secrets/.settings.key` 一致这件事,
  评审已经警告过密钥泄露应轮换;**这一步必须人工执行**,本轮只在交付里如实标记

### 4.2 上传闸 20MB 与 FASHN 内联上限 10MB 的落差(§3.75 二、II.8)

详见 `DECISIONS.md` §3.75。`provider_inline_size_message()` 在 FASHN 四处
oversize 现场统一收敛到同一条文案,`tests/pure/test_provider_inline_size_message.py`
守"不许再出现简略文案"。

### 4.3 `<ErrorNotice>` 棘轮(§3.75 三、III.2)

棘轮**已上**,17 处迁移**未做**(§3.75 决策原话:不盲改这 17 处)。宽口径 24 / 窄口径 17
`backend/tests/pure/test_error_notice_ratchet.py` 六条全绿。

### 4.4 `.claude/settings.local.json` 进 pack 黑名单(§3.75 一、III.6)

- `.gitignore` 已含 `.claude/settings.local.json`
- `tools/pack.sh` 的 `FORBIDDEN_FILES` 与 `tools/pack.ps1` 的 `$ForbiddenFiles`
  都加 `'settings.local.json'`
- `verify_delivery` 的 `paired_arrays` 表钉两套入口数组同形

### 4.5 离线子集复验(2026-08-12,本机 python3,无 node_modules / 无真库)

```
纯逻辑         2853/2853      0 失败
lint_offline   0 错           445 文件
verify_delivery 19/19
verify_imports  497 文件
audit_anchors   565/565
audit_source_guards 664
audit_doc_refs  全绿
audit_column_writers 553 列
verify_sample_data 10/10
前端 syntax-check 未在本轮跑 —— 与既有 D 类缺口同源,详见 STATUS.md「仍未执行」
```

## 五、评审给的其它 P1 / P2(本轮未消除)

| # | 评审原文(摘) | 当前状态 |
|---|---|---|
| P1-1 | a50/a51 的完整交接与回归记录补齐 | 本轮补:HANDOVER.md 末节 + STATUS.md 末节 |
| P1-2 | FASHN 轮询阻塞 worker | 留作 P1,文档自陈"上量后需改 webhook 驱动" |
| P1-3 | 幂等保护缺口(老建档路径) | 已由 §3.65 关闭(CSV 导入只接受已存在 SPU);`create_product` 也已要求解析 `spu_id`,见 DECISIONS §3.41 D4 |
| P1-4 | 存储与上传是开发形态 | 留作 P1;`STORAGE_BACKEND=local` 与 `STORAGE_BACKEND=s3` 是同一接口 |
| P1-5 | 无 CSRF Token 体系 | 留作 P1;`SameSite=Lax` 是当前阶段的最低防线 |
| P1-6 | 登录限流是进程内表,`--workers N` 会让阈值实际放大 N 倍 | a50/a51 提交里新增 `login_throttle.py`,但**仍是进程内** —— 待评估 |
| P2-1 | 计费口径"宁可多记"(`units_source=inferred`) | 已由 §3.74 D-1 收掉一半:`is_simulator` 由实现类声明,不是名单 |
| P2-2 | 溯源冲突只打 `logger.warning` 不落审计 | 留作 P2 |
| P2-3 | 外部来源的 AI 图仍能成为商品证据 | 留作 P2;图像侧判别不在本期 |

## 六、评审对前端的点名(本轮处理)

| # | 评审原文(摘) | 当前状态 |
|---|---|---|
| F-1 | 发布页没有任何一条浏览器用例点开过 | 留作 P1;Playwright 任务 24 |
| F-2 | 17 处 `<Alert+readError>` 未迁移到 `<ErrorNotice>` | 棘轮已上,迁移未做(本机跑不了 Vitest);见 §3.75 三 |
| F-3 | `ProductListPage` / `ReviewQueuePage` 筛选活不过刷新 | **本轮修**:迁到 `useUrlFilters`,与硬规则第 4 条对齐 |
| F-4 | 审核中心的"0 vs 没查到" | 留作 P1;§3.75 评估仍欠 |
| F-5 | 近 500 处 inline style 未收敛到间距令牌 | 留作 P2 |
| F-6 | 暗色模式没人眼过过 | 留作 P2 |
| F-7 | `color-mix()` 要求 Chrome 111+ / Safari 16.2+ | 留作 P2 |
| F-8 | 平板适配待业务决策 | 留作 P2 |

## 七、评审对运营的提示(已合并到 OPS-REVIEW)

- 试穿必须先登记模特模板 —— 自由上传的模特图在 §3.65 之后被以拒绝方式关掉
- 发布在第一个真实渠道接入前是导出 + 人工上传,平台状态与驳回人工录入
- 具名审计仍欠(只有 admin / operator 两个共享账号),文档建议方向是 users 表,
  不是回头配具名口令
- 吞吐摩擦点:批量批准、批量重算尺寸,**没有批量入口是刻意的**,量大时是瓶颈

## 八、本评审与仓库自陈的一致性核对

| 评审说 | 仓库自陈 | 一致 |
|---|---|---|
| 上传 ✅ 可用 | §3.74 a49 / OPS-REVIEW §1-§2 | ✅ |
| 试穿 ⚠️ 有条件 | §3.65(2026-08-11) + §3.74 D-1 | ✅ |
| 发布 �� 自动闭环未通 | `app/channels/registry.py` 仍是 Simulator | ✅ |
| FASHN 从未连过真端点 | `docs/PROVIDER-FASHN.md` §八首验清单 | ✅ |
| 仓库根曾出现 `.env` | HANDOVER.md §三(评审整改批) + STATUS.md §七 | ✅ |
| 17 处 Alert+readError 未迁 | `tests/pure/test_error_notice_ratchet.py` 宽口径 24 | ✅ |
| 两页筛选活不过刷新 | `tests/pure/test_a45_batch14_17_url_filters.py` 未覆盖这两页 | ✅ |
| `.claude/settings.local.json` 被打进包 | §3.75 一、III.6 留了 evidence | ✅ |
| 20MB vs 10MB 落差 | §3.75 二、II.8 决策 | ✅ |

**评审意见与仓库自陈完全对齐。** 评审没指出仓库自己不知道的事,
仓库也没"隐瞒"评审点过的事 —— 这一点决定了本轮的整改范围:
**P0 那一族(发布真渠道、FASHN 真端点、评分重校准、密钥轮换)不是这一次
能消除的,只能如实记下来给下一轮接手的人。**

## 九、下一步建议

按本仓 §3.74 一的教训排序:

1. **先在浏览器里走一遍**(任务 24 / Playwright),任何一行 P1 都靠这一跑钉死
2. **轮换主密钥**(评审 P0-4) —— 在冻结交付前必须做,这是人工动作
3. **接第一个真实 HTTP 渠道 transport** —— 业务先定平台,才能写 transport;接入前
   把客户端超时钳在 `LEASE_SECONDS` 以下、确认平台幂等
4. **FASHN 真端点首验** —— `PROVIDER-FASHN.md` §八清单逐条跑
5. **批量入口 / 平板适配 / 暗色模式过一遍** —— 都是上线前最后一次看的窗口