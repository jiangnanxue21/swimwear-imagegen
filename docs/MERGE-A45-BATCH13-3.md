# MERGE:A45-batch13-3 → batch14

**合入的是 `a45-batch13-3.patch`**(batch13-2 走读修复,六条 / 三条 P1),
**合入到的是 batch14 交付树**(阶段 3 第一批:真实多模态抽取器)。

两者是**同一基线的并行分支** —— batch14 的 STATUS 自报「基线 batch13-2」,
batch13-3 的 STATUS 自报「基线 batch13-2」。所以这不是补丁的重放,是一次真合并。

> **一句话结论:六条修复全部合入,代码 hunk 无冲突;文档层两处冲突已解,
> 合入方另修两条(ruff I001、真库池计数)。合入后离线门禁与合入前完全一致:
> 纯逻辑 2010/2010、交付 13/13、导入 349、样例 5/5、p0-gate 仍为 1 通过 /
> 5 未验证。R1 那条「删颜色被 RESTRICT 拦住」的验收在真库上跑过之前,
> 仍然是没被验证的 —— 合并没有改变这一点。**

---

## 一、审阅结论:六条逐条核过

| 项 | 病灶是否成立 | 修法是否对 | 合入后 |
|---|---|---|---|
| R1 `passive_deletes` | **成立**。`ColorVariant.skus` 是全树唯一一条「跨 RESTRICT 外键的 ORM 反向集合」,SQLAlchemy 默认会先把 `products.color_variant_id` 置 NULL 再删父行,而该列本批次恰好可空 | **对**,且 `"all"` 而非 `True` 的理由成立(已加载子行仍会被置 NULL) | ✅ |
| R2 权威同步 | **成立**。`update_product` 是改受众的唯一入口(全树只有 `api/products.py:181` 一个调用方),只改副本不改 `spus.audience` | **对**,三件事同事务;`or_` 已在 `product_service.py:7` 导入,无需补 import | ✅ |
| R3 身份标记 | **成立**。`_is_identity_column()` 只问 `info={"identity": True}`,而这两列没打 | **对**。风险面已核:唯一调用方是 API,`ProductUpdate` 不含这两个字段,无回归 | ✅ |
| R4 看门狗上屏 | **成立**。`identity_shadowed` 在 `imageSets.ts:115` 的联合类型里,`ImageSetTab` 不渲染 | **对**。字段名与后端 `drift()` 对得上;`drift` 是 `Partial<Record<...>>`,`?? []` 合法 | ✅ |
| R5 目录名回落 | **成立** | **对**。回落顺序被守卫钉住(编码不许压过名字) | ✅ |
| R6 `name` 截断 | **成立**。`limit_for("name")` = 255,拼接最长 281 | **对**。上限读 `field_limits`,不写死 | ✅ |

**另核过、没有问题**(免得下一轮重查):`Audience` 是 `StrEnum`,`.value` 落
`String(16)` 列安全;`spus.audience` 确为 NOT NULL,R2 的入口闸确实是同步方案
唯一能避开 500 的前提;`Spu` 没有 `products` 反向关系,所以 `products.spu_id`
的 RESTRICT 本来就没有被 ORM 绕过的路;`Spu.color_variants` 用
`cascade="all, delete-orphan"` 配 `ondelete="CASCADE"`,与 R1 不冲突。

## 二、合并冲突与处置

### C1 `DECISIONS.md` §3.24 编号撞车(hunk FAILED)

batch14 已占用 `§3.24`(「识别的判定与落库分家」),batch13-3 也写 `§3.24`。
两份文档同日、同基线、互不知情。

**处置**:batch13-3 的一节顺延为 **`§3.25`**,内容一字未改;
`STATUS.md` 里那句「决策沉到 `DECISIONS.md` §3.24」同步改为 §3.25。
选择顺延而不是给 batch14 让位,理由是 batch14 已随交付包发出、外部可能已引用。

### C2 `STATUS.md` 插入位置(fuzz 2)

