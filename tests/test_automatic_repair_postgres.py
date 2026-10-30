"""Реальный PostgreSQL в случайной изолированной схеме, без внешних отправок."""
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, text

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Database, uid
from max_postiz_agent.server_state import DatabaseArticleLedger

pytest_plugins = ["test_automatic_article_repair"]


@pytest.fixture
def repair_pg(repair_cycle):
    value = repair_cycle
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    admin = create_engine(url)
    schema = "repair_test_" + uid().replace("-", "")
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    isolated = url + ("&" if "?" in url else "?") + urlencode({"options": "-csearch_path=" + schema})
    original = value.core.db
    database = Database(value.core.settings, url=isolated)
    try:
        database.initialize()
        database.restore(original.export())
        value.core.db = database
        DatabaseArticleLedger(value.cycle).migrate()
        yield value
    finally:
        value.core.db = original
        database.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def test_postgres_corrected_candidate_creates_one_draft_and_persists_passed_claim(repair_pg):
    value = repair_pg
    value.review.update(approved=True, issues=[])
    prepared = value.cycle.repair_rejected(value.initial["id"])
    restarted = HourlyArticleCycle(value.core)
    record = restarted._load()["records"][0]
    assert prepared["state"] == record["state"] == "automatic_ready"
    assert record["automatic_repair_attempts"][0]["state"] == "passed"
    assert value.counts == {"hy": 1, "min": 2, "lt": 2, "qwen": 1}
    assert len(value.records) == len(value.writes) == 1
    with value.core.db.engine.connect() as connection:
        assert connection.scalar(text("select count(*) from hub_dispatches")) == 0
        assert connection.scalar(text("select count(*) from hub_approvals")) == 0


def test_postgres_audit_survives_ledger_crash_and_expiry_without_editor_replay(repair_pg, monkeypatch):
    value = repair_pg

    def crash(_):
        raise RuntimeError("Known crash after committed PostgreSQL claim")

    monkeypatch.setattr(value.cycle, "_save", crash)
    with pytest.raises(RuntimeError, match="Known crash"):
        value.cycle.repair_rejected(value.initial["id"])
    value.stamp[0] += 3600
    restarted = HourlyArticleCycle(value.core)
    assert restarted.automatic_repair_status(restarted._load()["records"][0])["reason"] == "CLAIM_LOST"
    assert value.counts["qwen"] == 0 and not value.records


def test_two_postgres_callers_cannot_issue_a_second_editor_request(repair_pg):
    value = repair_pg
    value.review.update(approved=True, issues=[])
    entered, release = threading.Event(), threading.Event()

    def wait_editor():
        entered.set()
        assert release.wait(10)

    value.controls.editor_hook = wait_editor
    second = HourlyArticleCycle(value.core, repair_factory=value.cycle.repair_factory)
    second._quality = value.cycle._quality
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(value.cycle.repair_rejected, value.initial["id"])
        assert entered.wait(10)
        other = pool.submit(second.repair_rejected, value.initial["id"])
        try:
            with pytest.raises(AgentError):
                other.result(5)
        finally:
            release.set()
        assert first.result(10)["state"] == "automatic_ready"
    assert value.counts["qwen"] == 1 and len(value.records) == 1
