"""Immutable platform metadata is part of the approved variant snapshot."""
from alembic import op
import sqlalchemy as sa

revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("hub_variants", sa.Column("metadata_json", sa.JSON(), nullable=False, server_default="{}"))


def downgrade():
    raise RuntimeError("Restore into a fresh database")
