# settings · 后台设置

**代码**:`backend/app/core/settings_schema.py`、`app/services/settings_service.py`、`settings_runtime.py`、`app/providers/_config.py`
**图**:[配置怎么生效](../assets/settings-layers.svg)
**取舍原文**:[`../SETTINGS.md`](../SETTINGS.md)

## 一个读入口

全系统读配置只有 `provider_setting()` 一个函数。所以只在它前面挂一层带 TTL 缓存的
数据库覆盖层,设置页就对**所有**调用点同时生效,包括另一个进程里的 Celery worker。

```
provider_setting("FASHN_API_KEY")
  → 数据库覆盖(TTL 缓存,只认声明表里的键)
  → 环境变量 / .env
  → 代码默认值
```

三条刻意的约束:

- **读失败绝不影响主流程。** 数据库连不上就退回环境变量,生成任务照跑。
- **只缓存,不监听。** 没有广播通道,worker 最多晚 TTL 秒看到新值。
  代价是一次任务可能用旧配置,收益是少一个会坏的组件。
- **只有声明表里的键能被覆盖。** 数据库里就算被塞进 `DATABASE_URL` 也不会生效。

## 字段表是唯一真相来源

`settings_schema.py` 的 `SETTING_GROUPS`:前端不认识任何一个具体配置项,
拿到分组、类型、选项就把页面画出来了。加一项只改这一处。

## 密钥:明文不出后端

写入走 Fernet 加密落库,主密钥取自 `SETTINGS_SECRET_KEY`;留空则在 `SETTINGS_KEY_DIR`
(默认项目根下 `.secrets/`)自动生成并打日志提醒 —— 单机开箱可用,
**多机部署必须显式配置**,否则各节点解不开对方写的值。

**拿不到加密能力时拒绝保存密钥**,而不是退化成明文。页面上永远只显示末位打码串。

**密钥目录独立于存储目录**:存储目录会被挂成 `/files` 静态服务,主密钥放进去等于连同
数据库一起公开(compose 里已是独立的 `secrets` 卷)。

## 不可改的那一档,有断言钉死

数据库地址、Redis、存储后端、S3 密钥、CORS、`ADMIN_TOKEN` 与 `SETTINGS_SECRET_KEY`
自身。理由是这些改错就是整站不可用,或者等于把锁和钥匙放在同一个抽屉里。
它们只能走部署。

## 评分模型与属性识别是两个独立分组

即便填的是同一个端点也要各填一遍。不做静默共享的理由:识别的置信度校准按
(字段 × 模型 × 提示词)分箱存,共享配置意味着换一次评分模型就把识别的全部校准数据
作废 —— 而运营看到的只是「今天开始所有属性都要人工确认了」,原因在另一个页面的
另一个下拉框里。

## 想把配置钉死在部署流水线上

设 `SETTINGS_ENV_LOCK=true`:凡是 `.env` 给过值的项在网页上只读,数据库覆盖也不生效。
生产环境建议这样。

每一项旁边标着值是谁给的 —— 后台设置 / 环境变量 / 默认值;后台改过的项可以一键
「恢复」,删掉覆盖即回到 `.env` 说了算的状态。

## 加一个配置项

见 [`../cookbook/add-a-setting.md`](../cookbook/add-a-setting.md)。
