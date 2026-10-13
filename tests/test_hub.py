"""Workflow boundaries: frozen approvals, per-platform results and unknown sends."""
import json

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from max_postiz_agent.adapters import Delivery
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import now
from max_postiz_agent.hub import HubService
from max_postiz_agent.hub_api import hub_app
from max_postiz_agent.hub_models import Dispatch, Target, Variant
from max_postiz_agent.json_source import collect_json
from max_postiz_agent.postiz import PostizClient
from conftest import write_topic
from test_workflow import seed


@pytest.fixture
def hub(service, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "hub-test-owner")
    monkeypatch.setenv("APP_VIEWER_SECRET", "hub-test-viewer")
    topic = service.settings.topic
    topic["channels"][0]["publication"]["mode"] = "review"
    write_topic(service.settings.root, topic)
    from max_postiz_agent.config import Settings
    service.activate(Settings.load(service.settings.root))
    config = {"schema_version": "1.0", "postiz": {}, "tasks": [], "targets": [
        {"id": "max", "profile_id": "main", "platform": "max", "route": "max_api", "enabled": True,
         "destination": "-123", "options": {"max_chars": 3500}},
        {"id": "pikabu", "profile_id": "main", "platform": "pikabu", "route": "manual", "enabled": True,
         "destination": "manual:owner", "options": {"max_chars": 15000}}]}
    (service.settings.root / "config/publishing.json").write_text(json.dumps(config), encoding="utf-8")
    value = HubService(service.settings, service.db, editor=service.editor, publisher=service.publisher, http=service.http)
    value.activate()
    for ident in ("max", "pikabu"):
        value.pause_target(ident, False)
    yield value
    value.postiz.close()


def variants(hub):
    draft_id = hub.edit_story("main", seed(hub))
    ids = hub.make_variants(draft_id)
    with hub.db.session() as s:
        return {s.get(Variant, ident).target_id: ident for ident in ids}


def approve(hub, ident, due=None):
    with hub.db.session() as s:
        v = s.get(Variant, ident)
        t = s.get(Target, v.target_id)
        revision, hashed = v.revision, fingerprint(hub.snapshot(v, t))
    return hub.approve_variant(ident, revision, hashed, due or now() - 1)


def test_targets_and_repeated_variants(hub):
    ids = variants(hub)
    with hub.db.session() as s:
        draft_id = s.get(Variant, ids["max"]).draft_id
    assert set(hub.make_variants(draft_id)) == set(ids.values())


def test_revision_revokes_approval_and_cancels_dispatch(hub):
    ident = variants(hub)["max"]
    dispatch = approve(hub, ident)
    with hub.db.session() as s:
        v = s.get(Variant, ident)
        body = v.body
    hub.revise_variant(ident, 1, body + "\nРедакторская правка.")
    with hub.db.session() as s:
        assert s.get(Dispatch, dispatch).state == "cancelled"
    assert hub.publish_due() == 0


def test_stale_hash_and_profile_blocked(hub):
    ident = variants(hub)["max"]
    with pytest.raises(AgentError, match="REVISION_CONFLICT"):
        hub.approve_variant(ident, 1, "wrong", now())
    hub.pause_target("max", True)
    with pytest.raises(AgentError, match="TARGET_PAUSED"):
        approve(hub, ident)


def test_partial_success_and_no_resend(hub, monkeypatch):
    ids = variants(hub)
    dispatched = {key: approve(hub, ident) for key, ident in ids.items()}
    calls = []
    monkeypatch.setattr(hub.publisher, "publish", lambda *a, **k: (calls.append(a), Delivery("confirmed", "msg"))[1])
    assert hub.publish_due() == 1
    assert hub.publish_due() == 0
    assert len(calls) == 1
    with hub.db.session() as s:
        assert s.get(Dispatch, dispatched["max"]).state == "published"
        assert not s.get(Dispatch, dispatched["max"]).verified
        d = s.get(Dispatch, dispatched["pikabu"])
        assert d.state == "awaiting_manual"
        assert (hub.settings.root / f"data/exports/{d.id}/post.json").exists()


def test_unknown_send_is_never_repeated(hub, monkeypatch):
    ident = variants(hub)["max"]
    dispatched = approve(hub, ident)
    calls = []
    monkeypatch.setattr(hub.publisher, "publish", lambda *a, **k: (calls.append(a), Delivery("unknown"))[1])
    hub.publish_due()
    hub.publish_due()
    assert len(calls) == 1
    with hub.db.session() as s:
        assert s.get(Dispatch, dispatched).state == "unknown"
    with pytest.raises(AgentError, match="CANCEL_OR_RECONCILE_FIRST"):
        hub.revise_variant(ident, 1, "Правка")


def test_expired_lease_stays_unknown(hub):
    dispatched = approve(hub, variants(hub)["max"])
    with hub.db.session.begin() as s:
        d = s.get(Dispatch, dispatched)
        d.state, d.lease_until = "sending", now() - 1
    hub.publish_due()
    with hub.db.session() as s:
        assert s.get(Dispatch, dispatched).state == "unknown"


