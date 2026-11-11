"""Real isolated PostgreSQL transactions for guarded unsent-DRAFT retirement."""

import copy
import os
from pathlib import Path
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlencode
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from max_postiz_agent import preapproval_retirement as recovery
from max_postiz_agent import mobile_domain
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, Database, now
from max_postiz_agent.db import Draft
from max_postiz_agent.hub_models import Variant
from max_postiz_agent.mobile_api import mobile_app
from max_postiz_agent.mobile_auth import MobilePrincipal
from max_postiz_agent.mobile_domain import MobileDomain
from max_postiz_agent.mobile_store import MobileOperation
from max_postiz_agent.service import Service
from max_postiz_agent.server_models import EditorialControl, EditorialLedger, PublicationSlot
import test_preapproval_retirement as checks
from pg_test_contract import CONTRACT_ENV, endpoint_spec, verify_identity

pytest_plugins = ["test_preapproval_retirement"]


@pytest.fixture
def retirement_pg(retirement_case):
    url = os.getenv("TEST_DATABASE_URL")
    explicit = os.getenv(CONTRACT_ENV)
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
            or not re.fullmatch(r"(?:test_[a-z0-9_]+|[a-z0-9_]+_tests)", parsed.database or "")
            or parsed.query
        ):
            pytest.fail(
                "Invalid TEST_DATABASE_URL; dedicated PostgreSQL test database required (value withheld)",
                pytrace=False,
            )
    value = retirement_case
    admin = create_engine(url)
    assert admin.dialect.name == "postgresql"
    if contract is not None:
        try:
            with admin.connect() as connection:
                verify_identity(connection, contract)
        except Exception:
            admin.dispose()
            raise
    schema = "preapproval_" + uuid4().hex
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    original = value.core.db
    isolated = url + "?" + urlencode({"options": "-csearch_path=" + schema})
    database = Database(value.core.settings, url=isolated)
    try:
        database.initialize()
        database.restore(original.export())
        value.core.db = database
        assert database.engine.dialect.name == "postgresql"
        yield value
    finally:
        value.core.db = original
        database.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def test_postgres_inspect_is_readonly(retirement_pg):
    checks.check_inspect_readonly(retirement_pg)


def test_postgres_atomic_retirement_preserves_all_history(retirement_pg):
    checks.check_retire(retirement_pg)


def test_postgres_completed_response_loss_replays_only_receipt(retirement_pg):
    checks.check_replay(retirement_pg)


def test_postgres_requires_explicit_owner_decision(retirement_pg):
    checks.check_owner(retirement_pg)


def test_postgres_pause_revision_cas(retirement_pg):
    checks.check_pause_cas(retirement_pg)


def test_postgres_even_revoked_approval_blocks(retirement_pg):
    checks.check_revoked(retirement_pg)


def test_postgres_corrupted_trace_blocks(retirement_pg):
    checks.check_trace(retirement_pg)


def test_postgres_unknown_get_cannot_be_repeated(retirement_pg, monkeypatch):
    checks.check_unknown_read(retirement_pg, monkeypatch)


def test_postgres_saved_raw_response_resumes_without_repeat(retirement_pg, monkeypatch):
    checks.check_saved_resume(retirement_pg, monkeypatch)


def test_postgres_permanent_retired_mutation_fence(retirement_pg):
    checks.check_guard(retirement_pg)


def test_postgres_concurrent_different_operations_have_single_retained_claim(
    retirement_pg, monkeypatch
):
    value = retirement_pg
    proposed = checks.plan(value)
    entered, release = threading.Event(), threading.Event()
    original = recovery._observe

    def held(*args, **kwargs):
        entered.set()
        assert release.wait(10)
        return original(*args, **kwargs)

    monkeypatch.setattr(recovery, "_observe", held)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(checks.apply, value, proposed, str(uuid4()))
        try:
            assert entered.wait(10)
            with pytest.raises(AgentError, match="OPERATION_CONFLICT"):
                checks.apply(value, proposed, str(uuid4()))
        finally:
            release.set()
        assert first.result(timeout=20)["state"] == "retired_no_send"
    with value.core.db.session() as session:
        assert len(list(session.scalars(select(Audit).where(Audit.action == recovery.CLAIM)))) == 1
        assert len(list(session.scalars(select(Audit).where(Audit.action == recovery.COMMIT)))) == 1


def test_postgres_control_row_lock_serializes_capture(retirement_pg):
    value = retirement_pg
    finished = threading.Event()

    def capture():
        with value.core.db.session.begin() as session:
            recovery._capture(value.core, session, value.record_id, locked=True)
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with value.core.db.session.begin() as session:
            session.scalar(
                select(EditorialControl).where(EditorialControl.id == "main").with_for_update()
            )
            future = pool.submit(capture)
            assert not finished.wait(0.25)
        future.result(timeout=10)
    assert finished.is_set()


