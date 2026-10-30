"""A human may resolve a valid machine refusal only for one exact approval."""
import copy
import json

import pytest
from sqlalchemy import select

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval, Variant
from max_postiz_agent.human_review import source_data, text_hash
from test_hourly_human_review import queued

pytest_plugins = ["test_hourly_human_review"]

NOTES = ("Я сравнил каждое замечание с английским оригиналом: дата указана в тексте, "
         "а выбранный русский термин сохраняет значение исходной фразы.")


@pytest.fixture
def exception_cycle(human_cycle, monkeypatch):
    value, core, article, records, calls, stamp, policy = human_cycle
    prepared = value.prepare(article)
    proof = {"valid": True, "evidence_hash": "e" * 64,
             "issues": ["The date is allegedly missing."], "reviewed": {"approved": False}}
    with core.db.session.begin() as session:
        variant = session.get(Variant, prepared["variant_id"])
        variant.metadata_json = {**variant.metadata_json, "machine_review_status": "rejected"}
        session.get(Target, variant.target_id).paused = True

    def evidence(session, variant):
        # The evidence validator has its own tests. Here its strict contract is
        # supplied explicitly so these tests isolate approval/audit/send gates.
        metadata = variant.metadata_json
        if (not proof["valid"] or metadata.get("machine_review_status") != "rejected"
                or metadata.get("machine_text_hash") != text_hash(variant.title, variant.body)
                or metadata.get("machine_source_hash") != fingerprint(source_data(session, variant))):
            return None
        return {key: copy.deepcopy(value) for key, value in proof.items() if key != "valid"}

    monkeypatch.setattr("max_postiz_agent.human_review.rejected_evidence", evidence, raising=False)
    return human_cycle, prepared, proof


def approve_exception(fixture, *, confirmed=True, notes=NOTES, actor="owner", human_confirmed=True):
    cycle, prepared, _ = fixture
    _, core, _, _, _, _, _ = cycle
    revision, hashed, due = queued(cycle, prepared)
    return core.approve_variant(prepared["variant_id"], revision, hashed, due, actor,
                                human_confirmed=human_confirmed,
                                machine_exception_confirmed=confirmed, machine_review_notes=notes)


@pytest.mark.parametrize("confirmed", [False, None, 1, "true"])
def test_machine_refusal_requires_literal_extra_confirmation(exception_cycle, confirmed):
    cycle, prepared, _ = exception_cycle
    _, core, _, _, _, _, _ = cycle
    with pytest.raises(AgentError, match="ARTICLE_MACHINE_EXCEPTION_CONFIRMATION_REQUIRED"):
        approve_exception(exception_cycle, confirmed=confirmed)
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
        assert session.get(Target, "max").paused
        assert session.get(Variant, prepared["variant_id"]).metadata_json["machine_review_status"] == "rejected"


@pytest.mark.parametrize("notes", ["", "Согласен", "да " * 30, "a" * 80, None, "x " * 2100])
def test_machine_refusal_requires_a_substantive_bounded_human_explanation(exception_cycle, notes):
    cycle, _, _ = exception_cycle
    _, core, _, _, _, _, _ = cycle
    with pytest.raises(AgentError, match="ARTICLE_MACHINE_EXCEPTION_NOTES_REQUIRED"):
        approve_exception(exception_cycle, notes=notes)
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
        assert session.get(Target, "max").paused


@pytest.mark.parametrize("actor,human_confirmed", [("owner", False), ("editor", False),
                                                 ("root", True), ("hourly_article_policy", True)])
def test_exception_never_substitutes_for_human_role_and_primary_consent(
        exception_cycle, monkeypatch, actor, human_confirmed):
    cycle, prepared, _ = exception_cycle
    _, core, _, _, _, _, _ = cycle
    revision, hashed, due = queued(cycle, prepared)
    monkeypatch.setattr(core.bridge, "sync", lambda **kwargs: pytest.fail("Authorization must precede synchronization"))
    with pytest.raises(AgentError, match="HUMAN_REVIEW_CONFIRMATION_REQUIRED"):
        core.approve_variant(prepared["variant_id"], revision, hashed, due, actor,
                             human_confirmed=human_confirmed, machine_exception_confirmed=True,
                             machine_review_notes=NOTES)


