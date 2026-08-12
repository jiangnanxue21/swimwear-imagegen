# ErrorNotice 迁移棘轮:说明

REVIEW III.2 点名:全站有 **17 处** `<Alert … description={readError(…)} />` 仍直接把
error 拍平成一句话喂给 antd 的 `<Alert>`,而不是把**原始 error** 交给统一出口
`<ErrorNotice error={…}>`。后果:这些页面上管理员拿不到请求编号和技术详情,排障只能
开浏览器控制台 —— 正是 `<ErrorNotice>`(A12)想终结的动作。

审阅同时点出:**「且没有棘轮测试防止新增」**。就算这些慢慢迁,只要没有守卫,新页面
照样会再写一个,债务不降反增。

## 守卫在哪

行为验证(迁移后的页面真的展示了请求编号)属于 Vitest / Playwright,那两套在只有
python3 的机器上跑不了。所以**能在离线门禁里真跑的**棘轮压在后端纯测试里:

    backend/tests/pure/test_error_notice_ratchet.py

## 口径:两个数

    宽口径(主基线)   pages/ + components/ 里**非 toast** 的 readError 调用点,合计 24
    窄口径(锚点)     `<Alert…readError(` 这一种,合计 17(评审当时点名的那批)

棘轮的第一版只有窄口径,而它只认现存代码恰好长的那个形状 —— 写成子元素
(`<Alert>{readError(e)}</Alert>`)、属性里套 JSX、或换成 `<Result>`,它一律看不见。
一道只认旧形状的守卫对新写法全部放行,等于不守。所以主基线换成宽口径:不解析 JSX,
只问「这个 `readError` 是不是 toast」。`message.error(readError())` / `notification.*`
是正当用法(一句话的浮层本就不展开技术详情),排除在外。

宽口径是窄口径的**超集,不等于「24 处都必须迁」** —— 有些计入的点(例如把错误串进
「模特列表没拉到,下面是空的不代表没有模特」这类提示)未必值得整块 `<ErrorNotice>`。
守的是**这个数只能下降**。

## 还债

迁走一处,就把该文件在 `BROAD_BASELINE` 里的数字**下调一格**;迁到 0 就删掉该行。
`current == baseline` 是双向锁:多了拦,少了也拦(少了说明有人迁了却忘了调基线,
留着虚高的基线等于给未来的新债留缝)。窄口径 `NARROW_ALERT_TOTAL` 同理。

迁移一处长什么样:

    - {list.isError && <Alert type="error" showIcon message={readError(list.error)} />}
    + {list.isError && <ErrorNotice error={list.error} title="读不到素材库" />}

原始 error 进 `<ErrorNotice>`,请求编号 / HTTP 状态 / 技术详情就都有落点了。

## 限度

这是读源码的**结构**守卫,不是行为验证:它证明不了迁移后的页面真的渲染出了请求编号 ——
**先跑 Vitest,那才是行为**。而且任何基于文本的检测器都有边界:把 `readError` 先赋给
一个变量再渲染,它同样看不见。**它抬高门槛,不是不可绕过。**
