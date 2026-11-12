"""Owner-audited retirement of an exact preapproval DRAFT, without sending."""

import copy
from datetime import datetime
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
from urllib.parse import unquote, urlsplit
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import or_, select, text, update
from sqlalchemy.exc import IntegrityError

from .article_reserve import RESERVE_CONTRACT, _IDENTITY, _slot_view
from .config import AgentError, fingerprint, inside
from .db import Approval, Attempt, Audit, Intent, Job, now
from .hub_models import (
    BridgePost,
    Dispatch,
    DispatchAttempt,
    RecurringTask,
    RemoteDraft,
    Target,
    TargetApproval,
    Variant,
    VariantRevision,
)
from .max_bridge import MUTATION_LEASE_SECONDS, MaxBridge
from .postiz_http_journal import PostizHttpJournal
from .server_models import (
    EditorialControl,
    EditorialLedger,
    EditorialPreparationClaim,
    PublicationSlot,
)
from .server_state import LEDGER_ID, fence_editorial_configuration

CONTRACT = "known-preapproval-draft-retirement-v1"
CLAIM = "preapproval_retirement_claim"
COMMIT = "preapproval_retirement_committed"
MAX_AGE = 120
_TIMEOUT_ERROR = "POSTIZ_UNKNOWN"
# Completed native preparation outcomes and retained, owner-audited historical
# diagnostic dispositions. An unsupported state never becomes quiescent by age.
_PREPARATION_TERMINAL = {
    "quality_ready",
    "rejected",
    "no_source",
    "abandoned_unknown",
    "diagnostic_complete",
    "diagnostic_no_upstream",
    "failed_input_planning",
    "failed_pre_model_validation",
    "failed_provider_rejection",
}


def require(value, code="PREAPPROVAL_RETIREMENT_NOT_SAFE"):
    if not value:
        raise AgentError(code, 409)


def _operation_id(value):
    try:
        require(
            isinstance(value, str) and str(UUID(value)) == value,
            "PREAPPROVAL_OPERATION_ID_REQUIRED",
        )
    except (ValueError, TypeError, AttributeError) as exc:
        raise AgentError("PREAPPROVAL_OPERATION_ID_REQUIRED", 409) from exc


def _fresh(observation):
    stamp = observation.get("at")
    require(
        type(stamp) in {int, float} and math.isfinite(stamp) and 0 <= now() - stamp <= MAX_AGE,
        "PREAPPROVAL_OBSERVATION_STALE",
    )


def _observed_identity(observation):
    return {
        k: v
        for k, v in observation.items()
        if k not in {"ordinal", "at", "completed_at", "artifact"}
    }


def _id(variant_id, stage):
    return str(uuid5(NAMESPACE_URL, CONTRACT + ":" + variant_id + ":" + stage))


def mutation_blocked(session, variant_id):
    return session.get(Audit, _id(variant_id, "claim")) is not None


def assert_mutable(session, variant_id):
    require(not mutation_blocked(session, variant_id), "PREAPPROVAL_RETIRED_OR_RECONCILING")


def draft_mutation_blocked(session, draft_id):
    # Legacy installations may have only the base Service tables. No recovery
    # claim means there is no new fence to resolve through optional Hub tables.
    if session.scalar(select(Audit.id).where(Audit.action == CLAIM).limit(1)) is None:
        return False
    return any(
        mutation_blocked(session, variant_id)
        for variant_id in session.scalars(select(Variant.id).where(Variant.draft_id == draft_id))
    )


def assert_draft_mutable(session, draft_id):
    require(not draft_mutation_blocked(session, draft_id), "PREAPPROVAL_RETIRED_OR_RECONCILING")


def fence_draft_mutation(session):
    """Serialize legacy writers with first retirement claim without optional DDL."""
    if session.get_bind().dialect.name == "postgresql":
        fence_editorial_configuration(session)
    else:
        # A zero-row base-table update obtains SQLite's writer lock even when
        # an old Service-only installation has no editorial_control table.
        session.execute(
            update(Audit).where(Audit.id == "__preapproval_fence__").values(at=Audit.at)
        )


def assert_no_unfinished(session):
    for row in session.scalars(select(Audit).where(Audit.action == CLAIM)):
        require(
            session.get(Audit, _id(row.entity, "commit")) is not None,
            "PREAPPROVAL_RECONCILIATION_IN_PROGRESS",
        )


def _view(row):
    return {column.key: copy.deepcopy(getattr(row, column.key)) for column in row.__table__.columns}


def _decode(raw, code):
    try:
        value = json.loads(raw)
        require(isinstance(value, dict), code)
        return value
    except (ValueError, TypeError, UnicodeError) as exc:
        raise AgentError(code, 409) from exc


