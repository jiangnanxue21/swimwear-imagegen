"""A45-batch14-20:PRD 阶段 4 —— 生成方案实体 + 任务颜色作用域 + 图片项 §6.5 两列。

新增:
- `generation_plans` 表(§4.7,修复风险表 D2)
- `generation_tasks`:`generation_plan_id` / `plan_fingerprint` /
  `color_variant_id` / `sample_fingerprint`
- `listing_image_items`:`shared_opt_in` / `angle`(§6.5 混排规则与角度验收)

**并线改号(A45-batch14-20 合并):** 本迁移原写作 `0040`,与同批并行线的
`0040_extraction_run_identity`(§4.6 识别 run 身份五列)撞号。两份改的是
**互不相交的表** —— 那份动 `attribute_extractions`,本份动 `generation_plans` /
`generation_tasks` / `listing_image_items` —— 所以合并方式是串起来而不是合表:
本份改为 `0041`,`down_revision` 指向 `0040`。**同号不是两个 head,是 alembic
加载期直接报 "Multiple revisions with the same identifier",整条链都起不来。**

**不回填。** 系统尚未投入使用(§3.1),没有存量行需要迁;而"顺手回填一个
默认方案"会给每个 SPU 造出一份没人配过的方案,它的指纹会立刻进幂等键,
于是第一次真正配方案时反而命中旧任务。

## 两条写迁移时踩过的

一、`UNIQUE(spu_id, COALESCE(color_variant_id,''))` **不能**写成
`sa.UniqueConstraint(...)` —— 唯一约束不接受表达式,`op.create_table` 里
放一个 `sa.func.coalesce` 会在 upgrade 时当场炸。要用表达式唯一索引
(`op.create_index(..., unique=True)` + 文本表达式)。
炸是好的失败方式;真正危险的是有人为了让它跑起来把 COALESCE 删掉,
那样 NULL 互不相等,SPU 默认方案可以存任意多份(见模型模块文档)。

二、`idempotency_key` 从 VARCHAR(128) 扩到 256。方案指纹要进键,而
`build_idempotency_key` 对显式键做的是 `[:128]` 截断 —— 截断之后两份
不同的输入可能得到同一个键,那是"幂等"这件事最坏的失效方式。
列先扩,截断长度在 `workflows/idempotency.py` 一并放宽。
"""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision = "0041"
down_revision = "0040"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ---------------------------------------------------------------- 新表
    op.create_table(
        "generation_plans",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("spu_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("color_variant_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("model_template_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("provider", sa.String(32), nullable=False, server_default=""),
        sa.Column("scene", sa.String(64), nullable=False, server_default=""),
        sa.Column("pose", sa.String(32), nullable=False, server_default=""),
        sa.Column("angles_json", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("budget_cap", sa.Numeric(12, 4), nullable=True),
        sa.Column("plan_fingerprint", sa.String(64), nullable=False, server_default=""),
        sa.Column("status", sa.String(16), nullable=False, server_default="DRAFT"),
        sa.Column("row_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_by", sa.String(64), nullable=False, server_default=""),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(["spu_id"], ["spus.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(
            ["color_variant_id"], ["color_variants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["model_template_id"], ["model_templates.id"], ondelete="SET NULL"
        ),
        sa.CheckConstraint(
            "budget_cap IS NULL OR budget_cap >= 0", name="ck_generation_plans_budget_cap"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    # 每层当前生效一份。表达式索引,见模块文档第一条。
    op.execute(
        "CREATE UNIQUE INDEX uq_generation_plans_scope "
        "ON generation_plans (spu_id, COALESCE(color_variant_id::text, '')) "
        "WHERE status <> 'ARCHIVED'"
    )
    op.create_index("ix_generation_plans_spu", "generation_plans", ["spu_id"])
    op.create_index(
        "ix_generation_plans_fingerprint", "generation_plans", ["plan_fingerprint"]
    )

    # ------------------------------------------------- generation_tasks 增列
    op.alter_column(
        "generation_tasks",
        "idempotency_key",
        existing_type=sa.String(128),
        type_=sa.String(256),
        existing_nullable=False,
    )
    op.add_column(
        "generation_tasks",
        sa.Column("generation_plan_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.add_column(
        "generation_tasks", sa.Column("plan_fingerprint", sa.String(64), nullable=True)
    )
    op.add_column(
        "generation_tasks",
        sa.Column("color_variant_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    # §5.3 双作用域样品指纹里**该颜色那一份**的快照。
    # 共享作用域那一份不落在这里:它属于事实层(§4.6 的 input_fingerprint),
    # 那批列还不存在。两个作用域分两批落,理由写在 HANDOVER。
    op.add_column(
        "generation_tasks", sa.Column("sample_fingerprint", sa.String(64), nullable=True)
    )
    op.create_foreign_key(
        "fk_generation_tasks_generation_plan_id",
        "generation_tasks",
        "generation_plans",
        ["generation_plan_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_foreign_key(
        "fk_generation_tasks_color_variant_id",
        "generation_tasks",
        "color_variants",
        ["color_variant_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index(
        "ix_generation_tasks_color_variant_id", "generation_tasks", ["color_variant_id"]
    )
    op.create_index(
        "ix_generation_tasks_plan_fingerprint", "generation_tasks", ["plan_fingerprint"]
    )

    # ---------------------------------------------- listing_image_items 增列
    #
    # `shared_opt_in` 默认 **false**,也就是 §6.5 的"默认不混入"。
    # 默认 true 在今天看不出差别(库里的图 variant_id 全是 NULL),
    # 而颜色绑定入口一上线,每个颜色的附图位都会自动灌满所有通用图。
    op.add_column(
        "listing_image_items",
        sa.Column(
            "shared_opt_in", sa.Boolean(), nullable=False, server_default="false"
        ),
    )
    op.add_column("listing_image_items", sa.Column("angle", sa.String(24), nullable=True))


def downgrade() -> None:
    op.drop_column("listing_image_items", "angle")
    op.drop_column("listing_image_items", "shared_opt_in")

    op.drop_index("ix_generation_tasks_plan_fingerprint", "generation_tasks")
    op.drop_index("ix_generation_tasks_color_variant_id", "generation_tasks")
    op.drop_constraint(
        "fk_generation_tasks_color_variant_id", "generation_tasks", type_="foreignkey"
    )
    op.drop_constraint(
        "fk_generation_tasks_generation_plan_id", "generation_tasks", type_="foreignkey"
    )
    op.drop_column("generation_tasks", "sample_fingerprint")
    op.drop_column("generation_tasks", "color_variant_id")
    op.drop_column("generation_tasks", "plan_fingerprint")
    op.drop_column("generation_tasks", "generation_plan_id")
    op.alter_column(
        "generation_tasks",
        "idempotency_key",
        existing_type=sa.String(256),
        type_=sa.String(128),
        existing_nullable=False,
    )

    op.drop_index("ix_generation_plans_fingerprint", "generation_plans")
    op.drop_index("ix_generation_plans_spu", "generation_plans")
    op.execute("DROP INDEX IF EXISTS uq_generation_plans_scope")
    op.drop_table("generation_plans")
