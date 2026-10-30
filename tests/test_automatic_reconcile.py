"""One known completed Postiz mutation can receive an exact policy approval once."""
import copy

import pytest
from sqlalchemy import select

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Draft
from max_postiz_agent.hub_models import BridgePost, Dispatch, RemoteDraft, Target, TargetApproval, Variant

pytest_plugins = ["test_automatic_policy"]
REASON = "The owner reconciled the completed Postiz mutation after fixing automatic scheduling limits."


@pytest.fixture
def queued_failure(automatic_cycle, monkeypatch):
    value, _ = automatic_cycle
    cycle, core, article, records, calls, stamp, policy = value
    core.settings.channel("main")["publication"].update(min_gap_minutes=120, max_posts_per_day=16)
    prepared = cycle.prepare(article)
    approve = core.approve_hourly_policy
    monkeypatch.setattr(core, "approve_hourly_policy", lambda *args: (_ for _ in ()).throw(AgentError("SCHEDULE_GAP_TOO_SHORT", 409)))
    with pytest.raises(AgentError, match="SCHEDULE_GAP_TOO_SHORT"):
        cycle.publish(prepared["id"], prepared["content_hash"])
    monkeypatch.setattr(core, "approve_hourly_policy", approve)
    failed = cycle.status(prepared["id"])
    assert failed["state"] == "needs_attention" and records[failed["postiz_id"]]["state"] == "QUEUE"
    monkeypatch.setattr(core.postiz, "request", lambda *args, **kwargs: pytest.fail("Recovery must not repeat a remote mutation"))
    monkeypatch.setattr(cycle, "_quality", lambda *args, **kwargs: pytest.fail("Recovery must not resample a model"))
    return value, failed


def recover(value, record):
    cycle = value[0]
    return cycle.reconcile_approval(record["id"], record["content_hash"], record["due_at"], REASON)


def test_known_queue_recovery_uses_one_policy_approval_without_post_or_model(queued_failure):
    value, failed = queued_failure
    cycle, core, _, records, calls, _, _ = value
    result = recover(value, failed)
    assert result["state"] == "pending" and result["due_at"] == failed["due_at"]
    assert result["postiz_id"] == failed["postiz_id"] and result["revision"] == failed["revision"]
    assert result["content_hash"] == failed["content_hash"] and len(records) == len(calls) == 1
    assert result["approval_reconciliations"][0]["original_error"] == "SCHEDULE_GAP_TOO_SHORT"
    with core.db.session() as session:
        approval = session.query(TargetApproval).one()
        assert approval.actor == "hourly_article_policy" and session.query(Dispatch).count() == 1
        assert session.query(Audit).filter_by(action="hourly_article_approval_reconcile_started").count() == 1
        assert session.query(Audit).filter_by(action="hourly_article_approval_reconcile_completed").count() == 1
        assert not session.query(Audit).filter_by(action="hourly_article_human_review").count()
    with pytest.raises(AgentError, match="ARTICLE_APPROVAL_RECONCILE_(NOT_SAFE|ALREADY_ATTEMPTED)"):
        recover(value, failed)


@pytest.mark.parametrize("error", ["POSTIZ_UNKNOWN", "ARTICLE_APPROVAL_INTERRUPTED", "BRIDGE_EDIT_IN_PROGRESS"])
def test_ambiguous_outcome_is_never_relabelled_as_known_approval_failure(queued_failure, error):
    value, failed = queued_failure
    ledger = value[0]._load()
    ledger["records"][0]["error"] = error
    value[0]._save(ledger)
    with pytest.raises(AgentError, match="ARTICLE_APPROVAL_RECONCILE_NOT_SAFE"):
        recover(value, failed)