def test_manual_attestation_and_stale_export(hub):
    dispatched = approve(hub, variants(hub)["pikabu"])
    hub.publish_due()
    with hub.db.session() as s:
        d = s.get(Dispatch, dispatched)
        revision, hashed = d.revision, d.snapshot_hash
    with pytest.raises(AgentError, match="PIKABU_STORY_URL_INVALID"):
        hub.attest_manual(dispatched, revision, hashed, "https://evil.org/story/example_123")
    with pytest.raises(AgentError, match="MANUAL_CONFIRMATION_STALE"):
        hub.attest_manual(dispatched, revision, "old", "https://pikabu.ru/story/example_123")
    result = hub.attest_manual(dispatched, revision, hashed, "https://pikabu.ru/story/example_123")
    assert result == {"state": "owner_attested", "live_verified": False}


def test_api_roles_session_and_history(hub):
    ident = variants(hub)["max"]
    client = TestClient(hub_app(hub))
    assert client.get("/api/hub/state").status_code == 401
    viewer = {"Authorization": "Bearer hub-test-viewer"}
    assert client.post("/api/hub/targets/max/pause", headers=viewer, json={"paused": True}).status_code == 403
    owner = {"Authorization": "Bearer hub-test-owner"}
    assert client.post("/api/v1/session/login", headers=owner).status_code == 200
    assert client.get("/api/hub/state").status_code == 200
    assert client.get(f"/api/hub/variants/{ident}/history").json()[0]["revision"] == 1
    assert client.post("/api/hub/targets/max/pause", headers={"Origin": "https://evil.org"}, json={"paused": True}).status_code == 403
    assert client.post("/api/v1/session/logout", headers={"Origin": "http://testserver"}).status_code == 200
    assert client.get("/api/hub/state").status_code == 401


def test_json_api_offset_and_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("SOURCE_KEY", "test-secret")
    calls = []
    class HTTP:
        def get(self, url, headers):
            calls.append((url, headers))
            rows = [{"id": n, "title": "Новость", "text": "Текст новости", "url": f"https://example.org/{n}",
                     "date": "2026-10-11T12:00:00Z"} for n in range(3)]
            return 200, json.dumps({"items": rows}).encode(), {}, url
    source = {"endpoint": "https://example.org/api", "access_ref": "SOURCE_KEY", "max_items_per_run": 2,
              "language_hint": "ru", "api": {"items_path": "items", "fields": {"id": "id", "text": "text",
              "title": "title", "url": "url", "published_at": "date"}}}
    first, cursor = collect_json(source, {}, HTTP(), tmp_path)
    second, _ = collect_json(source, cursor, HTTP(), tmp_path)
    assert [i.external_id for i in first + second] == ["0", "1", "2"]
    assert calls[0][1]["Authorization"] == "Bearer test-secret"


def test_postiz_draft_payload_and_unknown(monkeypatch):
    monkeypatch.setenv("POSTIZ_BASE_URL", "http://127.0.0.1:9999/public/v1")
    monkeypatch.setenv("POSTIZ_API_KEY", "test-secret")
    captured = []
    def respond(request):
        captured.append(request)
        return httpx.Response(200, json=[{"postId": "draft-1", "integration": "channel"}])
    client = PostizClient({}, httpx.MockTransport(respond))
    target = Target(platform="telegram", destination="channel", options={})
    assert client.create(target, "Текст", []) == "draft-1"
    body = json.loads(captured[0].content)
    assert body["type"] == "draft" and body["posts"][0]["settings"]["__type"] == "telegram"
    client.close()
    def timeout(request):
        raise httpx.ReadTimeout("opaque", request=request)
    client = PostizClient({}, httpx.MockTransport(timeout))
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        client.create(target, "Текст", [])
    client.close()


def test_postiz_schedule_requires_verified_guard(hub):
    ident = variants(hub)["max"]
    with hub.db.session.begin() as s:
        t = s.get(Target, "max")
        t.route = "postiz"
    approve(hub, ident)
    hub.publish_due()
    with hub.db.session() as s:
        d = s.scalar(select(Dispatch).where(Dispatch.variant_id == ident))
        assert d.state == "failed" and d.error == "POSTIZ_APPROVAL_GUARD_REQUIRED"


def test_archived_changed_article_preserves_versions(hub):
    variants(hub)
    hub.ingest("example_rss", [__import__('max_postiz_agent.collectors', fromlist=['Item']).Item(
        "article", "Обновление", "Видео длится 15 секунд.", "https://example.org/a", now(), "ru")])
    assert len(hub.search_archive("секунд")) == 2


def test_paused_target_does_not_block_other_target(hub):
    ids = variants(hub)
    approve(hub, ids["max"])
    pikabu = approve(hub, ids["pikabu"])
    hub.pause_target("max", True)
    hub.publish_due()
    with hub.db.session() as s:
        assert s.get(Dispatch, pikabu).state == "awaiting_manual"
