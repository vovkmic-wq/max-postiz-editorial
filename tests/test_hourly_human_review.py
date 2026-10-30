"""Human consent, exact machine evidence and bounded hourly delivery."""
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
from sqlalchemy import select

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import BridgePost, Dispatch, DispatchAttempt, RemoteDraft, Target, TargetApproval, Variant
from test_article_cycle import Quality

pytest_plugins = ["test_article_cycle"]


@pytest.fixture
def human_cycle(cycle, monkeypatch):
    value, core, _, _, _, stamp, policy = cycle
    policy["approval_mode"] = "human"
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    monkeypatch.setattr("max_postiz_agent.hub.now", lambda: stamp[0])
    monkeypatch.setattr("max_postiz_agent.human_review.now", lambda: stamp[0])
    return cycle


def queued(human_cycle, prepared, delay=180):
    value, core, _, records, _, stamp, _ = human_cycle
    due = stamp[0] + delay
    records[prepared["postiz_id"]].update(state="QUEUE", publishDate=datetime.fromtimestamp(due, UTC).isoformat())
    core.bridge.sync(force=True)
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        target = session.get(Target, variant.target_id)
        return variant.revision, fingerprint(core.snapshot(variant, target)), due


def human_approve(human_cycle, prepared, delay=180):
    _, core, _, _, _, _, _ = human_cycle
    revision, hashed, due = queued(human_cycle, prepared, delay)
    return core.approve_variant(prepared["variant_id"], revision, hashed, due,
                                actor="owner", human_confirmed=True)


def test_prepare_creates_only_one_draft_and_waits_for_person(human_cycle):
    value, core, article, records, calls, _, _ = human_cycle
    prepared = value.prepare(article)
    assert prepared["state"] == "awaiting_human_review"
    assert len(calls) == 1 and calls[0][3:] == ("draft", None)
    assert records[prepared["postiz_id"]]["state"] == "DRAFT"
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
    details = core.hourly_review_details(prepared["variant_id"])
    assert details["source_title"] == article["title"] and details["source_body"] == article["original"]
    assert details["machine_review_current"] is True and details["human_review_required"] is True


def test_automatic_publish_is_blocked_before_any_side_effect(human_cycle):
    value, core, article, _, calls, _, _ = human_cycle
    prepared = value.prepare(article)
    before = list(calls)
    with pytest.raises(AgentError, match="HUMAN_REVIEW_CONFIRMATION_REQUIRED"):
        value.publish(prepared["id"], prepared["content_hash"])
    assert calls == before
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0


@pytest.mark.parametrize("actor,confirmed", [("hourly_article_policy", True), ("root", True),
                                              ("owner", False), ("human_editor:owner", True)])
def test_actor_string_alone_cannot_grant_human_consent(human_cycle, monkeypatch, actor, confirmed):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    monkeypatch.setattr(core.bridge, "sync", lambda **_: pytest.fail("Authorization precedes synchronization"))
    with pytest.raises(AgentError, match="HUMAN_REVIEW_CONFIRMATION_REQUIRED"):
        core.approve_variant(prepared["variant_id"], prepared["revision"], prepared["content_hash"],
                             9999999999, actor, human_confirmed=confirmed)


def test_next_hour_can_prepare_while_prior_draft_waits(human_cycle):
    value, core, article, _, calls, stamp, _ = human_cycle
    first = value.prepare(article)
    stamp[0] += 3600

    class SecondQuality(Quality):
        def prepare(self, *args):
            result = super().prepare(*args)
            result["translation"] += " Новое научное наблюдение."
            result["body"] += " Новое научное наблюдение."
            return result

    value.quality_factory = SecondQuality
    second = value.prepare({**article, "url": "https://science.example.org/second/",
                            "original": article["original"] + " A second observation."})
    assert first["id"] != second["id"] and second["state"] == "awaiting_human_review"
    assert len(calls) == 2
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0


