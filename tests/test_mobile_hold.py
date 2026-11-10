"""Ручной барьер не подменяет lease, exact approval и доказательство исхода."""

import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import pytest
from sqlalchemy import select

from max_postiz_agent.browser_worker import authorize_dispatch
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, now
from max_postiz_agent.hub_models import BridgePost, Dispatch, Target, TargetApproval
from max_postiz_agent.mobile_hold import (
    apply_item_hold,
    capture_item_hold,
    item_hold,
    verify_item_hold,
)
from max_postiz_agent.server_models import EditorialControl
from max_postiz_agent.server_state import CONTROL_ID, ServerState
from test_hub import approve, variants
from test_max_bridge import approve as approve_max
from test_max_bridge import import_variant

pytest_plugins = ["test_max_bridge", "test_route_dispatch_postgres"]


def capture(core, item, action="hold_item"):
    with core.db.session() as session:
        return capture_item_hold(core, session, item, action)


def apply(core, plan, observation):
    with core.db.session.begin() as session:
        return apply_item_hold(
            core, session, plan, observation, actor="mobile:test", reason="Проверка редакции"
        )


def running(core):
    ServerState(core).initialize()
    with core.db.session.begin() as session:
        session.get(EditorialControl, CONTROL_ID).paused = False


@pytest.fixture
def held_max_fixture(bridge, monkeypatch):
    core, records, integrations, sent, _ = bridge
    due = now() + 120
    records["post-max"]["publishDate"] = datetime.fromtimestamp(due, UTC).isoformat()
    monkeypatch.setattr(core.postiz, "get_post_fresh", core.postiz.get_post)
    monkeypatch.setattr(core.postiz, "integrations_fresh", core.postiz.integrations)
    variant = import_variant(bridge)
    dispatch = approve_max((core, records, integrations, sent, due), variant)
    running(core)
    return core, records, integrations, sent, due, variant, dispatch


def test_max_hold_preserves_queue_and_approval_and_release_is_audited(
    held_max_fixture, monkeypatch
):
    core, records, _, sent, due, variant, dispatch = held_max_fixture
    plan = capture(core, variant)
    observation = verify_item_hold(core, plan)
    with core.db.session() as session:
        assert not item_hold(session, variant)["active"]
        previous_snapshot = session.get(Dispatch, dispatch).snapshot_hash
    held = apply(core, plan, observation)
    assert held["active"] and held["revision"] == 1
    assert held["delivery_path"] == "postiz_max_browser"
    with pytest.raises(AgentError, match="MOBILE_HOLD_STATE_CONFLICT"):
        apply(core, plan, observation)
    token = core.bridge._seal(
        {"kind": "channel", "target": "max", "destination": "-123", "expires": due}
    )
    assert core.bridge.submit(token, "integration-max", "post-max")["state"] == "held"
    with pytest.raises(AgentError, match="MOBILE_ITEM_HELD"):
        core.bridge.mutation(["post-max"])
    with monkeypatch.context() as clock:
        clock.setattr("max_postiz_agent.hub.now", lambda: due + 1)
        assert core.publish_due() == 0
    assert sent == [] and records["post-max"]["state"] == "QUEUE"
    release = capture(core, variant, "release_item_hold")
    state = ServerState(core)
    state.control("pause", state.read()["revision"], "fixture-owner")
    with pytest.raises(AgentError, match="MOBILE_HOLD_REVISION_CONFLICT"):
        apply(core, release, verify_item_hold(core, release))
    release = capture(core, variant, "release_item_hold")
    released = apply(core, release, verify_item_hold(core, release))
    assert not released["active"] and released["revision"] == 2 and sent == []
    with core.db.session() as session:
        current = session.get(Dispatch, dispatch)
        assert (
            current.state == "pending"
            and current.attempts == 0
            and current.snapshot_hash == previous_snapshot
        )
        assert session.get(TargetApproval, current.approval_id).revoked is False
        assert (
            session.get(EditorialControl, CONTROL_ID).paused and session.get(Target, "max").paused
        )
        assert session.get(Audit, held["audit_id"]).action == "hold_item"
        assert session.get(Audit, released["audit_id"]).details["external_mutations"] == 0


@pytest.mark.parametrize("change", ["unknown", "sending", "orphan_lease", "previous_attempt"])
def test_max_hold_rejects_local_uncertainty(held_max_fixture, change):
    core, _, _, _, _, variant, dispatch = held_max_fixture
    with core.db.session.begin() as session:
        row = session.get(Dispatch, dispatch)
        if change == "orphan_lease":
            session.get(BridgePost, "post-max").mutation_until = 1
        elif change == "previous_attempt":
            row.attempts = 1
        else:
            row.state = change
    with pytest.raises(AgentError, match="MOBILE_HOLD_NOT_SAFE"):
        capture(core, variant)


