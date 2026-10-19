"""Explicit calibrated policy approvals retain hourly and exact-publication guards."""
import copy
import json

import httpx
import pytest
from sqlalchemy import select

from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.automatic_policy import POLICY_ACTOR, automatic_evidence
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Draft
from max_postiz_agent.hub_models import BridgePost, Dispatch, DispatchAttempt, Target, TargetApproval, Variant

pytest_plugins = ["test_hourly_human_review"]


class Model:
    def __init__(self, value, model):
        self.value, self.model, self.client = value, model, self
        self.immutable_candidates = True

    def generate_text(self, _):
        return json.dumps(self.value, ensure_ascii=False), {}

    def close(self):
        pass


@pytest.fixture
def automatic_cycle(human_cycle, monkeypatch):
    cycle, core, _, _, _, stamp, policy = human_cycle
    policy.update(approval_mode="automatic", review_rubric="meaning-first-v1")
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    monkeypatch.setattr("max_postiz_agent.automatic_policy.now", lambda: stamp[0])
    review = {"approved": True, "topic": "science", "issues": [],
              "warnings": ["A different stylistic choice is possible."]}

    def quality(record_id=None, review_only=False):
        return ArticleQuality(Model({"title": "Космический ветер",
            "body": "Учёные изучили необычные процессы в атмосфере далёкой звезды."}, "hy-mt2-7b-q8"),
            Model(copy.deepcopy(review), "ministral"), prompt_dir=core.settings.root / "prompts",
            trace=cycle._trace_writer(record_id), review_rubric="meaning-first-v1",
            transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": []})))

    cycle.quality_factory = None
    monkeypatch.setattr(cycle, "_quality", quality)
    original_create = core.postiz.create

    def create(*args, **kwargs):
        ident = original_create(*args, **kwargs)
        settings = json.loads(human_cycle[3][ident]["settings"])
        settings["__type"] = "max-browser"
        human_cycle[3][ident]["settings"] = json.dumps(settings)
        return ident

    def request(method, path, **kwargs):
        assert method == "POST" and path == "/posts"
        payload = kwargs["json"]
        assert payload["republish"] is False and payload["type"] == "schedule"
        post = payload["posts"][0]
        ident = post["value"][0]["id"]
        remote = human_cycle[3][ident]
        assert post["value"][0]["content"] == remote["content"]
        assert post["group"] == remote["group"]
        # Model the installed global Postiz guard: a caller-held second lease
        # must fail rather than silently bypassing the actual HTTP contract.
        lease = core.bridge.mutation([ident])
        remote.update(state="QUEUE", publishDate=payload["date"])
        core.bridge.mutation([ident], release=lease["owner"])
        return [{"postId": ident}]

    monkeypatch.setattr(core.postiz, "create", create)
    monkeypatch.setattr(core.postiz, "request", request)
    with core.db.session.begin() as session:
        session.get(Target, "max").paused = True
    return human_cycle, review


def test_automatic_preparation_and_policy_approval_never_claim_human_consent(automatic_cycle):
    (cycle, core, article, records, calls, _, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    assert prepared["state"] == "automatic_ready" and len(records) == len(calls) == 1
    assert records[prepared["postiz_id"]]["state"] == "DRAFT"
    with core.db.session() as session:
        assert automatic_evidence(session, session.get(Variant, prepared["variant_id"]))["review"]["warnings"]
    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    assert planned["state"] == "pending" and len(calls) == 1 and calls[0][3] == "draft"
    assert records[planned["postiz_id"]]["state"] == "QUEUE"
    with core.db.session() as session:
        approval = session.query(TargetApproval).one()
        audit = session.scalar(select(Audit).where(Audit.entity == approval.id,
                                                   Audit.action == "hourly_article_policy_review"))
        assert approval.actor == audit.actor == POLICY_ACTOR
        assert audit.details["approval_mode"] == "automatic"
        assert not session.scalar(select(Audit).where(Audit.action == "hourly_article_human_review"))
        assert not session.get(Target, "max").paused


def test_restricted_worker_delivers_automatic_once_and_pauses_target(automatic_cycle):
    (cycle, core, article, records, _, stamp, _), _ = automatic_cycle
    core.publisher.mode = "browser"
    prepared = cycle.prepare(article)
    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    stamp[0] += 121
    assert core.dispatch_tick() == 1
    assert core.dispatch_tick() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, planned["dispatch_id"])
        assert dispatch.verified and dispatch.state == "published" and dispatch.attempts == 1
        assert session.get(Target, "max").paused
    records[planned["postiz_id"]]["state"] = "PUBLISHED"
    assert cycle.status(prepared["id"])["state"] == "published"


def test_new_rubric_still_preserves_a_negative_verdict_without_draft_or_approval(automatic_cycle):
    (cycle, core, article, records, calls, _, _), review = automatic_cycle
    review.update(approved=False, issues=["The translation changes a scientific fact."], warnings=[])
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article)
    assert not records and not calls
    with core.db.session() as session:
        assert session.query(Variant).count() == session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
    trace = next((core.settings.root / "data/hourly-articles").glob("quality-*.local.jsonl")).read_text(encoding="utf-8")
    assert "The translation changes a scientific fact." in trace and '"approved": false' in trace


