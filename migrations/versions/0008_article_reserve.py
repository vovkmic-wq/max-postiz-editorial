"""Долговечные claims подготовки и слоты доставки; прежняя история не меняется."""
from alembic import op
import sqlalchemy as sa

revision = "0008"
down_revision = "0007"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("publication_slots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("target_id", sa.String(64), nullable=False),
        sa.Column("slot_at", sa.Integer(), nullable=False),
        sa.Column("record_id", sa.String(36), nullable=False),
        sa.Column("text_hash", sa.String(64), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("binding", sa.JSON(), nullable=False),
        sa.Column("token", sa.String(36), nullable=False),
        sa.Column("control_revision", sa.Integer(), nullable=False),
        sa.Column("active_target", sa.String(64), unique=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("due_at", sa.Float(), nullable=False),
        sa.Column("dispatch_id", sa.String(36)),
        sa.Column("delivered_at", sa.Float()),
        sa.Column("error", sa.String(128)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.UniqueConstraint("target_id", "slot_at"),
        sa.UniqueConstraint("target_id", "record_id"),
        sa.UniqueConstraint("target_id", "text_hash"))
    op.create_table("editorial_preparation_claims",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("token", sa.String(36), nullable=False),
        sa.Column("control_revision", sa.Integer(), nullable=False),
        sa.Column("record_id", sa.String(36), unique=True),
        sa.Column("active_key", sa.String(32), unique=True),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("error", sa.String(128)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False))


def downgrade():
    raise RuntimeError("Восстанавливайте резервную копию в отдельную пустую базу")
