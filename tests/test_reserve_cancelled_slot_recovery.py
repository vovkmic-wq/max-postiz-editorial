"""Известная отмена без отправки освобождает слот; неопределённость сохраняет его."""
import copy
from datetime import UTC, datetime

import pytest

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Base, Audit, uid
from max_postiz_agent.hub_models import BridgePost, Dispatch, DispatchAttempt, RemoteDraft, Target, TargetApproval
from max_postiz_agent.server_models import EditorialLedger, PublicationSlot

pytest_plugins = ["test_hourly_reserve_controller"]


def database_snapshot(core):
    """Полный снимок тестовой БД, включая raw evidence и прежние approvals."""
    with core.db.session() as session:
        return {table.name: sorted(
            [dict(row) for row in session.execute(table.select()).mappings()], key=repr)
            for table in Base.metadata.sorted_tables}


@pytest.fixture
def cancelled_slot(reserve_controller):
    value = reserve_controller
    value.controller.prepare_once()
    value.controller.run_once()
    record = value.cycle._load()["records"][0]
    slot = value.store.get(value.store.status()["active"][0]["id"])
    value.stamp[0] = record["due_at"] + 1
    value.posts[record["postiz_id"]]["state"] = "ERROR"
    value.core.bridge.sync(force=True)
    assert value.cycle.status(record["id"])["state"] == "cancelled"
    value.state.control("pause", value.state.read()["revision"], "test-owner")
    value.state.control("start", value.state.read()["revision"], "test-owner")
    assert slot["control_revision"] != value.state.read()["revision"]
    value.failed_record = copy.deepcopy(value.cycle._load()["records"][0])
    value.failed_slot = slot
    value.remote_error = copy.deepcopy(value.posts[record["postiz_id"]])
    return value


def release(value, **kwargs):
    return value.store.release_cancelled_no_send(value.failed_slot["id"],
        token=kwargs.pop("token", value.failed_slot["token"]),
        remote=kwargs.pop("remote", copy.deepcopy(value.remote_error)),
        expected_revision=kwargs.pop("expected_revision", value.state.read()["revision"]), **kwargs)


def test_exact_cancelled_zero_attempt_slot_is_released_across_pause_revision(cancelled_slot):
    value = cancelled_slot
    before = database_snapshot(value.core)
    result = release(value)
    after = database_snapshot(value.core)
    assert result["state"] == "missed"
    assert value.store.status()["active"] == []
    assert value.cycle._load()["records"][0] == value.failed_record
    assert {table for table in before if before[table] != after[table]} == {
        "publication_slots", "audit_events"}
    slot = value.store.get(value.failed_slot["id"])
    for key in ("id", "token", "control_revision", "binding", "due_at", "dispatch_id", "text_hash", "evidence_hash"):
        assert slot[key] == value.failed_slot[key]
    with value.core.db.session() as session:
        dispatch = session.get(Dispatch, value.failed_record["dispatch_id"])
        assert dispatch.state == "cancelled" and dispatch.attempts == 0
        assert session.get(TargetApproval, dispatch.approval_id).revoked
        assert session.get(BridgePost, value.failed_record["postiz_id"]).active
        assert session.query(DispatchAttempt).count() == 0
        assert session.query(Audit).count() == len(before["audit_events"]) + 1
    assert len(value.writes) == 1
    stable = database_snapshot(value.core)
    with pytest.raises(AgentError):
        release(value)
    assert database_snapshot(value.core) == stable


