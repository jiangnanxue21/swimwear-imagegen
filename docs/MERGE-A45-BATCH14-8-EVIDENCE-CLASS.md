# A45-batch14-8:阶段 2 的 `evidence_class` 单点派生

> **一句话结论:那条「AI 图进识别输入产生真实付费调用」的开口,今天就堵上了,
> 不需要迁移、不需要真库。**STATUS 记的缓解手段(「人工确认素材库里没有 AI 图」)
> 在正常运行下做不到 —— 图不是人放进去的,是 `_persist_candidates` 每跑完一个
> 生成任务自己放的。纯逻辑 2090/2090、变异 24/24 验红、锚点 111/111、
> 交付 13/13、导入 359、样例 5/5。真库用例池**没有增加一条**。

---

## 一、动了什么

| 文件 | 改动 |
|---|---|
| `app/core/enums.py` | 新增 `EvidenceClass`(四值),与 `MediaSource` / `MediaRole` 并列 |
| `app/media/evidence_rules.py` | **新建。**派生 + CHECK 孪生 + 白名单判定 + 素材行适配器,零依赖 |
| `app/attributes/service.py` | `run_extraction` 接入白名单;全被过滤时单独报错 |
| `tests/pure/test_a45_batch14_7_evidence_class.py` | **新建。**31 条守卫,其中 4 条穷举 |
| `tools/mutate_batch14_7.py` | **新建。**24 条变异,先于守卫写 |
| `tools/verify_delivery.py` | `WIRED_MODULES` 登记 `asset_is_extraction_input` |

## 二、开口本来有多大

`shadow_from_candidate()` 把每张候选图写成该商品的 `MediaAsset`,
`source=AI_GENERATED`、`status=READY`。而 `usable_assets()` 只过滤
`product_id` + `status=READY`。两件事接起来:

    每跑完一轮生成 → 候选图进素材库 → 下一轮识别把它们全喂给付费抽取器

`media/service.py` 顶部那句「和人工上传的图从这里开始是同一种东西(§4.5)」
对上架链路是对的,对识别链路不对。**§5.1 就是 §4.5 的那条例外,之前没人切出来。**

## 三、PRD §4.8 规则表落不了码的五处

1. **`role = MODEL_REFERENCE` 按字面写是死分支。**`MODEL_REFERENCE` 是旧
   `AssetType` 的取值,`MediaRole` 里没有这个成员;`LEGACY_ASSET_TYPE_TO_ROLE`
   把它映射成 `MediaRole.MODEL_FRONT`(该表自己注明有损)。按字面写永不触发,
   改认 `MODEL_FRONT` 则每张正经模特图都被降级。处理:认标记集合,
   同时喂 `role` 与 `legacy_asset_type`。有守卫钉住这个凑合状态。
2. **「可信 IMPORTED_URL」本仓没有定义。**改为要求显式声明,默认 `False`。
3. **规则表没有 else 分支。**兜底 `REFERENCE_ONLY`,fail closed。
4. **CHECK 原文漏了 `generation_candidate_id`。**只有候选 id 的记录能合法地
   通过那条 CHECK 成为 `PRODUCT_EVIDENCE`。落库时两列都要写进 CHECK。
5. **规则表漏了 `BACKGROUND_REFERENCE`。**它和模特参考图同类(生成链路的输入
   参考),但旧映射把它落成 `MediaRole.OTHER`,在来源判定里畅通无阻。
   **这条是穷举旧枚举的守卫撞出来的**,挑样本的写法撞不到。

## 四、两个刻意不作为输入的东西

- **`role_source`(人工确认状态)**:只影响降级一侧。要求人工确认才降级,
  代价是在人来点确认之前每一轮识别都照付。fail closed。
- **`status`**:「能不能用」由白名单的 READY 回答。编进证据等级会造出
  第二个事实源,而人工放行会改状态、不该改证据等级。

两条都有守卫钉着,红了的意思是「先读那段理由」。

## 五、变异里最值得记一笔的两条

**P2(白名单条件取反)第一轮是绿的。**它不是关掉修复,是把修复反过来 ——
识别只跑在本该排除的图上,也就是**只跑 AI 图**。而它对当时的守卫完全隐形:
调用还在、结果照样赋回 `assets`、没有硬编码、没有第二套条件。
补的守卫钉的是条件的**形状**:必须是那个适配器的裸调用,不许取反、不许拼接。

**X1(把输入空间删剩一个来源)**暴露了穷举守卫会平凡通过。补了一条钉住
空间规模(4928)与四类结果分布的守卫 —— 派生函数塌成恒返回一个值时,
「没有 AI 输入能成为商品证据」会是一句正确的废话。

## 六、这一批**没有**做的

- 存储列、CHECK 约束、归属外键、迁移 —— 要真库。
- `media.evidence_assets_for(spu_id, scope)` 的 SQL 实现。判定已验过,那边只是翻译。
- 「模特参考图 / 背景参考图」那一路在 `run_extraction` 这个入口**还没生效**:
  `legacy_asset_type` 要 join 回 `product_assets` 才拿得到。今天它们仍会作为
  证据进识别 —— 与本批之前一样,不是回归。随归属外键一起收。
- 按颜色上传 UI、完整度检查、role 门禁口径(阶段 2 剩余交付)。

## 七、需要拍板的一件事

`run_extraction` 显式传 `imported_url_trusted=True`,**为的是维持现状而不是判定可信**。
今天 `IMPORTED_URL` 与 `PLATFORM_SYNC` 在生产代码里一个写入点都没有,
所以这个值改变不了任何一条真实数据的去留。接可信机制时改这一处,
不要改默认值 —— 默认值留给新调用点,新调用点该 fail closed。
