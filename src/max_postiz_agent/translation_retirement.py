"""Owner-only retirement of a failed bound translation, without retry or approval.

A failed connection does not prove that earlier translation segments never ran.
The historical model outcome remains unknown; its source can never be reused.
This maintenance domain is deliberately absent from public HTTP/control routes.
"""

import copy
import hashlib
import json
import math
import re
from uuid import NAMESPACE_URL, UUID, uuid5

from sqlalchemy import select

from .config import AgentError, fingerprint
from .db import Audit, Material, Story, now
from .hub_models import Dispatch, Variant
from .server_models import (
    EditorialControl,
    EditorialLedger,
    EditorialPreparationClaim,
    PublicationSlot,
)
from .server_state import CONTROL_ID, LEDGER_ID, fence_editorial_configuration
from .unbound_selection_reconciliation import _pin, _quiet, _view

CONTRACT = "bound-translation-transport-retirement-v1"
ACTION = "bound_translation_retired_without_retry"
ERROR = "TRANSLATION_SERVICE_UNAVAILABLE"


def require(value):
    if not value:
        raise AgentError("TRANSLATION_RETIREMENT_NOT_SAFE", 409)


def _audit_id(operation_id):
    try:
        require(isinstance(operation_id, str) and str(UUID(operation_id)) == operation_id)
    except (ValueError, TypeError, AttributeError) as exc:
        raise AgentError("TRANSLATION_RETIREMENT_NOT_SAFE", 409) from exc
    return str(uuid5(NAMESPACE_URL, CONTRACT + ":" + operation_id))


def _record(ledger, record_id):
    records = [r for r in ledger.payload.get("records", []) if r.get("id") == record_id]
    require(len(records) == 1)
    return records[0]


def _receipt(session, audit):
    require(audit.action == ACTION and audit.details.get("contract") == CONTRACT)
    details = audit.details
    request = details["request"]
    require(
        audit.id == _audit_id(request["operation_id"])
        and audit.actor == request["actor"]
        and audit.entity == request["claim_id"]
    )
    claim = session.get(EditorialPreparationClaim, audit.entity)
    ledger = session.get(EditorialLedger, LEDGER_ID)
    require(claim is not None and ledger is not None)
    record = _record(ledger, details["record_id"])
    require(
        fingerprint(_view(claim)) == details["claim_after_hash"]
        and fingerprint(record) == details["record_after_hash"]
    )
    require(
        claim.state == "abandoned_unknown"
        and claim.active_key is None
        and claim.error == ERROR
        and record.get("state") == "cancelled"
        and record.get("error") == ERROR
    )
    before_record = details["record_before"]
    before_claim = details["claim_before"]
    require(
        {
            k: v
            for k, v in record.items()
            if k not in {"state", "cancelled_at", "maintenance_disposition"}
        }
        == {k: v for k, v in before_record.items() if k != "state"}
        and {
            k: v for k, v in _view(claim).items() if k not in {"state", "active_key", "updated_at"}
        }
        == {k: v for k, v in before_claim.items() if k not in {"state", "active_key", "updated_at"}}
        and fingerprint(before_record) == request["expected_record_hash"]
        and fingerprint(before_claim) == request["expected_claim_hash"]
    )
    return {
        "state": "retired_without_retry",
        "operation_id": details["request"]["operation_id"],
        "claim_id": claim.id,
        "record_id": record["id"],
        "audit_id": audit.id,
        "receipt_hash": fingerprint(details),
        "historical_model_outcome": "unknown",
        "model_calls": 0,
        "external_operations": 0,
    }


def status(core, operation_id):
    """Response-loss recovery never opens evidence or invokes a model."""
    with core.db.session() as session:
        audit = session.get(Audit, _audit_id(operation_id))
        return _receipt(session, audit) if audit else {"state": "not_committed"}


