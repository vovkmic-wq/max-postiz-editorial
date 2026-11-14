"""Quarantine a single MAX delivery using positive proof that its worker finished before Send."""

import copy
from time import monotonic
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from .article_reserve import _hash, _postiz_due_matches, _slot_view
from .config import AgentError, fingerprint
from .db import Audit, now
from .hub_models import BridgePost, Dispatch, DispatchAttempt, Target, TargetApproval, Variant
from .server_models import EditorialControl, EditorialLedger, PublicationSlot
from .server_state import CONTROL_ID, LEDGER_ID, fence_editorial_configuration

AUTOMATIC_ACTION = "reserve_unknown_worker_finished"
AUTOMATIC_CONTRACT = "reserve-max-worker-finished-before-send-v1"
AUTOMATIC_ERROR = "BROWSER_FINISHED_BEFORE_SEND"
OWNER_ACTION = "owner_abandoned_unverified_delivery"
OWNER_CONTRACT = "owner-retire-unverified-max-delivery-b9f29832-v1"
OWNER_ERROR = "OWNER_ABANDONED_UNVERIFIED_DELIVERY"
UNSAFE = "ARTICLE_RECONCILIATION_REQUIRED"
MAX_OBSERVATION_AGE = 20


def retirement_id(variant_id):
    return str(uuid5(NAMESPACE_URL, "max-postiz:retired-delivery:" + variant_id))


def retirement_audit(session, variant_id):
    audit = session.get(Audit, retirement_id(variant_id))
    if audit is not None and audit.action in {AUTOMATIC_ACTION, OWNER_ACTION}:
        return audit
    return None


def _require(condition):
    if not condition:
        raise AgentError(UNSAFE, 409)


def validate_retained_journal(historical_settings, session, dispatch, raw_sha256, value):
    """Keep an archived preparing journal visible to later cohort-wide reconciliation."""
    audit = retirement_audit(session, dispatch.variant_id)
    if audit is None:
        return False
    details = audit.details
    automatic = audit.action == AUTOMATIC_ACTION
    code = "PREAPPROVAL_JOURNAL_INVALID"
    slot = session.get(PublicationSlot, details.get("slot_id"))
    approval = session.get(TargetApproval, dispatch.approval_id)
    ledger = session.get(EditorialLedger, LEDGER_ID)
    records = [r for r in ledger.payload["records"] if r.get("id") == details.get("record_id")] if ledger else []
    attempts = list(session.scalars(select(DispatchAttempt).where(
        DispatchAttempt.dispatch_id == dispatch.id).limit(2)))
    if not (audit.entity == dispatch.id == details.get("dispatch_id")
            and details.get("variant_id") == dispatch.variant_id
            and details.get("contract") == (AUTOMATIC_CONTRACT if automatic else OWNER_CONTRACT)
            and audit.actor == ("article_reserve" if automatic else "owner_operational_exception")
            and details.get("original_external_result") == "UNKNOWN"
            and details.get("permanent_no_retry") is True and details.get("source_remains_ever_used") is True
            and details.get("verified_delivery") is False
            and _hash(raw_sha256) and raw_sha256 == details.get("browser_journal_sha256")
            and (automatic or (details.get("historical_no_send_proved") is False
                               and details.get("original_attempt_preserved") is True))
            and value.get("contract") == "browser-send-journal-v1"
            and value.get("state") == "preparing" and value.get("send_started") is False
            and value.get("snapshot_hash") == fingerprint(dispatch.snapshot)
            and dispatch.state == "cancelled"
            and dispatch.error == (AUTOMATIC_ERROR if automatic else OWNER_ERROR)
            and dispatch.snapshot_hash == fingerprint({"content": dispatch.snapshot, "due_at": dispatch.due_at})
            and dispatch.attempts == 1 and dispatch.owner is None and dispatch.lease_until == 0
            and dispatch.verified is False and dispatch.external_id is None and dispatch.permalink is None
            and len(attempts) == 1 and attempts[0].result == "unknown"
            and attempts[0].id == details.get("original_unknown_attempt_id")
            and approval is not None and approval.revoked is True
            and approval.variant_id == dispatch.variant_id and approval.snapshot_hash == dispatch.snapshot_hash
            and slot is not None and slot.record_id == details.get("record_id")
            and slot.dispatch_id == dispatch.id and slot.state == "missed" and slot.error == dispatch.error
            and slot.active_target is None and slot.delivered_at is None and slot.due_at == dispatch.due_at
            and len(records) == 1):
        raise AgentError(code, 409)
    record = records[0]
    if not (record.get("state") == "cancelled" and record.get("dispatch_id") == dispatch.id
            and record.get("publication_slot_id") == slot.id
            and record.get("original_external_result") == "UNKNOWN"
            and record.get("permanent_no_retry") is True and record.get("source_remains_ever_used") is True
            and record.get("delivery_quarantine" if automatic else "owner_abandonment") == details):
        raise AgentError(code, 409)
    if automatic:
        from .browser_publisher import read_finished_preclick_receipt

        try:
            proof = read_finished_preclick_receipt(historical_settings, dispatch.id, snapshot=dispatch.snapshot)
        except AgentError as exc:
            raise AgentError(code, 409) from exc
        if proof["sha256"] != details.get("finished_receipt_sha256") or proof["journal_sha256"] != raw_sha256:
            raise AgentError(code, 409)
    return True


