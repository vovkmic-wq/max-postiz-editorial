"""Регрессии общей паузы и аварийных границ: только локальные фиктивные сервисы."""
import json
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from sqlalchemy import delete

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.config import AgentError
from max_postiz_agent.hub_models import Dispatch, Target, TargetApproval
from max_postiz_agent.server_models import EditorialLedger
from max_postiz_agent.server_state import DatabaseArticleLedger, ServerState, require_running

pytest_plugins = ["test_automatic_policy"]


def test_pause_during_approval_sync_cannot_reactivate_target(automatic_cycle, monkeypatch):
    (cycle, core, article, records, _, _, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    store = ServerState(core)
    store.initialize()
    store.control("start", store.read()["revision"], "postiz:review-owner")
    original_sync, original_approve = core.bridge.sync, core.approve_hourly_policy
    pause_committed = []

    def sync_then_pause(*args, **kwargs):
        result = original_sync(*args, **kwargs)
        # Пауза уже подтверждена, пока согласование ждёт внешний read-only sync.
        assert records[prepared["postiz_id"]]["state"] == "QUEUE"
        pause_committed.append(store.control(
            "pause", store.read()["revision"], "postiz:review-owner"))
        return result

    def approve_after_schedule(*args, **kwargs):
        # Не перехватываем sync планирования: проверяется именно окно approval.
        monkeypatch.setattr(core.bridge, "sync", sync_then_pause)
        return original_approve(*args, **kwargs)

    monkeypatch.setattr(core, "approve_hourly_policy", approve_after_schedule)
    with pytest.raises(AgentError, match="EDITORIAL_PAUSED"):
        cycle.publish(prepared["id"], prepared["content_hash"])

    assert len(pause_committed) == 1 and pause_committed[0]["paused"] is True
    assert store.read()["paused"] is True
    with core.db.session() as session:
        assert session.query(TargetApproval).count() == 0
        assert session.query(Dispatch).count() == 0
        assert session.get(Target, "max").paused is True
    # Уже выполненное планирование не повторяется и не маскируется отменой.
    assert records[prepared["postiz_id"]]["state"] == "QUEUE"
    record = cycle._load()["records"][0]
    assert record["state"] == "needs_attention" and record["error"] == "EDITORIAL_PAUSED"


def test_repeated_migration_repairs_missing_marker_and_forbids_legacy_fallback(cycle):
    old, core, article, *_ = cycle
    prepared = old.prepare(article)
    legacy_bytes = old.ledger_path.read_bytes()
    adapter = DatabaseArticleLedger(old)
    adapter.migrate()
    # Это durable-состояние сбоя между PG commit и созданием локального marker.
    adapter.marker.unlink()
    current = HourlyArticleCycle(core)
    payload = current._load()
    payload["records"][0]["state"] = "cancelled"
    current._save(payload)

    result = DatabaseArticleLedger(HourlyArticleCycle(core)).migrate()
    assert result["already_migrated"] is True
    assert adapter.marker.is_file()
    assert isinstance(json.loads(adapter.marker.read_text(encoding="utf-8")), dict)
    assert old.ledger_path.read_bytes() == legacy_bytes
    assert HourlyArticleCycle(core)._load()["records"][0]["state"] == "cancelled"

    with core.db.session.begin() as session:
        session.execute(delete(EditorialLedger))
    with pytest.raises(AgentError, match="ARTICLE_LEDGER_DATABASE_MISSING"):
        HourlyArticleCycle(core)._load()
    # Старый источник остаётся в резервном JSON, но не становится рабочим снова.
    assert json.loads(legacy_bytes)["records"][0]["id"] == prepared["id"]
    assert json.loads(legacy_bytes)["records"][0]["state"] == "prepared"


class FakeLeaderConnection:
    """Выполняет только заранее известные SQL-команды, без драйвера PostgreSQL."""
    invalidated = False

    def __init__(self):
        self.held = False
        self.closed = False
        self.unlocks = 0

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.closed = True

    def scalar(self, statement, parameters=None):
        assert not self.closed
        sql = str(statement)
        if "pg_try_advisory_lock" in sql:
            self.held = True
            return True
        if "pg_locks" in sql:
            return self.held
        if "pg_backend_pid" in sql:
            return 7331
        raise AssertionError("Неожиданный SQL в фиктивной блокировке")

    def execute(self, statement, parameters=None):
        assert "pg_advisory_unlock" in str(statement) and self.held
        self.held = False
        self.unlocks += 1

    def commit(self):
        assert not self.closed


@pytest.mark.parametrize("interrupted", [False, True])
def test_released_leader_leaves_permanent_fence_for_inflight_worker(interrupted):
    connection = FakeLeaderConnection()
    engine = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"), connect=lambda: connection)

    def forbidden_read(*_):
        raise AssertionError("Отозванный владелец не должен читать рабочее состояние")

    session = SimpleNamespace(get=forbidden_read)
    core = SimpleNamespace(db=SimpleNamespace(engine=engine, session=lambda: nullcontext(session)))
    store = ServerState(core)

    def own_worker():
        with store.worker_lock() as acquired:
            assert acquired and connection.held
            core.editorial_leader_check()
            if interrupted:
                raise RuntimeError("Фиктивное завершение сервера при долгой операции")

    if interrupted:
        with pytest.raises(RuntimeError, match="Фиктивное завершение"):
            own_worker()
    else:
        own_worker()

    assert connection.closed and not connection.held and connection.unlocks == 1
    # Имитируем возврат долгого Hy/Min после выхода server_cli из worker_lock.
    # Отозванный владелец обязан остановиться ДО доступа к БД/новой мутации.
    with pytest.raises(AgentError, match="EDITORIAL_LEADERSHIP_LOST"):
        require_running(core)
