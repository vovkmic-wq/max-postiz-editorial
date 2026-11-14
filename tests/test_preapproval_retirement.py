"""Native preapproval retirement preserves history and never performs Send."""

import copy
from datetime import datetime, timezone
import hashlib
import json
from uuid import uuid4
from types import SimpleNamespace

import pytest
import httpx
from fastapi.testclient import TestClient
from sqlalchemy import select

from max_postiz_agent import preapproval_retirement as recovery
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Base, Draft, Intent, Job
from max_postiz_agent.hub_models import (
    BridgePost,
    Dispatch,
    DispatchAttempt,
    RemoteDraft,
    Target,
    TargetApproval,
    Variant,
    VariantRevision,
)
from max_postiz_agent.server_models import (
    EditorialLedger,
    EditorialPreparationClaim,
    PublicationSlot,
)
from max_postiz_agent.server_api import editorial_app
from max_postiz_agent.service import Service
from max_postiz_agent.postiz_http_journal import PostizHttpJournal

pytest_plugins = ["test_illustrated_article_flow"]


def snapshot(core):
    with core.db.session() as session:
        return {
            table.name: sorted(
                [dict(x) for x in session.execute(table.select()).mappings()], key=repr
            )
            for table in Base.metadata.sorted_tables
        }


@pytest.fixture
def retirement_case(illustrated, monkeypatch):
    value = illustrated
    value.controller.prepare_once()
    ready = copy.deepcopy(value.cycle._load()["records"][0])
    valid = value.cycle.validate_reserve(ready["id"])
    slot = value.store.assign(
        ready,
        value.stamp[0] + 180,
        text_hash=ready["reserve_prepared_hash"],
        evidence_hash=valid["reserve_evidence_hash"],
    )
    # A real single DRAFT export/import, followed by the old failure marker.
    value.core.stage_remote(ready["variant_id"])
    with value.core.db.session.begin() as session:
        ledger = session.get(EditorialLedger, "hourly")
        payload = copy.deepcopy(ledger.payload)
        record = next(r for r in payload["records"] if r["id"] == ready["id"])
        record.update(
            state="needs_attention", error="ARTICLE_REMOTE_CHANGED", publication_slot_id=slot["id"]
        )
        ledger.payload = payload
        row = session.get(PublicationSlot, slot["id"])
        row.state, row.error = "blocked_unknown", "ARTICLE_RECONCILIATION_REQUIRED"
        variant = session.get(Variant, ready["variant_id"])
        session.get(RemoteDraft, variant.id).synced_revision = variant.revision - 1
    value.stamp[0] = slot["due_at"] + 1
    value.state.control("pause", value.state.read()["revision"], "test-owner")
    monkeypatch.setattr(recovery, "now", lambda: value.stamp[0])
    reads = []

    def read(post_id):
        reads.append(post_id)
        return copy.deepcopy(value.posts[post_id])

    monkeypatch.setattr(value.core.postiz, "get_post_fresh", read, raising=False)
    with value.core.db.session() as session:
        target = session.get(Variant, ready["variant_id"]).target_id
        from max_postiz_agent.hub_models import Target

        target = session.get(Target, target)
        account = {
            "id": target.options["postiz_integration_id"],
            "identifier": "max-browser",
            "accountId": target.destination,
            "disabled": False,
            "refreshNeeded": False,
        }
    value.integration = account
    monkeypatch.setattr(
        value.core.postiz, "integrations_fresh", lambda: [copy.deepcopy(account)], raising=False
    )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("external mutation/model/Send forbidden")

    for method in ("create", "upload", "delete", "reschedule_existing"):
        monkeypatch.setattr(value.core.postiz, method, forbidden)
    monkeypatch.setattr(value.core.publisher, "publish", forbidden)
    value.record_id, value.variant_id, value.slot_id, value.reads = (
        ready["id"],
        ready["variant_id"],
        slot["id"],
        reads,
    )
    value.model_count = len(value.events)
    return value


def plan(value):
    return recovery.inspect(value.core, value.record_id)


def apply(value, proposed=None, operation_id=None):
    proposed = proposed or plan(value)
    return recovery.apply(
        value.core,
        value.record_id,
        operation_id=operation_id or str(uuid4()),
        plan_hash=proposed["plan_hash"],
        actor="postiz:owner",
        retire_confirmed=True,
        reason="Owner retires this exact preapproval DRAFT without sending or reusing its source",
    )


def check_inspect_readonly(value):
    before = snapshot(value.core)
    proposed = plan(value)
    assert proposed["contract"] == recovery.CONTRACT and len(value.reads) == 2
    assert snapshot(value.core) == before and len(value.events) == value.model_count


def test_inspect_dryrun_is_readonly_and_double_reads(retirement_case):
    check_inspect_readonly(retirement_case)


def check_retire(value):
    before = snapshot(value.core)
    proposed = plan(value)
    result = apply(value, proposed)
    after = snapshot(value.core)
    assert result["state"] == "retired_no_send"
    changed = {key for key in before if before[key] != after[key]}
    assert changed == {"editorial_article_ledger", "publication_slots", "audit_events"}
    record = next(
        r
        for r in after["editorial_article_ledger"][0]["payload"]["records"]
        if r["id"] == value.record_id
    )
    assert record["state"] == "cancelled" and record["error"] == "ARTICLE_REMOTE_CHANGED"
    assert record["reserve_claim"] == proposed["snapshot"]["record"]["reserve_claim"]
    slot = next(r for r in after["publication_slots"] if r["id"] == value.slot_id)
    assert (
        slot["state"] == "missed" and slot["active_target"] is None and slot["dispatch_id"] is None
    )
    for key in ("token", "slot_at", "due_at", "binding", "text_hash", "evidence_hash", "record_id"):
        assert slot[key] == proposed["snapshot"]["slot"][key]
    assert (
        not after["hub_approvals"]
        and not after["hub_dispatches"]
        and not after["hub_dispatch_attempts"]
    )
    assert len(value.events) == value.model_count and all(
        p["state"] == "DRAFT" for p in value.posts.values()
    )
    return result, proposed


