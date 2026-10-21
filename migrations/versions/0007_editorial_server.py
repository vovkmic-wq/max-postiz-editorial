"""Перенос управления и журнала статей в PostgreSQL без удаления старой истории."""
from alembic import op
import sqlalchemy as sa

revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("editorial_control",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("paused", sa.Boolean(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("settings", sa.JSON(), nullable=False),
        sa.Column("runtime", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False),
        sa.Column("updated_by", sa.String(128), nullable=False))
    op.create_table("editorial_article_ledger",
        sa.Column("id", sa.String(32), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False))


def downgrade():
    raise RuntimeError("Восстанавливайте резервную копию в отдельную пустую базу")
