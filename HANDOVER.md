# 2026-08-21 a72 交接:SHEIN 评审 v1.3 的落码 —— 取证台账、解锁闸,以及三条"不许说反话"

> 决策记在 `docs/DECISIONS.md` §3.111 / §3.112 / §3.113。下方 a69 交接的归档在末尾。

本轮的输入是《PRD:SHEIN 真实渠道适配与自动发布》**评审稿 v1.3**,不是 PRD 本身
(PRD 不在交付包里,评审稿 P-19 已如实记着这件事)。所以落码的对象是评审的
**定稿动作与 P-01～P-21**,而不是 PRD 的条款。

## 一、开工前先清了基线的两处红

交付包 `swimwear-imagegen-a71.zip` 解开之后,`make check-offline` 当场是红的:

```
audit-guard-windows   test_shein_known_seams.py 的否定式断言读 .ts 没剥注释,棘轮 0 -> 1
ruff check            六个 SHEIN 模块 + 五个测试共 22 处 UP035 / I001 / UP012 / F401
```

两处都与本轮改动无关,是包自带的。第一处的形状本仓记过三次:**断言被自己的
解释性注释判红**。修法照仓内既有约定 —— 否定式剥注释,肯定式仍读原文以留在
那条门禁的射程内。

## 二、九件落码,每一件对着评审的一条

```
P-19/20/21/14  取证台账三列拆开                app/channels/shein/sources.py
P-01           店铺身份唯一派生点,今天拒绝派生   app/channels/shein/shop_identity.py
P-06           投递证据三档 + 不自动重试         app/workflows/delivery_evidence.py
P-05           重复信号自成一格                 app/workflows/shein_binding.py
P-07           一个计划一个子站点               app/channels/shein/site_plan.py
P-03           双人复核:先不做,写成会红的事实   app/workflows/dual_control.py
P-04(前半)    真实写操作解锁闸                 app/channels/shein/readiness.py
P-04(后半)    图片准备 checkpoint/租约/fence   app/workflows/shein_image_prep.py
—              状态页说得出「SHEIN 接没接」      pages/SystemStatusPage.tsx
```

三件值得单独说。

### 取证台账:两个日期不是同一件事

评审 v1.2 在那一列上出过一次错(把 S-20 的抓取时间挪到了 S-07 头上)。错法不重要,
**那一列同时装着两种事实**才重要:页面标注更新时间是官方侧的版本,本轮抓取时间是
我方侧的取证记录。混在一起,「这条上次何时被我方核对过」就从矩阵里读不出来 ——
而 S-01～S-31 逐页重开之后,复核结果需要一个落点。

状态由三列**算出来**,不手填。手填的形状是「行说自己已核验,而哈希那格是空的」。

### 解锁闸:六个能被单独读到的标志,汇不出一个答案

取证标志散在六个模块里,每一个都读得到,而**没有任何一处回答"那到底能不能发"**。
闸把它们**原样**串起来(不重写措辞 —— 重写的那份改了不会跟着变),当前 BLOCKED。

`shein.describe()` 的 `ready` 因此从一个字面 `False` 改成闸的答案。字面量在今天
看不出区别,而它在取证完成那天不会变 —— 硬规则 4 说的正是这件事。

### 双人复核:选了"先不做"

现有身份模型交不出两个人:浏览器登录只认 `admin` / `operator` 两个用户名,
`ADMIN_TOKEN` 命中时身份名固定是 `admin`。**一个假的双人复核比没有双人复核更危险** ——
它会让别的控制被放松,而它挡不住任何东西。规则本身照样落地并被验过,因为规则与
身份模型无关;`supported_today()` 不是手填的布尔,加一个不是角色名的用户名它自己就翻。

## 三、顺手找回了一条掉出去的横幅

`EnvironmentBanner` **不在 `AppLayout` 的渲染树里**。组件还在、它自己的注释还写着
「沉默在这里等于默认回答了"是真的"」,而它一次都没有被渲染过。

两侧都没有变红,因为**两侧描述的都是一个已经不成立的事实**:

```
tests/component/browser-login.test.tsx   按「外壳顶部那三条横幅」把三条都 mock 掉
                                          —— mock 掉之后恰恰看不见少了一条
tests/e2e/smoke.spec.ts                  还在给 /api/environment 铺桩,并解释
                                          这个组件在页面级 ErrorBoundary 外面
```

挂回去,并加一条会红的守卫。变异验证:拿掉那一行,`system-status-channels.test.tsx`
的「外壳顶部的三条横幅」当场红。

## 四、自查:我自己造了两处同形状的错

**这一节是交付前的自查结果,不是事后补的。** 两处都在取证台账上,形状与评审稿
§零 记的三次一样:**把一个充分看起来像证据的信号,当成一个更大结论的证明**。