@pytest.mark.parametrize("change", ["hash", "date", "near_date", "past", "body", "state", "integration", "evidence", "source", "prompt", "trace", "remote_unknown"])
def test_recovery_rejects_stale_exact_proof_before_claim(queued_failure, monkeypatch, change):
    value, failed = queued_failure
    cycle, core, _, records, _, stamp, _ = value
    checked = copy.deepcopy(failed)
    if change == "hash":
        checked["content_hash"] = "0" * 64
    elif change == "date":
        checked["due_at"] += 1
    elif change == "near_date":
        checked["due_at"] += 0.0005
    elif change == "past":
        stamp[0] = failed["due_at"] + 1
    elif change in {"body", "state"}:
        records[failed["postiz_id"]]["content" if change == "body" else "state"] = "Changed remote text" if change == "body" else "PUBLISHED"
    elif change == "integration":
        monkeypatch.setattr(core.postiz, "integrations", lambda: [{"id": "integration-max", "identifier": "max-browser",
            "accountId": "wrong-account", "disabled": False, "refreshNeeded": False}])
    elif change == "prompt":
        prompt = core.settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt"
        prompt.write_text(prompt.read_text(encoding="utf-8") + "\nNew instruction.", encoding="utf-8")
    elif change == "trace":
        (core.settings.root / failed["quality_trace"]).write_text("Replaced trace", encoding="utf-8")
    else:
        with core.db.session.begin() as session:
            variant = session.get(Variant, failed["variant_id"])
            if change == "remote_unknown":
                session.get(RemoteDraft, variant.id).state = "unknown"
            elif change == "evidence":
                variant.metadata_json = {**variant.metadata_json, "machine_review_evidence_hash": "0" * 64}
            else:
                variant.metadata_json = {**variant.metadata_json, "machine_source_hash": "0" * 64}
    with pytest.raises(AgentError):
        recover(value, checked)
    with core.db.session() as session:
        assert not session.query(TargetApproval).count() and not session.query(Dispatch).count()
        assert not session.query(Audit).filter_by(action="hourly_article_approval_reconcile_started").count()


@pytest.mark.parametrize("block", ["approval", "dispatch", "audit", "lease"])
def test_existing_delivery_history_or_lease_blocks_recovery(queued_failure, block):
    value, failed = queued_failure
    core = value[1]
    with core.db.session.begin() as session:
        if block in {"approval", "dispatch"}:
            approval = TargetApproval(variant_id=failed["variant_id"], revision=failed["revision"],
                snapshot_hash="0" * 64, scheduled_at=failed["due_at"], actor="editor", revoked=True)
            session.add(approval)
            session.flush()
            if block == "dispatch":
                session.add(Dispatch(variant_id=failed["variant_id"], approval_id=approval.id, revision=failed["revision"],
                    snapshot_hash="0" * 64, snapshot={}, due_at=failed["due_at"], state="cancelled"))
        elif block == "audit":
            session.add(Audit(actor="editor", action="target_approve", entity=failed["variant_id"]))
        else:
            session.scalar(select(BridgePost)).mutation_owner = "existing-edit"
    with pytest.raises(AgentError, match="ARTICLE_APPROVAL_RECONCILE_NOT_SAFE"):
        recover(value, failed)


def test_nan_metadata_due_is_not_equal_to_the_confirmed_finite_remote_date(queued_failure):
    value, failed = queued_failure
    cycle, core, _, _, _, _, _ = value
    with core.db.session.begin() as session:
        variant = session.get(Variant, failed["variant_id"])
        variant.metadata_json = {**variant.metadata_json, "postiz_due_at": float("nan")}
        checked_hash = fingerprint(core.snapshot(variant, session.get(Target, "max")))
    ledger = cycle._load()
    ledger["records"][0]["prepared_hash"] = checked_hash
    cycle._save(ledger)
    with pytest.raises(AgentError, match="ARTICLE_REMOTE_CHANGED"):
        recover(value, {**failed, "content_hash": checked_hash})


def test_orphan_expired_mutation_until_is_not_permission_to_reconcile(queued_failure):
    value, failed = queued_failure
    core, stamp = value[1], value[5]
    with core.db.session.begin() as session:
        link = session.scalar(select(BridgePost))
        link.mutation_owner, link.mutation_until = None, stamp[0] - 3600
    with pytest.raises(AgentError, match="ARTICLE_APPROVAL_RECONCILE_NOT_SAFE"):
        recover(value, failed)


def test_post_claim_failure_cannot_be_retried_even_if_ledger_claim_is_lost(queued_failure, monkeypatch):
    value, failed = queued_failure
    cycle, core, _, _, _, _, _ = value
    attempts = []

    def failure(*args):
        attempts.append(True)
        raise AgentError("SCHEDULE_GAP_TOO_SHORT", 409)

    monkeypatch.setattr(core, "approve_hourly_policy", failure)
    with pytest.raises(AgentError, match="SCHEDULE_GAP_TOO_SHORT"):
        recover(value, failed)
    ledger = cycle._load()
    ledger["records"][0].pop("approval_reconciliations")
    cycle._save(ledger)
    with pytest.raises(AgentError, match="ARTICLE_APPROVAL_RECONCILE_ALREADY_ATTEMPTED"):
        recover(value, failed)
    assert attempts == [True]


