# 新增一个 Provider

## 1. 写适配器

`backend/app/providers/<name>.py`,继承 `ImageGenerationProvider`,实现五个方法:

```python
def capabilities(self) -> ProviderCapabilities: ...   # 支持哪些生成模式、一次最多几张
def is_configured(self) -> bool: ...                  # 只读配置,绝不发网络请求
async def submit(self, request) -> str: ...           # 返回外部任务 ID
async def get_status(self, external_task_id): ...
async def fetch_results(self, external_task_id): ...
```

三个类属性别忘:

| 属性 | 怎么填 |
| --- | --- |
| `is_simulator` | 真 Provider 写 `False`。**默认是 True,忘了写会让状态条说「这是模拟环境」** |
| `accepts_local_storage_paths` | 只有能直接读本机存储路径的才写 `True`(且只对 `STORAGE_BACKEND=local` 有效) |
| `trusted_result_hosts` | 结果下载域名,**精确主机名,不接受通配符** |

## 2. 注册

`backend/app/providers/registry.py`:

- 加进 `PROVIDER_FACTORIES`;
- 请求映射真的落地了才加进 `IMPLEMENTED_PROVIDERS` —— 这张表决定前端能不能选它,
  没落地就让它在创建任务时被挡下,而不是跑到 worker 才失败;
- 需要的话把它放进 `DEFAULT_PRIORITY_ORDER`。

## 3. 错误映射

把厂商的错误码翻译成 `app/providers/errors.py` 里的异常类。翻译错的代价很具体:
把「配额用尽」映射成可重试,退避重试会把这一轮的时间白白烧掉;把「结果下载失败」
映射成可换家,等于重新付一次钱去解决一个网络问题。

拿不准时**抛基类** `ProviderError` —— 它的策略是最保守的。

## 4. 配置项

`.env.example` 加一组,`app/core/config.py` 的 `Settings` 加对应字段。
`tests/pure/test_config_contract.py` 会静态校验两边一一对应。

要让它能在网页上改,见 [新增一个后台配置项](add-a-setting.md)。

## 5. 计费与用量

`usage_from_candidates()` 决定一次调用记多少个计费单位。**不许为了接口形状完整填常量** ——
这一列要能追溯到真实来源。未配价的调用单独计数、不计入金额,而不是按 0 元记账。

## 6. 验证

```bash
curl -X POST http://localhost:8000/api/providers/<name>/test    # 连通性,不花钱
make baseline SKU=SW-001-BLK-S P=mock,<name>                    # 与 Mock 基线对比,会花钱
make smoke                                                      # 整条闭环
```

基线对比脚本显式传 `override_plan=True` 绕过生成方案 —— 它要对比的是 Provider 本身,
不是方案的效果。

## 会因为你漏做而变红的

- `tests/pure/` 里的 Provider 契约与路由用例(新家没实现某个抽象方法、或没进注册表)
- `tests/pure/test_environment.py` 的「注册表接缝」(`is_simulator` 没报上来)
- `test_config_contract.py`(`.env.example` 与 `Settings` 不一致)
