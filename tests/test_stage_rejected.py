"""Recovering rejected evidence stages once and preserves every publication boundary."""

import copy
import hashlib
import json

import pytest
from sqlalchemy import select

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Draft, Material, Story
from max_postiz_agent.hub_models import (
    BridgePost,
    Dispatch,
    RemoteDraft,
    Target,
    TargetApproval,
    Variant,
)
from max_postiz_agent.reddit import text_hash
from test_rejected_trace import CANDIDATE, REVIEW, encode, fixture as trace_fixture

pytest_plugins = ["test_hourly_human_review"]
REASON = "Owner requested that the retained refusal be reviewed by a human; no new inference."


@pytest.fixture
def recovery(human_cycle):
    cycle, core, standard, records, calls, stamp, policy = human_cycle
    record, article, events = trace_fixture()
    article.update(url=standard["url"], published_at=stamp[0] - 86400)
    claimed = stamp[0] - 7200
    shift = claimed - 800
    record.update(url=article["url"], input_hash=fingerprint(article),
                  source_published_at=article["published_at"], original_hash=text_hash(article["original"]),
                  policy_hash=fingerprint(policy), target_id=policy["target_id"],
                  source_id=policy["source_id"], slot=int(claimed // 3600),
                  claimed_at=claimed, cancelled_at=claimed + 400,
                  error="ARTICLE_QUALITY_REJECTED", preparation_actions=[{
                      "action": "operator_cancelled", "at": claimed + 400,
                      "reason": "Previous refusal retained privately before the new owner instruction."}])
    attempt = record["preparation_attempts"][-1]
    attempt["started_at"] += shift
    attempt["finished_at"] += shift
    for event in events:
        event["at"] += shift
    events[0]["source_untrusted"]["url"] = article["url"]
    full = CANDIDATE["title"] + "\n\n" + CANDIDATE["body"] + "\n\nИсточник: " + article["url"]
    full += "\nПеревод и редактура с помощью нейросетей."
    next(e for e in events if e["phase"] == "gate_passed")["characters"] = len(full)
    record["quality_trace"] = f"data/hourly-articles/quality-{record['id']}.local.jsonl"
    raw = encode(events)
    trace_path = core.settings.root / record["quality_trace"]
    trace_path.write_bytes(raw)
    cycle._save({"records": [record]})
    cycle.quality_factory = lambda: pytest.fail("No LLM during historical recovery")
    with core.db.session.begin() as session:
        for target in session.scalars(select(Target)):
            target.paused = True
    return {"cycle": cycle, "core": core, "record": copy.deepcopy(record), "article": article,
            "records": records, "calls": calls, "stamp": stamp, "policy": policy,
            "raw": raw, "trace_path": trace_path, "events": events}


def stage(value, **kwargs):
    return value["cycle"].stage_rejected(value["record"]["id"], value["article"],
                                         restore_cancelled=True, reason=REASON, **kwargs)


def counts(value):
    with value["core"].db.session() as session:
        return {model.__name__: session.query(model).count()
                for model in (Story, Material, Draft, Variant, RemoteDraft, TargetApproval, Dispatch)}


def assert_blocked(value, code):
    before = counts(value)
    ledger = copy.deepcopy(value["cycle"]._load())
    with pytest.raises(AgentError, match=code):
        stage(value)
    assert counts(value) == before and value["cycle"]._load() == ledger
    assert value["calls"] == [] and value["records"] == {}


def other_variant(value, metadata=None):
    core = value["core"]
    with core.db.session.begin() as session:
        story = Story(title="Another editorial record", text="A distinct text.",
                      text_hash=fingerprint({"fixture": "other-record"}))
        session.add(story)
        session.flush()
        draft = Draft(channel_id="main", story_id=story.id, current={},
                      config_id=core.settings.hash, state="review_pending")
        session.add(draft)
        session.flush()
        target = session.get(Target, "max")
        variant = Variant(draft_id=draft.id, target_id=target.id, title="Другой материал",
            body="Другой редакционный материал.", source_revision=1, config_hash=core.settings.hash,
            binding_hash=target.binding_hash, metadata_json=metadata or {})
        session.add(variant)
        session.flush()
        return variant.id, story.id


def test_restores_one_same_historical_record_as_rejected_draft_without_consent(recovery):
    value = recovery
    before = copy.deepcopy(value["record"])
    result = stage(value)
    assert result["id"] == before["id"] and result["state"] == "awaiting_human_review"
    assert len(value["calls"]) == len(value["records"]) == 1
    assert value["calls"][0][3:] == ("draft", None)
    assert value["records"][result["postiz_id"]]["state"] == "DRAFT"
    saved = value["cycle"]._load()["records"][0]
    for field in ("slot", "claimed_at", "cancelled_at", "preparation_attempts", "input_hash",
                  "original_hash", "policy_hash", "source_published_at"):
        assert saved[field] == before[field]
    assert saved["preparation_actions"][:-1] == before["preparation_actions"]
    recovery_action = saved["preparation_actions"][-1]
    assert recovery_action["previous_state"] == "cancelled"
    assert recovery_action["previous_error"] == "ARTICLE_QUALITY_REJECTED"
    assert recovery_action["reason"] == REASON and recovery_action["no_new_inference"] is True
    assert recovery_action["state"] == "staged_for_human"
    assert value["trace_path"].read_bytes() == value["raw"]
    with value["core"].db.session() as session:
        variant = session.get(Variant, result["variant_id"])
        assert variant.metadata_json["machine_review_status"] == "rejected"
        evidence = variant.metadata_json["machine_review_evidence"]
        assert evidence["review"] == REVIEW
        assert evidence["trace_hash"] == hashlib.sha256(value["raw"]).hexdigest()
        assert evidence["segment_hash"] == fingerprint(value["events"])
        assert evidence["text_hash"] == fingerprint({"title": variant.title, "body": variant.body})
        assert variant.body == value["records"][result["postiz_id"]]["content"]
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
        assert all(target.paused for target in session.scalars(select(Target)))
        assert session.scalar(select(Audit).where(Audit.action == "hourly_article_rejected_recovery_started"))
    details = value["core"].hourly_review_details(result["variant_id"])
    assert details["machine_review_current"] is True
    assert details["machine_review_status"] == "rejected"
    assert details["machine_exception_eligible"] is True
    assert details["machine_review_issues"] == REVIEW["issues"]
    assert details["human_review_required"] is True


def test_successful_recovery_cannot_stage_a_second_draft(recovery):
    result = stage(recovery)
    before = counts(recovery)
    with pytest.raises(AgentError, match="ARTICLE_REJECTED_RECOVERY_NOT_SAFE"):
        stage(recovery)
    assert counts(recovery) == before and len(recovery["calls"]) == 1
    assert set(recovery["records"]) == {result["postiz_id"]}


def test_unknown_remote_creation_keeps_durable_tracking_and_never_repeats(recovery, monkeypatch):
    original = recovery["core"].postiz.create

    def unknown(*args, **kwargs):
        original(*args, **kwargs)
        raise AgentError("POSTIZ_UNKNOWN", 503)

    monkeypatch.setattr(recovery["core"].postiz, "create", unknown)
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        stage(recovery)
    with recovery["core"].db.session() as session:
        assert session.query(Variant).count() == session.query(RemoteDraft).count() == 1
        assert session.scalar(select(RemoteDraft)).state == "unknown"
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
    with pytest.raises(AgentError, match="ARTICLE_PREPARATION_HAS_EXTERNAL_STATE"):
        stage(recovery)
    assert len(recovery["calls"]) == len(recovery["records"]) == 1


@pytest.mark.parametrize("restore", [False, None, 1, "true"])
def test_cancelled_record_needs_explicit_restore_true(recovery, restore):
    with pytest.raises(AgentError, match="ARTICLE_REJECTED_RECOVERY_NOT_SAFE"):
        recovery["cycle"].stage_rejected(recovery["record"]["id"], recovery["article"],
                                         restore_cancelled=restore, reason=REASON)
    assert not recovery["calls"] and counts(recovery)["Variant"] == 0


@pytest.mark.parametrize("reason", ["", "too short", "x" * 501, None])
def test_recovery_requires_an_auditable_reason(recovery, reason):
    with pytest.raises(AgentError, match="ARTICLE_RECOVERY_REASON_REQUIRED"):
        recovery["cycle"].stage_rejected(recovery["record"]["id"], recovery["article"],
                                         restore_cancelled=True, reason=reason)
    assert not recovery["calls"] and counts(recovery)["Variant"] == 0


@pytest.mark.parametrize("key,value", [("enabled", False), ("approval_mode", "legacy")])
def test_disabled_or_legacy_policy_cannot_stage_a_rejected_article(recovery, key, value):
    policy = {**recovery["policy"], key: value}
    (recovery["core"].settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    code = "ARTICLE_POLICY_DISABLED" if key == "enabled" else "ARTICLE_HUMAN_REVIEW_NOT_ELIGIBLE"
    assert_blocked(recovery, code)


@pytest.mark.parametrize("field", ["input_hash", "original_hash", "policy_hash", "target_id", "source_id"])
def test_recovery_rejects_mismatched_policy_and_source_bindings(recovery, field):
    ledger = recovery["cycle"]._load()
    ledger["records"][0][field] = "different"
    recovery["cycle"]._save(ledger)
    assert_blocked(recovery, "ARTICLE_RETRY_INPUT_MISMATCH")


def test_modified_article_cannot_be_recovered_from_previous_evidence(recovery):
    recovery["article"]["original"] += " A new claim."
    assert_blocked(recovery, "ARTICLE_RETRY_INPUT_MISMATCH")


def test_current_source_rights_must_still_allow_derivatives(recovery):
    recovery["core"].settings.source(recovery["policy"]["source_id"])["rights"]["allow_derivative"] = False
    assert_blocked(recovery, "ARTICLE_RIGHTS_REQUIRED")


def test_existing_record_variant_binding_blocks_recovery_before_any_remote_request(recovery):
    ledger = recovery["cycle"]._load()
    ledger["records"][0]["variant_id"] = "existing-or-uncertain-variant"
    recovery["cycle"]._save(ledger)
    assert_blocked(recovery, "ARTICLE_PREPARATION_HAS_EXTERNAL_STATE")


def test_canonical_trace_path_is_required(recovery):
    ledger = recovery["cycle"]._load()
    ledger["records"][0]["quality_trace"] = "data/human-review/export.local.json"
    recovery["cycle"]._save(ledger)
    assert_blocked(recovery, "ARTICLE_MACHINE_EVIDENCE_INVALID")


def test_corrupted_trace_cannot_create_local_or_remote_drafts(recovery):
    recovery["trace_path"].write_bytes(b"not canonical evidence")
    assert_blocked(recovery, "ARTICLE_REJECTED_EVIDENCE_INVALID")


def test_backlog_limit_keeps_cancelled_record_and_slot_intact(recovery):
    ledger = recovery["cycle"]._load()
    ledger["records"].extend({"id": f"waiting-{i}", "slot": recovery["record"]["slot"] - i - 1,
                               "state": "awaiting_human_review"} for i in range(24))
    recovery["cycle"]._save(ledger)
    assert_blocked(recovery, "ARTICLE_HUMAN_REVIEW_BACKLOG")


@pytest.mark.parametrize("duplicate", ["url", "original_hash"])
def test_cancelled_and_used_source_cannot_be_recovered_as_another_record(recovery, duplicate):
    ledger = recovery["cycle"]._load()
    ledger["records"].append({"id": "already-used", "slot": recovery["record"]["slot"] - 1,
        "state": "cancelled", duplicate: recovery["record"][duplicate]})
    recovery["cycle"]._save(ledger)
    assert_blocked(recovery, "ARTICLE_DUPLICATE_SOURCE")


def test_mutation_lease_on_another_post_blocks_recovery(recovery):
    ident, _ = other_variant(recovery)
    with recovery["core"].db.session.begin() as session:
        session.add(BridgePost(post_id="other-post", integration_id="integration-max", group_id="other-group",
            target_id="max", variant_id=ident, remote_hash="0" * 64, remote_state="DRAFT",
            due_at=recovery["stamp"][0], mutation_owner="inflight-editor", mutation_until=0))
    assert_blocked(recovery, "ARTICLE_OUTSTANDING_DELIVERY")


@pytest.mark.parametrize("state", ["pending", "sending", "unknown", "scheduled"])
def test_outstanding_delivery_blocks_recovery_without_retrying_or_resolving_it(recovery, state):
    ident, _ = other_variant(recovery)
    with recovery["core"].db.session.begin() as session:
        approval = TargetApproval(variant_id=ident, revision=1, snapshot_hash="0" * 64,
                                  scheduled_at=recovery["stamp"][0], actor="historical-fixture")
        session.add(approval)
        session.flush()
        session.add(Dispatch(variant_id=ident, approval_id=approval.id, revision=1,
            snapshot_hash="0" * 64, snapshot={}, state=state, due_at=recovery["stamp"][0]))
    assert_blocked(recovery, "ARTICLE_OUTSTANDING_DELIVERY")


def test_material_duplicate_blocks_recovery_even_when_other_ledger_is_missing(recovery):
    _, story_id = other_variant(recovery)
    with recovery["core"].db.session.begin() as session:
        session.add(Material(source_id=recovery["policy"]["source_id"], external_id="existing-source",
            story_id=story_id, title=recovery["article"]["title"], text=recovery["article"]["original"],
            url=recovery["article"]["url"], text_hash="0" * 64, language="en"))
    assert_blocked(recovery, "ARTICLE_DUPLICATE_SOURCE")


def test_duplicate_translation_prevents_a_new_draft_and_retains_negative_history(recovery):
    translation = CANDIDATE["title"] + "\n\n" + CANDIDATE["body"]
    other_variant(recovery, {"translation_hash": text_hash(translation)})
    before = counts(recovery)
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_TRANSLATION"):
        stage(recovery)
    assert counts(recovery) == before and not recovery["calls"]
    saved = recovery["cycle"]._load()["records"][0]
    assert saved["state"] == "needs_attention"
    assert saved["preparation_attempts"] == recovery["record"]["preparation_attempts"]
    assert saved["cancelled_at"] == recovery["record"]["cancelled_at"]


def test_unreconciled_other_preparation_blocks_recovery(recovery):
    ledger = recovery["cycle"]._load()
    ledger["records"].append({"id": "unfinished", "slot": recovery["record"]["slot"] - 1,
                               "state": "preparing"})
    recovery["cycle"]._save(ledger)
    assert_blocked(recovery, "ARTICLE_RECONCILIATION_REQUIRED")