def test_retirement_is_atomic_preserves_every_history_table(retirement_case):
    check_retire(retirement_case)


def schedule_timeout(value, *, status=504, late=True):
    value.stamp[0] += 600
    with value.core.db.session.begin() as session:
        slot = session.get(PublicationSlot, value.slot_id)
        bridge = session.scalar(select(BridgePost).where(BridgePost.variant_id == value.variant_id))
        started = slot.due_at - 100
        bridge.updated_at = started + (71 if late else 1)
        bridge.mutation_owner = str(uuid4())
        bridge.mutation_until = bridge.updated_at + 300
        ledger = session.get(EditorialLedger, "hourly")
        payload = copy.deepcopy(ledger.payload)
        record = next(r for r in payload["records"] if r["id"] == value.record_id)
        record.update(error="POSTIZ_UNKNOWN", postiz_id=bridge.post_id, due_at=slot.due_at)
        ledger.payload = payload
        # The project can remain in RUN while the exact blocked destination is paused.
        from max_postiz_agent.server_models import EditorialControl

        session.get(EditorialControl, "main").paused = False
        post_digest = hashlib.sha256(bridge.post_id.encode()).hexdigest()
    PostizHttpJournal(value.core.settings.root).append({
        "at": datetime.fromtimestamp(started, timezone.utc).isoformat(),
        "method": "POST", "endpoint": "/posts", "operation": "schedule",
        "post_id_sha256": post_digest, "http_status": status, "outcome": "http_error",
        "error_code": "POSTIZ_UNKNOWN", "stage": "response", "cas": "missing",
        "elapsed_ms": 61184, "read_timeout_s": 75,
    })


def bind_current_draft(value, *, corrupt_plaintext=False):
    with value.core.db.session.begin() as session:
        variant = session.get(Variant, value.variant_id)
        draft = session.get(RemoteDraft, variant.id)
        draft.synced_revision = variant.revision
        assert draft.remote_hash == fingerprint({"body": variant.body})
        if corrupt_plaintext:
            draft.remote_hash = fingerprint({"body": "changed"})
        ledger = session.get(EditorialLedger, "hourly")
        payload = copy.deepcopy(ledger.payload)
        record = next(r for r in payload["records"] if r["id"] == value.record_id)
        target = session.get(Target, variant.target_id)
        record["prepared_hash"] = fingerprint(value.core.snapshot(variant, target))
        assert record["prepared_hash"] != record["reserve_prepared_hash"]
        ledger.payload = payload


@pytest.mark.parametrize("current_draft_revision", [False, True])
def test_closes_expired_504_slot_and_releases_only_its_late_lease(retirement_case, current_draft_revision):
    value = retirement_case
    schedule_timeout(value)
    if current_draft_revision:
        bind_current_draft(value)
    before = snapshot(value.core)
    proposal = plan(value)
    assert snapshot(value.core) == before
    result = apply(value, proposal)
    after = snapshot(value.core)
    assert result["state"] == "retired_no_send"
    assert result["disposition"]["schedule_timeout"]["http_status"] == 504
    assert {key for key in before if before[key] != after[key]} == {
        "editorial_article_ledger", "publication_slots", "audit_events", "hub_bridge_posts"
    }
    bridge = after["hub_bridge_posts"][0]
    assert bridge["mutation_owner"] is None and bridge["mutation_until"] == 0
    assert bridge["remote_state"] == "DRAFT" and bridge["active"] is True
    slot = next(row for row in after["publication_slots"] if row["id"] == value.slot_id)
    assert slot["state"] == "missed" and slot["active_target"] is None
    assert not after["hub_dispatches"] and not after["hub_approvals"]
    assert len(value.events) == value.model_count


@pytest.mark.parametrize("kwargs", [{"status": 502}, {"late": False}])
def test_timeout_without_matching_504_and_late_lease_stays_blocked(retirement_case, kwargs):
    value = retirement_case
    schedule_timeout(value, **kwargs)
    before = snapshot(value.core)
    with pytest.raises(AgentError):
        plan(value)
    assert snapshot(value.core) == before and not value.reads


@pytest.mark.parametrize("proven_timeout", [False, True])
def test_current_draft_requires_proven_timeout_and_exact_plaintext(retirement_case, proven_timeout):
    value = retirement_case
    if proven_timeout:
        schedule_timeout(value)
    bind_current_draft(value, corrupt_plaintext=proven_timeout)
    before = snapshot(value.core)
    with pytest.raises(AgentError, match="PREAPPROVAL_RETIREMENT_NOT_SAFE"):
        plan(value)
    assert snapshot(value.core) == before and not value.reads


def check_replay(value):
    proposed = plan(value)
    operation = str(uuid4())
    first = apply(value, proposed, operation)
    before = snapshot(value.core)
    reads = len(value.reads)
    assert apply(value, proposed, operation) == first
    assert snapshot(value.core) == before and len(value.reads) == reads


def test_completed_operation_replays_receipt_without_effects(retirement_case):
    check_replay(retirement_case)


def check_owner(value):
    proposed = plan(value)
    before = snapshot(value.core)
    with pytest.raises(AgentError, match="OWNER_DECISION"):
        recovery.apply(
            value.core,
            value.record_id,
            operation_id=str(uuid4()),
            plan_hash=proposed["plan_hash"],
            actor="postiz:owner",
            retire_confirmed=False,
            reason="No owner retirement decision was given",
        )
    assert snapshot(value.core) == before