def test_backlog_limit_preserves_existing_drafts_without_claiming_another_slot(human_cycle):
    value, _, article, _, calls, stamp, _ = human_cycle
    value._save({"records": [
        {"id": str(i), "slot": int(stamp[0] // 3600) - i - 1,
         "state": "awaiting_human_review"} for i in range(24)]})
    with pytest.raises(AgentError, match="ARTICLE_HUMAN_REVIEW_BACKLOG"):
        value.prepare(article)
    assert len(value._load()["records"]) == 24 and not calls


def test_person_approves_same_postiz_queue_revision_date_once(human_cycle):
    value, core, article, records, calls, _, _ = human_cycle
    prepared = value.prepare(article)
    with core.db.session.begin() as session:
        session.get(Target, "max").paused = True
        others = {t.id: t.paused for t in session.scalars(select(Target)) if t.id != "max"}
    dispatch_id = human_approve(human_cycle, prepared)
    assert len(calls) == 1 and records[prepared["postiz_id"]]["state"] == "QUEUE"
    with core.db.session() as session:
        dispatch = session.get(Dispatch, dispatch_id)
        approval = session.get(TargetApproval, dispatch.approval_id)
        assert approval.actor == "human_editor:owner"
        assert approval.snapshot_hash == fingerprint({"content": dispatch.snapshot, "due_at": dispatch.due_at})
        assert not session.get(Target, "max").paused
        assert {t.id: t.paused for t in session.scalars(select(Target)) if t.id != "max"} == others
        assert session.scalar(select(Audit).where(Audit.action == "hourly_article_human_review"))
    with pytest.raises(AgentError, match="ARTICLE_OUTSTANDING_DELIVERY"):
        human_approve(human_cycle, prepared)
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 1


def test_machine_evidence_is_stale_after_postiz_edit(human_cycle):
    value, core, article, records, _, _, _ = human_cycle
    prepared = value.prepare(article)
    records[prepared["postiz_id"]]["content"] += " Дополнительная редакторская правка."
    revision, hashed, due = queued(human_cycle, prepared)
    assert core.hourly_review_details(prepared["variant_id"])["machine_review_current"] is False
    with pytest.raises(AgentError, match="ARTICLE_MACHINE_RECHECK_REQUIRED"):
        core.approve_variant(prepared["variant_id"], revision, hashed, due,
                             "owner", human_confirmed=True)
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == 0


def test_legacy_and_cancelled_records_cannot_receive_new_human_approval(human_cycle):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    revision, hashed, due = queued(human_cycle, prepared)
    ledger = value._load()
    ledger["records"][0]["state"] = "cancelled"
    value._save(ledger)
    with pytest.raises(AgentError, match="ARTICLE_HUMAN_REVIEW_NOT_ELIGIBLE"):
        core.approve_variant(prepared["variant_id"], revision, hashed, due, "owner", human_confirmed=True)


def test_worker_rejects_legacy_delegated_approval(human_cycle):
    value, core, article, _, _, stamp, _ = human_cycle
    prepared = value.prepare(article)
    ident = human_approve(human_cycle, prepared)
    with core.db.session.begin() as session:
        dispatch = session.get(Dispatch, ident)
        session.get(TargetApproval, dispatch.approval_id).actor = "hourly_article_policy"
    stamp[0] += 181
    assert core.publish_due() == 0
    with core.db.session() as session:
        assert session.get(Dispatch, ident).state == "cancelled"


def test_late_dispatch_requires_person_to_reschedule(human_cycle):
    value, core, article, _, _, stamp, _ = human_cycle
    prepared = value.prepare(article)
    ident = human_approve(human_cycle, prepared)
    stamp[0] += 180 + 3601
    assert core.publish_due() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        assert dispatch.state == "cancelled" and dispatch.error == "HUMAN_RESCHEDULE_REQUIRED"


def test_actual_recent_publication_defers_without_changing_approval_date(human_cycle):
    value, core, article, _, _, stamp, _ = human_cycle
    core.publisher.mode = "browser"
    prepared = value.prepare(article)
    ident = human_approve(human_cycle, prepared)
    with core.db.session.begin() as session:
        current = session.get(Dispatch, ident)
        older = Dispatch(variant_id=current.variant_id, approval_id=current.approval_id,
                         revision=current.revision, snapshot_hash="a" * 64,
                         snapshot=current.snapshot, due_at=stamp[0] - 4000, state="published", verified=True)
        session.add(older)
        session.flush()
        session.add(DispatchAttempt(dispatch_id=older.id, result="published", created_at=stamp[0] - 30))
        due = current.due_at
    stamp[0] += 181
    assert core.publish_due() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        assert dispatch.state == "pending" and dispatch.due_at == due and dispatch.attempts == 0
    # Once the actual interval has elapsed, send once using the original human
    # approval and date; do not shift it to construct a catch-up schedule.
    stamp[0] += 3390
    assert core.publish_due() == 1
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        assert dispatch.state == "published" and dispatch.verified
        assert dispatch.due_at == due and dispatch.attempts == 1
    assert core.publish_due() == 0


def test_verified_send_pauses_only_its_target(human_cycle):
    value, core, article, records, _, stamp, _ = human_cycle
    core.publisher.mode = "browser"
    prepared = value.prepare(article)
    ident = human_approve(human_cycle, prepared)
    stamp[0] += 181
    assert core.publish_due() == 1
    with core.db.session() as session:
        assert session.get(Dispatch, ident).verified
        assert session.get(Target, "max").paused
    assert value.status(prepared["id"])["state"] == "awaiting_postiz"
    records[prepared["postiz_id"]]["state"] = "PUBLISHED"
    assert value.status(prepared["id"])["state"] == "published"


def test_constructor_failure_closes_clients_without_requests(human_cycle, monkeypatch):
    from max_postiz_agent.translation import DedicatedTranslator
    value, core, _, _, _, _, _ = human_cycle
    clients = []

    class Model:
        def __init__(self, _):
            self.client = httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("No inference")))
            clients.append(self.client)

    monkeypatch.setattr("max_postiz_agent.article_cycle.OpenCode", Model)
    dedicated = DedicatedTranslator({"model": "hy-mt2-7b-q8", "base_url": "http://127.0.0.1:8094"},
        transport=httpx.MockTransport(lambda _: pytest.fail("No inference")))
    monkeypatch.setattr("max_postiz_agent.translation.configured_translator", lambda _: dedicated)
    monkeypatch.setattr("max_postiz_agent.article_quality.ArticleQuality",
                        lambda *args, **kwargs: (_ for _ in ()).throw(AgentError("TEST_CONSTRUCTOR_FAILURE")))
    value.quality_factory = None
    with pytest.raises(AgentError, match="TEST_CONSTRUCTOR_FAILURE"):
        value._quality()
    assert dedicated.client.is_closed and len(clients) == 1 and clients[0].is_closed


