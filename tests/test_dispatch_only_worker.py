"""A limited server never revives collectors, model jobs or unrelated routes."""
import json
import threading
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from max_postiz_agent.api import create_app
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Draft, Story, uid
from max_postiz_agent.hub_api import hub_app
from max_postiz_agent.hub_models import BridgePost, Dispatch, Target, TargetApproval, Variant
from test_hourly_human_review import human_approve

pytest_plugins = ["test_hourly_human_review"]


def unrelated_dispatch(core, prepared, *, target_id="other", route="manual", origin="postiz_max_bridge",
                       approval_mode="legacy", actor="owner", state="pending"):
    with core.db.session.begin() as session:
        original = session.get(Variant, prepared["variant_id"])
        target = session.get(Target, target_id)
        if target is None:
            target = Target(id=target_id, profile_id="main", platform="max", route=route,
                            destination="-456", enabled=True, paused=False, binding_hash="other-binding")
            session.add(target)
        target.paused = False  # Simulate an accidental unpause.
        source_draft = session.get(Draft, original.draft_id)
        story = Story(title=original.title, text=original.body, text_hash=fingerprint({"case": uid()}))
        session.add(story)
        session.flush()
        draft = Draft(channel_id=source_draft.channel_id, story_id=story.id,
                      current=source_draft.current, config_id=source_draft.config_id)
        session.add(draft)
        session.flush()
        variant = Variant(draft_id=draft.id, target_id=target.id, title=original.title, body=original.body,
                          sources=original.sources, media=[], source_revision=1, config_hash=original.config_hash,
                          binding_hash=target.binding_hash, metadata_json={**original.metadata_json,
                          "origin": origin, "approval_mode": approval_mode})
        session.add(variant)
        session.flush()
        snapshot = core.snapshot(variant, target)
        approval = TargetApproval(variant_id=variant.id, revision=variant.revision,
                                  snapshot_hash=fingerprint(snapshot), scheduled_at=0, actor=actor)
        session.add(approval)
        session.flush()
        dispatch = Dispatch(variant_id=variant.id, approval_id=approval.id, revision=variant.revision,
                            snapshot_hash=approval.snapshot_hash, snapshot=snapshot, due_at=0, state=state,
                            owner="old-worker" if state == "sending" else None, lease_until=0)
        session.add(dispatch)
        session.flush()
        return dispatch.id


