"""Резерв использует прежнюю QA-цепочку, но не создаёт внешних черновиков."""
import copy
import json
import os
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest
from sqlalchemy import create_engine, select, text

from max_postiz_agent.article_reserve import ArticleReserveStore
from max_postiz_agent.article_repair import ArticleRepair, REPAIR_MODEL
from max_postiz_agent.browser_runtime import profile_lock
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Database, uid
from max_postiz_agent.hub_models import Dispatch, RemoteDraft, Target, TargetApproval, Variant
from max_postiz_agent.server_state import DatabaseArticleLedger, ServerState

pytest_plugins = ["test_automatic_policy"]


@pytest.fixture
def reserve(automatic_cycle, monkeypatch):
    values, review = automatic_cycle
    cycle, core, article, records, writes, stamp, policy = values
    cycle.reserve_mode = True
    DatabaseArticleLedger(cycle).migrate()
    state = ServerState(core)
    state.initialize()
    state.control("start", state.read()["revision"], "test")
    monkeypatch.setattr("max_postiz_agent.article_reserve.now", lambda: stamp[0])
    return SimpleNamespace(cycle=cycle, core=core, article=article, records=records, writes=writes,
                           stamp=stamp, policy=policy, review=review, store=ArticleReserveStore(core))


def prepared(value):
    claim = value.store.begin_preparation()
    result = value.cycle.prepare(value.article, reserve_claim=claim)
    value.store.finish_preparation(claim, "quality_ready")
    return result


def assignment(value, result):
    record = value.cycle._record(value.cycle._load(), result["id"])
    validated = value.cycle.validate_reserve(result["id"])
    return value.store.assign(record, value.stamp[0] + 180,
        text_hash=record["reserve_prepared_hash"], evidence_hash=validated["reserve_evidence_hash"])


def test_quality_ready_keeps_exact_evidence_locally_without_postiz_or_approval(reserve):
    value = reserve
    result = prepared(value)
    assert result["state"] == "quality_ready"
    assert result["reserve_prepared_hash"] == result["content_hash"]
    assert not value.records and not value.writes
    with value.core.db.session() as session:
        variant = session.get(Variant, result["variant_id"])
        assert variant.metadata_json["machine_review_status"] == "passed"
        assert variant.metadata_json["reserve_binding_hash"] == result["reserve_binding_hash"]
        assert not session.scalar(select(RemoteDraft))
        assert not session.scalar(select(TargetApproval))
        assert not session.scalar(select(Dispatch))


