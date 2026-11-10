"""The mobile domain exercises exact recovery without publishing or production access."""

from hashlib import sha256
import json
import os
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.schema import CreateSchema, DropSchema

from max_postiz_agent.adapters import Delivery
from max_postiz_agent.browser_publisher import BrowserMax
from max_postiz_agent.browser_worker import _snapshot_request
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Audit, Base, Database, Draft, now
from max_postiz_agent.hub import HubService
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval, Variant
from max_postiz_agent.mobile_domain import MobileDomain
from max_postiz_agent.server_state import ServerState

ACTOR = "postiz:mobile:" + "a" * 32
START = "2000-01-01T00:00:00Z"
END = "2000-01-31T00:00:00Z"
DUE = 946771200


class FakeCore:
    _expire_send_leases = HubService._expire_send_leases
    _expire_send_lease = HubService._expire_send_lease
    _target = HubService._target
    _variant = HubService._variant
    snapshot = HubService.snapshot

    def __init__(self, tmp_path, engine):
        Base.metadata.create_all(engine)
        self.db = SimpleNamespace(session=sessionmaker(engine))
        self.db.audit = lambda *args, **kwargs: Database.audit(self.db, *args, **kwargs)
        self.settings = SimpleNamespace(root=tmp_path, hash="a" * 64, app={"max": {"adapter_mode": "browser"}})
        self.bridge = SimpleNamespace(enabled=False)
        self.postiz = SimpleNamespace(posts=lambda *_: [])
        self.publisher = BrowserMax(self.settings)
        self.routes = SimpleNamespace()

    def _assert_current_route_runtime(self):
        return None


@pytest.fixture(params=["sqlite", "postgres"] if os.getenv("TEST_DATABASE_URL") else ["sqlite"])
def mobile(tmp_path, request):
    engine = create_engine(os.environ["TEST_DATABASE_URL"] if request.param == "postgres" else "sqlite://")
    schema = None
    try:
        if request.param == "postgres":
            assert engine.dialect.name == "postgresql"
            schema = "test_mobile_" + uuid4().hex
            with engine.begin() as conn:
                conn.execute(CreateSchema(schema))
            isolated = engine.execution_options(schema_translate_map={None: schema})
        else:
            isolated = engine
        core = FakeCore(tmp_path, isolated)
        ServerState(core).initialize()
        yield MobileDomain(core)
    finally:
        if schema:
            with engine.begin() as conn:
                conn.execute(DropSchema(schema, cascade=True, if_exists=True))
        engine.dispose()


def _seed(mobile, platform, route, state="unknown", tag="", due=DUE):
    due = float(due)
    suffix = tag or platform
    ids = {key: key + "-" + suffix for key in ("target", "draft", "variant", "approval", "dispatch")}
    core = mobile.core
    with core.db.session.begin() as session:
        target = Target(id=ids["target"], profile_id="science", platform=platform,
                        route=route, enabled=True, paused=True, options={},
                        destination=suffix, binding_hash="b" * 64)
        session.add(target)
        session.add(Draft(id=ids["draft"], channel_id="science", story_id=suffix, current={},
                          config_id=core.settings.hash))
        session.flush()
        variant = Variant(id=ids["variant"], draft_id=ids["draft"], target_id=target.id,
                          revision=1, source_revision=1, title=suffix, body="Утверждённый текст",
                          media=[], sources=[], metadata_json={}, state=state,
                          config_hash=core.settings.hash, binding_hash=target.binding_hash)
        session.add(variant)
        session.flush()
        snapshot = core.snapshot(variant, target)
        snapshot_hash = fingerprint({"content": snapshot, "due_at": due})
        session.add(TargetApproval(id=ids["approval"], variant_id=variant.id, revision=1,
                                   snapshot_hash=snapshot_hash, scheduled_at=due, actor="owner"))
        session.flush()
        session.add(Dispatch(id=ids["dispatch"], variant_id=variant.id, approval_id=ids["approval"],
                             revision=1, snapshot_hash=snapshot_hash, snapshot=snapshot,
                             state=state, due_at=due, attempts=1))
    return ids, snapshot


