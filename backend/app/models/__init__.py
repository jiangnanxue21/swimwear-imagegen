"""ORM 模型注册表。

Alembic autogenerate 依赖此处的导入来发现全部表,新增模型必须在这里登记。
"""
from app.db.base import Base
from app.models.app_setting import AppSetting
from app.models.attribute import (
    AttributeCalibration,
    AttributeEvidence,
    ProductAttributeExtraction,
    ProductAttributeValue,
)
from app.models.audit_log import AuditLog
from app.models.batch_job import BatchActionReceipt, BatchJob, BatchJobItem
from app.models.evaluation import (
    CandidateEvaluation,
    EvaluationAttempt,
    EvaluationProblem,
    ReviewItem,
    RuleSet,
)
from app.models.generation import (
    GenerationAttempt,
    GenerationCandidate,
    GenerationTask,
    ProviderUsageRecord,
)
from app.models.listing_copy import ContentPlan, ListingCopy, ListingDraft
from app.models.generation_plan import GenerationPlan
from app.models.listing_image import ListingImageItem, ListingImageSet
from app.models.media_asset import (
    MediaAsset,
    MediaConsent,
    MediaDerivative,
    MediaMigrationMismatch,
)
from app.models.model_template import ModelTemplate
from app.models.output_asset import OutputAsset
from app.models.platform_rejection import PlatformRejection
from app.models.product import Product
from app.models.product_asset import ProductAsset
from app.models.prompt_template import PromptTemplate
from app.models.publishing import ChannelListing, PublishAttempt, PublishOutbox
from app.models.spu import ColorVariant, Spu
from app.models.task_dispatch import TaskDispatch

__all__ = [
    "Base",
    "Spu",
    "ColorVariant",
    "Product",
    "ProductAsset",
    "AuditLog",
    "AppSetting",
    "BatchJob",
    "BatchJobItem",
    "BatchActionReceipt",
    "PromptTemplate",
    "ModelTemplate",
    "GenerationPlan",
    "GenerationTask",
    "GenerationAttempt",
    "GenerationCandidate",
    "ProviderUsageRecord",
    "RuleSet",
    "CandidateEvaluation",
    "EvaluationProblem",
    "EvaluationAttempt",
    "ReviewItem",
    "OutputAsset",
    "TaskDispatch",
    "MediaAsset",
    "MediaDerivative",
    "MediaConsent",
    "MediaMigrationMismatch",
    "ProductAttributeExtraction",
    "AttributeEvidence",
    "ProductAttributeValue",
    "AttributeCalibration",
    "ListingImageSet",
    "ListingImageItem",
    "ContentPlan",
    "ListingCopy",
    "ListingDraft",
    "PlatformRejection",
    "ChannelListing",
    "PublishAttempt",
    "PublishOutbox",
]