def release_finished_unknown(store, cycle, target_id, records):
    """Observe Postiz once before the transaction. Preserve the original unknown attempt."""
    from .browser_publisher import read_finished_preclick_receipt
    from .max_bridge import MaxBridge

    core = store.core
    record_id = store.preparation_only_unknown(target_id, records, cycle=cycle)
    if record_id is None:
        raise AgentError(UNSAFE, 409)
    record = next(record for record in records if record["id"] == record_id)
    with core.db.session() as session:
        dispatch = session.get(Dispatch, record["dispatch_id"])
        control = session.get(EditorialControl, CONTROL_ID)
        _require(dispatch is not None and control is not None and not control.paused)
        starting_revision = control.revision
        snapshot = dispatch.snapshot
        request_id = dispatch.id
    proof = read_finished_preclick_receipt(core.settings, request_id, snapshot=snapshot)
    remote = core.postiz.get_post_fresh(record["postiz_id"])
    observed_at = monotonic()
    _require(isinstance(remote, dict) and remote.get("id") == record["postiz_id"] and remote.get("state") == "ERROR"
             and remote.get("releaseId") in (None, "") and remote.get("releaseURL") in (None, ""))
    body, _, due, remote_hash = MaxBridge.normalize(remote)
    queue_hash = MaxBridge.normalize({**remote, "state": "QUEUE"})[3]
    _require(body == snapshot["body"] and _postiz_due_matches(due, record["due_at"])
             and snapshot["metadata"].get("postiz_hash") == queue_hash)

    with core.db.session.begin() as session:
        fence_editorial_configuration(session)
        control = store._control(session)
        _require(control.revision == starting_revision)
        variant = session.scalar(select(Variant).where(Variant.id == record["variant_id"]).with_for_update())
        target = session.scalar(select(Target).where(Target.id == target_id).with_for_update())
        dispatch = session.scalar(select(Dispatch).where(Dispatch.id == request_id).with_for_update())
        _require(variant is not None and target is not None and dispatch is not None)
        approval = session.scalar(select(TargetApproval).where(
            TargetApproval.id == dispatch.approval_id).with_for_update())
        bridge = session.scalar(select(BridgePost).where(
            BridgePost.post_id == record["postiz_id"]).with_for_update())
        slot = session.scalar(select(PublicationSlot).where(
            PublicationSlot.active_target == target_id).with_for_update())
        ledger = session.scalar(select(EditorialLedger).where(EditorialLedger.id == LEDGER_ID).with_for_update())
        _require(approval is not None and bridge is not None and slot is not None and ledger is not None)
        payload = copy.deepcopy(ledger.payload)
        _require(store.preparation_only_unknown(target_id, payload["records"], cycle=cycle, session=session)
                 == record_id)
        current = next(row for row in payload["records"] if row["id"] == record_id)
        _require(current == record and dispatch.snapshot == snapshot == core.snapshot(variant, target)
                 and dispatch.variant_id == variant.id and slot.dispatch_id == dispatch.id
                 and bridge.post_id == remote["id"] and bridge.variant_id == variant.id
                 and bridge.target_id == target.id and bridge.active
                 and bridge.group_id == remote["group"] and bridge.integration_id == remote["integration"]["id"]
                 and bridge.integration_id == target.options.get("postiz_integration_id")
                 and _postiz_due_matches(bridge.due_at, dispatch.due_at)
                 and (bridge.remote_state, bridge.remote_hash) in {("QUEUE", queue_hash), ("ERROR", remote_hash)}
                 and bridge.error in {None, "REMOTE_EDIT_DURING_SEND"}
                 and retirement_audit(session, variant.id) is None)
        attempt = session.scalars(select(DispatchAttempt).where(
            DispatchAttempt.dispatch_id == dispatch.id).with_for_update()).one()
        receipt = proof["receipt"]
        stamp = now()
        _require(monotonic() - observed_at <= MAX_OBSERVATION_AGE)
        _require(dispatch.due_at <= receipt["started_at"] <= receipt["finished_at"] <= attempt.created_at <= stamp)
        _require(read_finished_preclick_receipt(core.settings, dispatch.id, snapshot=snapshot) == proof)
        disposition = {
            "contract": AUTOMATIC_CONTRACT, "closed_at": stamp, "control_revision": control.revision,
            "variant_id": variant.id, "target_id": target.id, "record_id": record_id, "slot_id": slot.id,
            "dispatch_id": dispatch.id, "original_unknown_attempt_id": attempt.id,
            "original_external_result": "UNKNOWN", "verified_delivery": False, "permanent_no_retry": True,
            "source_remains_ever_used": True, "snapshot_hash": dispatch.snapshot_hash,
            "browser_journal_sha256": proof["journal_sha256"], "finished_receipt_sha256": proof["sha256"],
            "remote_proof_hash": fingerprint(remote),
        }
        dispatch.state, dispatch.error, approval.revoked = "cancelled", AUTOMATIC_ERROR, True
        slot.state, slot.active_target, slot.error, slot.updated_at = "missed", None, AUTOMATIC_ERROR, stamp
        bridge.active, bridge.error, bridge.updated_at = False, AUTOMATIC_ERROR, stamp
        bridge.remote_state, bridge.remote_hash = "ERROR", remote_hash
        current.update(state="cancelled", dispatch_state="cancelled", original_external_result="UNKNOWN",
                       publication_slot_id=slot.id,
                       permanent_no_retry=True, source_remains_ever_used=True, delivery_quarantine=disposition)
        ledger.payload, ledger.revision, ledger.updated_at = payload, ledger.revision + 1, stamp
        session.add(Audit(id=retirement_id(variant.id), actor="article_reserve", action=AUTOMATIC_ACTION,
                          entity=dispatch.id, details=disposition))
        return _slot_view(slot)
