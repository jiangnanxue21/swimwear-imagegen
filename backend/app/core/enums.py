"""业务枚举。仅标准库,可被测试直接导入。"""
from __future__ import annotations

from enum import StrEnum


class ProductComplexity(StrEnum):
    SIMPLE = "SIMPLE"
    MEDIUM = "MEDIUM"
    COMPLEX = "COMPLEX"
    UNKNOWN = "UNKNOWN"


class ProductStatus(StrEnum):
    DRAFT = "DRAFT"          # 刚创建,素材不全
    READY = "READY"          # 素材齐备,可创建生成任务
    GENERATING = "GENERATING"
    NEEDS_REVIEW = "NEEDS_REVIEW"
    COMPLETED = "COMPLETED"
    ARCHIVED = "ARCHIVED"


class SpuStatus(StrEnum):
    """SPU 的生命周期(PRD v3.1 §4.2)。

    **和 `ProductStatus` 不是一回事,不要复用后者。** `ProductStatus` 描述的是
    一行 SKU 在**内容生产链路**上走到哪了(素材齐没齐、在不在生成、审没审完);
    这里描述的是这个款**在不在做**。一个 ACTIVE 的 SPU 底下可以同时有
    DRAFT 和 COMPLETED 的 SKU —— 两个维度正交,合成一列的话,
    "款停掉了"和"图还没出完"会互相覆盖。
    """

    DRAFT = "DRAFT"        # 建档中,还在补颜色和尺码
    ACTIVE = "ACTIVE"      # 在做
    DISABLED = "DISABLED"  # 停掉了。**不删** —— 素材、事实、图片集都挂在它下面


class SellableStatus(StrEnum):
    """颜色变体的可售状态(PRD v3.1 §4.3)。

    PAUSED 与 DISABLED 的区别是**打算**,不是当下:PAUSED 是"这个颜色先不上,
    过阵子再说",DISABLED 是"这个颜色不做了"。两者在库存和上架上的动作一样,
    但在"新增颜色时要不要提示已有同名颜色"上不一样 —— 合成一个取值的话,
    停售一次就再也提示不出来了。

    PLANNED 是建档阶段的默认值:颜色已经建了,样品还没到。
    """

    PLANNED = "PLANNED"
    ACTIVE = "ACTIVE"
    PAUSED = "PAUSED"
    DISABLED = "DISABLED"


class AssetType(StrEnum):
    GARMENT_FRONT = "GARMENT_FRONT"        # 商品正面图
    GARMENT_BACK = "GARMENT_BACK"          # 商品背面图
    GARMENT_DETAIL = "GARMENT_DETAIL"      # 商品细节图
    GARMENT_CUTOUT = "GARMENT_CUTOUT"      # 透明底商品图
    MODEL_REFERENCE = "MODEL_REFERENCE"    # 模特参考图
    BACKGROUND_REFERENCE = "BACKGROUND_REFERENCE"  # 背景参考图


#: 要求透明通道的素材类型
ALPHA_REQUIRED_ASSET_TYPES = frozenset({AssetType.GARMENT_CUTOUT})


class Audience(StrEnum):
    """受众(PRD v2 §2.3)。**与品类正交的一等业务维度,不是品类的子分类。**

    三条规则(§3.4):

    1. 受众在导入时确定,不由 AI 静默设置 —— 它进了 §4.2 的禁改清单。
       识别错受众的代价不是一个字段错,是整条链路(模特、槽位、检查项、
       尺码表、平台类目)全部走错,且每一步单看都是"正常完成"。
    2. 受众挂在 SPU 上,不参与颜色款继承。"有男款也有女款"是两个 SPU。
    3. **UNISEX 不是"没填"。** 它是一个明确的业务判断:两种受众的模特
       都可以穿、都可以上架。未确认的商品状态是 `audience IS NULL`
       ("待确认受众"),这里刻意**没有** UNCONFIRMED 取值 ——
       给"没填"一个枚举值,它就会被当成合法受众流进模特筛选和规则包派生。
    """

    WOMEN = "WOMEN"
    MEN = "MEN"
    UNISEX = "UNISEX"


#: 受众的界面文字(§21.3:文字标签,不用性别符号或颜色编码 ——
#: 符号跨站点含义不稳定,粉/蓝在部分市场是负面信号)。
AUDIENCE_LABELS: dict[Audience, str] = {
    Audience.WOMEN: "女装",
    Audience.MEN: "男装",
    Audience.UNISEX: "中性",
}


class GarmentType(StrEnum):
    """品类词表。**取值本身不分受众**(受众是商品上的独立字段,§2.3),
    但注释按 PRD §2.2 标出每个值通常出现在哪个受众下,便于对照规则包。
    """

    # --- 女装泳衣(§2.2) ---
    ONE_PIECE = "ONE_PIECE"
    BIKINI_TOP = "BIKINI_TOP"
    BIKINI_BOTTOM = "BIKINI_BOTTOM"
    BIKINI_SET = "BIKINI_SET"
    TANKINI = "TANKINI"
    COVER_UP = "COVER_UP"          # 罩衫/沙滩外搭;中性罩衫也用它
    DRESS = "DRESS"                # 泳裙
    TOP = "TOP"
    # --- 男装泳衣(§2.2,PRD v2 补齐 —— 此前男装只有 SWIM_SHORTS 一个值) ---
    SWIM_SHORTS = "SWIM_SHORTS"    # 平角泳裤/沙滩裤;中性沙滩裤也用它
    BOARDSHORTS = "BOARDSHORTS"    # 冲浪裤(长款、带口袋、抽绳腰)
    SWIM_BRIEFS = "SWIM_BRIEFS"    # 三角泳裤
    JAMMERS = "JAMMERS"            # 及膝竞速裤
    SWIM_TRUNKS_SET = "SWIM_TRUNKS_SET"  # 泳裤套装(裤 + 上衣)
    # --- 中性款(§2.2) ---
    RASH_GUARD = "RASH_GUARD"      # 防晒衣/水母衣
    OTHER = "OTHER"


class StrapType(StrEnum):
    """肩带样式。**女装结构词表**(PRD v2 §0.2:保留并标注受众)。

    受众适用性的执行点不在这里 —— 在 `attributes/registry.py` 的
    `applies_to`。枚举是全集,注册表决定哪个受众能问这个字段。
    """

    HALTER = "HALTER"
    SPAGHETTI = "SPAGHETTI"
    WIDE = "WIDE"
    STRAPLESS = "STRAPLESS"
    ONE_SHOULDER = "ONE_SHOULDER"
    CRISS_CROSS = "CRISS_CROSS"
    UNKNOWN = "UNKNOWN"


class NecklineType(StrEnum):
    """领型。**女装结构词表**(受众适用性见 `attributes/registry.py`)。"""

    V_NECK = "V_NECK"
    SCOOP = "SCOOP"
    SQUARE = "SQUARE"
    SWEETHEART = "SWEETHEART"
    HIGH_NECK = "HIGH_NECK"
    PLUNGE = "PLUNGE"
    BANDEAU = "BANDEAU"
    UNKNOWN = "UNKNOWN"


