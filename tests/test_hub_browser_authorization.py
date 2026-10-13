"""The real browser worker must honor hub approvals, not require legacy intents."""
import pytest
from sqlalchemy import select

from max_postiz_agent.browser_worker import authorize
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Intent, now
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval
from max_postiz_agent.max_bridge import MaxBridge
from test_hub import approve, variants

pytest_plugins = ["test_hub"]


@pytest.fixture
def browser_dispatch(hub):
    with hub.db.session.begin() as s:
        s.get(Target, "max").route = "max_browser"
    ident = variants(hub)["max"]
    dispatch_id = approve(hub, ident)
    with hub.db.session.begin() as s:
        d = s.get(Dispatch, dispatch_id)
        d.state, d.owner, d.lease_until = "sending", "fixture-worker", now() + 60
        payload = {"request_id": d.id, "chat_id": "-123", "text": d.snapshot["body"], "attachment": None}
        assert s.scalar(select(Intent)) is None
    return hub, dispatch_id, payload


def test_hub_approval_authorizes_real_browser_worker_without_legacy_intent(browser_dispatch):
    core, _, payload = browser_dispatch
    authorize(core.settings, payload)


@pytest.mark.parametrize("change", [{"text": "Подменённый текст"}, {"chat_id": "-999"},
                                  {"attachment": {"type": "browser-image", "payload": {}}}])
def test_hub_worker_rejects_changed_payload(browser_dispatch, change):
    core, _, payload = browser_dispatch
    with pytest.raises(AgentError, match="BROWSER_DISPATCH_NOT_APPROVED"):
        authorize(core.settings, {**payload, **change})


@pytest.mark.parametrize("state", ["pending", "unknown", "published"])
def test_hub_worker_requires_live_sending_lease(browser_dispatch, state):
    core, ident, payload = browser_dispatch
    with core.db.session.begin() as s:
        s.get(Dispatch, ident).state = state
    with pytest.raises(AgentError, match="BROWSER_DISPATCH_NOT_APPROVED"):
        authorize(core.settings, payload)


def test_hub_worker_rejects_expired_lease(browser_dispatch):
    core, ident, payload = browser_dispatch
    with core.db.session.begin() as s:
        s.get(Dispatch, ident).lease_until = now() - 1
    with pytest.raises(AgentError, match="BROWSER_DISPATCH_NOT_APPROVED"):
        authorize(core.settings, payload)


def test_hub_worker_rechecks_pause_and_revocation(browser_dispatch):
    core, ident, payload = browser_dispatch
    core.pause_target("max", True)
    with pytest.raises(AgentError, match="TARGET_PAUSED"):
        authorize(core.settings, payload)
    core.pause_target("max", False)
    with core.db.session.begin() as s:
        d = s.get(Dispatch, ident)
        s.get(TargetApproval, d.approval_id).revoked = True
    with pytest.raises(AgentError, match="APPROVAL_STALE"):
        authorize(core.settings, payload)


def test_hub_worker_rechecks_postiz_before_click(browser_dispatch, monkeypatch):
    core, _, payload = browser_dispatch
    calls = []
    def blocked(self, dispatch, target):
        calls.append(dispatch.id)
        raise AgentError("BRIDGE_REMOTE_CHANGED")
    monkeypatch.setattr(MaxBridge, "guard_send", blocked)
    with pytest.raises(AgentError, match="BRIDGE_REMOTE_CHANGED"):
        authorize(core.settings, payload)
    assert calls == [payload["request_id"]]