def test_queue_calendar_and_targets_preserve_orthogonal_states(mobile):
    vk, _ = _seed(mobile, "vk", "vk_browser", state="pending")
    telegram, _ = _seed(mobile, "telegram", "telegram_api")
    queue = mobile.read("queue", filters={"state": "unresolved"})
    assert [row["id"] for row in queue["items"]] == [telegram["variant"]]
    assert queue["items"][0]["dispatch_state"] == "unknown"
    assert queue["items"][0]["postiz_state"] is None
    assert queue["items"][0]["available_actions"] == ["reconcile_dispatch"]
    calendar = mobile.read("calendar", filters={"from": START, "to": END, "limit": "1"})
    assert calendar["postiz_available"] is True
    assert calendar["items"][0]["id"] == telegram["variant"]
    assert calendar["next_cursor"]
    other = mobile.read("calendar", filters={"from": START, "to": END, "cursor": calendar["next_cursor"]})
    assert [row["id"] for row in other["items"]] == [vk["variant"]]
    with pytest.raises(AgentError, match="MOBILE_CURSOR_INVALID"):
        mobile.read("calendar", filters={"from": START, "to": END, "state": "pending",
                                         "cursor": calendar["next_cursor"]})
    for target in mobile.read("targets", filters={})["items"]:
        assert target["available_actions"] == []
        assert target["blocked_reasons"] == ["EDITORIAL_PAUSED"]


def test_gateway_without_cgroup_exposes_browser_blocker_only(mobile):
    vk, _ = _seed(mobile, "vk", "vk_browser")
    pikabu, _ = _seed(mobile, "pikabu", "pikabu_browser")
    telegram, _ = _seed(mobile, "telegram", "telegram_api")
    gateway = MobileDomain(mobile.core, unavailable_browser_routes={"vk_browser", "pikabu_browser"})
    for ids in (vk, pikabu):
        item = gateway.read("queue", object_id=ids["variant"], filters={})
        assert "reconcile_dispatch" not in item["available_actions"]
        assert "BROWSER_WORKER_CGROUP_UNAVAILABLE" in item["blocked_reasons"]
        with pytest.raises(AgentError, match="BROWSER_WORKER_CGROUP_UNAVAILABLE"):
            gateway.plan("reconcile_dispatch", ids["dispatch"])
    assert gateway.plan("reconcile_dispatch", telegram["dispatch"])["effect"] == "reconcile_dispatch"


@pytest.mark.parametrize(("platform", "route"), [
    ("telegram", "telegram_api"), ("vk", "vk_browser"), ("pikabu", "pikabu_browser"),
])
def test_direct_platform_readback_uses_exact_snapshot(mobile, platform, route):
    ids, snapshot = _seed(mobile, platform, route)
    seen = []

    def readback(dispatch, target):
        seen.append((dispatch.id, target.route))
        assert dispatch.snapshot == snapshot
        return Delivery("confirmed", "1", verified=True,
                        evidence={"fixture": False, "verification": "browser_readback",
                                  "independent_readback": True})

    mobile.core.routes.reconcile = readback
    plan = mobile.plan("reconcile_dispatch", ids["dispatch"])
    result = mobile.execute("reconcile_dispatch", ids["dispatch"], plan, actor=ACTOR,
                            operation_id=str(uuid4()), reason="")
    assert result["verified"] is True
    assert result["outcome"] == "delivery_confirmed"
    assert result["external_mutations"] == 0
    assert seen == [(ids["dispatch"], route)]
    detail = mobile.read("dispatches", object_id=ids["dispatch"], filters={})
    assert detail["delivery_evidence"]["kind"] == "independent_readback"
    assert detail["attempts"][0]["result"] == "published"


@pytest.mark.parametrize(("kind", "verified", "receipt"), [
    ("fixture", True, {"fixture": True, "verification": "browser_readback", "independent_readback": True}),
    ("api_receipt", False, {"fixture": False, "verification": "api_receipt", "independent_readback": False}),
])
def test_unverified_direct_confirmation_does_not_release_unknown(mobile, kind, verified, receipt):
    ids, _ = _seed(mobile, "telegram", "telegram_api")
    mobile.core.routes.reconcile = lambda *_: Delivery("confirmed", "1", verified=verified, evidence=receipt)
    plan = mobile.plan("reconcile_dispatch", ids["dispatch"])
    result = mobile.execute("reconcile_dispatch", ids["dispatch"], plan, actor=ACTOR,
                            operation_id=str(uuid4()), reason="")
    assert result["outcome"] == "proof_insufficient" and result["verified"] is False
    assert result["dispatch_state"] == "unknown" and result["evidence_kind"] == kind
    with mobile.core.db.session() as session:
        dispatch = session.get(Dispatch, ids["dispatch"])
        assert dispatch.state == "unknown" and dispatch.external_id is None and not dispatch.verified
        assert session.get(Variant, ids["variant"]).state == "unknown"


