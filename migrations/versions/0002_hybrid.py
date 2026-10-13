"""Persistent hybrid projects, scene jobs, video fingerprints and source media."""

from alembic import op
from max_postiz_agent.db import Base

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade():
    for name in ("video_projects", "video_scenes", "video_assets", "material_media"):
        Base.metadata.tables[name].create(op.get_bind(), checkfirst=True)


def downgrade():
    raise RuntimeError("Restore a backup rather than drop content or job history.")
