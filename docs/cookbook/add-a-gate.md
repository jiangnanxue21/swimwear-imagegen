# 新增一道门禁

## 硬规则:门禁清单不许只写在文档里

加一条门禁 = **改三个地方**:

```
Makefile                             加一个目标,并挂进 check-offline 或 check
.github/workflows/ci.yml             加一步,run: 里是那条命令的字面量
backend/tools/verify_delivery.py     check_ci_runs_every_gate() 的表里加一行
```

`verify_delivery.py` 逐条在 `ci.yml` 里找**命令字面量**(不是 step 名字)——
所以改了 Makefile 却没改 ci.yml 时,它会红。

## 守卫写在哪一层

| 层 | 什么时候用 |
| --- | --- |
| `backend/tests/pure/` | 零三方依赖的判定:契约、纯函数、AST 静态检查。**不许 `import pytest`** |
| `backend/tests/test_*.py` | 需要真 PostgreSQL 的:ORM 约束、API、迁移升降级 |
| `backend/tools/audit_*.py` | 跨文件的一致性审计(引用指得到吗、锚点还在吗、列有谁写吗) |
| `frontend/tests/` | 前端类型、组件、URL 筛选契约 |

## 两条写法纪律

**钉一致性,不钉现状。** 一条好的守卫应该同时容纳两种世界:菜单裁剪了和没裁剪都能绿,
只拒绝「代码一个样、注释另一个样」。钉现状的守卫会因为**进步**而变红,
而那种红会训练人去改守卫。

**反向断言的窗口必须封闭。** 「这一段里不许出现 X」这类断言,窗口开得越宽,
一句文件顶部的免责声明就越能把整份文件一次性豁免掉 —— 而那正是这道门禁要抓的东西。
`make audit-guards` 专门盯这一条。

## 白名单要写理由

每一条豁免旁边写一句为什么。**没有理由的白名单会长大** —— 下一个被拦住的人,
最省事的做法是把自己那条加进去。带理由的那一栏至少要求他把理由写出来,
而写不出理由的时候人会去修代码。

## 如果它需要变异验证

变异脚本(`backend/tools/mutate_*.py`)用来验证「这条守卫真的会因为对应的缺陷变红」。
它们**刻意不进 CI**(跑一次要几十份工作树、十几分钟),所以锚点会过期 ——
`make audit-anchors` 只解析不执行地盯着这一点。

> 只解析是必须的:变异脚本的循环写在模块顶层、没有 `__main__` 保护,
> import 它就是当场改写真实工作树。**审计工具把被审对象跑起来,本身就是事故。**

## 验证

```bash
make check-offline       # 你的新目标应该在里面跑起来
make verify-delivery     # 接线自检:ci.yml 里找得到那条命令
```
