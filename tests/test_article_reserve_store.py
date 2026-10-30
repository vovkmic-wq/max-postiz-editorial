"""Резерв: долговечные неизвестные исходы, fencing и сохранение всей истории."""
import copy
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import select

from max_postiz_agent.article_cycle import HourlyArticleCycle
from max_postiz_agent.article_reserve import ArticleReserveStore, RESERVE_CONTRACT
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.db import Draft, now, uid
from max_postiz_agent.hub_models import BridgePost, Dispatch, DispatchAttempt, Target, TargetApproval, Variant
from max_postiz_agent.server_models import EditorialControl, EditorialLedger, PublicationSlot
from max_postiz_agent.server_state import DatabaseArticleLedger, ServerState

pytest_plugins = ["test_hub", "test_hub_postgres"]


@pytest.fixture
def reserve(hub):
    state = ServerState(hub)
    state.initialize()
    state.control("start", state.read()["revision"], "test")
    return hub, ArticleReserveStore(hub), state


def ready(core, store, *, text="A checked article"):
    claim = store.begin_preparation()
    ident, variant_id = uid(), uid()
    store.bind_preparation(claim, ident)
    record = {"id": ident, "slot": int(now() // 3600), "target_id": "max", "state": "quality_ready",
        "source_id": "example_rss", "url": "https://example.org/" + ident,
        "original_hash": fingerprint(ident), "input_hash": fingerprint({"id": ident}),
        "policy_hash": fingerprint("policy"), "source_published_at": now() - 3600,
        "variant_id": variant_id, "prepared_hash": fingerprint({"id": ident, "text": text}),
        "reserve_prepared_hash": fingerprint({"id": ident, "text": text}),
        "reserve_contract": RESERVE_CONTRACT, "reserve_claim": copy.deepcopy(claim)}
    with core.db.session.begin() as session:
        draft = Draft(id=uid(), channel_id="main", story_id=uid(), current={}, config_id=core.settings.hash)
        session.add(draft)
        session.flush()
        target = session.get(Target, "max")
        session.add(Variant(id=variant_id, draft_id=draft.id, target_id="max", body=text,
            source_revision=1, config_hash=core.settings.hash, binding_hash=target.binding_hash))
        ledger = session.get(EditorialLedger, "hourly")
        if ledger is None:
            session.add(EditorialLedger(id="hourly", revision=1, payload={"records": [record]}))
        else:
            ledger.payload = {"records": [*ledger.payload["records"], record]}
            ledger.revision += 1
    store.finish_preparation(claim, "quality_ready")
    return record


def assigned(store, record, *, due=None, text_hash=None):
    return store.assign(record, due or now() + 120,
        text_hash=text_hash or record["reserve_prepared_hash"], evidence_hash=fingerprint("evidence"))


def delivery(core, record, stamp):
    """Только симулированный факт в изолированной БД, без отправки."""
    with core.db.session.begin() as session:
        approval = TargetApproval(variant_id=record["variant_id"], revision=1,
            snapshot_hash=fingerprint("approval"), scheduled_at=stamp, actor="test")
        session.add(approval)
        session.flush()
        dispatch = Dispatch(variant_id=record["variant_id"], approval_id=approval.id, revision=1,
            snapshot_hash=approval.snapshot_hash, snapshot={}, state="published", due_at=stamp,
            verified=True, attempts=1)
        session.add(dispatch)
        session.flush()
        session.add(DispatchAttempt(dispatch_id=dispatch.id, result="published", created_at=stamp))
        return dispatch.id


def test_crash_before_selection_retains_claim_and_prevents_second_backend_call(reserve):
    core, store, _ = reserve
    claim = store.begin_preparation()
    assert ArticleReserveStore(core).active_preparation()["id"] == claim["id"]
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_PREPARATION_ACTIVE"):
        ArticleReserveStore(core).begin_preparation()
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_CLAIM_LOST"):
        store.require_preparation({**claim, "token": uid()})
    assert store.active_preparation()["state"] == "selecting"


def test_known_completion_releases_models_but_unknown_keeps_global_claim(reserve):
    _, store, _ = reserve
    first = store.begin_preparation()
    store.finish_preparation(first, "no_source")
    second = store.begin_preparation()
    store.bind_preparation(second, uid())
    store.finish_preparation(second, "blocked_unknown", error="MODEL_TIMEOUT")
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_PREPARATION_ACTIVE"):
        store.begin_preparation()
    assert store.active_preparation()["error"] == "MODEL_TIMEOUT"


def test_pause_then_resume_does_not_restore_old_preparation_fence(reserve):
    _, store, state = reserve
    claim = store.begin_preparation()
    state.control("pause", state.read()["revision"], "test")
    state.control("start", state.read()["revision"], "test")
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_CLAIM_LOST"):
        store.bind_preparation(claim, uid())
    assert store.active_preparation()["id"] == claim["id"]


def test_changed_settings_revision_cannot_start_models_with_old_filters(reserve):
    _, store, state = reserve
    previous = state.read()["revision"]
    state.control("pause", previous, "test")
    state.control("start", state.read()["revision"], "test")
    with pytest.raises(AgentError, match="EDITORIAL_REVISION_CONFLICT"):
        store.begin_preparation(expected_revision=previous)
    assert store.active_preparation() is None
    assert store.begin_preparation(expected_revision=state.read()["revision"])["state"] == "selecting"


def test_exact_ready_record_and_completed_claim_are_required_to_assign(reserve):
    core, store, _ = reserve
    record = ready(core, store)
    changed = {**record, "input_hash": fingerprint("changed")}
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_RECORD_MISMATCH"):
        assigned(store, changed)
    claim = record["reserve_claim"]
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_CLAIM_LOST"):
        store.require_preparation(claim, record["id"], active=True)
    assert store.require_preparation(claim, record["id"], active=False)["state"] == "quality_ready"
    slot = assigned(store, record)
    assert store.require_assignment(record["id"])["token"] == slot["token"]


def test_unknown_dispatch_holds_target_and_wrong_token_cannot_free_it(reserve):
    core, store, _ = reserve
    first, second = ready(core, store), ready(core, store)
    slot = assigned(store, first)
    store.update(slot["id"], "blocked_unknown", token=slot["token"], error="SEND_UNKNOWN")
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_TARGET_BUSY"):
        assigned(store, second)
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_SLOT_STATE_INVALID"):
        store.update(slot["id"], "missed", token=uid())
    assert store.status()["active"][0]["state"] == "blocked_unknown"
    assert "token" not in store.status()["active"][0]


def test_delivered_fact_survives_pause_and_blocks_replacement_until_published(reserve):
    core, store, state = reserve
    first, second = ready(core, store), ready(core, store)
    slot = assigned(store, first)
    stamp = now()
    dispatch = delivery(core, first, stamp)
    state.control("pause", state.read()["revision"], "test")
    store.update(slot["id"], "delivered", token=slot["token"], dispatch_id=dispatch, delivered_at=stamp)
    assert store.status()["last_delivered_at"] == stamp
    state.control("start", state.read()["revision"], "test")
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_TARGET_BUSY"):
        assigned(store, second, due=now() + 7200)
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_SLOT_STATE_INVALID"):
        store.update(slot["id"], "published", token=slot["token"])
    # Симулируем отдельную успешную status/GET-сверку Postiz в изолированной БД.
    with core.db.session.begin() as session:
        ledger = session.get(EditorialLedger, "hourly")
        updated = copy.deepcopy(ledger.payload)
        updated["records"][0].update(state="published", postiz_state="PUBLISHED",
            postiz_id="simulated-readback", dispatch_id=dispatch, published_at=stamp)
        ledger.payload, ledger.revision = updated, ledger.revision + 1
    store.update(slot["id"], "published", token=slot["token"])
    assert store.status()["active"] == []
    with pytest.raises(AgentError, match="ARTICLE_HOURLY_GAP"):
        assigned(store, second, due=slot["due_at"])
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_ASSIGNMENT_INVALID"):
        assigned(store, second, due=now() + 7200, text_hash=slot["text_hash"])


def test_delivery_timestamp_and_dispatch_must_match_saved_fact(reserve):
    core, store, _ = reserve
    record = ready(core, store)
    slot = assigned(store, record)
    stamp = now()
    dispatch = delivery(core, record, stamp)
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_SLOT_STATE_INVALID"):
        store.update(slot["id"], "delivered", token=slot["token"],
            dispatch_id=dispatch, delivered_at=stamp + 1)
    assert store.status()["active"][0]["delivered_at"] is None


def test_ledger_merges_concurrent_disjoint_records_without_losing_history(reserve):
    core, store, _ = reserve
    first, second = ready(core, store), ready(core, store)
    left, right = HourlyArticleCycle(core), HourlyArticleCycle(core)
    left.reserve_mode = right.reserve_mode = True
    original, newer = left._load(), right._load()
    historical_hash = fingerprint(original["records"][1])
    original["records"][0]["state"] = "pending"
    newer["records"][1]["state"] = "cancelled"
    right._save(newer)
    left._save(original)
    assert [r["state"] for r in left._load()["records"]] == ["pending", "cancelled"]
    assert original["records"][1]["state"] == "cancelled"
    assert original["records"][0]["id"] == first["id"]
    assert original["records"][1]["id"] == second["id"]
    assert historical_hash != fingerprint(original["records"][1])


def test_ledger_same_record_conflict_and_deletion_preserve_current_payload(reserve):
    core, store, _ = reserve
    ready(core, store)
    left, right = HourlyArticleCycle(core), HourlyArticleCycle(core)
    left.reserve_mode = right.reserve_mode = True
    old, current = left._load(), right._load()
    old["records"][0]["state"] = "pending"
    current["records"][0]["state"] = "cancelled"
    right._save(current)
    with pytest.raises(AgentError, match="ARTICLE_LEDGER_REVISION_CONFLICT"):
        left._save(old)
    fresh = left._load()
    fresh["records"].clear()
    with pytest.raises(AgentError, match="ARTICLE_LEDGER_REVISION_CONFLICT"):
        left._save(fresh)
    assert right._load() == current


def test_nested_load_does_not_replace_older_payload_base_or_drop_new_record(reserve):
    core, store, _ = reserve
    ready(core, store)
    value = HourlyArticleCycle(core)
    value.reserve_mode = True
    older = value._load()
    ready(core, store)
    value._load()
    older["records"][0]["state"] = "pending"
    value._save(older)
    assert len(older["records"]) == 2
    assert value._load()["records"][0]["state"] == "pending"


def test_postgres_preparation_and_assignment_races_keep_single_owner(pg_hub):
    core, _ = pg_hub
    state = ServerState(core)
    state.initialize()
    state.control("start", state.read()["revision"], "test")
    store = ArticleReserveStore(core)
    first, second = ready(core, store), ready(core, store)
    due = now() + 120
    def assign(record):
        try:
            return ArticleReserveStore(core).assign(record, due,
                text_hash=record["reserve_prepared_hash"], evidence_hash=fingerprint("evidence"))
        except AgentError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(assign, [first, second]))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "ARTICLE_RESERVE_TARGET_BUSY" in results
    with core.db.session() as session:
        assert len(list(session.scalars(select(PublicationSlot)))) == 1
    def begin(_):
        try:
            return ArticleReserveStore(core).begin_preparation()
        except AgentError as exc:
            return exc.code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(begin, range(2)))
    assert sum(isinstance(r, dict) for r in results) == 1
    assert "ARTICLE_RESERVE_PREPARATION_ACTIVE" in results


