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

        def automatic_repair_status(self, value):
            if controller._known_rejection(value):
                return {"eligible": False, "reason": "EXPIRED" if value["slot"] < int(stamp[0] // 3600)
                        else "EXHAUSTED", "attempts": 2}
            return {"eligible": False, "reason": "FAILED", "attempts": 0}

        def repair_rejected(self, _ident):
            pytest.fail("Исчерпанный или недоказанный отказ не вызывает модель")

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
    assert server.state.value["runtime"]["next_run_at"] == server.calls[-1][3]


def test_ready_has_priority_and_never_selects_another_source(server):
    server.ledger["records"].append(server.record)
    server.controller.selector = lambda *_a, **_k: pytest.fail("new source before ready")
    assert server.controller.run_once()["phase"] == "waiting_dispatch"
    assert [c[0] for c in server.calls] == ["snapshot", "publish"]


def test_revalidated_ready_clears_stale_runtime_error_before_scheduling_without_erasing_history(server):
    record = {**server.record, "error": "ARTICLE_QUALITY_REJECTED",
              "preparation_actions": [{"action": "explicit_correction", "old_error": "ARTICLE_QUALITY_REJECTED"}]}
    server.ledger["records"].append(record)
    before = copy.deepcopy(record)
    server.state.value["runtime"]["last_error"] = {"code": "ARTICLE_RECONCILIATION_REQUIRED"}
    publish = server.controller.cycle.publish

    def scheduled(*args, **kwargs):
        assert server.state.value["runtime"]["phase"] == "scheduling"
        assert server.state.value["runtime"]["last_error"] is None
        return publish(*args, **kwargs)

    server.controller.cycle.publish = scheduled
    assert server.controller.run_once()["phase"] == "waiting_dispatch"
    assert server.state.value["runtime"]["last_error"] is None
    assert server.ledger["records"] == [{**before, "state": "pending"}]
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
    server.state.value["runtime"]["last_error"] = {"code": "OLD_FAILURE"}
    assert server.controller.run_once()["phase"] == "hour_used"
    assert server.state.value["runtime"]["last_error"] == {"code": "OLD_FAILURE"}
    assert server.state.value["runtime"]["next_run_at"] == (int(server.stamp[0] // 3600) + 1) * 3600
    assert server.calls == []


def test_no_source_is_a_durable_single_attempt_for_the_hour(server):
    seen = []
    server.controller.selector = lambda *_a, **_k: seen.append(True)
    assert server.controller.run_once()["phase"] == "no_source"
    assert server.state.value["runtime"]["next_run_at"] == (int(server.stamp[0] // 3600) + 1) * 3600
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
    due = server.stamp[0] + 120
    server.ledger["records"].append({**server.record, "state": "pending", "due_at": due})
    server.controller._inventory = lambda: ([{"id": "existing", "variant_id": server.record["variant_id"], "state": "pending"}], False)
    server.controller._resume_pending = lambda *_: False
    assert server.controller.run_once()["phase"] == "waiting_dispatch"
    assert server.state.value["runtime"]["next_run_at"] == due
    assert server.calls == [("status", server.record["id"])]


@pytest.mark.parametrize("active", [False, True])
@pytest.mark.parametrize("observed", [
    {"state": "pending", "dispatch_state": "pending", "error": None},
    {"state": "published", "dispatch_state": "published", "delivery_verified": True,
     "postiz_state": "PUBLISHED", "error": None},
])
def test_known_status_success_clears_only_runtime_error_without_changing_ledger(server, active, observed):
    record = {**server.record, "state": "pending", "dispatch_id": "existing"}
    server.ledger["records"].append(record)
    original = copy.deepcopy(record)
    server.state.value["runtime"]["last_error"] = {"code": "ARTICLE_RECONCILIATION_REQUIRED"}
    if active:
        server.controller._inventory = lambda: ([{"id": "existing", "variant_id": record["variant_id"],
                                                 "state": "pending"}], False)
        server.controller._resume_pending = lambda *_: False

    def status(ident):
        server.calls.append(("status", ident))
        return {"id": ident, **observed}

    server.controller.cycle.status = status
    result = server.controller.run_once()
    assert result["phase"] == ("waiting_dispatch" if active else "reconciling")
    assert result["last_error"] is None and server.state.value["runtime"]["last_error"] is None
    assert server.ledger["records"] == [original]
    assert server.calls == [("status", record["id"])]


@pytest.mark.parametrize("observed", [
    {"state": "unknown", "error": "SEND_UNKNOWN"},
    {"state": "published_unverified", "dispatch_state": "published", "delivery_verified": False},
    {"state": "rejected", "error": "ARTICLE_QUALITY_REJECTED"},
    {"state": "needs_attention", "error": "ARTICLE_APPROVAL_INTERRUPTED"},
    {"state": "pending", "dispatch_state": "pending", "error": "STILL_UNRESOLVED"},
    {"state": "published", "delivery_verified": False, "postiz_state": "PUBLISHED"},
    {"state": "published", "delivery_verified": True, "postiz_state": "QUEUE"},
    None,
])
def test_uncertain_or_failed_status_does_not_hide_existing_error(server, observed):
    record = {**server.record, "state": "pending", "dispatch_id": "existing"}
    server.ledger["records"].append(record)
    original = copy.deepcopy(record)
    old_error = {"code": "ARTICLE_RECONCILIATION_REQUIRED"}
    server.state.value["runtime"]["last_error"] = copy.deepcopy(old_error)
    server.controller.cycle.status = lambda _: copy.deepcopy(observed)
    assert server.controller.run_once()["phase"] == "reconciling"
    assert server.state.value["runtime"]["last_error"] == old_error
    assert server.ledger["records"] == [original]
    assert server.calls == []


def test_any_lease_blocks_even_ready(server):
    server.ledger["records"].append(server.record)
    server.controller._inventory = lambda: ([], True)
    assert server.controller.run_once()["phase"] == "blocked"
    assert server.calls == []


def test_missing_or_changed_evidence_cannot_publish(server, monkeypatch):
    server.ledger["records"].append(server.record)
    server.state.value["runtime"]["last_error"] = {"code": "STILL_UNRESOLVED"}
    monkeypatch.setattr("max_postiz_agent.server_cycle.automatic_evidence", lambda *_: None)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_NOT_READY"):
        server.controller.run_once()
    assert server.state.value["runtime"]["last_error"] == {"code": "STILL_UNRESOLVED"}
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


def completed_rejection(server, *, extra_events=(), keep_staged=False):
    record = {**server.record, "state": "needs_attention", "error": "ARTICLE_QUALITY_REJECTED",
              "preparation_attempts": [{"finished_at": server.stamp[0]}]}
    if not keep_staged:
        for key in ("variant_id", "postiz_id", "dispatch_id"):
            record.pop(key, None)
    record["quality_trace"] = "data/hourly-articles/quality-" + record["id"] + ".local.jsonl"
    path = server.core.settings.root / record["quality_trace"]
    path.write_text("\n".join(json.dumps(e) for e in [
        {"phase": "review_parsed", "record_id": record["id"], "at": server.stamp[0], "review": {"approved": False}},
        *({"record_id": record["id"], "at": server.stamp[0], **event} for event in extra_events),
        {"phase": "rejected", "record_id": record["id"], "at": server.stamp[0], "error": "ARTICLE_QUALITY_REJECTED"}]), encoding="utf-8")
    return record, path


@pytest.fixture
def repairable(server):
    record, path = completed_rejection(server)
    server.ledger["records"].append(record)
    outcomes = ["passed"]
    server.controller.selector = lambda *_a, **_k: pytest.fail("Исправление не выбирает другой источник")

    def status(value):
        attempts = value.get("automatic_repair_attempts", [])
        if attempts:
            last = attempts[-1]
            if last["state"] in {"correcting", "checking"} or last.get("finished_at") is None:
                return {"eligible": False, "reason": "IN_PROGRESS", "attempts": len(attempts)}
            if last["state"] in {"failed", "unknown"}:
                return {"eligible": False, "reason": "FAILED", "attempts": len(attempts)}
        if not server.controller._known_rejection(value):
            return {"eligible": False, "reason": "FAILED", "attempts": len(attempts)}
        if value["slot"] < int(server.stamp[0] // 3600):
            return {"eligible": False, "reason": "EXPIRED", "attempts": len(attempts)}
        if attempts and attempts[-1]["state"] in {"no_change", "not_repairable"}:
            return {"eligible": False, "reason": attempts[-1]["state"].upper(), "attempts": len(attempts)}
        return {"eligible": len(attempts) < 2, "reason": "ELIGIBLE" if len(attempts) < 2 else "EXHAUSTED",
                "attempts": len(attempts)}

    def repair(ident):
        server.calls.append(("repair", ident))
        value = server.controller.cycle._record(server.ledger, ident)
        claims = value.setdefault("automatic_repair_attempts", [])
        claim = {"id": "claim-" + str(len(claims) + 1), "number": len(claims) + 1,
                 "state": "correcting", "started_at": server.stamp[0]}
        claims.append(claim)
        value["state"] = "automatic_repairing"
        assert outcomes, "Повтор исчерпанного durable claim"
        outcome = outcomes.pop(0)
        claim.update(state=outcome, finished_at=server.stamp[0])
        if outcome == "passed":
            value.update(state="automatic_ready", variant_id=server.record["variant_id"],
                         prepared_hash=server.record["prepared_hash"], postiz_id="single-repaired-draft")
            return copy.deepcopy(value)
        value.update(state="needs_attention", error="ARTICLE_QUALITY_REJECTED")
        if outcome in {"no_change", "not_repairable"}:
            return copy.deepcopy(value)
        if outcome in {"failed", "unknown"}:
            value["error"] = claim["error"] = "ARTICLE_AUTOMATIC_REPAIR_UNAVAILABLE"
            raise AgentError(value["error"], 503)
        assert outcome == "rejected"
        claim["error"] = "ARTICLE_QUALITY_REJECTED"
        raise AgentError("ARTICLE_QUALITY_REJECTED", 409)

    server.controller.cycle.automatic_repair_status = status
    server.controller.cycle.repair_rejected = repair
    server.repair_outcomes = outcomes
    server.rejected_record = record
    server.trace = path
    return server


def test_known_current_rejection_repairs_once_then_schedules_only_its_returned_draft(repairable):
    server = repairable
    original = copy.deepcopy(server.rejected_record)
    trace = server.trace.read_bytes()
    publish = server.controller.cycle.publish

    def same_draft(ident, *args, **kwargs):
        assert ident == original["id"]
        assert len(server.ledger["records"]) == 1
        assert server.ledger["records"][0]["postiz_id"] == "single-repaired-draft"
        return publish(ident, *args, **kwargs)

    server.controller.cycle.publish = same_draft
    assert server.controller.run_once()["phase"] == "waiting_dispatch"
    final = server.ledger["records"][0]
    assert [c[0] for c in server.calls if c[0] != "pre_draft_guard"] == ["repair", "snapshot", "publish"]
    for key in ("id", "slot", "url", "original_hash", "input_hash", "policy_hash", "target_id", "source_id"):
        assert final.get(key) == original.get(key)
    assert final["preparation_attempts"] == original["preparation_attempts"]
    assert final["automatic_repair_attempts"][0]["state"] == "passed"
    assert server.trace.read_bytes() == trace


def test_only_two_completed_repair_refusals_across_polling_and_restart(repairable):
    server = repairable
    server.repair_outcomes[:] = ["rejected", "rejected"]
    trace = server.trace.read_bytes()
    for number in (1, 2):
        result = server.controller.run_once()
        assert result["phase"] == "rejected"
        assert result["repair_attempts"] == number
        assert sum(c[0] == "repair" for c in server.calls) == number
        assert server.ledger["records"][0]["state"] == "needs_attention"
    saved = copy.deepcopy(server.ledger)
    restarted = ServerCycle(server.core, state=server.state, cycle=server.controller.cycle,
                            selector=server.controller.selector, clock=lambda: server.stamp[0])
    assert restarted.run_once()["repair_status"] == "EXHAUSTED"
    assert restarted.run_once()["phase"] == "rejected"
    assert sum(c[0] == "repair" for c in server.calls) == 2
    assert not any(c[0] in {"prepare", "publish", "cancel"} for c in server.calls)
    assert server.ledger == saved and server.trace.read_bytes() == trace


def test_new_prepare_quality_refusal_can_use_one_repair_in_same_pass(repairable):
    server = repairable
    record = server.ledger["records"].pop()
    server.controller.selector = lambda *_a, **_k: server.article

    def rejected(_article):
        server.calls.append(("prepare",))
        server.ledger["records"].append(record)
        raise AgentError("ARTICLE_QUALITY_REJECTED", 409)

    server.controller.cycle.prepare = rejected
    assert server.controller.run_once()["phase"] == "waiting_dispatch"
    assert [c[0] for c in server.calls if c[0] != "pre_draft_guard"] == ["prepare", "repair", "snapshot", "publish"]


@pytest.mark.parametrize("outcome", ["no_change", "not_repairable"])
def test_completed_no_change_or_nonrepairable_result_stops_samples_until_hour_ends(repairable, outcome):
    server = repairable
    server.repair_outcomes[:] = [outcome]
    assert server.controller.run_once()["repair_status"] == outcome.upper()
    claims = copy.deepcopy(server.ledger["records"][0]["automatic_repair_attempts"])
    assert server.controller.run_once()["phase"] == "rejected"
    assert sum(c[0] == "repair" for c in server.calls) == 1
    server.stamp[0] = (server.rejected_record["slot"] + 1) * 3600
    assert server.controller.run_once()["phase"] == "rejected"
    assert server.ledger["records"][0]["state"] == "cancelled"
    assert server.ledger["records"][0]["automatic_repair_attempts"] == claims
    assert not any(c[0] == "publish" for c in server.calls)


@pytest.mark.parametrize("outcome", ["failed", "unknown"])
def test_repair_infrastructure_or_unknown_failure_is_never_replayed_or_autoclosed(repairable, outcome):
    server = repairable
    server.repair_outcomes[:] = [outcome]
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_UNAVAILABLE"):
        server.controller.run_once()
    saved = copy.deepcopy(server.ledger)
    assert server.controller.run_once()["phase"] == "blocked"
    server.stamp[0] += 3600
    assert server.controller.run_once()["phase"] == "blocked"
    assert server.ledger == saved and sum(c[0] == "repair" for c in server.calls) == 1
    assert not any(c[0] in {"publish", "cancel"} for c in server.calls)


@pytest.mark.parametrize("claim_state", ["correcting", "checking"])
def test_interrupted_repair_claim_blocks_before_and_after_hour(repairable, claim_state):
    server = repairable
    record = server.ledger["records"][0]
    record.update(state="automatic_repairing", automatic_repair_attempts=[
        {"id": "durable", "number": 1, "state": claim_state, "started_at": server.stamp[0]}])
    saved = copy.deepcopy(server.ledger)
    assert server.controller.run_once()["phase"] == "blocked"
    server.stamp[0] += 3600
    assert server.controller.run_once()["phase"] == "blocked"
    assert server.ledger == saved and not any(c[0] in {"repair", "publish", "cancel"} for c in server.calls)


def test_claim_lost_audit_blocks_expired_legacy_refusal_even_before_ledger_claim_was_saved(repairable):
    server = repairable
    server.stamp[0] += 3600
    server.controller.cycle.automatic_repair_status = lambda _: {
        "eligible": False, "reason": "CLAIM_LOST", "attempts": 0}
    saved = copy.deepcopy(server.ledger)
    assert server.controller.run_once()["phase"] == "blocked"
    assert server.ledger == saved and server.calls == []


def test_hour_expiring_during_readonly_repair_status_never_calls_editor(repairable):
    server = repairable
    status = server.controller.cycle.automatic_repair_status

    def crossed(value):
        result = status(value)
        server.stamp[0] = (value["slot"] + 1) * 3600
        return result

    server.controller.cycle.automatic_repair_status = crossed
    original = copy.deepcopy(server.ledger)
    assert server.controller.run_once()["phase"] == "rejected"
    assert server.ledger == original
    assert not any(c[0] in {"repair", "publish", "cancel"} for c in server.calls)


def test_pause_while_repair_finishes_preserves_single_draft_without_scheduling(repairable):
    server = repairable
    repair = server.controller.cycle.repair_rejected

    def paused(ident):
        result = repair(ident)
        server.state.value["paused"] = True
        return result

    server.controller.cycle.repair_rejected = paused
    with pytest.raises(AgentError, match="EDITORIAL_PAUSED"):
        server.controller.run_once()
    assert server.ledger["records"][0]["state"] == "automatic_ready"
    assert server.ledger["records"][0]["postiz_id"] == "single-repaired-draft"
    assert not any(c[0] == "publish" for c in server.calls)


@pytest.mark.parametrize("damage", ["source", "slot", "returned_id", "missing_postiz", "dispatch", "due"])
def test_repair_result_requires_same_source_identity_and_one_unscheduled_draft(repairable, damage):
    server = repairable
    repair = server.controller.cycle.repair_rejected

    def broken(ident):
        result = repair(ident)
        record = server.ledger["records"][0]
        if damage == "returned_id":
            result["id"] = "different"
        elif damage == "source":
            record["original_hash"] = "changed"
        elif damage == "slot":
            record["slot"] += 1
        elif damage == "missing_postiz":
            record.pop("postiz_id")
        elif damage == "dispatch":
            record["dispatch_id"] = "existing-dispatch"
        else:
            record["due_at"] = 0
        return result

    server.controller.cycle.repair_rejected = broken
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_RESULT_INVALID"):
        server.controller.run_once()
    assert sum(c[0] == "repair" for c in server.calls) == 1
    assert not any(c[0] == "publish" for c in server.calls)


@pytest.mark.parametrize("lease", [False, True])
def test_global_delivery_or_lease_appearing_during_repair_blocks_scheduling(repairable, lease):
    server = repairable
    repair = server.controller.cycle.repair_rejected

    def concurrent(ident):
        result = repair(ident)
        server.controller._inventory = lambda: ([] if lease else [
            {"id": "other", "variant_id": "other", "state": "pending"}], lease)
        return result

    server.controller.cycle.repair_rejected = concurrent
    assert server.controller.run_once()["phase"] == "blocked"
    assert server.ledger["records"][0]["state"] == "automatic_ready"
    assert not any(c[0] == "publish" for c in server.calls)


@pytest.mark.parametrize("status", [None,
    {"eligible": "yes", "reason": "ELIGIBLE", "attempts": 0},
    {"eligible": True, "reason": "ELIGIBLE", "attempts": True},
    {"eligible": True, "reason": "ELIGIBLE", "attempts": 2},
    {"eligible": True, "reason": "FAILED", "attempts": 1},
])
def test_malformed_or_contradictory_repair_status_cannot_call_editor(repairable, status):
    server = repairable
    server.controller.cycle.automatic_repair_status = lambda _: copy.deepcopy(status)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_STATUS_INVALID"):
        server.controller.run_once()
    assert not any(c[0] in {"repair", "publish"} for c in server.calls)


def test_current_hour_known_rejection_preserves_candidate_and_all_evidence(server):
    record, path = completed_rejection(server)
    server.ledger["records"].append(record)
    original = copy.deepcopy(record)
    trace = path.read_bytes()
    server.controller.selector = lambda *_a, **_k: pytest.fail("selection before explicit correction")
    for _ in range(2):
        result = server.controller.run_once()
        assert result == {"phase": "rejected", "record_id": record["id"], "slot": record["slot"],
                          "repair_status": "EXHAUSTED", "repair_attempts": 2,
                          "last_error": {"code": "ARTICLE_QUALITY_REJECTED"},
                          "next_run_at": (record["slot"] + 1) * 3600}
        assert server.ledger["records"] == [original]
        assert path.read_bytes() == trace
    assert [c[0] for c in server.calls] == ["pre_draft_guard", "pre_draft_guard"]


def test_expired_known_rejection_closes_once_preserving_source_hour_and_trace(server):
    record, path = completed_rejection(server)
    original = copy.deepcopy(record)
    trace = path.read_bytes()
    server.ledger["records"].append(record)
    server.stamp[0] = (record["slot"] + 1) * 3600
    result = server.controller.run_once()
    assert result["phase"] == "rejected"
    assert result["last_error"] == {"code": "ARTICLE_QUALITY_REJECTED"}
    assert [c[0] for c in server.calls] == ["pre_draft_guard", "pre_draft_guard", "cancel"]
    assert server.ledger["records"] == [{**original, "state": "cancelled"}]
    assert path.read_bytes() == trace
    server.controller.selector = lambda *_a, **_k: None
    assert server.controller.run_once()["phase"] == "no_source"
    assert sum(c[0] == "cancel" for c in server.calls) == 1
    assert server.ledger["records"] == [{**original, "state": "cancelled"}]


def test_new_completed_rejection_is_reported_without_closing_its_current_hour(server):
    record, path = completed_rejection(server)
    trace = path.read_bytes()

    def rejected(_article):
        server.calls.append(("prepare",))
        server.ledger["records"].append(record)
        raise AgentError("ARTICLE_QUALITY_REJECTED", 409)

    server.controller.cycle.prepare = rejected
    result = server.controller.run_once()
    assert result["phase"] == "rejected"
    assert result["last_error"] == {"code": "ARTICLE_QUALITY_REJECTED"}
    assert result["next_run_at"] == (record["slot"] + 1) * 3600
    assert server.ledger["records"][0]["state"] == "needs_attention"
    assert server.controller.run_once()["phase"] == "rejected"
    assert [c[0] for c in server.calls] == ["prepare", "pre_draft_guard", "pre_draft_guard"]
    assert path.read_bytes() == trace


def test_explicitly_corrected_current_record_can_continue_through_existing_ready_path(server):
    record, _ = completed_rejection(server)
    server.ledger["records"].append(record)
    assert server.controller.run_once()["phase"] == "rejected"
    # Только внешний штатный correction-путь устанавливает новое проверенное состояние.
    server.ledger["records"][0] = copy.deepcopy(server.record)
    assert server.controller.run_once()["phase"] == "waiting_dispatch"
    assert [c[0] for c in server.calls] == ["pre_draft_guard", "snapshot", "publish"]


@pytest.mark.parametrize("phase", ["review_failed", "translation_failed", "spelling_failed",
                                   "review_invalid", "translation_invalid"])
def test_infrastructure_or_invalid_review_trace_never_autocloses_even_after_hour(server, phase):
    record, path = completed_rejection(server, extra_events=[{"phase": phase}])
    original = copy.deepcopy(record)
    trace = path.read_bytes()
    server.ledger["records"].append(record)
    server.stamp[0] = (record["slot"] + 1) * 3600
    assert server.controller.run_once()["phase"] == "blocked"
    assert server.ledger["records"] == [original]
    assert path.read_bytes() == trace
    assert server.calls == []


@pytest.mark.parametrize("slot", [None, True, "hour", -1, "future"])
def test_invalid_or_future_refusal_hour_never_autocloses(server, slot):
    record, _ = completed_rejection(server)
    record["slot"] = int(server.stamp[0] // 3600) + 1 if slot == "future" else slot
    server.ledger["records"].append(record)
    assert server.controller.run_once()["phase"] == "blocked"
    assert server.ledger["records"][0]["state"] == "needs_attention"
    assert server.calls == []


def test_incomplete_failure_trace_remains_blocked(server):
    server.ledger["records"].append({**server.record, "state": "needs_attention",
                                    "error": "ARTICLE_QUALITY_REJECTED"})
    server.stamp[0] += 3600
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
    server.state.value["runtime"]["last_error"] = {"code": "ARTICLE_RECONCILIATION_REQUIRED"}
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
    result = server.controller.run_once()
    assert result["phase"] == "waiting_interval"
    assert server.state.value["runtime"]["last_error"] is None
    assert server.state.value["runtime"]["next_run_at"] == result["earliest_due_at"] == actual + 7201
    assert not any(c[0] == "publish" for c in server.calls)
    server.record["slot"] -= 1
    server.stamp[0] += 4000
    assert server.controller.run_once()["phase"] == "waiting_dispatch"
    due = next(c[3] for c in server.calls if c[0] == "publish")
    assert due >= actual + 7201
    assert server.state.value["runtime"]["next_run_at"] == due


@pytest.mark.parametrize("phase", ["preparing", "selecting", "scheduling", "reconciling", "blocked"])
def test_active_or_blocked_phase_clears_stale_next_run_without_actions(server, phase):
    server.state.value["runtime"]["next_run_at"] = server.stamp[0] + 60
    server.state.value["runtime"]["last_error"] = {"code": "STILL_UNRESOLVED"}
    server.controller._phase(phase)
    assert server.state.value["runtime"]["next_run_at"] is None
    assert server.state.value["runtime"]["last_error"] == {"code": "STILL_UNRESOLVED"}
    assert server.calls == []


@pytest.mark.parametrize("due", [True, "tomorrow", float("nan"), float("inf"), -1])
def test_invalid_display_date_is_not_saved_as_next_run(server, due):
    server.controller._phase("waiting_dispatch", result={"due_at": due})
    assert server.state.value["runtime"]["next_run_at"] is None
    assert server.calls == []


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
