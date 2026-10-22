"""Карантин полного ответа с потерянной ссылкой не превращает отказ в одобрение."""

import copy
import json
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import select

from max_postiz_agent import article_repair_cycle as protocol_module
from max_postiz_agent.article_repair_cycle import AutomaticArticleRepair
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit
from max_postiz_agent.hub_models import Target
from max_postiz_agent.server_cycle import ServerCycle
from max_postiz_agent.server_state import ServerState
from test_automatic_article_repair import (CORRECT_BODY, add_unrelated_lease, current,
    no_delivery, repair_cycle as repair_fixture)

pytest_plugins = ["test_automatic_article_repair"]

REFERENCE = "https://science.example.org/first-look/"
REASON = "Адресный карантин полного сохранённого ответа: редактор потерял исходную научную ссылку."


@pytest.fixture
def linked_repair_cycle(automatic_cycle, monkeypatch):
    (cycle, _, article, _, _, _, _), _ = automatic_cycle
    article["original"] += " More information: " + REFERENCE
    original = cycle._quality

    def linked_quality(*args, **kwargs):
        quality = original(*args, **kwargs)
        quality.translator.value["body"] += " Подробности: " + REFERENCE
        return quality

    monkeypatch.setattr(cycle, "_quality", linked_quality)
    value = repair_fixture.__wrapped__(automatic_cycle, monkeypatch)
    # Полный JSON предлагает содержательную правку, но теряет literal URL.
    value.controls.output = {"title": "Космический ветер", "body": CORRECT_BODY}
    return value


@pytest.fixture
def failed_reference(linked_repair_cycle, monkeypatch):
    value = linked_repair_cycle
    # Воспроизводим сохранённый результат прежней версии протокола.
    with monkeypatch.context() as previous:
        previous.setattr(protocol_module, "QUARANTINE_ERRORS",
            protocol_module.QUARANTINE_ERRORS - {"TRANSLATION_REFERENCE_CHANGED"})
        with pytest.raises(AgentError, match="TRANSLATION_REFERENCE_CHANGED"):
            value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    assert saved["automatic_repair_attempts"][-1]["state"] == "failed"
    assert saved["error"] == "TRANSLATION_REFERENCE_CHANGED"
    ServerState(value.core).initialize()
    return value


def quarantine(value):
    return AutomaticArticleRepair(value.cycle).quarantine_failed_reference(value.initial["id"], REASON)


def test_future_complete_lost_reference_is_quarantined_without_more_qa(linked_repair_cycle):
    value = linked_repair_cycle
    with pytest.raises(AgentError, match="TRANSLATION_REFERENCE_CHANGED"):
        value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    claim = saved["automatic_repair_attempts"][-1]
    assert claim["state"] == "not_repairable" and claim["outcome_origin"] == "protocol_guard"
    assert saved["error"] == "ARTICLE_QUALITY_REJECTED"
    assert claim["error"] == "TRANSLATION_REFERENCE_CHANGED"
    trace = value.core.settings.root / claim["repair_trace"]
    before = trace.read_bytes()
    assert value.cycle.automatic_repair_status(saved)["reason"] == "NOT_REPAIRABLE"
    assert trace.read_bytes() == before
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_NOT_REPAIRABLE"):
        value.cycle.repair_rejected(saved["id"])
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    assert value.trace.read_bytes() == value.initial_trace
    no_delivery(value)


