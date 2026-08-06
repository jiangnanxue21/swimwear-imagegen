# A44 第四批修复说明（十条 P1/P2/P3 毛边）

> 依据：`REVIEW-A44-FINAL-verified-r2-1.md`（修订二）第三、六、八节。
> 环境同前：**无网络，`pydantic / sqlalchemy / fastapi / alembic / pytest` 均未安装。**
> 纯逻辑回归 **1595 → 1609，失败数恒为 2**（那 2 条是缺 `sqlalchemy` / `pydantic` 的 import 失败）。

## 一、这一批修了什么

| 编号 | 级 | 位置 | 做法 |
|---|:--:|---|---|
| **A-14** | P1 | `services/storage.py` | `S3ObjectStorage.delete` 只把 `404/NoSuchKey/NotFound` 判成 False，其余 `raise` |
| **A-24** | P2 | `batch_service.batch_file` | 导出闸失败**攒齐再报**，409 里点名 spu/sku 与各自原因，`detail.blocked` 给全量名单 |
| **A-27** | P2 | `listings/variant_key.py` | 新增 `key_matches_color()`，用铸造函数把 key 重算一遍再比 |
| **A-28** | P3 | 同上 | `drift()` 新增 `key_label_conflicts`，`refs_of()` 的承诺变成真的 |
| **A-29** | P2 | `ImageSetTab.tsx` | 版本变化不再静默清空草稿；改走 `applyEdit()` 记录 base 版本 + 顶部横幅 |
| **A-33** | P2 | `api/client.ts` · `api/batch.ts` | 默认超时 30s → 60s；生成/重生成/下载三条长动作放到 300s |
| **A-38** | P2 | `image_set_service.reject` | 退回时清 `approved_by/approved_at`，被清掉的值进审计 |
| **A-39** | P2 | `image_set_service.downgrade_sets_using` | 补 `audit.record`，并从隔离那里把 `actor` 传下来 |
| **A-40** | P3 | `batch_service` 两处重置 | 补 `row.target_step = None` |
| **A-41** | P3 | `workbench/batch.py` | `should_renew_lease` 默认值改引用 `ITEM_LEASE_SECONDS` |

## 二、几个需要说明的判断

### A-27 不自己写"允许前缀相等"

原判据是 `normalize(color) != normalize(key)`，两类**没改过名**的变体被恒判 renamed：
带消歧后缀的（`Red~2` 配色 `Red`）和被列宽截断的长颜色名。

修法不是在判定里加两条豁免，而是**用同一组铸造函数把 key 重算一遍**
（`split_disambiguator` → `seed_for` → `_fit`）再比。铸造与判定共用一条路径，
以后改铸造规则时判定自动跟上；各写一份的下场见 §3.4。

### A-24 闸门一个字没松

仍然是任何一件不合格就整批拒绝（§4.5.1）。变的只是报错里说清楚是哪几件。
最多点名 5 件，其余给个"另有 N 件" —— 再多的话错误条会长到没人读。

顺带把 `NotFoundError`（还没生成草稿）也并进名单：原来那一支是 404，
和过期草稿的 409 分头出去，运营看到两种毫不相干的报错。

### A-33 默认值只抬到 60s，不抬到 320s

直接对齐网关会让一次真正的网络黑洞转五分钟，运营连"是不是我网断了"都判断不了。
所以默认 60s 覆盖绝大多数写，已知长动作单独挂 `LONG_TIMEOUT_MS = 300s` ——
**略短于** nginx 的 `proxy_read_timeout 320s`，让超时由浏览器先报：
网关先断的话前端拿到的是一张 504 HTML，`describeError` 认不出来。

### A-38 清列不等于忘掉

`reject()` 现在清 `approved_by/approved_at`，但被清掉的值写进了这次退回的审计
payload（`cleared_approved_by` / `cleared_approved_at`）。清列是为了让"当前结论"
只有一个，不是为了把历史丢掉。

### A-39 记成 system 也好过不记

`downgrade_sets_using` 新增 `actor: str = "system"`，隔离路径把真实操作者传下来。
默认值保留是因为将来可能有真正无人值守的调用方 —— 那时记成 system 仍然远好过
一条审计都没有。

## 三、新增门禁：`tests/pure/test_a44_batch4_fixes.py`（14 条）

能跑真逻辑的跑真逻辑：A-27 / A-28 / A-41 直接调纯函数，断言的是行为不是形状。
其中 A-27 有一条**反向断言**（真改名仍要报出来）—— 少了它，"把诊断关掉"
也能让这条修复看起来通过。

跑不了的钉 AST：A-40 断言的是**成组关系**（凡是把那三个字段之一置 None 的
语句块，同块必须清 `target_step`），所以将来新增第三处重置路径也会被钉住。
A-14 断言 `delete` 与 `exists` 的"不存在"码集**相等**，把两处钉在一起。

**十条全部拿改动前的代码验过会红。** A-27 / A-28 是行为红
（原实现返回 `renamed=['Red~2']`、返回值里没有 `key_label_conflicts`），
其余八条是断言红。

## 四、这一批踩的坑

1. **`_fn()` 找错了类**：A-14 那条第一版用全模块搜 `delete`，命中的是
   `LocalObjectStorage.delete` —— 它本来就没有 `ClientError`，于是报
   "delete 里没有异常处理了"。假红，而且指向一个没问题的函数。已改成按类找。

2. **失败信息又不可读**：A-33 那条第一版直接 `re.search(...).group(1)`，
   在改动前的代码上抛 `AttributeError: 'NoneType' has no attribute 'group'`。
   第三批刚栽过一次，这里是第二次 —— 已改成先断常量存在。

3. **JSX 里写了 Markdown**：A-29 的横幅里写了 `**没有丢**`，那会原样渲染成
   星号。TS 语法解析发现不了这种错（它是合法的 JSX 文本），靠人眼看出来的。

## 五、验证缺口

| 归属 | 待验项 |
|---|---|
| **D-01** | A-24 的聚合 409：真库上造两件过期草稿 + 一件缺草稿，断言一次报三件且带 spu/sku。 |
| **D-01** | A-38 / A-39 都改了写路径，**一条真事务都没跑过**。A-39 的 `audit.record` 在降级循环里逐行调用，条目多时的开销没量过。 |
| **D-01** | A-14 改成 `raise` 之后，**上游调用方是否接得住**没有验证 —— 下架清理原来靠 `False` 继续往下走，现在会抛。这是本批**风险最高**的一条，接真库后应当先跑一次清理任务。 |
| **D-05 ～ D-08** | 前端三处改动（`client.ts` / `batch.ts` / `ImageSetTab.tsx`）只做了**语法**解析，`tsc` 的类型检查仍然没跑。`key_label_conflicts` 是新加的联合成员，类型错了只有 `tsc` 能发现。 |
| **D-12** | A-29 的横幅没有 React 行为测试：后台 refetch → 草稿还在 → 点"放弃"才载入新版，三步都只有静态断言。 |

## 六、还剩什么

A 类未修的已降到毛边区：A-09/A-10（health query 不失效）· A-11/A-12（有意取舍）·
A-15（时间戳契约）· A-22/A-23（计费与领取语义，均有设计张力，建议连同 C-01/C-02 一起做）·
A-30/A-31/A-32/A-34/A-35/A-36/A-37/A-42/A-43。

C 类里 **C-13（0027 downgrade 绕开长度前缀，P0）** 是剩余风险最高的一条，
建议作为下一批的第一项 —— 它和 B-07 是同一份数据上的两个方向。
