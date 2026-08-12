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


#: 免口令的本机开发环境。**判据只有这一份语义,不新造第三套。**
#:
#: 同样的字面量在 `app/api/deps.py` 里也有一份,而那不是疏忽:
#:
#:   1. `deps.py` 模块级 `from app.core.config import settings` —— core 反过来
#:      import api 是循环导入,`.importlinter` 的「core 是最底层」契约也禁止它
#:      (包括函数体内 import:import-linter 建的是完整依赖图,藏不住);
#:   2. `test_security_audit.py::test_local_bypass_does_not_cover_a_test_environment`
#:      用 AST 直接读 `deps.py` 里那条**赋值语句**的字符串常量,把
#:      "APP_ENV=test 不得免口令" 钉在那个文件上。改成 import 会让它失锚。
#:
#: 所以这里的处置和前后端匿名白名单是同一个套路:**允许两份字面量,但用一条
#: 门禁把它们钉成相等**(`test_browser_session_structure.py::
#: test_local_envs_agree_between_config_and_deps`),而不是让两份自由漂移。
#:
#: 刻意**不含** ``test``:local/dev/development 只可能是某人的开发机,
#: 而"测试环境"在多数团队里是一台真的、连着真 Key、别人也能访问的机器。
LOCAL_ENVS = frozenset({"local", "dev", "development"})

#: Session 签名密钥的最短长度。32 个字符 ≈ 一次 `token_urlsafe(24)`,
#: 低于这个长度的密钥离线爆破成本已经不够看,而 Cookie 是签名不加密的。
_MIN_SESSION_SECRET_LENGTH = 32