class BackStyle(StrEnum):
    """背部款式。**女装结构词表**(受众适用性见 `attributes/registry.py`)。"""

    OPEN_BACK = "OPEN_BACK"
    RACER_BACK = "RACER_BACK"
    CROSS_BACK = "CROSS_BACK"
    TIE_BACK = "TIE_BACK"
    FULL_BACK = "FULL_BACK"
    LACE_UP = "LACE_UP"
    UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------- 男装结构词表
#
# PRD v2 §18.4。与女装三张表(StrapType / NecklineType / BackStyle)对称:
# 枚举是全集,受众适用性由 `attributes/registry.py` 的 `applies_to` 决定。
# 男装最容易被生成改掉的是**腰头和裤长**(§14.2),这两张表因此排在最前。


class WaistbandType(StrEnum):
    """腰头形制。女装那张表里的"腰线"指连体泳衣的腰部收拢线,
    和这里的腰头是两回事,**不能复用同一个检查项名字**(§14.2)。"""

    ELASTIC = "ELASTIC"                      # 松紧
    DRAWSTRING = "DRAWSTRING"                # 抽绳
    ELASTIC_DRAWSTRING = "ELASTIC_DRAWSTRING"  # 松紧 + 抽绳
    BUTTON = "BUTTON"                        # 纽扣
    VELCRO = "VELCRO"                        # 魔术贴
    UNKNOWN = "UNKNOWN"


class InseamLength(StrEnum):
    """裤长区间。裤长明显改变会改到另一个品类的长度区间(§14.4)。"""

    SHORT = "SHORT"
    MID = "MID"
    LONG = "LONG"
    KNEE_LENGTH = "KNEE_LENGTH"
    UNKNOWN = "UNKNOWN"


class LinerType(StrEnum):
    """内衬。平铺图和模特图通常都看不见内衬 —— 注册表里这个字段是
    `visual=False`(§18.4:与 material 同一档,只能来自商品库或人工)。"""

    MESH_LINER = "MESH_LINER"
    BRIEF_LINER = "BRIEF_LINER"
    NO_LINER = "NO_LINER"
    UNKNOWN = "UNKNOWN"


class PocketConfig(StrEnum):
    NO_POCKETS = "NO_POCKETS"
    SIDE = "SIDE"
    BACK = "BACK"
    SIDE_AND_BACK = "SIDE_AND_BACK"
    UNKNOWN = "UNKNOWN"


class FitType(StrEnum):
    SLIM = "SLIM"        # 修身
    REGULAR = "REGULAR"  # 常规
    LOOSE = "LOOSE"      # 宽松
    UNKNOWN = "UNKNOWN"


class ClosureType(StrEnum):
    """开合结构。中性款(防晒衣/水母衣)的拉链检查项也用它(§14.3)。"""

    NONE = "NONE"
    ZIPPER = "ZIPPER"
    BUTTON = "BUTTON"
    VELCRO = "VELCRO"
    UNKNOWN = "UNKNOWN"


class PatternType(StrEnum):
    SOLID = "SOLID"
    STRIPE = "STRIPE"
    FLORAL = "FLORAL"
    ANIMAL = "ANIMAL"
    GEOMETRIC = "GEOMETRIC"
    TIE_DYE = "TIE_DYE"
    COLOR_BLOCK = "COLOR_BLOCK"
    PRINT_LOGO = "PRINT_LOGO"
    OTHER = "OTHER"


class AuditAction(StrEnum):
    CREATE = "CREATE"
    UPDATE = "UPDATE"
    DELETE = "DELETE"
    UPLOAD = "UPLOAD"
    IMPORT = "IMPORT"
    #: 读取。**只给"读这件事本身要留痕"的少数路径用**(目前只有批量上架
    #: 文件的下载,评审第 6 条要求生成与下载各记一次)。
    #: 普通 GET 不许写审计:那会让审计表被浏览器预取灌满,
    #: 真正的操作记录淹没在里面。
    READ = "READ"


# ==========================================================
# 阶段 3:生成任务
# ==========================================================


class TaskStatus(StrEnum):
    """生成任务状态。合法转移表定义在 app/workflows/state_machine.py。

    存库用字符串而非数据库枚举:状态机迭代频繁,加一个状态不该触发一次迁移。
    校验责任完全落在状态机,写库前必须过 TaskStateMachine.assert_can。
    """

    CREATED = "CREATED"
    QUEUED = "QUEUED"
    PREPROCESSING = "PREPROCESSING"
    SUBMITTING = "SUBMITTING"
    PROVIDER_RUNNING = "PROVIDER_RUNNING"
    DOWNLOADING = "DOWNLOADING"
    SCORING = "SCORING"
    REGENERATING = "REGENERATING"
    AUTO_APPROVED = "AUTO_APPROVED"
    AUTO_REJECTED = "AUTO_REJECTED"
    MANUAL_REVIEW = "MANUAL_REVIEW"
    MANUALLY_APPROVED = "MANUALLY_APPROVED"
    MANUALLY_REJECTED = "MANUALLY_REJECTED"
    FORMATTING = "FORMATTING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class RoutingMode(StrEnum):
    MANUAL = "MANUAL"      # 用户指定 Provider
    PRIORITY = "PRIORITY"  # 按配置顺序取第一个已配置的
    FALLBACK = "FALLBACK"  # 失败后自动切换,阶段 5 实现


class AttemptStatus(StrEnum):
    """单次 Provider 调用的结果。一次调用对应一条 GenerationAttempt。"""

    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"
    CANCELLED = "CANCELLED"


class CandidateStatus(StrEnum):
    """候选图状态。阶段 3 只到 DOWNLOADED,评分与淘汰属于阶段 4。"""

    PENDING = "PENDING"
    DOWNLOADED = "DOWNLOADED"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    SCORED = "SCORED"
    SELECTED = "SELECTED"
    REJECTED = "REJECTED"


class RegenerationReason(StrEnum):
    """重生原因。需求第九章:每次重生必须记录原因和修复策略。"""

    PROVIDER_ERROR = "PROVIDER_ERROR"
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    DOWNLOAD_FAILED = "DOWNLOAD_FAILED"
    NO_CANDIDATE_RETURNED = "NO_CANDIDATE_RETURNED"
    LOW_SCORE = "LOW_SCORE"            # 阶段 4 起使用
    HARD_FAIL = "HARD_FAIL"            # 阶段 4 起使用
    MANUAL_REQUEST = "MANUAL_REQUEST"


class ModelPose(StrEnum):
    STANDING_FRONT = "STANDING_FRONT"
    STANDING_BACK = "STANDING_BACK"
    THREE_QUARTER = "THREE_QUARTER"
    SITTING = "SITTING"
    WALKING = "WALKING"
    UNKNOWN = "UNKNOWN"


class ModelBodyType(StrEnum):
    """模特体型。**枚举是全集,筛选和展示按受众过滤**(PRD v2 §10.4)。

    `CURVY` 只在女装语境下有意义;`REGULAR` / `MUSCULAR` 是男装词。
    "给男装模特打上 CURVY"不应该是一个可以做到的操作 —— 执行点在
    `BODY_TYPES_FOR_AUDIENCE` + `model_template_service.create_template`
    的校验,不在这张全集表上。
    """

    SLIM = "SLIM"
    STRAIGHT = "STRAIGHT"
    CURVY = "CURVY"
    PLUS = "PLUS"
    ATHLETIC = "ATHLETIC"
    REGULAR = "REGULAR"    # 男装词表(PRD v2 §10.4)
    MUSCULAR = "MUSCULAR"  # 男装词表(PRD v2 §10.4)
    UNKNOWN = "UNKNOWN"


