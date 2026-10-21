"""Реальная PostgreSQL: владелец цикла и история проверяются в отдельной схеме."""
from types import SimpleNamespace

import pytest

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.config import AgentError
from max_postiz_agent.server_state import DatabaseArticleLedger, ServerState

pytest_plugins = ["test_hub_postgres"]


def test_postgres_allows_exactly_one_worker_and_revokes_finished_owner(pg_hub):
    core, _ = pg_hub
    state = ServerState(core)
    state.initialize()
    other = SimpleNamespace(db=core.db, settings=core.settings)
    with state.worker_lock() as first:
        assert first is True
        core.editorial_leader_check()
        with ServerState(other).worker_lock() as second:
            assert second is False
    with pytest.raises(AgentError, match="EDITORIAL_LEADERSHIP_LOST"):
        core.editorial_leader_check()
    with ServerState(other).worker_lock() as next_owner:
        assert next_owner is True
        other.editorial_leader_check()


def test_postgres_ledger_cas_keeps_both_source_claim_and_cancellation(pg_hub):
    core, _ = pg_hub
    cycle = HourlyArticleCycle(core)
    cycle._save({"records": [{"id": "historical", "slot": 10, "state": "cancelled", "url": "https://example.org/"}]})
    proof = DatabaseArticleLedger(cycle).migrate()
    assert proof["records"] == 1
    first, second = HourlyArticleCycle(core), HourlyArticleCycle(core)
    old = second._load()
    current = first._load()
    current["records"].append({"id": "new", "slot": 11, "state": "failed"})
    first._save(current)
    with pytest.raises(AgentError, match="ARTICLE_LEDGER_REVISION_CONFLICT"):
        second._save(old)
    assert HourlyArticleCycle(core)._load() == current
