"""设置页的字段声明表 —— 后台能改哪些配置,由这一份表说了算。

为什么做成声明表而不是在接口里逐个手写:

1. **前端不需要认识任何一个配置项。** 它拿到分组、字段、类型、选项就能把页面画出来,
   以后加一个配置项只改这里一处,前端不动。
2. **校验只有一个地方。** \"分辨率只能是 1k/2k/4k\"这种规则写在字段上,
   接口、脚本、测试共用同一份,不会出现界面拦住了而接口没拦住。
3. **可静态验证。** 纯测试会比对:表里的键必须都是 Settings 的真实字段,
   下拉选项必须是 Provider 层真的认识的取值 —— 拼错一个字母当场就红。

只用标准库,因此 core 层可以放心持有它。
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

#: 掩码字符。密钥回传前端时只留末尾几位,足够运营辨认\"是不是我填的那把\",又不泄露。
MASK_CHAR = "•"
MASK_TAIL = 4

#: 字段的输入控件类型。前端据此决定画输入框、数字框、开关还是下拉。
#: 命名带 TYPE_ 前缀是刻意的 —— 光写 PASSWORD 会被「禁止硬编码密钥」的扫描误判,
#: 而且它本来就是「控件类型」而不是「一个口令」。
TYPE_TEXT = "text"
TYPE_PASSWORD = "password"
TYPE_NUMBER = "number"
#: 只接受整数的数字项。和 TYPE_NUMBER 分开,是因为"轮次 2.7 轮"这种输入
#: 以前会被静默接受,然后在别处 int() 截断成 2 —— 用户填了什么、系统用了什么,
#: 两者不一致而且没有任何提示。前端也据此把 step 设成 1。
TYPE_INTEGER = "integer"
TYPE_BOOL = "bool"
TYPE_SELECT = "select"

#: 单个值的长度上限。密钥再长也到不了这个数,超了基本是粘错了东西。
MAX_VALUE_LENGTH = 1024


@dataclass(frozen=True)
class Option:
    value: str
    label: str


@dataclass(frozen=True)
class Field:
    key: str
    label: str
    type: str = TYPE_TEXT
    help: str = ""
    placeholder: str = ""
    #: 真值表示这一项是密钥:加密落库、回传打码、日志脱敏
    secret: bool = False
    options: tuple[Option, ...] = ()
    minimum: float | None = None
    maximum: float | None = None
    #: 收起在\"高级\"里,默认不展开 —— 超时、轮询这类不该干扰第一次配置
    advanced: bool = False


@dataclass(frozen=True)
class Group:
    key: str
    title: str
    description: str = ""
    #: 右上角的状态标签,例如\"待接入\"
    badge: str = ""
    fields: tuple[Field, ...] = field(default_factory=tuple)


def _yes_no() -> tuple[Option, ...]:
    return (Option("true", "开启"), Option("false", "关闭"))


SETTING_GROUPS: tuple[Group, ...] = (
    Group(
        key="general",
        title="生成通用",
        description="新建任务时的默认值。已经排队的任务不受影响,改动只作用于之后创建的任务。",
        fields=(
            Field(
                key="DEFAULT_PROVIDER",
                label="默认 Provider",
                type=TYPE_SELECT,
                help="创建任务时预选哪一家。未配置的 Provider 仍然会在提交时被挡下。",
                options=(
                    Option("mock", "Mock(本地演练,不花钱)"),
                    Option("fashn", "FASHN"),
                    Option("fal", "fal.ai"),
                    Option("comfyui", "ComfyUI"),
                ),
            ),
            Field(
                key="PROVIDER_ROUTING_MODE",
                label="路由方式",
                type=TYPE_SELECT,
                help="MANUAL 用任务里指定的那家;PRIORITY 按固定顺序挑第一个已配置且支持该模式的。",
                options=(
                    Option("MANUAL", "MANUAL(用指定的那家)"),
                    Option("PRIORITY", "PRIORITY(按顺序自动挑)"),
                ),
            ),
            Field(
                key="MAX_TOTAL_ROUNDS",
                label="单任务累计轮次上限",
                type=TYPE_INTEGER,
                help="含人工审核里追加的轮次。调高会显著增加额度消耗。",
                minimum=1,
                maximum=50,
            ),
        ),
    ),
    Group(
        key="fashn",
        title="FASHN",
        description=(
            "虚拟试穿与商品转模特图。在 app.fashn.ai/api 取 Key,"
            "只填 Key 即可,其余留空走官方默认值。"
        ),
        fields=(
            Field(
                key="FASHN_API_KEY",
                label="API Key",
                type=TYPE_PASSWORD,
                secret=True,
                placeholder="fa-...",
                help="填好后点\"测试连接\"确认额度。会产生费用。",
            ),
            Field(
                key="FASHN_TRYON_MODEL",
                label="试穿模型",
                type=TYPE_SELECT,
                help="tryon-max 质量优先,可发布;tryon-v1.6 快且便宜,输出固定 864×1296。",
                options=(
                    Option("tryon-max", "tryon-max(旗舰)"),
                    Option("tryon-v1.6", "tryon-v1.6(快且便宜)"),
                ),
            ),
            Field(
                key="FASHN_GENERATION_MODE",
                label="速度/质量档位",
                type=TYPE_SELECT,
                help=(
                    "留空交给 FASHN 自选。performance 仅 tryon-v1.6 支持,"
                    "fast 仅商品转模特图支持。"
                ),
                options=(
                    Option("", "自动"),
                    Option("performance", "performance(最快)"),
                    Option("fast", "fast(快)"),
                    Option("balanced", "balanced(均衡)"),
                    Option("quality", "quality(最好)"),
                ),
            ),
            Field(
                key="FASHN_RESOLUTION",
                label="输出分辨率",
                type=TYPE_SELECT,
                help="留空用官方默认 1k。分辨率越高消耗额度越多,tryon-v1.6 不支持此项。",
                options=(
                    Option("", "默认(1k)"),
                    Option("1k", "1k"),
                    Option("2k", "2k"),
                    Option("4k", "4k"),
                ),
            ),
            Field(
                key="FASHN_OUTPUT_FORMAT",
                label="输出格式",
                type=TYPE_SELECT,
                options=(Option("png", "PNG"), Option("jpeg", "JPEG")),
            ),
            Field(
                key="FASHN_BASE_URL",
                label="接口地址",
                placeholder="留空 = https://api.fashn.ai/v1",
                advanced=True,
            ),
            Field(
                key="FASHN_REQUEST_TIMEOUT_SECONDS",
                label="单次请求超时(秒)",
                type=TYPE_NUMBER,
                minimum=5,
                maximum=600,
                advanced=True,
            ),
            Field(
                key="FASHN_POLL_INTERVAL_SECONDS",
                label="轮询间隔(秒)",
                type=TYPE_NUMBER,
                minimum=0.5,
                maximum=60,
                advanced=True,
            ),
            Field(
                key="FASHN_POLL_TIMEOUT_SECONDS",
                label="出图等待上限(秒)",
                type=TYPE_NUMBER,
                help="一次生成通常 20-120 秒,官方 SDK 默认上限 300 秒。",
                minimum=30,
                maximum=1800,
                advanced=True,
            ),
            Field(
                key="FASHN_MAX_IMAGE_MB",
                label="内联素材大小上限(MB)",
                type=TYPE_INTEGER,
                help="超过这个大小直接拒绝,避免把巨图编码成 base64 打过去。",
                minimum=1,
                maximum=64,
                advanced=True,
            ),
            Field(
                key="FASHN_SEND_PUBLIC_URLS",
                label="改传公网 URL",
                type=TYPE_BOOL,
                help="存储换成公网可达的对象存储后开启,可省掉 base64 编码。",
                options=_yes_no(),
                advanced=True,
            ),
        ),
    ),
    Group(
        key="fal",
        title="fal.ai",
        badge="待接入",
        description="Key 可以先填,但请求映射尚未按官方文档落地,选中它提交任务会被挡下。",
        fields=(
            Field(key="FAL_API_KEY", label="API Key", type=TYPE_PASSWORD, secret=True),
            Field(key="FAL_BASE_URL", label="接口地址", advanced=True),
        ),
    ),
    Group(
        key="comfyui",
        title="ComfyUI",
        badge="待接入",
        description="自建实例。工作流 JSON 与节点 ID 走配置文件,不在这里填。",
        fields=(
            Field(
                key="COMFYUI_BASE_URL",
                label="服务地址",
                placeholder="http://comfyui:8188",
                help=(
                    "内网地址还需要加进下方\"允许下载的内网主机\","
                    "否则结果图下载会被 SSRF 校验拦下。"
                ),
            ),
            Field(
                key="COMFYUI_CONFIG_FILE",
                label="工作流配置文件",
                placeholder="./comfyui/config.yaml",
                advanced=True,
            ),
        ),
    ),
    Group(
        key="evaluator",
        title="评分模型 · 多模态",
        description=(
            "候选图的自动打分,需要能看图的模型。没有真实评分器时用 Mock,"
            "闭环照样跑通,但 Mock 按文件指纹给分,**不能用来决定图片上不上网站**。"
        ),
        fields=(
            Field(
                key="EVALUATOR_BACKEND",
                label="评分器",
                type=TYPE_SELECT,
                help=(
                    "生产环境选 vision 但没配好时会直接报错转人工,不会静默退回 Mock —— "
                    "退回 Mock 意味着没被真正评过的图自动发上网站。"
                ),
                options=(
                    Option("mock", "Mock(确定性打分,不花钱,仅用于演练)"),
                    Option("vision", "视觉大模型"),
                ),
            ),
            Field(
                key="VISION_MODEL_BASE_URL",
                label="接口地址",
                placeholder="https://api.openai.com/v1",
                help="带不带版本号都行,拼接时会自动去重,不会出现 /v1/v1/。",
            ),
            Field(
                key="VISION_MODEL_NAME",
                label="模型名",
                placeholder="例如 gpt-5.6-sol / doubao-seed-2.1-pro / qwen3-vl-plus",
                help="火山方舟使用推理接入点时,这里直接填 Endpoint ID。",
            ),
            Field(key="VISION_MODEL_API_KEY", label="API Key", type=TYPE_PASSWORD, secret=True),
            Field(
                key="VISION_MODEL_API_STYLE",
                label="API 形状",
                type=TYPE_SELECT,
                help="换厂商主要就改这一项。按接口形状选,与厂商名无关。",
                options=(
                    Option("responses", "Responses(OpenAI、火山方舟)"),
                    Option("chat_completions", "Chat Completions(百炼兼容模式及大多数兼容端点)"),
                ),
            ),
            Field(
                key="VISION_MODEL_RESPONSE_FORMAT",
                label="输出约束",
                type=TYPE_SELECT,
                help="端点报「不支持 response_format」时逐级往下降。",
                options=(
                    Option("json_schema", "JSON Schema(最严,推荐)"),
                    Option("json_object", "JSON Object(端点不支持 Schema 时)"),
                    Option("prompt_only", "仅靠提示词约束(兜底)"),
                ),
            ),
            Field(
                key="VISION_MODEL_ALLOW_ANONYMOUS",
                label="端点无需鉴权",
                type=TYPE_BOOL,
                options=_yes_no(),
                help=(
                    "自建端点但挂了公网域名时打开。默认按主机名判断:"
                    "回环、私网、容器服务名免 Key,公网地址要求填 Key。"
                ),
            ),
        ),
    ),
    Group(
        key="evaluator_vision_tuning",
        title="评分模型 · 多模态调参",
        description=(
            "改这些之前先读 docs/VISION-EVALUATOR.md。"
            "**改任何一项都可能移动分数分布,移动了就要重新校准 A/B/C/D 阈值。**"
        ),
        fields=(
            Field(
                key="VISION_MODEL_FULL_IMAGE_DETAIL",
                label="完整评分图片精度",
                type=TYPE_SELECT,
                help="FULL 要看印花、肩带、走线,精度低了会漏判。",
                options=(
                    Option("high", "high"),
                    Option("original", "original(仅部分端点支持)"),
                    Option("low", "low"),
                    Option("auto", "auto"),
                ),
            ),
            Field(
                key="VISION_MODEL_QUICK_IMAGE_DETAIL",
                label="快速检查图片精度",
                type=TYPE_SELECT,
                help="QUICK 只找明显硬错误,用 low 省钱。",
                options=(
                    Option("low", "low"),
                    Option("high", "high"),
                    Option("auto", "auto"),
                ),
            ),
            Field(
                key="VISION_MODEL_REASONING_EFFORT",
                label="推理强度",
                type=TYPE_SELECT,
                help=(
                    "千问 VL 必须选 none —— 思考内容混进输出会破坏标准 JSON。"
                    "选 none 时改发 temperature=0。"
                ),
                options=(
                    Option("none", "none(非思考模式)"),
                    Option("low", "low"),
                    Option("medium", "medium"),
                    Option("high", "high"),
                ),
            ),
            Field(
                key="VISION_MODEL_MAX_OUTPUT_TOKENS",
                label="最大输出 Token",
                type=TYPE_INTEGER,
                minimum=256,
                maximum=32000,
                help="太小会截断 JSON,表现为「输出被截断」而不是评分偏差。",
            ),
            Field(
                key="VISION_MODEL_MAX_REFERENCE_IMAGES",
                label="最多发几张参考图",
                type=TYPE_INTEGER,
                minimum=1,
                maximum=12,
                help="超出部分按顺序截断(顺序即重要性),截断会记一条 warning。",
            ),
            Field(
                key="VISION_MODEL_MAX_IMAGE_MB",
                label="单张图上限(MB)",
                type=TYPE_INTEGER,
                minimum=1,
                maximum=64,
                help="超了直接拒绝,不会悄悄压缩后发出去。",
            ),
            Field(
                key="VISION_MODEL_TIMEOUT_SECONDS",
                label="超时(秒)",
                type=TYPE_NUMBER,
                minimum=10,
                maximum=600,
            ),
            Field(
                key="VISION_MODEL_MAX_RETRIES",
                label="最大重试次数",
                type=TYPE_INTEGER,
                minimum=0,
                maximum=5,
                help="只对限流、超时和 5xx 生效;401/400/内容安全一律不重试。",
            ),
            Field(
                key="VISION_MODEL_RETRY_BASE_SECONDS",
                label="重试退避基数(秒)",
                type=TYPE_NUMBER,
                minimum=0.1,
                maximum=10,
            ),
            Field(
                key="VISION_MODEL_SEND_PUBLIC_URLS",
                label="发公网 URL 而非图片内容",
                type=TYPE_BOOL,
                options=_yes_no(),
                help=(
                    "只在存储换成公网可达的对象存储之后才打开。"
                    "开着但地址是 http://localhost 时会自动退回内联发送。"
                ),
            ),
            Field(
                key="VISION_MODEL_FAIL_CLOSED",
                label="评分器不可用时失败关闭",
                type=TYPE_BOOL,
                options=_yes_no(),
                help=(
                    "**生产环境必须开着。** 关掉意味着评分器出问题时退回 Mock,"
                    "而 Mock 会把相当比例的真实商品图判成 A 档直接放行。"
                ),
            ),
        ),
    ),
    Group(
        key="extractor",
        title="属性识别 · 多模态",
        description=(
            "看商品样品图、把颜色图案领型这些**事实**读出来的模型。与上面的评分模型"
            "**是两组独立配置**,即便填的是同一个端点也要各填一遍:识别的置信度校准"
            "按(字段 × 模型 × 提示词)分箱存,让两者共享一份配置的话,换一次评分模型"
            "会把识别的全部校准数据悄悄作废,而界面上不会有任何提示。"
        ),
        fields=(
            Field(
                key="EXTRACTOR_BACKEND",
                label="抽取器",
                type=TYPE_SELECT,
                help=(
                    "选 vision 但没配好时,第一次识别会直接报错并点名缺哪个键,"
                    "不会静默退回 Mock —— 退回 Mock 意味着本地编的属性会进文案宣称"
                    "和上架草稿,末端是一份看起来合规的假 Listing。"
                ),
                options=(
                    Option("mock", "Mock(按图片指纹编造,不花钱,仅用于演练)"),
                    Option("vision", "视觉大模型"),
                ),
            ),
            Field(
                key="EXTRACTOR_MODEL_BASE_URL",
                label="接口地址",
                placeholder="https://api.openai.com/v1",
                help="带不带版本号都行,拼接时会自动去重,不会出现 /v1/v1/。",
            ),
            Field(
                key="EXTRACTOR_MODEL_NAME",
                label="模型名",
                placeholder="例如 gpt-5.6-sol / doubao-seed-2.1-pro / qwen3-vl-plus",
                help="火山方舟使用推理接入点时,这里直接填 Endpoint ID。",
            ),
            Field(
                key="EXTRACTOR_MODEL_API_KEY",
                label="API Key",
                type=TYPE_PASSWORD,
                secret=True,
            ),
            Field(
                key="EXTRACTOR_MODEL_API_STYLE",
                label="API 形状",
                type=TYPE_SELECT,
                help="换厂商主要就改这一项。按接口形状选,与厂商名无关。",
                options=(
                    Option("responses", "Responses(OpenAI、火山方舟)"),
                    Option(
                        "chat_completions",
                        "Chat Completions(百炼兼容模式及大多数兼容端点)",
                    ),
                ),
            ),
            Field(
                key="EXTRACTOR_MODEL_RESPONSE_FORMAT",
                label="结构化输出",
                type=TYPE_SELECT,
                help=(
                    "端点报「不支持 response_format」时逐级往下降。降到 prompt_only "
                    "之后模型输出不再有格式保证,解析失败的那张图会算作失败并留痕,"
                    "**不会**被当成「什么都没识别出来」。"
                ),
                options=(
                    Option("json_schema", "JSON Schema(最严格,推荐)"),
                    Option("json_object", "JSON Object(只保证是 JSON)"),
                    Option("prompt_only", "仅靠提示词(端点两者都不支持时)"),
                ),
            ),
            Field(
                key="EXTRACTOR_MODEL_ALLOW_ANONYMOUS",
                label="端点无需鉴权",
                type=TYPE_BOOL,
                options=_yes_no(),
                help=(
                    "自建端点挂在公网域名上时打开。默认按主机判断,而判断不出来时"
                    "**要求填 Key** —— 猜错的方向必须是多要一个 Key,反过来会把"
                    "「忘了配 Key」伪装成「已配置」,然后每次调用都 401。"
                ),
            ),
            Field(
                key="EXTRACTOR_MODEL_IMAGE_DETAIL",
                label="图片精细度",
                type=TYPE_SELECT,
                help=(
                    "识别只有这一档,没有评分那种「快看/细看」之分:识别的产出会变成"
                    "商品事实,没有「粗看一眼」这个合法档位。"
                ),
                options=(
                    Option("high", "high(推荐)"),
                    Option("low", "low(省 token,细节字段会明显变差)"),
                    Option("auto", "auto(由端点决定)"),
                ),
            ),
            Field(
                key="EXTRACTOR_MODEL_REASONING_EFFORT",
                label="推理强度",
                type=TYPE_SELECT,
                help="千问 VL 用 none:思考内容混进输出会破坏标准 JSON。",
                options=(
                    Option("none", "none(非推理模型/千问 VL)"),
                    Option("low", "low"),
                    Option("medium", "medium"),
                    Option("high", "high"),
                ),
                advanced=True,
            ),
            Field(
                key="EXTRACTOR_MAX_IMAGES_PER_RUN",
                label="单次识别图片上限",
                type=TYPE_INTEGER,
                minimum=1,
                maximum=100,
                help=(
                    "识别目前是同步逐图调用,一次点击就是这么多次付费调用。"
                    "超出时接口直接拒绝并提示分批,**不会**先跑一半再超时。"
                    "Mock 不受这条限制。"
                ),
            ),
            Field(
                key="EXTRACTOR_MODEL_MAX_OUTPUT_TOKENS",
                label="最大输出 token",
                type=TYPE_INTEGER,
                minimum=256,
                maximum=32000,
                help="输出被截断时错误信息会直接点名这一项,不会报成「JSON 不合法」。",
                advanced=True,
            ),
            Field(
                key="EXTRACTOR_MODEL_MAX_IMAGE_MB",
                label="单图上限(MB)",
                type=TYPE_INTEGER,
                minimum=1,
                maximum=64,
                help="超了直接拒绝,不会悄悄压缩后发出去。",
                advanced=True,
            ),
            Field(
                key="EXTRACTOR_MODEL_TIMEOUT_SECONDS",
                label="超时(秒)",
                type=TYPE_NUMBER,
                minimum=5,
                maximum=600,
                advanced=True,
            ),
            Field(
                key="EXTRACTOR_MODEL_MAX_RETRIES",
                label="最大重试次数",
                type=TYPE_INTEGER,
                minimum=0,
                maximum=5,
                advanced=True,
            ),
            Field(
                key="EXTRACTOR_MODEL_RETRY_BASE_SECONDS",
                label="重试基准退避(秒)",
                type=TYPE_NUMBER,
                minimum=0,
                maximum=30,
                advanced=True,
            ),
            Field(
                key="EXTRACTOR_MODEL_SEND_IDEMPOTENCY_KEY",
                label="发供应商幂等键",
                type=TYPE_BOOL,
                options=_yes_no(),
                help=(
                    "传输层对超时与 5xx 自动重试,而客户端超时不等于供应商没收到 —— "
                    "同一张图可能被受理两次、计费两次。这个头是让供应商自己"
                    "认出重发的唯一手段。**开之前先确认端点支持**:不认识它的"
                    "严格网关会直接 400,表现是识别整条不通。"
                ),
                advanced=True,
            ),
            Field(
                key="EXTRACTOR_MODEL_SEND_PUBLIC_URLS",
                label="发公网 URL 而非图片内容",
                type=TYPE_BOOL,
                options=_yes_no(),
                help=(
                    "只在存储换成公网可达的对象存储之后才打开。"
                    "开着但地址是 http://localhost 时会自动退回内联发送。"
                ),
                advanced=True,
            ),
        ),
    ),
    Group(
        key="text_model",
        title="文本模型 · 文案生成",
        description=(
            "**文案生成器选 `llm` 时才会用到这一组。** 选 `template` 时下面的配置"
            "填了也不会被读到 —— 但两者的区别现在是**真的**:原来无论这里怎么选,"
            "代码里都恒定使用本地模板生成器,配好密钥和模型名也没有任何效果。"
        ),
        fields=(
            Field(
                key="COPY_GENERATOR",
                label="文案生成器",
                type=TYPE_SELECT,
                options=(
                    Option("template", "本地模板(不调用模型,不产生费用)"),
                    Option("llm", "文本模型(按下面的配置调用,产生费用)"),
                ),
                help=(
                    "切到「文本模型」后,接口地址和模型名必须填完整,否则文案生成"
                    "会直接报错转人工,而不是静默退回模板 —— 静默退回意味着"
                    "你以为在用模型,实际拿到的是模板文案。"
                ),
            ),
            Field(
                key="TEXT_MODEL_BASE_URL",
                label="接口地址",
                placeholder="https://api.openai.com/v1",
            ),
            Field(key="TEXT_MODEL_NAME", label="模型名", placeholder="例如 gpt-5.6-terra"),
            Field(key="TEXT_MODEL_API_KEY", label="API Key", type=TYPE_PASSWORD, secret=True),
            Field(
                key="TEXT_MODEL_API_STYLE",
                label="API 形状",
                type=TYPE_SELECT,
                options=(
                    Option("responses", "Responses"),
                    Option("chat_completions", "Chat Completions"),
                ),
                help=(
                    "决定往 /responses 还是 /chat/completions 发请求。选错的表现是"
                    "404,而 404 会被归类成「厂商不可达」,排查方向从一开始就是错的。"
                ),
            ),
        ),
    ),
    Group(
        key="network",
        title="下载安全",
        description="结果图一律由后端下载入库。默认拒绝一切内网地址,自建服务要在这里显式放行。",
        fields=(
            Field(
                key="DOWNLOAD_ALLOWED_HOSTS",
                label="允许下载的内网主机",
                placeholder="comfyui,10.0.0.5",
                help="逗号分隔。云元数据端点 169.254.169.254 即使填进来也一律拒绝。",
            ),
        ),
    ),
)


FIELDS: dict[str, Field] = {f.key: f for g in SETTING_GROUPS for f in g.fields}
SECRET_KEYS: frozenset[str] = frozenset(k for k, f in FIELDS.items() if f.secret)


def is_known(key: str) -> bool:
    return key in FIELDS


def mask(value: str) -> str:
    """密钥的展示形态。空值返回空串,调用方据此显示\"未配置\"。"""
    if not value:
        return ""
    if len(value) <= MASK_TAIL:
        return MASK_CHAR * 8
    return MASK_CHAR * 8 + value[-MASK_TAIL:]


