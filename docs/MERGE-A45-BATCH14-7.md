# MERGE:两份 patch → batch14-6

合入的是:

    fix-env-dependent-budget-test.patch          3 文件 / 6 hunk
    fix-ruff-import-contract-and-pillow.patch    34 文件 / 46 hunk

合入到的是 **a45-batch14-6 交付树**。

> **一句话结论:两份 patch 加起来 52 个 hunk,真正落地的只有 6 个 ——
> 其余 45 个在 batch14-6 里已经是那个样子了,1 个被主动放弃。
> 落地的 6 个里最要紧的一个不是 ruff 体例:`grading-stays-pure` 那条
> 依赖方向契约**此前是破的**,离线可达性检查证实了破口链条,合入后三条契约全通。
> 纯逻辑 2059/2059(与合入前逐条相同)、交付 13/13、导入 356、样例 5/5、
> 锚点 87/87。`ruff` 与 `lint-imports` 本身仍然没有在这台机器上跑过。**

---

## 一、真正落地的 6 个 hunk

| 文件 | 改了什么 | 属于哪份 patch |
|---|---|---|
| `app/evaluators/vision_schema.py` | `VISION_SYSTEM_PROMPT` / `_MEN` 两个常量的真身搬到这一层;`prompt_key_for` 函数体里那句反向 import 删掉 | ruff patch |
| `app/services/prompt_rules.py` | 改成从 `evaluators.vision_schema` 原样再导出 | ruff patch |
| `app/evaluators/ranking.py` | `getdata()` → `tobytes()`,两处 | ruff patch |
| `tests/pure/_helpers.py` | 新增 `temporary_config` | budget patch |
| `tools/run_pure_tests.py` | 自检加第二条:`tests/pure` 下不许裸写 `os.environ[...] = ...`,白名单 `_ENV_WRITERS = {"_helpers.py"}` | budget patch |

### 1.1 契约破口是真的,而且此前一直是破的

patch 的说明称 `prompt_key_for` 函数体里那句反向 import 是 `grading-stays-pure`
唯一的破口。这条**本轮验证过**:按 `.importlinter` 的三条 forbidden 契约做了一次
离线可达性检查(与 grimp 一样把函数体内的 import 也算进依赖图),合入前:

    BROKEN  grading-stays-pure
              app.evaluators.decision -> ... -> app.services.prompt_rules
              app.evaluators.rules    -> ... -> app.services.prompt_rules
              app.evaluators.scoring  -> ... -> app.services.prompt_rules

合入后三条全通。链条是 `scoring/rules/decision → vision_schema → services.prompt_rules`
——**没有任何一行代码写着一条明显反向的 import**,它绕了一层,而绕一层正是
`.importlinter` 那段注释里点名的形状。

反向的新边(`services.prompt_rules → evaluators.vision_schema`)不违反任何一条契约:
三条契约的 `source_modules` 里没有 `app.services`。也不成环:`evaluators/__init__.py`
只有文档字符串,`services/__init__.py` 是空的,`vision_schema` 顶层只 import
`core.enums` 与 `evaluators.base`。`prompt_service` 那一侧的再导出原样保留,
`tests/test_prompts.py` 的 `from app.services.prompt_rules import VISION_SYSTEM_PROMPT`
不用动。

**这一条把 `REVIEW-A45-BATCH14-6.md` §六第 4 项的"契约还没验过"关掉了一半**:
契约的**内容**验过了(离线等价实现),`lint-imports` 这个**命令**仍然没跑过。
两者的区别在下一台装得上 import-linter 的机器上才消得掉。

### 1.2 Pillow 的弃用不是预判,这台机器上就在报

本机 **Pillow 12.1.1**,调 `getdata()` 当场:

    DeprecationWarning: Image.Image.getdata is deprecated and will be
    removed in Pillow 14 (2027-10-15). Use get_flattened_data instead.

等价性实测过 —— 两处调用点的真实入参形状(`convert("L").resize(...)`)下
`list(getdata()) == list(tobytes())` 均为 True。`tests/pure/test_candidate_ranking.py`
的 18 条在改后全绿。patch 不用 `get_flattened_data` 的理由也核过:
`pyproject.toml` 写的是 `pillow>=11.0`,而那个替代品 12 才有。

