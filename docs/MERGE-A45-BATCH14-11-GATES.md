# A45-batch14-11:第一台装得齐后端依赖的机器,把欠的门禁跑了

> **一句话结论:此前四轮(14-7~14-10)"纯逻辑 N/N 全绿"里一直藏着 4 条
> 从未跑过、装齐依赖即红的用例 —— §5.1 白名单接线后夹具没跟上,而那份夹具
> 恰好整文件躺在"跳过 7"里。已修并加守卫。Ruff 与 lint-imports 首次真正执行
> (6 条清零、契约 3/3)。真库 207 条、前端四条、Docker 照旧没验,P0 没关,
> **本轮不宣布"没有 Bug"。**

配套交付:`a45-batch14-11.patch`(基于 batch14-10 交付包原样解压后的树,
`git apply` 已在干净基线验证无冲突)。

---

## 1. 本轮修改目标

进入本地人工测试前,把这台机器**能跑的门禁全部真的跑一遍**(此前四批的
交付机器缺 pydantic/sqlalchemy/ruff/lint-imports),修掉由此暴露的、会让
CI 与门禁变红的问题;顺带把落后四个批次的状态文档补齐。不做新功能,
不做迁移,不碰与人工测试无关的重构。

## 2. 修改文件列表(8 个)

| 文件 | 改动 |
|---|---|
| `backend/tests/pure/test_a45_batch14_stage3_extractor.py` | 夹具 `_FakeAsset` 补 `status="READY"`(+why 注释);**新增守卫 1 条** |
| `backend/app/attributes/scope_fingerprint.py` | UP035(`Iterable` 改从 `collections.abc` 导入)+ E501 折行,行为不变 |
| `backend/tests/pure/test_a45_batch14_7_evidence_class.py` | I001 导入重排(ruff --fix)+ 订正随重排失效的旧注释 |
| `backend/tests/pure/test_a45_batch14_9_scope_fingerprint.py` | I001 重排 + E501 断言消息折行 |
| `backend/tests/pure/test_a45_batch14_10_sample_completeness.py` | I001 重排(ruff --fix) |
| `backend/tests/test_a45_batch14_extraction_db.py` | 文档字符串断链修正(`docs/HANDOVER.md` → 根目录 `HANDOVER.md`) |
| `docs/STATUS.md` | 顶部新增 batch14-11 版本块;文档地图 12→13(补 PRD 行)+ 批次留档收编规矩一段 |
| `HANDOVER.md` | 按"最近交接"惯例整体重写为本轮交接(1210 行 → 52 行;上一份的结论在 STATUS batch14 块与各 MERGE 文档,原件在上一交付包内可查) |

## 3. 问题表(第三步产物,含已修与未修)

