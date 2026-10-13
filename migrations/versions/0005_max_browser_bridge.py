"""Durable authoritative Postiz mirror and mutation leases for the MAX browser bridge."""
from alembic import op
import sqlalchemy as sa

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table("hub_bridge_posts",
        sa.Column("post_id", sa.String(64), primary_key=True),
        sa.Column("integration_id", sa.String(64), nullable=False),
        sa.Column("group_id", sa.String(64), nullable=False),
        sa.Column("target_id", sa.String(64), sa.ForeignKey("hub_targets.id"), nullable=False),
        sa.Column("variant_id", sa.String(36), sa.ForeignKey("hub_variants.id"), nullable=False, unique=True),
        sa.Column("remote_hash", sa.String(64), nullable=False),
        sa.Column("remote_state", sa.String(32), nullable=False),
        sa.Column("due_at", sa.Float(), nullable=False),
        sa.Column("active", sa.Boolean(), nullable=False),
        sa.Column("error", sa.String(64)),
        sa.Column("mutation_owner", sa.String(36)),
        sa.Column("mutation_until", sa.Float(), nullable=False),
        sa.Column("updated_at", sa.Float(), nullable=False))


def downgrade():
    raise RuntimeError("Restore into a fresh database")
