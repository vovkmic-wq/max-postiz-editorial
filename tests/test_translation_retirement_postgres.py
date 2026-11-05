"""The real database guards run in the shared explicitly isolated PG fixture."""

from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import select

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, Base
from max_postiz_agent.server_models import EditorialControl
from test_unbound_selection_reconciliation_postgres import unbound_pg as base_pg  # noqa: F401
import test_translation_retirement as checks


@pytest.fixture
def bound_pg(base_pg, tmp_path, monkeypatch):  # noqa: F811
    # base_pg owns a newly generated schema in a verified dedicated test DB.
    engine = base_pg.core.db.engine
    Base.metadata.drop_all(engine)
    yield checks.make_case(tmp_path, engine, monkeypatch)


def test_postgres_atomic_retirement_and_response_loss(bound_pg):
    checks.check_commit(bound_pg)


def test_postgres_audit_failure_rollback(bound_pg):
    checks.check_rollback(bound_pg)


def test_postgres_pause_cas_refuses_without_writes(bound_pg):
    c = bound_pg
    with c.core.db.session.begin() as s:
        s.get(EditorialControl, "main").paused = False
    before = checks.snapshot(c)
    with pytest.raises(AgentError):
        checks.apply(c)
    assert checks.snapshot(c) == before


def test_postgres_concurrent_operation_commits_once(bound_pg):
    with ThreadPoolExecutor(max_workers=2) as pool:
        receipts = list(pool.map(lambda _: checks.apply(bound_pg), range(2)))
    assert receipts[0] == receipts[1]
    with bound_pg.core.db.session() as s:
        assert (
            len(list(s.scalars(select(Audit).where(Audit.action == checks.recovery.ACTION)))) == 1
        )