def test_recheck_uses_candidate_gate_and_durable_refusal(human_cycle, monkeypatch):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    details = core.hourly_review_details(prepared["variant_id"])
    calls = []

    class Recheck:
        translator = SimpleNamespace(client=httpx.Client())
        reviewer = SimpleNamespace(client=httpx.Client())

        def check_candidate(self, *args):
            calls.append(args)
            raise AgentError("ARTICLE_QUALITY_REJECTED")

        def close(self):
            pass

    monkeypatch.setattr("max_postiz_agent.article_cycle.HourlyArticleCycle._quality", lambda *args, **kwargs: Recheck())
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        core.recheck_hourly_variant(prepared["variant_id"], details["revision"], details["content_hash"])
    details = core.hourly_review_details(prepared["variant_id"])
    assert details["machine_review_current"] is False
    with pytest.raises(AgentError, match="ARTICLE_CANDIDATE_PREVIOUSLY_REJECTED"):
        core.recheck_hourly_variant(prepared["variant_id"], details["revision"], details["content_hash"])
    assert len(calls) == 1 and calls[0][2:] == (article["title"], article["original"], article["url"])


@pytest.mark.parametrize("config", [None, {"enabled": False, "model": "hy-mt2-7b-q8"},
                                    {"enabled": True, "model": "hy-mt2-1.8b-q8"}])
def test_human_prepare_requires_explicit_direct_hy_config(human_cycle, config):
    value, core, _, _, _, _, _ = human_cycle
    value.quality_factory = None
    if config:
        (core.settings.root / "config/translation.json").write_text(json.dumps({
            **config, "base_url": "http://127.0.0.1:8094"}))
    with pytest.raises(AgentError, match="HUMAN_HY_TRANSLATOR_REQUIRED"):
        value._quality()