def looks_like_mask(value: str) -> bool:
    """拦住\"把界面上的打码串原样存回去\"这种事。"""
    return bool(value) and MASK_CHAR in value


def normalize(key: str, raw: object) -> str:
    """把前端送来的值校验并归一成可落库的字符串。不合法直接抛 ValueError。

    纯函数,不碰数据库也不读环境变量 —— \"这个值为什么被拒\"永远能在单测里复现。
    """
    spec = FIELDS.get(key)
    if spec is None:
        raise ValueError(f"未知配置项:{key}")

    if raw is None:
        raw = ""
    if isinstance(raw, bool):
        text = "true" if raw else "false"
    else:
        text = str(raw).strip()

    if len(text) > MAX_VALUE_LENGTH:
        raise ValueError(f"{spec.label}:长度超过 {MAX_VALUE_LENGTH} 个字符")
    if spec.secret and looks_like_mask(text):
        raise ValueError(f"{spec.label}:这是界面上的打码串,请填写真实值或留空表示不改")

    if spec.type in (TYPE_NUMBER, TYPE_INTEGER):
        if not text:
            return ""
        try:
            number = float(text)
        except ValueError:
            raise ValueError(f"{spec.label}:需要一个数字") from None

        # float("nan") 和 float("inf") 都能解析成功,而 NaN 和**任何**数比较
        # 都是 False —— 下面两条上下界检查会一起放行它。存进去之后,
        # 拿它做超时、做轮次上限,行为完全不可预期。
        if not math.isfinite(number):
            raise ValueError(f"{spec.label}:需要一个有限的数字,不接受 NaN 或无穷大")

        if spec.type == TYPE_INTEGER and not number.is_integer():
            # 以前这里静默接受,然后在使用处 int() 截断 ——
            # 用户填 2.7、系统按 2 跑,两边不一致且没有任何提示
            raise ValueError(f"{spec.label}:只能填整数")

        if spec.minimum is not None and number < spec.minimum:
            raise ValueError(f"{spec.label}:不能小于 {_trim(spec.minimum)}")
        if spec.maximum is not None and number > spec.maximum:
            raise ValueError(f"{spec.label}:不能大于 {_trim(spec.maximum)}")
        return _trim(number)

    if spec.type == TYPE_BOOL:
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return "true"
        if lowered in {"0", "false", "no", "off", ""}:
            return "false"
        raise ValueError(f"{spec.label}:只能是开启或关闭")

    if spec.type == TYPE_SELECT:
        allowed = {o.value for o in spec.options}
        if text not in allowed:
            readable = ", ".join(sorted(o.value for o in spec.options if o.value)) or "(空)"
            raise ValueError(f"{spec.label}:只能是 {readable}")
        return text

    return text


def _trim(number: float) -> str:
    """1.0 显示成 1,2.5 保持 2.5 —— 轮次上限写成 10.0 会让人以为能填小数。"""
    if float(number).is_integer():
        return str(int(number))
    return str(number)
