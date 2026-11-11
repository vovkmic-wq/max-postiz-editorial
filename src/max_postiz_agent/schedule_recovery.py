"""Durable deadline for a MAX schedule change made before approval."""
import copy
import hashlib
import logging
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy import select

from .article_reserve import _IDENTITY, _slot_view
from .config import AgentError, fingerprint
from .db import Approval, Audit, Intent, now, uid
from .hub_models import BridgePost, Dispatch, RemoteDraft, Target, TargetApproval, Variant
from .mobile_hold import assert_item_unheld
from .preapproval_retirement import CLAIM, COMMIT, CONTRACT, _id, _verify_committed, mutation_blocked
from .server_models import EditorialControl, EditorialLedger, PublicationSlot
from .server_state import CONTROL_ID, LEDGER_ID, fence_editorial_configuration, require_running

ACTOR = "editorial_schedule"
DRAFT_CONFIRMED = "max_schedule_draft_confirmed"
DRAFT_CONFLICT = "max_schedule_draft_conflict"
SCHEDULE_STATES = {"automatic_scheduling", "automatic_pending_approval", "needs_attention"}
CAS_SECONDS = 20
RESTORE_INTERVAL = 30
_LOG = logging.getLogger(__name__)

Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]
Digest = Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")]
Millis = Annotated[int, Field(ge=0, lt=253402300800000)]


class ScheduleOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    operation_id: Annotated[str, Field(pattern=r"^[a-f0-9]{8}(-[a-f0-9]{4}){3}-[a-f0-9]{12}$")]
    post_id: Identifier
    group_id: Identifier
    integration_id: Identifier
    record_id: Identifier
    variant_id: Identifier
    slot_id: Identifier
    expected_hash: Digest
    content_sha256: Digest
    image_json_sha256: Digest
    settings_sha256: Digest
    draft_due_ms: Millis
    due_ms: Millis
    deadline_ms: Millis


def _milliseconds(stamp):
    return round(stamp * 1_000_000) // 1000


def _ensure(value, code="MAX_SCHEDULE_NOT_SAFE"):
    if not value:
        raise AgentError(code, 409)


def _record(ledger, record_id):
    records = [record for record in ledger.payload["records"] if record["id"] == record_id] if ledger else []
    _ensure(len(records) == 1)
    return records[0]


def plan(core, record, assignment, before):
    """Capture the exact existing Postiz draft before authorizing one write."""
    core.postiz.require_max_schedule_journal()
    remote = core.postiz.get_post_fresh(record["postiz_id"])
    _, _, draft_due, remote_hash = core.bridge.normalize(remote)
    _ensure(remote.get("id") == record["postiz_id"] and remote.get("state") == "DRAFT"
        and remote.get("integration", {}).get("providerIdentifier") == "max-browser"
        and not remote.get("parentPostId") and not remote.get("deletedAt")
        and not remote.get("releaseId") and not remote.get("releaseURL")
        and before["metadata"].get("postiz_hash") == remote_hash
        and before["metadata"].get("postiz_state") == "DRAFT"
        and all(isinstance(remote.get(key), str) for key in ("content", "image", "settings")))
    due_ms = _milliseconds(assignment["due_at"])
    stamp_ms = _milliseconds(now())
    deadline_ms = min(stamp_ms + CAS_SECONDS * 1000, due_ms - 5000)
    _ensure(stamp_ms < deadline_ms, "MAX_SCHEDULE_DEADLINE")
    operation = ScheduleOperation(operation_id=uid(), post_id=remote["id"], group_id=remote["group"],
        integration_id=remote["integration"]["id"], record_id=record["id"], variant_id=record["variant_id"],
        slot_id=assignment["id"], expected_hash=remote_hash, draft_due_ms=_milliseconds(draft_due),
        content_sha256=hashlib.sha256(remote["content"].encode()).hexdigest(),
        image_json_sha256=hashlib.sha256(remote["image"].encode()).hexdigest(),
        settings_sha256=hashlib.sha256(remote["settings"].encode()).hexdigest(),
        due_ms=due_ms, deadline_ms=deadline_ms)
    from .article_cycle import HourlyArticleCycle
    return {"operation": operation.model_dump(), "slot_token": assignment["token"],
            "control_revision": assignment["control_revision"],
            "draft_revision": before["revision"],
            "reviewed_hash": fingerprint(HourlyArticleCycle._reviewed_fields(before))}