def _files(core, record, variant, session):
    """Hash bounded saved evidence and schema-aware native publication journals."""
    evidence = {}
    names = [
        (record.get("input_file"), record.get("input_file_hash")),
        (
            record.get("quality_trace"),
            variant.metadata_json.get("machine_review_evidence", {}).get("trace_hash"),
        ),
    ]
    require(len(variant.media) == 1)
    names.append((variant.media[0].get("path"), variant.media[0].get("sha256")))
    for relative, expected in names:
        require(isinstance(relative, str) and re.fullmatch("[a-f0-9]{64}", expected or ""))
        path = inside(core.settings.root, relative)
        require(
            path.is_file()
            and not any(p.is_symlink() for p in (path, *path.parents))
            and path.stat().st_size <= 20 * 1024 * 1024
        )
        require(hashlib.sha256(path.read_bytes()).hexdigest() == expected)
        evidence[relative] = expected
    journal = core.settings.root / "data/browser-publications"
    receipts, total = {}, 0
    if journal.exists():
        require(
            not any(p.is_symlink() for p in (journal, *journal.parents)),
            "PREAPPROVAL_JOURNAL_INVALID",
        )
        paths = sorted(journal.iterdir())
        require(len(paths) <= 10000)
        for path in paths:
            require(path.is_file() and not path.is_symlink())
            suffix = next(
                (
                    s
                    for s in (
                        ".before-reload.png",
                        ".unknown.png",
                        ".json",
                        ".png",
                        ".jpg",
                        ".jpeg",
                        ".mp4",
                    )
                    if path.name.endswith(s)
                ),
                None,
            )
            require(suffix is not None, "PREAPPROVAL_JOURNAL_INVALID")
            request_id = path.name[: -len(suffix)]
            require(re.fullmatch(r"[A-Za-z0-9_-]{1,128}", request_id))
            limit = (2 if suffix == ".json" else 20) * 1024 * 1024
            require(path.stat().st_size <= limit, "PREAPPROVAL_JOURNAL_INVALID")
            with path.open("rb") as stream:
                raw = stream.read(limit + 1)
            total += len(raw)
            require(
                len(raw) <= (2 if suffix == ".json" else 20) * 1024 * 1024
                and total <= 1024 * 1024 * 1024
            )
            dispatch = session.get(Dispatch, request_id)
            intent = session.get(Intent, request_id)
            require(dispatch is not None or intent is not None, "PREAPPROVAL_JOURNAL_UNCORRELATED")
            require(dispatch is None or dispatch.variant_id != variant.id)
            require(intent is None or intent.draft_id != variant.draft_id)
            if path.suffix == ".json":
                _validate_journal(
                    core, session, request_id, _decode(raw, "PREAPPROVAL_JOURNAL_INVALID"), dispatch
                )
            else:
                if not (journal / (request_id + ".json")).is_file():
                    _retained_abandoned_image(session, dispatch, suffix, raw)
            receipts[path.name] = hashlib.sha256(raw).hexdigest()
    return {"evidence": evidence, "journals": receipts}


def _abandoned_attempt(session, dispatch):
    require(dispatch is not None, "PREAPPROVAL_JOURNAL_UNCORRELATED")
    snapshot = dispatch.snapshot
    require(
        dispatch.state == "cancelled"
        and dispatch.error == "OWNER_ABANDONED_UNVERIFIED_DELIVERY"
        and dispatch.owner is None
        and dispatch.lease_until == 0
        and dispatch.verified is False
        and dispatch.snapshot_hash == fingerprint({"content": snapshot, "due_at": dispatch.due_at})
        and snapshot.get("platform") == "max"
        and snapshot.get("route") == "max_browser",
        "PREAPPROVAL_JOURNAL_UNCORRELATED",
    )
    attempts = list(
        session.scalars(select(DispatchAttempt).where(DispatchAttempt.dispatch_id == dispatch.id))
    )
    require(len(attempts) == 1 and attempts[0].result == "unknown", "PREAPPROVAL_JOURNAL_UNCORRELATED")
    return attempts[0]


def _retained_abandoned_image(session, dispatch, suffix, raw):
    """Preserve an already abandoned historical attachment; never resolve its send."""
    _abandoned_attempt(session, dispatch)
    media = dispatch.snapshot.get("media", [])
    require(
        suffix == ".jpg"
        and len(media) == 1
        and media[0].get("mime") == "image/jpeg"
        and hashlib.sha256(raw).hexdigest() == media[0].get("sha256"),
        "PREAPPROVAL_JOURNAL_UNCORRELATED",
    )


def _validate_archived_unknown(session, dispatch, value):
    """Verify the archived owner decision while retaining its unknown delivery."""
    attempt = _abandoned_attempt(session, dispatch)
    audits = list(
        session.scalars(select(Audit).where(
            Audit.entity == dispatch.id,
            Audit.actor == "owner_operational_exception",
            Audit.action == "owner_abandoned_unverified_delivery",
        ))
    )
    code = "PREAPPROVAL_JOURNAL_INVALID"
    require(len(audits) == 1, code)
    details = audits[0].details
    preserved = {
        "contract": "owner-retire-unverified-max-delivery-2331-v1",
        "original_unknown_attempt_id": attempt.id,
        "original_external_result": "UNKNOWN",
        "historical_no_send_proved": False,
        "verified_delivery": False,
        "permanent_no_retry": True,
        "source_remains_ever_used": True,
    }
    require(
        value.get("delivery") is None
        and dispatch.external_id in (None, "")
        and dispatch.permalink in (None, "")
        and details.get("dispatch_id") == dispatch.id
        and details.get("original_attempt_preserved") is True
        and fingerprint({key: details.get(key) for key in preserved}) == fingerprint(preserved)
        and all(re.fullmatch(r"[a-f0-9]{64}", str(details.get(key, "")))
                for key in ("archive_sha256", "external_evidence_sha256")),
        code,
    )
    slot = session.get(PublicationSlot, details.get("slot_id"))
    ledger = session.get(EditorialLedger, LEDGER_ID)
    records = [record for record in ledger.payload["records"]
               if record.get("id") == details.get("record_id")] if ledger else []
    require(
        slot is not None
        and slot.record_id == details.get("record_id")
        and slot.dispatch_id == dispatch.id
        and slot.state == "missed"
        and slot.active_target is None
        and slot.delivered_at is None
        and slot.error == dispatch.error
        and len(records) == 1,
        code,
    )
    record = records[0]
    owner = record.get("owner_abandonment") or {}
    require(
        record.get("state") == "cancelled"
        and record.get("dispatch_id") == dispatch.id
        and record.get("publication_slot_id") == slot.id
        and record.get("original_external_result") == "UNKNOWN"
        and record.get("permanent_no_retry") is True
        and record.get("source_remains_ever_used") is True
        and fingerprint({key: owner.get(key) for key in preserved}) == fingerprint(preserved)
        and all(owner.get(key) == details[key] for key in ("archive_sha256", "external_evidence_sha256")),
        code,
    )


