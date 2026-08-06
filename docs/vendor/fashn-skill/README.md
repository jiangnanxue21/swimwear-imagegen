# FASHN 官方 skill(存档)

`app/providers/fashn.py` 的**唯一实现依据**。原样存档在这里,不做修改。

存档的理由:需求第五章要求"不得凭记忆虚构第三方 API"。把依据和实现放在同一个仓库里,
将来任何人都能逐条核对代码里的字段名、状态字符串、错误代码是从哪来的 ——
而不是去猜当初那个人是不是记错了。

- `SKILL.md` —— 集成总览、鉴权、三种调用路径
- `reference.md` —— 每个端点的完整 inputs、错误目录、额度、webhook、限流
- `LICENSE` —— 随原始仓库一并保留

上游若有更新,先替换这两个文件,再跑 `python3 tools/run_pure_tests.py fashn` ——
`tests/pure/test_fashn_provider.py` 把字段名写死了,接口变化会立刻变红。
