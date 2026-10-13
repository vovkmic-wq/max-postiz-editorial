from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from max_postiz_agent import image_schedule as schedule
from max_postiz_agent.api import create_app
from max_postiz_agent.config import AgentError


START = datetime(2026, 10, 11, 9, 30, tzinfo=schedule.MOSCOW)
DUE = START.replace(hour=12, minute=0)


# Проверяем сценарий: schedule starts at next boundary and does not catch up.
def test_schedule_starts_at_next_boundary_and_does_not_catch_up(project):
    result = schedule.configure(project, True, at=START)
    assert result["next_run_at"] == DUE.isoformat()
    calls = []
    # Операция runner: данного модуля.
    def runner(root, at):
        calls.append(at)
        return {"state": "confirmed", "intent_id": "test"}
    assert schedule.tick(project, at=START, runner=runner)["state"] == "not_due"
    assert calls == []
    assert schedule.tick(project, at=DUE, runner=runner)["state"] == "confirmed"
    assert schedule.tick(project, at=DUE + timedelta(minutes=30), runner=runner)["state"] == "already_checked"
    assert len(calls) == 1
    # After a long offline period, only the current interval is run.
    following = DUE + timedelta(days=1, hours=8)
    schedule.tick(project, at=following, runner=runner)
    assert calls == [DUE, following]


# Проверяем сценарий: error blocks all following slots and exception text is not saved.
def test_error_blocks_all_following_slots_and_exception_text_is_not_saved(project):
    schedule.configure(project, True, at=START)
    # Операция failed: данного модуля.
    def failed(root, at):
        raise RuntimeError("secret-in-transport-exception")
    result = schedule.tick(project, at=DUE, runner=failed)
    assert result["schedule_state"] == "requires_attention"
    assert "secret" not in str(schedule.status(project))
    again = schedule.tick(project, at=DUE + timedelta(hours=4), runner=lambda *a: pytest.fail("retried"))
    assert again["state"] == "requires_attention"
    resumed = schedule.configure(project, True, at=DUE + timedelta(hours=4))
    assert resumed["next_run_at"] == (DUE + timedelta(hours=8)).isoformat()


# Проверяем сценарий: saved running process requires explicit recovery.
def test_saved_running_process_requires_explicit_recovery(project):
    schedule.configure(project, True, at=START)
    schedule.write(project, "last", {"phase": "running", "slot": "2026-10-11-12"})
    assert schedule.tick(project, at=DUE, runner=lambda *a: pytest.fail("repeated interrupted run"))["state"] == "requires_attention"
    # Explicit resume acquires the OS run lock before acknowledging an interrupted process.
    assert schedule.configure(project, True, at=DUE)["state"] == "enabled"
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with schedule.lock(project, "run"):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with pytest.raises(AgentError, match="IMAGE_SCHEDULE_BUSY"):
            schedule.configure(project, True, at=DUE)


# Проверяем сценарий: pause prevents launch and lock prevents overlap.
def test_pause_prevents_launch_and_lock_prevents_overlap(project):
    schedule.configure(project, True, at=START)
    schedule.configure(project, False, at=START)
    assert schedule.tick(project, at=DUE, runner=lambda *a: pytest.fail("paused"))["state"] == "paused"
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with schedule.lock(project, "run"):
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with pytest.raises(AgentError, match="IMAGE_SCHEDULE_BUSY"):
            schedule.tick(project, at=DUE)


# Проверяем сценарий: resume refuses unresolved delivery.
def test_resume_refuses_unresolved_delivery(service, project):
    from max_postiz_agent.db import Intent

    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with service.db.session.begin() as db:
        db.add(Intent(draft_id="test-draft", channel_id="test", body_hash="hash", state="unknown"))
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="UNRESOLVED_PUBLICATION"):
        schedule.configure(project, True, at=START)


# Проверяем сценарий: schedule controls require owner and status is visible to viewer.
def test_schedule_controls_require_owner_and_status_is_visible_to_viewer(service, project, monkeypatch):
    monkeypatch.setenv("APP_AUTH_SECRET", "owner-test-key")
    monkeypatch.setenv("APP_VIEWER_SECRET", "viewer-test-key")
    headers = {"Authorization": "Bearer viewer-test-key"}
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with TestClient(create_app(service)) as client:
        assert client.get("/api/v1/images/status").status_code == 401
        assert client.get("/api/v1/images/status", headers=headers).status_code == 200
        assert client.post("/api/v1/images/schedule/resume", headers=headers).status_code == 403
        assert client.post("/api/v1/images/scan", headers=headers, json={}).status_code == 403
        headers = {"Authorization": "Bearer owner-test-key"}
        assert client.post("/api/v1/images/schedule/resume", headers=headers).json()["state"] == "enabled"
        assert client.post("/api/v1/images/schedule/pause", headers=headers).json()["state"] == "paused"


# Проверяем сценарий: embedded scheduler starts and stops with the project server.
def test_embedded_scheduler_starts_and_stops_with_the_project_server(service, project, monkeypatch):
    import threading

    called = threading.Event()
    # Операция checked: данного модуля.
    def checked(root):
        assert root == project
        called.set()
        return {"state": "paused"}
    monkeypatch.setattr(schedule, "tick", checked)
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with TestClient(create_app(service, run_worker=True)):
        assert called.wait(3)
        assert schedule.status(project)["engine"]["state"] == "online"
    assert schedule.status(project)["engine"]["state"] == "offline"


# Проверяем сценарий: pause is available during collection and is checked before send.
def test_pause_is_available_during_collection_and_is_checked_before_send(service, project, monkeypatch):
    from max_postiz_agent import cli, image_pipeline

    schedule.configure(project, True, at=START)
    monkeypatch.setattr(cli, "build_service", lambda root: service)
    monkeypatch.setattr(service, "close", lambda: None)
    # Операция collected: данного модуля.
    def collected(owner, *, at, publisher):
        # The active run holds this lock; pause must still be accepted.
        # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
        with schedule.lock(project, "run"):
            assert schedule.configure(project, False, at=at)["state"] == "paused"
            return publisher(owner, 1, "a" * 64)
    monkeypatch.setattr(image_pipeline, "auto_run", collected)
    monkeypatch.setattr(image_pipeline, "publish_rank", lambda *a: pytest.fail("sent after pause"))
    assert schedule.run_images(project, DUE)["state"] == "paused"


# Проверяем сценарий: unconfirmed caption correction blocks schedule resume.
def test_unconfirmed_caption_correction_blocks_schedule_resume(project):
    path = project / "data/browser-corrections/test.json"
    path.parent.mkdir(parents=True)
    path.write_text('{"state":"unknown","intent_id":"test"}', encoding="utf-8")
    # Удерживаем контекст ресурсов или транзакции до завершения этого блока.
    with pytest.raises(AgentError, match="UNRESOLVED_CORRECTION"):
        schedule.configure(project, True, at=START)