def test_failure_after_core_commit_preserves_approval_but_pauses_and_never_repeats(queued_failure, monkeypatch):
    value, failed = queued_failure
    cycle, core, _, _, _, _, _ = value
    approve = core.approve_hourly_policy

    def lost_completion(*args):
        approve(*args)
        raise AgentError("ARTICLE_APPROVAL_INTERRUPTED", 503)

    monkeypatch.setattr(core, "approve_hourly_policy", lost_completion)
    with pytest.raises(AgentError, match="ARTICLE_APPROVAL_INTERRUPTED"):
        recover(value, failed)
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 1
        assert session.get(Target, "max").paused
    with pytest.raises(AgentError, match="ARTICLE_APPROVAL_RECONCILE_NOT_SAFE"):
        recover(value, failed)


def test_final_approval_lock_rejects_cancelled_history_created_after_reconcile_claim(queued_failure, monkeypatch):
    value, failed = queued_failure
    _, core, _, _, _, _, _ = value
    approve = core.approve_hourly_policy

    def interleaved_history(*args):
        with core.db.session.begin() as session:
            session.add(TargetApproval(variant_id=failed["variant_id"], revision=failed["revision"],
                snapshot_hash="0" * 64, scheduled_at=failed["due_at"], actor="editor", revoked=True))
        return approve(*args)

    monkeypatch.setattr(core, "approve_hourly_policy", interleaved_history)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_READY"):
        recover(value, failed)
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == 1 and not session.query(Dispatch).count()
        assert session.get(Target, "max").paused


def test_publish_schedule_preflight_fails_before_any_postiz_mutation(automatic_cycle, monkeypatch):
    value, _ = automatic_cycle
    cycle, core, article, records, _, _, _ = value
    prepared = cycle.prepare(article)
    monkeypatch.setattr(core.postiz, "request", lambda *args, **kwargs: pytest.fail("Invalid plan must not schedule"))
    monkeypatch.setattr(core, "_check_schedule_limits", lambda *args, **kwargs: (_ for _ in ()).throw(AgentError("SCHEDULE_GAP_TOO_SHORT", 409)))
    with pytest.raises(AgentError, match="SCHEDULE_GAP_TOO_SHORT"):
        cycle.publish(prepared["id"], prepared["content_hash"])
    assert records[prepared["postiz_id"]]["state"] == "DRAFT"
    assert cycle.status(prepared["id"])["state"] == "automatic_ready"


def test_default_status_finalizes_older_completed_delivery_before_next_preparation(automatic_cycle, monkeypatch):
    value, _ = automatic_cycle
    cycle, core, article, records, _, stamp, _ = value
    quality_factory = cycle._quality
    core.publisher.mode = "browser"
    prepared = cycle.prepare(article)
    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    with core.db.session.begin() as session:
        first = session.get(Variant, prepared["variant_id"])
        draft = Draft(channel_id="main", story_id="latest-other", current={}, config_id=first.config_hash)
        session.add(draft)
        session.flush()
        latest = Variant(draft_id=draft.id, target_id="max", title="Latest completed article", body="Other completed text",
            sources=[], media=[], metadata_json={}, source_revision=1, config_hash=first.config_hash, binding_hash=first.binding_hash)
        session.add(latest)
        session.flush()
        latest_id = latest.id
    ledger = cycle._load()
    ledger["records"][0].update(slot=prepared["slot"] - 2, automatic_enrolled=True)
    ledger["records"].append({"id": "latest-completed", "slot": prepared["slot"] - 1,
        "state": "published", "target_id": "max", "variant_id": latest_id})
    cycle._save(ledger)
    stamp[0] += 121
    assert core.dispatch_tick() == 1
    records[planned["postiz_id"]]["state"] = "PUBLISHED"
    finished = cycle.status()
    assert finished["id"] == prepared["id"] and finished["state"] == "published"
    stamp[0] += 3601

    def second_quality(*args, **kwargs):
        result = quality_factory(*args, **kwargs)
        result.translator.value = {"title": "Новый звёздный ветер", "body":
            "Исследователи обнаружили необычный звёздный ветер и изучили свойства далёкой звезды."}
        return result

    monkeypatch.setattr(cycle, "_quality", second_quality)
    second = cycle.prepare({**article, "url": "https://science.example.org/new-article/",
                            "original": article["original"] + " The next scientific observation was different."})
    assert second["state"] == "automatic_ready" and second["id"] != prepared["id"]
