"""Idempotent history import from an explicit read-only MAX database copy."""
import hashlib
import sqlite3
from pathlib import Path

from sqlalchemy import select

from .config import AgentError
from .db import Base, safe_payload


TABLES = ("config_versions", "stories", "materials", "drafts", "draft_revisions", "approvals",
          "publication_intents", "publication_attempts", "audit_events")


def import_history(db, source, apply=False):
    source = Path(source).resolve()
    if not source.is_file():
        raise AgentError("IMPORT_SOURCE_NOT_FOUND")
    if db.engine.url.database and Path(db.engine.url.database).resolve() == source:
        raise AgentError("IMPORT_SOURCE_IS_TARGET")
    digest = hashlib.sha256(source.read_bytes()).hexdigest()
    conn = sqlite3.connect(source.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("BEGIN")
    counts = {}
    try:
        available = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        with db.engine.begin() as target:
            for name in TABLES:
                if name not in available:
                    continue
                table = Base.metadata.tables[name]
                count = 0
                # Identifier comes exclusively from the fixed TABLES allowlist.
                query = 'SELECT * FROM "' + name + '"'  # noqa: S608
                for original in conn.execute(query):
                    row = {k: v for k, v in dict(original).items() if k in table.c}
                    for column in table.columns:
                        if column.name in row and column.type.__class__.__name__ == "JSON" and isinstance(row[column.name], str):
                            from .config import parse_json
                            row[column.name] = safe_payload(parse_json(row[column.name]))
                    if target.execute(select(table.c.id).where(table.c.id == row["id"])).first():
                        # Older activation code marked imported archives stale.
                        # Repair only the untouched original revision/configuration.
                        if apply and name == "drafts":
                            target.execute(table.update().where(table.c.id == row["id"],
                                table.c.state == "stale", table.c.revision == row["revision"],
                                table.c.config_id == row["config_id"]).values(state="archived"))
                        continue
                    if name in {"drafts", "publication_intents"}:
                        row["state"] = "archived"
                    if name == "approvals":
                        row["revoked"] = True
                    for column in table.columns:
                        if column.type.__class__.__name__ == "Boolean" and column.name in row:
                            row[column.name] = bool(row[column.name])
                    if apply:
                        target.execute(table.insert().values(**row))
                    count += 1
                counts[name] = count
    finally:
        conn.close()
    return {"state": "imported" if apply else "dry_run", "source_sha256": digest,
            "new_rows": counts, "active_publications_imported": 0}