def test_explicit_owner_retirement_decision_is_required(retirement_case):
    check_owner(retirement_case)


def check_pause_cas(value):
    proposed = plan(value)
    value.state.control("pause", value.state.read()["revision"], "owner-changed-pause")
    before = snapshot(value.core)
    with pytest.raises(AgentError, match="PLAN_CHANGED"):
        apply(value, proposed)
    assert snapshot(value.core) == before


def test_pause_revision_cas_conflict_has_no_effect(retirement_case):
    check_pause_cas(retirement_case)


def check_revoked(value):
    with value.core.db.session.begin() as session:
        session.add(
            TargetApproval(
                variant_id=value.variant_id,
                revision=1,
                snapshot_hash="0" * 64,
                scheduled_at=value.stamp[0],
                actor="old-owner",
                revoked=True,
            )
        )
    before = snapshot(value.core)
    with pytest.raises(AgentError):
        plan(value)
    assert snapshot(value.core) == before


def test_even_revoked_approval_prevents_no_send_retirement(retirement_case):
    check_revoked(retirement_case)


@pytest.mark.parametrize("change", ["QUEUE", "PUBLISHED", "ERROR", "missing"])
def test_remote_status_unknown_or_not_draft_stays_blocked(retirement_case, change):
    value = retirement_case
    post = next(iter(value.posts.values()))
    if change == "missing":
        post.pop("state")
    else:
        post["state"] = change
    before = snapshot(value.core)
    with pytest.raises((AgentError, KeyError)):
        plan(value)
    assert snapshot(value.core) == before


@pytest.mark.parametrize("change", ["text", "group", "integration", "id", "media"])
def test_remote_binding_and_media_drift_rejected(retirement_case, change):
    value = retirement_case
    post = next(iter(value.posts.values()))
    if change == "text":
        post["content"] += " different"
    elif change == "group":
        post["group"] = "other"
    elif change == "integration":
        post["integration"]["id"] = "other"
    elif change == "id":
        post["id"] = "other"
    else:
        post["image"] = "[]"
    before = snapshot(value.core)
    with pytest.raises(AgentError):
        plan(value)
    assert snapshot(value.core) == before


def check_trace(value):
    record = value.cycle._record(value.cycle._load(), value.record_id)
    (value.core.settings.root / record["quality_trace"]).write_bytes(b"changed")
    before = snapshot(value.core)
    with pytest.raises(AgentError):
        plan(value)
    assert snapshot(value.core) == before


def test_corrupt_preserved_trace_blocks(retirement_case):
    check_trace(retirement_case)


def check_unknown_read(value, monkeypatch):
    proposed = plan(value)
    original = recovery._observe

    def unavailable(*args, **kwargs):
        raise AgentError("POSTIZ_UNAVAILABLE", 503)

    monkeypatch.setattr(recovery, "_observe", unavailable)
    operation = str(uuid4())
    with pytest.raises(AgentError, match="POSTIZ_UNAVAILABLE"):
        apply(value, proposed, operation)
    monkeypatch.setattr(recovery, "_observe", original)
    before = len(value.reads)
    with pytest.raises(AgentError, match="READ_UNKNOWN"):
        apply(value, proposed, operation)
    assert (
        len(value.reads) == before and value.store.get(value.slot_id)["state"] == "blocked_unknown"
    )


def test_started_without_durable_read_response_remains_unknown(retirement_case, monkeypatch):
    check_unknown_read(retirement_case, monkeypatch)


def check_saved_resume(value, monkeypatch):
    proposed = plan(value)
    original = recovery._observe
    operation = str(uuid4())
    calls = []

    class Crash(BaseException):
        pass

    def after_saved(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append(result)
        if len(calls) == 1:
            raise Crash()
        return result

    monkeypatch.setattr(recovery, "_observe", after_saved)
    with pytest.raises(Crash):
        apply(value, proposed, operation)
    assert len(value.reads) == 3
    monkeypatch.setattr(recovery, "_observe", original)
    assert apply(value, proposed, operation)["state"] == "retired_no_send"
    assert len(value.reads) == 4


def test_saved_read_response_resumes_without_repeating_get(retirement_case, monkeypatch):
    check_saved_resume(retirement_case, monkeypatch)


@pytest.mark.parametrize("stage", ["read1_completed", "read2_completed"])
def test_crash_after_completed_stage_resumes_once(retirement_case, monkeypatch, stage):
    value = retirement_case
    proposed = plan(value)
    original = recovery._event
    operation = str(uuid4())

    class Crash(BaseException):
        pass

    def event(*args):
        result = original(*args)
        if args[2] == stage:
            raise Crash()
        return result

    monkeypatch.setattr(recovery, "_event", event)
    with pytest.raises(Crash):
        apply(value, proposed, operation)
    monkeypatch.setattr(recovery, "_event", original)
    assert apply(value, proposed, operation)["state"] == "retired_no_send"
    assert len(value.reads) == 4


def check_guard(value):
    check_retire(value)
    before = snapshot(value.core)
    with pytest.raises(AgentError, match="RETIRED_OR_RECONCILING"):
        with value.core.db.session.begin() as session:
            value.core._variant(session, value.variant_id, True)
    assert snapshot(value.core) == before


def test_retired_variant_cannot_reenter_mutation_paths(retirement_case):
    check_guard(retirement_case)


@pytest.mark.parametrize(
    "field,value",
    [("accountId", "other"), ("identifier", "other"), ("disabled", True), ("refreshNeeded", True)],
)
def test_live_integration_rebinding_or_disabled_state_blocks(retirement_case, field, value):
    case = retirement_case
    case.integration[field] = value
    before = snapshot(case.core)
    with pytest.raises(AgentError, match="INTEGRATION_CHANGED"):
        plan(case)
    assert snapshot(case.core) == before


def test_slow_photo_read_cannot_refresh_an_old_post_observation(retirement_case, monkeypatch):
    case = retirement_case
    original = recovery._photo

    def slow(*args):
        case.stamp[0] += recovery.MAX_AGE + 1
        return original(*args)

    monkeypatch.setattr(recovery, "_photo", slow)
    with pytest.raises(AgentError, match="OBSERVATION_STALE"):
        plan(case)


def test_photo_mime_must_match_saved_original(retirement_case):
    case = retirement_case
    case.core.postiz.client.close()
    case.core.postiz.client = httpx.Client(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(
                200, content=case.image_path.read_bytes(), headers={"Content-Type": "image/png"}
            )
        )
    )
    with pytest.raises(AgentError):
        plan(case)