@pytest.mark.parametrize("change", [
    "attempt_counter", "unknown_attempt", "published_attempt", "sending", "unknown", "published",
    "owner", "lease", "verified", "external_id", "permalink", "unrevoked_approval",
    "mutation_owner", "mutation_until", "inactive_bridge", "bridge_error",
    "slot_binding", "slot_dispatch", "slot_due", "blocked_unknown_slot", "record_unknown",
    "remote_queue", "remote_published", "remote_missing_state", "remote_content", "remote_group",
    "remote_integration", "remote_id", "remote_release_id", "remote_release_url", "remote_date",
    "signed_snapshot", "future_due", "stale_revision", "wrong_token", "paused", "browser_receipt", "unpaused_target",
])
def test_uncertain_or_changed_state_never_releases_target(cancelled_slot, change):
    value = cancelled_slot
    remote = copy.deepcopy(value.remote_error)
    revision = value.state.read()["revision"]
    token = value.failed_slot["token"]
    with value.core.db.session.begin() as session:
        dispatch = session.get(Dispatch, value.failed_record["dispatch_id"])
        bridge = session.get(BridgePost, value.failed_record["postiz_id"])
        slot = session.get(PublicationSlot, value.failed_slot["id"])
        if change == "attempt_counter":
            dispatch.attempts = 1
        elif change in {"unknown_attempt", "published_attempt"}:
            session.add(DispatchAttempt(dispatch_id=dispatch.id,
                result="unknown" if change == "unknown_attempt" else "published", created_at=value.stamp[0]))
        elif change in {"sending", "unknown", "published"}:
            dispatch.state = change
        elif change == "owner":
            dispatch.owner = "worker-owner"
        elif change == "lease":
            dispatch.lease_until = value.stamp[0] + 60
        elif change == "verified":
            dispatch.verified = True
        elif change in {"external_id", "permalink"}:
            setattr(dispatch, change, "delivery-evidence")
        elif change == "unrevoked_approval":
            session.get(TargetApproval, dispatch.approval_id).revoked = False
        elif change == "unpaused_target":
            session.get(Target, "max").paused = False
        elif change == "mutation_owner":
            bridge.mutation_owner = "postiz-worker"
        elif change == "mutation_until":
            bridge.mutation_until = value.stamp[0] + 60
        elif change == "inactive_bridge":
            bridge.active = False
        elif change == "bridge_error":
            bridge.error = "REMOTE_EDIT_DURING_SEND"
        elif change == "slot_binding":
            slot.binding = {**slot.binding, "original_hash": "0" * 64}
        elif change == "slot_dispatch":
            slot.dispatch_id = None
        elif change == "slot_due":
            slot.due_at += .002
        elif change == "blocked_unknown_slot":
            slot.state = "blocked_unknown"
            dispatch.state = "unknown"
        elif change == "record_unknown":
            ledger = session.get(EditorialLedger, "hourly")
            ledger.payload = {**ledger.payload, "records": [
                {**record, "state": "unknown"} if record["id"] == value.failed_record["id"] else record
                for record in ledger.payload["records"]]}
        elif change in {"remote_queue", "remote_published"}:
            remote["state"] = "QUEUE" if change == "remote_queue" else "PUBLISHED"
        elif change == "remote_missing_state":
            remote.pop("state")
        elif change == "remote_content":
            remote["content"] += " Изменённый текст."
        elif change == "remote_group":
            remote["group"] = "other-group"
        elif change == "remote_integration":
            remote["integration"] = {**remote["integration"], "id": "other-integration"}
        elif change == "remote_id":
            remote["id"] = "other-post"
        elif change in {"remote_release_id", "remote_release_url"}:
            remote["releaseId" if change == "remote_release_id" else "releaseURL"] = "already-delivered"
        elif change == "remote_date":
            remote["publishDate"] = datetime.fromtimestamp(slot.due_at + .002, UTC).isoformat()
        elif change == "signed_snapshot":
            dispatch.snapshot = {**dispatch.snapshot, "body": dispatch.snapshot["body"] + " changed"}
        elif change == "future_due":
            value.stamp[0] = slot.due_at - 1
        elif change == "stale_revision":
            revision -= 1
        elif change == "wrong_token":
            token = "wrong-token"
    if change == "paused":
        value.state.control("pause", revision, "test-owner")
        revision = value.state.read()["revision"]
    elif change == "browser_receipt":
        directory = value.core.settings.root / "data/browser-publications"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / (value.failed_record["dispatch_id"] + ".json")).write_text("{}", encoding="utf-8")
    before = database_snapshot(value.core)
    with pytest.raises(AgentError):
        release(value, remote=remote, token=token, expected_revision=revision)
    assert database_snapshot(value.core) == before
    assert value.store.status()["active"][0]["id"] == value.failed_slot["id"]
    assert len(value.writes) == 1


def test_controller_releases_known_cancelled_slot_and_next_tick_remains_runnable(cancelled_slot):
    value = cancelled_slot
    result = value.controller.run_once()
    assert result["phase"] == "reconciling" and result["slot_state"] == "missed"
    assert value.store.status()["active"] == []
    assert value.controller.run_once()["phase"] == "reserve_empty"
    assert value.cycle._load()["records"][0] == value.failed_record
    assert len(value.posts) == len(value.writes) == 1


def test_old_generic_blocked_marker_is_recoverable_only_with_exact_no_send_proof(cancelled_slot):
    value = cancelled_slot
    with value.core.db.session.begin() as session:
        slot = session.get(PublicationSlot, value.failed_slot["id"])
        slot.state, slot.error = "blocked_unknown", "ARTICLE_RECONCILIATION_REQUIRED"
    assert release(value)["state"] == "missed"
    assert value.cycle._load()["records"][0] == value.failed_record


def second_candidate(value):
    article = {**value.article, "url": "https://science.example.org/article/unused/",
        "original": ("Researchers measured magnetic fields around a distant planet and recorded a faint radio signal. "
                     "The new analysis compares local particles with previous measurements to explain its structure. ") * 2}
    value.controller.selector = lambda *_args, **kwargs: (copy.deepcopy(article) if not any(
        record.get("url") == article["url"] for record in kwargs["ledger"]["records"]) else None)
    value.controller.prepare_once()
    return value.cycle._load()["records"][-1]


