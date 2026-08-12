# 2026-08-11 a48 交接:a47 自审复核 —— 交付的树跑不过前端第一道门禁

> 决策记在 `docs/DECISIONS.md` §3.73。下方 a46-phase6 交接紧接着,保留为历史记录。

## 本轮不是新需求,是复核 a47 并焊上两条缝

先说结论,因为它不好看:**a47 交付出去的这棵树,`npm run typecheck` 会红。**
`components/AppLayout.tsx` 的 `WarningOutlined` 是死 import,从 a46-phase6
删掉 `sharedActor` 告警横幅那一刻起就死在那里,而离线门禁**结构上看不见它**。

a47 自己没做错什么:它没动那个文件,而它能跑的门禁全绿。这正是问题 ——
发现者只能是「有人恰好在联网机器上跑了一次前端门禁」,而这棵树上多数轮次
没有那样一台机器。a46-phase6 自审时**专门去找**死 import,找到两处,漏了第三处。

## 开工前先记下基线(§9 那条规矩:分清"我改红的"和"本来就红的")

    纯测试                  2788/2788,0 失败,10 条缺依赖跳过
    make audit-anchors      565/565(33 份脚本)
    make audit-guards       653 个守卫,反向断言窗口封闭
    make audit-doc-refs     活文档路径引用全部指得到
    make verify-imports     489 个文件全部解析得通
    make verify-sample-data 10/10
    make verify-delivery    18/19(唯一 FAIL 是「不是 Git 工作树」,解包目录跑不了)
    frontend syntax-check   97/97 —— **而它当时只解析语法**

## 改了什么

    frontend/tools/syntax-check.mjs   加第二遍:死 import(走 TS 的 AST,不走正则)
                                      覆盖面 src -> src + tests(tsconfig 的 include
                                      本来就是两棵,原来那句"离线绿了"在 tests 上
                                      从来没成立过)
    frontend/src/components/AppLayout.tsx   删掉死 import,留墓碑注释
    frontend/CLAUDE.md + AGENTS.md    门禁一节写清 syntax-check 是那五条的**离线替身**
                                      与它仍验不到什么(两份逐字一致)
    Makefile                          check-offline 的注释与两处 SKIP 文案跟上新口径
    app/scripts/provider_baseline.py  显式 override_plan=True —— 见下一节
    app/scripts/smoke_test.py         回读出参,被方案接管时如实打 note(不 fail)
    tests/pure/test_a48_...           3 条:每个调用方必须表态 / 基线必须绕 / 反平凡
    docs/DECISIONS.md §3.73           三条结论 + 本轮验不到什么

## a47 改的是一个函数的入参语义,受影响的是它全部调用方

这一条比死 import 值钱。a47 §5 让方案接管出图参数是对的,但它把
`create_task` 的 `provider` / `model_template_id` / 一轮张数 / 提示词
从「调用方说了算」改成了「方案在的时候方案说了算」——而 a47 只跟了一个调用方。

漏掉的是 `provider_baseline.py`:SPU 上有 ACTIVE 方案时,两条腿的 `provider=`
**双双**被换成方案里那一个,而脚本照旧打印一张对比表。不报错,答案是假的。
`LOCAL_MANUAL_TEST.md` §4.6 的 §5 验收第一步正是「配一份 ACTIVE 方案」,
照着文档走一遍 `make baseline` 就废了。

`smoke_test.py` 是同一件事的另一面而结论相反(冒烟要测运营真实走的路,
所以**不**绕方案,改成如实报出被接管)。两者的取舍写在 §3.73 第二节。

## 变异验红:六条,全部按预期

守卫必须证明它会红,否则加了等于没加。用 copy/restore 做,不用反向替换 ——
**第一次尝试就是在这里翻的车**,值得记一笔:`sed 's/...$/'` 在 CRLF 文件上
锚点根本不匹配,而 sed 不报错;于是"变异"没生效、测试照旧全绿,
差一点被读成"守卫不设防"。这与 `audit_anchors.py` 防的是同一件事 ——
**一个没命中的锚点和一个没有锚点是一回事**,所以现在变异脚本先断言命中数为 1。

    B  删掉 provider_baseline 的 override_plan=       变红 2 条(表态 + 基线必须绕)
    C  把它改成 override_plan=False                   变红 1 条(只有基线那条)
    E  删掉 HTTP 接口那一处的 override_plan=          变红 1 条(只有表态那条)
    D  SERVICE_ALIASES 清空                           变红 2 条(反平凡用例先响)
    F  把 WarningOutlined 加回 AppLayout              syntax-check 退出码 1,点名文件与行
    G  在 tests/ 下种一个死 import                    同上 —— 证明第二棵子树真在射程里

C 与 E 分别只红一条,这是设计:两条守卫钉的是不同的东西(有没有表态 /
基线表成哪个值),它们如果总是一起红,其中一条就是多余的。

## 第二批:同一条缝的第三面,以及我自己改出来的第二份真相

第一批交完之后又过了一遍,补了三件:

    frontend/tools/syntax-check.mjs   第三遍:断 import —— 本地模块里根本没有
                                      这个导出名。后端 verify_imports.py 早就在
                                      check-offline 里,**前端一直没有对侧**
    tests/pure/test_a45_batch11_...   那条冒烟守卫的判据补两条(见下)
    docs/PROVIDER-FASHN.md            基线脚本绕过方案这件事,写在用它的人会看的地方

**第三遍扫下来是干净的,一处都没有。** 加它的理由不是发现了缺陷,是
「这一次没漏」和「有东西在看」是两件事 —— a46-phase6 与 a47 各做过一次
横跨九个文件的删除,而那类改动漏掉调用点时,离线没有任何东西会红。

**第二件是我自己造成的,值得单独说。** `test_smoke_exercises_the_license_gate_
instead_of_the_bypass` 的文档字符串说「冒烟真的执行了 §10.5/§11 四道检查」,
而它的判据只有"请求体里传没传 model_template_id"。a47 之后这句话变成有条件
成立(条件是那只 SKU 的 SPU 上没有 ACTIVE 方案)。我给 smoke 加了回读却没有
同时加判据的话,就会留下**一条绿着的守卫说着一件不一定成立的事** —— 正是
§3.70 点名的形状,而这一次是我在修那类问题的同一轮里制造的。已同批改写。

第二批变异验红,五条:

    H  具名导入一个不存在的导出        BROKEN,点名模块与名字
    I  指向一个不存在的本地文件        BROKEN,点名"找不到对应文件"
    K  默认导入一个没有 default 的模块  BROKEN,点名 default(as X)
    J  把 types.ts 的 AUDIENCES 改名   BROKEN ×3 —— 三个调用点全部点到,
                                       这正是"改名漏了调用点"的真实形状
    L  删掉 smoke 的回读               冒烟守卫变红
    M  删掉"由生成方案接管"那句话       同上,红在另一行

## 收工复跑(全绿)

    纯测试                  2791/2791(+3),0 失败,10 条缺依赖跳过
    make audit-anchors      565/565
    make audit-guards       654 个守卫,窗口封闭
    make audit-doc-refs     全部指得到
    make verify-imports     490 个文件
    make verify-sample-data 10/10
    make verify-delivery    18/19(同基线,仍是那条 Git 工作树)
    frontend syntax-check   115/115 files clean(语法 + 死 import + 断 import;
                            文件数从 97 涨到 115 是因为纳入了 tests/,不是新增了文件)

## 没做的,和为什么 —— 这一节比上面那一节重要

**前端四条(typecheck / lint / Vitest / build)与 Playwright 一条都没跑。**
无网络、装不上 `node_modules`。所以本轮修的那一行死 import 会让 typecheck
变红这件事,依据是 `tsconfig.json` 的 `noUnusedLocals: true` 与 TS 的成文行为,
**是推断,不是一次真实的红**。同理,新加的第二遍虽然自己做过变异验红,
它与 tsc 的判定是否逐条一致,也要等一台联网机器。

复跑:

    cd frontend && npm ci && npm run typecheck && npm run lint && npm run test && npm run build

**改过的两个脚本一次都没运行过。** `provider_baseline` 与 `smoke_test` 都要
真库 + Redis + worker 才跑得起来,本机三样都没有(且按仓库约定,真实基础设施
验证须由用户明确触发)。它们改的是「排障工具会不会给出假答案」——
而假答案正是在排障时最贵,所以接第一台有库的机器时建议把这两条排在前面:

    make baseline SKU=SW-001-BLK-S P=mock,fashn   # 先给该 SPU 配一份 ACTIVE 方案
    make smoke                                     # 看有没有那条「由方案接管」的 note

**a47 自己欠的账一条都没还**:`tests/test_a47_plan_governs_db.py`(§5.5 四条
等式 + override 分支 + 403)仍是写了没跑;PRD 验收 #11(实际出图角度集合 ==
`gp.required_angles`)在这个基线上落在提示词而不是数据上,要验到它得给候选图
加角度标注 —— 那仍是另一轮的事(§3.72 五)。PRD 验收 #22(Mock / Simulator
happy path 完整回归)同样未执行。

**我最不放心、建议第一个看的两处**:

    syntax-check 第二遍的误报面   属性名位置排了四种(属性访问 / 属性赋值 /
                                  属性签名 / 限定名右半边)。全仓 115 个文件
                                  当前零误报,但这个清单是我按 TS 的节点类型
                                  列的,不是穷举出来的 —— 第一次有人被它冤枉时,
                                  先看是不是第五种位置
    smoke_test 的回读             `TaskOut` 有 `provider` 与 `model_template_id`
                                  两个字段,我按 schema 读的,没有跑过一次真实
                                  响应。字段名对不上时表现是"永远不打那条 note",
                                  而不是报错

---

# 2026-08-11 a46-phase6 交接:登录 PRD v1.3 的 Phase 3 + Phase 4

> 决策记在 `docs/DECISIONS.md` §3.71。下方 phase5 交接紧接着,保留为历史记录。

## 先说清楚本轮做了 PRD 的哪一段

PRD v1.3 的基线是 `20260810-1230`,而这棵树已经落了 a46-phase1~5。逐条核对之后,
**Phase 0/1/2 已经在树上**(5 个 env + 启动校验、itsdangerous、SessionMiddleware
与中间件顺序门禁、login/logout/whoami、Session 优先、精确匿名白名单、conftest
夹具、LoginPage、`withCredentials`)。本轮做的是 **Phase 3 与 Phase 4**:

    Phase 3  清理 Token UI + 菜单角色收敛 + 三处 401/403 文案
    Phase 4  §41 的门禁逐条改写 + 文档四处 + DECISIONS 一条

验收总表 34 项里,本轮关掉的是 #16 #17 #21 #22 #24 #25 #26 #29 #30 #33。

## 改了什么

    frontend/src/api/client.ts        删 Token 存储链、请求拦截器、adminHeaders、
                                      authRejected 组;401/403 三处文案改写
    api/{settings,prompts,generation,batch}.ts   adminHeaders() 调用点全删
    hooks/useIdentity.ts              enabled 收敛成 !health.isError;isAdmin 只认
                                      后端;删 usingAdminFallback 与口令订阅
    components/AppLayout.tsx          NavGroup.adminOnly;visible 按 isAdmin 过滤;
                                      顶栏「系统设置」只给管理员;删 sharedActor 告警
    components/ColdStartBanner.tsx    只剩「后端不可用」一支
    pages/SettingsPage.tsx            删口令录入卡与 saveToken;operator 进来是 403
    App.tsx                           「系统管理」标 adminOnly;三段注释翻正
    8 条纯层门禁                      4 改写 / 3 删除留墓碑 / 1 翻转(见 §3.71 第三节)
    3 份前端测试                      nav-and-url-filters 反向门禁翻转;
                                      client.test.ts 删拦截器组、改 401 文案断言;
                                      error-and-cold-start 删三条口令用例、补一条
    2 条变异锚点                      mutate_a46_phase5.py C2 / mutate_batch14_4.py Q11
    文档四处 + DECISIONS §3.71

## 本轮离线验证复跑

    纯测试                  2755/2755,0 失败,10 条缺依赖跳过
    变异验红                9/9 + 2/2(C3 那条本轮翻了方向:功能落地之后,
                            原来的反向变异被抽空了,顺带把「已裁剪」的判据
                            从 or 收紧成 and —— 声明了却没人读的标记等于没有)
    make audit-anchors      565/565(33 份脚本)
    make audit-guards       反向断言窗口封闭
    make audit-doc-refs     活文档路径引用全部指得到
    make verify-imports     全部解析得通
    make verify-sample-data 10/10
    make verify-delivery    18/19(唯一 FAIL 是「不是 Git 工作树」,解包目录跑不了)
    frontend syntax-check   96/96

## 交付后自审(同轮补):七处,五处是我自己造成的

按"反思这轮改动"重新过了一遍,列出并已修:

    1  client.test.ts 的 401 用例被我改成必红      断言「重新登录」,而单测环境里
                                                  /health 从不返回,authMode 停在
                                                  默认 token,走的是免登录分支。
                                                  改为钉免登录支的负向不变量,
                                                  会话支的文案改由纯层守卫接(见 4)
    2  error-and-cold-start 我新增的用例引用了      Wrapped / 裸 render 都不存在,
       两个不存在的帮手                            文件里真实的是 renderBanner()。
                                                  已按同文件其余用例的写法重写,
                                                  beforeEach 顺带补上 sessionAuth 复位
    3  browser-login.test.tsx 的 mock 还揣着       usingAdminFallback: false ×2,
       已删字段                                    接口里这个字段本轮删了。已清
    4  会话文案「请重新登录」没有任何可跑的守卫    新增 test_the_session_401_copy_
                                                  sends_people_to_login...(剥注释判,
                                                  历史引用放行),变异 D3 验红
    5  两处死 import                              ColdStartBanner 的 Space、
                                                  SettingsPage 的 brandVars ——
                                                  typecheck 会红而 syntax-check 看不见
    6  client.ts 三段注释仍在解释旧文案            "口令模式下该说到设置页核对口令"
                                                  一族;AUTH_FORBIDDEN 分支的注释
                                                  还在说"口令是对的"。全部改写 ——
                                                  正是 phase5 修的那类病,自己又犯了
    7  SettingsPage 的 identity 注释说的是         那句 Alert 随录入卡删了,identity
       一个已删除的用途                            现在服务 403 早退。已改写并留台账

另有一处**行为记录**(非缺陷,写下来防误会):后端不可用且浏览器**冷加载**时,
横幅只显示运营话术(不带命令)—— isAdmin 只认 whoami,而后端挂了 whoami 答不了。
已登录页面里后端中途挂掉时,react-query 的缓存身份仍在,管理员照旧看到命令。
这与 phase2 会话模式的行为一致,不是本轮引入的回归。

