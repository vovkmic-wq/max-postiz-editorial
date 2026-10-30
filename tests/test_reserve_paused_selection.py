"""Пауза после GET не оставляет незанятый моделями selecting claim навсегда."""
import copy

import pytest

from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit, Base, uid
from max_postiz_agent.server_models import EditorialLedger, EditorialPreparationClaim

pytest_plugins = ["test_hourly_reserve_controller"]


def snapshot(core):
    with core.db.session() as session:
        return {table.name: sorted([dict(row) for row in session.execute(table.select()).mappings()], key=repr)
            for table in Base.metadata.sorted_tables}


def pause(value):
    value.state.control("pause", value.state.read()["revision"], "fixture-owner")
    return value.state.read()["revision"]


def test_exact_unbound_selection_closes_on_new_pause_and_old_get_cannot_bind(reserve_controller):
    value = reserve_controller
    claim = value.store.begin_preparation()
    revision = pause(value)
    before = snapshot(value.core)
    result = value.store.finish_unbound_selection_on_pause(claim, expected_revision=revision)
    after = snapshot(value.core)
    assert result["state"] == "no_source" and result["error"] == "EDITORIAL_PAUSED"
    assert value.store.active_preparation() is None
    assert {name for name in before if before[name] != after[name]} == {"editorial_preparation_claims", "audit_events"}
    with pytest.raises(AgentError):
        value.store.bind_preparation(claim, uid())
    value.state.control("start", revision, "fixture-owner")
    with pytest.raises(AgentError):
        value.store.bind_preparation(claim, uid())
    assert value.store.begin_preparation()["id"] != claim["id"]


def test_prepare_once_pause_during_get_finishes_only_the_unbound_selection(reserve_controller):
    value = reserve_controller

    def select(_settings, **_kwargs):
        pause(value)
        return copy.deepcopy(value.article)

    value.controller.selector = select
    assert value.controller.prepare_once() == {"phase": "paused"}
    assert value.store.active_preparation() is None
    assert value.cycle._load()["records"] == []
    assert not value.model_calls and not value.posts and not value.writes
    with value.core.db.session() as session:
        assert session.query(Audit).filter_by(action="reserve_unbound_selection_paused").one().details["model_calls"] == 0


@pytest.mark.parametrize("damage", ["active", "revision", "same_revision", "token", "bound", "unknown", "record_link", "prior_bind"])
def test_uncertain_or_changed_claim_is_never_released(reserve_controller, damage):
    value = reserve_controller
    claim = value.store.begin_preparation()
    if damage == "bound":
        value.store.bind_preparation(claim, uid())
    if damage == "unknown":
        value.store.finish_preparation(claim, "blocked_unknown", "MODEL_TIMEOUT")
    if damage == "record_link":
        with value.core.db.session.begin() as session:
            ledger = session.get(EditorialLedger, "hourly")
            ledger.payload = {"records": [{"id": uid(), "reserve_claim": copy.deepcopy(claim)}]}
    if damage == "prior_bind":
        with value.core.db.session.begin() as session:
            value.core.db.audit(session, "fixture", "reserve_preparation_bound", claim["id"], record_id=uid())
    revision = pause(value) if damage != "active" else value.state.read()["revision"]
    if damage == "revision":
        revision += 1
    if damage == "same_revision":
        with value.core.db.session.begin() as session:
            session.get(EditorialPreparationClaim, claim["id"]).control_revision = revision
        claim["control_revision"] = revision
    if damage == "token":
        claim["token"] = uid()
    before = snapshot(value.core)
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_PAUSED_SELECTION_NOT_SAFE"):
        value.store.finish_unbound_selection_on_pause(claim, expected_revision=revision)
    assert snapshot(value.core) == before