def _validate_journal(core, session, request_id, value, dispatch):
    from types import SimpleNamespace
    from .browser_worker import _snapshot_request, read_preclick_failure

    require(dispatch is not None and isinstance(value, dict), "PREAPPROVAL_JOURNAL_UNCORRELATED")
    snapshot = dispatch.snapshot
    require(
        isinstance(snapshot, dict) and value.get("request_id") == request_id,
        "PREAPPROVAL_JOURNAL_INVALID",
    )
    historical = SimpleNamespace(hash=snapshot.get("config_hash"), root=core.settings.root)
    if value.get("state") == "preclick_failed":
        read_preclick_failure(
            historical, request_id, snapshot=snapshot, chat_id=snapshot["destination"]
        )
        return
    expected, presentation = _snapshot_request(
        historical, request_id, snapshot, snapshot["destination"]
    )
    require(
        value.get("chat_id") == expected["chat_id"]
        and value.get("text") == expected["text"]
        and value.get("attachment") is bool(expected["attachment"])
        and value.get("attachment_type")
        == (expected["attachment"]["type"] if expected["attachment"] else None),
        "PREAPPROVAL_JOURNAL_INVALID",
    )
    archived = value.get("contract") == "browser-send-journal-v1" and value.get("state") == "unknown"
    if not archived:
        require(
            value.get("state") == "confirmed"
            and dispatch.state == "published"
            and dispatch.verified is True
            and value.get("delivery", {}).get("state") == "confirmed"
            and value.get("delivery", {}).get("external_id") == dispatch.external_id
            and session.scalar(select(DispatchAttempt.id).where(
                DispatchAttempt.dispatch_id == request_id, DispatchAttempt.result == "published"
            )) is not None,
            "PREAPPROVAL_JOURNAL_INVALID",
        )
    # The two historical native formats predate dispatch_snapshot_hash. Their
    # exact payload is still reconstructed from the immutable dispatch snapshot.
    if "contract" not in value:
        allowed = {
            "state",
            "request_id",
            "chat_id",
            "text",
            "attachment",
            "attachment_type",
            "before_count",
            "content_hash",
            "started_at",
            "delivery",
            "error",
            "composer_title_proof",
            "illustration_image_sha256",
            *presentation,
        }
        attachment = expected["attachment"]
        require(
            not set(value) - allowed
            and value.get("state") == "confirmed"
            and value.get("chat_id") == expected["chat_id"]
            and value.get("text") == expected["text"]
            and value.get("attachment") is bool(attachment)
            and value.get("attachment_type") == (attachment["type"] if attachment else None)
            and all(value.get(k) == v for k, v in presentation.items())
            and value.get("content_hash") == fingerprint({**expected, **presentation})
            and type(value.get("before_count")) is int
            and value["before_count"] >= 0
            and type(value.get("started_at")) in {int, float}
            and math.isfinite(value["started_at"]),
            "PREAPPROVAL_JOURNAL_INVALID",
        )
        return
    payload = value.get("request_payload", {})
    allowed = {
        "attachment",
        "attachment_type",
        "base_payload",
        "base_payload_hash",
        "before_count",
        "body_sha256",
        "chat_id",
        "composer_title_proof",
        "config_hash",
        "content_hash",
        "contract",
        "delivery",
        "error",
        "error_detail",
        "illustration_dimensions",
        "illustration_image_sha256",
        "illustration_proof_hash",
        "phase",
        "presentation",
        "presentation_contract",
        "request_attachment",
        "request_id",
        "request_payload",
        "send_started",
        "snapshot_hash",
        "started_at",
        "state",
        "text",
        "title_format",
        "title_text",
    }
    require(
        not set(value) - allowed
        and value.get("contract") == "browser-send-journal-v1"
        and value.get("send_started") is True
        and value.get("config_hash") == historical.hash
        and value.get("snapshot_hash") == fingerprint(snapshot)
        and value.get("base_payload") == expected
        and value.get("base_payload_hash") == fingerprint(expected)
        and value.get("body_sha256") == hashlib.sha256(expected["text"].encode()).hexdigest()
        and value.get("content_hash") == fingerprint(payload)
        and payload == {**expected, **presentation, "dispatch_snapshot_hash": fingerprint(snapshot)}
        and value.get("presentation") == presentation
        and all(payload.get(k) == v for k, v in expected.items())
        and payload.get("dispatch_snapshot_hash") == fingerprint(snapshot),
        "PREAPPROVAL_JOURNAL_INVALID",
    )
    if archived:
        _validate_archived_unknown(session, dispatch, value)


def _schedule_timeout_proof(core, record, slot, bridge):
    code = "PREAPPROVAL_POSTIZ_TIMEOUT_UNPROVEN"
    require(
        record.get("publication_slot_id") == slot.id
        and record.get("due_at") == slot.due_at
        and record.get("postiz_id") == bridge.post_id
        and slot.error == "ARTICLE_RECONCILIATION_REQUIRED"
        and isinstance(bridge.mutation_owner, str)
        and type(bridge.updated_at) in {float, int}
        and type(bridge.mutation_until) in {float, int}
        and 0 < bridge.mutation_until <= now()
        and abs(bridge.updated_at + MUTATION_LEASE_SECONDS - bridge.mutation_until) < 0.001,
        code,
    )
    events = PostizHttpJournal(core.settings.root).events_for_post(bridge.post_id)
    require(len(events) == 1, code)
    event = events[0]
    require(
        all(event.get(key) == expected for key, expected in {
            "method": "POST", "endpoint": "/posts", "operation": "schedule",
            "http_status": 504, "cas": "missing", "outcome": "http_error",
            "stage": "response", "error_code": _TIMEOUT_ERROR,
        }.items())
        and type(event.get("elapsed_ms")) is int
        and type(event.get("read_timeout_s")) in {float, int}
        and 60_000 <= event["elapsed_ms"] <= 70_000
        and event["elapsed_ms"] < event["read_timeout_s"] * 1000,
        code,
    )
    try:
        require(str(UUID(bridge.mutation_owner)) == bridge.mutation_owner, code)
        started = datetime.fromisoformat(event["at"])
        require(started.tzinfo is not None, code)
        started_at = started.timestamp()
    except (KeyError, TypeError, ValueError, OverflowError, OSError) as exc:
        raise AgentError(code, 409) from exc
    finished_at = started_at + event["elapsed_ms"] / 1000
    # The exact bridge lease appeared after the 504 had already returned.
    # An earlier, still active, or uncorrelated lease cannot prove this case.
    require(
        bridge.due_at <= started_at < finished_at < bridge.updated_at < slot.due_at
        and bridge.updated_at - finished_at <= 30,
        code,
    )
    return {"http_status": 504, "event_hash": fingerprint(event), "requested_at": started_at,
            "response_at": finished_at, "lease_acquired_at": bridge.updated_at,
            "lease_until": bridge.mutation_until}