def test_old_failure_has_one_durable_quarantine_and_preserves_all_original_fields(failed_reference):
    value = failed_reference
    before = current(value)
    previous = before["automatic_repair_attempts"][-1]
    trace = value.core.settings.root / previous["repair_trace"]
    old_raw = trace.read_bytes()
    assert value.cycle.automatic_repair_status(before)["reason"] == "FAILED"
    assert current(value) == before  # status не выполняет адресное восстановление.
    result = quarantine(value)
    saved = current(value)
    claim = saved["automatic_repair_attempts"][-1]
    assert result["state"] == claim["state"] == "not_repairable"
    assert claim["quarantine_previous_failure"] == previous
    assert claim["error"] == previous["error"] and claim["finished_at"] == previous["finished_at"]
    assert saved["error"] == "ARTICLE_QUALITY_REJECTED" and saved["state"] == "needs_attention"
    assert saved["slot"] == before["slot"] and saved["url"] == before["url"]
    assert saved["preparation_attempts"] == before["preparation_attempts"]
    assert trace.read_bytes() == old_raw and value.trace.read_bytes() == value.initial_trace
    with value.core.db.session() as session:
        audit = session.get(Audit, str(uuid5(NAMESPACE_URL, claim["id"] + ":quarantine")))
        assert audit.details == {"claim_id": claim["id"], "error": previous["error"],
            "trace_hash": claim["repair_trace_hash"], "snapshot_hash": claim["snapshot_hash"]}
        receipt = session.scalar(select(Audit).where(
            Audit.action == "automatic_article_repair_reference_quarantine"))
        assert receipt.details["reason"] == REASON and receipt.details["previous_failure"] == previous
    assert value.cycle.automatic_repair_status(saved)["reason"] == "NOT_REPAIRABLE"
    with pytest.raises(AgentError):
        quarantine(value)
    value.stamp[0] += 3600
    assert value.cycle.automatic_repair_status(current(value))["reason"] == "EXPIRED"
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)


@pytest.mark.parametrize("damage", ["claim_audit", "claim_ledger", "binding", "trace", "raw_valid",
    "raw_wrong_error", "missing_return", "snapshot", "input", "prompt", "policy", "source",
    "record_input", "lease", "target", "external", "running", "other_error", "unknown", "finished"])
def test_damaged_or_uncertain_failure_cannot_be_quarantined(failed_reference, damage):
    value = failed_reference
    ledger = value.cycle._load()
    record = ledger["records"][0]
    claim = record["automatic_repair_attempts"][-1]
    trace = value.core.settings.root / claim["repair_trace"]
    if damage == "claim_audit":
        with value.core.db.session.begin() as session:
            session.delete(session.get(Audit, claim["id"]))
    elif damage == "claim_ledger":
        record.pop("automatic_repair_attempts")
    elif damage == "binding":
        claim["input_hash"] = "0" * 64
    elif damage in {"trace", "raw_valid", "raw_wrong_error", "missing_return"}:
        events = [json.loads(line) for line in trace.read_text(encoding="utf-8").splitlines()]
        if damage == "trace":
            events[0]["context"]["numeric_tokens_must_match_exactly"] = ["123"]
        elif damage == "raw_valid":
            events[1]["raw"] = json.dumps({"title": "Космический ветер", "body": CORRECT_BODY
                + " Подробности: " + REFERENCE})
        elif damage == "raw_wrong_error":
            events[1]["raw"] = json.dumps({"title": "Космический ветер", "body": CORRECT_BODY + " 123"})
        else:
            events.pop(1)
        trace.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    elif damage in {"snapshot", "input"}:
        path = value.core.settings.root / (claim["snapshot_path"] if damage == "snapshot" else record["input_file"])
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "prompt":
        path = value.core.settings.root / "prompts/ARTICLE_REPAIR.txt"
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "policy":
        path = value.core.settings.root / "data/hourly-article-policy.local.json"
        policy = json.loads(path.read_text(encoding="utf-8"))
        policy["owner_instruction"] += " changed"
        path.write_text(json.dumps(policy), encoding="utf-8")
    elif damage == "source":
        value.core.settings.source(record["source_id"])["rights"]["allow_derivative"] = False
    elif damage == "record_input":
        record["input_hash"] = "0" * 64
    elif damage == "lease":
        add_unrelated_lease(value)
    elif damage == "target":
        with value.core.db.session.begin() as session:
            session.get(Target, "max").paused = False
    elif damage == "external":
        record["postiz_id"] = "unconfirmed"
    elif damage == "running":
        store = ServerState(value.core)
        store.control("start", store.read()["revision"], "owner")
    elif damage in {"other_error", "unknown"}:
        record["error"] = claim["error"] = "TRANSLATION_NUMBER_CHANGED" if damage == "other_error" else "POSTIZ_UNKNOWN"
    elif damage == "finished":
        claim["finished_at"] = claim["started_at"] - 1
    value.cycle._save(ledger)
    expected = copy.deepcopy(current(value))
    with pytest.raises((AgentError, OSError)):
        quarantine(value)
    assert current(value) == expected
    with value.core.db.session() as session:
        assert not session.scalar(select(Audit).where(Audit.action == "automatic_article_repair_quarantined"))
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    assert not value.records and not value.writes