def test_postgres_commit_failure_rolls_back_ledger_slot_and_disposition(retirement_pg):
    value = retirement_pg
    proposed, operation = checks.plan(value), str(uuid4())
    with value.core.db.session() as session:
        before_ledger = copy.deepcopy(session.get(EditorialLedger, "hourly").payload)
        before_slot = recovery._view(session.get(PublicationSlot, value.slot_id))
    session_class = value.core.db.session.class_

    def fail_commit(session, _context, _instances):
        if any(isinstance(row, Audit) and row.action == recovery.COMMIT for row in session.new):
            raise RuntimeError("simulated atomic commit failure")

    event.listen(session_class, "before_flush", fail_commit)
    try:
        with pytest.raises(RuntimeError, match="atomic commit failure"):
            checks.apply(value, proposed, operation)
    finally:
        event.remove(session_class, "before_flush", fail_commit)
    with value.core.db.session() as session:
        assert session.get(EditorialLedger, "hourly").payload == before_ledger
        assert recovery._view(session.get(PublicationSlot, value.slot_id)) == before_slot
        assert session.get(Audit, recovery._id(value.variant_id, "commit")) is None
        assert session.get(Audit, recovery._id(value.variant_id, "claim")) is not None
    reads = len(value.reads)
    assert checks.apply(value, proposed, operation)["state"] == "retired_no_send"
    assert len(value.reads) == reads


def test_postgres_legacy_approval_serializes_with_first_retirement_claim(
    retirement_pg, monkeypatch
):
    import max_postiz_agent.service as service_module

    value = retirement_pg
    proposed = checks.plan(value)
    approval_entered, release_approval, capture_entered = (
        threading.Event(),
        threading.Event(),
        threading.Event(),
    )
    original_slot, original_capture = service_module.next_slot, recovery._capture

    def held_slot(*args, **kwargs):
        approval_entered.set()
        assert release_approval.wait(15)
        return original_slot(*args, **kwargs)

    def capture(*args, **kwargs):
        if kwargs.get("locked"):
            capture_entered.set()
        return original_capture(*args, **kwargs)

    monkeypatch.setattr(service_module, "next_slot", held_slot)
    monkeypatch.setattr(recovery, "_capture", capture)

    def legacy_approval():
        with value.core.db.session.begin() as session:
            draft = session.get(Draft, session.get(Variant, value.variant_id).draft_id)
            Service._approve(value.core, session, draft, "legacy-fixture-owner")

    with ThreadPoolExecutor(max_workers=2) as pool:
        approval = pool.submit(legacy_approval)
        assert approval_entered.wait(15)
        retirement = pool.submit(checks.apply, value, proposed)
        try:
            assert capture_entered.wait(15)
            assert not retirement.done()
            # The attempted retirement has reached its native capture but must
            # wait behind the still-uncommitted legacy approval transaction.
            with pytest.raises(TimeoutError):
                retirement.result(timeout=0.25)
        finally:
            release_approval.set()
        approval.result(timeout=20)
        with pytest.raises(AgentError):
            retirement.result(timeout=20)
    with value.core.db.session() as session:
        assert session.get(Audit, recovery._id(value.variant_id, "claim")) is None
    assert len(value.reads) == 2

@pytest.mark.parametrize("timed_out_schedule", [False, True])
def test_mobile_owner_retires_only_proven_unsent_draft(retirement_pg, timed_out_schedule, monkeypatch):
    value = retirement_pg
    if timed_out_schedule:
        checks.schedule_timeout(value)
        monkeypatch.setattr(mobile_domain, "now", lambda: value.stamp[0])
    actor = "postiz:mobile:" + "a" * 32

    class Owner:
        def authenticate(self, _authorization):
            return MobilePrincipal(actor, "owner", now())

    with TestClient(mobile_app(value.core.db, MobileDomain(value.core), Owner(),
                               environment="fixture", write_enabled=True),
                    headers={"Authorization": "Bearer fixture"}) as client:
        detail = client.get("/mobile/v1/queue/" + value.variant_id)
        assert detail.status_code == 200, detail.json()
        assert "retire_preapproval_no_send" in detail.json()["available_actions"]
        response = client.post("/mobile/v1/operation-plans", json={
            "action": "retire_preapproval_no_send", "object_id": value.record_id,
        })
        assert response.status_code == 201, response.json()
        plan = response.json()
        request = {"operation_id": str(uuid4()), "plan_id": plan["plan_id"],
                   "plan_hash": plan["plan_hash"], "confirmed": True,
                   "reason": "Verified no send before approval"}
        response = client.post("/mobile/v1/operations", json=request)
        assert response.status_code == 202, response.json()
        receipt = response.json()
        assert receipt["state"] == "succeeded", receipt
        assert receipt["result"]["outcome"] == "retired_no_send"
        assert receipt["result"]["external_mutations"] == 0
        assert client.get("/mobile/v1/operations/" + request["operation_id"]).json() == receipt
        assert client.post("/mobile/v1/operations", json=request).json() == receipt
        assert client.get("/mobile/v1/audit").json()["items"] == [receipt]

        # A process crash between native commit and mobile receipt must be read-only on replay.
        with value.core.db.session.begin() as session:
            operation = session.get(MobileOperation, request["operation_id"])
            operation.state, operation.result = "running", None
        reads = len(value.reads)
        resolved = client.get("/mobile/v1/operations/" + request["operation_id"]).json()
        assert resolved["state"] == "succeeded" and resolved["result"] == receipt["result"]
        assert client.post("/mobile/v1/operations", json=request).json() == resolved
        assert len(value.reads) == reads

    with value.core.db.session() as session:
        assert session.get(PublicationSlot, value.slot_id).state == "missed"
    assert all(post["state"] == "DRAFT" for post in value.posts.values())
    assert len(value.events) == value.model_count
