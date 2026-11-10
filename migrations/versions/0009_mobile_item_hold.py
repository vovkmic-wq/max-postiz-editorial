"""Ручной барьер записи и его монотонная ревизия."""

import sqlalchemy as sa
from alembic import op

revision = "0009"
down_revision = "0008"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "mobile_item_holds",
        sa.Column("variant_id", sa.String(36), sa.ForeignKey("hub_variants.id"), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("actor", sa.String(64), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
    )


def downgrade():
    raise RuntimeError("Восстанавливайте резервную копию в отдельную пустую базу")