def test_prepared_revision_payload_is_cryptographically_bound(retirement_case):
    case = retirement_case
    with case.core.db.session.begin() as session:
        row = session.scalar(
            select(VariantRevision).where(
                VariantRevision.variant_id == case.variant_id, VariantRevision.revision == 1
            )
        )
        row.payload = {**row.payload, "body": row.payload["body"] + " changed"}
    with pytest.raises(AgentError, match="PREPARED_HISTORY_CHANGED"):
        plan(case)


def test_saved_response_corruption_never_repeats_completed_get(retirement_case, monkeypatch):
    case = retirement_case
    proposed, operation = plan(case), str(uuid4())
    original = recovery._event

    class Crash(BaseException):
        pass

    def event(*args):
        result = original(*args)
        if args[2] == "read1_completed":
            raise Crash()
        return result

    monkeypatch.setattr(recovery, "_event", event)
    with pytest.raises(Crash):
        apply(case, proposed, operation)
    path = (
        case.core.settings.root
        / "data/preapproval-reconciliations"
        / operation
        / "read1.local.json"
    )
    raw = json.loads(path.read_bytes())
    raw["raw_postiz"]["content"] += " changed"
    path.write_text(json.dumps(raw))
    count = len(case.reads)
    monkeypatch.setattr(recovery, "_event", original)
    with pytest.raises(AgentError):
        apply(case, proposed, operation)
    assert len(case.reads) == count and case.store.get(case.slot_id)["state"] == "blocked_unknown"


@pytest.fixture
def recovery_api(retirement_case, monkeypatch):
    case = retirement_case
    monkeypatch.setenv("EDITORIAL_INTERNAL_KEY", "fixture-preapproval-internal-key-000000000000")
    monkeypatch.setenv("EDITORIAL_OWNER_USER_ID", "owner-id")
    monkeypatch.setenv("EDITORIAL_OWNER_ORGANIZATION_ID", "org-id")
    headers = {
        "X-Editorial-Key": "fixture-preapproval-internal-key-000000000000",
        "X-Editorial-Owner": "owner-id",
        "X-Editorial-Organization": "org-id",
    }
    return case, TestClient(editorial_app(case.core, case.state)), headers


def private_response(response, status):
    assert response.status_code == status
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["Pragma"] == "no-cache"


def test_owner_routes_deny_without_reading_database_or_transport(recovery_api, monkeypatch):
    case, client, _ = recovery_api

    def forbidden(*args, **kwargs):
        raise AssertionError("unauthorized request accessed recovery")

    monkeypatch.setattr(recovery, "inspect", forbidden)
    monkeypatch.setattr(recovery, "apply", forbidden)
    monkeypatch.setattr(recovery, "operation_status", forbidden)
    monkeypatch.setattr(case.core.db, "session", forbidden)
    for method, path in [
        (client.get, "/preapproval/" + case.record_id),
        (client.get, "/operations/" + str(uuid4())),
        (client.post, "/preapproval/" + case.record_id + "/retire"),
    ]:
        private_response(method("/internal/editorial/reconciliation" + path), 403)
    assert not case.reads


def test_recovery_routes_reject_query_and_invalid_body_without_effects(recovery_api):
    case, client, headers = recovery_api
    path = "/internal/editorial/reconciliation/preapproval/" + case.record_id
    private_response(client.get(path + "?token=never-accepted", headers=headers), 422)
    response = client.post(
        path + "/retire", headers=headers, json={"reason": "private-input-not-echoed"}
    )
    private_response(response, 422)
    assert "private-input-not-echoed" not in response.text and not case.reads


def test_owner_api_inspect_apply_status_and_response_loss(recovery_api):
    case, client, headers = recovery_api
    path = "/internal/editorial/reconciliation/preapproval/" + case.record_id
    response = client.get(path, headers=headers)
    private_response(response, 200)
    operation = str(uuid4())
    decision = {
        "operation_id": operation,
        "plan_hash": response.json()["plan_hash"],
        "retire_confirmed": True,
        "reason": "Owner retires this exact unsent draft while preserving all history",
    }
    first = client.post(path + "/retire", headers=headers, json=decision)
    private_response(first, 200)
    count = len(case.reads)
    again = client.post(path + "/retire", headers=headers, json=decision)
    private_response(again, 200)
    assert again.json() == first.json() and len(case.reads) == count
    status = client.get(
        "/internal/editorial/reconciliation/operations/" + operation, headers=headers
    )
    private_response(status, 200)
    assert status.json()["state"] == "retired_no_send" and len(status.content) < 4096


def test_owner_api_unexpected_errors_still_private(recovery_api, monkeypatch):
    case, client, headers = recovery_api

    def unavailable(*args):
        raise RuntimeError("private details must not escape")

    monkeypatch.setattr(recovery, "inspect", unavailable)
    response = client.get(
        "/internal/editorial/reconciliation/preapproval/" + case.record_id, headers=headers
    )
    private_response(response, 500)
    assert "private details" not in response.text