补丁的上文锚点是 `# 当前状态` + 空行,而 batch14 已在该位置插了自己的横幅。
`patch` 用 fuzz 2 丢掉上文、靠下文锚点落位 —— **落对了**(batch14 → batch13-3
→ batch13-2,倒序不乱),但这是运气,不是设计。已人工复核确认。

### C3 合入方另修:`spu_service.py` 的 import 顺序违反 ruff I001

补丁把 `from app.core.field_limits import limit_for` 插在
`from app.core.errors import (...)` **之前**,而 `errors` < `field_limits`。
`pyproject.toml` 的 ruff `select` 含 `"I"`,CI 的 `make lint` 会红。
本机没有 ruff(无网络),但这是确定性违规 —— 已在合入时调到 `errors` 块之后。

### C4 合入方另修:真库池计数

batch13-3 的 STATUS 写「池子累计 37 条」。按文件实际函数数点过:
12-4 的 6 + 12-5 的 7 + 12-7 的 11 + 13 的 18(含 13-2 追加)+ 13-3 的 7
+ batch14 的 11 = **60**。原文 37 在它自己的分支内也对不上(应为 49)。
已按实际数订正,并注明订正来源。

## 三、合入后跑过的

| 门禁 | 合入前(batch14) | 合入后 |
|---|---|---|
| 纯逻辑用例 | 2001/2001 | **2010/2010**(+9,batch13-3 的守卫全绿) |
| `verify_imports` | 349 文件 | 349 文件 OK |
| `verify_delivery` | 13/13 | 13/13 |
| `lint_offline`(F401/UP017) | 338 文件 OK | 338 文件 OK |
| `verify_sample_data` | 5/5 | 5/5 |
| 前端 `syntax-check.mjs` | 84/84 | 84/84(含改动后的 `ImageSetTab.tsx`) |
| `p0_gate` | 1 通过 / 5 未验证 | 1 通过 / 5 未验证(**未变,也不该变**) |

## 四、合入后**仍然没跑过**的(不要当成已验)

1. **本批 7 条真库用例**,以及池子里另外 53 条。**R1 那条尤其**:它钉的是
   一句已随两个交付包发出去的验收(`HANDOVER.md` 第 339 行仍写着
   「删颜色被 RESTRICT 拦住」)。代码改了之后那句话**应该**为真了 ——
   但「应该为真」和「验过为真」正是 R1 这条缺陷本身的形状。
2. **`tsc` / ESLint / Vitest 一次没跑**(无 node_modules)。
   `syntax-check.mjs` 只证明 `ImageSetTab.tsx` 能被解析,不证明类型对。
3. **`ruff` 全规则未跑**(无网络装不上)。C3 是靠读配置定的罪,不是靠跑。
4. **batch13-3 的变异脚本没有随补丁交付**。「12 次变异全部验红」这句
   在合入侧**无法复现** —— 树里只有 `tools/mutate_batch14.py`。
   9 条守卫本身跑过且全绿,但「守卫会不会假绿」这件事没有被重新验证过,
   而那正是 batch13-3 自己在第三节强调的那件事(M11 第一版就是假绿)。

## 五、合入时看见、**刻意没做**的

- 没有替 batch13-3 补 `HANDOVER.md`。补丁没动它,而 batch14 的 HANDOVER
  是一份交付叙述,不是索引;由下一轮交付方决定怎么写自己的那一段。
- 没有把 R1 的规则(§3.25 第一节)做成全树不变式测试。理由与做法见
  当时的「反思」——那是一条建议,不是这次合并该顺手夹带的改动。

> **后续(A45-batch14-2,已落码)**:上面两条里的第二条,以及当时反思里
> 指出的「batch14 的 `missing_reason` 无人渲染」,都已在下一轮做掉 ——
> 分别是 `REVIEW-A45-BATCH14.md` 的 **F4** 与 **F2**。`HANDOVER.md` 仍未动。