```
第一列叫「页面标题」   那些字符串是从引用它的端点反推的本仓称呼,不是官方页面上
                       那行字。下一个人拿它核对页面会核对不上
第二列不分来路         S-07 的 2026-07-10 与 S-20 的 2026-08-21 都来自评审稿转述,
                       而台账把它们记成了我方的取证记录
```

第二处的代价具体:`VERIFIED` 会在没有任何人打开过页面的情况下被算出来 ——
而这张表存在的全部理由就是回答「谁在哪天真的读过它」。已改:第一列叫「本仓称呼」,
新增第四列 `provenance`,**转述的行永远到不了 `VERIFIED`**,守卫钉住。

还有一处不是错但值得写下来:`site_plan.py` 与 `shein_image_prep.py` 今天**没有
调用点**。判定先落地、接线随后是这个仓库认可的做法,但它与"写完就忘了接"在代码里
长得一模一样 —— 差别只在有没有人说出来。所以有一张接线台账
(`test_shein_readiness.py` 末尾):没接线的必须在表里并写清在等什么,接上那天守卫
会红,提醒把那一行删掉。

## 五、外部复审四条,以及它们为什么门禁一条都没红

交付之后的外部静态复审报了 1 个 P0、3 个 P1。**四条我都逐条复现过,都是真的,
都已修。** 记在 `DECISIONS.md` §3.114。

```
P0  实测动作的答案没有落点   GRANULARITY 能记"跑过了",记不下"答案是什么",
                             于是作用域永远是 appid:value。身份若是店铺级,
                             换个应用就查不到历史绑定 -> 重复创建
P1  写好了没人读的检查       archive_mismatches() 有守卫、没有调用点。
                             "填一个哈希、不存正文"照样 VERIFIED、照样不阻断
P1  一把键背了四个事实       用货号构造 SPU 绑定(双 SKU -> 两条绑定);
                             上下架与价格被压成同一个 SKU×site
P1  any 与 >= 2              加进来一个具名账号就自称支持双人复核,而凑不出两个人
```

**共同点值得单独写下来:它们全都被 `BLOCKED` 状态遮着。** 自动化门禁一条不红 ——
因为今天没有任何路径走得到那些分支。取证补齐、闸抬起来的那一刻它们才会同时暴露,
而那正是最不该出事的时刻。

这一轮我自己的两次自查(第四节)抓的是"我写的东西对不对";外部这四条抓的是
**"我写的东西在它真正生效的那天对不对"** —— 后者靠跑门禁看不出来,只能靠人读。

## 六、复审第二轮:3 个 P1、1 个 P2,同样逐条复现后修

记在 `DECISIONS.md` §3.115。

```
P1  黑名单式判据         probes_outstanding() 判的是 `== NOT_RUN`。三条状态写成
                         TYPO 之后阻断清空、身份派生成功 —— 一个拼错的字符串
                         把 fail-closed 绕过去。改成白名单:不是 PASSED 的都没通过
P1  幂等键 ≠ 事实身份    "平台有 idempotencyKey"回答的是另一个问题。没有本地事实键,
                         两个仓位的同一个 SKU 在本地是同一行,后写的覆盖先写的
P1  名单 ≠ 凭据          ann 与 bob 可以共用一把 OPERATOR_PASSWORD。数名字数不出人,
                         新增 PER_PERSON_CREDENTIALS_ISSUED,依据是 auth.py 怎么查口令
P2  三次 TemporaryDirectory  补丁重复,前两次立即丢引用
```

**这一轮里接线台账自己证明了它是双向的:** `site_plan.py` 因为第二条被
`readiness` 读了,那条守卫当场红并点名「台账里却已经接线的」—— 于是台账删掉那一行。

两轮复审加起来的教训只有一条,而它不是"某个判据写错了":**我写的每一处 fail-closed,
都要问一遍"它拦的是我想到的那些坏值,还是所有不是好值的值"。** 第一轮的四条与
这一轮的前三条,全都是前者。

## 七、门禁

```
纯测试            3533/3533(本轮 +198)
ruff              全绿(修了包自带的 22 处)
verify_delivery   23/23
其余静态审计      全 PASS
前端              tsc 0 错、ESLint --max-warnings=0 干净、Vitest 213/213(+9)、
                  vite build 出得了产物、离线体检 154/154
```

**本轮的前端改动是真的跑过的。** a69 交接里那句「前端十项修复一行都没有被执行过」
在这一轮不适用 —— `npm ci` 装得上,四层门禁都执行了。

## 八、没做的,以及为什么

```
真库            requires_db 用例、Alembic 升降级、0055/0056  —— 按 CLAUDE.md,
                本地真实基础设施验证须由用户明确触发,本轮没有这条指令
Playwright      任务 24 未开工,容器里下不了浏览器
docker build    没有 daemon
真浏览器点击     jsdom 不是浏览器:布局、字体、真实网络一条都验不到
```