def historical_journal(case, legacy):
    from max_postiz_agent.browser_worker import _snapshot_request

    with case.core.db.session() as session:
        variant = session.get(Variant, case.variant_id)
        saved = case.core.snapshot(variant, session.get(Target, variant.target_id))
    request = str(uuid4())
    base, presentation = _snapshot_request(case.core.settings, request, saved, saved["destination"])
    payload = {**base, **presentation}
    value = {
        "state": "confirmed",
        "request_id": request,
        "chat_id": saved["destination"],
        "text": saved["body"],
        "attachment": True,
        "attachment_type": "browser-image",
        "before_count": 0,
        "started_at": 1.0,
        **presentation,
        "delivery": {"state": "confirmed", "external_id": "historical-delivery"},
    }
    if not legacy:
        payload["dispatch_snapshot_hash"] = fingerprint(saved)
        value.update(
            contract="browser-send-journal-v1",
            send_started=True,
            config_hash=saved["config_hash"],
            snapshot_hash=fingerprint(saved),
            base_payload=base,
            base_payload_hash=fingerprint(base),
            body_sha256=recovery.hashlib.sha256(saved["body"].encode()).hexdigest(),
            request_payload=payload,
            presentation=presentation,
        )
    value["content_hash"] = fingerprint(payload)
    dispatch = SimpleNamespace(
        id=request,
        snapshot=saved,
        state="published",
        verified=True,
        external_id="historical-delivery",
    )
    return request, value, dispatch


@pytest.mark.parametrize("legacy", [True, False])
def test_native_legacy_and_rich_journals_bind_exact_historical_snapshot(retirement_case, legacy):
    request, value, dispatch = historical_journal(retirement_case, legacy)
    session = SimpleNamespace(scalar=lambda _query: "published-attempt")
    recovery._validate_journal(retirement_case.core, session, request, value, dispatch)
    for field, changed in [
        ("request_id", str(uuid4())),
        ("text", "changed"),
        ("content_hash", "0" * 64),
        ("state", "unknown"),
    ]:
        corrupt = {**value, field: changed}
        with pytest.raises(AgentError):
            recovery._validate_journal(retirement_case.core, session, request, corrupt, dispatch)


def test_unknown_or_mismatched_journal_schema_is_never_ignored(retirement_case):
    request, value, dispatch = historical_journal(retirement_case, True)
    session = SimpleNamespace(scalar=lambda _query: "published-attempt")
    for changed in [
        {"request_id": request, "hidden_send": True},
        {**value, "unknown_schema": {}},
        {**value, "contract": "unrecognized-version"},
    ]:
        with pytest.raises(AgentError):
            recovery._validate_journal(retirement_case.core, session, request, changed, dispatch)


def test_retained_old_abandoned_photo_does_not_resolve_unknown_send(retirement_case):
    case = retirement_case
    request, _, dispatch = historical_journal(case, True)
    dispatch.state, dispatch.error = "cancelled", "OWNER_ABANDONED_UNVERIFIED_DELIVERY"
    dispatch.owner, dispatch.lease_until, dispatch.verified, dispatch.due_at = None, 0, False, 1.0
    dispatch.snapshot_hash = fingerprint({"content": dispatch.snapshot, "due_at": dispatch.due_at})
    session = SimpleNamespace(scalars=lambda _query: [SimpleNamespace(result="unknown")])
    raw = case.image_path.read_bytes()
    recovery._retained_abandoned_image(session, dispatch, ".jpg", raw)
    assert dispatch.state == "cancelled" and dispatch.verified is False and dispatch.id == request
    for suffix, data in [(".png", raw), (".jpg", b"changed")]:
        with pytest.raises(AgentError):
            recovery._retained_abandoned_image(session, dispatch, suffix, data)
    dispatch.state = "unknown"
    with pytest.raises(AgentError):
        recovery._retained_abandoned_image(session, dispatch, ".jpg", raw)


@pytest.mark.parametrize("corrupted", [None, "missing_audit", "attempt", "slot", "record", "journal"])
def test_archived_unknown_browser_journal_requires_original_owner_disposition(retirement_case, corrupted):
    request, journal, dispatch = historical_journal(retirement_case, False)
    dispatch.state, dispatch.error = "cancelled", "OWNER_ABANDONED_UNVERIFIED_DELIVERY"
    dispatch.owner, dispatch.lease_until, dispatch.verified = None, 0, False
    dispatch.external_id, dispatch.permalink, dispatch.due_at = None, None, 1.0
    dispatch.snapshot_hash = fingerprint({"content": dispatch.snapshot, "due_at": dispatch.due_at})
    journal.pop("delivery")
    journal.update(state="unknown", phase="receipt_reload")
    attempt = SimpleNamespace(id=str(uuid4()), result="unknown")
    preserved = {
        "contract": "owner-retire-unverified-max-delivery-2331-v1",
        "original_unknown_attempt_id": attempt.id,
        "original_external_result": "UNKNOWN",
        "historical_no_send_proved": False,
        "verified_delivery": False,
        "permanent_no_retry": True,
        "source_remains_ever_used": True,
        "archive_sha256": "1" * 64,
        "external_evidence_sha256": "2" * 64,
    }
    slot = SimpleNamespace(id=str(uuid4()), record_id=str(uuid4()), dispatch_id=request,
                           state="missed", active_target=None, delivered_at=None, error=dispatch.error)
    audit = SimpleNamespace(details={**preserved, "dispatch_id": request, "slot_id": slot.id,
                                     "record_id": slot.record_id, "original_attempt_preserved": True})
    record = {"id": slot.record_id, "state": "cancelled", "dispatch_id": request, "publication_slot_id": slot.id,
              "original_external_result": "UNKNOWN", "permanent_no_retry": True,
              "source_remains_ever_used": True, "owner_abandonment": dict(preserved)}
    if corrupted == "attempt":
        attempt.id = str(uuid4())
    if corrupted == "slot":
        slot.active_target = "max_test"
    if corrupted == "record":
        record["owner_abandonment"]["verified_delivery"] = True
    if corrupted == "journal":
        journal["snapshot_hash"] = "0" * 64
    session = SimpleNamespace(
        scalars=lambda query: ([attempt] if query.column_descriptions[0]["entity"] is DispatchAttempt
                               else [] if corrupted == "missing_audit" else [audit]),
        get=lambda model, _key: slot if model is PublicationSlot else SimpleNamespace(payload={"records": [record]}),
    )
    if corrupted:
        with pytest.raises(AgentError):
            recovery._validate_journal(retirement_case.core, session, request, journal, dispatch)
    else:
        recovery._validate_journal(retirement_case.core, session, request, journal, dispatch)
        assert journal["state"] == "unknown" and attempt.result == "unknown" and dispatch.verified is False