@pytest.mark.parametrize("actor", ["owner", "editor"])
def test_valid_exception_is_audited_for_exact_approval_and_remains_a_machine_refusal(exception_cycle, actor):
    cycle, prepared, proof = exception_cycle
    _, core, _, _, _, _, _ = cycle
    ident = approve_exception(exception_cycle, actor=actor, notes="  " + NOTES + "  ")
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        approval = session.get(TargetApproval, dispatch.approval_id)
        variant = session.get(Variant, prepared["variant_id"])
        audit = session.scalar(select(Audit).where(Audit.entity == approval.id,
                                                   Audit.action == "hourly_article_machine_exception"))
        assert approval.actor == audit.actor == "human_editor:" + actor
        assert audit.details == {
            "approval_id": approval.id, "variant_id": variant.id, "revision": variant.revision,
            "content_hash": fingerprint(core.snapshot(variant, session.get(Target, "max"))),
            "snapshot_hash": dispatch.snapshot_hash, "due_at": dispatch.due_at,
            "target_id": "max", "destination": "-123", "evidence_hash": proof["evidence_hash"],
            "machine_exception_confirmed": True, "notes": NOTES, "notes_hash": fingerprint(NOTES),
        }
        assert variant.metadata_json["machine_review_status"] == "rejected"
        assert not session.get(Target, "max").paused


def test_valid_human_exception_can_send_once_through_the_restricted_worker(exception_cycle):
    cycle, _, _ = exception_cycle
    _, core, _, _, _, stamp, _ = cycle
    core.publisher.mode = "browser"
    ident = approve_exception(exception_cycle)
    stamp[0] += 181
    assert core.dispatch_tick() == 1
    assert core.dispatch_tick() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        assert dispatch.state == "published" and dispatch.verified and dispatch.attempts == 1
        assert session.get(Target, "max").paused


def test_extra_confirmation_cannot_excuse_invalid_hard_gates_or_missing_evidence(exception_cycle):
    cycle, _, proof = exception_cycle
    _, core, _, _, _, _, _ = cycle
    proof["valid"] = False
    with pytest.raises(AgentError, match="ARTICLE_MACHINE_RECHECK_REQUIRED"):
        approve_exception(exception_cycle)
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0


@pytest.mark.parametrize("change", ["missing", "duplicate", "actor", "approval_id", "variant_id", "revision",
                                   "content_hash", "snapshot_hash", "due_at", "target_id", "destination",
                                   "evidence_hash", "machine_exception_confirmed", "notes", "notes_hash"])
def test_send_requires_the_same_complete_exception_audit(exception_cycle, monkeypatch, change):
    cycle, _, _ = exception_cycle
    _, core, _, _, _, stamp, _ = cycle
    ident = approve_exception(exception_cycle)
    with core.db.session.begin() as session:
        dispatch = session.get(Dispatch, ident)
        audit = session.scalar(select(Audit).where(Audit.entity == dispatch.approval_id,
                                                   Audit.action == "hourly_article_machine_exception"))
        if change == "missing":
            session.delete(audit)
        elif change == "duplicate":
            session.add(Audit(actor=audit.actor, action=audit.action, entity=audit.entity, details=audit.details))
        elif change == "actor":
            audit.actor = "hourly_article_policy"
        else:
            changed = {"revision": 999, "due_at": audit.details["due_at"] + 1,
                       "machine_exception_confirmed": False, "notes": "ОК"}.get(change, "different-value")
            audit.details = {**audit.details, change: changed}
    monkeypatch.setattr(core.publisher, "publish", lambda *args, **kwargs: pytest.fail("Invalid exception must not send"))
    stamp[0] += 181
    assert core.publish_due() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        assert dispatch.state == "cancelled" and dispatch.error == "ARTICLE_MACHINE_EXCEPTION_NOT_APPROVED"
        assert dispatch.attempts == 0


@pytest.mark.parametrize("change", ["evidence_changed", "evidence_invalid"])
def test_send_revalidates_independent_evidence_after_human_approval(exception_cycle, monkeypatch, change):
    cycle, _, proof = exception_cycle
    _, core, _, _, _, stamp, _ = cycle
    ident = approve_exception(exception_cycle)
    if change == "evidence_changed":
        proof["evidence_hash"] = "f" * 64
    else:
        proof["valid"] = False
    monkeypatch.setattr(core.publisher, "publish", lambda *args, **kwargs: pytest.fail("Changed evidence must not send"))
    stamp[0] += 181
    assert core.publish_due() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        expected = "ARTICLE_MACHINE_EXCEPTION_NOT_APPROVED" if change == "evidence_changed" else "ARTICLE_MACHINE_RECHECK_REQUIRED"
        assert dispatch.state == "cancelled" and dispatch.error == expected


