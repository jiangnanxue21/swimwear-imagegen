# Cookbook

每篇一件事:**做什么、改哪几个文件、哪一道门禁会因为你漏做而变红**。

这些配方不解释设计理由 —— 理由在 [`../subsystems/README.md`](../subsystems/README.md)
的对应页面和 [`../DECISIONS.md`](../DECISIONS.md) 里。

| 配方 | 什么时候用 |
| --- | --- |
| [新增一个 Provider](add-a-provider.md) | 要接一家新的出图服务 |
| [新增一个评分模型后端](add-an-evaluator-backend.md) | 要接一家新的视觉大模型,或它的 API 形状和现有四种都不一样 |
| [新增一个品类](add-a-category.md) | 泳装之外的品类要能走完整条链路 |
| [新增一个后台配置项](add-a-setting.md) | 要让某个值能在网页上改,而不是改 `.env` 重启 |
| [新增一条日志事件](add-a-log-event.md) | 加了一段代码,希望它在运行日志页里能被筛出来 |
| [新增一道门禁](add-a-gate.md) | 写了一条守卫,要让它真的在 CI 里跑 |

## 通用收尾

不管做哪一件,收尾都是这三步:

```bash
make check-offline    # 后端离线子集,含六道审计
make fe-check         # 改了前端才需要,需联网
make audit-doc-refs   # 改了文档里的路径引用
```

以及一条不会有人提醒你的:**如果你的改动让某份文档里的一句话变成假的,改那句话**。
这个仓库里最贵的缺陷形态不是坏链接,是**一句失实的话替一段代码背书** ——
读到「那边已经钉住了」的人不会再去那边看。