def _evidence(core, proof, record, claim, trusted_proof_root):
    raw = _pin(core, proof, trusted_proof_root)
    seal = json.loads(raw)
    require(
        isinstance(seal, dict)
        and seal.get("contract") == CONTRACT
        and seal.get("claim_hash") == fingerprint(_view(claim))
        and seal.get("record_hash") == fingerprint(record)
        and seal.get("owner_decision") == "retire_source_preserve_unknown_no_retry"
    )
    refs = seal.get("files")
    require(isinstance(refs, dict) and set(refs) == {"trace", "input", "journal", "health"})
    content = {
        name: _pin(core, ref, trusted_proof_root, readable_config=True)
        for name, ref in refs.items()
    }
    health = json.loads(content["health"])
    require(
        type(health.get("at")) in {int, float}
        and 0 <= now() - health["at"] <= 120
        and health.get("http_status") == 200
        and health.get("generation_requests") == 0
    )
    body = health.get("body", {})
    require(
        body.get("status") == "configured"
        and body.get("artifacts_verified") is True
        and body.get("backend_contract") == "madlad400-candle-causal-v2"
        and body.get("model") == "madlad400-3b-q4k-causal-v2"
        and body.get("busy") is False
        and body.get("quarantined") is False
    )
    for name, field in (("trace", "quality_trace"), ("input", "input_file")):
        require(refs[name]["path"] == str(core.settings.root / record[field]))
    require(hashlib.sha256(content["input"]).hexdigest() == record.get("input_file_hash"))
    article = json.loads(content["input"])
    require(fingerprint(article) == record.get("input_hash"))
    events = [json.loads(line) for line in content["trace"].splitlines()]
    require(
        len(events) == 2
        and [e.get("phase") for e in events] == ["translation_started", "translation_failed"]
        and all(
            e.get("record_id") == record["id"]
            and type(e.get("attempt")) is int
            and e["attempt"] == 1
            for e in events
        )
        and events[1].get("error") == ERROR
    )
    stamps = [e.get("at") for e in events]
    require(
        all(type(v) in {float, int} and math.isfinite(v) for v in stamps)
        and claim.created_at <= stamps[0] <= stamps[1] <= claim.updated_at
    )
    source = events[0].get("context", {}).get("source_untrusted", {})
    require(
        source
        == {
            "title": article.get("title"),
            "body": article.get("original"),
            "url": article.get("url"),
        }
    )
    attempts = record.get("preparation_attempts", [])
    require(
        len(attempts) == 1
        and attempts[0].get("number") == 1
        and attempts[0].get("state") == "needs_attention"
        and attempts[0].get("error") == ERROR
        and attempts[0].get("operator_retry") is False
        and attempts[0]["started_at"] <= stamps[0] <= stamps[1] <= attempts[0]["finished_at"]
    )
    journal = [json.loads(line) for line in content["journal"].splitlines()]
    wrapper = str(core.settings.root / "deployment/linux/madlad-ssh-stdio.sh")
    # Full, owner-pinned journal window: exactly the failed exec and its two
    # terminal socat messages. Never infer zero earlier segments from this.
    require(
        len(journal) == 3
        and all(
            e.get("_SYSTEMD_UNIT") == "max-postiz-madlad-tunnel.service"
            and e.get("_BOOT_ID") == seal.get("boot_id")
            for e in journal
        )
    )
    times = [int(e["__REALTIME_TIMESTAMP"]) / 1_000_000 for e in journal]
    require(times == sorted(times) and stamps[0] <= times[0] <= times[-1] <= stamps[1])
    child, parent = journal[0].get("_PID"), journal[1].get("_PID")
    require(
        isinstance(child, str)
        and child.isdecimal()
        and isinstance(parent, str)
        and parent.isdecimal()
        and child != parent
        and journal[2].get("_PID") == parent
        and journal[0]["MESSAGE"].endswith(
            f'socat[{child}] E execvp("{wrapper}", "{wrapper}"): Permission denied'
        )
        and journal[1]["MESSAGE"].endswith(
            f"socat[{parent}] W waitpid(): child {child} exited with status 1"
        )
        and re.search(
            r" E read\(5, 0x[0-9a-f]+, 8192\): Connection reset by peer$", journal[2]["MESSAGE"]
        )
        is not None
    )
    # Re-read all pinned inputs under the same transaction before mutation.
    require(_pin(core, proof, trusted_proof_root) == raw)
    require(
        all(
            _pin(core, refs[n], trusted_proof_root, readable_config=True) == b
            for n, b in content.items()
        )
    )
    return seal