def test_hold_cannot_claim_non_max_provider_or_changed_remote(held_max_fixture):
    core, records, integrations, sent, _, variant, _ = held_max_fixture
    plan = capture(core, variant)
    integrations[0]["identifier"] = "telegram"
    with pytest.raises(AgentError, match="MOBILE_HOLD_PROVIDER_UNVERIFIED"):
        verify_item_hold(core, plan)
    integrations[0]["identifier"] = "max-browser"
    records["post-max"]["releaseURL"] = "https://example.test/existing"
    with pytest.raises(AgentError, match="MOBILE_HOLD_REMOTE_CHANGED"):
        verify_item_hold(core, plan)
    assert sent == []
    with core.db.session() as session:
        assert not item_hold(session, variant)["active"]


def test_browser_preclick_still_denies_a_held_dispatch_if_a_worker_has_a_lease(
    held_max_fixture, monkeypatch
):
    core, _, _, sent, due, variant, dispatch = held_max_fixture
    plan = capture(core, variant)
    apply(core, plan, verify_item_hold(core, plan))
    with core.db.session.begin() as session:
        # Defend at the pre-click checkpoint independently of queue selection.
        row = session.get(Dispatch, dispatch)
        row.state, row.owner, row.lease_until = "sending", "fixture-worker", due + 300
    monkeypatch.setattr("max_postiz_agent.db.now", lambda: due + 1)
    with pytest.raises(AgentError, match="MOBILE_ITEM_HELD"):
        authorize_dispatch(core.settings, core.db, {"request_id": dispatch})
    assert sent == []


def test_native_lease_generation_invalidates_stale_hold_capture(bridge, monkeypatch):
    core, records, _, _, _ = bridge
    records["post-max"]["state"] = "DRAFT"
    monkeypatch.setattr(core.postiz, "get_post_fresh", core.postiz.get_post)
    monkeypatch.setattr(core.postiz, "integrations_fresh", core.postiz.integrations)
    variant = import_variant(bridge)
    running(core)
    plan = capture(core, variant)
    observation = verify_item_hold(core, plan)
    lease = core.bridge.mutation(["post-max"])
    core.bridge.mutation(["post-max"], release=lease["owner"])
    with pytest.raises(AgentError, match="MOBILE_HOLD_REVISION_CONFLICT"):
        apply(core, plan, observation)


@pytest.mark.parametrize("platform", ["telegram", "vk", "pikabu"])
def test_local_direct_hold_never_performs_postiz_reads_and_keeps_expired_dispatch_blocked(
    four_hub, platform, monkeypatch
):
    core = four_hub
    due = now() + 120
    variant = variants(core)[platform]
    dispatch = approve(core, variant, due)
    running(core)

    def unexpected(*args, **kwargs):
        pytest.fail("A direct route must not use Postiz")

    monkeypatch.setattr(core.postiz, "get_post_fresh", unexpected)
    monkeypatch.setattr(core.postiz, "integrations_fresh", unexpected)
    plan = capture(core, variant)
    assert apply(core, plan, verify_item_hold(core, plan))["delivery_path"] == "project_dispatch"
    monkeypatch.setattr("max_postiz_agent.hub.now", lambda: due + 1)
    monkeypatch.setattr("max_postiz_agent.mobile_hold.now", lambda: due + 1)
    assert core.publish_due(platform_routes_only=True) == 0
    with pytest.raises(AgentError, match="MOBILE_HOLD_DUE_ELAPSED"):
        capture(core, variant, "release_item_hold")
    assert core.fixture_publishers[platform].publications == []
    with core.db.session() as session:
        assert session.get(Dispatch, dispatch).state == "pending"
        assert session.get(Target, platform).paused is False
        assert item_hold(session, variant)["active"]


def test_postgres_live_worker_claim_wins_over_older_hold_plan(postgres_routes, monkeypatch):
    core = postgres_routes
    variant = variants(core)["telegram"]
    due = now() + 120
    dispatch = approve(core, variant, due)
    running(core)
    plan = capture(core, variant)
    observation = verify_item_hold(core, plan)
    monkeypatch.setattr("max_postiz_agent.hub.now", lambda: due + 1)
    monkeypatch.setattr("max_postiz_agent.db.now", lambda: due + 1)
    entered, leave = threading.Event(), threading.Event()
    original_send = core._send

    def delay_send(row, target):
        entered.set()
        assert leave.wait(10)
        return original_send(row, target)

    monkeypatch.setattr(core, "_send", delay_send)
    with ThreadPoolExecutor(max_workers=1) as pool:
        worker = pool.submit(core.publish_due, platform_routes_only=True)
        try:
            assert entered.wait(10)
            with pytest.raises(AgentError, match="MOBILE_HOLD_NOT_SAFE"):
                apply(core, plan, observation)
            with core.db.session() as session:
                assert not item_hold(session, variant)["active"]
                assert session.get(Dispatch, dispatch).state == "sending"
                assert session.scalar(select(Audit.id).where(Audit.action == "hold_item")) is None
        finally:
            leave.set()
        assert worker.result(10) == 1
