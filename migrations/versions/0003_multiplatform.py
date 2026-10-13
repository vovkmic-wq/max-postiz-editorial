"""Multi-platform approval, dispatch and source archive tables."""
from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    # The checked-in DDL is a frozen snapshot; importing current application models
    # here would make future installations dependent on unrelated code changes.
    from pathlib import Path
    sql = (Path(__file__).parent / "0003_schema.sql").read_text(encoding="utf-8")
    for statement in sql.split(";\n"):
        if statement.strip():
            op.execute(sa.text(statement))


def downgrade():
    raise RuntimeError("Destructive downgrade disabled; restore into a fresh database")
