"""The actual Postiz journal migration, applied to an isolated Post projection."""
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError, DBAPIError

from pg_test_contract import CONTRACT_ENV, endpoint_spec, verify_identity

ROOT = Path(__file__).resolve().parents[1]
DDL = """
CREATE TYPE "State" AS ENUM ('DRAFT','QUEUE','ERROR','PUBLISHED');
CREATE TABLE "Integration" (
    id text PRIMARY KEY, "organizationId" text NOT NULL, "providerIdentifier" text NOT NULL,
    disabled boolean NOT NULL DEFAULT false, "refreshNeeded" boolean NOT NULL DEFAULT false,
    "deletedAt" timestamp(3)
);
CREATE TABLE "Post" (
    id text PRIMARY KEY, "organizationId" text NOT NULL, "integrationId" text REFERENCES "Integration",
    "group" text NOT NULL, state "State" NOT NULL, "publishDate" timestamp(3) NOT NULL,
    "updatedAt" timestamp(3), content text NOT NULL, image text, settings text,
    "parentPostId" text REFERENCES "Post", "intervalInDays" integer, "releaseId" text,
    "releaseURL" text, "deletedAt" timestamp(3)
);
INSERT INTO "Integration" VALUES ('fixture-integration', 'fixture-org', 'max-browser');
INSERT INTO "Post" (id, "organizationId", "integrationId", "group", state, "publishDate", content, image, settings)
VALUES ('fixture-post', 'fixture-org', 'fixture-integration', 'fixture-group', 'DRAFT',
        '2026-11-11T11:00:00.123', 'Заголовок', '[{"path":"/uploads/example.jpg"}]', '{"__type":"max-browser"}');
"""


@pytest.fixture
def postiz_journal_pg():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    try:
        parsed = make_url(url)
    except (ArgumentError, ValueError):
        pytest.fail("Invalid dedicated PostgreSQL test endpoint", pytrace=False)
    if (parsed.drivername != "postgresql+psycopg" or parsed.query
            or not re.fullmatch(r"(?:test_[a-z0-9_]+|[a-z0-9_]+_tests)", parsed.database or "")):
        pytest.fail("Invalid dedicated PostgreSQL test endpoint", pytrace=False)
    admin = create_engine(parsed)
    schema = "schedule_journal_" + uuid4().hex
    with admin.begin() as connection:
        if os.getenv(CONTRACT_ENV):
            verify_identity(connection, endpoint_spec("routes", url, ROOT))
        connection.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    engine = create_engine(parsed, connect_args={"options": f"-csearch_path={schema}"})
    try:
        with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            raw = connection.connection.driver_connection
            raw.execute(DDL)
            raw.execute((ROOT / "deployment/postiz-max/max-schedule-journal.sql").read_text(encoding="utf-8"))
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        admin.dispose()


def _binding(connection, *, deadline_delta=5000, due_delta=10000):
    raw = connection.exec_driver_sql('SELECT content, image, settings FROM "Post"').one()
    stamp = _now_ms(connection)
    content, image, settings = (hashlib.sha256(value.encode()).hexdigest() for value in raw)
    return {"operation_id": str(uuid4()), "post_id": "fixture-post", "group_id": "fixture-group",
        "integration_id": "fixture-integration", "content_sha256": content, "image_json_sha256": image,
        "settings_sha256": settings, "draft_due_ms": int(datetime(2026, 11, 11, 11, 0, 0, 123000, UTC)
                                                        .timestamp() * 1000),
        "due_ms": stamp + due_delta, "deadline_ms": stamp + deadline_delta}


def _now_ms(connection):
    return connection.exec_driver_sql("SELECT floor(extract(epoch from clock_timestamp()) * 1000)::bigint").scalar_one()


def _change(connection, binding, action, org="fixture-org"):
    return connection.exec_driver_sql(
        "SELECT max_schedule_change(%s, %s::jsonb, %s)", (org, json.dumps(binding), action)).scalar_one()


def _state(connection):
    return connection.exec_driver_sql(
        'SELECT state, (extract(epoch FROM "publishDate") * 1000)::bigint FROM "Post"').one()


def test_postgres_lost_schedule_response_can_restore_exact_draft(postiz_journal_pg):
    engine = postiz_journal_pg
    with engine.begin() as connection:
        binding = _binding(connection, deadline_delta=800, due_delta=1300)
        assert _change(connection, binding, "schedule") == {"outcome": "scheduled", "new_schedule": True}
        assert _change(connection, binding, "schedule") == {"outcome": "scheduled", "new_schedule": False}
        assert _change(connection, binding, "retire")["outcome"] == "deadline_pending"
    with engine.begin() as connection:
        assert _state(connection) == ("QUEUE", binding["due_ms"])
        assert connection.exec_driver_sql("SELECT outcome FROM max_schedule_journal").scalar_one() == "scheduled"
        connection.exec_driver_sql("SELECT pg_sleep(GREATEST(0, %s - extract(epoch FROM clock_timestamp())))",
                                   (binding["due_ms"] / 1000 + 0.02,))
        assert _change(connection, binding, "retire")["outcome"] == "draft_restored"
        assert _change(connection, binding, "retire")["outcome"] == "draft_restored"
        assert _state(connection) == ("DRAFT", binding["draft_due_ms"])
        assert connection.exec_driver_sql(
            "SELECT scheduled_at IS NOT NULL AND retired_at IS NOT NULL FROM max_schedule_journal").scalar_one()
    # A delayed Postiz workflow cannot overwrite the restored DRAFT.
    with pytest.raises(DBAPIError, match="MAX_SCHEDULE_DRAFT_RETIRED"), engine.begin() as connection:
        connection.exec_driver_sql("""UPDATE "Post" SET state='ERROR' WHERE id='fixture-post'""")


def test_postgres_deadline_is_checked_after_waiting_for_native_row_lock(postiz_journal_pg):
    engine = postiz_journal_pg
    with engine.begin() as connection:
        binding = _binding(connection, deadline_delta=450, due_delta=600)

    def late_schedule():
        with engine.begin() as other:
            return _change(other, binding, "schedule")

    with ThreadPoolExecutor(max_workers=1) as pool:
        with engine.begin() as connection:
            connection.exec_driver_sql('SELECT id FROM "Post" FOR UPDATE')
            waiting = pool.submit(late_schedule)
            time.sleep(0.65)
        assert waiting.result(timeout=4)["outcome"] == "deadline_elapsed"
    with engine.begin() as connection:
        assert _change(connection, binding, "retire")["outcome"] == "draft_preserved"
        assert _state(connection) == ("DRAFT", binding["draft_due_ms"])
        assert _change(connection, binding, "schedule")["outcome"] == "draft_preserved"


@pytest.mark.parametrize("edit", ["content", "release_id", "foreign_org"])
def test_postgres_retirement_refuses_changed_or_already_released_post(postiz_journal_pg, edit):
    with postiz_journal_pg.begin() as connection:
        binding = _binding(connection, deadline_delta=-2000, due_delta=-1000)
        org = "fixture-org"
        if edit == "content":
            connection.exec_driver_sql("""UPDATE "Post" SET content='changed'""")
        elif edit == "release_id":
            connection.exec_driver_sql("""UPDATE "Post" SET "releaseId"='external', state='PUBLISHED'""")
        else:
            org = "another-org"
        before = _state(connection)
        assert _change(connection, binding, "retire", org=org)["outcome"] == "draft_conflict"
        assert _state(connection) == before
        assert connection.exec_driver_sql("SELECT count(*) FROM max_schedule_journal").scalar_one() == 0