| ID | 严重度 | 位置 | 触发条件 | 实际结果 | 预期结果 | 根因 | 证据 | 阻塞人工测试? | 最小修复 | 状态 |
|---|---|---|---|---|---|---|---|---|---|---|
| G-01 | **High** | `tests/pure/test_a45_batch14_stage3_extractor.py` 4 条用例(台账×2、上限×2);夹具在原 87-92 行 | 在装齐 pydantic/sqlalchemy 的机器上跑纯测试 | 4 条 FAILED:`ValidationError: 该商品的 N 条可用素材全部不能作为识别证据(§5.1)`;两种运行器一致 | 4 条通过(它们守的是花费台账与付费上限,不是白名单) | batch14-7 给 `run_extraction` 接 §5.1 白名单后判定**复查** `status=READY`(`is_extraction_input` 第一条件),而 `_FakeAsset` 无 `status`;该文件在缺依赖机器上整体 skip,接线后无任何东西变红,四轮"全绿"均带病 | 本机 `run_pure_tests.py` 与 `pytest tests/pure` 双双复现;修后双绿 | **是**(CI backend job 会红;且这 4 条守着"Mock 不进台账/付费上限先于花钱"两条真金规则) | 夹具补 `status="READY"`——还原 `usable_assets()` 只返回 READY 行的生产前提,非扩 mock;另加守卫 G-01b | ✅ 已修 |
| G-02 | Medium | `app/attributes/scope_fingerprint.py:59,121` + 三份 batch14-7/9/10 守卫文件 | `ruff check app tests`(历史上从未执行) | 6 条违规(UP035×1、E501×2、I001×3),`make lint` / CI 红 | 0 违规 | 交付机器装不上 ruff,四批只跑了阉割版 `lint_offline`(仅 F401/UP017) | 本机 ruff 0.16.1 输出;修后 `All checks passed!` | 是(CI 门禁) | UP035/I001 用 `--fix`;两处 E501 手工折行;I001 重排前已核实两个运行器各自保证 `sys.path`(`run_pure_tests.main()` 首句 insert、pytest 走 `pythonpath=["."]`),并把 14-7 文件里随重排失效的"保证 app 在 sys.path 上"旧注释订正 | ✅ 已修 |
| G-03 | Low | `backend/tests/test_a45_batch14_extraction_db.py:23` | 读该真库用例文档字符串按图索骥 | 指向不存在的 `docs/HANDOVER.md` | 指向仓库根 `HANDOVER.md` | 写文档时记错路径 | 全仓引用扫描,唯一断链(另一处 `REVIEW-A44-BATCH8.md` 是**刻意不留**的文件,见 `MERGE-A44-BATCH8-PATCH.md`,不是断链) | 否 | 改一行 | ✅ 已修 |
| D-01 | Medium(文档) | `docs/STATUS.md` / `HANDOVER.md` | 想知道当前状态、按 STATUS 找 14-7~14-10 的记录 | STATUS 版本块停在 batch14-2,四批只有 MERGE 文档;HANDOVER 停在 batch14 | STATUS/HANDOVER 与实际批次同步 | 批次连发四轮只写了 MERGE,没回写 STATUS/HANDOVER | `grep batch14-10 docs/STATUS.md` 为空 | 否,但会误导下一轮接手人 | 补 14-11 块并在块内注明"查旧批次去 MERGE"(不代写四个历史块的二手转述);重写 HANDOVER;地图补 PRD 行 | ✅ 已修 |
| E-01 | High(环境) | 前端四条门禁 | 本机无网络、无 npm 缓存 | `npm ci --offline` → ENOTCACHED,tsc/ESLint/Vitest/build 全部**无法执行** | — | 环境限制 | 实测报错留存 | **是**(14-7 起前端有改动,只过了 syntax-check 84/84) | 无法在本机修;下一台有 node_modules 的机器补跑 | ⛔ 未验证,如实上报 |
| E-02 | High(环境) | 真库 207 条 pytest、Alembic 真实升降级、Redis/P0-6、Docker build、变异脚本全量重跑 | 本机无 PostgreSQL/Redis/docker | 全部 skip / 无法执行;Alembic 离线 `--sql` 仅验到 0001→0032(1257 行 SQL),0033 起为数据迁移按设计不支持离线 | — | 环境限制 | pytest `207 skipped`;`alembic upgrade head --sql` 报错定位 0033 `fetchall` | **是**(池子里 60+ 条演练在内) | 无法在本机修;锚点已做静态审计 150/150(不替代变异重跑) | ⛔ 未验证,如实上报 |
| B-01 | Backlog | `app/attributes/service.py`(§5.1 取数)、`workbench/service.py`(`has_primary_image` 无读取点)等 | — | STATUS「已知限制」已逐条留档的欠账(识别输入白名单 SQL 版、识别付费与事务同生死、17 处 `Alert+readError` 等) | — | 各批已声明的分期边界 | STATUS 已知限制表 | 部分是,已在表内标注 | 均需真库/前端环境,超出"最小范围"约束 | 不动,维持台账 |

## 4. 暂未修复的问题及原因

上表 E-01 / E-02(环境硬缺口,伪造通过违反本任务红线)与 B-01(既有分期
台账,动它们不满足"最小范围、与人工测试直接相关"两条)。**没有发现新的
Critical。**另记一条环境观察:wheelhouse 里 starlette 1.3.1 会在 pytest 输出
`httpx testclient deprecated` 警告 —— 仅警告,2190 条全过,不改代码。

## 5. 新增或修改的测试

- 新增 `test_the_fake_asset_still_passes_the_evidence_whitelist`(G-01 的回归
  守卫):白名单下次再收紧条件(如 `evidence_class` 存储列)时,**先红的是
  这一条并把话说全**,而不是四条业务断言一起红成"素材全不合格"让人误判
  白名单误伤生产。纯断言、无 pytest 依赖,双运行器均通过。
- 修改:`_FakeAsset` 补 `status`(见 G-01,含 why 注释);两处 E501 折行只动
  排版;三处 I001 只动导入顺序。**没有删除测试、降低断言、扩大 mock 或跳过
  异常。**变异脚本未全量重跑(见 E-02),但 `audit_anchors` 150/150 证明
  本轮排版改动没有让任何变异锚点失效。

## 6 & 7. 实际执行的命令与逐条结果