def forbid_broad_work(core, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("Dispatch-only mode invoked broad or model work")

    for method in ("tick", "recurring_due", "schedule_sources", "process_inbox", "make_variants", "model_task"):
        monkeypatch.setattr(core, method, forbidden)
    monkeypatch.setattr(core.reddit_mail, "poll", forbidden)
    monkeypatch.setattr(core.reddit_intake, "process_one", forbidden)


def test_dispatch_only_sends_human_article_and_leaves_other_routes_untouched(human_cycle, monkeypatch):
    value, core, article, _, _, stamp, _ = human_cycle
    core.publisher.mode = "browser"
    prepared = value.prepare(article)
    approved = human_approve(human_cycle, prepared)
    unrelated = [unrelated_dispatch(core, prepared, target_id="other-" + route, route=route)
                 for route in ("manual", "max_api", "postiz", "max_browser")]
    expired = unrelated_dispatch(core, prepared, target_id="expired-legacy", state="sending")
    forbid_broad_work(core, monkeypatch)
    stamp[0] += 181
    assert core.dispatch_tick() == 1
    assert core.dispatch_tick() == 0
    with core.db.session() as session:
        assert session.get(Dispatch, approved).verified
        assert session.get(Target, "max").paused
        for ident in unrelated:
            dispatch = session.get(Dispatch, ident)
            assert dispatch.state == "pending" and dispatch.attempts == 0
        assert session.get(Dispatch, expired).state == "sending"


@pytest.mark.parametrize("origin,mode,actor", [
    ("postiz_max_bridge", "human", "human_editor:owner"),
    ("hourly_article_test", "legacy", "human_editor:owner"),
    ("hourly_article_test", "human", "hourly_article_policy"),
    ("hourly_article_test", "human", "owner"),
])
def test_dispatch_only_never_revives_legacy_approval_on_same_unpaused_target(
        human_cycle, monkeypatch, origin, mode, actor):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    ident = unrelated_dispatch(core, prepared, target_id="max", origin=origin, approval_mode=mode, actor=actor)
    forbid_broad_work(core, monkeypatch)
    assert core.dispatch_tick() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        assert dispatch.state == "pending" and dispatch.attempts == 0


def test_dispatch_only_expired_human_lease_becomes_unknown_without_retry(human_cycle):
    value, core, article, _, _, stamp, _ = human_cycle
    prepared = value.prepare(article)
    ident = human_approve(human_cycle, prepared)
    with core.db.session.begin() as session:
        dispatch = session.get(Dispatch, ident)
        dispatch.state, dispatch.owner, dispatch.lease_until = "sending", "old-worker", stamp[0] - 1
    stamp[0] += 181
    assert core.dispatch_tick() == 0
    with core.db.session() as session:
        dispatch = session.get(Dispatch, ident)
        assert dispatch.state == "unknown" and dispatch.attempts == 0
        assert dispatch.error == "SEND_LEASE_EXPIRED"


def test_dispatch_only_disabled_policy_does_not_even_poll_postiz(human_cycle, monkeypatch):
    _, core, _, _, _, _, policy = human_cycle
    policy["enabled"] = False
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    monkeypatch.setattr(core.bridge, "sync", lambda *a, **k: pytest.fail("Paused policy must not poll"))
    assert core.dispatch_tick() == 0


def test_dispatch_only_rejects_legacy_policy_without_work(human_cycle, monkeypatch):
    _, core, _, _, _, _, policy = human_cycle
    policy["approval_mode"] = "legacy"
    (core.settings.root / "data/hourly-article-policy.local.json").write_text(json.dumps(policy))
    monkeypatch.setattr(core.bridge, "sync", lambda *a, **k: pytest.fail("Legacy policy must not poll"))
    with pytest.raises(AgentError, match="HUMAN_REVIEW_CONFIRMATION_REQUIRED"):
        core.dispatch_tick()


def test_scoped_postiz_sync_preserves_other_target_links(human_cycle):
    value, core, article, _, _, _, _ = human_cycle
    prepared = value.prepare(article)
    unrelated_dispatch(core, prepared, target_id="other-max", route="max_browser")
    with core.db.session.begin() as session:
        other = session.scalar(select(Variant).where(Variant.target_id == "other-max"))
        session.add(BridgePost(post_id="not-in-scoped-list", integration_id="other-integration", group_id="other-group",
                               target_id="other-max", variant_id=other.id, remote_hash="old", remote_state="DRAFT",
                               due_at=0, active=True))
    core.bridge.sync(force=True, target_id="max")
    with core.db.session() as session:
        assert session.get(BridgePost, "not-in-scoped-list").active


@pytest.mark.parametrize("custom", [False, True])
def test_existing_embedded_worker_uses_explicit_callback_or_compatible_default(service, monkeypatch, custom):
    called = threading.Event()
    if custom:
        monkeypatch.setattr(service, "tick", lambda: pytest.fail("Full tick must not run"))
        callback = called.set
    else:
        monkeypatch.setattr(service, "tick", called.set)
        callback = None
    with TestClient(create_app(service, run_worker=True, legacy_image_worker=False, worker_tick=callback)):
        assert called.wait(7)


def test_hub_manual_tick_uses_same_limited_callback(human_cycle, monkeypatch):
    _, core, _, _, _, _, _ = human_cycle
    called = []
    monkeypatch.setattr(core, "tick", lambda: pytest.fail("Full tick must not run"))
    monkeypatch.setattr(core, "dispatch_tick", lambda: called.append("dispatch") or 0)
    with TestClient(hub_app(core, dispatch_only=True)) as client:
        response = client.post("/api/hub/tick", headers={"Authorization": "Bearer hub-test-owner"})
        assert response.status_code == 200 and response.json() == {"published": 0}
    assert called == ["dispatch"]


@pytest.mark.parametrize("paused", [False, True])
def test_serve_dispatch_only_flag_configures_existing_worker(tmp_path, monkeypatch, paused):
    from max_postiz_agent import hub_cli

    calls, closed = [], []
    service = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(hub_cli, "build", lambda *args, **kwargs: service)
    monkeypatch.setattr("max_postiz_agent.hub_api.hub_app", lambda *args, **kwargs: calls.append((args, kwargs)) or "app")
    monkeypatch.setattr("uvicorn.run", lambda *args, **kwargs: None)
    hub_cli.main(["--root", str(tmp_path), "serve", "--dispatch-only", *(["--no-worker"] if paused else [])])
    assert calls == [((service, not paused), {"dispatch_only": True})]
    assert closed == [True]
