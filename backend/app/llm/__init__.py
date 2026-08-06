"""多模态/文本模型的共用传输层(M2)。

    images.py      图片准备:SSRF、逐跳重定向、钉 IP、大小与像素上限、base64
    transport.py   HTTP 往返:重试、退避、错误归类、客户端生命周期
    redaction.py   日志脱敏:请求摘要里不出现 Key、base64、完整请求体

## 依赖方向(M2 验收判据之一)

本包**不得** import `app/evaluators`、`app/extractors`、`app/channels`。
它是被它们共用的下层,反向依赖会立刻变成循环 import,
而且意味着「传输层知道自己在为谁服务」—— 那样就没法被第二个调用方复用。

`tests/pure/test_llm_layer.py` 用 AST 静态校验这条,违反即 CI 红。
"""