def _capture(core, session, record_id, *, locked=False):
    if locked:
        fence_editorial_configuration(session)

    def one(model, condition):
        query = select(model).where(condition)
        return session.scalar(query.with_for_update() if locked else query)

    control = one(EditorialControl, EditorialControl.id == "main")
    require(control is not None, "PREAPPROVAL_PAUSE_REQUIRED")
    initial = session.get(EditorialLedger, LEDGER_ID)
    matches = (
        [r for r in initial.payload.get("records", []) if r.get("id") == record_id]
        if initial
        else []
    )
    require(len(matches) == 1)
    record = matches[0]
    variant = one(Variant, Variant.id == record.get("variant_id"))
    require(variant is not None)
    target = one(Target, Target.id == variant.target_id)
    remote = one(RemoteDraft, RemoteDraft.variant_id == variant.id)
    bridges = (
        list(
            session.scalars(
                select(BridgePost).where(BridgePost.variant_id == variant.id).with_for_update()
            )
        )
        if locked
        else list(session.scalars(select(BridgePost).where(BridgePost.variant_id == variant.id)))
    )
    slots = (
        list(
            session.scalars(
                select(PublicationSlot)
                .where(PublicationSlot.record_id == record_id)
                .with_for_update()
            )
        )
        if locked
        else list(
            session.scalars(select(PublicationSlot).where(PublicationSlot.record_id == record_id))
        )
    )
    require(len(bridges) == len(slots) == 1)
    bridge, slot = bridges[0], slots[0]
    ledger = one(EditorialLedger, EditorialLedger.id == LEDGER_ID)
    # Refresh the ledger after acquiring all locks; no split cycle._save commit.
    if locked:
        session.refresh(ledger)
    records = [r for r in ledger.payload["records"] if r.get("id") == record_id]
    require(len(records) == 1)
    record = records[0]
    is_schedule_timeout = record.get("error") == _TIMEOUT_ERROR
    require(control.paused is True or is_schedule_timeout, "PREAPPROVAL_PAUSE_REQUIRED")
    require(
        record.get("state") == "needs_attention"
        and record.get("error") in {"ARTICLE_REMOTE_CHANGED", _TIMEOUT_ERROR}
        and record.get("reserve_contract") == RESERVE_CONTRACT
        and record.get("variant_id") == variant.id
        and record.get("target_id") == target.id
        and not record.get("dispatch_id")
        and not record.get("published_at")
        and record.get("postiz_id") in (None, bridge.post_id)
    )
    claim = record.get("reserve_claim", {})
    prepared = session.get(EditorialPreparationClaim, claim.get("id"))
    binding = {
        key: record.get(key)
        for key in (
            "id",
            "reserve_contract",
            "reserve_claim",
            "input_hash",
            "policy_hash",
            "target_id",
            "source_id",
            "original_hash",
            "source_published_at",
        )
    }
    binding_audit = session.get(
        Audit, str(uuid5(NAMESPACE_URL, "max-postiz:reserve-binding:" + record_id))
    )
    require(
        prepared is not None
        and prepared.record_id == record_id
        and prepared.token == claim.get("token")
        and prepared.control_revision == claim.get("control_revision")
        and prepared.active_key is None
        and prepared.state == "quality_ready"
        and binding_audit is not None
        and binding_audit.actor == "article_reserve"
        and binding_audit.action == "article_reserve_preparation_binding"
        and binding_audit.details == {"binding": binding, "binding_hash": fingerprint(binding)}
        and record.get("reserve_binding_hash") == fingerprint(binding)
    )
    require(
        slot.state == "blocked_unknown"
        and slot.active_target == target.id
        and slot.dispatch_id is None
        and slot.delivered_at is None
        and slot.due_at <= now()
        and slot.target_id == target.id
        and slot.binding == {k: record.get(k) for k in _IDENTITY}
        and slot.text_hash == record.get("reserve_prepared_hash")
        and slot.evidence_hash == variant.metadata_json.get("machine_review_evidence_hash")
    )
    require(
        target.platform == "max"
        and target.route == "max_browser"
        and target.enabled is True
        and target.paused is True
        and variant.binding_hash == target.binding_hash
        and variant.config_hash == core.settings.hash
        and all(t.paused is True for t in session.scalars(select(Target)))
    )
    timeout_proof = _schedule_timeout_proof(core, record, slot, bridge) if is_schedule_timeout else None
    current_draft = (
        timeout_proof is not None
        and remote is not None
        and remote.synced_revision == variant.revision
        and remote.remote_hash == fingerprint({"body": variant.body})
    )
    require(
        remote is not None
        and remote.state == "draft"
        and remote.external_id == bridge.post_id
        and remote.correlation
        and (remote.synced_revision == variant.revision - 1 or current_draft)
        and bridge.remote_state == "DRAFT"
        and bridge.active is True
        and bridge.error is None
        and bridge.integration_id == target.options.get("postiz_integration_id")
        and bridge.target_id == target.id
        and (timeout_proof is not None or (bridge.mutation_owner is None and bridge.mutation_until == 0))
    )
    require(
        list(
            session.scalars(
                select(RemoteDraft.variant_id).where(RemoteDraft.external_id == bridge.post_id)
            )
        )
        == [variant.id],
        "PREAPPROVAL_REMOTE_RELATIONSHIP_AMBIGUOUS",
    )
    require(
        not session.scalar(select(TargetApproval.id).where(TargetApproval.variant_id == variant.id))
        and not session.scalar(select(Dispatch.id).where(Dispatch.variant_id == variant.id))
        and not session.scalar(select(Approval.id).where(Approval.draft_id == variant.draft_id))
        and not session.scalar(select(Intent.id).where(Intent.draft_id == variant.draft_id))
    )
    # All other leases are still disqualifying, including expired or NULL leases.
    bridge_leases = select(BridgePost.post_id).where(
        or_(BridgePost.mutation_owner.is_not(None), BridgePost.mutation_until.is_(None),
            BridgePost.mutation_until != 0)
    )
    if timeout_proof is not None:
        bridge_leases = bridge_leases.where(BridgePost.post_id != bridge.post_id)
    checks = [
        select(Dispatch.id).where(
            or_(
                Dispatch.state.not_in({"published", "cancelled"}),
                Dispatch.state.is_(None),
                Dispatch.owner.is_not(None),
                Dispatch.lease_until.is_(None),
                Dispatch.lease_until != 0,
            )
        ),
        bridge_leases,
        select(Job.id).where(
            or_(
                Job.state.not_in({"done", "failed", "cancelled"}),
                Job.state.is_(None),
                Job.owner.is_not(None),
                Job.lease_until.is_(None),
                Job.lease_until != 0,
            )
        ),
        select(RecurringTask.id).where(
            or_(
                RecurringTask.owner.is_not(None),
                RecurringTask.lease_until.is_(None),
                RecurringTask.lease_until != 0,
            )
        ),
        select(EditorialPreparationClaim.id).where(
            or_(
                EditorialPreparationClaim.active_key.is_not(None),
                EditorialPreparationClaim.state.is_(None),
                EditorialPreparationClaim.state.not_in(_PREPARATION_TERMINAL),
            )
        ),
        select(RemoteDraft.variant_id).where(
            or_(RemoteDraft.state.is_(None), RemoteDraft.state != "draft")
        ),
        select(Intent.id).where(
            or_(
                Intent.state.is_(None),
                Intent.state.not_in({"confirmed", "rejected", "archived", "cancelled"}),
            )
        ),
        select(Job.id).where(
            Job.subject_id.in_({record_id, variant.id, variant.draft_id, bridge.post_id}),
            Job.state.in_({"queued", "retry_wait", "leased"}),
        ),
        select(PublicationSlot.id).where(
            PublicationSlot.active_target.is_not(None), PublicationSlot.id != slot.id
        ),
    ]
    require(not any(session.scalar(q.limit(1)) is not None for q in checks))
    require(
        not session.scalar(
            select(DispatchAttempt.id)
            .outerjoin(Dispatch, Dispatch.id == DispatchAttempt.dispatch_id)
            .where(Dispatch.id.is_(None))
        )
        and not session.scalar(
            select(Attempt.id)
            .outerjoin(Intent, Intent.id == Attempt.intent_id)
            .where(Intent.id.is_(None))
        ),
        "PREAPPROVAL_ORPHAN_ATTEMPT",
    )
    versions = list(
        session.scalars(
            select(VariantRevision)
            .where(VariantRevision.variant_id == variant.id)
            .order_by(VariantRevision.revision)
        )
    )
    require(
        [row.revision for row in versions] == [1, 2] and variant.revision == 2,
        "PREAPPROVAL_PREPARED_HISTORY_CHANGED",
    )
    prepared = versions[0].payload
    current_snapshot = core.snapshot(variant, target)
    current_version = next(v.payload for v in versions if v.revision == variant.revision)
    require(
        fingerprint(prepared) == record.get("reserve_prepared_hash")
        and record.get("prepared_hash") == fingerprint(current_snapshot if current_draft else prepared)
        and prepared.get("revision") == versions[0].revision
        and current_version == current_snapshot,
        "PREAPPROVAL_PREPARED_HISTORY_CHANGED",
    )
    comparable = copy.deepcopy(current_snapshot)
    comparable["body"], comparable["revision"] = prepared["body"], prepared["revision"]
    for key in ("postiz_post_id", "postiz_hash", "postiz_due_at", "postiz_state"):
        comparable["metadata"].pop(key, None)
    require(
        comparable == prepared
        and variant.metadata_json.get("postiz_post_id") == bridge.post_id
        and variant.metadata_json.get("postiz_hash") == bridge.remote_hash
        and variant.metadata_json.get("postiz_state") == "DRAFT",
        "PREAPPROVAL_STAGE_HISTORY_CHANGED",
    )
    snapshot = {
        "control_revision": control.revision,
        "paused": control.paused,
        "ledger_revision": ledger.revision,
        "ledger_hash": fingerprint(ledger.payload),
        "record": copy.deepcopy(record),
        "slot": {**_slot_view(slot), "active_target": slot.active_target},
        "variant": _view(variant),
        "target": _view(target),
        "remote": _view(remote),
        "bridge": _view(bridge),
        "variant_history_hash": fingerprint([_view(v) for v in versions]),
        "files": _files(core, record, variant, session),
    }
    if timeout_proof is not None:
        snapshot["schedule_timeout"] = timeout_proof
    return snapshot, (control, ledger, record, slot, variant, target, remote, bridge)


