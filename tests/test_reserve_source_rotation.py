"""Reserve source fairness changes order only; source guards and history stay exact."""
import copy
from types import SimpleNamespace

import pytest

from max_postiz_agent.server_cycle import ServerCycle


@pytest.fixture
def selection(monkeypatch):
    policies = [{"source_id": name, "rights_evidence": "unchanged-" + name}
                for name in ("nasa_svs", "noirlab_iotw", "esahubble_potw")]
    calls = []
    selected = {"source": None}
    settings = {"source": "open_science", "nasa": {"max_words": 180}}
    ledger = {"records": []}
    cycle = SimpleNamespace(reserve_mode=False, _policies=lambda: policies,
        _policy_for_article=lambda article: next(p for p in policies if p["source_id"] == article["selected"]))
    stamp = [3600]

    def choose(source_id, options, actual_ledger, policy, **kwargs):
        assert options is settings["nasa"] and actual_ledger is ledger
        assert policy is next(p for p in policies if p["source_id"] == source_id)
        assert kwargs == {"now": stamp[0], "checkpoint": True}
        calls.append(source_id)
        return {"selected": source_id} if selected["source"] == source_id else None

    monkeypatch.setattr("max_postiz_agent.server_nasa.select_nasa_article",
        lambda _core, options, actual_ledger, policy, **kwargs:
            choose("nasa_svs", options, actual_ledger, policy, **kwargs))
    monkeypatch.setattr("max_postiz_agent.server_science.select_science_article",
        lambda _core, source_id, options, actual_ledger, policy, **kwargs:
            choose(source_id, options, actual_ledger, policy, **kwargs))
    state = SimpleNamespace(assert_running=lambda: None, phase=lambda *args, **kwargs: None)
    controller = ServerCycle(SimpleNamespace(), state=state, cycle=cycle, clock=lambda: stamp[0])
    return SimpleNamespace(controller=controller, cycle=cycle, calls=calls, ledger=ledger, policies=policies,
                           settings=settings, stamp=stamp, selected=selected)


def run(value):
    before = copy.deepcopy((value.ledger, value.policies, value.settings))
    result = value.controller._select(value.settings, value.ledger, value.policies[0])
    assert (value.ledger, value.policies, value.settings) == before
    return result


def test_legacy_keeps_hour_rotation_even_with_reserve_history(selection):
    value = selection
    value.ledger["records"] = [{"reserve_contract": "article-reserve-v1", "source_id": "noirlab_iotw",
                                "state": "cancelled"}]
    run(value)
    assert value.calls == ["noirlab_iotw", "esahubble_potw", "nasa_svs"]


def test_initial_reserve_without_prior_candidate_keeps_hour_rotation(selection):
    value = selection
    value.cycle.reserve_mode = True
    value.stamp[0] = 2 * 3600
    run(value)
    assert value.calls == ["esahubble_potw", "nasa_svs", "noirlab_iotw"]


@pytest.mark.parametrize("state", ["cancelled", "quality_ready", "published", "rejected"])
def test_last_known_noirlab_candidate_rotates_to_hubble_without_waiting_next_hour(selection, state):
    value = selection
    value.cycle.reserve_mode = True
    value.ledger["records"] = [{"reserve_contract": "article-reserve-v1", "source_id": "noirlab_iotw",
                                "state": state}]
    value.selected["source"] = "esahubble_potw"
    assert run(value) == {"selected": "esahubble_potw"}
    assert value.calls == ["esahubble_potw"]


def test_tail_historical_records_do_not_override_last_reserve_source(selection):
    value = selection
    value.cycle.reserve_mode = True
    value.ledger["records"] = [
        {"reserve_contract": "article-reserve-v1", "source_id": "nasa_svs", "state": "cancelled"},
        {"source_id": "esahubble_potw", "state": "published"},
        {"reserve_contract": "a-different-contract", "source_id": "noirlab_iotw", "state": "cancelled"},
    ]
    run(value)
    assert value.calls == ["noirlab_iotw", "esahubble_potw", "nasa_svs"]


def test_only_historical_records_do_not_seed_the_new_reserve_rotation(selection):
    value = selection
    value.cycle.reserve_mode = True
    value.ledger["records"] = [{"source_id": "esahubble_potw", "state": "published"},
                                {"source_id": "nasa_svs", "state": "cancelled"}]
    run(value)
    assert value.calls == ["noirlab_iotw", "esahubble_potw", "nasa_svs"]


@pytest.mark.parametrize("source", [None, "not_registered"])
def test_missing_or_unknown_source_falls_back_to_clock_when_no_known_reserve_exists(selection, source):
    value = selection
    value.cycle.reserve_mode = True
    value.ledger["records"] = [{"reserve_contract": "article-reserve-v1", "source_id": source,
                                "state": "cancelled"}]
    run(value)
    assert value.calls == ["noirlab_iotw", "esahubble_potw", "nasa_svs"]


def test_unknown_tail_does_not_erase_last_registered_reserve_source(selection):
    value = selection
    value.cycle.reserve_mode = True
    value.ledger["records"] = [
        {"reserve_contract": "article-reserve-v1", "source_id": "esahubble_potw", "state": "cancelled"},
        {"reserve_contract": "article-reserve-v1", "source_id": "not_registered", "state": "cancelled"},
    ]
    run(value)
    assert value.calls == ["nasa_svs", "noirlab_iotw", "esahubble_potw"]