def _max_journal(mobile, ids, snapshot):
    core = mobile.core
    ident = ids["dispatch"]
    base, presentation = _snapshot_request(core.settings, ident, snapshot, "max")
    payload = {**base, **presentation, "dispatch_snapshot_hash": fingerprint(snapshot)}
    path = core.settings.root / "data/browser-publications" / (ident + ".json")
    path.parent.mkdir(parents=True)
    journal = {"contract": "browser-send-journal-v1", "state": "unknown", "send_started": True,
               "request_id": ident, "chat_id": "max", "config_hash": core.settings.hash,
               "snapshot_hash": fingerprint(snapshot), "base_payload": base,
               "base_payload_hash": fingerprint(base), "request_payload": payload,
               "content_hash": fingerprint(payload), "presentation": presentation,
               "text": base["text"], "body_sha256": sha256(base["text"].encode()).hexdigest(),
               "request_attachment": None, "attachment": False, "attachment_type": None,
               "before_count": 0, "started_at": now()}

    def save():
        path.write_text(json.dumps(journal), encoding="utf-8")

    save()
    return journal, save


def test_max_requires_native_journal_before_promoting_unknown(mobile, monkeypatch):
    ids, snapshot = _seed(mobile, "max", "max_browser")
    journal, save = _max_journal(mobile, ids, snapshot)
    calls = []

    def readback(action, *, chat_id, request_id):
        calls.append(action)
        assert chat_id == "max" and request_id == ids["dispatch"]
        result = {"state": "confirmed", "external_id": "browser-" + request_id, "permalink": None}
        journal.update(state="confirmed", delivery=result)
        save()
        return result

    monkeypatch.setattr(mobile.core.publisher, "call", readback)
    plan = mobile.plan("reconcile_dispatch", ids["dispatch"])
    result = mobile.execute("reconcile_dispatch", ids["dispatch"], plan, actor=ACTOR,
                            operation_id=str(uuid4()), reason="")
    assert result["dispatch_state"] == "published" and result["verified"] is True
    assert calls == ["reconcile"]
    with mobile.core.db.session() as session:
        assert session.get(Dispatch, ids["dispatch"]).verified
        audit = session.scalar(select(Audit).where(Audit.action == "route_reconcile_read_only"))
        assert audit.actor == ACTOR and audit.details["evidence"]["journal_sha256"]


def test_max_refuses_corrupt_journal_and_retains_unknown(mobile, monkeypatch):
    ids, snapshot = _seed(mobile, "max", "max_browser")
    journal, save = _max_journal(mobile, ids, snapshot)
    plan = mobile.plan("reconcile_dispatch", ids["dispatch"])
    journal["text"] = "другой материал"
    save()
    monkeypatch.setattr(mobile.core.publisher, "call", lambda *a, **k: pytest.fail("No external read"))
    with pytest.raises(AgentError, match="MOBILE_MAX_READBACK_PROOF_INVALID"):
        mobile.execute("reconcile_dispatch", ids["dispatch"], plan, actor=ACTOR,
                       operation_id=str(uuid4()), reason="")
    with mobile.core.db.session() as session:
        assert session.get(Dispatch, ids["dispatch"]).state == "unknown"


def test_stale_pause_plan_refuses_second_actor_update(mobile):
    current = mobile.store.read()
    mobile.store.control("start", current["revision"], ACTOR)
    plan = mobile.plan("pause_project", "main")
    with mobile.core.db.session.begin() as session:
        from max_postiz_agent.server_models import EditorialControl
        session.get(EditorialControl, "main").revision += 1
    with pytest.raises(AgentError, match="MOBILE_PLAN_CHANGED"):
        mobile.execute("pause_project", "main", plan, actor=ACTOR, operation_id=str(uuid4()), reason="")
    assert mobile.store.read()["paused"] is False
