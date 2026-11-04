"""Real PostgreSQL locks/atomicity; only explicitly dedicated test databases."""

import copy
import os
from pathlib import Path
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from max_postiz_agent import unbound_selection_reconciliation as recovery
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import RecurringTask, Target
from max_postiz_agent.server_models import EditorialControl, EditorialPreparationClaim
from pg_test_contract import CONTRACT_ENV, endpoint_spec, verify_identity
import test_unbound_selection_reconciliation as checks


@pytest.fixture
def unbound_pg(tmp_path, monkeypatch):
    url = os.environ.get("TEST_DATABASE_URL")
    explicit = os.environ.get(CONTRACT_ENV)
    contract = (
        endpoint_spec("routes", url, Path(__file__).resolve().parents[1]) if explicit else None
    )
    if contract is None:
        if not url:
            pytest.skip("TEST_DATABASE_URL is not configured")
        parsed = None
        try:
            parsed = make_url(url)
        except (ArgumentError, ValueError):
            pass
        if (
            parsed is None
            or parsed.drivername != "postgresql+psycopg"
            or parsed.query
            or not re.fullmatch(r"(?:test_[a-z0-9_]+|[a-z0-9_]+_tests)", parsed.database or "")
        ):
            pytest.fail("Dedicated TEST_DATABASE_URL required (value withheld)", pytrace=False)
    admin = create_engine(url)
    assert admin.dialect.name == "postgresql"
    schema = "unbound_" + uuid4().hex
    engine = None
    created = False
    try:
        if contract is not None:
            with admin.connect() as connection:
                verify_identity(connection, contract)
        with admin.begin() as connection:
            connection.execute(text('CREATE SCHEMA "' + schema + '"'))
        created = True
        engine = create_engine(
            url, connect_args={"options": "-csearch_path=" + schema + " -cstatement_timeout=5000"}
        )
        assert engine.dialect.name == "postgresql"
        yield checks.make_case(tmp_path, engine, monkeypatch)
    finally:
        if engine is not None:
            engine.dispose()
        if created:
            with admin.begin() as connection:
                connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def test_postgres_readonly_inspect(unbound_pg):
    assert unbound_pg.core.db.engine.dialect.name == "postgresql"
    checks.check_inspect(unbound_pg)


def test_postgres_atomic_two_table_history_delta(unbound_pg):
    checks.check_commit(unbound_pg)


def test_postgres_response_loss_replay_status_no_proof_reads(unbound_pg):
    checks.check_replay(unbound_pg)


def test_postgres_pause_cas(unbound_pg):
    checks.check_reject(
        unbound_pg, lambda c: checks.mutate_row(c, EditorialControl, "main", paused=False)
    )


def test_postgres_revision_cas(unbound_pg):
    checks.check_reject(unbound_pg, lambda c: c.arguments.update(expected_control_revision=9))


def test_postgres_changed_claim_binding_refused(unbound_pg):
    checks.check_reject(
        unbound_pg,
        lambda c: checks.mutate_row(
            c, EditorialPreparationClaim, c.claim_id, record_id=str(uuid4())
        ),
    )


def test_postgres_unpaused_target_refused(unbound_pg):
    checks.check_reject(unbound_pg, lambda c: checks.mutate_row(c, Target, "max", paused=False))


def test_postgres_new_unknown_artifact_refused(unbound_pg):
    def change(c):
        directory = c.core.settings.root / "data/opencode-requests"
        directory.mkdir()
        (directory / "orphan.json").write_text("unknown request")

    checks.check_reject(unbound_pg, change)


def test_postgres_changed_owner_seal_refused(unbound_pg):
    checks.check_reject(
        unbound_pg, lambda c: c.arguments["evidence_hashes"].update(both_lanes_copy="0" * 64)
    )


def test_postgres_invalid_no_upstream_proof_refused(unbound_pg):
    checks.check_reject(
        unbound_pg,
        lambda c: checks.change_proof(c, "parser_reproduction", lambda p: p.update(http_calls=1)),
    )


def test_postgres_status_detects_audit_or_row_drift(unbound_pg):
    c = unbound_pg
    result = checks.check_commit(c)
    checks.mutate_row(c, Audit, result["audit_id"], action="changed")
    with pytest.raises(AgentError):
        recovery.operation_status(c.core, c.operation_id)


def test_postgres_unrelated_owned_lease_refused(unbound_pg):
    c = unbound_pg
    with c.core.db.session.begin() as session:
        session.add(
            RecurringTask(
                id="owned",
                profile_id="max",
                interval_seconds=3600,
                lookback_hours=24,
                due_at=0,
                owner="owner",
                lease_until=0,
            )
        )
    checks.check_reject(c, lambda _: None)


def test_postgres_audit_insert_failure_rolls_back_claim(unbound_pg):
    checks.check_audit_failure_rollback(unbound_pg)


def test_postgres_unsupported_text_policy_resealed_is_refused(unbound_pg):
    checks.check_reject(unbound_pg, lambda c: checks.change_policy(c, presentation_contract=None))


def test_postgres_concurrent_same_operation_commits_once(unbound_pg):
    c = unbound_pg
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(recovery.apply, c.core, c.claim_id, **copy.deepcopy(c.arguments))
            for _ in range(2)
        ]
        results = [f.result(timeout=10) for f in futures]
    assert results[0] == results[1]
    with c.core.db.session() as session:
        assert len(list(session.scalars(select(Audit).where(Audit.action == recovery.ACTION)))) == 1
    assert c.calls == []


def test_postgres_control_row_lock_prevents_stale_commit(unbound_pg):
    c = unbound_pg
    connection = c.core.db.engine.connect()
    transaction = connection.begin()
    connection.execute(
        select(EditorialControl).where(EditorialControl.id == "main").with_for_update()
    )
    attempted = threading.Event()

    def before_cursor(_connection, _cursor, statement, _parameters, _context, _many):
        if "editorial_control" in statement and "FOR UPDATE" in statement:
            attempted.set()

    event.listen(c.core.db.engine, "before_cursor_execute", before_cursor)
    pool = ThreadPoolExecutor(max_workers=1)
    try:
        future = pool.submit(recovery.apply, c.core, c.claim_id, **c.arguments)
        assert attempted.wait(3), "Native caller never reached the actual Control FOR UPDATE"
        assert not future.done()
        connection.execute(text("UPDATE editorial_control SET revision=9 WHERE id='main'"))
        transaction.commit()
        with pytest.raises(AgentError):
            future.result(timeout=7)
        with c.core.db.session() as session:
            assert session.get(EditorialPreparationClaim, c.claim_id).state == "blocked_unknown"
            assert not session.scalar(select(Audit.id).where(Audit.action == recovery.ACTION))
    finally:
        if transaction.is_active:
            transaction.rollback()
        connection.close()
        pool.shutdown(wait=True)
        event.remove(c.core.db.engine, "before_cursor_execute", before_cursor)