def test_recheck_real_constructor_uses_valid_record_trace_without_translation(human_cycle, monkeypatch):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    details = core.hourly_review_details(prepared["variant_id"])
    observed = []

    def stop(check, *args):
        observed.append((check.translator.model, check.reviewer.model))
        raise AgentError("TEST_STOP_BEFORE_INFERENCE")

    monkeypatch.setattr("max_postiz_agent.article_quality.ArticleQuality.check_candidate", stop)
    with pytest.raises(AgentError, match="TEST_STOP_BEFORE_INFERENCE"):
        core.recheck_hourly_variant(prepared["variant_id"], details["revision"], details["content_hash"])
    assert observed == [("human_editor_candidate", "proxmox/qwen25-coder-7b")]
    assert core.hourly_review_details(prepared["variant_id"])["machine_review_current"] is False


def test_target_mutation_lease_blocks_human_approval(human_cycle):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    revision, hashed, due = queued(human_cycle, prepared)
    with core.db.session.begin() as session:
        link = session.scalar(select(BridgePost).where(BridgePost.variant_id == prepared["variant_id"]))
        link.mutation_owner = "human-editor-busy"
    with pytest.raises(AgentError, match="BRIDGE_EDIT_IN_PROGRESS"):
        core.approve_variant(prepared["variant_id"], revision, hashed, due,
                             "owner", human_confirmed=True)


def test_changed_revision_cannot_receive_human_approval(human_cycle):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    revision, hashed, due = queued(human_cycle, prepared)
    with pytest.raises(AgentError, match="REVISION_CONFLICT"):
        core.approve_variant(prepared["variant_id"], revision - 1, hashed, due,
                             "owner", human_confirmed=True)


def test_status_during_send_preserves_browser_authorization(human_cycle, monkeypatch):
    from max_postiz_agent.adapters import Delivery
    from max_postiz_agent.browser_worker import authorize_dispatch

    value, core, article, _, _, stamp, _ = human_cycle
    core.publisher.mode = "browser"
    prepared = value.prepare(article)
    ident = human_approve(human_cycle, prepared)
    stamp[0] += 181
    # The browser process creates its own service; use the isolated fixture's
    # service so this test exercises authorization without network or a browser.
    monkeypatch.setattr("max_postiz_agent.hub.HubService", lambda *args, **kwargs: core)
    monkeypatch.setattr("max_postiz_agent.db.now", lambda: stamp[0])
    monkeypatch.setattr(core, "close", lambda: None)
    observed = []

    def publish(chat_id, text, attachment, *, request_id):
        observed.append(value.status(prepared["id"])["state"])
        assert request_id == ident
        assert authorize_dispatch(core.settings, core.db, {
            "request_id": request_id, "chat_id": chat_id, "text": text,
            "attachment": attachment,
        }) is True
        return Delivery("confirmed", "fixture-status-mid")

    monkeypatch.setattr(core.publisher, "publish", publish)
    assert core.publish_due() == 1
    assert observed == ["sending"]
    with core.db.session() as session:
        assert session.get(Dispatch, ident).state == "published"
        assert session.get(Dispatch, ident).verified
        assert session.get(Target, "max").paused


def test_earlier_reschedule_is_observed_across_cycle_panel_and_postiz(human_cycle):
    value, core, article, records, _, stamp, _ = human_cycle
    core.publisher.mode = "browser"
    prepared = value.prepare(article)
    old_ident = human_approve(human_cycle, prepared, delay=7200)
    # The human moves the same Postiz post earlier, which revokes the old
    # approval. It then needs a new explicit approval of that exact date.
    new_ident = human_approve(human_cycle, prepared, delay=3600)
    with core.db.session() as session:
        assert session.get(Dispatch, old_ident).state == "cancelled"
        assert session.get(Dispatch, new_ident).due_at < session.get(Dispatch, old_ident).due_at
    token = core.bridge._seal({"kind": "channel", "target": "max", "destination": "-123",
                               "expires": stamp[0] + 7200})
    assert value.status(prepared["id"])["dispatch_id"] == new_ident
    assert core.hourly_review_details(prepared["variant_id"])["status"] == "pending"
    assert core.bridge.status(token, "integration-max", prepared["postiz_id"])["state"] == "pending"
    stamp[0] += 3601
    assert core.publish_due() == 1
    assert core.bridge.status(token, "integration-max", prepared["postiz_id"])["state"] == "published"
    records[prepared["postiz_id"]]["state"] = "PUBLISHED"
    assert value.status(prepared["id"])["state"] == "published"