@pytest.mark.parametrize("backend", ["reserve_controller", "postgres_reserve"])
def test_owner_archived_preparing_journal_preserves_unknown_and_blocks_mutation(request, backend):
    from max_postiz_agent.browser_worker import _request_record, _snapshot_request
    from max_postiz_agent.unknown_delivery import OWNER_ACTION, OWNER_CONTRACT, OWNER_ERROR, retirement_id

    value = request.getfixturevalue(backend)
    value.controller.prepare_once()
    value.controller.run_once()
    record = value.cycle._load()["records"][0]
    dispatch_id = record["dispatch_id"]
    with value.core.db.session.begin() as session:
        dispatch = session.get(Dispatch, dispatch_id)
        attempt_id = str(uuid4())
        settings = value.core.settings
        start_at = dispatch.due_at + 1
        content = copy.deepcopy(dispatch.snapshot)
        base, presentation = _snapshot_request(settings, dispatch_id, content, content["destination"])
        payload = {**base, **presentation, "dispatch_snapshot_hash": fingerprint(content)}
        journal = {**_request_record(settings, payload),
                   "contract": "browser-send-journal-v1", "state": "preparing", "phase": "session",
                   "send_started": False, "started_at": start_at}
        raw = json.dumps(journal, ensure_ascii=False).encode()
        journal_hash = hashlib.sha256(raw).hexdigest()
        slot = session.get(PublicationSlot, record["publication_slot_id"])
        approval = session.get(TargetApproval, dispatch.approval_id)
        bridge = session.scalar(select(BridgePost).where(BridgePost.variant_id == dispatch.variant_id))
        details = {
            "contract": OWNER_CONTRACT,
            "variant_id": dispatch.variant_id,
            "slot_id": slot.id,
            "record_id": slot.record_id,
            "dispatch_id": dispatch_id,
            "original_unknown_attempt_id": attempt_id,
            "original_attempt_preserved": True,
            "original_external_result": "UNKNOWN",
            "historical_no_send_proved": False,
            "verified_delivery": False,
            "permanent_no_retry": True,
            "source_remains_ever_used": True,
            "browser_journal_sha256": journal_hash,
        }
        dispatch.state, dispatch.error, dispatch.attempts = "cancelled", OWNER_ERROR, 1
        approval.revoked = True
        slot.state, slot.error, slot.active_target = "missed", OWNER_ERROR, None
        bridge.active, bridge.error, bridge.remote_state = False, OWNER_ERROR, "ERROR"
        ledger = session.get(EditorialLedger, "hourly")
        output = copy.deepcopy(ledger.payload)
        output["records"][0].update(state="cancelled", original_external_result="UNKNOWN",
                                    permanent_no_retry=True, source_remains_ever_used=True,
                                    owner_abandonment=copy.deepcopy(details))
        ledger.payload = output
        session.add(DispatchAttempt(id=attempt_id, dispatch_id=dispatch_id, result="unknown", created_at=start_at+1))
        audit_id = retirement_id(dispatch.variant_id)
        session.add(Audit(id=audit_id, actor="owner_operational_exception", action=OWNER_ACTION,
                          entity=dispatch_id, details=details, at=start_at+2))
    journal_path = settings.root / "data/browser-publications" / (dispatch_id + ".json")
    journal_path.parent.mkdir(parents=True, exist_ok=True)
    journal_path.write_bytes(raw)
    with value.core.db.session() as session:
        dispatch = session.get(Dispatch, dispatch_id)
        recovery._validate_journal(value.core, session, dispatch_id, journal, dispatch, raw_sha256=journal_hash)
        assert recovery.mutation_blocked(session, dispatch.variant_id)
        assert value.core.bridge._blocked_post(session, record["postiz_id"]) == dispatch.variant_id
        with pytest.raises(AgentError, match="PREAPPROVAL_JOURNAL_INVALID"):
            recovery._validate_journal(value.core, session, dispatch_id, journal, dispatch, raw_sha256="0"*64)
        assert session.get(DispatchAttempt, attempt_id).result == "unknown"
        assert dispatch.verified is False and journal_path.read_bytes() == raw
    with value.core.db.session.begin() as session:
        # Alter both copies to ensure cross-comparison alone cannot certify a historical no-send.
        audit = session.get(Audit, audit_id)
        audit.details = {**audit.details, "historical_no_send_proved": True}
        ledger = session.get(EditorialLedger, "hourly")
        output = copy.deepcopy(ledger.payload)
        output["records"][0]["owner_abandonment"] = copy.deepcopy(audit.details)
        ledger.payload = output
    with value.core.db.session() as session, pytest.raises(AgentError, match="PREAPPROVAL_JOURNAL_INVALID"):
        recovery._validate_journal(value.core, session, dispatch_id, journal, session.get(Dispatch, dispatch_id),
                                   raw_sha256=journal_hash)