def test_recovery_schedules_distinct_ready_source_without_repeating_cancelled_post(cancelled_slot):
    value = cancelled_slot
    prepared = second_candidate(value)
    assert prepared["state"] == "quality_ready"
    result = value.controller.run_once()
    assert result["phase"] == "reconciling" and result["slot_state"] == "missed"
    assert value.controller.run_once()["phase"] == "waiting_dispatch"
    records = value.cycle._load()["records"]
    assert records[0] == value.failed_record
    assert records[1]["id"] == prepared["id"] and records[1]["state"] == "pending"
    assert records[1]["postiz_id"] != records[0]["postiz_id"]
    assert len(value.posts) == len(value.writes) == 2
    # Прежний час не переиспользован после missed; его source claim остаётся в истории.
    assert records[1]["due_at"] // 3600 > value.failed_slot["due_at"] // 3600
    with value.core.db.session() as session:
        assert session.get(Dispatch, records[0]["dispatch_id"]).state == "cancelled"
        assert session.get(Dispatch, records[0]["dispatch_id"]).attempts == 0
        assert session.query(DispatchAttempt).count() == 0


def other_dispatch(value, record, state):
    with value.core.db.session.begin() as session:
        approval = TargetApproval(variant_id=record["variant_id"], revision=1,
            snapshot_hash=fingerprint("historical-approval"), scheduled_at=value.stamp[0] - 7200,
            actor="test-historical", revoked=state == "published")
        session.add(approval)
        session.flush()
        dispatch = Dispatch(variant_id=record["variant_id"], approval_id=approval.id, revision=1,
            snapshot_hash=approval.snapshot_hash, snapshot={}, due_at=approval.scheduled_at,
            state=state, attempts=1 if state == "published" else 0, verified=state == "published")
        session.add(dispatch)
        session.flush()
        if state == "published":
            session.add(DispatchAttempt(dispatch_id=dispatch.id, result="published", created_at=dispatch.due_at))
        return dispatch.id


def test_historical_attempt_for_another_article_is_preserved_and_does_not_block_recovery(cancelled_slot):
    value = cancelled_slot
    other = second_candidate(value)
    dispatch_id = other_dispatch(value, other, "published")
    before = database_snapshot(value.core)
    assert release(value)["state"] == "missed"
    after = database_snapshot(value.core)
    assert after["hub_dispatch_attempts"] == before["hub_dispatch_attempts"]
    assert after["hub_dispatches"] == before["hub_dispatches"]
    with value.core.db.session() as session:
        assert session.get(Dispatch, dispatch_id).verified


@pytest.mark.parametrize("other_state", ["pending", "sending", "unknown", "lease", "mutation", "remote_unknown"])
def test_other_target_activity_prevents_release(cancelled_slot, other_state):
    value = cancelled_slot
    other = second_candidate(value)
    if other_state in {"pending", "sending", "unknown", "lease"}:
        dispatch_id = other_dispatch(value, other, "cancelled" if other_state == "lease" else other_state)
        if other_state == "lease":
            with value.core.db.session.begin() as session:
                session.get(Dispatch, dispatch_id).lease_until = value.stamp[0] + 60
    elif other_state == "mutation":
        with value.core.db.session.begin() as session:
            session.add(BridgePost(post_id="other-post", integration_id="integration-max", group_id="other-group",
                target_id="max", variant_id=other["variant_id"], mutation_owner="other-worker",
                mutation_until=value.stamp[0] + 60, remote_hash=fingerprint("other-remote"),
                remote_state="DRAFT", due_at=value.stamp[0]))
    else:
        with value.core.db.session.begin() as session:
            session.add(RemoteDraft(variant_id=other["variant_id"], state="unknown", synced_revision=1,
                correlation=uid()))
    before = database_snapshot(value.core)
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_CANCELLED_NOT_SAFE"):
        release(value)
    assert database_snapshot(value.core) == before


def test_postiz_get_failure_preserves_slot_and_does_not_create_another_post(cancelled_slot, monkeypatch):
    value = cancelled_slot

    def unavailable(_post_id):
        raise AgentError("POSTIZ_UNAVAILABLE", 503)

    monkeypatch.setattr(value.core.postiz, "get_post", unavailable)
    with pytest.raises(AgentError, match="POSTIZ_UNAVAILABLE"):
        value.controller.run_once()
    assert value.store.get(value.failed_slot["id"])["state"] == "pending"
    assert value.store.status()["active"][0]["id"] == value.failed_slot["id"]
    assert len(value.posts) == len(value.writes) == 1


def test_control_change_during_fresh_get_invalidates_recovery_fence(cancelled_slot, monkeypatch):
    value = cancelled_slot

    def control_changes(_post_id):
        value.state.control("pause", value.state.read()["revision"], "other-owner-action")
        value.state.control("start", value.state.read()["revision"], "other-owner-action")
        return copy.deepcopy(value.remote_error)

    monkeypatch.setattr(value.core.postiz, "get_post", control_changes)
    with pytest.raises(AgentError, match="EDITORIAL_REVISION_CONFLICT"):
        value.controller.run_once()
    assert value.store.get(value.failed_slot["id"])["state"] == "pending"
    assert value.store.status()["active"][0]["id"] == value.failed_slot["id"]
    assert len(value.posts) == len(value.writes) == 1