## 没做的,和为什么 —— 这一节比上面那一节重要

**前端四条(typecheck / lint / Vitest / build)与 Playwright 一条都没跑。**
机器没有网络,`npm ci` 装不上。而本轮**动了 9 个 `.tsx` / `.ts` 源文件的可执行
代码**(不是注释)——这和 phase5 那种"只改注释"的情况完全不同。
`syntax-check.mjs` 96/96 只证明它们**解析得通**,证明不了类型、渲染与用例。

具体没被执行、且本轮**新改动的**前端用例:

    tests/component/nav-and-url-filters.test.tsx   翻转后的两条(operator 看不到管理入口)
    tests/unit/client.test.ts                      删掉拦截器组之后的其余各条
    tests/component/error-and-cold-start.test.tsx  删三条、新增一条之后的整份
    tests/component/browser-login.test.tsx         13 条,phase2 起就没跑过
    tests/e2e/login.spec.ts                        3 条,同上

复跑:

    cd frontend && npm ci && npm run typecheck && npm run lint && npm run test && npm run build
    cd frontend && npx playwright install chromium && npm run e2e

**我最不放心、建议第一个看的三处**:

    SettingsPage 的 403 早退      它在 `identity.who && !identity.isAdmin` 时直接
                                  return,而那一页原来靠下面的 hooks 顺序渲染。
                                  早退在 hooks 之后,应当没问题 —— 但没跑过
    AppLayout 下拉的展开语法      「系统设置」那一项改成了 `...(isAdmin ? [...] : [])`
                                  的展开写法,antd 的 items 接受它,但 TS 的字面量
                                  推断在这种展开下偶尔要显式标注
    error-and-cold-start 新增的那条  自审后已改用文件里真实的 renderBanner() 与
                                  container.textContent 判空,并显式复位
                                  sessionAuth —— 但整份文件仍然一次没跑过

**PRD §23 的 `RequireAuth` 组件没有做**,理由在 §3.71 第四节:行为上现有实现
已满足 §23 每一条,搬动它要重写 13 条从未执行过的用例。这是一处**显式的
范围偏离**,不是遗漏。

真库 pytest、Alembic 升降级、Redis / Celery 集成同样未执行(本地协作约束)。
PRD §42 那 25 条后端测试里,涉及 Session 的绝大部分在 `tests/test_auth_session.py`
里已有,但**它需要 pytest 与真实依赖,本机跑不了**。

---

# 2026-08-11 a46-phase5 交接:文档审核收口 —— 15 处说法与代码对不上

> 决策记在 `docs/DECISIONS.md` §3.70。下方 phase4 交接紧接着,保留为历史记录。

## 本轮不是加功能,是把「说的」和「做的」对齐

phase4 的离线门禁全绿、数字属实,我复跑过一遍一条不差。本轮查的是那些数字
**管不到**的那一层,也就是 `audit_doc_refs.py` 自己承认看不见的:路径指得到,
但那句话说错了内容。逐条证据在 §3.70,按后果分四类:

    照着做会当场失败   MANUAL-ACCEPTANCE §3.1 的 UAT 基线起不来(缺浏览器登录
                       三项,而 §5.1 跑的是 `${KEY:?}` 的生产 overlay);§5.3 的
                       鉴权矩阵还是 Header 口令那套;DEPLOYMENT 全文「登录」0 次,
                       §九仍写「MVP 没有账号体系,唯一的防线是网络层」
    说反了             App.tsx / AppLayout.tsx / useIdentity.ts / auth.py 四处仍按
                       「菜单按角色隐藏」写,而那一版从来没有落地;SETTINGS.md §三
                       的「不新建 app/api/auth.py」挂在它反对的那个文件旁边
    宣告不存在的守卫   health.py 与 client.ts 都说 `test_browser_login_frontend.py`
                       钉着这件事 —— 全仓没有这个文件
    冻住的数           文档地图「13 份」表里 15 行、「29 份」实际 9 份;STATUS 的
                       「12 条 Vitest」是 phase2 自审当场改掉的第一版数;README 的
                       「15 个硬错误代码」实际 21;「30 张素材」实际 51,同一句话
                       在五份文档里各冻一份;三份 AGENTS.md 是 CLAUDE.md 的旧副本

顺带查出一处门禁自己的射程问题:`DECISIONS.md` 的 §3.63~§3.69 用 `## 3.6x` 写,
而「决策日志编号不重复」的正则是 `^##\s+§` —— 最新七节撞号了它也看不见。已统一。

## 改了什么

    docs/DEPLOYMENT.md        §二加生产 overlay 与三把启动键;§三表加三行;
                              §九安全清单改写;「关于没有账号体系」整节重写成
                              「有登录,但没有用户表」,列出三条仍然成立的限制
    docs/MANUAL-ACCEPTANCE.md §3.1 env 补三项 + 两条说明;§5.3 拆成「浏览器走登录页」
                              与「脚本走请求头口令」两张表;示例条数不再写死
    docs/SETTINGS.md          §三的论证保留原文并说明它被什么推翻了、哪一半没落空
    docs/STATUS.md            文档地图 13→19 并补四份活文档(LOCAL_MANUAL_TEST /
                              MANUAL-ACCEPTANCE / HANDOVER / AGENTS);29→9;
                              「12 条 Vitest」改成不写数并指向本文 phase2 一节
    README.md                 接口表补 `/auth/*` 三条与七组「表外接口组」;页面表
                              11→23 行并按侧栏四组重排(**首页是 `/today` 不是
                              `/dashboard`**);目录结构补 8 个包;示例条数、硬错误
                              条数、env 分组数一律改为不写死;FASHN 自检补 `X-Admin-Token`
    四个前端/后端注释          菜单收敛那一族改成「那一版没有落地」;两处幽灵守卫
                              改指 `test_a46_phase2_browser_login_seam.py`
    tools/pack.sh             两处 `test_delivery_hygiene.py` 改指 verify_delivery
    两处纯测试注释            `test_a45_batch27_seven_steps.py` 改指 batch26 那份
    CLAUDE.md ×3 / AGENTS.md ×3  AGENTS 整份同步成 CLAUDE 的逐字副本;根 CLAUDE.md
                              的目录块加一行说明这件事
    docs/DECISIONS.md         §3.63~§3.69 标题规范化;新增 §3.70

## 新增守卫与变异验证

`tests/pure/test_a46_phase5_doc_truth.py` 7 条 + phase2 那条扩窗口(从「只钉 README」
扩到部署者会打开的三份 —— 翻掉的理由在 §3.70 第三节)。变异 **11/11 验红**,
分两份脚本:

    tools/mutate_a46_phase5.py              9/9   钉 test_a46_phase5_doc_truth.py
    tools/mutate_a46_phase5_deploy_docs.py  3/3   钉 phase2 套件(启动键新旧窗口各一,
                                            会话文案一条 —— phase6 自审补)

拆两份不是洁癖:第一版用 `SUITE_FILTER = "a46"` 一个子串盖住两个套件,
`make audit-anchors` **当场拦下** —— 子串会让一条变异被别的套件的守卫抓住,
而报告记在这份脚本头上,归因是假的。本轮主题就是「说的和做的要对上」,
在自己的变异脚本上放过它说不过去。

## 本轮离线验证复跑(全绿)

    纯测试                  2758/2758,0 失败,10 条缺依赖跳过
    make audit-anchors      564/564(33 份脚本,含本轮新增的两份)
    make audit-guards       648 个读源码守卫,反向断言窗口封闭
    make audit-doc-refs     活文档路径引用全部指得到
    make verify-imports     486 个文件全部解析得通
    make verify-sample-data 10/10
    make verify-delivery    18/19(唯一 FAIL 是「不是 Git 工作树」,解包目录跑不了,
                            与 phase2 同)
    frontend syntax-check   96/96
    tools/pack.sh           打包 + 复验通过,清单随包留档

## 没做的,和为什么

机器**仍然没有网络**。前端四条(typecheck / lint / Vitest / build)与 Playwright
一条都没跑。本轮动了 4 个 `.tsx` / `.ts` 文件,但**改的全是注释与文档字符串,
没有一行可执行代码** —— syntax-check 96/96 能证明它们仍解析得通,不能证明类型
与用例。phase2 留的那份验证缺口(21 条 Vitest + 3 条 Playwright + 2 条 pytest)
原样欠着,清单与复跑命令见下面 phase2 那一节。

真库 pytest、Alembic 真库升降级、Redis / Celery 集成同样未执行(本地协作约束:
需用户明确指令)。具名审计(users 表)照旧欠着。

## 建议下一个人先看的两处

    1. 文档地图那条守卫只钉「数和行数一致」,不钉「有没有漏收活文档」——
       漏收要靠人。加文档时顺手看一眼 §七。
    2. `test_no_live_comment_claims_a_guard_file_that_does_not_exist` 判的是时态,
       靠一张过去式标记表。写注释时如果引用一个已删掉的文件,记得带上
       「原先 / 已并入 / 当年」之类的词,否则它会红 —— 那是刻意的。

---

# 2026-08-10 a46-phase4 交接:打包假阴性定位收口 + 离线全量复验

> 决策记在 `docs/DECISIONS.md` §3.69。下方 phase3 交接紧接着,保留为历史记录。

## phase3 留的那桩悬案,查出来了,修掉了

phase3 交接说:`pack.sh` 偶发误报"缺少必备文件",清单是全的、包是好的、
那一行就在清单里,复现率约 3/12,"根因仍然没查出来,这里不编一个"。

根因是四件各自无害的事叠加:`grep -q` **命中即退**;清单 31.7 KB 经
bash printf 分多次写入管道;grep 先退、printf 没写完 → SIGPIPE(141);
`set -o pipefail` 让 141 压过 grep 的 0,`if !` 把它读成"没找到"。
所以受害者全是清单靠前的文件、诊断三条全绿 —— grep 其实**命中了**。
逐进程状态抓到过现行:printf=141、grep=0。完整推理、复现数据与一般化
见 §3.69。上一轮那份 listing.txt 诊断没白加:它把范围收窄到比对路径上,
这次就是沿着那条缝找到的。

    同形最小化 400 次      修前 38 次假阴性(退出码全 141);修后 0 次
    真实 pack.sh 连打      修前 15 次中 3 次失败(受害者与历史一致);
                           修后 40 次全过
    反向路径               必备文件真缺失 → 正确报缺、删包、清单留档;
                           故意拆掉排除规则 → 复验逐条报禁品、删包

改动三处代码:

    tools/pack.sh                    复验一律读落盘清单文件,不吃管道;
                                     清单从"失败才落盘"改为每次都落盘
                                     (`<包名>.zip.listing.txt`,与包同目录)
    tools/pack.ps1                   本无此病(内存 List 比对,无管道),
                                     只同步"清单每次落盘",证据口径一致
    backend/tools/verify_delivery.py 守卫 +2:正向钉修后的形状,反向禁止
                                     管道喂 grep -q;两个方向都做过变异验证

## 本轮离线验证复跑(全绿)

    纯测试                 2751/2751,0 失败,10 条缺依赖跳过
    make audit-anchors     553/553(31 份脚本)
    make audit-guards      638 个读源码守卫,反向断言窗口封闭
    make audit-doc-refs    活文档路径引用全部指得到
    make verify-imports    483 个文件全部解析得通
    make verify-sample-data 10/10
    make verify-delivery   19/19(本机有 git 工作树,phase2 那条 18/19
                           的唯一 FAIL 在这里不复现)
    frontend syntax-check  96/96
    tools/pack.sh          正向 40 连打全过 + 两条反向路径(见上)

## 没做的,和为什么

打这一批的机器**仍然没有网络**。phase2 留的那份验证缺口原样欠着:
21 条 Vitest + 3 条 Playwright + 2 条 pytest(auth_mode)一条都没能执行,
清单与复跑命令见下面 phase2 那一节 —— `npm ci` / `pip install` /
Playwright 浏览器在本机全部装不上。这个缺口只能等一台有网络或有
预装依赖的机器,不是这轮不想还。

具名审计(users 表)照旧欠着,见 phase2「下一步建议」第 3 条。

## 交付物

本包用**修后的** `pack.sh` 打出,包旁附 `*.zip.listing.txt` 清单留档。

---

# 2026-08-10 a46-phase3 交接:让浏览器登录能被部署、被验收

> 决策记在 `docs/DECISIONS.md` §3.68。下方 phase2 交接紧接着,保留为历史记录。

## 本轮修的是"两轮都落完了,而它仍然部署不了"

`config._check_browser_auth` 规定非 local 环境三项配不全**直接起不来**。
而 phase1 + phase2 落完之后:

    docker-compose.prod.yml   一个字都没提这三项
    全仓 .md                  `ADMIN_PASSWORD` 出现 0 次