#: 一眼可辨的占位值。它们全都出现在公开文档、示例文件和教程里,
#: 所以"配了但配的是它"和"没配"在安全上是同一件事 —— 一起拦。
_PLACEHOLDER_SECRETS = frozenset(
    {"change_me", "changeme", "change-me", "password", "secret", "admin", "operator"}
)


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

    # --- 连接池与请求并发(a51)---
    #
    # ## 这一组在此之前**一个都不存在**,而两侧的默认值是冲突的
    #
    # `create_engine()` 只传了 `pool_pre_ping`,于是走 SQLAlchemy 的默认:
    # `pool_size=5` + `max_overflow=10` = **同时最多 15 条连接**。
    #
    # 而 API 层有 209 个同步 `def` 端点(只有 9 个 `async def`)。FastAPI 把
    # 同步端点全部丢进 anyio 的线程池,默认容量 **40**。也就是说:
    #
    #     40 个并发请求  ->  抢 15 条连接  ->  25 个在池上排队
    #                    ->  `pool_timeout` 默认 30 秒后抛
    #                        `QueuePool limit of size 5 overflow 10 reached`
    #
    # 这不是"调优",是两个默认值互相不知道对方存在。而且此前**没有任何
    # 环境变量能改它们** —— 出了事既不能扩池也不能限流,只能改代码重发版。
    #
    # 有意思的是这个现象被预见过:`frontend/src/api/client.ts` 的超时提示里
    # 写着「或数据库连接被占满」。**症状写进了文案,旋钮一直没有。**
    #
    # ## 为什么默认值是 10 + 20 对 30,而不是把池扩到 40
    #
    # 池容量要 >= 线程池容量,否则多出来的线程只能在池上排队;但连接不是免费的
    # —— PostgreSQL 的 `max_connections` 默认 100,而这套还有 Celery worker
    # (prefork,每个子进程一套自己的池)要分。所以两边一起收到 30:
    # API 进程最多 30 条,给 worker 和运维留出余量。
    #
    # 要撑更高并发,**两个数一起调**。只调其中一个不会报错,只会让另一个
    # 变成瓶颈 —— `tests/pure/test_a51_pool_capacity.py` 钉的就是这个关系。
    DB_POOL_SIZE: int = 10
    DB_MAX_OVERFLOW: int = 20
    #: 池满之后等多久放弃。默认 30 秒是 SQLAlchemy 的默认值,这里显式写出来 ——
    #: 一个"请求挂 30 秒然后 500"的行为不该是没人知道自己选过的。
    DB_POOL_TIMEOUT_SECONDS: int = 30
    #: 连接活多久就主动换掉。防的是中间件(pgbouncer / 云厂商 LB / 防火墙)
    #: 单方面掐掉空闲连接而池子并不知情。`pool_pre_ping` 已经能兜住大部分,
    #: 但它是"用之前探一次",探测本身也有代价;定期回收让长连接不至于老到
    #: 需要每次探。1800 秒明显短于常见的 3600 秒空闲上限。
    DB_POOL_RECYCLE_SECONDS: int = 1800
    #: 同时能有多少个同步端点在跑。**它就是 DB 连接的真实需求量上限** ——
    #: 每个同步端点拿一条连接(`get_session` 每请求一个 Session)。
    SERVER_THREADPOOL_SIZE: int = 30

    #: 信不信 `X-Real-IP` / `X-Forwarded-For`。**默认关**,理由与打开的前提
    #: 写在 `core/client_ip.py` 顶部 —— 一句话是:只有"能连到这个进程的
    #: 东西都是我们的反代"时才能打开。本仓 compose 把后端绑在 127.0.0.1,
    #: 生产用本仓的 nginx,所以那两种部署下应当打开(compose 里已设)。
    #: 直接把后端暴露到公网时必须保持关闭,否则登录限流可以被伪造头绕过。
    TRUST_PROXY_HEADERS: bool = False

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

    # --- 浏览器登录(Browser Auth) ---
    #
    # 这一组回答的是「坐在浏览器前的这个人是谁」,和上面那两把**机器凭据**
    # (ADMIN_TOKEN / OPERATOR_TOKENS,给 CLI、脚本、pytest、服务间调用用)
    # 是两件事,故意分开:
    #
    #   1. 用户密码与 API Token 语义不同;
    #   2. 登录之后浏览器不该继续持有一把 API Token;
    #   3. 改密码不必同步去改所有脚本;
    #   4. OPERATOR_TOKENS 支持「多条目 + 名字:口令」,形状上不适合当单账号密码;
    #   5. 避免管理员密码被每个请求当请求头反复发送。
    #
    # **这五项刻意不进 `app/core/settings_schema.py`。** 进了那里就意味着值可以
    # 被设置页写入、加密落库、并受 SETTINGS_ENV_LOCK 影响 —— 于是出现
    # 「用管理员会话去改管理员密码」的自指闭环,而且密码的真相来源变成两个
    # (env + DB)。改密码和改 Provider Key 不该共用同一条通路。
    #: 浏览器登录用的两个固定账号密码。只从环境变量读。
    ADMIN_PASSWORD: str = ""
    OPERATOR_PASSWORD: str = ""
    #: 签名浏览器 Session Cookie 用。多机部署必须显式配置,否则各节点签名互不认。
    AUTH_SESSION_SECRET: str = ""
    #: 默认 12 小时。**注意这是滑动过期(idle timeout),不是登录后的绝对存活时长** ——
    #: Starlette 的 SessionMiddleware 在每一个 session 非空的响应上重写 Set-Cookie,
    #: 所以只要页面还在发请求,它就不会到期。取舍记在 docs/DECISIONS.md。
    AUTH_SESSION_MAX_AGE_SECONDS: int = 43200
    AUTH_SESSION_COOKIE_NAME: str = "swimwear_session"

    @model_validator(mode="after")
    def _check_browser_auth(self):
        """非本机环境必须把浏览器登录配全,**配不全就起不来**。

        ## 判据为什么是 LOCAL_ENVS 而不是 is_production

        `is_production` 只认 production/prod。用它做判据的话,`APP_ENV=uat`、
        `staging`、`test` 会落进一个"既不强制、也没说不强制"的未定义区间 ——
        而那几个名字对应的往往正是别人也能访问的真机器。

        这里复用的是 `deps.resolve_identity` 判断"要不要口令"的**同一条语义**:
        只有 local/dev/development 才免。少一个字母的差别不该决定要不要密码。

        ## 为什么是抛错而不是打日志

        `main.lifespan` 里的 `secrets_dir_is_exposed` 是"只打 error 就放行"的
        先例,那条被判定为错误的权衡:一条启动时刷过去的红字,在容器日志里
        和其余几十行一样,没有人会因为它去改配置。配置不全的后果是登录页
        永远登不进去、或者更糟 —— 用一把空密钥签 Cookie。所以在这里拦死。

        ## local 也校验,只是「全空」额外放行(2026-08-11 评审第 9 条)

        原来 local 直接 `return self`,于是下面这些配置都能正常启动:

            只配 ADMIN_PASSWORD,另外两个空
            AUTH_SESSION_SECRET 只有几个字符
            ADMIN_PASSWORD 与 OPERATOR_PASSWORD 相同(两个角色形同一个)

        而 `browser_auth_configured` 只要三项**任意一项**非空就返回 True ——
        也就是说系统会认为"这个部署启用了 Session 登录",而登录配置其实不可用。

        最难查的是只配密码没配 `AUTH_SESSION_SECRET` 那一种:`main.py` 会
        `secrets.token_urlsafe(48)` **随机生成**一把签名密钥。单 worker 时
        表现正常;多 worker 时每个 worker 各一把,于是

            worker A 登录成功 -> 下一个请求落到 worker B -> Cookie 验签失败

        看起来就是"随机掉登录",而没有任何一处会说为什么。

        所以 local 的规则变成:**三项全空 -> 免登录模式;否则按非 local 的
        标准全部校验。** 半配一律起不来 —— 与这个校验器"配不全就起不来"
        的整体姿态一致,只是把"配不全"的判据从"非 local"扩到"动过这一组"。

        照 `_check_spend_ratios` 的写法。
        """
        admin = (self.ADMIN_PASSWORD or "").strip()
        operator = (self.OPERATOR_PASSWORD or "").strip()
        secret = (self.AUTH_SESSION_SECRET or "").strip()

        if self.APP_ENV.strip().lower() in LOCAL_ENVS and not (
            admin or operator or secret
        ):
            # 本机开发且三项全空:沿用旧的 Legacy Token + ROLE_DEV 模式。
            # 只要动过其中任意一项,就落到下面的完整校验 —— 半配的登录
            # 比没有登录更难查,见 docstring。
            return self

        problems: list[str] = []

        if not admin:
            problems.append("ADMIN_PASSWORD 为空")
        if not operator:
            problems.append("OPERATOR_PASSWORD 为空")
        if admin and operator and admin == operator:
            # 两个密码相同 = 两个角色形同一个。而界面、审计、403 全都按
            # "这是两个人"在工作,于是权限边界在文档上存在、在现实里不存在。
            problems.append("ADMIN_PASSWORD 与 OPERATOR_PASSWORD 不能相同")
        if not secret:
            problems.append("AUTH_SESSION_SECRET 为空")
        elif len(secret) < _MIN_SESSION_SECRET_LENGTH:
            problems.append(
                f"AUTH_SESSION_SECRET 至少 {_MIN_SESSION_SECRET_LENGTH} 个字符"
                f"(当前 {len(secret)})"
            )
        for label, value in (
            ("ADMIN_PASSWORD", admin),
            ("OPERATOR_PASSWORD", operator),
            ("AUTH_SESSION_SECRET", secret),
        ):
            if value and value.lower() in _PLACEHOLDER_SECRETS:
                # 占位值比空值更危险:空值会被上面拦住,占位值会一路启动成功,
                # 而它是公开的 —— .env.example 和每一份教程里都写着同一个词。
                problems.append(f"{label} 仍然是占位值({value})")
        if self.AUTH_SESSION_MAX_AGE_SECONDS <= 0:
            problems.append(
                f"AUTH_SESSION_MAX_AGE_SECONDS 必须为正(当前 "
                f"{self.AUTH_SESSION_MAX_AGE_SECONDS})"
            )

        if problems:
            # 报错第一句要说清**为什么轮到我校验**:非 local 是"这个环境必须配",
            # local 半配是"你动过这一组,那就得配全"。两句话指向的下一步不同 ——
            # 后者还有"三项一起清空"这条出路,而那正是本机开发最常想要的那条
            why = (
                f"APP_ENV={self.APP_ENV} 不属于 {sorted(LOCAL_ENVS)},必须配置浏览器登录"
                if self.APP_ENV.strip().lower() not in LOCAL_ENVS
                else (
                    "浏览器登录只配了一半 —— 三项要么全空(本机免登录模式),"
                    "要么全部配齐。半配的表现是「系统认为登录已启用而它其实不可用」,"
                    "多 worker 下还会因为随机签名密钥而反复掉登录"
                )
            )
            raise ValueError(
                why + ":" + ";".join(problems)
                + "。生成密钥:python3 -c \"import secrets;"
                "print(secrets.token_urlsafe(48))\""
            )
        return self

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

    @model_validator(mode="after")
    def _check_cors_is_not_a_credentialed_wildcard(self):
        """`CORS_ORIGINS=*` 与带凭据的跨域不能并存(a51)。

        `main.py` 那个中间件是 `allow_credentials=True` 写死的(它必须是 ——
        浏览器登录靠 Cookie)。配上 `*` 之后 Starlette 会把请求的 Origin
        原样回显,效果等于**任何站点都能带着用户的登录态调这套 API**。

        ## 今天它不可利用,而这正是要拦的理由

        Session Cookie 是 `SameSite=Lax`,跨站 XHR 根本不带它 —— 所以真配成
        `*` 也打不进来。也就是说这个洞现在被**另一处配置**兜着,而那一处
        与它没有任何显式关联:哪天为了别的需求把 Cookie 改成 `SameSite=None`
        (跨站嵌入、第三方 iframe 是最常见的两个理由),这里会当场变成一个
        真的洞,而改 Cookie 的人没有任何理由去看 CORS 配置。

        拦在启动期,两处就不必互相记得对方。

        ## 为什么不是"把 `*` 悄悄换成默认值"

        那样起得来、也不报错,而运维以为自己开的跨域生效了 —— 排查会从
        "为什么跨域没生效"开始,而不是从"这个配置被拒绝了"开始。
        """
        if "*" in self.cors_origin_list:
            raise ValueError(
                "CORS_ORIGINS 不能是 `*`:跨域是带凭据的(allow_credentials=True),"
                "通配符会让任何站点都能用用户的登录态调这套 API。请逐条列出来源。"
            )
        return self

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
    def is_local_env(self) -> bool:
        """本机开发环境。判据与 `deps.LOCAL_ENVS` 同源(见模块顶部注释)。"""
        return self.APP_ENV.strip().lower() in LOCAL_ENVS

    @property
    def browser_auth_configured(self) -> bool:
        """浏览器登录是不是已经配了。

        **"配了"的判据是三项里有任意一项非空,不是三项都齐。** 方向是刻意的:

        - 非 local 环境下 `_check_browser_auth` 已经保证"要么三项齐全、
          要么起不来",所以这里在非 local 只会是 True;
        - local 环境下,只要有人填了其中一项,就说明他**想**测登录。这时候
          必须真的走 Session,不能让 `ROLE_DEV` 那条免口令的回落把他悄悄绕开 ——
          否则本地怎么点都是通的,admin/operator 的差异、logout、403
          一个都验不到,而人工验收正是要验这些。
        """
        return bool(
            (self.ADMIN_PASSWORD or "").strip()
            or (self.OPERATOR_PASSWORD or "").strip()
            or (self.AUTH_SESSION_SECRET or "").strip()
        )

    @property
    def session_cookie_https_only(self) -> bool:
        """Session Cookie 要不要 Secure 标记。**判据写死在这里,不新增 env。**

        v1.2 只说了"HTTPS 环境 true、local HTTP false",没说谁来判 —— 留空的
        结果是每个实现者各写一套,而这个值配错**不会报错**:配成 True 之后
        本地 HTTP 上浏览器会安静地不回传 Cookie,表现是"登录成功但刷新就掉线"。

        复用两个已有配置,不新增 env(新增就要再进一遍 `.env.example` 契约)。
        `PUBLIC_BASE_URL` 默认 http://localhost:8000,于是本地自动为 False。
        """
        return self.is_production or self.PUBLIC_BASE_URL.strip().lower().startswith(
            "https://"
        )

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
