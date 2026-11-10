"""Durable plans and receipts for the mobile gateway."""
from alembic import op
import sqlalchemy as sa

revision = "0010"
down_revision = "0009"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("mobile_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(64), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("object_id", sa.String(128), nullable=False),
        sa.Column("plan_hash", sa.String(64), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("expires_at", sa.Float(), nullable=False))
    op.create_index("ix_mobile_plans_expires_at", "mobile_plans", ["expires_at"])
    op.create_table("mobile_operations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("plan_id", sa.String(36), sa.ForeignKey("mobile_plans.id"), nullable=False, unique=True),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("environment", sa.String(64), nullable=False),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("object_id", sa.String(128), nullable=False),
        sa.Column("request_hash", sa.String(64), nullable=False),
        sa.Column("reason", sa.String(512), nullable=False),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("code", sa.String(96)),
        sa.Column("result", sa.JSON()),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False))
    op.create_index("ix_mobile_operations_created_at", "mobile_operations", ["created_at"])


def downgrade():
    raise RuntimeError("Сохраняйте журнал мобильных операций при откате")