def test_legacy_cas_still_rejects_stale_writer(reserve):
    core, store, _ = reserve
    ready(core, store)
    left, right = HourlyArticleCycle(core), HourlyArticleCycle(core)
    stale, fresh = left._load(), right._load()
    fresh["records"][0]["state"] = "cancelled"
    right._save(fresh)
    with pytest.raises(AgentError, match="ARTICLE_LEDGER_REVISION_CONFLICT"):
        DatabaseArticleLedger(left).save(stale)


@pytest.mark.parametrize("attempted", [False, True])
def test_pause_resume_rebinds_only_exact_unattempted_pending_slot(reserve, attempted):
    core, store, state = reserve
    record = ready(core, store)
    slot = assigned(store, record)
    post_id = "test-post-" + uid()
    with core.db.session.begin() as session:
        variant = session.get(Variant, record["variant_id"])
        variant.metadata_json = {"machine_review_evidence_hash": slot["evidence_hash"]}
        approval = TargetApproval(variant_id=variant.id, revision=variant.revision,
            snapshot_hash=fingerprint("pending"), scheduled_at=slot["due_at"], actor="hourly_article_policy")
        session.add(approval)
        session.flush()
        dispatch = Dispatch(variant_id=variant.id, approval_id=approval.id, revision=variant.revision,
            snapshot_hash=approval.snapshot_hash, snapshot={}, state="pending", due_at=slot["due_at"])
        session.add(dispatch)
        session.flush()
        dispatch_id = dispatch.id
        session.add(BridgePost(post_id=post_id, integration_id="test", group_id="test", target_id="max",
            variant_id=variant.id, remote_hash=fingerprint("remote"), remote_state="QUEUE", due_at=slot["due_at"]))
        ledger = session.get(EditorialLedger, "hourly")
        updated = copy.deepcopy(ledger.payload)
        updated["records"][0].update(dispatch_id=dispatch.id, due_at=slot["due_at"],
            postiz_id=post_id, state="pending")
        ledger.payload, ledger.revision = updated, ledger.revision + 1
        if attempted:
            session.add(DispatchAttempt(dispatch_id=dispatch.id, result="failed"))
    store.update(slot["id"], "pending", token=slot["token"], dispatch_id=dispatch_id)
    state.control("pause", state.read()["revision"], "test")
    state.control("start", state.read()["revision"], "test")
    with pytest.raises(AgentError, match="ARTICLE_RESERVE_ASSIGNMENT_LOST"):
        store.require_assignment(record["id"])
    if attempted:
        with pytest.raises(AgentError, match="ARTICLE_RESERVE_PENDING_RESUME_NOT_SAFE"):
            with core.db.session.begin() as session:
                store.rebind_pending(session, record["id"], dispatch_id)
        assert store.get(slot["id"])["control_revision"] == slot["control_revision"]
    else:
        with core.db.session.begin() as session:
            resumed = store.rebind_pending(session, record["id"], dispatch_id)
        assert resumed["token"] == slot["token"] and resumed["due_at"] == slot["due_at"]
        assert resumed["control_revision"] == state.read()["revision"]
        assert store.require_assignment(record["id"])["dispatch_id"] == dispatch_id