#: 受众 -> 可选体型(§10.4)。UNKNOWN 不在表里:它是"没填",两边都允许。
#: UNISEX 模特不存在 —— 模特本人总有受众;UNISEX 是**商品**的取值,
#: 含义是两种受众的模特都可以穿(§10.5),所以这张表没有 UNISEX 行。
BODY_TYPES_FOR_AUDIENCE: dict[Audience, tuple[ModelBodyType, ...]] = {
    Audience.WOMEN: (
        ModelBodyType.SLIM,
        ModelBodyType.STRAIGHT,
        ModelBodyType.CURVY,
        ModelBodyType.ATHLETIC,
        ModelBodyType.PLUS,
    ),
    Audience.MEN: (
        ModelBodyType.SLIM,
        ModelBodyType.REGULAR,
        ModelBodyType.ATHLETIC,
        ModelBodyType.MUSCULAR,
        ModelBodyType.PLUS,
    ),
}


class ModelLicenseStatus(StrEnum):
    """模特授权状态(PRD v2 §11)。

    `UNVERIFIED` 是迁移回填的默认值:0030 之前入库的模特没有任何授权字段,
    把它们标成 LICENSED 等于替不存在的授权文件签字。UNVERIFIED 的模特
    **仍可创建任务**(否则存量全停摆),但会出现在管理员的待补清单里;
    EXPIRED / REVOKED 则硬阻断新任务(§11.3),执行点在
    `model_template_service.assert_usable`。
    """

    UNVERIFIED = "UNVERIFIED"
    LICENSED = "LICENSED"
    EXPIRED = "EXPIRED"
    REVOKED = "REVOKED"


class GenerationPlanStatus(StrEnum):
    """生成方案状态(PRD v3.1 §4.7)。

    `ARCHIVED` 而不是删行:方案指纹进生成幂等键,删掉一份方案之后,
    那批图"当初是按什么参数生成的"就没有了答案 —— 而图片集过期判定
    (§8.1「更换生成方案」那一行)比的正是这个指纹。

    `DRAFT` 与 `ACTIVE` 分开,是因为 §4.7 的唯一约束是
    `UNIQUE(spu_id, COALESCE(color_variant_id,''))` —— 每层当前生效**一份**。
    编辑一份正在用的方案会当场改掉幂等键,于是在编辑器里改到一半的参数
    会立刻影响下一次创建任务。DRAFT 让"改"和"生效"分成两步。
    """

    DRAFT = "DRAFT"
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ImageAngle(StrEnum):
    """生成方案要覆盖的拍摄角度(§4.7 `angles_json` 的键)。

    ## 为什么它不是 `MediaRole`,也不是 `ModelPose`

    三者回答三个不同的问题,合并任何两个都会丢掉一个答案:

        MediaRole    这张图**是什么**(正面图 / 尺码表 / 包装)—— 素材层的属性
        ModelPose    模特**摆什么姿势** —— 模特模板的属性,一个模板一个姿势
        ImageAngle   这个颜色**还缺哪一张** —— 图片集的验收口径

    §6.5 明写「必要角度按 GenerationPlan 的 `angles_json` 配置验收,
    **不是只数总张数**」。数总张数的写法在多颜色下会给出错误的绿灯:
    一个颜色出了 4 张正面图,总数达标,而背面一张都没有。
    """

    FRONT = "FRONT"
    BACK = "BACK"
    SIDE = "SIDE"
    THREE_QUARTER = "THREE_QUARTER"
    DETAIL = "DETAIL"
    FLAT_LAY = "FLAT_LAY"


#: 需求第九章默认规则
DEFAULT_CANDIDATE_COUNT = 4
DEFAULT_MAX_ROUNDS = 3


# ==========================================================
# 阶段 4:评分与自动决策
# ==========================================================


class Grade(StrEnum):
    """候选图分档(需求第十二章)。

    顺序有意义:A 最好,D 最差。`worst_of()` 依赖这个顺序做"取更差者",
    因为分档规则里既有分数区间也有问题严重度上限,两者取交集。
    """

    A = "A"
    B = "B"
    C = "C"
    D = "D"

    @property
    def rank(self) -> int:
        return "ABCD".index(self.value)


def worst_of(*grades: Grade) -> Grade:
    """取最差的一档。分数算出 A 但存在明显问题时,必须被压到 C。"""
    return max(grades, key=lambda g: g.rank)


class HardFailCode(StrEnum):
    """硬错误(需求第十一章)。

    出现任意一条,不论总分多高都不能自动通过。
    硬错误只表示**当前候选图**失败,不代表整个任务失败 ——
    未耗尽轮次时应自动淘汰并重生,而不是立刻交给人工。
    """

    GARMENT_WRONG = "GARMENT_WRONG"
    GARMENT_PART_MISSING = "GARMENT_PART_MISSING"
    GARMENT_PART_ADDED = "GARMENT_PART_ADDED"
    # --- 女装专属结构码(PRD v2 §14.4:男装规则包里不启用) ---
    STRAP_CHANGED = "STRAP_CHANGED"
    NECKLINE_CHANGED = "NECKLINE_CHANGED"
    BACK_STYLE_CHANGED = "BACK_STYLE_CHANGED"
    COVERAGE_CHANGED = "COVERAGE_CHANGED"
    COLOR_VARIANT_WRONG = "COLOR_VARIANT_WRONG"
    PATTERN_DISTORTED = "PATTERN_DISTORTED"
    LOGO_OR_TEXT_CHANGED = "LOGO_OR_TEXT_CHANGED"
    SKU_IMAGE_MISMATCH = "SKU_IMAGE_MISMATCH"
    ANATOMY_HARD_ERROR = "ANATOMY_HARD_ERROR"
    FACE_HARD_ERROR = "FACE_HARD_ERROR"
    IMAGE_CORRUPTED = "IMAGE_CORRUPTED"
    PRODUCT_SEVERELY_OCCLUDED = "PRODUCT_SEVERELY_OCCLUDED"
    # --- 男装专属结构码(PRD v2 §14.4:女装规则包里不启用) ---
    WAISTBAND_CHANGED = "WAISTBAND_CHANGED"  # 腰头形制被改(松紧变抽绳、抽绳消失等)
    INSEAM_CHANGED = "INSEAM_CHANGED"        # 裤长改到了另一个品类的长度区间
    LINER_CHANGED = "LINER_CHANGED"          # 内衬被增删或明显改变
    # --- 生成缺陷,不是"商品被换了"(PRD v3.1 §6.5 事实一致性清单末两项) ---
    #
    # 这两条**不能**用 GARMENT_WRONG 顶替,理由和下面 AUDIENCE_MISMATCH 那条同源:
    # GARMENT_WRONG 的语义是"模型画的不是这件衣服",对策是换 seed / 换 Provider;
    # 这两条的语义是"衣服是对的,渲染塌了",对策是**换模特模板或降低融合强度** ——
    # 而 `repair.py` 的定向重生策略正是按码选方向的。混进 GARMENT_WRONG 里,
    # 一件左右不对称的正确泳衣会被反复换 seed 重生,每一轮都是真实付费调用。
    #
    # §6.5 清单里的九项,前七项在上面都有对应码,只有这两项没有 —— 那不是
    # 因为它们不重要,是因为受众前时代的码表是按"商品还原度"一根轴列的。
    #: 设计上对称的部件被生成成左右不对称(单边肩带、两侧裁片不等宽)
    ASYMMETRY_INTRODUCED = "ASYMMETRY_INTRODUCED"
    #: 融合缺陷:衣服与皮肤/背景粘连、边缘熔化、图案穿过身体边界
    FUSION_DEFECT = "FUSION_DEFECT"

    # --- 受众错配(§12.4) ---
    #
    # 含义是"候选图中**穿着者**的呈现与商品受众不符"。
    # **不能用 GARMENT_WRONG 顶替**:那条码的语义是"商品换了",而受众错配时
    # 商品往往是对的 —— 错的是穿的人。两者混在一起会让重生策略选错方向:
    # GARMENT_WRONG 该换 seed / 换 Provider,受众错配该**换模特并核对商品受众**。
    # 每个受众的规则包里都启用它(§18.5 两份示例都有)。
    AUDIENCE_MISMATCH = "AUDIENCE_MISMATCH"


