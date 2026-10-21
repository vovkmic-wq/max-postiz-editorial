"""Офлайн-проверки контроллера без сети, реальных моделей и публикаций."""
import copy
import json
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.hub_models import BridgePost, Dispatch, Target, TargetApproval, Variant
from max_postiz_agent.server_cycle import ServerCycle
from max_postiz_agent.server_state import ServerState, default_settings

pytest_plugins = ["test_article_cycle"]


class State:
    def __init__(self):
        self.value = {"paused": False, "revision": 1, "settings": default_settings(), "runtime": {}}
        self.phases = []

    def read(self):
        return copy.deepcopy(self.value)

    def phase(self, phase, **details):
        self.phases.append(phase)
        self.value["runtime"].update(phase=phase, **details)

    def assert_running(self):
        if self.value["paused"]:
            raise AgentError("EDITORIAL_PAUSED", 409)


@pytest.fixture
def server(cycle, monkeypatch):
    old, core, article, _, _, stamp, policy = cycle
    base = old.prepare(article)
    record = old._load()["records"][0]
    snapshot = old._snapshot(record)
    record.update(state="automatic_ready", prepared_hash=fingerprint(snapshot))
    state = State()
    calls = []
    ledger = {"records": []}
    automatic = {**policy, "approval_mode": "automatic", "review_rubric": "meaning-first-v1"}

    class Cycle:
        directory = old.directory

        def _load(self):
            return copy.deepcopy(ledger)

        def _policy(self):
            return automatic

        def _snapshot(self, value):
            calls.append(("snapshot", value["id"]))
            return copy.deepcopy(snapshot)

        def _cadence(self, value, due, check_gap=True):
            return 0

        def _record(self, value, ident):
            return next(r for r in value["records"] if r["id"] == ident)

        def prepare(self, value):
            calls.append(("prepare", value))
            ledger["records"].append(copy.deepcopy(record))
            return copy.deepcopy(record)

        def publish(self, ident, hashed, due_at=None):
            calls.append(("publish", ident, hashed, due_at))
            ledger["records"][0]["state"] = "pending"
            return {"id": ident, "state": "pending"}

        def status(self, ident):
            calls.append(("status", ident))
            return {"id": ident, "state": "pending"}

        def _assert_pre_draft(self, value):
            calls.append(("pre_draft_guard", value["id"]))
            if any(value.get(k) for k in ("variant_id", "postiz_id", "dispatch_id")):
                raise AgentError("ARTICLE_PREPARATION_NOT_CANCELLABLE", 409)

        def cancel_preparation(self, ident, reason):
            calls.append(("cancel", ident, reason))
            self._record(ledger, ident)["state"] = "cancelled"

    monkeypatch.setattr("max_postiz_agent.server_cycle.automatic_evidence", lambda *_: {"review": {"approved": True}})
    def selector(*_args, **_kwargs):
        return copy.deepcopy(article)
    controller = ServerCycle(core, state=state, selector=selector, cycle=Cycle(), clock=lambda: stamp[0])
    return SimpleNamespace(controller=controller, state=state, ledger=ledger, calls=calls,
                           record=record, snapshot=snapshot, core=core, article=article, stamp=stamp,
                           automatic=automatic, base=base)


def test_paused_never_selects_or_reads_cycle(server):
    server.state.value["paused"] = True
    server.controller.selector = lambda *_a, **_k: pytest.fail("selection while paused")
    assert server.controller.run_once() == {"phase": "paused"}
    assert server.calls == []