@pytest.mark.parametrize("state", ["sending", "unknown"])
def test_observing_uncertain_send_never_requeues_or_repeats_it(human_cycle, state):
    value, core, article, _, _, stamp, _ = human_cycle
    prepared = value.prepare(article)
    ident = human_approve(human_cycle, prepared)
    with core.db.session.begin() as session:
        dispatch = session.get(Dispatch, ident)
        dispatch.state, dispatch.owner, dispatch.lease_until = state, "existing-worker", stamp[0] + 600
    stamp[0] += 181
    assert value.status(prepared["id"])["state"] == state
    assert core.publish_due() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        assert dispatch.state == state and dispatch.attempts == 0
        assert session.query(DispatchAttempt).count() == 0


def test_hourly_media_is_blocked_before_human_approval_or_inference(human_cycle, monkeypatch):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    revision, hashed, due = queued(human_cycle, prepared)
    with core.db.session.begin() as session:
        session.get(Variant, prepared["variant_id"]).media = [{"path": "data/media/unapproved.png"}]
    monkeypatch.setattr(core, "_quality", lambda *args: None)
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        revision, hashed = variant.revision, fingerprint(core.snapshot(variant, session.get(Target, "max")))
    with pytest.raises(AgentError, match="ARTICLE_MEDIA_NOT_ALLOWED"):
        core.approve_variant(prepared["variant_id"], revision, hashed, due,
                             "owner", human_confirmed=True)
    with pytest.raises(AgentError, match="ARTICLE_MEDIA_NOT_ALLOWED"):
        core.recheck_hourly_variant(prepared["variant_id"], revision, hashed)
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0


def test_recheck_preserves_exact_postiz_text_instead_of_replacing_it(human_cycle, monkeypatch):
    value, core, article, records, _, _, _ = human_cycle
    prepared = value.prepare(article)
    remote_body = records[prepared["postiz_id"]]["content"]
    details = core.hourly_review_details(prepared["variant_id"])
    seen = []

    class Recheck:
        translator = SimpleNamespace(client=httpx.Client())
        reviewer = SimpleNamespace(client=httpx.Client())

        def check_candidate(self, title, body, *source):
            seen.append((title, body, source))
            return {"title": title.strip(), "body": body + " normalized export footer",
                    "translation": title.strip() + "\n\n" + body}

        def close(self):
            pass

    monkeypatch.setattr("max_postiz_agent.article_cycle.HourlyArticleCycle._quality", lambda *args, **kwargs: Recheck())
    result = core.recheck_hourly_variant(prepared["variant_id"], details["revision"], details["content_hash"])
    assert result["machine_review_current"] is True and len(seen) == 1
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        assert variant.body == remote_body
        assert variant.metadata_json["quality"] == "operator_ministral_languagetool"
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
    assert records[prepared["postiz_id"]]["content"] == remote_body


def test_human_details_expose_machine_failure_without_granting_current_proof(human_cycle):
    cycle, core, article, *_ = human_cycle
    prepared = cycle.prepare(article)
    with core.db.session.begin() as session:
        variant = session.get(Variant, prepared["variant_id"])
        variant.metadata_json = {**variant.metadata_json, "machine_text_hash": None,
                                 "machine_review_status": "failed",
                                 "machine_review_issues": ["Retained independent finding"]}
    details = core.hourly_review_details(prepared["variant_id"])
    assert details["machine_review_current"] is False
    assert details["machine_review_status"] == "failed"
    assert details["machine_review_issues"] == ["Retained independent finding"]
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0


def test_human_recheck_cannot_reuse_an_existing_translation(human_cycle, monkeypatch):
    value, core, article, records, _, stamp, _ = human_cycle
    first = value.prepare(article)
    stamp[0] += 3600

    class SecondQuality(Quality):
        def prepare(self, *args):
            result = super().prepare(*args)
            result["translation"] += " Новое наблюдение."
            result["body"] += " Новое наблюдение."
            return result

    value.quality_factory = SecondQuality
    second = value.prepare({**article, "url": "https://science.example.org/different/",
                            "original": article["original"] + " A different source."})
    # The source footer is changed to this post's own URL; the translated
    # narrative is still a duplicate, even though the source and URL differ.
    records[second["postiz_id"]]["content"] = records[first["postiz_id"]]["content"].replace(
        article["url"], "https://science.example.org/different/")
    core.bridge.sync(force=True)
    details = core.hourly_review_details(second["variant_id"])

    class Recheck:
        translator = SimpleNamespace(client=httpx.Client())
        reviewer = SimpleNamespace(client=httpx.Client())

        def check_candidate(self, title, body, *source):
            return {"translation": title.strip() + "\n\n" + body.strip()}

        def close(self):
            pass

    monkeypatch.setattr("max_postiz_agent.article_cycle.HourlyArticleCycle._quality", lambda *args, **kwargs: Recheck())
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_TRANSLATION"):
        core.recheck_hourly_variant(second["variant_id"], details["revision"], details["content_hash"])
    assert core.hourly_review_details(second["variant_id"])["machine_review_current"] is False