def test_preparation_does_not_hold_sender_lock_and_can_cross_hour(reserve, monkeypatch):
    value = reserve
    original = value.cycle._quality

    def quality(*args, **kwargs):
        instance = original(*args, **kwargs)
        prepare = instance.prepare

        def slow(*a, **k):
            with profile_lock(value.cycle.directory / "cycle.lock"):
                result = prepare(*a, **k)
                value.stamp[0] += 3600
                return result

        instance.prepare = slow
        return instance

    monkeypatch.setattr(value.cycle, "_quality", quality)
    result = prepared(value)
    assert result["state"] == "quality_ready"
    assert result["slot"] < int(value.stamp[0] // 3600)
    assert not value.writes


def test_distinct_sources_can_fill_same_hour_but_history_still_blocks_duplicates(reserve, monkeypatch):
    value = reserve
    first = prepared(value)
    value.article = {**value.article, "url": "https://science.example.org/article/second/",
                     "original": value.article["original"] + "The study opens new possibilities."}
    original = value.cycle._quality

    def quality(*a, **k):
        instance = original(*a, **k)
        instance.translator.value = copy.deepcopy(instance.translator.value)
        instance.translator.value["body"] += " Исследование открывает новые возможности."
        return instance

    monkeypatch.setattr(value.cycle, "_quality", quality)
    second = prepared(value)
    assert first["slot"] == second["slot"] and first["id"] != second["id"]
    assert len(value.cycle._load()["records"]) == 2
    claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.cycle.prepare(value.article, reserve_claim=claim)
    assert not value.writes


def test_stage_requires_exact_assignment_and_is_never_replayed(reserve):
    value = reserve
    result = prepared(value)
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_ASSIGNMENT_LOST"):
        value.cycle.stage_reserve(result["id"])
    slot = assignment(value, result)
    staged = value.cycle.stage_reserve(result["id"])
    assert staged["state"] == "automatic_ready"
    assert staged["publication_slot_id"] == slot["id"]
    assert len(value.writes) == 1 and value.writes[0][3] == "draft"
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_NOT_READY"):
        value.cycle.stage_reserve(result["id"])
    with value.core.db.session() as session:
        assert not session.scalar(select(TargetApproval)) and not session.scalar(select(Dispatch))


@pytest.mark.parametrize("mutation", ["trace", "input", "text", "claim"])
def test_stale_or_changed_evidence_blocks_before_any_external_mutation(reserve, mutation):
    value = reserve
    result = prepared(value)
    if mutation == "trace":
        path = value.core.settings.root / result["quality_trace"]
        path.write_bytes(path.read_bytes() + b"\n")
    elif mutation == "input":
        path = value.core.settings.root / result["input_file"]
        path.write_bytes(path.read_bytes() + b" ")
    elif mutation == "text":
        with value.core.db.session.begin() as session:
            variant = session.get(Variant, result["variant_id"])
            variant.body += " Изменение."
    else:
        ledger = value.cycle._load()
        ledger["records"][0]["reserve_claim"]["token"] = "replaced"
        value.cycle._save(ledger)
    with pytest.raises(AgentError):
        value.cycle.validate_reserve(result["id"])
    assert not value.writes


def test_unknown_stage_has_one_remote_attempt_and_retains_reconciliation_state(reserve, monkeypatch):
    value = reserve
    result = prepared(value)
    assignment(value, result)
    calls = []

    def unknown(*args, **kwargs):
        calls.append(args)
        raise AgentError("POSTIZ_UNKNOWN", 503)

    monkeypatch.setattr(value.core.postiz, "create", unknown)
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        value.cycle.stage_reserve(result["id"])
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_NOT_READY"):
        value.cycle.stage_reserve(result["id"])
    saved = value.cycle._record(value.cycle._load(), result["id"])
    assert saved["state"] == "needs_attention" and saved["error"] == "POSTIZ_UNKNOWN"
    assert len(calls) == 1


def test_exact_assigned_date_is_required_before_remote_schedule(reserve):
    value = reserve
    result = prepared(value)
    slot = assignment(value, result)
    staged = value.cycle.stage_reserve(result["id"])
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_ASSIGNMENT_CHANGED"):
        value.cycle.publish(staged["id"], staged["content_hash"], due_at=slot["due_at"] + 1)
    assert len(value.writes) == 1
    with value.core.db.session() as session:
        assert not session.scalar(select(TargetApproval)) and not session.scalar(select(Dispatch))
    saved = value.cycle._record(value.cycle._load(), result["id"])
    assert fingerprint(value.cycle._snapshot(saved)) == staged["content_hash"]


def test_removing_both_reserve_markers_cannot_downgrade_durable_candidate_to_legacy(reserve):
    from max_postiz_agent.automatic_policy import eligible
    value = reserve
    result = prepared(value)
    assignment(value, result)
    staged = value.cycle.stage_reserve(result["id"])
    ledger = value.cycle._load()
    record = value.cycle._record(ledger, result["id"])
    record.pop("reserve_contract")
    value.cycle._save(ledger)
    with value.core.db.session.begin() as session:
        variant = session.get(Variant, staged["variant_id"])
        metadata = copy.deepcopy(variant.metadata_json)
        metadata.pop("reserve_contract")
        variant.metadata_json = metadata
    with value.core.db.session() as session:
        with pytest.raises(AgentError, match="ARTICLE_RESERVE_BINDING_INVALID"):
            eligible(value.core, session, session.get(Variant, staged["variant_id"]))


def test_only_intact_unassigned_expired_source_can_leave_ready_reserve(reserve):
    value = reserve
    value.article["published_at"] = value.stamp[0] - value.policy["max_source_age_hours"] * 3600 + 60
    result = prepared(value)
    trace = value.core.settings.root / result["quality_trace"]
    original_trace = trace.read_bytes()
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_INVALIDATION_NOT_SAFE"):
        value.cycle.invalidate_reserve(result["id"], "STALE_SOURCE")
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_INVALIDATION_NOT_SAFE"):
        value.cycle.invalidate_reserve(result["id"], "ARTICLE_AUTOMATIC_RECHECK_REQUIRED")
    value.stamp[0] += 120
    with pytest.raises(AgentError, match="STALE_SOURCE"):
        value.cycle.validate_reserve(result["id"])
    cancelled = value.cycle.invalidate_reserve(result["id"], "STALE_SOURCE")
    assert cancelled["state"] == "cancelled" and cancelled["error"] == "STALE_SOURCE"
    assert cancelled["prepared_hash"] == result["prepared_hash"]
    assert trace.read_bytes() == original_trace and not value.writes
    assert cancelled["preparation_attempts"] == result["preparation_attempts"]
    claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.cycle.prepare({**value.article, "published_at": value.stamp[0] - 1}, reserve_claim=claim)


@pytest.mark.parametrize("blocker", ["slot", "trace"])
def test_expiry_does_not_hide_changed_proof_or_an_assigned_slot(reserve, blocker):
    value = reserve
    value.article["published_at"] = value.stamp[0] - value.policy["max_source_age_hours"] * 3600 + 60
    result = prepared(value)
    if blocker == "slot":
        assignment(value, result)
    else:
        trace = value.core.settings.root / result["quality_trace"]
        trace.write_bytes(trace.read_bytes() + b"\n")
    value.stamp[0] += 120
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_INVALIDATION_NOT_SAFE"):
        value.cycle.invalidate_reserve(result["id"], "STALE_SOURCE")
    saved = value.cycle._record(value.cycle._load(), result["id"])
    assert saved["state"] == "quality_ready" and not value.writes


def test_source_expiring_before_exact_due_is_excluded_before_assignment_or_draft(reserve):
    value = reserve
    value.article["published_at"] = value.stamp[0] - value.policy["max_source_age_hours"] * 3600 + 60
    result = prepared(value)
    due = value.stamp[0] + 180
    assert value.cycle.validate_reserve(result["id"])["state"] == "quality_ready"
    with pytest.raises(AgentError, match="STALE_SOURCE"):
        value.cycle.validate_reserve(result["id"], due_at=due)
    excluded = value.cycle.invalidate_reserve(result["id"], "STALE_SOURCE", due_at=due)
    assert excluded["state"] == "cancelled" and excluded["error"] == "STALE_SOURCE"
    assert excluded["preparation_actions"][-1]["due_at"] == due
    assert not value.writes and value.store.status()["active"] == []


def rejected(value):
    value.review.update(approved=False, issues=["The translation omits the discovery of the stellar wind."])
    claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        value.cycle.prepare(value.article, reserve_claim=claim)
    record = copy.deepcopy(value.cycle._load()["records"][-1])
    trace = (value.core.settings.root / record["quality_trace"]).read_bytes()
    return claim, record, trace


def editor(value, bodies, hook=None):
    from test_automatic_article_repair import patched
    calls = []

    class Editor:
        model = REPAIR_MODEL

        def generate_text(self, prompt):
            context = json.loads(prompt.split("UNTRUSTED_JSON_DATA:\n", 1)[1])
            calls.append(context)
            if hook:
                hook()
            return json.dumps(patched(context, bodies[len(calls) - 1]), ensure_ascii=False), {}

    value.cycle.repair_factory = lambda trace: ArticleRepair(Editor(),
        prompt_dir=value.core.settings.root / "prompts", trace=trace)
    return calls


def test_changed_repair_crosses_hour_with_new_independent_qa_and_keeps_original_refusal(reserve):
    from test_automatic_article_repair import CORRECT_BODY
    value = reserve
    claim, original, trace = rejected(value)
    value.stamp[0] += 3600
    calls = editor(value, [CORRECT_BODY])
    value.review.update(approved=True, issues=[])
    result = value.cycle.repair_rejected(original["id"])
    assert result["state"] == "quality_ready" and len(calls) == 1
    saved = value.cycle._record(value.cycle._load(), original["id"])
    assert saved["preparation_attempts"][0] == original["preparation_attempts"][0]
    assert saved["automatic_repair_attempts"][0]["state"] == "passed"
    assert (value.core.settings.root / saved["quality_trace"]).read_bytes().startswith(trace)
    value.store.finish_preparation(claim, "quality_ready")
    assert value.cycle.validate_reserve(original["id"])["state"] == "quality_ready"
    assert not value.writes


def test_two_failed_repairs_preserve_claims_and_can_close_known_candidate_without_publication(reserve):
    from test_automatic_article_repair import CORRECT_BODY, SECOND_BODY
    value = reserve
    claim, original, trace = rejected(value)
    calls = editor(value, [CORRECT_BODY, SECOND_BODY])
    for _ in range(2):
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            value.cycle.repair_rejected(original["id"])
    saved = value.cycle._record(value.cycle._load(), original["id"])
    assert value.cycle.automatic_repair_status(saved)["reason"] == "EXHAUSTED"
    assert [attempt["state"] for attempt in saved["automatic_repair_attempts"]] == ["rejected", "rejected"]
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_EXHAUSTED"):
        value.cycle.repair_rejected(original["id"])
    assert len(calls) == 2
    previous_claims = copy.deepcopy(saved["automatic_repair_attempts"])
    value.cycle.cancel_preparation(original["id"], "Known quality rejection exhausted two durable repair attempts")
    value.store.finish_preparation(claim, "rejected", "ARTICLE_QUALITY_REJECTED")
    final = value.cycle._record(value.cycle._load(), original["id"])
    assert final["state"] == "cancelled" and final["error"] == "ARTICLE_QUALITY_REJECTED"
    assert final["automatic_repair_attempts"] == previous_claims
    assert (value.core.settings.root / final["quality_trace"]).read_bytes().startswith(trace)
    assert not value.writes and value.store.active_preparation() is None


def test_sender_starting_during_editor_does_not_interrupt_local_quality_result(reserve):
    from test_article_cycle import seed_legacy_dispatch
    from test_automatic_article_repair import CORRECT_BODY
    value = reserve
    first = prepared(value)
    value.article = {**value.article, "url": "https://science.example.org/article/second/",
                     "original": value.article["original"] + "A second independent observation followed."}
    claim, original, _ = rejected(value)

    def sending():
        dispatch_id = seed_legacy_dispatch(value.core, first)
        with value.core.db.session.begin() as session:
            dispatch = session.get(Dispatch, dispatch_id)
            dispatch.state, dispatch.owner, dispatch.lease_until = "sending", "simulated-sender", value.stamp[0] + 600
            session.get(Target, "max").paused = False

    calls = editor(value, [CORRECT_BODY], hook=sending)
    value.review.update(approved=True, issues=[])
    result = value.cycle.repair_rejected(original["id"])
    assert result["state"] == "quality_ready" and len(calls) == 1
    value.store.finish_preparation(claim, "quality_ready")
    assert not value.writes
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_ASSIGNMENT_LOST"):
        value.cycle.stage_reserve(result["id"])
    new_claim = value.store.begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_RECONCILIATION_REQUIRED"):
        value.cycle.prepare({**value.article, "url": "https://science.example.org/article/third/"},
                            reserve_claim=new_claim)


@pytest.fixture
def reserve_pg(reserve):
    value = reserve
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL is not configured")
    admin = create_engine(url)
    schema = "reserve_pipeline_" + uid().replace("-", "")
    with admin.begin() as connection:
        connection.execute(text('CREATE SCHEMA "' + schema + '"'))
    isolated = url + ("&" if "?" in url else "?") + urlencode({"options": "-csearch_path=" + schema})
    original = value.core.db
    database = Database(value.core.settings, url=isolated)
    try:
        database.initialize()
        database.restore(original.export())
        value.core.db = database
        yield value
    finally:
        value.core.db = original
        database.engine.dispose()
        with admin.begin() as connection:
            connection.execute(text('DROP SCHEMA "' + schema + '" CASCADE'))
        admin.dispose()


def test_postgres_pipeline_preserves_evidence_through_single_draft_and_exact_approval(reserve_pg):
    value = reserve_pg
    result = prepared(value)
    slot = assignment(value, result)
    staged = value.cycle.stage_reserve(result["id"])
    pending = value.cycle.publish(result["id"], staged["content_hash"], due_at=slot["due_at"])
    value.store.update(slot["id"], "pending", token=slot["token"], dispatch_id=pending["dispatch_id"])
    assert pending["state"] == "pending" and len(value.writes) == 1
    assert value.records[pending["postiz_id"]]["state"] == "QUEUE"
    with value.core.db.session() as session:
        approval = session.scalar(select(TargetApproval))
        dispatch = session.scalar(select(Dispatch))
        assert approval.actor == "hourly_article_policy"
        assert dispatch.due_at == approval.scheduled_at == slot["due_at"]
        assert dispatch.snapshot_hash == approval.snapshot_hash
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 1
    with pytest.raises(AgentError):
        value.cycle.publish(result["id"], pending["content_hash"], due_at=slot["due_at"])
