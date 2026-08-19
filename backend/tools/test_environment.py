"""测试进程的确定性 Provider 配置。

项目 Settings 会固定读取仓库根 ``.env``。纯测试若继承开发者本机的 FASHN Key、
ComfyUI 地址或路由模式,同一份代码会随机器配置得到不同结论。这里必须在
``app.core.config`` 首次导入前覆盖这些值;测试自己需要其它组合时再局部改写。

只管 Provider 选择,不碰数据库、Redis 或其它集成配置。
"""
from __future__ import annotations

import os


_PROVIDER_BASELINE = {
    # 空 API Key 不会触发 SETTINGS_ENV_LOCK,所以测试入口要明确关闭数据库覆盖。
    # 否则“纯”Provider 测试会连接开发者数据库,甚至读进其中的真实 Key。
    "PROVIDER_SETTINGS_ENV_ONLY": "true",
    "PROVIDER_PRICE_BOOK": "",
    "FASHN_API_KEY": "",
    "FASHN_BASE_URL": "",
    "FASHN_TRYON_MODEL": "tryon-max",
    "FASHN_GENERATION_MODE": "",
    "FASHN_RESOLUTION": "",
    "FASHN_OUTPUT_FORMAT": "png",
    "FASHN_REQUEST_TIMEOUT_SECONDS": "60",
    "FASHN_POLL_INTERVAL_SECONDS": "2",
    "FASHN_POLL_TIMEOUT_SECONDS": "300",
    "FASHN_MAX_IMAGE_MB": "10",
    "FASHN_SEND_PUBLIC_URLS": "false",
    "FAL_API_KEY": "",
    "FAL_BASE_URL": "",
    "COMFYUI_BASE_URL": "",
    "COMFYUI_CONFIG_FILE": "./comfyui/config.yaml",
    "DEFAULT_PROVIDER": "mock",
    "PROVIDER_ROUTING_MODE": "MANUAL",
}


def install_provider_test_baseline() -> None:
    """覆盖开发者配置,让纯测试从同一份未配置 Provider 状态起跑。"""
    os.environ.update(_PROVIDER_BASELINE)