@pytest.mark.parametrize("uncertain_remote", [False, True])
def test_staged_hourly_edits_require_postiz_without_local_mutation(human_cycle, uncertain_remote):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    with core.db.session.begin() as session:
        variant = session.get(Variant, prepared["variant_id"])
        if uncertain_remote:
            link = session.scalar(select(BridgePost).where(BridgePost.variant_id == variant.id))
            session.delete(link)
            tracked = session.get(RemoteDraft, variant.id)
            tracked.external_id, tracked.state = None, "unknown"
            metadata = dict(variant.metadata_json)
            metadata.pop("postiz_post_id", None)
            variant.metadata_json = metadata
        original = (variant.revision, variant.title, variant.body, dict(variant.metadata_json))
    with pytest.raises(AgentError, match="ARTICLE_EDIT_IN_POSTIZ_REQUIRED"):
        core.revise_variant(prepared["variant_id"], original[0], original[2] + " Локальная правка.")
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        assert (variant.revision, variant.title, variant.body, variant.metadata_json) == original


def test_persisted_local_hourly_edit_cannot_bypass_canonical_postiz_text(human_cycle, monkeypatch):
    from max_postiz_agent.human_review import text_hash

    value, core, article, records, _, stamp, _ = human_cycle
    prepared = value.prepare(article)
    queued(human_cycle, prepared)
    remote_body = records[prepared["postiz_id"]]["content"]
    # Reproduce a local edit that an earlier server could machine-check while
    # leaving the Postiz hash unchanged. It is historical state, not an API path.
    with core.db.session.begin() as session:
        variant = session.get(Variant, prepared["variant_id"])
        variant.body += " Уточнение в локальной копии."
        variant.revision += 1
        variant.metadata_json = {**variant.metadata_json,
            "machine_text_hash": text_hash(variant.title, variant.body)}
    ident = human_approve(human_cycle, prepared)
    monkeypatch.setattr(core.publisher, "publish", lambda *args, **kwargs: pytest.fail("Canonical Postiz text differs"))
    stamp[0] += 181
    assert core.publish_due() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        assert dispatch.state == "failed" and dispatch.error == "BRIDGE_REMOTE_CHANGED"
    assert records[prepared["postiz_id"]]["content"] == remote_body


def test_postiz_headline_edit_updates_title_and_rechecks_exact_narrative(human_cycle, monkeypatch):
    value, core, article, records, _, _, _ = human_cycle
    prepared = value.prepare(article)
    new_title = "Уточнённые наблюдения за звёздным ветром"
    remote = records[prepared["postiz_id"]]
    remote["content"] = remote["content"].replace(prepared["title"], new_title, 1)
    core.bridge.sync(force=True)
    details = core.hourly_review_details(prepared["variant_id"])
    assert details["machine_review_current"] is False
    observed = []

    class Recheck:
        translator = SimpleNamespace(client=httpx.Client())
        reviewer = SimpleNamespace(client=httpx.Client())

        def check_candidate(self, title, body, *source):
            observed.append((title, body, source))
            return {"translation": title + "\n\n" + body}

        def close(self):
            pass

    monkeypatch.setattr("max_postiz_agent.article_cycle.HourlyArticleCycle._quality", lambda *args, **kwargs: Recheck())
    result = core.recheck_hourly_variant(prepared["variant_id"], details["revision"], details["content_hash"])
    assert result["machine_review_current"] is True
    assert observed == [(new_title, "Учёные изучили необычные процессы в атмосфере далёкой звезды.",
                         (article["title"], article["original"], article["url"]))]
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        assert variant.title == new_title and variant.body == remote["content"]
        assert session.query(TargetApproval).count() == session.query(Dispatch).count() == 0