@pytest.mark.parametrize(
    "operation", ["", "not-a-uuid", "00000000-0000-0000-0000-00000000000", None]
)
def test_operation_status_rejects_invalid_identifier_without_database(
    retirement_case, operation, monkeypatch
):
    def forbidden(*args):
        raise AssertionError("invalid operation accessed database")

    monkeypatch.setattr(retirement_case.core.db, "session", forbidden)
    with pytest.raises(AgentError, match="OPERATION_ID_REQUIRED"):
        recovery.operation_status(retirement_case.core, operation)


def test_future_revision_contradicting_current_variant_blocks(retirement_case):
    case = retirement_case
    with case.core.db.session.begin() as session:
        row = session.scalar(
            select(VariantRevision).where(
                VariantRevision.variant_id == case.variant_id, VariantRevision.revision == 2
            )
        )
        session.add(
            VariantRevision(
                variant_id=case.variant_id,
                revision=3,
                payload=copy.deepcopy(row.payload),
                actor="orphan-history",
            )
        )
    with pytest.raises(AgentError, match="PREPARED_HISTORY_CHANGED"):
        plan(case)


def test_operation_identifier_cannot_be_reused_for_another_case(retirement_case):
    case, operation = retirement_case, str(uuid4())
    proposed = plan(case)
    with case.core.db.session.begin() as session:
        session.add(
            Audit(
                actor="postiz:owner",
                action=recovery.CLAIM,
                entity="different-variant",
                details={"operation_id": operation},
            )
        )
    before, reads = snapshot(case.core), len(case.reads)
    with pytest.raises(AgentError, match="OPERATION_CONFLICT"):
        apply(case, proposed, operation)
    assert snapshot(case.core) == before and len(case.reads) == reads


@pytest.mark.parametrize("changed", ["slot", "record"])
def test_completed_operation_status_refuses_current_state_drift(retirement_case, changed):
    case, operation = retirement_case, str(uuid4())
    apply(case, operation_id=operation)
    with case.core.db.session.begin() as session:
        if changed == "slot":
            session.get(PublicationSlot, case.slot_id).error = "unexpected-change"
        else:
            ledger = session.get(EditorialLedger, "hourly")
            value = copy.deepcopy(ledger.payload)
            next(row for row in value["records"] if row["id"] == case.record_id)["error"] = (
                "unexpected-change"
            )
            ledger.payload = value
    with pytest.raises(AgentError, match="COMMITTED_STATE_CHANGED"):
        recovery.operation_status(case.core, operation)


@pytest.mark.parametrize(
    "kind,state",
    [
        ("intent", "pending"),
        ("intent", "retry_wait"),
        ("intent", "unrecognized-future-state"),
        ("job", "queued"),
        ("job", "retry_wait"),
        ("job", "unrecognized-future-state"),
    ],
)
def test_unrelated_outstanding_legacy_transport_or_job_blocks(retirement_case, kind, state):
    case = retirement_case
    with case.core.db.session.begin() as session:
        if kind == "intent":
            session.add(
                Intent(
                    draft_id="unrelated-draft",
                    channel_id="other-channel",
                    body_hash="0" * 64,
                    state=state,
                )
            )
        else:
            session.add(
                Job(
                    kind="edit",
                    subject_id="unrelated-draft",
                    config_id=case.core.settings.hash,
                    due_at=case.stamp[0] + 3600,
                    key="unrelated-pending-edit",
                    state=state,
                )
            )
    before = snapshot(case.core)
    with pytest.raises(AgentError):
        plan(case)
    assert snapshot(case.core) == before and not case.reads


def unrelated_variant(case, session):
    original = session.get(Variant, case.variant_id)
    cloned = recovery._view(original)
    cloned["id"] = str(uuid4())
    draft = recovery._view(session.get(Draft, original.draft_id))
    draft["id"], draft["story_id"] = str(uuid4()), str(uuid4())
    session.add(Draft(**draft))
    session.flush()
    cloned["draft_id"] = draft["id"]
    session.add(Variant(**cloned))
    session.flush()
    return cloned["id"]


def test_duplicate_remote_draft_relationship_blocks_before_claim(retirement_case):
    case = retirement_case
    with case.core.db.session.begin() as session:
        other = unrelated_variant(case, session)
        existing = session.get(RemoteDraft, case.variant_id)
        session.add(
            RemoteDraft(
                variant_id=other,
                external_id=existing.external_id,
                state="draft",
                remote_hash=existing.remote_hash,
                synced_revision=1,
                correlation=str(uuid4()),
            )
        )
    before = snapshot(case.core)
    with pytest.raises(AgentError, match="REMOTE_RELATIONSHIP_AMBIGUOUS"):
        plan(case)
    assert snapshot(case.core) == before and not case.reads


@pytest.mark.parametrize("kind", ["preparation", "remote"])
def test_unsupported_unrelated_claim_or_remote_state_blocks(retirement_case, kind):
    case = retirement_case
    with case.core.db.session.begin() as session:
        if kind == "preparation":
            record = case.cycle._record(case.cycle._load(), case.record_id)
            original = session.get(EditorialPreparationClaim, record["reserve_claim"]["id"])
            fields = recovery._view(original)
            fields.update(
                id=str(uuid4()),
                token=str(uuid4()),
                record_id=None,
                active_key=None,
                state="unrecognized-future-state",
            )
            session.add(EditorialPreparationClaim(**fields))
        else:
            session.add(
                RemoteDraft(
                    variant_id=unrelated_variant(case, session),
                    external_id="unrelated-draft",
                    synced_revision=1,
                    correlation=str(uuid4()),
                    state="unrecognized-future-state",
                )
            )
    before = snapshot(case.core)
    with pytest.raises(AgentError):
        plan(case)
    assert snapshot(case.core) == before and not case.reads


