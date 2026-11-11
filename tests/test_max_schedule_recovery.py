"""Просроченная постановка в очередь закрывается без вызова отправителя."""
import copy
from datetime import UTC, datetime

import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import BridgePost, Dispatch, TargetApproval
from max_postiz_agent.preapproval_retirement import mutation_blocked

pytest_plugins = ["test_hourly_reserve_controller"]


@pytest.mark.parametrize("applied", [False, True])
def test_504_waits_until_due_then_releases_even_with_postiz_offline(reserve_controller, monkeypatch, applied):
    from max_postiz_agent.schedule_recovery import ScheduleRecovery

    value = reserve_controller
    core = value.core
    recovery = ScheduleRecovery(core)
    monkeypatch.setattr("max_postiz_agent.schedule_recovery.now", lambda: value.stamp[0])
    monkeypatch.setattr(core.postiz, "schedule_journal_enabled", True)
    monkeypatch.setattr(core.postiz, "require_max_schedule_journal", lambda: None)
    monkeypatch.setattr(core.postiz, "get_post_fresh", lambda ident: copy.deepcopy(value.posts[ident]))
    schedule_calls, retire_calls = [], []
    online = False

    def schedule(operation):
        schedule_calls.append(operation)
        assert recovery.start(operation) == {"owner": operation.operation_id}
        if applied:
            value.posts[operation.post_id].update(state="QUEUE", publishDate=
                datetime.fromtimestamp(operation.due_ms / 1000, UTC).isoformat())
        raise AgentError("POSTIZ_UNKNOWN", 503)

    def retire(operation):
        retire_calls.append(operation)
        assert recovery.authorize_retirement(operation) == {"retired": True}
        if not online:
            raise AgentError("POSTIZ_UNKNOWN", 503)
        value.posts[operation.post_id].update(state="DRAFT", publishDate=
            datetime.fromtimestamp(operation.draft_due_ms / 1000, UTC).isoformat())
        return "draft_restored" if applied else "draft_preserved"

    def unexpected_send(*_args, **_kwargs):
        pytest.fail("a timed-out schedule must never call MAX")

    monkeypatch.setattr(core.postiz, "schedule_max_draft", schedule)
    monkeypatch.setattr(core.postiz, "retire_max_schedule", retire)
    monkeypatch.setattr(core.publisher, "publish", unexpected_send)
    value.controller.prepare_once()
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        value.controller.run_once()
    record = value.cycle._load()["records"][0]
    operation, = schedule_calls
    value.stamp[0] = record["due_at"] - 1
    recovery.run_once()
    assert value.store.status("max")["active"][0]["state"] == "blocked_unknown"
    assert not retire_calls
    with pytest.raises(AgentError, match="MAX_SCHEDULE_DEADLINE"):
        recovery.start(operation)

    value.stamp[0] = record["due_at"]
    recovery.run_once()
    closed = value.cycle._load()["records"][0]
    assert closed["state"] == "cancelled"
    assert closed["no_send_disposition"]["postiz_state"] == "draft_restore_pending"
    assert closed["url"] == record["url"]
    assert not value.store.status("max")["active"]
    assert value.store.get(operation.slot_id)["state"] == "missed"
    with core.db.session() as session:
        link = session.get(BridgePost, operation.post_id)
        assert link.mutation_owner is None and link.mutation_until == 0
        assert mutation_blocked(session, record["variant_id"])
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 0

    online = True
    value.stamp[0] += 31
    # The outbox survives a worker restart and a lost retirement response.
    recovery = ScheduleRecovery(core)
    recovery.run_once()
    recovery.run_once()
    assert schedule_calls == [operation] and retire_calls == [operation, operation]
    assert len(value.writes) == 1 and value.posts[operation.post_id]["state"] == "DRAFT"
    with core.db.session() as session:
        result = session.query(Audit).filter_by(action="max_schedule_draft_confirmed").one()
        assert result.details["outcome"] == ("draft_restored" if applied else "draft_preserved")


@pytest.mark.parametrize("blocker", ["foreign_lease", "approval"])
def test_expired_scheduling_does_not_clear_foreign_lease_or_approval(reserve_controller, monkeypatch, blocker):
    from max_postiz_agent.schedule_recovery import ScheduleRecovery

    value = reserve_controller
    core = value.core
    recovery = ScheduleRecovery(core)
    monkeypatch.setattr("max_postiz_agent.schedule_recovery.now", lambda: value.stamp[0])
    monkeypatch.setattr(core.postiz, "schedule_journal_enabled", True)
    monkeypatch.setattr(core.postiz, "require_max_schedule_journal", lambda: None)
    monkeypatch.setattr(core.postiz, "get_post_fresh", lambda ident: copy.deepcopy(value.posts[ident]))

    def schedule(operation):
        recovery.start(operation)
        with core.db.session.begin() as session:
            if blocker == "foreign_lease":
                session.get(BridgePost, operation.post_id).mutation_owner = "foreign"
            else:
                session.add(TargetApproval(variant_id=operation.variant_id, revision=1,
                    snapshot_hash="a" * 64, scheduled_at=operation.due_ms / 1000, actor="fixture"))
        raise AgentError("POSTIZ_UNKNOWN", 503)

    monkeypatch.setattr(core.postiz, "schedule_max_draft", schedule)
    value.controller.prepare_once()
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        value.controller.run_once()
    record = value.cycle._load()["records"][0]
    value.stamp[0] = record["due_at"] + 1
    recovery.run_once()
    assert value.store.status("max")["active"][0]["state"] == "blocked_unknown"
    with core.db.session() as session:
        assert session.get(BridgePost, record["postiz_id"]).mutation_owner
        assert not mutation_blocked(session, record["variant_id"])
