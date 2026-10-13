"""Build a frozen initial PostgreSQL DDL snapshot, only when absent."""
from pathlib import Path
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateIndex, CreateTable
from max_postiz_agent.db import Base

path = Path(__file__).resolve().parents[1] / "migrations/versions/0001_schema.sql"
if not path.exists():
    statements = []
    for table in Base.metadata.sorted_tables:
        if not table.name.startswith("hub_"):
            statements.append(str(CreateTable(table).compile(dialect=postgresql.dialect())).strip())
            statements.extend(str(CreateIndex(i).compile(dialect=postgresql.dialect())) for i in table.indexes)
    path.write_text(";\n".join(statements) + ";\n", encoding="utf-8")
print("Frozen legacy schema ready")
