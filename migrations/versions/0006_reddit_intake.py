"""Shared durable inbox for owner submissions and IFTTT mail."""
from alembic import op

import sqlalchemy as sa

revision = "0006"
down_revision = "0005"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("hub_reddit_submissions",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("reddit_id", sa.String(32), nullable=False, unique=True),
        sa.Column("url", sa.Text(), nullable=False),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("original", sa.Text(), nullable=False),
        sa.Column("original_hash", sa.String(64), unique=True),
        sa.Column("origin", sa.String(16), nullable=False),
        sa.Column("target_id", sa.String(64), sa.ForeignKey("hub_targets.id"), nullable=False),
        sa.Column("state", sa.String(32), nullable=False),
        sa.Column("rights_ref", sa.Text(), nullable=False),
        sa.Column("rights_actor", sa.String(64), nullable=False),
        sa.Column("variant_id", sa.String(36), sa.ForeignKey("hub_variants.id")),
        sa.Column("translation_hash", sa.String(64), unique=True),
        sa.Column("owner", sa.String(36)),
        sa.Column("lease_until", sa.Float(), nullable=False),
        sa.Column("error", sa.String(64)),
        sa.Column("created_at", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False))
    op.create_index("ix_hub_reddit_submissions_state", "hub_reddit_submissions", ["state"])


def downgrade():
    raise RuntimeError("Restore into a fresh database")