def test_free_hour_claim_precedes_one_prepare_and_exact_publish(server):
    def select(_settings, **kwargs):
        assert server.state.value["runtime"]["phase"] == "selecting"
        assert server.state.value["runtime"]["cycle_claim_hour"] == int(server.stamp[0] // 3600)
        assert kwargs["ledger"] == {"records": []}
        return server.article
    server.controller.selector = select
    result = server.controller.run_once()
    assert result["phase"] == "waiting_dispatch"
    assert [c[0] for c in server.calls] == ["prepare", "snapshot", "publish"]
    assert server.calls[-1][2] == fingerprint(server.snapshot)
    assert server.calls[-1][3] >= server.stamp[0] + 120


def test_ready_has_priority_and_never_selects_another_source(server):
    server.ledger["records"].append(server.record)
    server.controller.selector = lambda *_a, **_k: pytest.fail("new source before ready")
    assert server.controller.run_once()["phase"] == "waiting_dispatch"
    assert [c[0] for c in server.calls] == ["snapshot", "publish"]


def test_only_one_of_multiple_ready_is_selected(server):
    older = {**server.record, "id": "older", "slot": server.record["slot"] - 1}
    newer = {**server.record, "id": "newer", "slot": server.record["slot"] - 2}
    server.ledger["records"].extend([older, newer])
    assert server.controller.run_once()["record_id"] == "older"
    assert sum(c[0] == "publish" for c in server.calls) == 1


@pytest.mark.parametrize("state", ["published", "cancelled", "failed", "rejected"])
def test_used_hour_is_not_caught_up(server, state):
    server.ledger["records"].append({**server.record, "state": state})
    assert server.controller.run_once()["phase"] == "hour_used"
    assert server.calls == []


def test_no_source_is_a_durable_single_attempt_for_the_hour(server):
    seen = []
    server.controller.selector = lambda *_a, **_k: seen.append(True)
    assert server.controller.run_once()["phase"] == "no_source"
    assert server.controller.run_once()["phase"] == "hour_used"
    assert seen == [True]
    server.stamp[0] += 3600 * 5
    assert server.controller.run_once()["phase"] == "no_source"
    assert seen == [True, True]  # one current hour, never a five-hour catchup


@pytest.mark.parametrize("state", ["sending", "unknown", "scheduled", "awaiting_manual"])
def test_global_active_other_target_blocks_new_preparation(server, state):
    server.controller._inventory = lambda: ([{"id": "other", "variant_id": "other", "state": state}], False)
    assert server.controller.run_once()["phase"] == "blocked"
    assert server.calls == []


def test_existing_pending_only_observes_same_record_without_publish(server):
    server.ledger["records"].append({**server.record, "state": "pending"})
    server.controller._inventory = lambda: ([{"id": "existing", "variant_id": server.record["variant_id"], "state": "pending"}], False)
    server.controller._resume_pending = lambda *_: False
    assert server.controller.run_once()["phase"] == "waiting_dispatch"
    assert server.calls == [("status", server.record["id"])]


def test_any_lease_blocks_even_ready(server):
    server.ledger["records"].append(server.record)
    server.controller._inventory = lambda: ([], True)
    assert server.controller.run_once()["phase"] == "blocked"
    assert server.calls == []


def test_missing_or_changed_evidence_cannot_publish(server, monkeypatch):
    server.ledger["records"].append(server.record)
    monkeypatch.setattr("max_postiz_agent.server_cycle.automatic_evidence", lambda *_: None)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_READY"):
        server.controller.run_once()
    assert not any(c[0] == "publish" for c in server.calls)


def test_changed_reviewed_hash_cannot_publish(server):
    server.ledger["records"].append({**server.record, "prepared_hash": "0" * 64})
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_HASH_MISMATCH"):
        server.controller.run_once()


def test_model_pause_between_selection_and_prepare_is_respected(server):
    def select(*_a, **_k):
        server.state.value["paused"] = True
        return server.article
    server.controller.selector = select
    with pytest.raises(AgentError, match="EDITORIAL_PAUSED"):
        server.controller.run_once()
    assert server.calls == []


def test_unknown_prepare_is_not_retried(server):
    def unknown(_article):
        server.ledger["records"].append({**server.record, "state": "staging_automatic_draft"})
        raise AgentError("POSTIZ_UNKNOWN", 503)
    server.controller.cycle.prepare = unknown
    with pytest.raises(AgentError, match="POSTIZ_UNKNOWN"):
        server.controller.run_once()
    assert server.controller.run_once()["phase"] == "blocked"
    assert not any(c[0] == "publish" for c in server.calls)


def test_completed_known_rejection_closes_without_retry_or_publish(server):
    record = {**server.record, "state": "needs_attention", "error": "ARTICLE_QUALITY_REJECTED",
              "preparation_attempts": [{"finished_at": server.stamp[0]}]}
    for key in ("variant_id", "postiz_id", "dispatch_id"):
        record.pop(key, None)
    record["quality_trace"] = "data/hourly-articles/quality-" + record["id"] + ".local.jsonl"
    path = server.core.settings.root / record["quality_trace"]
    path.write_text("\n".join(json.dumps(e) for e in [
        {"phase": "review_parsed", "record_id": record["id"], "at": server.stamp[0], "review": {"approved": False}},
        {"phase": "rejected", "record_id": record["id"], "at": server.stamp[0], "error": "ARTICLE_QUALITY_REJECTED"}]), encoding="utf-8")
    server.ledger["records"].append(record)
    assert server.controller.run_once()["phase"] == "rejected"
    assert [c[0] for c in server.calls] == ["pre_draft_guard", "cancel"]
    assert server.controller.run_once()["phase"] == "hour_used"


def test_incomplete_failure_trace_remains_blocked(server):
    server.ledger["records"].append({**server.record, "state": "needs_attention",
                                    "error": "ARTICLE_QUALITY_REJECTED"})
    assert server.controller.run_once()["phase"] == "blocked"
    assert not any(c[0] == "cancel" for c in server.calls)


def test_remote_staged_failed_record_cannot_be_cancelled_as_predraft(server):
    record = {**server.record, "state": "needs_attention", "error": "ARTICLE_QUALITY_REJECTED",
              "preparation_attempts": [{"finished_at": server.stamp[0]}]}
    record["quality_trace"] = "data/hourly-articles/quality-" + record["id"] + ".local.jsonl"
    (server.core.settings.root / record["quality_trace"]).write_text("\n".join(json.dumps(e) for e in [
        {"phase": "gate_rejected", "record_id": record["id"], "at": server.stamp[0]},
        {"phase": "rejected", "record_id": record["id"], "at": server.stamp[0], "error": "ARTICLE_QUALITY_REJECTED"}]), encoding="utf-8")
    server.ledger["records"].append(record)
    with pytest.raises(AgentError, match="ARTICLE_PREPARATION_NOT_CANCELLABLE"):
        server.controller.run_once()
    assert not any(c[0] == "cancel" for c in server.calls)


def test_default_reddit_cannot_bypass_source_policy(server):
    server.state.value["settings"]["source"] = "reddit"
    server.state.value["settings"]["reddit"]["subreddits"] = ["science"]
    server.controller.selector = None
    with pytest.raises(AgentError, match="EDITORIAL_SOURCE_POLICY_REQUIRED"):
        server.controller.run_once()


def test_default_nasa_enables_only_metadata_checkpoint(server, monkeypatch):
    server.controller.selector = None
    seen = []

    def selector(*args, **kwargs):
        seen.append(kwargs)
        return None

    monkeypatch.setattr("max_postiz_agent.server_nasa.select_nasa_article", selector)
    assert server.controller.run_once()["phase"] == "no_source"
    assert len(seen) == 1 and seen[0]["checkpoint"] is True
    assert server.calls == []


def test_source_selection_cannot_spill_a_preparation_into_another_hour(server):
    def select(*_a, **_k):
        server.stamp[0] += 3600
        return server.article
    server.controller.selector = select
    assert server.controller.run_once()["phase"] == "selection_slot_expired"
    assert server.calls == []


def test_real_database_bridge_owner_blocks_before_ready_publish(server):
    server.ledger["records"].append(server.record)
    with server.core.db.session.begin() as session:
        session.add(BridgePost(post_id="leased", integration_id="max", group_id="one", target_id="max",
            variant_id=server.record["variant_id"], remote_hash="a" * 64, remote_state="DRAFT",
            due_at=server.stamp[0], active=True, mutation_owner="other-owner", mutation_until=server.stamp[0] + 60))
    assert server.controller.run_once()["phase"] == "blocked"
    assert server.calls == []


def test_real_database_revoked_approval_is_not_reused_for_automatic_ready(server):
    server.ledger["records"].append(server.record)
    with server.core.db.session.begin() as session:
        session.add(TargetApproval(variant_id=server.record["variant_id"], revision=1,
            snapshot_hash="a" * 64, scheduled_at=server.stamp[0], actor="old", revoked=True))
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_READY"):
        server.controller.run_once()
    assert not any(c[0] == "publish" for c in server.calls)


def test_configured_interval_uses_actual_delivery_time_and_waits_until_schedule_window(server, monkeypatch):
    server.ledger["records"].append(server.record)
    server.state.value["settings"]["interval_seconds"] = 7200
    actual = server.stamp[0] - 600

    class Session:
        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def get(self, *_):
            return SimpleNamespace(id=server.record["variant_id"])

        def scalar(self, *_):
            return None

        def scalars(self, *_):
            return [SimpleNamespace(created_at=actual)]

    monkeypatch.setattr(server.core.db, "session", Session)
    server.controller._inventory = lambda: ([], False)
    assert server.controller.run_once()["phase"] == "waiting_interval"
    assert not any(c[0] == "publish" for c in server.calls)
    server.record["slot"] -= 1
    server.stamp[0] += 4000
    assert server.controller.run_once()["phase"] == "waiting_dispatch"
    due = next(c[3] for c in server.calls if c[0] == "publish")
    assert due >= actual + 7201


def pending(server, monkeypatch, *, expired=False):
    store = ServerState(server.core)
    store.initialize()
    store.control("start", 1, "owner")
    due = float(server.stamp[0] - 3601 if expired else server.stamp[0] + 120)
    with server.core.db.session.begin() as session:
        v = session.get(Variant, server.record["variant_id"])
        target = session.get(Target, v.target_id)
        remote = {"id": "pending-post", "content": v.body, "image": "[]", "childrenPost": [],
                  "intervalInDays": None, "publishDate": datetime.fromtimestamp(due, UTC).isoformat(),
                  "state": "QUEUE", "group": "group", "settings": {}, "integration": {"id": "integration-max"}}
        hashed = server.core.bridge.normalize(remote)[3]
        v.metadata_json = {**v.metadata_json, "approval_mode": "automatic", "policy_hash": fingerprint(server.automatic),
                           "postiz_hash": hashed, "postiz_state": "QUEUE", "postiz_due_at": due,
                           "postiz_post_id": remote["id"]}
        target.paused = True
        snapshot = server.core.snapshot(v, target)
        bound = fingerprint({"content": snapshot, "due_at": due})
        approval = TargetApproval(variant_id=v.id, revision=v.revision, snapshot_hash=bound,
                                  scheduled_at=due, actor="hourly_article_policy", revoked=False)
        session.add(approval)
        session.flush()
        d = Dispatch(variant_id=v.id, approval_id=approval.id, revision=v.revision,
                     snapshot_hash=bound, snapshot=snapshot, state="pending", due_at=due)
        session.add(d)
        session.add(BridgePost(post_id=remote["id"], integration_id="integration-max", group_id="group",
            target_id=target.id, variant_id=v.id, remote_hash=hashed, remote_state="QUEUE", due_at=due, active=True))
        session.flush()
        server.record.update(state="pending", policy_hash=fingerprint(server.automatic), prepared_hash=fingerprint(snapshot), due_at=due,
                             postiz_id=remote["id"], dispatch_id=d.id)
        ident, approval_id = d.id, approval.id
    server.ledger["records"].append(server.record)
    monkeypatch.setattr(server.core.postiz, "get_post", lambda _: copy.deepcopy(remote))
    monkeypatch.setattr(server.core, "_quality", lambda *_: server.calls.append(("quality",)))
    return store, remote, ident, approval_id


def test_global_pause_then_start_resumes_only_same_exact_future_dispatch(server, monkeypatch):
    store, _, ident, _ = pending(server, monkeypatch)
    store.control("pause", 2, "owner")
    store.control("start", 3, "owner")
    assert server.controller.run_once()["phase"] == "waiting_dispatch"
    with server.core.db.session() as session:
        assert not session.get(Target, "max").paused
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 1
        assert session.get(Dispatch, ident).attempts == 0
    assert not any(c[0] in {"prepare", "publish"} for c in server.calls)
    assert ("quality",) in server.calls


@pytest.mark.parametrize("damage", ["revoked", "remote_body", "evidence", "paused", "already_attempted", "expired"])
def test_damaged_pending_cannot_be_resumed(server, monkeypatch, damage):
    store, remote, ident, approval_id = pending(server, monkeypatch, expired=damage == "expired")
    if damage == "remote_body":
        remote["content"] += " changed"
    if damage == "evidence":
        monkeypatch.setattr("max_postiz_agent.server_cycle.automatic_evidence", lambda *_: None)
    if damage == "paused":
        store.control("pause", 2, "owner")
    with server.core.db.session.begin() as session:
        if damage == "revoked":
            session.get(TargetApproval, approval_id).revoked = True
        if damage == "already_attempted":
            session.get(Dispatch, ident).attempts = 1
    with pytest.raises(AgentError):
        server.controller._resume_pending(server.record, ident, default_settings(), server.automatic)
    with server.core.db.session() as session:
        assert session.get(Target, "max").paused
        assert session.query(Dispatch).count() == session.query(TargetApproval).count() == 1