按 README 部一次生产,拿到的是一个反复重启的后端,而 `docker compose up -d`
打印成功。修法与理由见 §3.68,一句话:把已经存在的强制**搬到看得见的地方**。

    docker-compose.prod.yml   backend/worker/beat 三个服务用 `${KEY:?消息}`
                              显式要求三项(worker/beat 也构造 Settings ——
                              只给 backend 加的表现是"页面能开,任务队列在
                              悄悄重启")
    README.md                 新增「浏览器登录」一节:两个账号、三项配置、
                              怎么生成密钥、换密钥=全员登出、多机必须同一把、
                              滑动过期不是绝对存活时长、设置页那两把是机器凭据
    LOCAL_MANUAL_TEST.md      新增 §4.5:本机怎么开、怎么确认、要走的六步,
                              外加一条容易误判的现象(会话过期不会立刻跳,
                              要等下一次请求撞 401)
    纯层守卫 +3               从 `_check_browser_auth` 本身解析必填键,要求
                              compose 逐个 `:?` 且三个服务都引到;同一份键
                              必须在 README 出现过。两条都做过变异验证

### 顺带:一次没能复现的打包失败,以及它留下的东西

本轮打包时 `pack.sh` 报了一次 `!! 交付包缺少必备文件:.gitattributes` 并按设计
删了包。**而那个文件就在工作树里**,紧接着连打三次全部通过,至今没有复现出来。
上一轮也遇到过一次同样的中断,当时我用 `tail -3` 截掉了输出,只看到删包的收尾
文字,**误判成"包里有禁品"** —— 两个分支共用同一段收尾话术。

加上诊断之后它又报了一次(这回是 `.github/workflows/ci.yml`),而诊断把范围
**收窄到了一处**:

    清单条目数   836 —— 与**成功那次一模一样**(unzip -Z1 含目录条目)
    包的完整性   unzip -t 通过
    那个文件     在工作树里存在;在成功包的清单里排第 5 行
    文件名编码   全 ASCII,locale 是 POSIX,grep 不会把清单当二进制
    排除模式     126 条,逐条比过,没有一条命中它

也就是说:**包是好的、清单是全的、要找的那一行就在清单里,而 `grep -qxF`
没匹配上。** 问题不在排除规则、不在 zip、不在磁盘(还剩 9.9G),
而在 `LISTING=$(unzip -Z1 ...)` 到 `grep -qxF` 这一小段路径上。
两次报的都是清单最前面几行的文件。复现率约 3/12。

**根因仍然没查出来,这里不编一个。** 能做的三件都做了:失败时打印条目数与
完整性、指出该文件是否在工作树、并把清单落到 `$OUT.listing.txt`(包会被删,
这份不会)—— 下一次撞上,那份文件足以一次定位。

`pack.ps1` 同步了前两条(它用 .NET 的 ZipFile,失败面不同,但"分不清这两种"
是同一个问题)。

**如果你撞上同样的事,请把完整输出和那份 listing.txt 留下来** —— 不要像我
第一次那样用 `tail` 截掉,那次我因此把它误判成了"包里有禁品"。

**本轮其余部分全部可离线验证** —— 改的是编排文件、文档和纯层守卫,没有前端代码。
compose 用 PyYAML 解过(含 `!reset` / `!override` 自定义标签),确认锚点在
backend/worker/beat 三处都展开成那三个键。

上一轮那份验证缺口(21 条 Vitest + 3 条 Playwright + 2 条 pytest 未执行)
**依然原样欠着**,清单见下面 phase2 那一节。

---

# 2026-08-10 a46-phase2 交接:浏览器登录的前端接线

> 决策记在 `docs/DECISIONS.md` §3.67(后端那一半在 §3.66)。
> 下方 2026-08-09 及更早的交接保留为历史记录。

## 先读这一节:本轮有一个真实的验证缺口

打这一批的机器**没有网络**。`npm ci` 装不上,`pip install fastapi` 也装不上,
Playwright 浏览器下不来。所以:

    跑过并绿      纯测试 2747/2747(0 失败,10 条缺依赖跳过)
                  make audit-anchors 553/553
                  make audit-guards / audit-doc-refs / verify-imports / verify-sample-data
                  mutate_batch14_4.py 20/20 条变异验红
                  frontend/tools/syntax-check.mjs 96/96
                  verify_delivery.py 18/19(唯一 FAIL 是「不是 Git 工作树」,
                                            解包目录跑不了它,属预期)
                  tools/pack.sh 正反两次(见下面「打包脚本」一节)

    **一次都没跑**  npm run typecheck / lint / vitest / build
                  pytest(含真库那一批)
                  npx playwright test

本轮主体是**前端**,所以这个缺口不小。具体没被执行的新增用例(数字是数出来的,
不是估的 —— 第一版这里写"12 条"而实际是 13,自审时改正):

    frontend/tests/component/browser-login.test.tsx    13 条
    frontend/tests/unit/client.test.ts                  6 条(自审补的那一组)
    frontend/tests/component/error-and-cold-start.test.tsx  2 条
    frontend/tests/e2e/login.spec.ts                    3 条
    backend/tests/test_auth_session.py                  2 条(auth_mode 那两条)

纯层新增的 8 条(`test_a46_phase2_browser_login_seam.py`)**跑过并绿**,
而且逐条做过变异验证 —— 它们不在上面这份清单里。

复跑:

    cd frontend && npm ci && npm run typecheck && npm run lint && npm run test
    cd frontend && npx playwright install chromium && npm run e2e
    cd backend  && pytest tests/test_auth_session.py

几个我自己最不放心、建议第一个看的点:

    getByLabelText('用户名')     antd `Form.Item` 的 label 与 input 是靠生成的
                                 id 关联的。这个 Form 没有 `name`,我按 id 直接
                                 是字段名推的 —— 定位不到的话改成 `getByPlaceholderText`
    Dropdown 展开时序            退出登录那两条要先点开顶栏身份区再点菜单项,
                                 jsdom 里 rc-trigger 的 portal 有没有渲染完,
                                 我验不了。红的话多半是要加 `await screen.findByText`
    e2e 的 page.getByLabel       同上,而且 Playwright 的 label 关联更严格

## 本轮交付

### 1. 打包脚本:`data/` 里的图片不再进包

上一版交付包里躺着 `data/s1.jpg`(5.8 MB,占整包一半多)。那个目录不在
`.gitignore` 里、全仓也没有任何代码或文档引用它 —— 也就是说没有任何机制拦着它。

`tools/pack.sh` 新增 `IMAGE_FREE_DIRS` / `IMAGE_EXTENSIONS` 两个声明,排除侧与
复验侧从同一份生成。**排的是扩展名,不是整个 `data/`**:将来往里放 README
或参数样例仍然跟着交付走。大小写做了字符类展开(`jpg` -> `[jJ][pP][gG]`),
因为 Info-ZIP 的 `-x` 在 Unix 上大小写敏感,写 `data/*.jpg` 的话 `data/S1.JPG`
照样进包 —— 而复验侧如果照抄同一个模式,两边会**一起**漏。

`tools/pack.ps1` 同步(PowerShell 的 `-like` 本身不区分大小写,那一侧不需要
展开),两个新数组挂进了 `verify_delivery.py` 的 `paired_arrays` 表:改一侧
不改另一侧当场红。那张表防的正是「Linux 排掉了、Windows 打出来的包里还有」。

实测两次:

    正向   s1.jpg / S1_UPPER.JPG / Mixed.JpEg / nested/deep.png / icon.SVG 全排掉;
           data/ 目录条目与 data/README.md 保留。包体 10.5M -> 4.4M
    反向   故意把排除侧那行删掉重打 —— 复验侧逐条报出禁品并**删包退出**。
           也就是说第 2 步对这条新规则是真的在跑

### 2. 浏览器登录的前端接线(phase2 主体)

后端只加了一处接缝:`/health` 返回 `auth_mode`(`session` / `token`),
取 `settings.browser_auth_configured` —— 和 `resolve_identity` 同一个属性。
理由与备选见 §3.67 第一节。

前端:

    api/client.ts        withCredentials: true(**整条链路的最后一跳**)
                         401(不含 403)触发会话失效信号
                         describeError 的 401 尾巴按模式分两句话
    hooks/useIdentity.ts enabled 从 hasToken 改成 (sessionAuth || hasToken);
                         新增 sessionAuth / needsLogin
    pages/LoginPage.tsx  新增。safeNext() 挡开放重定向;
                         口令模式下不画表单,直说这里没有这条路
    App.tsx              /login 挂成 AppLayout 的**兄弟**,不是子路由
    AppLayout.tsx        needsLogin 时跳登录页并带 ?next=;顶栏加退出登录
    ColdStartBanner.tsx  会话模式整条让位给登录页(那几句说的都是口令的事)
    SettingsPage.tsx     一句「这两把不是你的登录密码」

### 3. 顺手修的四条僵尸守卫(**不是本轮引入的**)

打开交付包时纯层就有 3 条红、变异脚本 1 条锚点失效。清单与处理见 §3.67 第五节。
删掉其中一条时露出一个洞(变异 Q1 从 RED 变 GREEN),已用「同一个 key 不出现
在两组里」补上,`mutate_batch14_4.py` 回到 20/20。

`docs/STATUS.md` 里两处跟着过期的说法(「「系统管理」只对管理员显示」)
一并订正。

## 自审发现并修掉的一个洞(本轮第二遍才看见)

第一版把 401 会话失效信号**算出来了,而没有任何人订阅**。后果不是少一个功能,
是"用着用着会话过期"这条路径整个不生效:身份探测挂着 60 秒 `staleTime`,
后端 Cookie 过期之后前端手里那份"我是 operator"还能继续有效一分多钟 ——
运营点什么都失败,而界面**不会把他送去登录页**,页面上也没有登录入口。

三样东西当时是死的:`SESSION_EXPIRED_EVENT`(一个从没被 dispatch 过的事件名)、
`isSessionExpired`(没有读者)、`onSessionExpiredChange`(没有订阅者)。

而**两侧的用例全绿**。`browser-login.test.tsx` 把 `useIdentity` 整个 mock 掉了,
它验的是"拿到 `needsLogin` 之后怎么办",洞在"`needsLogin` 永远不会变成真"。
这正是本仓 §3.43 那一族,而我在给它加守卫的同一轮里又造了一个。

修法:

    接线   `useIdentity` 订阅信号,401 时 `resetQueries(['auth-probe'])`。
           **不是** `invalidateQueries` —— 后者在重新请求失败时保留旧数据,
           `probe.data` 还在、`needsLogin` 还是假,等于什么都没做
    减法   删掉 `SESSION_EXPIRED_EVENT` 与 `isSessionExpired`(没有读者),
           `isSessionAuth` 改成模块内私有(只有 `describeError` 用)
    守卫   纯层加"信号必须有消费端"+"这两个死导出不许回来",两条都做过变异验证
    用例   `tests/unit/client.test.ts` 加 6 条,**一个 mock 都不用**,
           直接让真实响应拦截器跑:401 通知 / 403 不通知 / 匿名 401 不通知 /
           连着两次只响一次(防死循环)/ 登录后清掉 / 成功请求让它落下来

判定仍然只有一处:跳不跳登录页由 whoami 的回答决定,401 只是让人**再问一遍**。
反过来"看见 401 就跳"会把一次偶发 401 变成一个能无限循环的动线。

顺带修的一处 UX:登录页在 `/health` 回来之前不再画表单(改成骨架屏)。
先画表单再改主意的代价是口令模式下人已经开始输入,然后整块被换掉。

## 下一步建议

1. **先在浏览器里走一遍**,再看别的。这一批的成色完全取决于此。
2. 上面那三个定位点(getByLabelText / Dropdown 时序 / Playwright getByLabel)
   如果红,多半是定位方式问题而不是功能问题 —— 改用例,别改实现。
3. 具名审计仍然欠着(§3.66 第二节):浏览器登录只有 admin / operator 两个
   固定账号。要按人追溯,下一步是「users 表 + 每人一个账号」,
   **不是**回头去配 `OPERATOR_TOKENS`。

# 2026-08-09 人工测试准入收口交接:身份先行、0054 异步识别、签名预览

> 当前结论以 `docs/DECISIONS.md` §3.65、`docs/STATUS.md` 顶部与
> `docs/AC-VERIFICATION.md` §11 为准。下方 F-12/F-4 交接保留为历史记录。

## 本轮交付

- CSV 导入只接受已存在 SPU；单颜色可补唯一颜色，多颜色必须给 `variant_code`，
  预览与提交共用数据库身份解析并写齐 `spu_id` / `color_variant_id` / 受众 / 品类。
- 属性识别改为 Celery 异步：HTTP 先提交 QUEUED run 再投递，worker 使用排队时
  素材快照，支持取消、逐图成绩、失败颜色重试、relay/reaper 与断点续跑。
- preview→commit 使用独立 HMAC token，绑定操作者、文件摘要、预览摘要、数据库
  身份状态与有效期；文件或数据库事实变化后必须重新预览。
- 迁移 head 为 `0054`；`sample-data/products.csv` 保留为解析和拒绝路径回归样本，
  正式播种只走 `spus.json` 与 SPU 服务。

## 已有真环境记录

2026-08-09 用户授权的 PostgreSQL + Redis 回归记录为：全量 pytest 3128/3128，
新建 UAT 库从空库升级到 `0054`，真实 Celery worker ping 与 Uvicorn 健康检查通过；
前端 Vitest 100/100、Chromium Playwright 6/6。本次文档整理没有重新连接真库。

## 当前边界

- Docker CLI 仍缺失，两个镜像与 compose 六服务未执行；真实 FASHN / 真实渠道无凭据。
- 仓库根当前存在 `.env`，本轮 `verify_delivery` 因此为 18/19；未读取、未删除。
  交付前必须把它移出仓库树，若含真实凭据先轮换，不能只靠打包黑名单遮住。
- **PRD 阶段 2 尚未验收关闭**：`MODEL_REFERENCE` 绕行分支仍会跳过受众与授权
  检查。冒烟使用已授权模板只是不再踩缝，不是关缝。
- 工作树含大量未提交改动与新迁移；不要为了门禁变绿擅自暂存或覆盖用户改动。

---

# 2026-08-09 评审修复交接:F-12/F-4 颜色维已可操作,`DELIVERY_STAGE` 仍是 4

> 当前结论以 `docs/DECISIONS.md` §3.64 与 `docs/STATUS.md` 顶部为准。
> 下方 batch29/30 内容保留为历史交接,其中“真库未跑/Playwright 未跑”已被本轮
> 验证结果取代,不要再当作当前状态。

## 本轮修改

- `/products/{id}/flow?color=...` 由后端校验颜色属于同一 SPU,并确定性解析到
  对应 SKU;响应同时返回选中颜色和写入目标,前端不自行推测映射。
- 向导增加颜色选择器并把选择写进 URL。文案、属性、素材、图片集切到选中色;
  生成方案与素材上传也默认使用该颜色作用域。
- 未显式选色时使用后端 `wizard.focus_color`;选中色没有 SKU 时明确阻断,
  不回落到入口 SKU 或其他颜色。
- F-17 的 `0052_generation_plan_draft_slot`、可编辑 DRAFT 与并发收口仍保留;
  详见 §3.63。本轮颜色维不新增迁移。

## 已验证

```
真实 PostgreSQL 全量 pytest          3109/3109
纯逻辑执行器                       2728/2728
mutation anchors                   553/553
```

前端 typecheck、lint(0 error/4 条既有 warning)、Vitest 97/97、build、
syntax-check 93/93 与 Playwright 3/3 均通过。Ruff、样例数据 10/10、导入解析、
源码守卫、文档引用与列写入审计也全部通过。

新迁移与移动后的测试文件如果尚未 `git add`,
`verify_delivery.py` 会只因“存在未跟踪源码”报 16/17;这是 Git 索引状态,
不是代码或测试失败。不要为了让门禁变绿而擅自代用户暂存文件。

## 当前边界

非 ACTIVE 颜色在选择器中禁用;显式 URL 仍可定位该颜色,具体写动作没有新增权限,
继续由既有服务端动作闸校验。其余评审项不借本轮宣称完成,`DELIVERY_STAGE`
没有从 4 上调。

---

# A45-batch29/30 交接:阶段 6 五批全部落码,而 `DELIVERY_STAGE` 仍是 4

> 验收依据:PRD §13 阶段 6、§14.1「阶段 6 仓内执行版」的 AC-01/05/14~17。
> 完整决定见 `docs/DECISIONS.md` §3.59(6-4)与 §3.60(6-5);
> 当前状态见 `docs/STATUS.md` 顶部;分批、AC-05 的归属与**仍欠的四件事**
> 见 `docs/REVIEW-STAGE6-CONCLUSION.md` §六 / §七。

## 先读这三条

### 1. 落码完毕 ≠ 验收通过,也 ≠ 标记可以推进

阶段 6 的五批(batch25 / 26+27 / 28 / 29 / 30)到本批全部落码。
`DELIVERY_STAGE` 仍是 **4**,理由与阶段 5 那次一字不差:推到 5 会让
11 条列写入欠账 + 3 条欠账守卫当场逾期变红,而那 14 条都不是阶段 5 或 6
的交付项。把标记推上去再回来改还款日,是 `STATUS.md` 顶部那段注释
明令禁止的做法。

而且阶段 6 本身还欠着四件(结论文件 §七),最要紧的两件:
**浏览器一次都没实测**(Playwright 在任务 24),**真库一次都没跑**
(本轮按仓库约定只跑纯测试与离线子集)。

### 2. 有两处运营看得见的数字变了

```
没配生成方案的商品   阻断数 0 -> 1     那条阻断本来就在,只是从没被数进去
HTTP 生成文案        变严             从"有任意一个已确认属性"变成"属性步 DONE"
```

第一条是修一个缺陷带出来的(见下);第二条是 AC-05 要求的收紧 ——
服务端拒绝的判据必须与向导上按钮亮不亮的判据完全一致。
**批次执行不受影响**,它走 service 层,有自己的跳过判定。

### 3. 开工第一件事撞上的那个缺陷,值得单独看一眼

batch27 增维时把 `SETUP` / `PLAN` 两步的 `Issue` 传进了 `StepResult` 的
**第三个位置参数**(那是 `summary`,`issues` 是第四个)。三个后果同时发生,
两批之内没有任何测试看见:

```
界面     出参里 summary 是对象数组,而前端类型写的是 string ——
         React 渲染它会抛错,归属未挂或没配方案的商品**总览标签页整块打不开**
阻断数   blocking_count 数 result.issues,那两步的 BLOCKING 一条都没进去
判定     所有按 issue 取因由的下游(包括本批的 gate_reason)全部落空
```

没红的原因很干净:**没有任何测试读那两步的 `summary`**,而
`SETUP_*` / `PLAN_*` 五个问题码全仓零引用 —— 算出来从来没有人读过。
修法三件:改位置参数、`StepResult.__post_init__` 在构造期拒绝非字符串的
summary、`_evaluate_plan` 的"等上游"分支按本层规矩收掉那条 issue。

## 这两批做了什么

```
6-4 / batch29  app/workbench/wizard.py          七步判定 + AC-05 动作闸(零依赖)
               api/workbench 的 wizard 块       挂在详情端点上,**不新开端点**
               src/pages/WizardPage.tsx         /wizard/:id,?step= / ?color= 承担刷新恢复
6-5 / batch30  app/workflows/cost_estimate.py   费用预估判定(未配价 = None,不是 0)
               app/workbench/cost_rollup.py     取数:ColorView -> Demand(与颜色维同一次读)
               app/workbench/impact.py          AC-17,逐格取自 stale_matrix
               GET /workbench/change-sources    变更源清单
               GET /products/{id}/impact        这次修改会让什么失效(只读)
```

### 向导复用详情页那七个面板,不另写一套

另写一套的表现是同一件事在两个页面上有两种做法,而运营会在其中一个页面上
找不到某个按钮。向导的增量是**编排**:按 `STEP_ORDER` 排成一条路,并把
"这一步现在能不能做、不能是谁挡着"说出来。

唯一把人送出向导的是建档步 —— 那是 AC-01 允许的高级异常:归属要人**决定**
这个 SKU 属于哪个颜色,系统按 `spu` 字符串码反查是 §3.39 明令禁掉的形状。

### 费用预估:两处"不许给 0"

未配价 -> 金额 `None`、不进总额、界面显示「未配价」;算不出数量(没配方案、
没有图片集的颜色)-> 单独进 `unknown` 并让 `is_complete` 变假。
默认 Mock 一个都不在价目表里,所以开发环境会说"1 项未配价" —— 那是对的。
「预计 ¥0」是一句有具体数值的假话。

### 影响提示不重算,而且"不受影响"也显示

`stale_matrix` 那 32 格逐格比对(effect 与 mechanism 都钉着)。四列全给、
`affected` 是字段不是筛选:只列受影响的,清单里没有文案就有两种可能
(不受影响 / 这张表漏了它),而后者本仓真的发生过。

**「字段」那一维没有** —— 矩阵是 (变更源 × 对象) 的,事前给不出字段。
事后那份在 `/draft/stale-reason`(BE-205)。这条如实记在已知限制里。

## 跑过的 / 没跑的

```
跑过   纯逻辑 2710/2710   交付 17/17   样例数据 10/10   Ruff 全绿   架构契约 3/3
       前端 typecheck / lint(0 错 4 warning,与上批同)/ Vitest 95/95 /
       build / syntax-check 全绿
没跑   真库 pytest、Alembic 真库升降级、Redis / Celery 集成、Playwright
```

没跑的那一行**不是"应该没问题"**:阶段 6 五批全部是"判定 + 接线",
而本仓这一族缺陷(§3.43)十五次里有十四次是接线断在最后一跳 ——
最后一跳恰恰是纯测试与 AST 守卫最看不见的地方。要复跑,见
`docs/STATUS.md`「怎么验证」。

---

# (上一版)A45-batch21/22/23 交接:阶段 5 五项交付全部落码,而 `DELIVERY_STAGE` 仍是 4

> 验收依据:PRD §13 阶段 5、§14.1 的 AC-10/11/18/19。
> 完整决定见 `docs/DECISIONS.md` §3.53,当前状态见 `docs/STATUS.md` 顶部,
> 五项与批次的对应、AC 证据、仍欠的账见 `docs/REVIEW-STAGE5-5-1-CONCLUSION.md`。

## 先读这一条:落码完毕 ≠ 标记可以推进

PRD §13 阶段 5 的五项交付到本批全部落码(batch19~23)。**而
`DELIVERY_STAGE` 仍然是 4**,理由和前几批完全不同:

    前几批   交付项没落完
    这一批   落完了,而推到 5 会让 11 条列写入欠账 + 3 条欠账守卫
             **当场逾期变红** —— `还款日:阶段 N` 读作「推进到 N 之前要还清」

那 14 条**都不是阶段 5 的交付项**:识别侧 token 计量四列、原始响应留存四列、
几个缺入口的 UI 字段,当初被随手写上"阶段 5"这个还款日。

把标记推上去再回来改还款日,是 `STATUS.md` 顶部那段注释明令禁止的做法。
所以这里留着数字 4 和这段解释,而不是留一个好看的 5。

## 三个批次做了什么

```
5-3 / batch21  文案幂等单元(迁移 0050 + workflows/copy_idempotency.py)
5-5 / batch22  导出预览的颜色→SKU→图片(listings/export_preview.py)
5-4 / batch23  颜色投影与确认流(attributes/colour_projection.py)
```

### 5-3:缺口只长在**单件入口**上

`listing_copies` 一直是 SPU 粒度的,而入口是 SKU 粒度的。S/M/L 三行各点一次
「生成文案」= 三次真实 LLM 调用、三个版本,输入完全相同。批量那条路早就按
SPU 去重了 —— 这类"批量对了、单件没对"的不对称值得记住:批量因为要处理
50 件天然会被设计成幂等的,单件看起来"就一次点击",于是没人给它加。

两个决定:`REJECTED` 不占幂等槽位(否则这个 SPU 的文案永远修不好,现象是
"点了没反应"),因此 0050 **不加唯一索引**;存量 NULL 判「重新生成」而不是
「复用」—— 与 0049 那两列方向相反,**默认值按代价选,不按对称选**。

### 5-5:预览的正确性不在"算得准",在"算不了"

`image_preview()` 的签名里只有落库的那份映射,拿不到图片集与颜色表 ——
想重新推断也无从下手。重新推断的预览看起来更准,而它和导出不再是一回事:
上游变过之后,预览显示红色有主图、导出那一行是空的,**两边都不报错**。

### 5-4:一笔账靠"一个不存在的字段名"躲了几批

`ColorVariant.display_name` 的列注释写着触发条件是「`standard_color_name`
被确认时」,而**全仓没有这个字段**(注册表里是 `primary_color`);
另一句「要等 owner_id 切 UUID」在迁移 0046 就不成立了。
照那句话去接线的人会先找那个字段、找不到,然后得出"还缺前置"的结论。

守卫因此**去注册表里查那个字段**,而不只是把名字定死在常量里。

## 两条方法论,下一批会再遇到

1. **读源码的守卫,第一步剥注释。** 本批三次被自己的 docstring 咬红
   (第七、八、九次)。这不是疏忽,是这个仓库注释密度高的必然副作用。
2. **变异验不红时,先问那个缺陷存不存在。** batch23 的 B3 怎么写都不红,
   因为 SQLAlchemy 本来就会折叠同值赋值 —— 处理方式是删掉那条变异并把
   `needs_update` 的理由改对,不是把断言改松让数字好看。

## 复验(2026-08-08,容器内 PostgreSQL 16)

```
全量 pytest(真库)  2952 passed, 0 failed, 0 skipped
纯逻辑             2620/2620,0 跳过
迁移               0050 → 0049 → 0048 → 0050 空库演练通过,单 head
变异               batch21 13/13  batch22 10/10  batch23 9/9(batch19/20 复验 13/13、15/15)
锚点               541/541(30 份脚本)  源码守卫 584  列写入 541  交付 17/17
Ruff 全绿          架构契约 3/3        样例 10/10
前端 typecheck / Vitest 74/74 / build / syntax 89/89 全绿,lint 0 错 / 4 条既有 warning
```

**仍未执行**:Docker build、Playwright、真实平台环境。

**AC-10/11/18/19 四条现在都有真库自动化证据,但一条都不能标完整通过。**
「自动化验过」不等于「真环境验收过」。完整通过仍是 **0/22**,阶段 P0 未关闭。

## 下一步

两条独立的线:

    收标记   清掉那 14 条到期账(它们属于阶段 3 的计量接线与零散 UI 字段),
             清完 `DELIVERY_STAGE` 才能推到 5
    收验收   上线演练(结论文件第六节)+ 真环境 AC 验收。存量草稿在颜色轴
             不可证明,这期间 READY / 导出对它们**禁行**,需要提前公告

---

# A45-batch20 交接:阶段 5 批次 5-2B —— 颜色维接线,以及第一份真库证据

> 验收依据:`docs/REVIEW.md` §4.10 / §6.7、PRD §14.1 的 AC-18 / AC-19。
> 完整决定见 `docs/DECISIONS.md` §3.52,当前状态见 `docs/STATUS.md` 顶部,
> 阶段 5 的排期与欠账认领见 `docs/REVIEW-STAGE5-5-1-CONCLUSION.md`。

## 先读这一条:本批开工时门禁是红的,而红的原因是一份不存在的文件

`docs/REVIEW-STAGE5-5-1-CONCLUSION.md` 被三处**活文档**引用(PRD §14.1、
`STATUS.md` 顶部、`DECISIONS.md` §3.51),而它一次都没有被提交。
`make check-offline` 的第 14 道门禁正红在这一条上。

这不是"文档没写全"。三处都在说「详细排期见这里」,于是读到那句话的人
**不会再去别处找**,而那里什么都没有。补上它是本批做的第一件事。

## 做了什么

5-1 交付的是两列 + 迁移 + 零依赖判定层,`app/` 下零调用。本批把它接上:

```
app/workbench/upstream_collect.py   新。取数层:库行 -> ColorUpstream 投影
app/workbench/service.py            build_draft 写两列;refresh_draft 加颜色轴;
                                    READY 门禁走 ready_problems() 合取入口
app/listings/image_set_service.py   _to_view -> to_view(公开)
```

欠账两笔一起还:`audit_column_writers.LEDGER` 的两条列写入删除,
5-1 的两条欠账守卫**翻转成正向**(不是删除 —— 要防的事换了方向但没消失)。

## 本批最要紧的不是接线,是那 9 条真库用例

开工时数过一个数:**全仓零条测试调用过 `build_draft`。**
`grep -rn build_draft tests/` 只命中 5-1 那份纯守卫里的文本断言。

意味着接线做完之后整套真库用例照样全绿 —— 它们一条都不走这条路。
`tests/test_a45_batch20_draft_color_axis_db.py` 是这条链路的第一份执行证据,
覆盖:两列真的落进 JSONB、新增 ACTIVE 颜色让草稿过期(`source_fingerprint`
**算不出**这件事)、PLANNED 颜色怎么折腾都不过期、停用说"停用"不说"删除"、
存量 NULL 判不可证明、READY 两侧各自阻断、单色 SPU 与无外键存量行不被误伤。

## 三个决定,改之前先读

1. **图片集轴从「被比较」搬到「记下但不比」。** 图片集按 SPU 批准,
   每个 ACTIVE 颜色引用同一版,两边都比会让重批一次出 N+1 条重复提示。
   值仍落进快照(AC-19 要可解释的版本引用),比较只由 `diff_components` 做。
   `color_image_set` 从 `COMPONENTS` 与前端标签表一起删除,集合相等守卫钉着。
2. **颜色维问题是 `level="error"`,不是 warning。**
   `image_set_service.variant_coverage` 的旧文档字符串说硬阻断"不是修复,是停产"——
   **那句话今天不成立了**(批准门禁早就在传真实 variant_ids),本批一并改掉。
3. **没有归属外键的存量行给空颜色轴,不兜底。** 按 `spu` 字符串码反查 SPU 行
   是 §3.39 禁掉的形状,反查出来的是另一个款的颜色集。

## 复验(2026-08-08,容器内装得上 PostgreSQL 16)

```
全量 pytest(真库)  2891 passed, 0 failed, 0 skipped
纯逻辑             2582/2582,0 跳过        迁移 0049 → 0048 → 0049 空库演练通过
本批变异             15/15(第一轮 11/15)  batch19 变异 13/13 复验
锚点             509/509(27 份脚本)      源码守卫 568   列写入 540   交付 17/17
Ruff 全绿          架构契约 3/3            样例 10/10    imports 439
前端 typecheck / Vitest 74/74 / build / syntax 89/89 全绿,lint 0 错 / 4 条既有 warning
```

**仍未执行**:Docker build、Playwright、真实平台环境。
**AC-01～AC-22 仍无一条在真环境验收过,阶段 P0 未关闭。真库 ≠ 真环境。**

## 下一步

5-3(文案幂等键 + 入口改 SPU 粒度)。5-2A 剩下的只有存量行 `spu_id` 回填,
已按上面第 3 条如实呈现而非兜底;`variant_key` 那一半在 14-28 就退役了,
复核当时把它列成前置是按旧事实排的。

上线前要按结论文件第六节做分批重生成演练:存量草稿在颜色轴不可证明,
这期间 READY / 导出对它们**禁行**,那是预期行为,需要提前公告。

---

# A45-batch17-2 补丁审核:发布落库 fencing 合入,并补正来包的三处盲点

> 审核输入:`patch/A45-batch17-2-review-fixes.patch` 与同目录交接。
> 验收依据:`docs/REVIEW.md` §4.1 D / §7.4 / §7.8、根硬规则 4。
> 完整决定见 `docs/DECISIONS.md` §3.47,当前状态见 `docs/STATUS.md` 顶部。

来包的主修复成立:发布 Outbox 新增 `lease_token`(迁移 0047),领取时换令牌,
续租与结果落库认同一把令牌,终态和人工重投吊销。迟到调用失去执行权后只写
审计,不再把较新的 DONE / SUCCEEDED / LISTED 覆盖成未知状态。

合入前补了三处来包自身没有守住的问题:

- SQLAlchemy 会把 `lease_token == None` 编译成 `IS NULL`;原实现会让无令牌
  调用命中存量 NULL 行。续租与落库现在都先显式拒绝空令牌,新增 1 条纯守卫
  与 M10/M11 两个反向变异。
- 时钟台账守卫用 Windows 反斜杠拼路径时会把 `core\\clock.py` 误判为入口外文件;
  改为 `Path.as_posix()` 后跨平台一致。
- 真库并发用例的 teardown 写入审计用 outbox ID,却拿 listing/attempt ID 删除;
  现单独记录 outbox ID,避免测试向共享测试库遗留审计行。

补丁的 5 个新增文件原来没有标准 Git new-file 头,`git apply` 会报不存在;
已只修元数据、不改来包内容语义后完成合入。变异工具也补齐 Windows 临时目录与
缓存排除,本机 11/11 变异全部验红。

本机复验(2026-08-08):后端纯测试 2506/2506；Ruff 全绿；架构契约 3/3；
交付 16/16；样例 10/10；imports 428；锚点 467/467；源码守卫 547；
列写入审计 537。前端 typecheck、Vitest 74/74、build、syntax 88/88 全绿，
lint 0 error / 4 个既有 warning。

仍欠真实环境证据:`tests/test_publish_lease_concurrency_db.py` 因本机未配置
`TEST_DATABASE_URL` 明确 7 skip；迁移 0047 只确认静态单 head,未做真库
upgrade/downgrade。重叠投递窗口本身仍在,本批只关闭状态互踩。

---

# A45-batch17-1 补丁审核:只移植与当前基线独立成立的两项

> 来包 `patch/A45-batch17-1-offline-blind-spots.patch` 明确要求先有
> A45-batch17；当前树是 A45-batch15 / batch14-28 并线，缺少方案宿主页、
> 宿主页组件用例、batch17 变异脚本及其交接文档。完整结论见
> `docs/DECISIONS.md` §3.46，当前状态见 `docs/STATUS.md` 顶部。

本次没有把补丁整包硬套到错误基线上。已移植两项独立成立的改进：

- `oneOfParam` 自带 `narrow()`，分页回调与 URL 读取共用白名单和 fallback；
  守卫与 U14 变异钉住“用 `as` 让 tsc 闭嘴、运行期却写入非法档位”的退化。
- 失败时清空表格的页面加入空态棘轮；存量三处只许减少，新页面不能再把
  “拉不到”包装成“确实没有”。

没有合入的部分：`GenerationPlansPage` 的空态修复、其 QueryClient 测试包装器、
`mutate_batch17.py`、batch17 两份交接文档及补丁里的 §3.44。前四项依赖不存在的
前置功能；最后一项会与当前 `DECISIONS.md` 已有 §3.44 直接撞号。等宿主页真正
落地时，应重新审核原补丁对应两项，不能把本次结论写成“阶段 4 宿主页已合入”。

本机复验（2026-08-08）：纯测试 2495/2495；URL 变异 14/14 RED；前端
typecheck、Vitest 74/74、build 通过，lint 0 错/4 条既有 warning；交付 16/16、
样例 10/10、imports 424、锚点 456/456、源码守卫 542、架构契约 3/3、前端语法
88/88。`make check-offline` 在 Windows 被 Microsoft Store 的 `python3.exe`
占位符挡住，以上结果是按 Makefile 的字面顺序用仓库 `.venv` 等价逐项执行。

---

# A45-batch15-merged 交接:说缺口已关的话,以及 docs 的一次清账

> 上一份交接(14-25)的正文原样保留在下面。本批新增的四节在最前面。
> **本批不改任何业务行为** —— 交付的是四处失实陈述的订正、两道会变红的门禁、
> docs 目录删 42 份留 17 份。完整论证在 `docs/DECISIONS.md` §3.42。

## 子、先读这一条:本批修的不是「过期的文档」

四处缺陷逐条核过,共同点是**都在宣告一件没发生的事**,而且宣告方向一致:
**都说某个缺口是关着的。**

    create_product 注释   「CSV 导入从此要求 SPU 先存在」→ import_products 根本不调它
    README                「make check 不需要网络」      → check 依赖 fe-check,要 npm ci
    audit_anchors         「今天只剩 X 用 CASES 形状」    → X 已退役,全仓零个 CASES 表
    四份纯测试 docstring   「真库层在 tests/test_api_*.py」→ 四个文件树里不存在

**过期的文档让人多走弯路,这一类让人不走。** 读到「那边已经覆盖了」的人
不会再去看那边有没有东西。`README` 那条还多一层:照旧文案在没网的机器上敲
`make check`,拿到的是装依赖失败的红 —— 人会怀疑门禁坏了,而不是文档写错了。

## 丑、CSV 导入那笔债:本批**没有**关它,只保证说法与实现一致

> **当前状态（§3.65）：这笔债后来选择方案 (a) 并已关闭。** 下两段保留的是
> batch15 当时的判断，不是现行契约；现行导入要求已存在 SPU，多颜色必须给
> `variant_code`，失败行进入 `errors`，不会自动建最简 SPU。

`import_products` 在 batch15 当时仍直接 `Product(**row)`:不解析 spu 码、不抄 audience、
不过 C-03 闸,写进去的行 `spu_id` 是 NULL。`test_csv_import_creates_products` /
`test_reimport_is_idempotent` 照样绿,因为它们走的正是这条没关的路。

**没关的理由不是没时间,是它需要一个决定**(§3.41 那条分界线):

    (a) SPU 不存在的行计入 errors 跳过
    (b) 按 CSV 里的 spu 码自动建一个最简 SPU 再挂 SKU

两种对运营的可感知行为不一样,要产品侧拍板。本批做的是:改掉那句假注释,
在 `import_products` 的 docstring 里把缺口三行列清楚,并让
`tests/pure/test_a45_batch16_doc_truth.py` 盯住二者一致 ——
**做了决定的那天,那几句话得跟着改,否则守卫会红。**

## 寅、两道新门禁,以及一个被变异逼出来的数

**`backend/tools/audit_doc_refs.py`(第 14 道门禁,已接 Makefile / CI /
verify_delivery)。** 此前没有任何门禁盯"这句话指的东西还在不在"。
两档:活文档与活代码是 ERROR(拦),历史台账是 WARN(不拦)——
台账里「batch14-21 的说明写在 MERGE-…-FACTS-STALE.md」这句话在写下那天是真的,
判它错只会逼人改历史。

**`backend/tests/pure/test_a45_batch16_doc_truth.py`(5 条守卫)。**
每一条钉的都是两份真相之间的一致性,**不钉任何一份的现状** —— 按 §3.31,
钉现状的守卫会因为进步而变红,而那种红会训练人去改守卫。

被变异逼出来的那个数:两道门禁都要放行"把假话原样引出来再驳掉"的写法,
否则会逼人删掉真话。放行靠"驳斥标记在封闭窗口内"。窗口半径最初写 2 行,
**变异 M1(把假注释改回去)不响** —— 那段注释里本来就有一句
「原来写的是……那句话是错的」,它在 ±2 行内**替另一句假话作了担保**。
收到 0 行(必须同一行)之后 M1 变红。**一句驳斥只能管它自己引的那句。**

六条变异现在全红。

## 卯、docs 删了 42 份,但有 5 份是从上一轮的删除清单里撤回来的

判据是 CLAUDE.md 自己那条「过程文档不留档 —— 结论进 docs/DECISIONS.md」。
删之前逐份查了三件事:有没有门禁 `read_text` 它(一份都没有)、
有没有活代码 docstring 点名它(有 5 份)、那 5 份的结论在 DECISIONS.md 里吗
(查不到)。

**第二三条一起成立的 5 份保留** —— 删掉它们等于用一个悬空引用换掉另一个,
而那正是本批要修的缺陷:

    MERGE-A45-BATCH14-20-STAGE4-IMAGE-PRODUCTION.md  REVIEW-A44-BATCH7.md
    REVIEW-A45-BATCH12-2.md  REVIEW-A45-BATCH12-3.md  REVIEW-A45-BATCH14-3.md

它们要能删,得先把被点名的那一节内容搬进 DECISIONS.md 再改 docstring 指过来。
本批没做:搬的是行为验证记录,搬错了比不搬贵。

42 份的结论去处逐份列在 `DECISIONS.md` §3.42 第六节的台账里。

## 辰、PRD 两处 + 那笔还不上的债

文件名 `..._prd_v3_1.md` 与正文自报的 v3.1.1 不符,已改名 `..._prd_v3_1_1.md`。

**v3.0 原文不在仓库**,而 v3.1 用增量写法,21 处「沿用 v3.0」把 21 个章节号
变成了指向空地的引用。原文补不回来,但已在 PRD 开头加了逐条清册,
守卫钉「清册列的章节集合 == 正文里真正悬空的章节集合」——
补回 v3.0 之后逐节消化,表跟着缩短,守卫一直绿。

其中 §14.1 最贵:**AC-01~AC-20 是 §14.3 人工测试准入的判据,而判据本身拿不到。**
这与 P0 那 5 项的「未验证」是两种东西 —— P0 缺机器,补一台就能推进;
AC-01~20 缺判据,补一百台也没用。`AC-02 / AC-06 / AC-07 / AC-20` 连阶段归属
都反推不出来:§13 四条阶段验收行的并集只覆盖 16 条。

---

# A45-batch14-25 交接:14-24 那条 `GenerationCandidate.height` 是假的,以及它为什么是假的

> 上一份交接(14-24)的正文原样保留在下面。本批新增的三节在最前面。
> **本批不改业务代码,也不改审计代码** —— 交付的是对 14-24 判定证据的复核。
> 逐条在 `docs/MERGE-A45-BATCH14-25-WRITE-EVIDENCE-AUDIT.md`。

## 子、先撤回一条结论:`height` 与 `width` 从来没有不对称

14-24 说「`width` 有写入点而 `height` 没有,Provider 回传的尺寸只落了一半」。
**代码里没有这回事。** 两列写在同一行:

    app/tasks/generation_tasks.py:2176
        row.width, row.height = info.width, info.height

`height` 判错是因为扫描器只认顶层 `ast.Attribute` 目标,解构赋值整条不进判定;
`width` 判"对"了,而证据来自 `export_writer.py:237` 里 openpyxl 设 Excel 列宽
的一行 —— 与这个模型无关。**两个方向相反的缺陷,合起来造出了一个读起来
完全合理的发现。**

顺带推翻审计文档里那句「它会漏掉列,不会造出假红」。它造出了,而且因为
被写进 `LEDGER`,它不是红的,是**绿的、带理由和还款日的一条欠账**。

## 丑、另外两个缺陷,以及一笔被它们盖住的真欠账

**接收者不解类型** —— 全仓 752 处属性赋值记到了错的模型上。多数无害,
但 4 列的"有人写"判定 100% 建立在误判上。其中:

    BatchJobItem.reused    Boolean, nullable=False, default=False  → 库里恒为 False
                           读点 batch_service.py:2725 进出参
                           前端 api/batch.ts:237 有类型位
                           写点 全仓零处
                           唯一"证据":outcome.reused += 1,而 outcome 是
                           局部 dataclass _PersistOutcome

**`reused` 正是审计文档里被当作反例点名的那一列。** 那扇 `setattr` 的门
关对了,它从属性赋值那扇门走了进来 —— 不同路径、不同文件、不同语法。

**台账只自净一半** —— `AttributeCalibration.notes` 指向一列不存在的列,
永久绿。自净只查"有没有写入点",不查"列还在不在"。

## 寅、七条守卫一条都没响,原因不在判定放哪,在样本选在哪

自净守卫注入的是 `MediaAsset.sha256`,写入点是构造点关键字 ——
**落在扫描器能力范围的正中央。它验的是机制,不是覆盖面。**

台账里最可能出错的条目,恰恰是扫描器看不见其写入的那些,对自净天然免疫。
14-24 已按 §3.37 把守卫从"读源码"改成"算",改对了仍然没接住。
建议进 §3.40,并把自净守卫改成注入两个样本(构造点一个、解构赋值一个)。

**还有一条前提要改:**「今天没有一份迁移回填过列」是错的。42 份里有一份
(`0021:156`),只在 `downgrade()` 路径,不改判定 —— 但正确说法是
"没有一份 `upgrade()` 回填过列"。下一个人找回填先例时会找到它。

---

# A45-batch14-24 交接:§3.38 那条规矩现在有机械落点了

> 上一份交接(14-23)的正文原样保留在下面。本批新增的三节在最前面。

## 子、本批不改业务代码。它改的是"下一笔同类欠账要靠谁发现"

`tools/audit_column_writers.py`:每一列都要回答「谁写它」,答不出就红。
挂在 `verify_delivery`(第 16 条)与 CI 上,跑完约一秒。

做它的理由是两笔账的共同点:`color_variant_id` 躲了五批、§4.8 去重键
躲了六批,**两次都是人逐条对着 PRD §13 核才核出来的**。一条规矩如果它的
执行依赖有人记得去核,它的失效方式就是有人不记得。

首跑找出第三例:`PublishAttempt.provider_request_id` —— 列注释写着
"出事时这是唯一能和平台对账的东西",接口在返、`PublishPage` 有显示位,
而**全仓零写入点**。同批另有 22 列,逐条在
`docs/MERGE-A45-BATCH14-24-COLUMN-WRITER-AUDIT.md`。

## 丑、两条别顺手改掉的

**一、`LEDGER` 是台账,不是白名单。** 区别只有自净那一条:某一列一旦有了
写入点,它的条目就失效并被点名,必须删掉。见到"台账条目已失效"时该做的是
**删条目**,不是把那一列从写入侧改回去。

**二、覆盖面地板取的是整数(400),不是精确值 534。** 它挡的是塌方不是波动
—— 加列是常态,精确值会让每次加列都要来改这个数字,而那种数字改着改着
就没人看了(这个仓库有 19 处这样的数字冻在自己那一刻)。

**地板红的时候先查是不是某处新的动态写入把大批模型划进了"判不了",
不要调低地板。** 那正是第一版的失效形状:输出「0 列都答得出」而退出码是 0。

## 寅、这份审计看不见什么

    迁移里的回填          op.execute("UPDATE ... SET col = ...") 不算写入点
    数据库触发器          全仓没有
    Product 这个模型      product_service.py 用 **kwargs 构造,判不了

前两条今天不构成失真(0040 / 0041 / 0042 都明写不回填),真开始回填时
要在审计里补一条。第三条写在报告里而不是藏起来 —— **盲区要可见**。

以及一条不变的:**审计说一列有写入点,不等于那个写入点是对的。**
它回答的是"有没有人写",不是"写得对不对"。

---

# A45-batch14-23 交接:§6.5 两列有写入路径了,§4.8 去重键那笔账终于有人记

> 上一份交接(14-22)的正文原样保留在下面。本批新增的四节在最前面。

## 子、先读这一条:本批的分界线是"缺代码"还是"缺一个决定"

这一轮的指令是「把开发阶段里不需要人工介入的做掉」。落地时按两条线切:

    只缺有人写   →  做。今天这台机器上写完当场可验
    缺一个决定   →  不做。写得出来,但不该由写的人拍板

第二类替人做完的最大问题不是做错,是**它会以"已完成"的身份进下一轮清点**,
而没有人复核过那个决定。逐条归类在
`docs/MERGE-A45-BATCH14-23-ITEM-COLUMNS-AND-DEDUPE.md` 第五节。

## 丑、还清的那笔:两列的写入路径,以及守卫为什么断言"每一个"

14-20 记的账还清了:`listing_image_items.shared_opt_in` / `angle` 现在
有写入路径(入参 → 服务层构造点 → 归一化),欠账守卫按它自己的交代删掉,
换成正向守卫。

**正向守卫断言的是"每一个 `ListingImageItem` 构造点都写",不是"存在一个"。**
退化回去最省事的路径不是删 kwarg,是**加第二个构造点** —— 候选入集、
复制图片集、导入,任意一条新链路自己 `ListingImageItem(...)` 一份而漏掉
这两列。那时旧构造点仍然写着,"存在一个"照样绿,而漏掉的那批行在库里
与从前完全一样。

三处刻意的不对称别顺手抹平:入参角度是枚举、出参是 `str`(入严出宽,
存量值不该炸详情接口);`shared_opt_in` 入参有默认值而不是可空;
空串归一成 `None`(库里不留两种"没标注",否则 `covered_angles` 会多算一个
永远匹配不上的成员)。

## 寅、记上的那笔:§4.8 去重键 —— **它有守卫,而守卫是绿的**

PRD §4.8 要求去重键改成 `UNIQUE(spu_id, COALESCE(color_variant_id,''), sha256)`,
今天仍是 `UNIQUE(product_id, sha256)`。它躲过六批,而这次躲的方式和 14-22
那一列**不一样**,这一点比欠账本身值得记:

    14-22 那一列   没有守卫盯它
    这一条         有守卫,叫 test_dedupe_key_is_product_scoped,一直绿着

原因是那条守卫把两句写在了一起:「不许全局唯一」(永久不变量)与
「今天是 product 作用域」(欠账)。混在一起时整条按"不变量"被对待,
于是它读起来像成绩 —— 清点表把阶段 2 交付第一项记成完整落地六批。

**更贵的是第二层:它把 PRD 的待办钉成了退化路径。** 谁按 §4.8 落新键,
这条当场红;而让它变绿最省事的做法正好是把新键改回旧键,守卫的措辞
("跨商品不去重是刻意的")还会替这个动作提供理由。

本批拆成两条,欠账那一半挂还款日:阶段 5。一般化写进 §3.39:
**一条守卫同时钉不变量与现状时,它对现状的那一半是伪装成成绩的欠账。**

**别顺手还它。** `media_assets` 今天没有 `spu_id` 列,只有 `spu: String(64)`
这个反规范化字符串码。§4.8 的键要 `spu_id`,所以第一步是回答「素材挂在
SPU 上,挂的是字符串码还是真外键」—— 而那个答案连带决定 `variant_key`
退役往哪切。分开做的中间态是:一批素材按字符串码去重、另一批按外键,
**两者对"同一个 SPU"的判断可以不一致。**

## 卯、改准了一条过期的理由:方案面板不是"缺一行 import"

14-20 那条面板守卫写着"后者等有人写一行 import"。**那句话是错的**,
本批逐条核路由时核出来:面板要 `spuId`(UUID),而全前端没有任何一条路径
拿得到 SPU 的主键 —— `api/spus.ts` 不存在,`SpuGroup` 与 workbench 出参
都只有 `spu: string` 字符串码,`publish.ts` 的 `external_spu_id` 是平台侧
外部 id。**唯一在出参里给 `spu_id` 的 schema 是方案接口自己。**

所以这笔账和「三步建档 UI」是同一笔,都卡在"前端要有一个知道 SPU 主键的
宿主页"。守卫的断言没动,改的只有理由 —— §3.33:照着错理由去做的人不会
发现自己在做错事,他会发现无处可写,然后多半把 `spu` 字符串码传进 `spuId`,
那时接口 422,而错因指向后端。

## 辰、这台机器验到了什么、没验到什么

```
纯逻辑      2445/2445   0 失败,7 跳过        本批 +2(2443 → 2445)
本批变异       9/9      一次全红
锚点        436/436     21 份脚本
守卫窗口       514 个
交付         15/15
样例数据     10/10
前端语法      86/86
```

**仍未执行,与前批同:** 前端四条(无 `node_modules`)、
`alembic upgrade/downgrade`(`0037`–`0042` 从未执行)、全部 `requires_db`
(池子 264 条,近四批的 38 条一次都没跑过)、Ruff / lint-imports、
Docker build、Playwright。

**本批没有新增真库用例。** 两列的写入路径是纯层可证的(构造点写没写、
归一化做没做);"真的写进那一行了吗"要等 `alembic upgrade head` 先跑过,
而 `0041` 从未执行 —— 那是先决条件,不是本批的欠账。

**验收侧照旧:AC-01～AC-22 没有一条在真环境验收过。阶段 P0 仍未关闭
(6 项里 1 项通过、5 项因缺 PostgreSQL / Redis / node_modules / docker 未验证)。**

---

# A45-batch14-22 交接:素材颜色归属有了写入路径,样例数据换成新结构

> 上一份交接(14-21)的正文原样保留在下面。本批新增的三节在最前面。

## 子、先读这一条:上一批报的 AC-21 是虚的,本批才补实

`media_assets.color_variant_id` 落库了(0037)、被两处判定读着,而**全仓
没有任何写入路径** —— 逐条对着 PRD §13 核阶段 1-3 时才核出来,它躲过了
整整五批。

这一列恒为 NULL 时颜色维整个塌成一维:每张图都算通用图,于是「给 A 色补图」
和「补一张通用图」在指纹上完全一样。上一批刚接的 `facts_stale` 会一路绿着,
而 **AC-21 那条全称命题在真数据上永远平凡成立**。

本批补上写入链(接口 → asset_service → 影子写 → ingest,四层)。逐条说明见
`docs/MERGE-A45-BATCH14-22-COLOUR-ATTRIBUTION.md`。

**这个洞躲得掉的原因值得记一句**:它不属于任何一条守卫盯的类别 ——
接线门禁盯"函数有没有被调用",这一列不是函数;欠账守卫盯"有人写过一句话
说它欠着",而没有人写过。§3.38 把它一般化成:**一列新增的、被判定读取的列,
落库的那一批必须同时回答「谁写它」。**

## 丑、下一台有库的机器,先跑这两条

    tests/test_a45_batch14_22_colour_attribution_db.py::test_the_colour_survives_all_the_way_into_the_row
    tests/test_a45_batch14_21_facts_stale_db.py::test_adding_an_asset_for_one_colour_leaves_the_other_colours_facts_alone

第一条红了说明写入链在真环境断在某处,而纯层 13 条会全部绿着 —— 那是本批
修的洞原样回来的样子。第二条是 AC-21 本体,**它现在才第一次有真数据可算**。

跑之前先 `make seed`:样例数据换成新结构了,`spus.json` 里 `SPU-SW-101` 是
三色九 SKU,`SPU-SW-102` 是单色对照组。两个都要 —— 只有多色样本时,一个把
颜色维整个关掉的缺陷会表现成「全都不过期」。

## 寅、两条别顺手改掉的

**一、去重命中时只补空归属,不改已定的。** §5.3:改归属 A→B 会让三个指纹
同时变化,也就是三批事实同时过期。让一次"再传一次同一张图"顺手触发它,
是最难查的一类数据变更 —— 运营做的动作和看到的结果在两个页面上。

要改归属,走显式的改归属动作(它会留下 `changed_fingerprint_scopes`)。

**二、没有 `spu_id` 的商品拒绝按颜色上传。** 老建档路径建的商品给不出
"本商品所在 SPU",而 §4.3 那条「跨 SPU 同名颜色是常态」意味着光看 UUID
分不出它属于谁。放行等于允许把图挂到另一个款的颜色上 —— 那个款会突然
一批事实过期,而现象不指向"上传时选错了颜色"。

## 卯、那颗雷现在更值得当心了

`variant_key` 退役(阶段 1 剩余项)那一刻**不是一次数据迁移,是一次身份
变更**:回填 `color_variant_id` 会让已确认的颜色属性、图片标签、14-21 落的
事实指纹同时指向不存在的变体。

**本批又给它加了一类**:在此之前素材归属恒为 NULL,身份变更影响不到它;
现在素材真的挂在 `color_variant_id` 上了。那一步必须和属性 owner_id 改写
在同一个动作里做。

---

# A45-batch14-21 交接:`facts_stale` 接线,欠账还款日有了会响的门禁

> 上一份交接(14-20 并线合并)的正文从「〇、」起原样保留在下面 —— 它记录的是
> **当时**的判断,不改。本批新增的三节在最前面。

## 甲、这一批还的是上一份交接里点名的那笔欠账

上一份 §五点五 写着:「**`facts_stale` 派生零调用点** ❌ —— **AC-21 今天演示
不出来**」,并说明这是 §3.34 那条规矩的一个新失效方向:**欠账守卫的还款日
到了、还款没发生时,守卫本身不会变红。**

本批两件事一起做:接上 `facts_stale`,以及给"还款日"加一条会响的机制。

逐条说明见 `docs/MERGE-A45-BATCH14-21-FACTS-STALE.md`。**下一台有库的机器,
前三件事**在那份文档的第五节 —— 尤其是 9 条真库用例一次都没跑过。

## 乙、最要紧的一条口径,别顺手改掉

**事实的指纹不是 run 行那一列。**

0040 的 `input_fingerprint` 在 `ProductAttributeExtraction` 上,记的是「这次
run 吃进去的是哪批素材」——共享作用域那一个。0042 这一列在
`ProductAttributeValue` 上,记的是「这条事实按哪批素材立起来」。

拿前者填后者,共享事实与各颜色事实会**共用同一个哈希** —— 给颜色 A 补一张图
会 stale 掉 B 色事实,**D1 从后门原样回来**,而每一条事实都带着一个看起来
很像的 64 位指纹。欠账守卫当初逐字点名的就是这条捷径,现在它是一条反向断言。

## 丙、三条新规矩,以及一条你可能会想改回去的

**一、`<!-- DELIVERY_STAGE: N -->` 在 `docs/STATUS.md` 顶部,它是机器可读的。**
每往上加一,所有 `还款日:阶段 N ≤ 新值` 的欠账守卫会当场变红。**那是它存在的
全部意义。** 变红时该做的是还上那笔欠账,或者把还款日往后改并写清为什么 ——
不是回来把这个数字调回去。

**二、欠账守卫的声明写在 docstring 开头那一段。** 解释可以出现在任何地方,
声明不行。这条不是洁癖:第一版按全文匹配,当场把一条**已还清**的守卫和两条
**盯着门禁自己**的元守卫判成了欠账 —— 而假红最省事的消法是把门禁整条删掉。

**三、`ProductAttributeValue.input_fingerprint` 不回填,存量事实会集体判过期。**
这是刻意的,理由在 §3.37 第六节:没有人知道一条存量事实当初按哪批素材算出来。
真遇到存量时先做数据盘点,**不是回来把这一条改成"默认不过期"**。

**你可能会想改回去的那一条**:工作台上每个必填字段都会出一条
`ATTR_FACT_STALE`,看起来很吵。在 §3.1 那句"系统尚未正式使用"被真库验证之前,
吵是对的方向 —— 反方向是一批来路不明的事实带着"没变"的证明进导出。

## 丁、有一条守卫瞎了十二批,现在修好了

`test_the_migration_chain_has_exactly_one_head` 的正则只认
`revision: str = "..."`,而 0041 是裸赋值,**从来没进过链表**——当时它
**碰巧绿**。0042 挂在 0041 下面把断点移到链中间才照出来。

已加**覆盖率断言**(解析出的条数必须等于 `versions/` 下的文件数),并在
`test_a45_batch14_21_delivery_gate.py` 里加了一层元守卫钉住那句断言 ——
因为那句断言写在守卫自己身上,而守卫的断言没有人守。

**一条守卫的正确性不该取决于被它漏掉的东西恰好在哪。**

---

# A45-batch14-20 交接:两条并行线合并 —— 阶段 3 接线欠账还清,阶段 4 落码

> **给其余并行线的一句话:本包动了迁移链**(head `0039` → **`0041`**)。
> **两份迁移,不是一份。**`0040` 是识别 run 身份五列,`0041` 是生成方案表 +
> 任务四列 + 图片项两列。后者原写作 `0040`,合并时改的号。
>
> 后端动了 `models/attribute.py`、`attributes/service.py`、`extractors/*`、
> `listings/image_set_{rules,service}.py`(**有一处行为翻转**)、
> `evaluators/*`、`workbench/stale_matrix.py`、`workflows/idempotency.py`、
> `services/generation_service.py`、`tools/verify_delivery.py`。
> 前端新增方案面板与 API 客户端,**没接进任何路由,也没编译过**。

逐条说明分两份:

    docs/MERGE-A45-BATCH14-20-RUN-IDENTITY.md             阶段 3,§4.6 五列 + §9.2 幂等
    docs/MERGE-A45-BATCH14-20-STAGE4-IMAGE-PRODUCTION.md  阶段 4,§6.5 + 生成方案

## 〇、先读这一条:这个包是两批并线合的,撞了三处

两条线都自称 `A45-batch14-20`,基线都是 14-19,谁都不知道对方存在。

| 撞在哪 | 表现 | 处理 |
|---|---|---|
| **迁移号** | 两份都是 `revision = "0040"` / `down_revision = "0039"` | 阶段 4 那份改 `0041`,`down_revision` 指向 `0040` |
| `tools/mutate_batch14_20.py` | 同名不同内容(20 条 vs 41 条) | 拆成 `_run_identity.py` / `_stage4.py` |
| `docs/DECISIONS.md` | 两份 §3.34 | 阶段 4 那节改编为 §3.35 |

**迁移撞号那条是门禁抓到的,不是人看出来的。**它不是"两个 head" ——
alembic 在**加载期**就报 `Multiple revisions with the same identifier`,
整条链一步都跑不了。`verify_delivery.py` 那条「迁移链单一 head」里恰好有一句
revision 唯一性检查(A24 那轮 `0021` 撞号返工换来的),合并时它当场变红。

**三处撞车里,只有这一处有门禁** —— 而它有,是因为有人为它付过一次代价。
另外两处(同名变异脚本、两份 §3.34)当时没有任何东西会响:同名文件是
后写的覆盖先写的,同号决策则永远不会炸,它只是让「§3.34 说了什么」从此有
两个都说得通的答案。本次给决策编号补了一条门禁(交付项 14 条,新增
「决策日志编号不重复」);同名文件那一条没补,理由写在 §3.36。

两份动的是互不相交的表(`attribute_extractions` vs `generation_plans` /
`generation_tasks` / `listing_image_items`),所以合并方式是**串起来**,
不是合表。

## 一、下一台有库的机器,前三件事

```
alembic upgrade head                                          # 0040 + 0041
pytest tests/test_a45_batch14_20_run_identity_db.py -v        # 11 条,没跑过
pytest tests/test_a45_batch14_20_stage4_db.py -v              #  9 条,没跑过
```

**这两条先跑:**

`test_the_partial_unique_index_lets_a_failed_run_be_retried`(0040)。
它验的是那个「部分」:谓词写丢了(退化成全表唯一)的表现是**一次识别失败之后
同样的输入再也建不出第二个 run** —— 输入没变、模型没变、字段没变,而那正是
重试的定义。运营看到的是一个再也识别不了的商品,唯一的解法是去改点什么来
骗过约束。

`uq_generation_plans_scope` 那条(0041)。它是**表达式唯一索引**
(`COALESCE(color_variant_id::text,'')` + `WHERE status <> 'ARCHIVED'`)。
写成 `UniqueConstraint` 会因为 NULL 互不相等而挡不住第二份 SPU 默认方案,
那时 `resolve_plan()` 每次按查询顺序挑一份,同一个 SPU 两次创建任务用了不同的
参数。**纯层守卫看不见数据库索引。**

它红的时候**先看是不是索引写法的问题,不要去改模型让它变绿** ——
让它变绿最省事的做法正好是把 COALESCE 删掉,而那正是这条索引要防的东西。

跑完手工确认一次三方一致(ORM 声明 / 迁移里的冻结字面量 / 真建出来的索引):

```sql
SELECT pg_get_indexdef(indexrelid) FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
 WHERE c.relname IN ('uq_attr_extractions_idempotency_key',
                     'uq_generation_plans_scope');
```

三者之间任意两个漂移都不报错,只是让「这个键被占了吗」在不同环境下有不同
答案,而只有其中一边会多付钱。

## 二、有一处既有行为被翻转了

`image_set_rules.primary_for()` 原来在颜色没有专属主图时**回落到 SPU 通用图**。
§6.5 把 BLOCK-02 挂了几版才等到的那个业务决定定死了:

> 不得回退使用其他颜色的图片,缺图就是缺图(BLOCKED)。

回落是"看起来更友好"的那一侧,它的具体后果是**红色 SKU 挂着黑色主图上架**
—— 因为颜色绑定入口上线之前,通用图就是第一个颜色的图。

同一条决定让 `variant_coverage` 从**诊断**变成**门禁**:原来那句"只要集里存在
通用图,所有变体都算被覆盖"没有了。三条老守卫因此翻转,翻转后的断言由变异
R1 / R2 重新咬一遍。

BLOCK-02 等的从来不是 schema —— `ListingImageItem.variant_id` 与 COALESCE
唯一约束**从 0013 起就在库里**。缺的是"通用图与颜色图混排以谁为准"这一句话。

## 三、三条接线欠账,各有一条点名守卫记账

阶段 4 的五项交付里有三项是**半截**。三条都有守卫,**接线那天它们会红,
那是还款日**:

```
test_the_plan_panel_is_written_but_not_reachable_yet_and_this_is_the_ledger
test_the_two_new_item_columns_have_no_writer_yet_and_this_is_the_ledger
_variant_sample_fingerprint() 的 docstring(共享作用域那一半)
```

### 第二条最要紧:它的失效方式是"报错报在别的地方"

`listing_image_items` 的 `shared_opt_in` / `angle` 两列本批落库,`_to_view`
读它们,§6.5 的四条规则用它们判定。**但 `create_set` 没有写过它们** ——
全树没有任何写入路径。

后果不是"少了个功能":

    shared_opt_in 恒 False   每张通用图恒定命中 UNMARKED_SHARED_IMAGE
    angle 恒 NULL            某个颜色一配上方案,那个颜色的必要角度
                            **永远覆盖不了**,图片集再也批不过

第二行尤其安静:门禁不报"少了写入路径",它报的是**「缺正面图」**。
运营会去补图,补多少张都没用,因为新图的 `angle` 同样是 NULL。

阶段 4 原稿把这件事记在「验不到什么」里,措辞是"接线点在服务层,而服务层要
sqlalchemy"。**合并复审时改了**:在 `create_set` 里多写两个 kwarg 不需要
**运行** sqlalchemy,只需要有人写。缺的不是环境,是代码 —— 混成一句话的后果
是下一个人以为"等机器就行"。

### 第三条:一句理由在合并当天变质了

阶段 4 原稿写的是「共享作用域那一半接不了,因为 §4.6 的 `input_fingerprint`
**那批列还不存在**」。

**合并之后这句话不成立了** —— 阶段 3 那批刚刚把 `input_fingerprint` 落了。
但结论没变,变的是理由:那一列落在 `ProductAttributeExtraction`,也就是
**识别 run 行**;而 `facts_stale()` 问的是"这条事实还成不成立",
`ProductAttributeValue` 上没有这一列。一次 run 可以产出多条事实,一条事实
也可以跨多次 run 存活 —— **"一条事实继承哪一次 run 的指纹"是个还没做的决定,
不是一次赋值。**

按 §3.33 的规矩改了措辞:**一条过期的理由比没有理由更糟**。下一个人照着
"列不存在"去查,会发现列就在那儿,然后顺手把它接上 —— 接到的是 run 行。

硬接的表现没变:`facts_stale(stored=None)` 恒为 True,**全库事实一次性集体
过期**,运营看到"所有东西同时过期了"而查不出原因。

## 四、两份迁移各自的刻意选择,别顺手改掉

**0040:`status` 不回填,`server_default='FAILED'`。** 回填要把
`terminal_status_for` 重写成 SQL 的 CASE(第二个判定点),或者把它 import
进迁移 —— 后者更糟:迁移冻结在时间里,而判定会演进,三个月后在一台新库上
`alembic upgrade head` 会用**新规则**去写**旧行**,无声无息。全仓 41 份迁移
没有一份 import 过 `app.*`,这条不由这里开口子。

方向是有代价的:一次真的成功过的旧 run 会显示成失败。选它是因为反方向更贵
—— 默认 COMPLETED 会让一次从来没有被判定过的 run 以「算数」的身份参与事实
合并与占键两件要花钱的事。

**0041:也不回填,而且"顺手回填一个默认方案"是错的。** 那会给每个 SPU 造出
一份没人配过的方案,它的指纹会立刻进幂等键,于是第一次真正配方案时反而命中
旧任务。

**0041:`idempotency_key` 从 VARCHAR(128) 扩到 256。** 方案指纹要进键,而
`build_idempotency_key` 对显式键做的是 `[:128]` 截断 —— 截断之后两份不同的
输入可能得到同一个键,那是"幂等"这件事最坏的失效方式。列先扩,截断长度在
`workflows/idempotency.py` 一并放宽。

## 五、幂等今天覆盖不到哪几条路

建不出键就留空,**不编一个**:

| 情况 | 为什么不凑 |
|---|---|
| 增量识别(`only_media_ids`) | `canonical_scope` 只有共享/指定颜色/全部三种形状,「任意素材子集」不是其一。硬塞成 ALL 会让 `requested_scope` 说一句不真的话,而它是键的一部分 |
| 商品没有 `spu_id` | 拿 `product_id` 顶上会让两个不同 SPU 的同型请求算出同一个键 —— 第二个商品填上第一个商品的属性,接口 200 |
| 抽取器报不出调用前版本 | 取响应里的版本 = 付过钱才算得出键,而键要挡的正是付钱前那两下 |

**第二条今天覆盖面很大**:老建档路径(`create_product`、CSV 导入)还不写
`products.spu_id`,那是阶段 1 的剩余项。在它落地之前,只有走 `POST /spus`
三步建档链路建出来的商品拿得到幂等保护。

这个方向是刻意的:少挡一次的代价是一次重复付费,挡错一次的代价是一个再也
识别不了的商品。

## 五点五、阶段 1-3 还没做完,别把"合并干净"当成"前面都好了"

合并复审逐条对着 PRD §13 核了一遍阶段 1-3 的 18 项交付,**11 项完整、
7 项没完成**,而其中**只有 1 项真正卡在人或环境上**(异步化要 Redis + worker
才验得到)。其余全是纯代码欠账,今天这台机器上就能写:

| 欠账 | 后端 | 前端 | 影响哪条验收 |
|---|---|---|---|
| 老建档路径不写 `products.spu_id` | ❌ | — | 幂等保护今天只覆盖 `POST /spus` 建出来的商品 |
| `owner_id` 切 UUID + `variant_key` 退役(仍 41 处) | ❌ | — | 阶段 1 守卫「不存在按字符串推断归属的接口」 |
| 三步建档 UI | ✅ 端点齐了 | ❌ 只有只读列表 | 「不填视觉属性即可建档」 |
| 样例数据 / Fixture 重置为新结构 | ❌ | — | 「可构造三颜色九 SKU 的 SPU」——今天的 `products.csv` 是老平表,10 件单 SKU |
| `media_assets` 新去重键 | ❌ | — | §4.8 要 `UNIQUE(spu_id, COALESCE(color_variant_id,''), sha256)`,今天还是 `(product_id, sha256)` |
| `evidence_class` 存储列 + 库级 CHECK | 派生已单点 ✅ | — | D3 的第三道锁;今天靠派生单点顶着 |
| 按颜色上传 UI | ✅ 完整度检查在 | ❌ | AC-03 / AC-04 |
| ~~**`facts_stale` 派生零调用点**~~ | ✅ 14-21 | — | AC-21 **算得出来了**,但没在真库上验过 |
| 识别 run 异步化 + cancel + QUEUED | ❌ | ❌ | 唯一一条真的要环境 |

最后两条值得单独看一眼,理由在下一节和 §六。

### `facts_stale` 那条:上一版 STATUS 把阶段 3 记成了 5/6,实际是 4/6

阶段 3 交付第五项原文是「双作用域指纹计算**与 `facts_stale` 派生**」。
计算那一半接了;**派生那一半全仓零调用点** —— `facts_stale` 与
`changed_scopes` 只出现在 docstring 和 §8.1 矩阵的机制串里。

系统因此**回答不了"这条商品事实还成不成立"**。`flow.py` 里那几个 `stale`
是 `CopyFacts` / `DraftFacts`(文案与草稿的下游过期),不是商品事实。
**AC-21(传 A 色图不 stale B 色事实)今天不是会答错,是没有任何地方在算。**

这条早有守卫记账(`test_the_wired_half_is_registered_and_the_unwired_half_is_not`),
而**它写的还款日是"那是阶段 4 的事"** —— 要等属性值行也带上指纹列。
阶段 4 落码了,这件事没做。这是 §3.34 那条规矩的一个新失效方向:
**欠账守卫的还款日到了、还款没发生时,守卫本身不会变红** ——
它只钉住"没接线"这个事实,不钉"什么时候该接"。

## 六、验不到的两件事,别拿门禁绿灯当答案

**一、§6.5 门禁上线会不会让存量图片集集体无法批准。**
§3.1 写着"系统尚未投入使用、不考虑存量数据迁移",但那句话**从来没有在真库上
被验证过**。DB 用例里有一条专门断言这个前提(库里没有绑定颜色的已批准图片集)。
**它红了说明那句话不成立,那时该做的是先做数据盘点,不是调松门禁。**

**二、索引语义类变异在这台机器上必然 GREEN。**
`Index(..., unique=True)` 改成 `UniqueConstraint(...)`、把 `postgresql_where`
删掉这一类,纯层守卫看不见。它们**刻意没有列进**两份变异脚本 ——
列一条明知抓不住的变异进去,只会让「41/41」和「20/20」变成一句谎话。

## 七、门禁(合并后重跑,不是两批数字相加)

```
纯逻辑         2394/2394   0 失败,7 跳过(缺 pydantic / sqlalchemy)
阶段 4 变异       41/41     一次全红
阶段 3 变异       20/20     一次全红
锚点           372/372     18 份脚本
守卫窗口审计      495 个    反向断言都吃着封闭窗口
交付            14/14      含「迁移链单一 head」(已认 0041)+ 本次新增「决策日志编号不重复」
样例数据          5/5
导入            400 个文件
```

**仍未执行:** 前端四条(tsc / ESLint / Vitest / build,无 node_modules)、
`alembic upgrade/downgrade`(0037 / 0038 / 0039 / **0040** / **0041** 从未执行)、
**全部 `requires_db` 用例**(池子 80 条,本合并 +20)、Ruff / lint-imports 本体、
Docker build、Playwright。

**验收侧照旧:AC-01～AC-22 没有一条在真环境验收过。阶段 P0 仍未关闭。**


# 评审整改批交接:REVIEW II.8 / III.2 / III.6 / II.1(2026-08-12)

这一批不是常规迭代,是**一次外部代码评审**驱动的整改。评审把缺口分两类:要接真实
FASHN / 视觉模型 / 真实渠道才能验的,和今天就能改的。本批只碰第二类,且每条都配了
能进 `make check-offline` 的验证 —— 沿用铁律:从未执行过的门禁 = 不存在的门禁。
完整论证见 `docs/DECISIONS.md` §3.75。

## 一、这一批改了什么(四处代码 + 四份文档)

    交付卫生(III.6)   删掉随包泄漏的 `.claude/settings.local.json`(内含开发机
                       Windows 绝对路径);`.gitignore` + pack.sh/pack.ps1 各加
                       `settings.local.json`(basename,任意层级都拦)
    上传/FASHN(II.8)  上传闸 20MB 与 FASHN 内联上限 10MB 的落差:四个 oversize
                       现场收敛到 `_oversize_error` 的运营可操作文案(点名 FASHN、
                       说明上传闸更宽、给修复动作);逻辑在
                       `backend/app/services/upload_validation.py::provider_inline_size_message`
    ErrorNotice(III.2) 补上「防止新增」的棘轮:`backend/tests/pure/test_error_notice_ratchet.py`
                       冻结 17 处 `<Alert…readError()>` 反模式,只减不增
    发布租约(II.1)    在 `backend/app/channels/registry.py` 的 `_TRANSPORTS` 登记点
                       写清不变量:第一个真实 transport 的客户端超时必须钳在
                       `LEASE_SECONDS`(180s)之下 —— 现在是改动点上的注释,不是守卫

## 二、下一台有库 / 有前端依赖 / 有真实 Key 的机器,该跑什么

本批**只在离线子集里验过**。下面这些没跑过,冻结交付前按 §3.75 五补齐:

```
# 前端(装 node_modules 后):III.2 的棘轮只守结构,迁移后的行为要这三条
cd frontend && npm ci && npm run typecheck && npm run test && npx playwright test

# 真库:发布并发那 7 条(II.1 的守卫对象一旦落地就靠它)
cd backend && pytest tests/test_publish_lease_concurrency_db.py -v   # 7 条,没跑过

# 真实 FASHN Key:按 docs/PROVIDER-FASHN.md §8 首验(计费头语义 / 超时重发计费 /
# 幂等键接受性)—— 这决定 II.8 的文案之外,那道限制本身在真端点上怎么表现
```

## 三、这一批没做、以及为什么(别当成漏了)

- **17 处 ErrorNotice 迁移本身没做。** 迁移是行为改动,tsc/Vitest 离线跑不了,盲改
  等于发一个没跑过的改动。本批只补棘轮止血,迁移清单在
  `frontend/tests/ratchet-error-notice.test-notes.md`,留给有前端依赖的机器。
- **II.8 没做「建任务期前置校验」。** 那需要建任务时就拿到确切源图文件列表,而这份
  列表在 worker 里按 plan 才解析出来。取舍与理由见 §3.75 二。
- **II.1 没落成守卫,只落成注释。** 真实 HTTP transport 尚不存在,没有可钳的超时常量;
  为一个还没有的东西造守卫是过早的。等 transport 落地再把它从注释升级成反向断言。

## 四、a50/a51 的交接仍缺(评审 II.5,本批未消除)

包内时间戳显示 08-12 有一批改动(a50/a51:登录限流 `login_throttle.py`、
`client_ip.py`、路由级代码分割、nginx 安全头、`celery_app.py`、`db/session.py` 等),
但此前 HANDOVER 停在 a48、STATUS 验证停在 08-09、DECISIONS 停在 §3.74。**本节补的是
评审整改这一批的交接,不是 a50/a51 的。** a50/a51 的完整交接 + 在有网络 + 真库的机器上
重跑完整门禁,仍是冻结交付前的必办项 —— 别让「改了但没有交接记录」的批次带着离线绿出门。

## 五、门禁(本批离线子集,2026-08-12)

```
纯逻辑         2824/2824   0 失败,10 跳过(缺 httpx / pydantic / sqlalchemy);本批 +11
交付卫生        18/19      唯一 FAIL 是「非 Git 工作树」—— tarball 解出的目录本就跑不了
                          那条,这正是它该有的作用(见 verify_delivery)
导入           495 个文件   app.* 全部解析得通
锚点           565/565
守卫窗口审计     660 个
文档路径引用     全绿        活文档 + 活代码指得到;台账里 27 个只提示不拦
```

**仍未执行:** 前端 tsc / ESLint / Vitest / build(无 node_modules)、全部真库用例、
Ruff / lint-imports 本体、Docker build、Playwright、真实 FASHN / 视觉模型 / 真实渠道。

---

# 2026-08-12 a50/a51 交接:登录限流 / 客户端 IP / 路由级代码分割 / nginx 安全头 / celery_app / db/session —— 以及本次评审整改

> 决策记在 `docs/DECISIONS.md` §3.74 a49 与 §3.75 评审整改(II.8 / III.2 / III.6 / II.1)。
> 评审意见归档:`docs/REVIEW-EXTERNAL-2026-08-12.md`。本次评审整改 I / II / III 节
> 走查看的是本批修改前的工作树,本节合上 a50/a51 那一批未交付的账。

## 一、本批做了什么(代码 + 仓库卫生)

a50 与 a51 没有正式的 HANDOVER 标题。包内文件时间戳与 git log 显示 08-12
有一批 commit 信息是「收尾」的提交,改的是下列文件:

    backend/app/core/client_ip.py              新增。客户端 IP 解析(代理链支持)
    backend/app/core/login_throttle.py         新增。登录限流(进程内表)
    backend/app/tasks/celery_app.py            调整初始化顺序与会话时区
    backend/app/db/session.py                  timezone=utc 钉死在 connect_args
    backend/app/main.py                        路由级代码分割 + nginx 安全头中间件
    nginx.conf / docker nginx / 反向代理模板   安全头(CSP / X-Frame-Options / Referrer-Policy)
    .gitignore                                 加 .claude/settings.local.json
    tools/pack.sh + tools/pack.ps1             FORBIDDEN_FILES / $ForbiddenFiles 加
                                               settings.local.json
    backend/tests/pure/test_provider_inline_size_message.py
                                               II.8 收敛 FASHN 20MB vs 10MB 文案
    backend/tests/pure/test_error_notice_ratchet.py
                                               III.2 上棘轮(宽口径 24 / 窄口径 17)
    docs/DECISIONS.md §3.74 / §3.75            评审整改落档
    docs/REVIEW-EXTERNAL-2026-08-12.md         本节对应的归档(评审原话)

本轮(`2026-08-12`)的本次外审驱动整改:

    docs/REVIEW-EXTERNAL-2026-08-12.md       评审意见归档
    backend/tests/pure/test_provider_inline_size_message.py
                                              顺手修一处 `Path` 死 import(lint_offline)
    frontend/src/pages/ProductListPage.tsx   4 项筛选 + 2 项排序搬进 URL(GAP-033 末笔)
    frontend/src/pages/ReviewQueuePage.tsx    4 项筛选 + 2 项排序搬进 URL(GAP-033 末笔)

## 二、凭据移出仓库树(评审 II.5 + a49 一致)

**动作**:仓库根 `.env` 与 `.secrets/.settings.key` 移到仓库外
`$env:USERPROFILE\swimwear-imagegen-secret-backup\`(.env.bak + .secrets.bak);
**两个文件均未被 Git 跟踪**,`.gitignore` 第 3 / 4 行已含,重新创建不会被跟踪。

**影响**:
- `verify_delivery`:仓库树无凭据,**18/19 → 19/19**
- `test_environment.py::test_the_default_mock_deployment_reports_every_facet_as_simulated`:
  移走 .env 后 `EVALUATOR_BACKEND` 走模型默认 `mock`,默认部署回到全 SIMULATED
- **未做**:主密钥轮换(`SETTINGS_SECRET_KEY` 与 `.secrets/.settings.key` 仍是旧值)。
  评审 P0-4 / a49 均警告过:���钥泄露应轮换,这是**人工动作**,冻结交付前必须做

**下一步凭据恢复路径**(给接手这台机器的人):

```powershell
$bk = "$env:USERPROFILE\swimwear-imagegen-secret-backup"
Copy-Item "$bk\.env.bak" "D:\source code\swimwear-imagegen\.env" -Force
Copy-Item "$bk\.secrets.bak\.settings.key" "D:\source code\swimwear-imagegen\.secrets\.settings.key" -Force
```

## 三、本次离线复验(2026-08-12,本机 python3,无 node_modules / 无真库)

```
纯逻辑              2853/2853       0 失败
lint_offline        0 错            445 文件
verify_delivery     19/19
verify_imports      497 文件
audit_anchors       565/565         (33 份脚本)
audit_source_guards 664 守卫
audit_doc_refs      全绿            27 个只提示不拦(历史台账)
audit_column_writers 553 列          28 条台账 + 1 条模型 Product 用 **kwargs 看不到
verify_sample_data  10/10
```

## 四、真库 pytest 复验(2026-08-12,用户授权 PG/Redis 真库)

**用户授权 PG** `127.0.0.1:5432` 用户 `postgres` / 库 `swimwear_imagegen_test`
(以 `_test` 结尾,夹具可清空);**Redis** `39.97.61.13:6379` 数据库 1。
**凭据仅在跑测试时通过环境变量注入**,未写入仓库任何文件,跑完即丢。

### 4.1 通过的真库用例(全跑,**51 红 397 绿**)

| 分组 | 文件 | 结果 |
|---|---|---|
| 草稿颜色维(评审 P0-1) | `test_a45_batch20_draft_color_axis_db.py` | **9/9** |
| 发布并发(评审 P0-1) | `test_publish_lease_concurrency_db.py` | **7/7** |
| 迁移升级 / 降级 | `test_migrations.py` | **7/7** |
| 阶段 4-5 接缝 | `test_a45_batch14_19_evidence_query_db.py` / `14_20_run_identity` / `14_20_stage4` | **全绿** |
| 阶段 5-6 接缝 | `test_a45_batch18_lease_visibility_db.py` / `21 / 22 / 23 / 24 / 28 / 31` | **81/81** |
| 批次 / 接收 / 导入 / 轮询 / 发布 | `test_batch_lease_concurrency_db.py` / `_receipt_lifecycle` / `import_preview_version` / `poll_and_delist` / `publish_flow` / `publishing` / `a45_batch14_21_facts_stale_db` | **90/100** |
| API generation / reviews | `test_api_generation.py` / `test_api_reviews.py` | **31 红** |
| 方案接管 | `test_a47_plan_governs_db.py` | **0/10** |
| 重建 / 计费 | `test_a45_batch12_4_recovery_db.py` / `12_5_lease_and_billing_db.py` | 部分红 |
| **合计** | | **51 failed, 397 passed, 0 skipped** |

### 4.2 红的 51 条同源,**不是本轮引入**

a49 §3.74 那一批整改改了一处接口语义:

- `generation_service._validate_assets_used_in_generation` 在 **模特模板未传**
  时,旧版"告警后 return",改成 **拒绝创建任务**(§3.74 D-1)
- `_build_request` 里"拿不到模板图就退回自由上传模特图"的兜底被删
  (§3.74 D-1 后段)

这导致 `tests/test_api_generation.py` / `test_api_reviews.py` /
`test_a47_plan_governs_db.py` / `test_a45_batch12_4_recovery_db.py` 等
**51 条真库用例的预期行为过期了** —— 它们原本期望「自由模特图能跑出任务,
只是产生假图」,现在接口返回 `INPUT_INVALID` 422。

**这件事在仓库自陈里没有**: §3.74 / §3.75 / STATUS / HANDOVER 均未点名
"老真库用例在 a49 之后是红的"。本轮把它如实记下来作为**新发现**。

**修复方案**(本轮**未做**):
- a:用例里传 `model_template_id`(若 mock 套能给出)
- b:把断言改成「422 + INPUT_INVALID」
- c:把这批用例标记为 xfail 并写明 "a49 之前期望"

**关键判定:这 51 条红不算本轮整改引入的回归**,因为 a49 是评审整改批的产物,
本轮(2026-08-12)只是真跑了一遍 —— **从未跑过的门禁 = 不存在的门禁**。
修这些测试的债务属于 a49 之后的接力,本轮边界是「发现并如实标记」。

### 4.3 命令

**注意**:`.secrets/.settings.key` 与 `.env` 已被移出仓库树,跑测试前临时
复制到 `.secrets-test/`(详见 §二)。

## 五、仍未跑、且本轮没有消除的

| 项 | 当前状态 | 建议 |
|---|---|---|
| 前端 tsc / ESLint / Vitest / build | 本机无 node_modules | 联网机器:`cd frontend && npm ci && npm run typecheck && npm run lint && npm run test && npm run build` |
| Playwright (chromium) | 任务 24 仍未开工 | 同上 |
| 17 处 `<Alert+readError>` 迁移 | 棘轮已上,迁移未做(§3.75 三、III.2) | 等有 Vitest 的机器,按棘轮迁移 |
| FASHN 真端点首验 | 未做 | `docs/PROVIDER-FASHN.md` §八清单 |
| 评分阈值校准 | 未做 | ≥20 条人工审核样本跑 `make calibrate` |
| 发布链路接真实 HTTP transport | 未做 | 业务先定第一个平台;接入前把客户端超时钳在 `LEASE_SECONDS` 以下 |
| 主密钥轮换 | 未做(本批只移出仓库树) | 冻结交付前必须做 |
| Docker build × 2 | 本机无 Docker CLI | 真机 |

## 六、下一步建议

按本仓 §3.74 一的教训排序:

1. **复跑完整门禁**:`cd frontend && npm ci && npm run typecheck && npm run lint && npm run test && npm run build`
   + `cd frontend && npx playwright install chromium && npm run e2e` + `make test`
2. **轮换主密钥**(评审 P0-4)
3. **浏览器实测**(任务 24 / Playwright)
4. **17 处迁移 + 棘轮首跑**(等 Vitest)
5. **接第一个真实 HTTP 渠道 transport**(业务决定)

冻结交付前必办:1 + 2 + 3;4 / 5 可以排到下一个阶段。