| 命令 | 修前 | 修后 |
|---|---|---|
| 离线装依赖(58 whl,`pip --no-index --find-links`) | ✅ | — |
| `python3 tools/run_pure_tests.py`(venv 全依赖) | 🔴 2151/2155,4 failed | ✅ **2156/2156** |
| 同上(裸 python3,复刻交付机路径) | 🔴 同 4 条 | ✅ 2149/2149 + 7 跳过 |
| `pytest`(全量) | 🔴 4 failed / 2185 passed / 207 skipped | ✅ **2190 passed / 207 skipped** |
| `ruff check app tests`(首跑) | 🔴 6 errors | ✅ All checks passed |
| `lint-imports`(首跑) | ✅ 3 kept / 0 broken | ✅ 同 |
| `tools/verify_delivery.py` | ✅ 13/13 | ✅ 13/13 |
| `tools/verify_sample_data.py` / `verify_imports.py` / `audit_anchors.py` | ✅ 5/5 · 365 · 150/150 | ✅ 同 |
| `node tools/syntax-check.mjs` | ✅ 84/84 | ✅ 84/84 |
| `alembic history` / `heads` | ✅ 链 0001→0036 单 head | — |
| `alembic upgrade head --sql`(离线) | ⚠️ 0032 前可生成,0033 数据迁移不支持离线 | —(如实记录,不算通过) |
| 后端应用导入 + `GET /api/health` | ✅ 200 `{"status":"ok"}` | — |
| `npm ci --offline` | ⛔ ENOTCACHED | —(无法执行) |
| `git apply --check` patch(于干净基线) | — | ✅ 无冲突 |

## 8. 最担心出现回归的位置

1. **三份守卫文件的导入重排。**安全前提是"两个运行器各自保证 sys.path";
   总纲硬规则 1 已写"新增第三个运行器时同样的三行必须跟着走"——真加第三个
   运行器又漏了这一步,这三份文件会是第一批炸的。14-7 文件里我留了指路注释。
2. **G-01 的修法方向。**若将来有人把 §5.1 的 status 复查从
   `is_extraction_input` 里摘掉("usable_assets 已经筛过了"),新守卫不会拦
   ——它只保证夹具跟得上白名单,不保证白名单不被放宽;放宽由
   `test_a45_batch14_7_evidence_class.py` 的穷举守着,两边别一起动。
3. **HANDOVER 整体重写。**旧 1210 行的结论我核对过都在 STATUS batch14 块与
   MERGE 文档里,但"都"字没有机器验证;原件在上一交付包内可随时回查。

## 9. 建议 Codex 独立重点审核的位置

1. 复活的那 4 条用例的**断言语义**:它们首次在接了 §5.1 白名单的代码上运行
   ——付费上限按"过滤后张数"计、Mock 零台账行,这两条期望是否仍是产品想要的
   (我读代码认为是,值得第二双眼睛)。
2. `evidence_rules.asset_is_extraction_input` 对 `status` 的复查与
   `usable_assets()` 的 SQL 过滤是否会在"状态放宽"那天分叉(服务层注释声称
   刻意双保险,Codex 可判这个设计决定本身)。
3. patch 里 STATUS/HANDOVER 的措辞是否有替既往批次"报喜"的地方——我刻意
   逐条标了"未验证",请按同一标准挑刺。
4. 三处 I001 重排逐字 diff(机器改的,人再看一遍)。

## 10. 当前是否已经适合进入 Codex 审核

**代码与本机可验门禁:适合**——全部命令绿,patch 自洽。
**作为人工测试准入:不适合**,与前四批同一原因且一条没少:P0 未关
(真库 207 条、Alembic 真实升降级、前端 tsc/ESLint/Vitest/build、Docker、
Redis/P0-6 全部未验证)。建议 Codex 审 patch 的同时,把下一台真库机器的
开工清单按 HANDOVER 第三节排期。

---

## 附:文档要不要整理——评估与已做/建议

**已做(随 patch)**:STATUS 补 14-11 块并注明 14-7~14-10 的查询入口;
HANDOVER 重写归位"最近交接";地图 12→13 补 PRD;修一处断链;地图开头补
一段批次留档的收编规矩(结论沉 `DECISIONS.md` 再删原件,别只删不沉)。

**建议但未动(需要一次专门决定,不适合夹带)**:
- `docs/` 现有 40 份 md,地图外 27 份 MERGE/REVIEW 批次留档。总纲写"过程
  文档不留档",实际惯例已演化为"批次留档 + STATUS 链接索引"。二选一:要么
  把总纲那句改成如实描述现状(我倾向这个——A44/A45 的多轮互审记录有真实
  复查价值),要么做一次批量收编。**别维持"规矩说一套、目录长一套"。**
- A44 批次的 7 份 REVIEW(BATCH3~7、RESPONSE、FINAL-r3)距今最远、被引用
  最少,若要收编从它们开始,且先跑一遍全仓引用扫描(本轮用的那条 grep 可
  直接复用)。
- STATUS 正文第三节"已知限制"已 30+ 行,可考虑把"~~已修~~"划线项移入各自
  批次块,只留现行限制——纯编辑工作,收益是新人首读时间,风险是历史对照
  变两跳,留给你拍板。