#: 快速检查(需求第十三章)只查这几条 —— 它们不依赖细粒度打分,肉眼级别就能判定
QUICK_CHECK_HARD_FAIL_CODES: frozenset[HardFailCode] = frozenset(
    {
        HardFailCode.GARMENT_WRONG,
        HardFailCode.IMAGE_CORRUPTED,
        HardFailCode.ANATOMY_HARD_ERROR,
        HardFailCode.PRODUCT_SEVERELY_OCCLUDED,
        HardFailCode.COLOR_VARIANT_WRONG,
    }
)


class ProblemSeverity(StrEnum):
    """问题严重度。决定分档上限,与总分共同作用。

    MINOR    换 seed 或调提示词就有机会解决 -> 最好 B
    MAJOR    需要换模特模板甚至换 Provider   -> 最好 C
    CRITICAL 硬错误                          -> 只能 D
    """

    MINOR = "MINOR"
    MAJOR = "MAJOR"
    CRITICAL = "CRITICAL"


#: 严重度 -> 该候选图能拿到的最好分档
SEVERITY_GRADE_CAP: dict[ProblemSeverity, Grade] = {
    ProblemSeverity.MINOR: Grade.B,
    ProblemSeverity.MAJOR: Grade.C,
    ProblemSeverity.CRITICAL: Grade.D,
}


class RecommendedAction(StrEnum):
    """分档结论对应的动作(需求第十二章)。

    注意 REJECT 的含义是"淘汰这张候选图",不是"任务失败"。
    任务级别的去向由 `RoundDecision` 汇总本轮全部候选后决定。
    """

    AUTO_APPROVE = "AUTO_APPROVE"          # A 档
    REGENERATE_TUNED = "REGENERATE_TUNED"  # B 档:定向重生
    REGENERATE_RESEED = "REGENERATE_RESEED"  # C 档:换 seed / 模特,必要时换 Provider
    REJECT = "REJECT"                      # D 档:当前候选直接淘汰
    MANUAL_REVIEW = "MANUAL_REVIEW"


#: 分档 -> 默认动作
GRADE_ACTION: dict[Grade, RecommendedAction] = {
    Grade.A: RecommendedAction.AUTO_APPROVE,
    Grade.B: RecommendedAction.REGENERATE_TUNED,
    Grade.C: RecommendedAction.REGENERATE_RESEED,
    Grade.D: RecommendedAction.REJECT,
}


class EvaluationOutcome(StrEnum):
    """一次评分尝试的结局。

    单独一套枚举,而不是复用 CandidateStatus:候选图的状态说的是\"这张图现在怎么样\",
    这里说的是\"我们向评分器发的这一次请求怎么样\"。两者的失败原因完全不重叠 ——
    评分器限流和候选图被淘汰是两回事,混在一个字段里就没法回答
    \"上周有多少次评分是因为限流没做成的\"。
    """

    SUCCEEDED = "SUCCEEDED"
    #: 模型返回了东西,但不符合约定的结构(缺维度、非 JSON、多余维度……)
    PARSE_FAILED = "PARSE_FAILED"
    #: 调不通:限流、超时、鉴权、额度、内容安全拒绝
    PROVIDER_ERROR = "PROVIDER_ERROR"
    #: 评分器根本用不了(没配置且不允许退回 Mock)
    UNAVAILABLE = "UNAVAILABLE"
    #: 整轮评分失败,已安排重新评分(不重新生成图片)
    ROUND_RETRY_SCHEDULED = "ROUND_RETRY_SCHEDULED"


class EvaluationDepth(StrEnum):
    """评分深度(需求第十三章)。

    预排序后只对前两名做完整评分,后两名做快速硬错误检查,以此压低评分成本。
    """

    FULL = "FULL"
    QUICK = "QUICK"