**P-06 的第四句欠着**:「service 在结果事务 append-only 落库」没有落点。重试会覆盖
同一行 attempt 的 `safe_response_snapshot`,所以要一张 append-only 的表 + 一条迁移,
而迁移升降级要真库。同时今天没有任何东西**产**证据(SHEIN 没有 transport,
Simulator 同步返回)。已登记成带还款日的欠账守卫(阶段 5),在
`tests/pure/test_delivery_evidence.py` 末尾;`shein_binding` 那边的
`NOT_CREATED_CONFIRMED` 无生产者同理。

**`DELIVERY_STAGE` 仍然是 4。** 本轮新增两条还款日为阶段 5 的欠账,推闸会让它们
连同既有的那批当场逾期 —— 而它们都不是本轮的交付项。

## 九、下一步不是写代码

评审 P-19 说得很清楚:基准没定之前,那些以「仓内缺口」为依据的条款**不是有分歧,
是不可评审**。所以下一步是取证,不是编码:

```
1  ZIP 与 SHA256SUMS 对工作树逐项比较,明确以哪棵树为基准
2  S-01～S-31 逐页重开(S-07 切 3001926),按 SOURCES.md 的形状存档
3  稳定店铺身份的取值来源 + 三个实测动作
```

台账、闸与守卫都已经建好,三列在等数据。**填上数据它们自己会翻**;
在那之前 `derive()` 抛错、`readiness.mode()` 是 BLOCKED、`build_request()` 拒绝构造报文
——这三句话是机读的,不是文档里的一句话。

## 更早的交接

**只留最近一轮。** 写入新一轮时,把上一轮迁进 `docs/notes/` —— 口径见
`docs/DECISIONS.md` §3.107。它们记的是写下那天的事实,里面引用的文件后来可能
已经删掉,那不是错误。