def _photo(core, remote, expected_mime):
    images = remote["image"]
    if isinstance(images, str):
        images = json.loads(images)
    require(isinstance(images, list) and len(images) == 1)
    parsed = urlsplit(images[0]["path"])
    base = urlsplit(core.postiz.base)
    allowed = {(base.scheme, base.netloc)}
    if core.postiz.public_origin:
        origin = urlsplit(core.postiz.public_origin)
        allowed.add((origin.scheme, origin.netloc))
    require(
        not parsed.query
        and not parsed.fragment
        and not parsed.username
        and (not parsed.netloc or (parsed.scheme, parsed.netloc) in allowed)
        and unquote(parsed.path).startswith("/uploads/")
        and ".." not in Path(unquote(parsed.path)).parts
    )
    url = base.scheme + "://" + base.netloc + parsed.path
    digest = hashlib.sha256()
    size = 0
    with core.postiz.client.stream(
        "GET",
        url,
        headers={"Cache-Control": "no-cache, no-store", "Pragma": "no-cache"},
        params={"owned_maxreadnonce": secrets.token_hex(16)},
    ) as response:
        require(response.status_code == 200 and not response.is_redirect)
        require(
            response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
            == expected_mime
        )
        for block in response.iter_bytes():
            size += len(block)
            require(size <= 20 * 1024 * 1024)
            digest.update(block)
    require(size > 0)
    return digest.hexdigest()