class ScheduleRecovery:
    def __init__(self, core):
        self.core = core
        self.next_restore = 0

    def _capture(self, session, operation):
        from .article_cycle import HourlyArticleCycle

        fence_editorial_configuration(session)
        control = session.get(EditorialControl, CONTROL_ID, with_for_update=True)
        variant = session.get(Variant, operation.variant_id, with_for_update=True)
        _ensure(control and variant and not mutation_blocked(session, operation.variant_id))
        target = session.get(Target, variant.target_id, with_for_update=True)
        link = session.get(BridgePost, operation.post_id, with_for_update=True)
        slot = session.get(PublicationSlot, operation.slot_id, with_for_update=True)
        ledger = session.get(EditorialLedger, LEDGER_ID, with_for_update=True)
        record = _record(ledger, operation.record_id)
        draft = session.get(RemoteDraft, variant.id)
        tracked = record.get("schedule_journal") or {}
        op = operation.model_dump()
        _ensure(tracked.get("operation") == op and tracked.get("draft_revision") in
                (variant.revision, variant.revision - 1)
            and record["state"] in SCHEDULE_STATES and not record.get("dispatch_id")
            and record.get("variant_id") == variant.id and record.get("postiz_id") == operation.post_id
            and record.get("publication_slot_id") == operation.slot_id
            and variant.state == "review_pending" and target and target.enabled
            and target.platform == "max" and target.route == "max_browser"
            and target.options.get("postiz_integration_id") == operation.integration_id
            and draft and draft.state == "draft" and draft.external_id == operation.post_id
            and link and link.variant_id == variant.id and link.target_id == target.id and link.active
            and link.group_id == operation.group_id and link.integration_id == operation.integration_id
            and link.remote_state in {"DRAFT", "QUEUE", "ERROR"}
            and slot and slot.state in {"assigned", "blocked_unknown"}
            and slot.record_id == record["id"] and slot.target_id == slot.active_target == target.id
            and slot.token == tracked.get("slot_token") and slot.control_revision == tracked.get("control_revision")
            and slot.due_at == record.get("due_at") and _milliseconds(slot.due_at) == operation.due_ms
            and not slot.dispatch_id and slot.delivered_at is None
            and slot.binding == {key: record.get(key) for key in _IDENTITY}
            and tracked.get("reviewed_hash") == fingerprint(HourlyArticleCycle._reviewed_fields(
                self.core.snapshot(variant, target))))
        _ensure((link.mutation_owner is None and link.mutation_until == 0) or
                (link.mutation_owner == operation.operation_id and link.mutation_until == operation.deadline_ms / 1000))
        # Both the normal approval path and legacy send hold the exact local
        # variant/draft. Any retained approval, including a revoked one, wins.
        _ensure(not session.scalar(select(Dispatch.id).where(Dispatch.variant_id == variant.id).limit(1))
            and not session.scalar(select(TargetApproval.id).where(TargetApproval.variant_id == variant.id).limit(1))
            and not session.scalar(select(Approval.id).where(Approval.draft_id == variant.draft_id).limit(1))
            and not session.scalar(select(Intent.id).where(Intent.draft_id == variant.draft_id).limit(1)))
        return control, link, slot, ledger, record, tracked

    def start(self, operation):
        _ensure(self.core.postiz.schedule_journal_enabled, "MAX_SCHEDULE_JOURNAL_DISABLED")
        _ensure(_milliseconds(now()) < operation.deadline_ms, "MAX_SCHEDULE_DEADLINE")
        with self.core.db.session.begin() as session:
            control, link, slot, _, record, tracked = self._capture(session, operation)
            require_running(self.core, session)
            assert_item_unheld(session, operation.variant_id)
            stamp = now()
            _ensure(_milliseconds(stamp) < operation.deadline_ms < operation.due_ms
                and operation.deadline_ms <= _milliseconds(stamp) + (CAS_SECONDS + 1) * 1000,
                "MAX_SCHEDULE_DEADLINE")
            _ensure(slot.state == "assigned" and record["state"] == "automatic_scheduling"
                and control.revision == tracked["control_revision"] and not link.error
                and link.remote_state == "DRAFT" and link.remote_hash == operation.expected_hash
                and _milliseconds(link.due_at) == operation.draft_due_ms and link.mutation_owner is None)
            link.mutation_owner, link.mutation_until = operation.operation_id, operation.deadline_ms / 1000
            link.updated_at = max(stamp, link.updated_at)
        return {"owner": operation.operation_id}

    def _close_slot(self, operation):
        with self.core.db.session.begin() as session:
            _, link, slot, ledger, record, _ = self._capture(session, operation)
            stamp = now()
            _ensure(stamp >= slot.due_at and _milliseconds(stamp) >= operation.deadline_ms, "MAX_SCHEDULE_DEADLINE")
            op = operation.model_dump()
            before = {"record": copy.deepcopy(record),
                      "slot": {**_slot_view(slot), "active_target": slot.active_target}}
            disposition = {"operation_id": operation.operation_id, "at": stamp,
                "reason": "slot_deadline_elapsed_before_approval", "postiz_state": "draft_restore_pending",
                "source_remains_used": True, "released_lease": link.mutation_owner,
                "previous_error": record.get("error")}
            output = {**record, "state": "cancelled", "cancelled_at": stamp,
                      "error": None, "no_send_disposition": disposition}
            ledger.payload = {"records": [output if item["id"] == record["id"] else item
                                          for item in ledger.payload["records"]]}
            ledger.revision += 1
            ledger.updated_at = stamp
            slot.state, slot.active_target, slot.error, slot.updated_at = "missed", None, None, stamp
            link.mutation_owner, link.mutation_until = None, 0
            # The normal retired-variant fence and the outbox commit together.
            common = {"contract": CONTRACT, "operation_id": operation.operation_id,
                      "minimum_runtime_version": "0.15.5", "automatic_downgrade_allowed": False}
            session.add(Audit(id=_id(operation.variant_id, "claim"), actor=ACTOR, action=CLAIM,
                entity=operation.variant_id, details={**common, "before": before, "operation": op}))
            session.add(Audit(id=_id(operation.variant_id, "commit"), actor=ACTOR, action=COMMIT,
                entity=operation.variant_id, details={**common, "record_id": record["id"],
                    "variant_id": operation.variant_id, "slot_id": slot.id, "postiz_id": link.post_id,
                    "state": "retired_no_send", "at": stamp, "disposition": disposition,
                    "before_record": before["record"], "before_slot": before["slot"],
                    "after_record_hash": fingerprint(output),
                    "after_slot_hash": fingerprint({**_slot_view(slot), "active_target": slot.active_target})}))

    def authorize_retirement(self, operation):
        with self.core.db.session() as session:
            claim = session.get(Audit, _id(operation.variant_id, "claim"))
            commit = session.get(Audit, _id(operation.variant_id, "commit"))
            _ensure(claim and claim.actor == ACTOR and claim.action == CLAIM and claim.entity == operation.variant_id
                and commit and commit.actor == ACTOR and commit.action == COMMIT
                and claim.details.get("operation") == operation.model_dump()
                and commit.details.get("slot_id") == operation.slot_id
                and commit.details.get("state") == "retired_no_send")
            _verify_committed(session, claim, commit)
        return {"retired": True}

    def _due_operations(self):
        with self.core.db.session() as session:
            due_records = set(session.scalars(select(PublicationSlot.record_id).where(
                PublicationSlot.active_target.is_not(None), PublicationSlot.due_at <= now(),
                PublicationSlot.state.in_({"assigned", "blocked_unknown"}))))
            ledger = session.get(EditorialLedger, LEDGER_ID) if due_records else None
            operations = [(record["schedule_journal"] or {}).get("operation")
                          for record in ledger.payload["records"]
                          if record["id"] in due_records and record["state"] in SCHEDULE_STATES
                          and record.get("schedule_journal")] if ledger else []
        return operations

    def _restore_one(self):
        # Commit rows without an outcome are a persistent outbox. The only
        # repeated write here is a CAS restore using the same operation ID.
        with self.core.db.session() as session:
            outcome = Audit.__table__.alias("schedule_outcome")
            row = session.scalar(select(Audit).where(Audit.actor == ACTOR, Audit.action == CLAIM,
                ~select(outcome.c.id).where(outcome.c.entity == Audit.entity,
                    outcome.c.action.in_({DRAFT_CONFIRMED, DRAFT_CONFLICT})).exists()).order_by(Audit.at).limit(1))
            self.next_restore = now() + RESTORE_INTERVAL
            if row is None:
                return
            operation = ScheduleOperation.model_validate(row.details["operation"])
        result = self.core.postiz.retire_max_schedule(operation)
        _ensure(result in {"draft_preserved", "draft_restored", "draft_conflict"})
        with self.core.db.session.begin() as session:
            ident = _id(operation.variant_id, "schedule-draft-result")
            if session.get(Audit, ident) is None:
                if result != "draft_conflict":
                    link = session.get(BridgePost, operation.post_id, with_for_update=True)
                    if (link and link.variant_id == operation.variant_id and link.group_id == operation.group_id
                            and link.integration_id == operation.integration_id and not link.mutation_owner
                            and link.mutation_until == 0):
                        link.remote_hash, link.remote_state = operation.expected_hash, "DRAFT"
                        link.due_at, link.updated_at = operation.draft_due_ms / 1000, now()
                session.add(Audit(id=ident, actor=ACTOR,
                    action=DRAFT_CONFLICT if result == "draft_conflict" else DRAFT_CONFIRMED,
                    entity=operation.variant_id, details={"operation_id": operation.operation_id,
                        "postiz_id": operation.post_id, "outcome": result}))

    def run_once(self):
        leader = getattr(self.core, "editorial_leader_check", None)
        if leader is not None:
            leader()
        closed = False
        for value in self._due_operations():
            try:
                self._close_slot(ScheduleOperation.model_validate(value))
                closed = True
            except (AgentError, ValidationError):
                # Wrong identity, a foreign lease or an approval keeps this
                # slot under the existing reconciliation guard.
                continue
        if not closed and now() < self.next_restore:
            return
        try:
            self._restore_one()
        except (AgentError, ValidationError) as exc:
            _LOG.warning("MAX schedule draft recovery pending (%s)", getattr(exc, "code", "invalid_operation"))