@pytest.mark.parametrize("change", ["hard_gate", "raw_verdict", "source", "trace", "legacy_rubric", "spelling"])
def test_policy_cannot_approve_invalid_or_old_evidence(automatic_cycle, change):
    (cycle, core, article, records, calls, _, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    with core.db.session.begin() as session:
        variant = session.get(Variant, prepared["variant_id"])
        evidence = copy.deepcopy(variant.metadata_json["machine_review_evidence"])
        if change == "hard_gate":
            evidence["hard_gates_passed"] = False
        elif change == "raw_verdict":
            evidence["review_raw"] = json.dumps({"approved": False, "topic": "science", "issues": ["Changed fact"], "warnings": []})
        elif change == "source":
            evidence["source_hash"] = "a" * 64
        elif change == "trace":
            evidence.pop("trace_hash")
        elif change == "legacy_rubric":
            evidence["review_rubric"] = "legacy"
        else:
            evidence["spelling_unresolved"] = [{"message": "Hard grammar error"}]
        variant.metadata_json = {**variant.metadata_json, "machine_review_evidence": evidence,
                                 "machine_review_evidence_hash": fingerprint(evidence)}
        hashed = fingerprint(core.snapshot(variant, session.get(Target, "max")))
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_(NOT_READY|RECHECK_REQUIRED)"):
        cycle.publish(prepared["id"], hashed)
    assert len(records) == len(calls) == 1 and records[prepared["postiz_id"]]["state"] == "DRAFT"


def test_machine_hash_and_policy_hash_are_checked_before_postiz_side_effects(automatic_cycle):
    (cycle, core, article, records, calls, _, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_HASH_MISMATCH"):
        cycle.publish(prepared["id"], "0" * 64)
    assert len(records) == len(calls) == 1 and records[prepared["postiz_id"]]["state"] == "DRAFT"
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0


def test_uncertain_scheduling_is_never_retried_by_automatic_publish(automatic_cycle, monkeypatch):
    (cycle, core, article, _, _, _, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    attempts = []

    def uncertain(*args, **kwargs):
        attempts.append(True)
        core.bridge.mutation([prepared["postiz_id"]])
        raise AgentError("POSTIZ_UNKNOWN", 503)

    monkeypatch.setattr(core.postiz, "request", uncertain)
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        cycle.publish(prepared["id"], prepared["content_hash"])
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_READY"):
        cycle.publish(prepared["id"], prepared["content_hash"])
    assert attempts == [True]
    with core.db.session() as session:
        assert session.scalar(select(BridgePost)).mutation_owner


def test_mutation_lease_blocks_policy_publish_before_scheduling(automatic_cycle):
    (cycle, core, article, records, calls, _, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    with core.db.session.begin() as session:
        session.scalar(select(BridgePost)).mutation_owner = "existing-owner"
    with pytest.raises(AgentError, match="ARTICLE_OUTSTANDING_DELIVERY"):
        cycle.publish(prepared["id"], prepared["content_hash"])
    assert len(records) == len(calls) == 1 and records[prepared["postiz_id"]]["state"] == "DRAFT"


@pytest.mark.parametrize("change", ["missing", "actor", "evidence_hash", "due_at", "target_id", "content_hash"])
def test_automatic_send_rechecks_exact_policy_audit(automatic_cycle, monkeypatch, change):
    (cycle, core, article, _, _, stamp, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    with core.db.session.begin() as session:
        dispatch = session.get(Dispatch, planned["dispatch_id"])
        audit = session.scalar(select(Audit).where(Audit.entity == dispatch.approval_id,
                                                   Audit.action == "hourly_article_policy_review"))
        if change == "missing":
            session.delete(audit)
        elif change == "actor":
            audit.actor = "human_editor:owner"
        else:
            audit.details = {**audit.details, change: audit.details[change] + 1 if change == "due_at" else "different"}
    monkeypatch.setattr(core.publisher, "publish", lambda *args, **kwargs: pytest.fail("Invalid policy audit must not send"))
    stamp[0] += 121
    assert core.dispatch_tick() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, planned["dispatch_id"])
        assert dispatch.state == "cancelled" and dispatch.error == "ARTICLE_POLICY_APPROVAL_STALE"


def test_automatic_hourly_gap_uses_actual_delivery_time_and_keeps_approved_date(automatic_cycle):
    (cycle, core, article, _, _, stamp, _), _ = automatic_cycle
    core.publisher.mode = "browser"
    prepared = cycle.prepare(article)
    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    with core.db.session.begin() as session:
        current = session.get(Dispatch, planned["dispatch_id"])
        old = Dispatch(variant_id=current.variant_id, approval_id=current.approval_id, revision=current.revision,
                       snapshot_hash="a" * 64, snapshot=current.snapshot, due_at=stamp[0] - 4000,
                       state="published", verified=True)
        session.add(old)
        session.flush()
        session.add(DispatchAttempt(dispatch_id=old.id, result="published", created_at=stamp[0] - 30))
        due = current.due_at
    stamp[0] += 121
    assert core.dispatch_tick() == 0
    stamp[0] += 3450
    assert core.dispatch_tick() == 1
    with core.db.session() as session:
        assert session.get(Dispatch, planned["dispatch_id"]).due_at == due


def test_automatic_ready_crossing_hour_claims_only_current_slot_without_a_catchup_batch(automatic_cycle):
    (cycle, core, article, records, calls, stamp, _), _ = automatic_cycle
    stamp[0] = int(stamp[0] // 3600) * 3600 + 3540
    prepared = cycle.prepare(article)
    stamp[0] += 90
    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    assert planned["state"] == "pending" and planned["automatic_slot"] == int(stamp[0] // 3600)
    with pytest.raises(AgentError, match="ARTICLE_(OUTSTANDING_DELIVERY|SLOT_ALREADY_CLAIMED)"):
        cycle.prepare({**article, "url": "https://science.example.org/another/",
                       "original": article["original"] + " A new observation."})
    assert len(records) == len(calls) == 1 and records[prepared["postiz_id"]]["state"] == "QUEUE"
    core.publisher.mode = "browser"
    stamp[0] += 121
    assert core.dispatch_tick() == 1
    records[prepared["postiz_id"]]["state"] = "PUBLISHED"
    assert cycle.status(prepared["id"])["state"] == "published"
    with pytest.raises(AgentError, match="ARTICLE_SLOT_ALREADY_CLAIMED"):
        cycle.prepare({**article, "url": "https://science.example.org/another/",
                       "original": article["original"] + " A new observation."})


def test_human_mode_still_refuses_policy_approval(human_cycle):
    cycle, core, article, _, _, stamp, _ = human_cycle
    prepared = cycle.prepare(article)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_ELIGIBLE"):
        core.approve_hourly_policy(prepared["variant_id"], prepared["revision"], prepared["content_hash"], stamp[0] + 120)


@pytest.mark.parametrize("mode,rubric", [("automatic", None), ("automatic", "legacy"),
                                         ("automatic", "meaning-first-v2"), ("human", "meaning-first-v1")])
def test_policy_requires_an_explicit_known_rubric_only_for_automatic(automatic_cycle, mode, rubric):
    (cycle, core, _, _, _, _, policy), _ = automatic_cycle
    policy["approval_mode"] = mode
    if rubric is None:
        policy.pop("review_rubric", None)
    else:
        policy["review_rubric"] = rubric
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    with pytest.raises(AgentError, match="ARTICLE_POLICY_INVALID"):
        cycle._policy()


@pytest.mark.parametrize("after_approval", [False, True])
def test_current_review_prompt_change_invalidates_prepared_or_approved_text(automatic_cycle, monkeypatch, after_approval):
    (cycle, core, article, records, _, stamp, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    planned = cycle.publish(prepared["id"], prepared["content_hash"]) if after_approval else None
    prompt = core.settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt"
    prompt.write_text(prompt.read_text(encoding="utf-8") + "\nNew review instruction.", encoding="utf-8")
    if not after_approval:
        with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_RECHECK_REQUIRED"):
            cycle.publish(prepared["id"], prepared["content_hash"])
        assert records[prepared["postiz_id"]]["state"] == "DRAFT"
    else:
        monkeypatch.setattr(core.publisher, "publish", lambda *args, **kwargs: pytest.fail("Stale prompt must not send"))
        stamp[0] += 121
        assert core.dispatch_tick() == 0
        with core.db.session() as session:
            dispatch = session.get(Dispatch, planned["dispatch_id"])
            assert dispatch.state == "cancelled" and dispatch.error == "ARTICLE_AUTOMATIC_RECHECK_REQUIRED"


def test_backlog_counts_migrated_automatic_drafts_before_claiming_a_new_slot(automatic_cycle):
    (cycle, _, article, _, calls, stamp, _), _ = automatic_cycle
    cycle._save({"records": [{"id": str(i), "slot": int(stamp[0] // 3600) - i - 1,
                              "state": "automatic_ready", "automatic_enrolled": True} for i in range(24)]})
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_BACKLOG"):
        cycle.prepare(article)
    assert len(cycle._load()["records"]) == 24 and not calls


def test_review_prompt_hash_normalizes_crlf_for_windows_portability(automatic_cycle):
    (cycle, core, article, _, _, _, _), _ = automatic_cycle
    prompt = core.settings.root / "prompts/ARTICLE_REVIEW_MEANING_FIRST_V1.txt"
    canonical = prompt.read_text(encoding="utf-8")
    prompt.write_bytes(canonical.replace("\n", "\r\n").encode("utf-8"))
    prepared = cycle.prepare(article)
    # Changing only file newline conventions does not alter the actual prompt
    # given to the independent reviewer or invalidate its exact text evidence.
    prompt.write_bytes(canonical.encode("utf-8"))
    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    assert planned["state"] == "pending"


def test_staged_automatic_text_cannot_be_changed_locally_behind_postiz(automatic_cycle, monkeypatch):
    (cycle, core, article, records, _, _, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    monkeypatch.setattr(cycle, "_quality", lambda *args, **kwargs: pytest.fail("Staged text cannot be locally retranslated"))
    with pytest.raises(AgentError, match="ARTICLE_EDIT_IN_POSTIZ_REQUIRED"):
        cycle.revise_prepared(prepared["id"], {"title": "Новый заголовок", "body": "Новый текст перевода."})
    assert records[prepared["postiz_id"]]["state"] == "DRAFT"
    assert cycle.status(prepared["id"])["content_hash"] == prepared["content_hash"]


@pytest.mark.parametrize("response", [[], {}, [None], [{"postId": "a-different-post"}],
                                      [{"postId": "same"}, {"postId": "same"}]])
def test_rescheduling_requires_exact_single_existing_post_id_without_retry(automatic_cycle, monkeypatch, response):
    (cycle, core, article, _, _, stamp, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    requests = []

    def request(method, path, **kwargs):
        requests.append((method, path, kwargs["json"]))
        return response

    monkeypatch.setattr(core.postiz, "request", request)
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        core.postiz.reschedule_existing(prepared["postiz_id"], prepared["body"], stamp[0] + 120)
    assert len(requests) == 1
    payload = requests[0][2]
    assert payload["republish"] is False and payload["type"] == "schedule"
    assert payload["posts"][0]["value"][0]["id"] == prepared["postiz_id"]


def test_successful_reschedule_observes_async_guard_finish_without_unlocking(automatic_cycle, monkeypatch):
    (cycle, core, article, records, _, _, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    leases, waits = [], []

    def guarded_request(method, path, **kwargs):
        assert not leases
        leases.append(core.bridge.mutation([prepared["postiz_id"]])["owner"])
        records[prepared["postiz_id"]].update(state="QUEUE", publishDate=kwargs["json"]["date"])
        return [{"postId": prepared["postiz_id"]}]

    def observe_finish(delay):
        waits.append(delay)
        core.bridge.mutation([prepared["postiz_id"]], release=leases[0])

    monkeypatch.setattr(core.postiz, "request", guarded_request)
    monkeypatch.setattr("max_postiz_agent.automatic_policy.time.sleep", observe_finish)
    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    assert planned["state"] == "pending" and len(waits) == 1
    assert planned["revision"] == prepared["revision"] + 1
    with core.db.session() as session:
        assert not session.scalar(select(BridgePost)).mutation_owner
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 1


def test_default_status_prefers_older_active_migrated_record_to_latest_completed(automatic_cycle):
    (cycle, core, article, _, _, stamp, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    planned = cycle.publish(prepared["id"], prepared["content_hash"])
    with core.db.session.begin() as session:
        existing = session.get(Variant, prepared["variant_id"])
        draft = Draft(channel_id="main", story_id="later-completed-story", current={}, config_id=existing.config_hash)
        session.add(draft)
        session.flush()
        later = Variant(draft_id=draft.id, target_id=existing.target_id, title="Later article",
                        body="Later completed article", sources=existing.sources, media=[], metadata_json={},
                        source_revision=1, config_hash=existing.config_hash, binding_hash=existing.binding_hash)
        session.add(later)
        session.flush()
        later_id = later.id
    ledger = cycle._load()
    ledger["records"][0].update(slot=int(stamp[0] // 3600) - 2, automatic_enrolled=True)
    ledger["records"].append({"id": "later-completed", "slot": int(stamp[0] // 3600) - 1,
        "target_id": "max", "variant_id": later_id, "state": "published"})
    cycle._save(ledger)
    result = cycle.status()
    assert result["id"] == prepared["id"] and result["dispatch_id"] == planned["dispatch_id"]
    assert result["state"] == "pending"
    with core.db.session() as session:
        assert not session.get(Target, "max").paused