def _observe(core, snapshot, operation_id, ordinal, *, save=False):
    began = now()
    remote = core.postiz.get_post_fresh(snapshot["bridge"]["post_id"])
    integrations = core.postiz.integrations_fresh()
    matches = [
        i
        for i in integrations
        if isinstance(i, dict) and i.get("id") == snapshot["bridge"]["integration_id"]
    ]
    require(len(matches) == 1, "PREAPPROVAL_INTEGRATION_CHANGED")
    integration = matches[0]
    _integration(snapshot, integration)
    body, media, due, remote_hash = MaxBridge.normalize(remote)
    settings = remote.get("settings") or {}
    if isinstance(settings, str):
        settings = json.loads(settings)
    require(
        remote.get("state") == "DRAFT"
        and remote.get("id") == snapshot["bridge"]["post_id"]
        and remote.get("group") == snapshot["bridge"]["group_id"]
        and remote.get("integration", {}).get("id") == snapshot["bridge"]["integration_id"]
        and settings.get("hub_variant_id") == snapshot["variant"]["id"]
        and settings.get("hub_correlation") == snapshot["remote"]["correlation"]
        and settings.get("__type") == "max-browser"
        and remote.get("releaseId") in (None, "")
        and remote.get("releaseURL") in (None, "")
        and remote_hash == snapshot["bridge"]["remote_hash"]
        and body == snapshot["variant"]["body"]
        and due == snapshot["bridge"]["due_at"]
    )
    photo = _photo(core, remote, snapshot["variant"]["media"][0]["mime"])
    require(photo == snapshot["variant"]["media"][0]["sha256"])
    observation = {
        "operation_id": operation_id,
        "ordinal": ordinal,
        "at": began,
        "completed_at": now(),
        "remote_hash": remote_hash,
        "body_hash": fingerprint(body),
        "image_sha256": photo,
        "image_mime": snapshot["variant"]["media"][0]["mime"],
        "raw_postiz_hash": fingerprint(remote),
        "integration_hash": fingerprint(integration),
        "binding_hash": fingerprint(
            {
                "id": remote["id"],
                "group": remote["group"],
                "integration": remote["integration"]["id"],
                "settings": settings,
                "due": due,
            }
        ),
        "snapshot_hash": fingerprint(snapshot),
    }
    _fresh(observation)
    if save:
        directory = core.settings.root / "data/preapproval-reconciliations" / operation_id
        require(not any(p.is_symlink() for p in (directory, *directory.parents)))
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        path = directory / ("read" + str(ordinal) + ".local.json")
        raw = (
            json.dumps(
                {
                    "contract": CONTRACT,
                    "observation": observation,
                    "raw_postiz": remote,
                    "raw_integration": integration,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        ).encode()
        require(len(raw) <= 2 * 1024 * 1024)
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        if os.name != "nt":
            for parent in (directory, directory.parent, directory.parent.parent):
                descriptor = os.open(parent, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
        observation["artifact"] = {
            "path": path.relative_to(core.settings.root).as_posix(),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }
    return observation


def _integration(snapshot, integration):
    require(
        integration.get("id") == snapshot["bridge"]["integration_id"]
        and integration.get("identifier") == "max-browser"
        and integration.get("accountId") == snapshot["target"]["destination"]
        and integration.get("disabled") is False
        and integration.get("refreshNeeded") is False,
        "PREAPPROVAL_INTEGRATION_CHANGED",
    )


def _saved_observation(core, operation_id, ordinal, snapshot):
    path = (
        core.settings.root
        / "data/preapproval-reconciliations"
        / operation_id
        / ("read" + str(ordinal) + ".local.json")
    )
    if not path.exists():
        return None
    require(
        path.is_file()
        and not any(p.is_symlink() for p in (path, *path.parents))
        and path.stat().st_size <= 2 * 1024 * 1024
    )
    raw = path.read_bytes()
    value = json.loads(raw)
    result = value["observation"]
    _integration(snapshot, value["raw_integration"])
    body, _, due, remote_hash = MaxBridge.normalize(value["raw_postiz"])
    require(
        value.get("contract") == CONTRACT
        and result.get("operation_id") == operation_id
        and result.get("ordinal") == ordinal
        and result.get("snapshot_hash") == fingerprint(snapshot)
        and result.get("raw_postiz_hash") == fingerprint(value["raw_postiz"])
        and result.get("integration_hash") == fingerprint(value["raw_integration"])
        and remote_hash == snapshot["bridge"]["remote_hash"] == result.get("remote_hash")
        and body == snapshot["variant"]["body"]
        and result.get("body_hash") == fingerprint(body)
        and due == snapshot["bridge"]["due_at"]
        and value["raw_postiz"].get("state") == "DRAFT"
        and result.get("image_sha256") == snapshot["variant"]["media"][0]["sha256"]
        and result.get("image_mime") == snapshot["variant"]["media"][0]["mime"]
    )
    result["artifact"] = {
        "path": path.relative_to(core.settings.root).as_posix(),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return result


def inspect(core, record_id):
    """Read-only local plan and two fresh external reads; no DB/queue mutation."""
    with core.db.session() as session:
        if session.get_bind().dialect.name == "postgresql":
            session.execute(text("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"))
        snapshot, _ = _capture(core, session, record_id)
        require(
            not mutation_blocked(session, snapshot["variant"]["id"]),
            "PREAPPROVAL_OPERATION_ALREADY_EXISTS",
        )
    observations = [_observe(core, snapshot, "inspect", ordinal) for ordinal in (1, 2)]
    require(_observed_identity(observations[0]) == _observed_identity(observations[1]))
    with core.db.session() as session:
        current, _ = _capture(core, session, record_id)
    require(current == snapshot, "PREAPPROVAL_PLAN_CHANGED")
    for observation in observations:
        _fresh(observation)
    return {
        "contract": CONTRACT,
        "plan_hash": fingerprint(snapshot),
        "snapshot": snapshot,
        "observations": observations,
        "effect": "record_cancelled_slot_missed_draft_preserved_no_send",
    }


def _event(core, variant_id, stage, details):
    try:
        with core.db.session.begin() as session:
            ident = _id(variant_id, stage)
            existing = session.get(Audit, ident)
            require(existing is None, "PREAPPROVAL_STAGE_ALREADY_EXISTS")
            session.add(
                Audit(
                    id=ident,
                    actor="owner_reconciliation",
                    action="preapproval_retirement_" + stage,
                    entity=variant_id,
                    details=copy.deepcopy(details),
                )
            )
    except IntegrityError as exc:
        raise AgentError("PREAPPROVAL_OPERATION_IN_PROGRESS", 409) from exc


def _verify_committed(session, claim, committed):
    ledger = session.get(EditorialLedger, LEDGER_ID)
    records = (
        [
            row
            for row in ledger.payload.get("records", [])
            if row.get("id") == committed.details["record_id"]
        ]
        if ledger
        else []
    )
    slot = session.get(PublicationSlot, committed.details["slot_id"])
    require(
        claim is not None
        and len(records) == 1
        and slot is not None
        and committed.details["operation_id"] == claim.details["operation_id"]
        and committed.details["variant_id"] == claim.entity
        and fingerprint(records[0]) == committed.details["after_record_hash"]
        and fingerprint({**_slot_view(slot), "active_target": slot.active_target})
        == committed.details["after_slot_hash"],
        "PREAPPROVAL_COMMITTED_STATE_CHANGED",
    )


def apply(core, record_id, *, operation_id, plan_hash, actor, reason, retire_confirmed):
    require(
        retire_confirmed is True
        and isinstance(actor, str)
        and actor.startswith("postiz:")
        and isinstance(reason, str)
        and 20 <= len(reason.strip()) <= 500,
        "PREAPPROVAL_OWNER_DECISION_REQUIRED",
    )
    _operation_id(operation_id)
    # A lost response after commit is read back before ordinary preconditions:
    # the committed record is now cancelled and must never be re-applied.
    with core.db.session() as session:
        ledger = session.get(EditorialLedger, LEDGER_ID)
        found = (
            [r for r in ledger.payload.get("records", []) if r.get("id") == record_id]
            if ledger
            else []
        )
        require(len(found) == 1)
        variant_id = found[0].get("variant_id")
        done = session.get(Audit, _id(variant_id, "commit"))
        if done is not None:
            original = session.get(Audit, _id(variant_id, "claim"))
            slot = session.get(PublicationSlot, done.details["slot_id"])
            require(
                original is not None
                and original.details["operation_id"] == operation_id
                and original.details["plan_hash"] == plan_hash
                and done.details["operation_id"] == operation_id
                and fingerprint(found[0]) == done.details["after_record_hash"]
                and fingerprint({**_slot_view(slot), "active_target": slot.active_target})
                == done.details["after_slot_hash"],
                "PREAPPROVAL_COMMITTED_STATE_CHANGED",
            )
            return copy.deepcopy(done.details)
    with core.db.session.begin() as session:
        snapshot, rows = _capture(core, session, record_id, locked=True)
        variant_id = rows[4].id
        require(
            not any(
                row.entity != variant_id and row.details.get("operation_id") == operation_id
                for row in session.scalars(select(Audit).where(Audit.action == CLAIM))
            ),
            "PREAPPROVAL_OPERATION_CONFLICT",
        )
        claim_id = _id(variant_id, "claim")
        existing = session.get(Audit, claim_id)
        done = session.get(Audit, _id(variant_id, "commit"))
        if done is not None:
            require(
                existing.details["operation_id"] == operation_id
                and done.details["operation_id"] == operation_id
            )
            return copy.deepcopy(done.details)
        if existing is None:
            require(fingerprint(snapshot) == plan_hash, "PREAPPROVAL_PLAN_CHANGED")
            session.add(
                Audit(
                    id=claim_id,
                    actor=actor,
                    action=CLAIM,
                    entity=variant_id,
                    details={
                        "contract": CONTRACT,
                        "operation_id": operation_id,
                        "plan_hash": plan_hash,
                        "before": copy.deepcopy(snapshot),
                        "reason": reason.strip(),
                        "at": now(),
                        "minimum_runtime_version": "0.14.1",
                        "automatic_downgrade_allowed": False,
                    },
                )
            )
        else:
            require(
                existing.details["operation_id"] == operation_id
                and existing.details["plan_hash"] == plan_hash,
                "PREAPPROVAL_OPERATION_CONFLICT",
            )
            require(existing.details["before"] == snapshot, "PREAPPROVAL_PLAN_CHANGED")
    observations = []
    for ordinal in (1, 2):
        stage = "read" + str(ordinal)
        with core.db.session() as session:
            started = session.get(Audit, _id(variant_id, stage + "_started"))
            completed = session.get(Audit, _id(variant_id, stage + "_completed"))
        if completed is not None:
            observation = copy.deepcopy(completed.details)
            saved = _saved_observation(core, operation_id, ordinal, snapshot)
            require(saved == observation, "PREAPPROVAL_SAVED_EVIDENCE_CHANGED")
        else:
            observation = (
                _saved_observation(core, operation_id, ordinal, snapshot)
                if started is not None
                else None
            )
            if observation is None:
                require(started is None, "PREAPPROVAL_READ_UNKNOWN")
                _event(
                    core,
                    variant_id,
                    stage + "_started",
                    {"operation_id": operation_id, "at": now()},
                )
                observation = _observe(core, snapshot, operation_id, ordinal, save=True)
            _event(core, variant_id, stage + "_completed", observation)
        require(
            observation.get("operation_id") == operation_id
            and observation.get("ordinal") == ordinal
        )
        _fresh(observation)
        observations.append(observation)
    require(_observed_identity(observations[0]) == _observed_identity(observations[1]))
    with core.db.session.begin() as session:
        current, rows = _capture(core, session, record_id, locked=True)
        require(current == snapshot, "PREAPPROVAL_PLAN_CHANGED")
        for observation in observations:
            _fresh(observation)
        control, ledger, record, slot, variant, target, remote, bridge = rows
        done = session.get(Audit, _id(variant_id, "commit"))
        if done is not None:
            return copy.deepcopy(done.details)
        stamp = float(now())
        payload = copy.deepcopy(ledger.payload)
        output = next(r for r in payload["records"] if r["id"] == record_id)
        disposition = {
            "contract": CONTRACT,
            "operation_id": operation_id,
            "at": stamp,
            "previous_state": record["state"],
            "previous_error": record["error"],
            "before_record_hash": fingerprint(record),
            "before_slot_hash": fingerprint(snapshot["slot"]),
            "postiz_id": bridge.post_id,
            "source_remains_used": True,
            "draft_preserved": True,
            "observation_hashes": [fingerprint(x) for x in observations],
        }
        if "schedule_timeout" in snapshot:
            disposition["schedule_timeout"] = snapshot["schedule_timeout"]
            bridge.mutation_owner, bridge.mutation_until = None, 0
        output.update(state="cancelled", cancelled_at=stamp, no_send_disposition=disposition)
        output["preparation_actions"] = [
            *output.get("preparation_actions", []),
            {"action": "owner_retired_preapproval_draft", **disposition},
        ]
        result = session.execute(
            update(EditorialLedger)
            .where(
                EditorialLedger.id == LEDGER_ID,
                EditorialLedger.revision == snapshot["ledger_revision"],
            )
            .values(payload=payload, revision=snapshot["ledger_revision"] + 1, updated_at=stamp)
        )
        require(result.rowcount == 1, "ARTICLE_LEDGER_REVISION_CONFLICT")
        slot.state, slot.active_target, slot.error, slot.updated_at = (
            "missed",
            None,
            "OWNER_PREAPPROVAL_DRAFT_RETIRED",
            stamp,
        )
        receipt = {
            "contract": CONTRACT,
            "operation_id": operation_id,
            "record_id": record_id,
            "variant_id": variant_id,
            "slot_id": slot.id,
            "postiz_id": bridge.post_id,
            "state": "retired_no_send",
            "at": stamp,
            "minimum_runtime_version": "0.14.1",
            "automatic_downgrade_allowed": False,
            "control_revision_unchanged": control.revision,
            "before_record": snapshot["record"],
            "before_slot": snapshot["slot"],
            "disposition": disposition,
            "after_record_hash": fingerprint(output),
            "after_ledger_hash": fingerprint(payload),
            "after_slot_hash": fingerprint(
                {**_slot_view(slot), "active_target": slot.active_target}
            ),
        }
        session.add(
            Audit(
                id=_id(variant_id, "commit"),
                actor=actor,
                action=COMMIT,
                entity=variant_id,
                details=receipt,
            )
        )
        return copy.deepcopy(receipt)


def operation_status(core, operation_id, *, expected_actor=None, expected_record_id=None):
    _operation_id(operation_id)
    with core.db.session() as session:
        claims = [
            row
            for row in session.scalars(select(Audit).where(Audit.action == CLAIM))
            if row.details.get("operation_id") == operation_id
        ]
        require(len(claims) == 1, "PREAPPROVAL_OPERATION_NOT_FOUND")
        if expected_actor is not None:
            require(claims[0].actor == expected_actor
                    and claims[0].details["before"]["record"]["id"] == expected_record_id,
                    "PREAPPROVAL_OPERATION_NOT_FOUND")
        variant_id = claims[0].entity
        rows = [
            session.get(Audit, _id(variant_id, stage))
            for stage in (
                "claim",
                "read1_started",
                "read1_completed",
                "read2_started",
                "read2_completed",
                "commit",
            )
        ]
        if rows[-1] is not None:
            _verify_committed(session, claims[0], rows[-1])
            if expected_actor is not None:
                return copy.deepcopy(rows[-1].details)
        return {
            "contract": CONTRACT,
            "operation_id": operation_id,
            "variant_id": variant_id,
            "state": "retired_no_send" if rows[-1] else "blocked_reconciliation",
            "stages": [
                {"action": row.action, "at": row.at, "hash": fingerprint(row.details)}
                for row in rows
                if row
            ],
        }
