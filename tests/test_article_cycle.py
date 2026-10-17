"""Scoped article phases never bypass Postiz or repeat an uncertain publication."""
import json
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Material, now
from max_postiz_agent.hub_models import Dispatch, DispatchAttempt, RemoteDraft, Target, TargetApproval, Variant

pytest_plugins = ["test_max_bridge"]


class Quality:
    def prepare(self, title, original, url):
        translation = "Космический ветер\n\nУчёные изучили необычные процессы в атмосфере далёкой звезды."
        return {"title": "Космический ветер", "body": translation + "\n\nИсточник: " + url,
                "translation": translation, "original": title + "\n\n" + original, "url": url, "topic": "science"}

    def close(self):
        pass


@pytest.fixture
def cycle(bridge, monkeypatch):
    core, records, _, _, _ = bridge
    records.clear()
    core.settings.channel("main")["publication"].update(max_posts_per_day=24, min_gap_minutes=60)
    stamp = [int(now() // 3600) * 3600 + 1800]
    monkeypatch.setattr("max_postiz_agent.article_cycle.now", lambda: stamp[0])
    policy = {"enabled": True, "target_id": "max", "source_id": "example_rss",
              "allowed_hosts": ["science.example.org"], "rights_evidence": "test_permission",
              "owner_instruction": "2026-10-17 hourly translated article tests",
              "min_gap_seconds": 3600, "max_per_day": 24}
    data = core.settings.root / "data"
    data.mkdir(exist_ok=True)
    (data / "hourly-article-policy.local.json").write_text(json.dumps(policy))
    article = {"url": "https://science.example.org/article/first/", "title": "An unusual stellar wind",
        "original": "Scientists studied an unusual atmosphere and discovered a stellar wind in the distant star. " * 2,
        "published_at": stamp[0] - 3600}
    value = HourlyArticleCycle(core, Quality)
    calls = []

    def create(target, body, media, kind="draft", due_at=None):
        calls.append((target, body, media, kind, due_at))
        ident = "article-post-" + str(len(calls))
        records[ident] = {"id": ident, "content": body,
            "publishDate": datetime.fromtimestamp(due_at or stamp[0], UTC).isoformat(),
            "state": "QUEUE" if kind == "schedule" else "DRAFT", "image": "[]", "childrenPost": [],
            "intervalInDays": None, "group": "group-article",
            "integration": {"id": "integration-max", "providerIdentifier": "max-browser"},
            "settings": json.dumps(target.options.get("settings", {}))}
        return ident

    monkeypatch.setattr(core.postiz, "create", create)
    return value, core, article, records, calls, stamp, policy


def test_prepare_preserves_real_source_and_never_schedules_or_approves(cycle):
    value, core, article, records, calls, _, policy = cycle
    result = value.prepare(article)
    assert result["state"] == "prepared" and result["url"] == article["url"]
    assert not records and not calls
    with core.db.session() as session:
        material = session.scalar(select(Material))
        variant = session.get(Variant, result["variant_id"])
        assert material.url == article["url"] and material.source_id == policy["source_id"]
        assert material.published_at == article["published_at"]
        assert variant.sources[0]["id"] == material.id
        assert variant.metadata_json["rights_evidence"] == policy["rights_evidence"]
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0


def test_exact_review_required_and_schedule_once_with_source_evidence(cycle):
    value, core, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_HASH_MISMATCH"):
        value.publish(prepared["id"], "0" * 64)
    assert calls == []
    result = value.publish(prepared["id"], prepared["content_hash"])
    assert result["state"] == "pending"
    assert len(calls) == 1 and calls[0][3] == "schedule"
    with core.db.session() as session:
        variant = session.get(Variant, prepared["variant_id"])
        dispatch = session.get(Dispatch, result["dispatch_id"])
        approval = session.get(TargetApproval, dispatch.approval_id)
        assert variant.sources and variant.metadata_json["origin"] == "hourly_article_test"
        assert dispatch.snapshot["sources"] == variant.sources
        assert approval.actor == "hourly_article_policy"
        assert approval.snapshot_hash == fingerprint({"content": core.snapshot(variant, session.get(Target, "max")), "due_at": result["due_at"]})
    with pytest.raises(AgentError, match="ARTICLE_RECONCILIATION_REQUIRED"):
        value.publish(prepared["id"], prepared["content_hash"])
    assert len(calls) == 1


def test_late_prepared_slot_is_not_caught_up(cycle):
    value, _, article, _, calls, stamp, _ = cycle
    prepared = value.prepare(article)
    stamp[0] += 3600
    with pytest.raises(AgentError, match="ARTICLE_SLOT_EXPIRED"):
        value.publish(prepared["id"], prepared["content_hash"])
    assert not calls


def test_claimed_slot_and_duplicate_original_are_durable(cycle):
    value, _, article, _, _, stamp, _ = cycle
    prepared = value.prepare(article)
    with pytest.raises(AgentError, match="ARTICLE_SLOT_ALREADY_CLAIMED"):
        value.prepare(article)
    ledger = value._load()
    ledger["records"][0]["state"] = "failed"
    value._save(ledger)
    stamp[0] += 3600
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.prepare({**article, "url": "https://science.example.org/article/second/"})
    assert value.status(prepared["id"])["variant_id"] == prepared["variant_id"]


def test_unknown_postiz_creation_requires_reconciliation(cycle, monkeypatch):
    value, core, article, _, _, stamp, _ = cycle
    prepared = value.prepare(article)
    attempts = []

    def uncertain(*args):
        attempts.append(args)
        raise AgentError("POSTIZ_UNKNOWN", 503)

    monkeypatch.setattr(core.postiz, "create", uncertain)
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        value.publish(prepared["id"], prepared["content_hash"])
    with core.db.session() as session:
        assert session.get(RemoteDraft, prepared["variant_id"]).state == "unknown"
        assert session.query(Dispatch).count() == 0
    stamp[0] += 3600
    with pytest.raises(AgentError, match="ARTICLE_RECONCILIATION_REQUIRED"):
        value.prepare({**article, "url": "https://science.example.org/article/second/"})
    assert len(attempts) == 1


def test_remote_text_change_cannot_receive_delegated_approval(cycle, monkeypatch):
    value, core, article, records, calls, _, _ = cycle
    prepared = value.prepare(article)
    original_create = core.postiz.create

    def edited(*args):
        ident = original_create(*args)
        records[ident]["content"] += " Изменено в Postiz."
        return ident

    monkeypatch.setattr(core.postiz, "create", edited)
    with pytest.raises(AgentError, match="ARTICLE_REMOTE_CHANGED"):
        value.publish(prepared["id"], prepared["content_hash"])
    with core.db.session() as session:
        assert session.query(Dispatch).count() == 0
    assert len(calls) == 1


def test_status_requires_live_delivery_and_postiz_published_then_pauses(cycle):
    value, core, article, records, _, stamp, _ = cycle
    prepared = value.prepare(article)
    planned = value.publish(prepared["id"], prepared["content_hash"])
    with core.db.session.begin() as session:
        dispatch = session.get(Dispatch, planned["dispatch_id"])
        dispatch.state, dispatch.verified = "published", False
    assert value.status()["state"] == "published_unverified"
    assert value.status()["delivery_verified"] is False
    with core.db.session.begin() as session:
        session.get(Dispatch, planned["dispatch_id"]).verified = True
        session.add(DispatchAttempt(dispatch_id=planned["dispatch_id"], result="published", created_at=planned["due_at"] + 5))
    records[planned["postiz_id"]]["state"] = "QUEUE"
    assert value.status()["state"] == "awaiting_postiz"
    with core.db.session() as session:
        assert not session.get(Target, "max").paused
    records[planned["postiz_id"]]["state"] = "PUBLISHED"
    assert value.status()["state"] == "published"
    with core.db.session() as session:
        assert session.get(Target, "max").paused
    stamp[0] += 1801
    # A new hour can prepare while the minimum interval is still running.
    # A different valid translation keeps this a genuinely new article.
    class NextQuality(Quality):
        def prepare(self, *args):
            result = super().prepare(*args)
            result["translation"] += " Дополнительные научные сведения."
            result["body"] += " Дополнительные научные сведения."
            return result
    value.quality_factory = NextQuality
    following = value.prepare({**article, "url": "https://science.example.org/article/next/",
                               "original": article["original"] + " New facts."})
    scheduled = value.publish(following["id"], following["content_hash"])
    assert scheduled["due_at"] >= planned["due_at"] + 5 + 3601


@pytest.mark.parametrize("change", [{"url": "https://other.example.org/"}, {"published_at": 1}, {"url": "https://science.example.org/article/?token=private"}])
def test_invalid_or_stale_inputs_never_claim_a_slot(cycle, change):
    value, _, article, _, _, _, _ = cycle
    with pytest.raises(AgentError):
        value.prepare({**article, **change})
    assert value._load()["records"] == []


def test_legacy_stage_default_still_creates_one_draft(cycle):
    value, core, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    core.stage_remote(prepared["variant_id"])
    assert len(calls) == 1 and calls[0][3] == "draft"
    with pytest.raises(AgentError, match="REMOTE_DRAFT_ALREADY_TRACKED"):
        core.stage_remote(prepared["variant_id"], due_at=now() + 60)


@pytest.mark.parametrize("due", [-1, float("nan"), float("inf"), True])
def test_stage_rejects_invalid_date_before_remote_mutation(cycle, due):
    value, core, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    with pytest.raises(AgentError, match="SCHEDULE_INVALID"):
        core.stage_remote(prepared["variant_id"], due_at=due)
    assert calls == []


def test_source_rights_are_required_before_generation(cycle):
    value, core, article, _, _, _, _ = cycle
    core.settings.source("example_rss")["rights"]["allow_derivative"] = False
    with pytest.raises(AgentError, match="ARTICLE_RIGHTS_REQUIRED"):
        value.prepare(article)
    assert value._load()["records"] == []


def test_generic_ingest_body_hash_prevents_new_headline_duplicate(cycle):
    value, core, article, _, _, _, _ = cycle
    with core.db.session.begin() as session:
        session.add(Material(source_id="example_rss", external_id="old-record", story_id="old-story",
            title="A completely different title", text=article["original"],
            url="https://science.example.org/old/", language="en",
            text_hash=fingerprint(" ".join(article["original"].casefold().split()))))
    with pytest.raises(AgentError, match="ARTICLE_DUPLICATE_SOURCE"):
        value.prepare(article)
    assert value._load()["records"] == []


def test_failed_approval_repauses_without_creating_dispatch(cycle, monkeypatch):
    value, core, article, _, _, _, _ = cycle
    prepared = value.prepare(article)

    def blocked(*args, **kwargs):
        raise AgentError("QUALITY_GATE_BLOCKED")

    monkeypatch.setattr(core, "approve_variant", blocked)
    with pytest.raises(AgentError, match="QUALITY_GATE_BLOCKED"):
        value.publish(prepared["id"], prepared["content_hash"])
    with core.db.session() as session:
        assert session.get(Target, "max").paused
        assert session.query(Dispatch).count() == 0


def test_unknown_send_remains_nonterminal_and_cannot_publish_again(cycle):
    value, core, article, _, calls, _, _ = cycle
    prepared = value.prepare(article)
    planned = value.publish(prepared["id"], prepared["content_hash"])
    with core.db.session.begin() as session:
        session.get(Dispatch, planned["dispatch_id"]).state = "unknown"
    assert value.status()["state"] == "unknown"
    with pytest.raises(AgentError, match="ARTICLE_RECONCILIATION_REQUIRED"):
        value.publish(prepared["id"], prepared["content_hash"])
    assert len(calls) == 1