@pytest.mark.parametrize("kind", ["preparation", "remote"])
def test_null_claim_or_remote_state_is_rejected_by_persisted_schema(retirement_case, kind):
    from sqlalchemy.exc import IntegrityError

    case = retirement_case
    before = snapshot(case.core)
    with pytest.raises(IntegrityError):
        with case.core.db.session.begin() as session:
            if kind == "preparation":
                record = case.cycle._record(case.cycle._load(), case.record_id)
                row = session.get(EditorialPreparationClaim, record["reserve_claim"]["id"])
            else:
                row = session.get(RemoteDraft, case.variant_id)
            row.state = None
    assert snapshot(case.core) == before and not case.reads


def test_legacy_service_approval_and_revision_cannot_bypass_retired_variant(retirement_case):
    case = retirement_case
    apply(case)
    with case.core.db.session() as session:
        draft = session.get(Draft, session.get(Variant, case.variant_id).draft_id)
    before = snapshot(case.core)
    with pytest.raises(AgentError, match="RETIRED_OR_RECONCILING"):
        Service.approve(case.core, draft.id, draft.revision, fingerprint(draft.current))
    with pytest.raises(AgentError, match="RETIRED_OR_RECONCILING"):
        with case.core.db.session.begin() as session:
            Service._approve(case.core, session, session.get(Draft, draft.id), "legacy-owner")
    with pytest.raises(AgentError, match="RETIRED_OR_RECONCILING"):
        Service.revise(case.core, draft.id, draft.revision, "An attempted legacy rewrite")
    assert snapshot(case.core) == before


def test_legacy_publisher_skips_retired_draft_before_creating_intent(retirement_case, monkeypatch):
    from max_postiz_agent.db import Approval, Channel

    case = retirement_case
    apply(case)
    with case.core.db.session.begin() as session:
        draft = session.get(Draft, session.get(Variant, case.variant_id).draft_id)
        draft.state, draft.scheduled_at = "scheduled", 1.0
        channel = session.get(Channel, draft.channel_id)
        channel.paused, channel.chat_id = False, "legacy-fixture-channel"
        channel.capabilities = {
            "publish": True,
            "binding_hash": fingerprint(case.core.settings.app["max"]),
        }
        session.add(
            Approval(
                draft_id=draft.id,
                revision=draft.revision,
                content_hash=fingerprint(draft.current),
                config_id=case.core.settings.hash,
                actor="contradictory-legacy-fixture",
            )
        )
        channel_id = draft.channel_id
    # Everything required by the old generic publisher is now eligible. Its
    # no-send result must come from the retirement fence, not an unrelated pause.
    profile = copy.deepcopy(case.core.settings.channel(channel_id))
    profile["enabled"], profile["publication"]["mode"] = True, "review"
    monkeypatch.setattr(case.core.settings, "channel", lambda _channel: profile)
    before = snapshot(case.core)
    assert Service.publish_due(case.core) == 0
    assert snapshot(case.core) == before and len(case.events) == case.model_count


def test_legacy_browser_preclick_rechecks_retired_draft(retirement_case, monkeypatch):
    from max_postiz_agent import browser_worker, db

    case = retirement_case
    apply(case)
    request = str(uuid4())
    with case.core.db.session.begin() as session:
        draft = session.get(Draft, session.get(Variant, case.variant_id).draft_id)
        session.add(
            Intent(
                id=request,
                draft_id=draft.id,
                channel_id=draft.channel_id,
                state="sending",
                body_hash=fingerprint(draft.current["body"]),
            )
        )
    # Inject this owned test DB explicitly; authorize must not read an ambient URL.
    monkeypatch.setattr(db, "Database", lambda _settings: case.core.db)
    before = snapshot(case.core)
    with pytest.raises(AgentError, match="RETIRED_OR_RECONCILING"):
        browser_worker.authorize(
            case.core.settings,
            {"request_id": request, "chat_id": "fixture-only", "text": draft.current["body"]},
        )
    assert snapshot(case.core) == before


def test_legacy_bare_service_schema_needs_no_optional_hub_table(service):
    from sqlalchemy import create_engine, inspect as schema_inspect
    from sqlalchemy.orm import sessionmaker
    from max_postiz_agent.db import Approval

    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Audit.__table__, Draft.__table__, Approval.__table__])
    sessions = sessionmaker(engine)
    try:
        assert not schema_inspect(engine).has_table("hub_variants")
        with sessions.begin() as session:
            draft = Draft(
                id=str(uuid4()),
                channel_id=service.settings.topic["channels"][0]["id"],
                story_id=str(uuid4()),
                revision=1,
                state="review_pending",
                commercial=False,
                current={"body": "Fixture text", "checks": []},
                config_id=service.settings.hash,
            )
            session.add(draft)
            session.flush()
            Service._approve(service, session, draft, "fixture-owner")
            draft_id = draft.id
        with sessions() as session:
            assert len(list(session.scalars(select(Approval)))) == 1
            assert recovery.draft_mutation_blocked(session, draft_id) is False
        assert not schema_inspect(engine).has_table("hub_variants")
    finally:
        engine.dispose()


def test_bare_schema_with_retained_claim_fails_closed_without_hub_table():
    from sqlalchemy import create_engine
    from sqlalchemy.exc import OperationalError
    from sqlalchemy.orm import sessionmaker

    engine = create_engine("sqlite://")
    Audit.__table__.create(engine)
    sessions = sessionmaker(engine)
    try:
        with sessions.begin() as session:
            session.add(
                Audit(
                    actor="postiz:owner",
                    action=recovery.CLAIM,
                    entity="unknown-variant",
                    details={},
                )
            )
        with sessions() as session:
            with pytest.raises(OperationalError):
                recovery.assert_draft_mutable(session, "legacy-draft")
    finally:
        engine.dispose()