### 1.3 新那条 os.environ 门禁,现存代码零违规

扫过 `tests/pure/` 全目录:唯一的写入点在 `_helpers.py`(`temporary_secret_key` /
`temporary_config` 两个封装自己),已在白名单里。`test_a45_batch12_5_fixes.py`
里那句 `os.environ["VISION_MODEL_TIMEOUT_SECONDS"]` 在**文档字符串**里 ——
自检走 AST 不走正则,不会命中,这正是那段注释写明的理由。

## 二、45 个 hunk 已经在树里了

ruff patch 的 34 个文件里,31 个的 hunk 被 `patch -N` 判为
"Reversed (or previously applied)" 或上下文已变。**逐个核过落点**,结论一致:
batch14-6 已经修过同一处,只是写法不同。举三个:

| 位置 | patch 的写法 | batch14-6 的写法 |
|---|---|---|
| `test_a44_batch5_fixes.py` E741 | `ln` | `line`,并折成三行 |
| `test_a44_batch7_fixes.py` E741 | `ln` | `line` |
| `generation_tasks.py` E501 | 抽 `step_label` 变量 | f-string 折成两行 |

`test_a44_batch3_fixes.py` 的 `can_approve_block` 那一处,树里的代码与 patch
**逐字相同**。

复核了 patch 针对的每一类规则,全仓 `app` + `tests`(除 per-file-ignore 的
`test_product_import.py`):

    B905  zip 没写 strict=     0
    E741  l / I / O 作名字      0
    UP037 引号包住的类型注解    0
    B023  闭包捕获循环变量      batch_service.py 两个回调的默认参数绑定已在
    E501  超过 100 列           4(见第四节)

## 三、放弃的 1 个 hunk

**budget patch 对 `test_the_budget_tracks_configuration_instead_of_freezing_at_import`
的改写,不合入。**

那条用例在 batch14-5 已经修过一次,修法不同而且更直接:它 monkeypatch
`_config._override`,推的是取值链的**第一层**——设置页写进数据库的覆盖值,
也就是"改完就生效"那句话真正走的那条路。第一层与 pydantic 装没装无关,
所以它本来就不会分裂成两种结论。

patch 的版本推的是第 2/3 层(环境变量 + 重建 `settings` 单例)。两种都对,
但换上去会把 batch14-5 那段记录事故经过的文档字符串一起换掉,而被测的东西
反而离生产路径远了一层。

**`temporary_config` 本身照收**:新那条门禁的报错信息写着"改配置请用
`_helpers.temporary_config`",那个函数必须存在,这句话才不是空指令。
它现在没有调用方,是留给下一条要改配置的用例的。

## 四、剩下的 4 条 E501

两份 patch 都没有覆盖,本轮也没有动 —— 合并就是合并,顺手改别的会让
"这个包等于 batch14-6 + 两份 patch"这句话不再成立。

    app/workbench/batch_service.py:182            104 列   嵌套调用的 f-string
    app/workflows/dispatch_policy.py:185          108 列   常量行尾挂 type: ignore
    app/workflows/dispatch_policy.py:188          108 列   同上
    tests/pure/test_a45_batch14_4_fixes.py:335    111 列   正则字面量

后两类折行要小心:`# type: ignore[arg-type]` 挪位置会失效,正则字面量折行会改变
匹配内容。

## 五、合入后跑过的

| 门禁 | 结果 |
|---|---|
| 纯逻辑 `run_pure_tests.py` | **2059/2059**,7 条跳过(本机缺 pydantic / sqlalchemy)—— 与合入前逐条相同 |
| 依赖方向契约(离线等价实现) | **3/3**,合入前 1 条 BROKEN |
| verify-delivery | 13/13 |
| verify-imports | 356 个文件 |
| verify-sample-data | 5/5 |
| audit-anchors | 87/87(4 份变异脚本) |
| ruff 目标规则的手工复核 | B905 / E741 / UP037 全 0,E501 剩 4 |

**仍未执行**(与 batch14-6 相同,合并没有改变任何一条):`ruff` 本体、
`lint-imports` 本体、真库 pytest、Alembic、前端四件套、Docker build、
Redis 相关的 P0-6。本机没有装 ruff / import-linter,且无外网。
