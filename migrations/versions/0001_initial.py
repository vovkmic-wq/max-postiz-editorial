"""Initial persistent editor, scheduler, inbox and outbox schema."""

from alembic import op
from pathlib import Path

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    ddl = Path(__file__).with_name("0001_schema.sql").read_text(encoding="utf-8")
    for statement in ddl.split(";\n"):
        if statement.strip():
            op.execute(statement)


def downgrade():
    raise RuntimeError(
        "Destructive downgrade disabled; use an encrypted backup into a fresh target."
    )
