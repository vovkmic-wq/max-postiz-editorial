"""Факт доставки и слот фиксируются атомарно, без новых внешних операций."""
import pytest

from max_postiz_agent.db import Audit, uid
from max_postiz_agent.hub_models import Dispatch, DispatchAttempt, Variant
from max_postiz_agent.server_models import PublicationSlot
from max_postiz_agent.server_state import ServerState
from test_article_cycle import seed_legacy_dispatch

pytest_plugins = ["test_automatic_policy"]


@pytest.fixture
def delivery_slot(automatic_cycle):
    (cycle, core, article, _, _, stamp, _), _ = automatic_cycle
    prepared = cycle.prepare(article)
    dispatch_id = seed_legacy_dispatch(core, prepared)
    state = ServerState(core)
    state.initialize()
    slot_id, token = uid(), uid()
    with core.db.session.begin() as session:
        variant = session.get(Variant, prepared["variant_id"])
        # Это фикстура завершения отправки; согласование ею не выполняется.
        variant.metadata_json = {**variant.metadata_json, "reserve_contract": "article-reserve-v1"}
        session.add(PublicationSlot(id=slot_id, target_id=variant.target_id,
            slot_at=int(stamp[0] // 3600) * 3600, record_id=prepared["id"], text_hash="1" * 64,
            evidence_hash="2" * 64, binding={"variant_id": variant.id}, token=token,
            control_revision=state.read()["revision"], active_target=variant.target_id,
            due_at=stamp[0], dispatch_id=dispatch_id, state="pending"))
    return core, state, dispatch_id, slot_id, stamp


def finish_delivery(core, session, dispatch_id, stamp):
    dispatch = session.get(Dispatch, dispatch_id)
    dispatch.state, dispatch.verified = "published", True
    attempt = DispatchAttempt(dispatch_id=dispatch_id, result="published", created_at=stamp)
    session.add(attempt)
    session.flush()
    core._record_reserve_delivery(session, dispatch, attempt)


def test_max_fact_is_durable_before_postiz_reconciliation(delivery_slot):
    core, _, dispatch_id, slot_id, stamp = delivery_slot
    with core.db.session.begin() as session:
        finish_delivery(core, session, dispatch_id, stamp[0])
    with core.db.session() as session:
        slot = session.get(PublicationSlot, slot_id)
        assert slot.state == "delivered" and slot.delivered_at == stamp[0]
        assert slot.active_target == slot.target_id  # Postiz ещё требует сверки.
        assert session.get(Dispatch, dispatch_id).state == "published"
        assert session.query(DispatchAttempt).one().created_at == slot.delivered_at


def test_dispatch_attempt_and_slot_roll_back_together(delivery_slot):
    core, _, dispatch_id, slot_id, stamp = delivery_slot
    with pytest.raises(RuntimeError, match="simulated commit failure"):
        with core.db.session.begin() as session:
            finish_delivery(core, session, dispatch_id, stamp[0])
            assert session.get(PublicationSlot, slot_id).state == "delivered"
            raise RuntimeError("simulated commit failure")
    with core.db.session() as session:
        assert session.get(Dispatch, dispatch_id).state == "pending"
        assert session.get(PublicationSlot, slot_id).state == "pending"
        assert session.query(DispatchAttempt).count() == 0


def test_pause_revision_does_not_discard_completed_max_fact(delivery_slot):
    core, state, dispatch_id, slot_id, stamp = delivery_slot
    state.control("pause", state.read()["revision"], "test-owner")
    with core.db.session.begin() as session:
        finish_delivery(core, session, dispatch_id, stamp[0])
    with core.db.session() as session:
        assert session.get(PublicationSlot, slot_id).delivered_at == stamp[0]
        assert session.get(Dispatch, dispatch_id).verified
    assert state.read()["paused"]


def test_slot_bookkeeping_error_keeps_confirmed_dispatch_and_audit(delivery_slot):
    core, _, dispatch_id, slot_id, stamp = delivery_slot
    with core.db.session.begin() as session:
        slot = session.get(PublicationSlot, slot_id)
        slot.binding = {"variant_id": "wrong-variant"}
    with core.db.session.begin() as session:
        finish_delivery(core, session, dispatch_id, stamp[0])
    with core.db.session() as session:
        assert session.get(Dispatch, dispatch_id).state == "published"
        assert session.query(DispatchAttempt).one().result == "published"
        assert session.get(PublicationSlot, slot_id).state == "pending"
        audits = session.query(Audit).filter(Audit.action == "reserve_delivery_persistence_failed").all()
        assert len(audits) == 1 and audits[0].entity == dispatch_id