def test_passed_review_keeps_its_default_contract_without_an_exception(human_cycle, monkeypatch):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    monkeypatch.setattr("max_postiz_agent.human_review.rejected_evidence",
                        lambda *args: pytest.fail("Passed proof must not use rejected evidence"), raising=False)
    revision, hashed, due = queued(human_cycle, prepared)
    core.approve_variant(prepared["variant_id"], revision, hashed, due, "owner", human_confirmed=True)
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == 1
        assert not session.scalar(select(Audit).where(Audit.action == "hourly_article_machine_exception"))


@pytest.mark.parametrize("confirmed,notes", [(True, NOTES), (False, NOTES), (True, "")])
def test_passed_review_does_not_accept_an_unneeded_exception(human_cycle, confirmed, notes):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    revision, hashed, due = queued(human_cycle, prepared)
    with pytest.raises(AgentError, match="ARTICLE_MACHINE_EXCEPTION_NOT_ALLOWED"):
        core.approve_variant(prepared["variant_id"], revision, hashed, due, "owner", human_confirmed=True,
                             machine_exception_confirmed=confirmed, machine_review_notes=notes)


def install_rejected_evidence(core, prepared, change=None):
    with core.db.session.begin() as session:
        variant = session.get(Variant, prepared["variant_id"])
        review = {"approved": False, "topic": "science", "issues": ["The date is allegedly omitted."]}
        evidence = {"review_raw": json.dumps(review), "review": review,
                    "spelling_matches": [], "spelling_adjudicated": [], "spelling_unresolved": [],
                    "hard_gates_passed": True, "text_hash": text_hash(variant.title, variant.body),
                    "source_hash": fingerprint(source_data(session, variant)), "trace_hash": "a" * 64}
        if change == "hard_gate_failed":
            evidence["hard_gates_passed"] = False
        elif change == "spelling_failed":
            matches = [{"message": "Unresolved spelling finding", "offset": 0, "length": 1,
                        "rule": {"id": "MORFOLOGIK_RULE_RU_RU"}}]
            evidence["spelling_matches"] = evidence["spelling_unresolved"] = matches
        elif change == "malformed_verdict":
            evidence["review_raw"] = "not a parseable verdict"
        elif change == "missing_source_proof":
            evidence["source_hash"] = "b" * 64
        elif change == "missing_trace_proof":
            evidence.pop("trace_hash")
        variant.metadata_json = {**variant.metadata_json, "machine_review_status": "rejected",
                                 "machine_review_evidence": evidence,
                                 "machine_review_evidence_hash": fingerprint(evidence)}
        return evidence


def test_real_evidence_validator_and_gate_allow_only_the_explicit_human_exception(human_cycle):
    value, core, article, _, _, stamp, _ = human_cycle
    core.publisher.mode = "browser"
    prepared = value.prepare(article)
    evidence = install_rejected_evidence(core, prepared)
    revision, hashed, due = queued(human_cycle, prepared)
    with pytest.raises(AgentError, match="ARTICLE_MACHINE_EXCEPTION_CONFIRMATION_REQUIRED"):
        core.approve_variant(prepared["variant_id"], revision, hashed, due, "owner", human_confirmed=True)
    ident = core.approve_variant(prepared["variant_id"], revision, hashed, due, "owner", human_confirmed=True,
                                 machine_exception_confirmed=True, machine_review_notes=NOTES)
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        audit = session.scalar(select(Audit).where(Audit.entity == dispatch.approval_id,
                                                   Audit.action == "hourly_article_machine_exception"))
        assert audit.details["evidence_hash"] == fingerprint(evidence)
    stamp[0] += 181
    assert core.dispatch_tick() == 1
    assert core.dispatch_tick() == 0


@pytest.mark.parametrize("change", ["hard_gate_failed", "spelling_failed", "malformed_verdict",
                                   "missing_source_proof", "missing_trace_proof"])
def test_real_invalid_evidence_is_not_overridable_by_human_flags(human_cycle, change):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    install_rejected_evidence(core, prepared, change)
    revision, hashed, due = queued(human_cycle, prepared)
    with pytest.raises(AgentError, match="ARTICLE_MACHINE_RECHECK_REQUIRED"):
        core.approve_variant(prepared["variant_id"], revision, hashed, due, "owner", human_confirmed=True,
                             machine_exception_confirmed=True, machine_review_notes=NOTES)
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