class ReviewStatus(StrEnum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    REGENERATION_REQUESTED = "REGENERATION_REQUESTED"
    CANCELLED = "CANCELLED"


OPEN_REVIEW_STATUSES: frozenset[ReviewStatus] = frozenset({ReviewStatus.PENDING})


class ReviewReason(StrEnum):
    """任务为什么进了人工审核队列。

    审核对象是**商品任务**,不是每一张低分候选图(需求第十二章)。
    """

    ROUNDS_EXHAUSTED = "ROUNDS_EXHAUSTED"        # 轮次用尽仍无 A 档
    PROVIDER_EXHAUSTED = "PROVIDER_EXHAUSTED"    # 可用 Provider 都试过了
    REQUIRES_HUMAN_ERROR = "REQUIRES_HUMAN_ERROR"  # 鉴权/内容安全类错误
    SPOT_CHECK = "SPOT_CHECK"                    # A 档随机抽检,不阻塞任务
    MANUAL_REQUEST = "MANUAL_REQUEST"


#: 抽检项只是复核,不挡住任务继续走后续流程
NON_BLOCKING_REVIEW_REASONS: frozenset[ReviewReason] = frozenset({ReviewReason.SPOT_CHECK})


# ==========================================================
# 阶段 6:网站图片输出
# ==========================================================


class OutputPurpose(StrEnum):
    """最终图片的用途(需求第十五章)。

    用途而不是尺寸做枚举:网站改版会改像素,但"详情页要用哪张"这件事不会变。
    前端按用途取图,尺寸调整只需改规格表,不用改调用方。
    """

    ORIGINAL = "ORIGINAL"      # 原始高质量版本,不缩放
    DETAIL = "DETAIL"          # 商品详情页
    THUMBNAIL = "THUMBNAIL"    # 商品列表缩略图
    MOBILE = "MOBILE"          # 移动端
    SQUARE = "SQUARE"          # 正方形(社媒与网格位)


class FitMode(StrEnum):
    """缩放方式。

    NONE     不动,只重新编码
    CONTAIN  等比缩到框内,短边留空,输出尺寸不固定
    PAD      等比缩到框内再补边到精确尺寸,输出尺寸固定
    """

    NONE = "NONE"
    CONTAIN = "CONTAIN"
    PAD = "PAD"


# ==========================================================
# 代码评审修复引入的领域常量
# ==========================================================

#: 能当「商品图」用的素材角色。详情图、背景图、模特图都不行 ——
#: 拿一张吊牌特写去做试穿,Provider 会认认真真生成一个穿着吊牌的模特。
GARMENT_ROLES: tuple[str, ...] = (
    AssetType.GARMENT_CUTOUT.value,
    AssetType.GARMENT_FRONT.value,
)

#: 提交阶段网络超时:请求可能已经被 Provider 受理,结果未知。
#: 单独一个错误码,是因为处理方式和普通失败不同 —— 不允许自动重试,否则重复计费。
SUBMIT_UNKNOWN_CODE = "SUBMIT_RESULT_UNKNOWN"

#: A45-batch12-2 / EX-03:**外部任务确实存在,只是这一次没把结果取回来。**
#:
#: 与 `SUBMIT_RESULT_UNKNOWN` 的区别是**确定性**,不是阶段:那个码的意思是
#: "不知道对方受理了没有",这个码的意思是"知道受理了,external_task_id 就在
#: 任务上,只是 get_status / fetch_results 这一次失败了"。
#:
#: 分开是因为两者该走的恢复路径相反。混用的代价在 EX-03 里已经发生过:
#: 状态查询超时落成普通 `NETWORK_TIMEOUT`,`retry_task()` 走通用分支
#: **清掉 external_task_id 回 QUEUED**,下一个 worker 重新 `submit()` ——
#: 第一次那笔生成还在对方那里跑着、钱已经花了,而我们主动丢掉了它的身份。
#:
#: 带这个码的任务重试时必须保留 external_task_id,从 `PROVIDER_RUNNING`
#: 接着查、接着下载(见 `generation_service.can_resume_provider_results`)。
PROVIDER_RESULT_PENDING_CODE = "PROVIDER_RESULT_PENDING"

#: A45-batch12-2 / EX-05:断点续跑依赖的**源文件本身**没了或读不出图。
#:
#: 两个码分开,是因为运营的下一步动作不同:丢了要问存储/清理脚本,
#: 坏了要看是不是写入截断。但它们对**重试**的含义相同 —— 见下面那个集合。
SOURCE_ASSET_MISSING_CODE = "SOURCE_ASSET_MISSING"
SOURCE_ASSET_CORRUPT_CODE = "SOURCE_ASSET_CORRUPT"

#: 带这两个码的任务**不许走普通重试**。
#:
#: `can_resume_formatting()` 的判据只有\"库里有 SELECTED 行、且没有成品图\",
#: 它看不见那一行指着的对象还在不在。于是文件没了之后重试会一遍遍回到
#: FORMATTING 读同一个坏路径:`FAILED -> FORMATTING -> FAILED` 无限循环,
#: 永远产不出成品图,而界面上每一次都显示\"正在出图\"。
#:
#: 但**也不能**让它掉进通用重试分支 —— 那条分支会清 external_task_id 回
#: QUEUED 重新生成,而重新生成要再花一次 Provider 的钱。所以正确的处理是
#: 挡住,并要求人显式选择:换一张候选,或者带 force + 说明重新生成。
SOURCE_ASSET_BROKEN_CODES: frozenset[str] = frozenset(
    {SOURCE_ASSET_MISSING_CODE, SOURCE_ASSET_CORRUPT_CODE}
)

#: 任务已落库但没能进队列。由 find_stranded / requeue_stranded 负责捞回来。
DISPATCH_FAILED_CODE = "DISPATCH_FAILED"

#: worker 在中途消失,任务卡在某个进行中状态且长时间没有心跳。
#: 由 reap_stalled 落成 FAILED,走正常重试路径。
WORKER_STALLED_CODE = "WORKER_STALLED"

#: Outbox 连续投递失败超过上限、已放弃自动重试。
#: 和 DISPATCH_FAILED 分开:那个是\"这次没投出去,还会再试\",
#: 这个是\"不再试了,任务已经落成失败\",两者要给人的动作完全不同。
DISPATCH_ABANDONED_CODE = "DISPATCH_ABANDONED"


class DispatchStatus(StrEnum):
    """派发意图(Outbox)的状态。

    这张表记的不是"任务",而是"我们打算把这个任务投进队列"这件事本身。
    它和业务数据写在同一个事务里,因此不存在"业务提交了、意图丢了"的窗口。
    """

    #: 等待投递。relay 会反复尝试,直到成功或超过上限。
    PENDING = "PENDING"
    #: 已经投进队列。重复投递是安全的(worker 侧有原子认领),所以这里不追求恰好一次。
    DISPATCHED = "DISPATCHED"
    #: 连续失败超过上限,不再自动重试,需要人介入。
    ABANDONED = "ABANDONED"
    #: 已被一次新的重试取代,不该再投。
    #:
    #: 派发放弃之后业务任务已经落成 FAILED,那条意图描述的是"上一次打算投递它"。
    #: 把它拨回 PENDING 重投是没用的:worker 收到消息会发现任务不在可认领状态,
    #: 原子认领失败直接跳过。所以重新激活的正确做法是走 retry_task 产生一条
    #: **新的**意图,旧的标成这个状态归档 —— 既留下痕迹,又不会再被 relay 捡起来。
    SUPERSEDED = "SUPERSEDED"


#: 派发意图的来源。出问题时能一眼看出是哪条路径在漏投。
class DispatchReason(StrEnum):
    CREATE = "create"
    RETRY = "retry"
    NEXT_ROUND = "next_round"
    REVIEW_REGENERATE = "review_regenerate"
    REQUEUE = "requeue"


# ==========================================================
# 阶段 9-13(M0 契约):素材 / 属性 / 刊登 / 渠道
#
# 这一段只有枚举,没有表也没有服务 —— M0 的产物就是契约本身。
# 放在同一个文件里而不是各层自己定义,是因为 `app/attributes/registry.py`
# 要用它们做静态对齐断言:注册表里声明 ENUM 类型的字段,取值必须
# 逐一落在这里的某个枚举上,对不上 CI 直接红。两处各写一份就没法断言了。
# ==========================================================


class MediaSource(StrEnum):
    """素材从哪来。**与 `MediaRole` 正交。**

    v1 把「是不是 AI 生成的」和「它是正面图还是尺码表」混在 `AssetType` /
    `OutputAsset.purpose` 里,后果是 AI 图天然只能当成品图,而供应商推来的
    正面图和人工上传的正面图要走两条不同的代码路径。这两件事必须分开问。
    """

    MANUAL_UPLOAD = "MANUAL_UPLOAD"      # 人工在后台上传
    IMPORTED_URL = "IMPORTED_URL"        # 从外链导入
    SUPPLIER_FEED = "SUPPLIER_FEED"      # 供应商推送
    AI_GENERATED = "AI_GENERATED"        # 本系统生成链路产出
    PLATFORM_SYNC = "PLATFORM_SYNC"      # 从平台回抓


class MediaRole(StrEnum):
    """这张图在商品里扮演什么角色。与来源无关。"""

    PRODUCT_FRONT = "PRODUCT_FRONT"
    PRODUCT_BACK = "PRODUCT_BACK"
    MODEL_FRONT = "MODEL_FRONT"
    MODEL_BACK = "MODEL_BACK"
    DETAIL = "DETAIL"
    SIZE_CHART = "SIZE_CHART"
    FLAT_LAY = "FLAT_LAY"
    PACKAGING = "PACKAGING"
    OTHER = "OTHER"


class EvidenceClass(StrEnum):
    """这张图能不能当作**关于本商品的证据**(§4.8 / §5.1)。

    与 `MediaSource`、`MediaRole` 同为正交的第三根轴,但和那两根不同:
    **它不是被赋值的,是被派生的。**唯一写入点是
    `app.media.evidence_rules.derive_evidence_class`,路由层与脚本一律不许直写。

    ## 为什么要有这一根轴

    §5.1 的抽取输入白名单需要回答「这张图算不算证据」。今天回答这个问题的是
    `usable_assets()` 里的 `status = READY` —— 而 READY 的含义是「这个文件是好的」,
    不是「这张图说的是这件商品的事」。一张刚生成完、下载成功、尺寸合规的 AI 候选图
    完全满足 READY,于是它会进识别输入并**产生一次真实的付费调用**,
    而它对属性证据的贡献必然是零(硬规矩 6:AI 图不能是任何属性的唯一来源)。

    ## 为什么是派生而不是存的时候标

    PRD 风险表 D3 记着这件事:设计成独立赋值的存储列时,它会和 `source` /
    溯源列漂移,而**一张 AI 图被误标成 `PRODUCT_EVIDENCE` 就穿透了整个白名单**。
    派生 + 单写入点 + 库级 CHECK 三件套,是让「标错」这个动作没有落点。

    ## 四个取值指向四种不同的下游待遇,不要合并

        PRODUCT_EVIDENCE     可以进识别输入。**只有这一个可以。**
        GENERATED_RESULT     本系统生成链路的产物。可以进图片集、进发布,
                             但永远不进识别 —— 拿它识别等于让模型给自己的输出打分
        REFERENCE_ONLY       可以看,但它讲的不是这件商品的事(模特参考图、
                             来路不明的外链)。既不进识别,也不能直接发布
        CHANNEL_DERIVATIVE   从平台回抓的。它讲的是「平台上现在挂着什么」,
                             那是渠道现状而不是商品事实;拿它识别会把一次
                             上架错误在下一轮识别里洗成「确认过的事实」
    """

    PRODUCT_EVIDENCE = "PRODUCT_EVIDENCE"
    GENERATED_RESULT = "GENERATED_RESULT"
    REFERENCE_ONLY = "REFERENCE_ONLY"
    CHANNEL_DERIVATIVE = "CHANNEL_DERIVATIVE"


class RoleSource(StrEnum):
    """`MediaRole` 是谁定的。

    模型猜的角色不能直接用于主图位(§7.3)—— 主图放错是消费者第一眼就看到的错误,
    而角色识别恰恰是最容易在平铺图和模特图之间搞混的一类判断。
    """

    HUMAN = "HUMAN"      # 人工指定
    RULE = "RULE"        # 规则推断(文件名、上传位)
    MODEL = "MODEL"      # 模型识别
    UNSET = "UNSET"      # 还没定


class MediaStatus(StrEnum):
    """素材生命周期。

    `QUARANTINED` 单独成态而不是直接删:图片合规预检(水印、logo、露出)
    有假阳性,删掉就找不回来了。隔离的素材不参与上架,但可以人工放行。
    """

    PENDING = "PENDING"
    READY = "READY"
    QUARANTINED = "QUARANTINED"
    FAILED = "FAILED"
    DELETED = "DELETED"


class OwnerType(StrEnum):
    """属性挂在哪一层。

    只用 `product_id` 表达不了真实的商品结构:品类和材质属于 SPU,颜色属于变体,
    尺码和 GTIN 属于 SKU,平台特有属性属于某一次刊登。等接 SHEIN 的 SPU-SKU
    结构时这个维度必然要加,现在加比那时候加便宜。
    """

    SPU = "SPU"
    PRODUCT = "PRODUCT"
    VARIANT = "VARIANT"
    SKU = "SKU"
    CHANNEL = "CHANNEL"


class AttributeSource(StrEnum):
    """属性值的来源。合并时的优先级见 `extractors/merge.py`。"""

    MANUAL = "MANUAL"          # 人工确认。永不被自动覆盖
    EXTRACTED = "EXTRACTED"    # 图片识别
    IMPORTED = "IMPORTED"      # 导入的结构化数据
    SUPPLIER = "SUPPLIER"      # 供应商结构化字段(不是描述文本)
    DEFAULT = "DEFAULT"        # 类目默认值
    DERIVED = "DERIVED"        # 由其它字段推导


class MissingReason(StrEnum):
    """识别时"这个字段为什么没有值"(PRD v3.1 §4.5,A45-batch14)。

    落在 `attribute_evidence.missing_reason` 上:**落 evidence 不落 value**。
    "未知"不作为确定值写入 —— 一条 value 为 NULL、reason 说清缘由的证据,
    和一个低置信度的猜测值,在下游是两件事:前者告诉运营"补一张背面图"
    或"去要供应商数据",后者会参与合并投票。

    三个取值指向三个不同的动作,不要合并:

        INSUFFICIENT_EVIDENCE       这张图上看不清 —— 换一张更清楚的图
        NOT_VISUALLY_DETERMINABLE   这个角度/这类图判断不了 —— 补对应角度的图
        AWAITING_SUPPLIER_DATA      看图永远判断不了(材质克重一类)——
                                    等供应商结构化数据。模型自己产不出这一个
                                    (非视觉字段根本不进识别 Schema),
                                    留着是给导入/人工标记用的
    """

    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    NOT_VISUALLY_DETERMINABLE = "NOT_VISUALLY_DETERMINABLE"
    AWAITING_SUPPLIER_DATA = "AWAITING_SUPPLIER_DATA"


class AttributeStatus(StrEnum):
    """属性值的采信程度。

    `CANDIDATE` 与 `SUGGESTED` 的区别是这一版的关键:前者是「留了证据但不采信」
    (未校准、置信度过低、模型说看不清),后者是「够格进人工确认队列」。
    混成一个的话,未校准字段会和低分字段一起涌进待确认列表,人只会全部忽略。
    """

    CANDIDATE = "CANDIDATE"
    SUGGESTED = "SUGGESTED"
    CONFIRMED = "CONFIRMED"
    CONFLICT = "CONFLICT"
    REJECTED = "REJECTED"
    SUPERSEDED = "SUPERSEDED"


class ImageSetStatus(StrEnum):
    """图片集状态。发布只读 `APPROVED`。

    `REJECTED` 是 A10 加的:快审里"退回"一件之后,系统必须记得这件被人退回过。
    退回若只落一条审计,判定层看不见它 —— 图片集回到 DRAFT、校验依旧通过,
    于是下一步又变成"等待批准",那件商品立刻回到快审队列排在原处。
    运营退回它、它自己走回来,退回这个动作等于不存在。

    加这个值**不需要迁移**:`status` 是 `String(16)` 而不是数据库枚举
    (和 `TaskStatus` 那条注释同一个理由——状态会迭代,加一个不该触发一次迁移)。
    """

    DRAFT = "DRAFT"
    PENDING_REVIEW = "PENDING_REVIEW"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    ARCHIVED = "ARCHIVED"


class CopyStatus(StrEnum):
    """渠道文案状态。"""

    DRAFT = "DRAFT"
    VALIDATED = "VALIDATED"
    REJECTED = "REJECTED"
    APPROVED = "APPROVED"
    ARCHIVED = "ARCHIVED"


class DraftStatus(StrEnum):
    """刊登草稿状态。

    `STALE` 是纯 dataclass 方案完全无法表达的一类问题:草稿构建之后,
    属性被改了、图片集出了新版本、文案重新生成了 —— 草稿里的快照全都过期了,
    而它看起来仍然是「校验通过」的。`STALE` 草稿不允许导出与提交。
    """

    BUILDING = "BUILDING"
    VALIDATED = "VALIDATED"
    INVALID = "INVALID"
    STALE = "STALE"
    APPROVED = "APPROVED"
    ARCHIVED = "ARCHIVED"


class PlatformStatus(StrEnum):
    """平台侧状态(OPS-REVIEW P1:驳回回流)。

    这是**系统外发生的事**在系统内的记录:文件导出之后,运营把它传到平台,
    平台审核、上架或者驳回 —— 这些动作系统看不见,只能由运营手工录入。
    首期没有平台 API 对接,所以取值刻意少:够把"驳回件从系统外表格收回工作台"
    就行,不试图复刻各平台自己的十几种审核细分状态。

    `REJECTED` 不允许手工直接设置:它只能由「记录驳回」产生,离开它则要求
    没有未解决的驳回记录 —— 否则状态和驳回台账会各说各话。
    """

    NONE = "NONE"            # 还没传平台(导出前 / 导出后未上传)
    SUBMITTED = "SUBMITTED"  # 已上传平台,等审核
    LIVE = "LIVE"            # 平台审核通过,已上架
    REJECTED = "REJECTED"    # 平台驳回,有待处理的驳回记录


class RejectionStatus(StrEnum):
    """一条平台驳回记录的生命周期(评审第 7 条)。

    原来只有 OPEN / RESOLVED 两态,而 RESOLVED **只需要运营勾一个框或者
    写一句备注**就能到达 —— 系统没有看到任何修改、新导出或指纹变化。
    于是"已解决"这个审计结论证明不了商品真的被修过,驳回台账退化成备忘录。

    中间态补上之后,三态各自对应一件**可核对的事实**:

        OPEN                  平台驳回了,还没人动
        FIXED_PENDING_EXPORT  运营声明改过了,但系统还没看到新的导出留痕。
                              这是"我改了"这句话的落点 —— 它是一次声明,
                              不是一个结论,所以它**仍然阻断**
        RESOLVED              系统自己看到了证据:驳回之后有新导出,
                              且指纹与被驳回的那一版不同。**只能这样到达**

    OPEN 与 FIXED_PENDING_EXPORT 都算未解决,都会以阻断问题出现在工作台。
    """

    OPEN = "OPEN"
    FIXED_PENDING_EXPORT = "FIXED_PENDING_EXPORT"
    RESOLVED = "RESOLVED"


#: 还没真正解决的驳回状态。**flow 层的阻断计数按它算**,不按 `== OPEN`。
#: 少了 FIXED_PENDING_EXPORT 的话,运营点一次"我改好了"就能让商品从
#: 阻断列表里消失 —— 那正是第 7 条要修掉的行为,只是换了个状态名。
UNRESOLVED_REJECTION_STATUSES: tuple[str, ...] = (
    RejectionStatus.OPEN.value,
    RejectionStatus.FIXED_PENDING_EXPORT.value,
)


class PublishOperation(StrEnum):
    """一次发布动作的类型。**进幂等键。**

    v1 的幂等键不含操作类型,于是「创建」和「更新」会算出同一个键 ——
    要么更新被当成重复创建吞掉,要么创建被当成更新放过去。
    """

    CREATE = "CREATE"
    UPDATE = "UPDATE"
    REPUBLISH = "REPUBLISH"
    DELIST = "DELIST"


class PublishStatus(StrEnum):
    """发布任务状态(§11.5)。

    两条硬约束写在这里,转移表在 M8 落地时必须遵守:

    **终态只有 `CANCELLED` 和 `ARCHIVED`。** `VALIDATION_FAILED` 不是终态 ——
    字段缺失或属性不合法,改了就能重来。`LISTED` 也不是终态,后面还有
    更新、暂停、下架。把它们标成终态的代价是将来改状态机要动所有断言。

    **干跑分支不通往 `SUBMITTED` / `LISTED`。** 干跑止于 `DRY_RUN_COMPLETED`,
    不产生平台 ID,不计入任何 Dashboard 指标。
    """

    DRAFT = "DRAFT"
    MAPPING = "MAPPING"
    VALIDATING = "VALIDATING"
    VALIDATED = "VALIDATED"
    VALIDATION_FAILED = "VALIDATION_FAILED"
    REVISING = "REVISING"
    # 干跑分支
    DRY_RUN_VALIDATED = "DRY_RUN_VALIDATED"
    DRY_RUN_COMPLETED = "DRY_RUN_COMPLETED"
    # 真实提交分支
    UPLOADING_IMAGES = "UPLOADING_IMAGES"
    SUBMITTING = "SUBMITTING"
    SUBMITTED = "SUBMITTED"
    SUBMIT_RESULT_UNKNOWN = "SUBMIT_RESULT_UNKNOWN"
    SUBMIT_FAILED = "SUBMIT_FAILED"
    PLATFORM_REVIEWING = "PLATFORM_REVIEWING"
    PLATFORM_REJECTED = "PLATFORM_REJECTED"
    LISTED = "LISTED"
    # 上架之后
    UPDATING = "UPDATING"
    UPDATE_FAILED = "UPDATE_FAILED"
    PAUSED = "PAUSED"
    #: 下架已排队、还没拿到平台的确认(7.3 节的下架分支,任务 16)。
    #: 7.3 节把下架画成 `DELIST_QUEUED → DELISTING → DELISTED` 三步,这里
    #: 只落一个 DELISTING —— **队列这件事已经有一个权威表示了**:
    #: `PublishOutbox` 行本身。再给它一个状态名,就有两处各自记录"排队了没有",
    #: 而两处迟早会不一致(outbox 已经 LEASED、listing 还写着 DELIST_QUEUED),
    #: 那时没人说得清该信哪个。创建那条分支已经是这么处理的:排队和在途
    #: 都记 SUBMITTING,队列状态去 outbox 上查。
    DELISTING = "DELISTING"
    DELISTED = "DELISTED"
    REPUBLISHING = "REPUBLISHING"
    # 终态
    CANCELLED = "CANCELLED"
    ARCHIVED = "ARCHIVED"


#: 发布状态机的终态。**只有两个。**
PUBLISH_TERMINAL_STATES: frozenset[str] = frozenset(
    {PublishStatus.CANCELLED.value, PublishStatus.ARCHIVED.value}
)

#: 干跑分支允许出现的状态。M8 的转移表必须保证 dry_run=true 的任务
#: 不存在通往 SUBMITTED / LISTED 的路径。
DRY_RUN_STATES: frozenset[str] = frozenset(
    {
        PublishStatus.DRAFT.value,
        PublishStatus.MAPPING.value,
        PublishStatus.VALIDATING.value,
        PublishStatus.VALIDATED.value,
        PublishStatus.VALIDATION_FAILED.value,
        PublishStatus.REVISING.value,
        PublishStatus.DRY_RUN_VALIDATED.value,
        PublishStatus.DRY_RUN_COMPLETED.value,
        PublishStatus.CANCELLED.value,
        PublishStatus.ARCHIVED.value,
    }
)


class RejectCategory(StrEnum):
    """平台驳回原因归类。决定驳回回流到哪条修复路径。"""

    IMAGE = "IMAGE"
    COPY = "COPY"
    ATTRIBUTE = "ATTRIBUTE"
    CATEGORY = "CATEGORY"
    COMPLIANCE = "COMPLIANCE"
    OTHER = "OTHER"


class ConsentScope(StrEnum):
    """素材授权范围。三个标志位相互独立,缺一不可(§12.1)。"""

    AI_PROCESSING = "AI_PROCESSING"
    CROSS_BORDER_TRANSFER = "CROSS_BORDER_TRANSFER"
    COMMERCIAL_PUBLISH = "COMMERCIAL_PUBLISH"
    DERIVATIVE_WORKS = "DERIVATIVE_WORKS"


class PublishAttemptStatus(StrEnum):
    """一次**提交尝试**的结局(方案 7.2 节 PublishAttempt.status)。

    刻意与 `PublishStatus` 分开。后者是「这个商品在渠道上现在处于什么状态」,
    是**当前事实**;这里是「第 3 次尝试发生了什么」,是**历史记录**,写完不再改。

    合成一个枚举的代价在超时那条路径上最明显:一次 UNKNOWN 的尝试之后,
    listing 的状态可能仍然是 SUBMITTING(还在查),也可能变成 LISTED(查到了)。
    尝试记录必须保持 UNKNOWN —— 那次调用确实不知道结果,后来查清楚了
    不能倒回去改写历史,否则「我们当时是不是真的不知道」这个问题永远查不清。
    """

    PENDING = "PENDING"
    IN_FLIGHT = "IN_FLIGHT"
    SUCCEEDED = "SUCCEEDED"
    #: HTTP 超时/连接中断,平台**可能**已经收到。不许直接重发,见 7.4 节
    UNKNOWN = "UNKNOWN"
    #: 平台明确拒绝(4xx/业务错误),重发同样的内容不会成功
    FAILED = "FAILED"
    #: 本地在发出去之前就放弃了(校验没过、凭证缺失、被人工取消)
    ABORTED = "ABORTED"


class PublishOutboxStatus(StrEnum):
    """Outbox 行的调度状态(方案 7.2 节 PublishOutbox.status)。

    Outbox 存在的理由是「写库」和「调外部 API」不能在同一个事务里 ——
    事务提交了但 API 没发出去,和 API 发出去了但事务回滚了,是两种都不能接受的
    结果。先在同一个事务里写一行 PENDING,再由后台按 `next_attempt_at` 取出来发。

    `DEAD` 不是「失败」的同义词:它表示重试次数用尽,**需要人来看一眼**。
    自动重试到永远会把一个配置错误变成一份持续增长的账单。
    """

    PENDING = "PENDING"
    #: 已被某个 worker 领走,`lease_until` 之前别人不许碰
    LEASED = "LEASED"
    DONE = "DONE"
    #: 重试用尽,进人工异常队列
    DEAD = "DEAD"


#: Outbox 的终态。到了这两个状态,调度器不再看这一行。
PUBLISH_OUTBOX_TERMINAL_STATES: frozenset[str] = frozenset(
    {PublishOutboxStatus.DONE.value, PublishOutboxStatus.DEAD.value}
)

#: 尝试记录里「结果未知」的那一个。单独拎出来是因为它有专属处置流程
#: (7.4 节:先查平台或用幂等键确认,确认不了进人工队列),而不是普通失败。
PUBLISH_ATTEMPT_UNRESOLVED: frozenset[str] = frozenset(
    {PublishAttemptStatus.PENDING.value, PublishAttemptStatus.IN_FLIGHT.value,
     PublishAttemptStatus.UNKNOWN.value}
)


class ExtractionRunStatus(StrEnum):
    """一次识别 run 的状态(§4.6)。

    §4.6 要求识别改成异步:接口立即返回 run_id,Celery 执行,支持 cancel。
    那需要五个新列 + 一条迁移 + Celery 任务,**都要真库**。这个枚举与
    `app.attributes.run_state` 的判定先落地,理由与 `EvidenceClass` 那次相同:
    判定验完再接线,比接完线再补判定安全 —— 后者的中间态是「一个没人验过的
    状态机正在决定运营看到什么」。

    ## 今天它已经有一个真实消费者

    异步那一半还没接,但**终态判定今天就用得上**:同步路径跑完之后,
    「这次识别到底算成功、部分成功还是失败」这个问题现在由**前端**回答
    (`AttributeTab.tsx` 按 `succeeded_count` / `failed_count` 自己判三档)——
    那是硬规则 4 明令禁止的形状。后端派生出这一个值之后,前端只展示。

    ## 六个取值的边界

        QUEUED           已受理、还没开始。**这一档今天不会出现**(同步执行),
                         留着是因为幂等键要认它:同意图的第二次请求撞上
                         QUEUED 的那一行时,答案是「返回原 run」而不是新建
        RUNNING          正在逐图调用。付费调用发生在这一档里
        COMPLETED        每一张图都跑完了,没有失败
        PARTIAL_SUCCESS  有成功也有失败。**它是终态,不是中间态** ——
                         失败的那几张要重试的话走一个新 run(§13「按颜色重试」)
        FAILED           没有任何一张成功。今天这一档在库里长得和 COMPLETED
                         一模一样:`error_code` 是 NULL、接口返回 200
        CANCELLED        人喊停,且**确实停在了做完之前**。喊得太晚(最后一张
                         已经跑完)不算取消 —— 理由见 `run_state.terminal_status_for`

    终态集合、转移表与「哪几档算数」都在 `app.attributes.run_state`,
    不在这里。枚举只负责有哪些取值。
    """

    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class UnitsSource(StrEnum):
    """`ProviderUsageRecord.billable_units` **是谁说的**(A45-batch14-18 / 任务 9)。

    放在 `core/enums.py` 而不是 `providers/base.py`:模型层和 Provider 层
    都要认它,而 `core` 是两边都能依赖的那一层(契约一)。判定本身
    (`settle_billable_units`)留在 Provider 层——枚举只负责有哪些取值。

    ## 为什么这两档必须分开

    §10.2 第 5 条准入是「用量记录与 Provider 后台账单条数一致」。对不上的
    时候第一个要回答的问题是"这个数是厂商说的还是我们猜的"：前者对不上
    要去查厂商,后者对不上是我们算错了,两条路完全相反。合成一档的话
    每一行看起来同样权威,而在这一批之前**所有行都是猜的**。
    """

    #: 厂商自己报的,和它的账单同源。FASHN 走 `x-fashn-credits-used` 响应头
    PROVIDER = "provider"
    #: 我们数候选图数出来的。和账单只是**碰巧**可能相等 ——
    #: FASHN 的官方计价表里一张图是 2~5 个额度,取决于运营在设置页选的档位
    INFERRED = "inferred"
