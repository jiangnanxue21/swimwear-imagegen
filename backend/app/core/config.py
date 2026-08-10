"""应用配置。所有密钥只能来自环境变量,禁止硬编码(需求第十九章)。

未配置任何 Provider Key 时应用必须能正常启动,因此所有第三方相关字段均可为空。
"""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from app.core.vocab import (
    BATCH_EXECUTION_MODES,
    COPY_GENERATORS,
    TEXT_API_STYLES,
)

BACKEND_ROOT = Path(__file__).resolve().parents[2]
PROJECT_ROOT = BACKEND_ROOT.parent


def _pin_resp2(url: str) -> str:
    """给 Celery 的 result backend URL 钉上 `protocol=2`(RESP2)。

    redis-py 8.x 把默认协议从 RESP2 改成 RESP3。Celery 的 result backend 直接
    由 redis-py 建连；项目连的远端 Redis(或前置代理)在 `HELLO 3 AUTH` 阶段会
    直接断连，于是任务做完后结果写不回去。

    **只用在 Redis result backend 上。** broker 虽然最终也用 redis-py 建连，
    但参数由 Kombu 的 Redis transport 筛选，它没有暴露 `protocol`；把参数写进
    broker URL 会在 worker 启动时直接抛
    `TypeError: Connection._init_params() got an unexpected keyword argument 'protocol'`。
    broker 的协议兼容性因此由 `app.tasks.redis_transport` 保证。

    用户**显式**写了 `?protocol=...` 时不覆盖 —— 那是有人刻意选了另一档。
    """
    parts = urlsplit(url)
    if parts.scheme not in {"redis", "rediss"}:
        return url
    params = dict(parse_qsl(parts.query, keep_blank_values=True))
    if "protocol" not in params:
        params["protocol"] = "2"
    return urlunsplit(parts._replace(query=urlencode(params)))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- 应用 ---
    APP_NAME: str = "swimwear-imagegen"
    APP_ENV: str = "local"
    DEBUG: bool = True
    LOG_LEVEL: str = "INFO"
    API_PREFIX: str = "/api"
    CORS_ORIGINS: str = "http://localhost:5173"

    # --- 数据库 ---
    POSTGRES_HOST: str = "localhost"
    POSTGRES_PORT: int = 5432
    POSTGRES_USER: str = "imagegen"
    POSTGRES_PASSWORD: str = "imagegen"
    POSTGRES_DB: str = "imagegen"
    #: 显式覆盖用,留空则由上面各项拼接
    DATABASE_URL: str = ""

    # --- Redis / Celery ---
    REDIS_URL: str = "redis://localhost:6379/0"
    CELERY_BROKER_URL: str = ""
    CELERY_RESULT_BACKEND: str = ""
    CELERY_TASK_ALWAYS_EAGER: bool = False

    # --- 费用与预算(A23) ---
    #
    # 这一组回答的是「这个月在付费模型上花了多少、会不会花超」。
    # 它记的是**本系统发起的调用**,不是厂商账户余额 —— 别的系统共用同一把 Key、
    # 厂商侧的赠送额度、失败但仍计费的调用,每一样都会让两者对不上。
    # 界面上一律叫「预算」不叫「余额」,权威数字在厂商控制台。

    #: 价目表,JSON。形状:{"fashn": {"*": {"micros": 40000, "currency": "USD"}}}
    #: micros 是微单位,1 货币单位 = 1_000_000 微(整数,避免浮点尾数)。
    #: 留空则所有调用记 cost=0 且 unit_price=NULL,看板会提示"未配价"
    PROVIDER_PRICE_BOOK: str = ""
    #: 价目表版本号,随流水快照下来。调价时改这个值,历史账目才对得上
    PRICING_VERSION: str = "v1"
    #: 月度预算,微单位。0 = 未设,看板显示"未设预算"而不是一条 0% 的绿条
    SPEND_MONTHLY_BUDGET_MICROS: int = 0
    SPEND_CURRENCY: str = "USD"
    #: 用量占比达到这两条线时分别转黄、转红
    SPEND_WARN_RATIO: float = 0.7
    SPEND_CRITICAL_RATIO: float = 0.9

    @model_validator(mode="after")
    def _check_spend_ratios(self):
        """告警阈值必须 warn < critical。

        配反了不会报错,只会让 WARN 那一档**永远进不去**:
        `evaluate_budget` 先判 critical,花到 80% 时 `0.8 >= 0.7` 直接判成
        CRITICAL,于是"该留意了"和"快没了"变成同一句话,而运营会按
        后者的紧急程度对待每一次预算提醒 —— 提醒也就失效了。
        """
        warn, critical = float(self.SPEND_WARN_RATIO), float(self.SPEND_CRITICAL_RATIO)
        if not 0 < warn < critical:
            raise ValueError(
                f"SPEND_WARN_RATIO({warn})必须大于 0 且小于 "
                f"SPEND_CRITICAL_RATIO({critical})"
            )
        return self

    # --- 存储 ---
    STORAGE_BACKEND: str = "local"  # local | s3(阶段 6)
    STORAGE_LOCAL_DIR: str = "./storage"
    PUBLIC_BASE_URL: str = "http://localhost:8000"
    S3_ENDPOINT_URL: str = ""
    S3_BUCKET: str = ""
    S3_ACCESS_KEY_ID: str = ""
    S3_SECRET_ACCESS_KEY: str = ""
    S3_REGION: str = ""
    #: CDN 或自定义域名。留空则用 endpoint/bucket 拼
    S3_PUBLIC_BASE_URL: str = ""

    # --- 上传限制 ---
    MAX_UPLOAD_SIZE_MB: int = 20
    MIN_IMAGE_EDGE_PX: int = 256
    #: 允许从内网地址下载的主机白名单(逗号分隔)。自建 ComfyUI 时把它的主机名加进来。
    #: 链路本地地址(云元数据端点)即使加进来也一律拒绝。
    DOWNLOAD_ALLOWED_HOSTS: str = ""

    # --- Provider(阶段 3 起使用,未配置时对应 Provider 显示"未配置") ---
    FASHN_API_KEY: str = ""
    #: 留空则用官方默认 https://api.fashn.ai/v1
    FASHN_BASE_URL: str = ""
    #: 试穿模型:tryon-max(旗舰)| tryon-v1.6(快且便宜)
    FASHN_TRYON_MODEL: str = "tryon-max"
    #: 速度/质量档位。留空交给 FASHN 自动选择
    FASHN_GENERATION_MODE: str = ""
    #: 输出分辨率 1k | 2k | 4k,留空用 FASHN 默认(1k)
    FASHN_RESOLUTION: str = ""
    FASHN_OUTPUT_FORMAT: str = "png"
    FASHN_REQUEST_TIMEOUT_SECONDS: float = 60.0
    FASHN_POLL_INTERVAL_SECONDS: float = 2.0
    FASHN_POLL_TIMEOUT_SECONDS: float = 300.0
    #: 内联提交素材的大小上限;超过直接拒绝,避免把巨图编码成 base64 打过去
    FASHN_MAX_IMAGE_MB: int = 10
    #: 存储换成公网可达的对象存储后打开,可省掉 base64 编码
    FASHN_SEND_PUBLIC_URLS: bool = False
    FAL_API_KEY: str = ""
    FAL_BASE_URL: str = ""
    COMFYUI_BASE_URL: str = ""
    COMFYUI_CONFIG_FILE: str = "./comfyui/config.yaml"
    DEFAULT_PROVIDER: str = "mock"
    PROVIDER_ROUTING_MODE: str = "MANUAL"

    #: 一个任务累计允许跑多少轮(含人工审核里追加的)。防止反复重生把额度烧光
    MAX_TOTAL_ROUNDS: int = 10

    # --- 评分(阶段 4 起使用) ---
    EVALUATOR_BACKEND: str = "mock"
    VISION_MODEL_API_KEY: str = ""
    VISION_MODEL_BASE_URL: str = ""
    #: 视觉评分模型名。evaluators/vision.py 一直在读它,阶段 8 补进配置契约
    VISION_MODEL_NAME: str = ""
    #: responses | chat_completions。按 API 形状选,不按厂商选 ——
    #: 厂商名进了业务判断,换一家就要改代码。
    VISION_MODEL_API_STYLE: str = "responses"
    #: json_schema | json_object | prompt_only。端点不支持 Schema 时逐级降级。
    VISION_MODEL_RESPONSE_FORMAT: str = "json_schema"
    VISION_MODEL_TIMEOUT_SECONDS: float = 90.0
    VISION_MODEL_MAX_IMAGE_MB: int = 8
    VISION_MODEL_MAX_REFERENCE_IMAGES: int = 4
    VISION_MODEL_MAX_OUTPUT_TOKENS: int = 1800
    VISION_MODEL_MAX_RETRIES: int = 2
    VISION_MODEL_RETRY_BASE_SECONDS: float = 0.5
    #: low | high | original | auto。FULL 看细节,QUICK 只看大面。
    VISION_MODEL_FULL_IMAGE_DETAIL: str = "high"
    VISION_MODEL_QUICK_IMAGE_DETAIL: str = "low"
    #: none | low | medium | high。千问 VL 用 none:思考内容会破坏标准 JSON。
    VISION_MODEL_REASONING_EFFORT: str = "low"
    #: true 时尽量用 storage.public_url();false 时读出来转 data URL。
    #: 存储换成公网可达的对象存储后再打开,否则厂商访问不到 localhost 地址。
    VISION_MODEL_SEND_PUBLIC_URLS: bool = False
    #: 生产环境必须为 true:评分器用不了时抛错转人工,而不是静默回退 Mock。
    VISION_MODEL_FAIL_CLOSED: bool = True
    #: 显式声明端点无鉴权。自建但挂公网域名时用它,不必迁就主机名启发式。
    VISION_MODEL_ALLOW_ANONYMOUS: bool = False

    # --- 属性识别(阶段 3 起使用,A45-batch14) ---
    #: mock | vision。选 vision 但没配好时第一次调用直接报错,不静默回退 Mock
    EXTRACTOR_BACKEND: str = "mock"
    #: 独立于 VISION_MODEL_*,**不回退共享**:两个能力常用同一个端点,
    #: 但那要显式填两遍 —— 静默共享会让换评分模型时识别模型跟着换,
    #: 而识别的校准分箱按 (字段 × 模型 × Prompt) 存,模型悄悄换掉之后
    #: 所有分箱作废、置信度全部退回"未校准",没有任何提示
    EXTRACTOR_MODEL_API_KEY: str = ""
    EXTRACTOR_MODEL_BASE_URL: str = ""
    EXTRACTOR_MODEL_NAME: str = ""
    #: responses | chat_completions。按 API 形状选,不按厂商选
    EXTRACTOR_MODEL_API_STYLE: str = "responses"
    #: json_schema | json_object | prompt_only。端点不支持 Schema 时逐级降级
    EXTRACTOR_MODEL_RESPONSE_FORMAT: str = "json_schema"
    EXTRACTOR_MODEL_TIMEOUT_SECONDS: float = 60.0
    EXTRACTOR_MODEL_MAX_IMAGE_MB: int = 8
    EXTRACTOR_MODEL_MAX_OUTPUT_TOKENS: int = 1500
    EXTRACTOR_MODEL_MAX_RETRIES: int = 2
    EXTRACTOR_MODEL_RETRY_BASE_SECONDS: float = 0.5
    #: 识别只有一档 detail:识别产出进商品事实,没有"粗看一眼"这个合法档位
    EXTRACTOR_MODEL_IMAGE_DETAIL: str = "high"
    #: none | low | medium | high。千问 VL 用 none:思考内容会破坏标准 JSON
    EXTRACTOR_MODEL_REASONING_EFFORT: str = "low"
    EXTRACTOR_MODEL_SEND_PUBLIC_URLS: bool = False
    #: 发不发 `Idempotency-Key` 头(A45-batch18 / P1-2)。传输层对超时与 5xx
    #: 自动重试,而"客户端超时"不等于"供应商没收到" —— 幂等键是唯一能让
    #: 供应商把重发认成同一笔的手段。**默认关**:不认识这个头的严格网关
    #: 会直接 400,而那时表现是"识别整条不通",排查方向和幂等毫无关系
    EXTRACTOR_MODEL_SEND_IDEMPOTENCY_KEY: bool = False
    #: 显式声明端点无鉴权。自建但挂公网域名时用它,不必迁就主机名启发式
    EXTRACTOR_MODEL_ALLOW_ANONYMOUS: bool = False
    #: 付费抽取器单次识别的图片上限。识别还是同步路径(异步化在阶段 3 第二批),
    #: 这道闸拦的是"一次点击 = 一整批付费调用"。Mock 不受限
    EXTRACTOR_MAX_IMAGES_PER_RUN: int = 12

    # --- 文本模型(非多模态) ---
    #: **这一组已经有真实调用点了** —— `app/listings/copy_generator.py` 的
    #: `LLMCopyGenerator` 用它发文案生成请求。原来这里写着「目前没有任何调用点」,
    #: 那句话在文案生成器接上之后就过期了,而它误导性很强:运营照着它理解,
    #: 会以为填了 TEXT_MODEL_* 不影响任何行为,于是不会去查为什么文案还是模板出的。
    TEXT_MODEL_API_KEY: str = ""
    TEXT_MODEL_BASE_URL: str = ""
    TEXT_MODEL_NAME: str = ""
    TEXT_MODEL_API_STYLE: str = "chat_completions"

    #: 用哪个文案生成器。`template` = 本地模板,`llm` = 走 TEXT_MODEL_*。
    #:
    #: **这个字段原来根本不存在。** `get_generator()` 里写的是
    #: `getattr(settings, "COPY_GENERATOR", TEMPLATE_GENERATOR)` —— `getattr` 的
    #: 兜底把「字段没声明」和「用户选了 template」变成了同一件事,于是配好
    #: Qwen 的密钥和模型名之后,工作台仍然在出本地模板文案,而且没有任何
    #: 地方会说一句。声明成真字段之后,拼错的值在启动期就会被下面的
    #: 校验器拦住,而不是安静地退回模板。
    COPY_GENERATOR: str = "template"

    #: 批量执行在哪里跑(评审第 2 条)。
    #:
    #:     inline   在请求内执行,但**每件一个独立事务**。默认值 ——
    #:              M3 的模板文案与本地识别是毫秒级的,拆异步只会多一层
    #:              没人观察的间接
    #:     celery   接口只建批次、立刻返回 batch_id,由 worker 领取执行,
    #:              前端轮询。**接上真实模型后必须切到它**:50 次远程调用
    #:              挤在一个 HTTP 请求里,网关先断,而钱已经花了
    BATCH_EXECUTION_MODE: str = "inline"

    # --- 后台设置页(阶段 8) ---
    #: 后台写入的密钥在落库前用它加密。留空则自动在密钥目录生成密钥文件并打日志提醒。
    #: 多机部署必须显式配置,否则各节点各自生成、互相解不开。
    SETTINGS_SECRET_KEY: str = ""
    #: 自动生成的主密钥放哪。**绝不能放进 STORAGE_LOCAL_DIR** —— 那个目录在
    #: local 存储模式下由后端挂成 /files 静态服务,主密钥进去就等于挂到公网上。
    #: 留空 = 项目根下的 .secrets/;docker compose 里指向一个独立的卷。
    SETTINGS_KEY_DIR: str = ""
    #: 打开后:凡是环境变量已经给了值的配置项,后台一律只读,数据库覆盖不生效。
    #: 生产环境用它把配置钉死在部署流水线上。
    SETTINGS_ENV_LOCK: bool = False
    #: 覆盖值的进程内缓存时长。改配置后最迟这么久 worker 也会看到,不需要重启。
    SETTINGS_CACHE_TTL_SECONDS: float = 10.0
    #: 设置页写接口的口令,请求头 X-Admin-Token。留空时非 local 环境一律拒绝写入。
    ADMIN_TOKEN: str = ""
    #: 日常业务写接口(传商品、建生成任务、审图)的口令,请求头 X-Operator-Token。
    #: 支持 `名字:口令` 的写法并用逗号分隔,这样审计日志里记的是已验证的真名,
    #: 而不是任人填写的 X-Actor。留空且没配 ADMIN_TOKEN 时,非 local 环境一律拒绝写入。
    OPERATOR_TOKENS: str = ""

    @field_validator("LOG_LEVEL")
    @classmethod
    def _upper(cls, v: str) -> str:
        return v.upper()

    @field_validator("COPY_GENERATOR")
    @classmethod
    def _known_generator(cls, v: str) -> str:
        """拼错的生成器名在**启动期**就炸,不留到运行期。

        白名单在 `app/core/vocab.py`,`copy_generator` 的注册表也认同一份 ——
        两边各写一份字符串的话,加了新生成器却忘了改校验器,表现是新生成器在
        启动期被拒,而报错信息指着一份已经过期的白名单。

        v4.1 Phase 0:这里原来是**函数体内** import `app.workbench.batch`,
        注释说是"为了避开模块级的环"。环确实避开了,方向没修 —— core 仍然
        依赖 workbench,只是把依赖藏进函数体,连 AST 静态检查都看不见
        (`test_import_graph.py` 当年就没看见)。白名单上移到 core 之后,
        这里是一条普通的模块级 import。
        """
        chosen = v.strip().lower()
        if chosen not in COPY_GENERATORS:
            raise ValueError(
                f"COPY_GENERATOR 只能是 {' / '.join(COPY_GENERATORS)},收到 {v!r}"
            )
        return chosen

    @field_validator("BATCH_EXECUTION_MODE")
    @classmethod
    def _known_batch_mode(cls, v: str) -> str:
        """同 COPY_GENERATOR:白名单与判定层共用一份,拼错在启动期炸。

        写错时**不能**静默退回 inline —— 那意味着运维以为批次跑在 worker 上,
        实际仍然挤在 HTTP 请求里,而这个差别只有在第一次超时时才暴露。
        """
        chosen = v.strip().lower()
        if chosen not in BATCH_EXECUTION_MODES:
            raise ValueError(
                f"BATCH_EXECUTION_MODE 只能是 {' / '.join(BATCH_EXECUTION_MODES)},收到 {v!r}"
            )
        return chosen

    @field_validator("TEXT_MODEL_API_STYLE")
    @classmethod
    def _known_api_style(cls, v: str) -> str:
        """同上。这个值决定往哪个端点发请求,写错的表现是 404 ——

        而 404 会被传输层归类成「厂商不可达」,排查方向从一开始就是错的。
        """
        chosen = v.strip().lower()
        if chosen not in TEXT_API_STYLES:
            raise ValueError(
                f"TEXT_MODEL_API_STYLE 只能是 {' / '.join(TEXT_API_STYLES)},收到 {v!r}"
            )
        return chosen

    @property
    def sqlalchemy_url(self) -> str:
        if self.DATABASE_URL:
            return self.DATABASE_URL
        return (
            f"postgresql+psycopg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @property
    def broker_url(self) -> str:
        # broker 经 Kombu 的 Redis transport 建连；它不把 protocol 暴露成 transport
        # option，而 URL query 又会落到不接收该参数的 Kombu Connection。因此这里
        # 不能注入 protocol=2，RESP2 由 celery_app 配置的自定义 transport 注入。
        return self.CELERY_BROKER_URL or self.REDIS_URL

    @property
    def result_backend(self) -> str:
        return _pin_resp2(self.CELERY_RESULT_BACKEND or self.REDIS_URL)

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def storage_dir(self) -> Path:
        p = Path(self.STORAGE_LOCAL_DIR)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    @property
    def secrets_dir(self) -> Path:
        """自动生成的主密钥所在目录。

        默认值刻意不落在 storage_dir 里面:那个目录会被挂成 /files 静态服务,
        任何放进去的东西都视同公开。``secrets_dir_is_exposed`` 会在启动时复查这一点,
        因为这条约束一旦被人用 SETTINGS_KEY_DIR 破坏,是完全无声的。
        """
        raw = self.SETTINGS_KEY_DIR.strip()
        if not raw:
            return (PROJECT_ROOT / ".secrets").resolve()
        p = Path(raw)
        return p if p.is_absolute() else (PROJECT_ROOT / p).resolve()

    @property
    def secrets_dir_is_exposed(self) -> bool:
        """密钥目录是否落在了对外托管的存储目录里。"""
        if self.STORAGE_BACKEND != "local":
            return False
        try:
            self.secrets_dir.relative_to(self.storage_dir)
        except ValueError:
            return False
        return True

    @property
    def is_production(self) -> bool:
        """明确的生产环境。

        只认这两个名字,不做「除了 local 都算生产」的推断:那种推断会让
        某人把 APP_ENV 写成 staging 时悄悄套上一堆生产限制,排查起来毫无线索。
        """
        return self.APP_ENV.strip().lower() in {"production", "prod"}

    @property
    def max_upload_bytes(self) -> int:
        return self.MAX_UPLOAD_SIZE_MB * 1024 * 1024


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
