"""Подбор нескольких источников не вызывает перевод и не сбрасывает общий журнал."""
import copy
from types import SimpleNamespace

import pytest
import httpx

from max_postiz_agent.config import AgentError
from max_postiz_agent.server_cycle import ServerCycle
from max_postiz_agent.server_state import default_settings, validate_settings


@pytest.fixture
def mixed(monkeypatch):
    policies = [{"source_id": ident} for ident in (
        "nasa_svs_articles", "noirlab_iotw", "esahubble_potw")]
    phases, calls = [], []
    options = default_settings()
    options["source"] = "open_science"
    ledger = {"records": [{"id": "cancelled", "url": "https://svs.gsfc.nasa.gov/1/",
                           "slot": 1, "state": "cancelled"}]}
    article = {"url": "https://esahubble.org/images/potw2552a/", "title": "Scientific title",
               "original": "Complete source", "published_at": 0}
    cycle = SimpleNamespace(_policies=lambda: copy.deepcopy(policies),
        _policy_for_article=lambda value: copy.deepcopy(policies[2]))
    state = SimpleNamespace(assert_running=lambda: None,
                            phase=lambda phase, **details: phases.append((phase, details)))
    controller = ServerCycle(SimpleNamespace(), state=state, cycle=cycle, clock=lambda: 0)

    def nasa(core, configured, received, policy, **kwargs):
        assert configured == options["nasa"] and received is ledger and kwargs["checkpoint"]
        calls.append(policy["source_id"])
        return None

    def science(core, source_id, configured, received, policy, **kwargs):
        assert configured == options["nasa"] and received is ledger and kwargs["checkpoint"]
        assert policy["source_id"] == source_id
        calls.append(source_id)
        if source_id == "noirlab_iotw":
            raise AgentError("SCIENCE_SOURCE_UNAVAILABLE", 503)
        return copy.deepcopy(article)

    monkeypatch.setattr("max_postiz_agent.server_nasa.select_nasa_article", nasa)
    monkeypatch.setattr("max_postiz_agent.server_science.select_science_article", science)
    return SimpleNamespace(controller=controller, options=options, ledger=ledger, article=article,
                           policies=policies, calls=calls, phases=phases, science=science)


def test_unavailable_source_does_not_block_a_different_licensed_source_or_retry(mixed):
    before = copy.deepcopy(mixed.ledger)
    assert mixed.controller._select(mixed.options, mixed.ledger, mixed.policies[0]) == mixed.article
    assert mixed.calls == [p["source_id"] for p in mixed.policies]
    assert mixed.ledger == before
    assert mixed.phases[-1][1]["source_errors"] == [
        {"source_id": "noirlab_iotw", "code": "SCIENCE_SOURCE_UNAVAILABLE"}]


def test_source_priority_rotates_without_a_second_pass(mixed):
    mixed.controller.clock = lambda: 7200
    assert mixed.controller._select(mixed.options, mixed.ledger, mixed.policies[0]) == mixed.article
    assert mixed.calls == ["esahubble_potw"]


def test_real_nasa_timeout_is_not_retried_and_other_licensed_source_is_checked(mixed, monkeypatch):
    from max_postiz_agent.server_nasa import _get
    requests = []
    def timeout(request):
        requests.append(str(request.url))
        raise httpx.ReadTimeout("known GET failure", request=request)
    def nasa(*_args, **_kwargs):
        with httpx.Client(transport=httpx.MockTransport(timeout)) as client:
            return _get(client, "https://svs.gsfc.nasa.gov/api/search/")
    monkeypatch.setattr("max_postiz_agent.server_nasa.select_nasa_article", nasa)
    assert mixed.controller._select(mixed.options, mixed.ledger, mixed.policies[0]) == mixed.article
    assert len(requests) == 1
    assert mixed.calls == ["noirlab_iotw", "esahubble_potw"]


@pytest.mark.parametrize("code", ["EDITORIAL_PAUSED", "EDITORIAL_LEADERSHIP_LOST",
    "NASA_DEDUP_CORPUS_INCOMPLETE", "NASA_EXCLUSIONS_INVALID", "SCIENCE_CHECKPOINT_INVALID",
    "SCIENCE_SOURCE_PROOF_CONFLICT"])
def test_pause_or_lost_leadership_never_falls_through_to_another_source(mixed, monkeypatch, code):
    def failed(*_args, **_kwargs):
        raise AgentError(code, 409)
    monkeypatch.setattr("max_postiz_agent.server_nasa.select_nasa_article", failed)
    with pytest.raises(AgentError, match=code):
        mixed.controller._select(mixed.options, mixed.ledger, mixed.policies[0])
    assert not mixed.calls


def test_result_must_belong_to_the_selected_policy(mixed):
    mixed.controller.clock = lambda: 7200
    mixed.controller.cycle._policy_for_article = lambda _: mixed.policies[0]
    with pytest.raises(AgentError, match="ARTICLE_SOURCE_CHANGED"):
        mixed.controller._select(mixed.options, mixed.ledger, mixed.policies[0])
    assert mixed.calls == ["esahubble_potw"]


def test_new_mode_uses_the_existing_validated_filter_contract():
    original = default_settings()
    configured = copy.deepcopy(original)
    configured["source"] = "open_science"
    configured["nasa"].update(authors=["Observatory"], keywords=["galaxy"], max_words=400)
    assert validate_settings(configured) == configured
    assert default_settings() == original
    configured["nasa"]["max_words"] = 1001
    with pytest.raises(AgentError, match="EDITORIAL_SETTINGS_INVALID"):
        validate_settings(configured)