def test_postgres_concurrent_ledger_merge_keeps_preparation_and_dispatch(pg_hub):
    core, _ = pg_hub
    state = ServerState(core)
    state.initialize()
    state.control("start", state.read()["revision"], "test")
    store = ArticleReserveStore(core)
    historical, current = ready(core, store), ready(core, store)
    first = HourlyArticleCycle(core, reserve_mode=True)
    second = HourlyArticleCycle(core, reserve_mode=True)
    dispatcher, preparation = first._load(), second._load()
    saved_history = fingerprint(historical)
    dispatcher["records"][1]["state"] = "pending"
    new_record = {"id": uid(), "slot": int(now() // 3600), "state": "preparing"}
    preparation["records"].append(new_record)
    barrier = Barrier(2)
    def save(value):
        cycle, payload = value
        barrier.wait(timeout=10)
        cycle._save(payload)
    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(save, [(first, dispatcher), (second, preparation)]))
    result = HourlyArticleCycle(core)._load()["records"]
    assert len(result) == 3
    assert fingerprint(result[0]) == saved_history
    assert result[1]["id"] == current["id"] and result[1]["state"] == "pending"
    assert result[2] == new_record


def test_postgres_delivery_fact_does_not_take_control_lock_after_target_lock(pg_hub):
    core, _ = pg_hub
    state = ServerState(core)
    state.initialize()
    state.control("start", state.read()["revision"], "test")
    store = ArticleReserveStore(core)
    record = ready(core, store)
    slot = assigned(store, record)
    stamp = now()
    dispatch_id = delivery(core, record, stamp)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with core.db.session.begin() as holder:
            holder.scalar(select(EditorialControl).where(EditorialControl.id == "main").with_for_update())
            future = pool.submit(store.update, slot["id"], "delivered", token=slot["token"],
                dispatch_id=dispatch_id, delivered_at=stamp)
            try:
                result = future.result(timeout=3)
            finally:
                holder.rollback()
    assert result["delivered_at"] == stamp