| 日期 | 轮次 | 归档 |
| --- | --- | --- |
| 2026-08-19 | 2026-08-19 a69 交接:界面走查 + 「报错就是界面文案」这条约定终于有人守了 | [2026-08-19-handover-a69](docs/notes/2026-08-19-handover-a69.md) |
| 2026-08-19 | 2026-08-19 a68 交接:a67 外部审计四条全部落地 | [2026-08-19-handover-a68](docs/notes/2026-08-19-handover-a68.md) |
| 2026-08-16 | 2026-08-16 a54 交接:上一轮走查的十处修复,加一条失锚的变异 | [2026-08-16-handover-a54](docs/notes/2026-08-16-handover-a54.md) |
| 2026-08-16 | 2026-08-16 a53 交接:运行日志控制台 —— 归类、展示、原文 | [2026-08-16-handover-a53](docs/notes/2026-08-16-handover-a53.md) |
| 2026-08-15 | 2026-08-15 a51 交接:建档的四类失败提前到"下一步",SPU 停用补上一个到不了的状态 | [2026-08-15-handover-a51](docs/notes/2026-08-15-handover-a51.md) |
| 2026-08-11 | 2026-08-11 a48 交接:a47 自审复核 —— 交付的树跑不过前端第一道门禁 | [2026-08-11-handover-a48](docs/notes/2026-08-11-handover-a48.md) |
| 2026-08-11 | 2026-08-11 a46-phase6 交接:登录 PRD v1.3 的 Phase 3 + Phase 4 | [2026-08-11-handover-a46-phase6](docs/notes/2026-08-11-handover-a46-phase6.md) |
| 2026-08-11 | 2026-08-11 a46-phase5 交接:文档审核收口 —— 15 处说法与代码对不上 | [2026-08-11-handover-a46-phase5](docs/notes/2026-08-11-handover-a46-phase5.md) |
| 2026-08-10 | 2026-08-10 a46-phase4 交接:打包假阴性定位收口 + 离线全量复验 | [2026-08-10-handover-a46-phase4](docs/notes/2026-08-10-handover-a46-phase4.md) |
| 2026-08-10 | 2026-08-10 a46-phase3 交接:让浏览器登录能被部署、被验收 | [2026-08-10-handover-a46-phase3](docs/notes/2026-08-10-handover-a46-phase3.md) |
| 2026-08-10 | 2026-08-10 a46-phase2 交接:浏览器登录的前端接线 | [2026-08-10-handover-a46-phase2](docs/notes/2026-08-10-handover-a46-phase2.md) |
| 2026-08-09 | 2026-08-09 人工测试准入收口交接:身份先行、0054 异步识别、签名预览 | [2026-08-09-handover-2026-08-09-0054](docs/notes/2026-08-09-handover-2026-08-09-0054.md) |
| 2026-08-09 | 2026-08-09 评审修复交接:F-12/F-4 颜色维已可操作,`DELIVERY_STAGE` 仍是 4 | [2026-08-09-handover-2026-08-09-f-12-f-4-delive](docs/notes/2026-08-09-handover-2026-08-09-f-12-f-4-delive.md) |
| 2026-08-12 | A45-batch29/30 交接:阶段 6 五批全部落码,而 `DELIVERY_STAGE` 仍是 4 | [2026-08-12-handover-a45-batch29](docs/notes/2026-08-12-handover-a45-batch29.md) |
| 2026-08-12 | (上一版)A45-batch21/22/23 交接:阶段 5 五项交付全部落码,而 `DELIVERY_STAGE` 仍是 4 | [2026-08-12-handover-a45-batch21](docs/notes/2026-08-12-handover-a45-batch21.md) |
| 2026-08-12 | A45-batch20 交接:阶段 5 批次 5-2B —— 颜色维接线,以及第一份真库证据 | [2026-08-12-handover-a45-batch20](docs/notes/2026-08-12-handover-a45-batch20.md) |
| 2026-08-12 | A45-batch17-2 补丁审核:发布落库 fencing 合入,并补正来包的三处盲点 | [2026-08-12-handover-a45-batch17-2](docs/notes/2026-08-12-handover-a45-batch17-2.md) |
| 2026-08-12 | A45-batch17-1 补丁审核:只移植与当前基线独立成立的两项 | [2026-08-12-handover-a45-batch17-1](docs/notes/2026-08-12-handover-a45-batch17-1.md) |
| 2026-08-12 | A45-batch15-merged 交接:说缺口已关的话,以及 docs 的一次清账 | [2026-08-12-handover-a45-batch15](docs/notes/2026-08-12-handover-a45-batch15.md) |
| 2026-08-12 | A45-batch14-25 交接:14-24 那条 `GenerationCandidate.height` 是假的,以及它为什么是假的 | [2026-08-12-handover-a45-batch14-25](docs/notes/2026-08-12-handover-a45-batch14-25.md) |
| 2026-08-12 | A45-batch14-24 交接:§3.38 那条规矩现在有机械落点了 | [2026-08-12-handover-a45-batch14-24](docs/notes/2026-08-12-handover-a45-batch14-24.md) |
| 2026-08-12 | A45-batch14-23 交接:§6.5 两列有写入路径了,§4.8 去重键那笔账终于有人记 | [2026-08-12-handover-a45-batch14-23](docs/notes/2026-08-12-handover-a45-batch14-23.md) |
| 2026-08-12 | A45-batch14-22 交接:素材颜色归属有了写入路径,样例数据换成新结构 | [2026-08-12-handover-a45-batch14-22](docs/notes/2026-08-12-handover-a45-batch14-22.md) |
| 2026-08-12 | A45-batch14-21 交接:`facts_stale` 接线,欠账还款日有了会响的门禁 | [2026-08-12-handover-a45-batch14-21](docs/notes/2026-08-12-handover-a45-batch14-21.md) |
| 2026-08-12 | A45-batch14-20 交接:两条并行线合并 —— 阶段 3 接线欠账还清,阶段 4 落码 | [2026-08-12-handover-a45-batch14-20](docs/notes/2026-08-12-handover-a45-batch14-20.md) |
| 2026-08-12 | 评审整改批交接:REVIEW II.8 / III.2 / III.6 / II.1(2026-08-12) | [2026-08-12-handover-review-ii-8-iii-2-iii-6-ii](docs/notes/2026-08-12-handover-review-ii-8-iii-2-iii-6-ii.md) |
| 2026-08-12 | 前端(装 node_modules 后):III.2 的棘轮只守结构,迁移后的行为要这三条 | [2026-08-12-handover-node-modules-iii-2](docs/notes/2026-08-12-handover-node-modules-iii-2.md) |
| 2026-08-12 | 真库:发布并发那 7 条(II.1 的守卫对象一旦落地就靠它) | [2026-08-12-handover-7-ii-1](docs/notes/2026-08-12-handover-7-ii-1.md) |
| 2026-08-12 | 真实 FASHN Key:按 docs/PROVIDER-FASHN.md §8 首验(计费头语义 / 超时重发计费 / | [2026-08-12-handover-fashn-key-docs-provider-fa](docs/notes/2026-08-12-handover-fashn-key-docs-provider-fa.md) |
| 2026-08-12 | 幂等键接受性)—— 这决定 II.8 的文案之外,那道限制本身在真端点上怎么表现 | [2026-08-12-handover-ii-8](docs/notes/2026-08-12-handover-ii-8.md) |
| 2026-08-12 | 2026-08-12 a50/a51 交接:登录限流 / 客户端 IP / 路由级代码分割 / nginx 安全头 / celery_app / db/session —— 以及本次评审整改 | [2026-08-12-handover-a50](docs/notes/2026-08-12-handover-a50.md) |