def apply(
    core,
    claim_id,
    *,
    operation_id,
    expected_revision,
    expected_claim_hash,
    expected_record_hash,
    proof,
    actor,
    trusted_proof_root=None,
):
    """Atomic cancellation + terminal unknown claim + immutable audit receipt."""
    audit_id = _audit_id(operation_id)
    require(
        isinstance(actor, str) and re.fullmatch(r"owner_maintenance:[A-Za-z0-9_.-]{1,45}", actor)
    )
    request = {
        "operation_id": operation_id,
        "claim_id": claim_id,
        "actor": actor,
        "expected_revision": expected_revision,
        "expected_claim_hash": expected_claim_hash,
        "expected_record_hash": expected_record_hash,
        "proof": proof,
    }
    with core.db.session.begin() as session:
        fence_editorial_configuration(session)
        control = session.scalar(
            select(EditorialControl).where(EditorialControl.id == CONTROL_ID).with_for_update()
        )
        claim = session.scalar(
            select(EditorialPreparationClaim)
            .where(EditorialPreparationClaim.id == claim_id)
            .with_for_update()
        )
        ledger = session.scalar(
            select(EditorialLedger).where(EditorialLedger.id == LEDGER_ID).with_for_update()
        )
        old = session.get(Audit, audit_id)
        if old:
            require(
                old.actor == actor
                and old.entity == claim_id
                and old.details.get("request") == request
            )
            return _receipt(session, old)
        require(
            control is not None
            and claim is not None
            and ledger is not None
            and control.paused is True
            and type(expected_revision) is int
            and control.revision == expected_revision == claim.control_revision + 1
            and fingerprint(_view(claim)) == expected_claim_hash
            and claim.active_key == "models"
            and claim.state == "blocked_unknown"
            and claim.error == ERROR
            and isinstance(claim.record_id, str)
        )
        _quiet(session, claim)
        record = _record(ledger, claim.record_id)
        require(
            fingerprint(record) == expected_record_hash
            and record.get("state") == "needs_attention"
            and record.get("error") == ERROR
            and record.get("reserve_contract") == "article-reserve-v1"
            and not any(
                record.get(k) is not None
                for k in (
                    "cancelled_at",
                    "maintenance_disposition",
                    "variant_id",
                    "postiz_id",
                    "dispatch_id",
                    "material_id",
                    "story_id",
                    "due_at",
                    "prepared_hash",
                    "reserve_prepared_hash",
                    "publication_slot_id",
                    "automatic_repair_attempts",
                )
            )
        )
        binding = {
            k: record.get(k)
            for k in (
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
        initial = record.get("reserve_claim", {})
        require(
            initial.get("id") == claim.id
            and initial.get("token") == claim.token
            and initial.get("control_revision") == claim.control_revision
        )
        audit = session.get(
            Audit, str(uuid5(NAMESPACE_URL, "max-postiz:reserve-binding:" + record["id"]))
        )
        require(
            audit is not None
            and audit.actor == "article_reserve"
            and audit.entity == record["id"]
            and audit.action == "article_reserve_preparation_binding"
            and audit.details == {"binding": binding, "binding_hash": fingerprint(binding)}
            and record.get("reserve_binding_hash") == fingerprint(binding)
        )
        # No Postiz state, staging lineage, slot, approval or publication may be retired.
        require(
            not session.scalar(select(Material).where(Material.url == record.get("url")))
            and not session.scalar(
                select(Story).where(
                    Story.text_hash == fingerprint({"hourly_article": record["id"]})
                )
            )
            and not session.scalar(
                select(PublicationSlot).where(PublicationSlot.record_id == record["id"])
            )
        )
        require(
            not any(
                v.metadata_json.get("record_id") == record["id"]
                for v in session.scalars(select(Variant))
            )
            and not any(
                d.snapshot.get("metadata", {}).get("record_id") == record["id"]
                for d in session.scalars(select(Dispatch))
            )
        )
        seal = _evidence(core, proof, record, claim, trusted_proof_root)
        stamp = now()
        require(type(stamp) in {int, float} and math.isfinite(stamp) and stamp >= claim.updated_at)
        before_record, before_claim = copy.deepcopy(record), _view(claim)
        payload = copy.deepcopy(ledger.payload)
        updated = next(r for r in payload["records"] if r["id"] == record["id"])
        updated.update(
            state="cancelled",
            cancelled_at=stamp,
            maintenance_disposition={
                "contract": CONTRACT,
                "operation_id": operation_id,
                "proof_sha256": proof["sha256"],
                "historical_model_outcome": "unknown",
                "source_remains_used": True,
                "no_retry": True,
                "approval_permitted": False,
            },
        )
        ledger.payload, ledger.revision, ledger.updated_at = payload, ledger.revision + 1, stamp
        claim.state, claim.active_key, claim.updated_at = "abandoned_unknown", None, stamp
        details = {
            "contract": CONTRACT,
            "request": request,
            "record_id": record["id"],
            "claim_before": before_claim,
            "record_before": before_record,
            "claim_after_hash": fingerprint(_view(claim)),
            "record_after_hash": fingerprint(updated),
            "evidence": seal,
            "model_calls": 0,
            "external_operations": 0,
        }
        audit = Audit(id=audit_id, actor=actor, action=ACTION, entity=claim.id, details=details)
        session.add(audit)
        session.flush()
        return _receipt(session, audit)