def test_audit_before_failed_save_blocks_unknown_recovery_retry(failed_reference, monkeypatch):
    value = failed_reference
    before = current(value)

    def lost(_ledger):
        raise OSError("simulated lost quarantine ledger write")

    with monkeypatch.context() as failure:
        failure.setattr(value.cycle, "_save", lost)
        with pytest.raises(OSError, match="lost quarantine"):
            quarantine(value)
    assert current(value) == before
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_QUARANTINE_ALREADY_ATTEMPTED"):
        quarantine(value)
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)


@pytest.mark.parametrize("reason", ["", "too short", None])
def test_quarantine_requires_an_explicit_audit_reason(failed_reference, reason):
    value = failed_reference
    before = current(value)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_QUARANTINE_REASON_REQUIRED"):
        AutomaticArticleRepair(value.cycle).quarantine_failed_reference(value.initial["id"], reason)
    assert current(value) == before


# Прежние проверки полного невалидного JSON и неизвестной сети сохранены.
def invalid_record(value):
    return value.cycle._load()["records"][0]


def invalid_quarantine(value):
    value.controls.output = "not json"
    with pytest.raises(AgentError, match="ARTICLE_REPAIR_RESPONSE_INVALID"):
        value.cycle.repair_rejected(value.initial["id"])
    assert invalid_record(value)["automatic_repair_attempts"][0]["outcome_origin"] == "protocol_guard"


def test_complete_invalid_response_closes_next_hour_without_freeing_source_or_repeating_models(repair_cycle):
    value = repair_cycle
    invalid_quarantine(value)
    value.stamp[0] += 3600
    store = ServerState(value.core)
    store.initialize()
    store.control("start", 1, "owner")
    controller = ServerCycle(value.core, state=store, cycle=value.cycle, clock=lambda: value.stamp[0])
    assert controller._close_known_rejection(invalid_record(value)) is True
    saved = invalid_record(value)
    assert saved["state"] == "cancelled" and saved["slot"] == value.initial["slot"]
    assert saved["url"] == value.article["url"] and saved["original_hash"] == value.initial["original_hash"]
    assert value.trace.read_bytes() == value.initial_trace
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    assert not value.records and not value.writes


@pytest.mark.parametrize("damage", ["raw", "time", "binding", "audit"])
def test_quarantine_proof_damage_blocks_expired_auto_close(repair_cycle, damage):
    from sqlalchemy import select
    from max_postiz_agent.db import Audit
    value = repair_cycle
    invalid_quarantine(value)
    claim = invalid_record(value)["automatic_repair_attempts"][0]
    if damage == "audit":
        with value.core.db.session.begin() as session:
            audit = session.scalar(select(Audit).where(Audit.action == "automatic_article_repair_quarantined"))
            session.delete(audit)
    else:
        path = value.core.settings.root / claim["repair_trace"]
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if damage == "raw":
            events[1]["raw"] += " changed"
        elif damage == "time":
            events[1]["at"] = True
        else:
            events[1]["binding"]["input_hash"] = "changed"
        path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    value.stamp[0] += 3600
    assert value.cycle.automatic_repair_status(invalid_record(value))["reason"] == "TRACE_INVALID"
    controller = ServerCycle(value.core, cycle=value.cycle, clock=lambda: value.stamp[0])
    assert controller._close_known_rejection(invalid_record(value)) is False
    assert value.counts["qwen"] == 1 and not value.records


def test_unknown_editor_network_failure_is_never_quarantined(repair_cycle):
    value = repair_cycle
    value.controls.output = AgentError("OPENCODE_GENERATION_FAILED", 503)
    with pytest.raises(AgentError, match="OPENCODE_GENERATION_FAILED"):
        value.cycle.repair_rejected(value.initial["id"])
    value.stamp[0] += 3600
    saved = invalid_record(value)
    assert saved["automatic_repair_attempts"][0]["state"] == "failed"
    assert value.cycle.automatic_repair_status(saved)["reason"] == "FAILED"
    assert value.counts["qwen"] == 1 and not value.records
