"""Карантин полного ответа с потерянной ссылкой не превращает отказ в одобрение."""

import copy
import json
from uuid import NAMESPACE_URL, uuid5

import pytest
from sqlalchemy import select

from max_postiz_agent import article_repair_cycle as protocol_module
from max_postiz_agent.article_links import LEGACY_REFERENCE_CONTRACT, REFERENCE_CONTRACT
from max_postiz_agent.article_repair import ArticleRepair, repair_prompt_name
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


def contract_factory(value, contract):
    """Исторический контракт загружает свой прежний промпт уже в конструкторе."""
    factory = value.cycle.repair_factory

    def create(trace):
        editor = factory(trace)
        return ArticleRepair(editor.editor, prompt_dir=value.core.settings.root / "prompts",
                             trace=trace, contract=contract)

    return create


def legacy_preparation(automatic_cycle, monkeypatch):
    """Создать историческую QA v1 до первого Hy, не переписывая отказ задним числом."""
    (cycle, _, _, _, _, _, _), _ = automatic_cycle
    monkeypatch.setattr("max_postiz_agent.article_cycle.REFERENCE_CONTRACT", LEGACY_REFERENCE_CONTRACT)
    quality_factory = cycle._quality

    def quality(*args, **kwargs):
        value = quality_factory(*args, **kwargs)
        value.reference_contract = LEGACY_REFERENCE_CONTRACT
        return value

    monkeypatch.setattr(cycle, "_quality", quality)


@pytest.fixture
def legacy_repair_cycle(automatic_cycle, monkeypatch):
    legacy_preparation(automatic_cycle, monkeypatch)
    return repair_fixture.__wrapped__(automatic_cycle, monkeypatch)


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
def legacy_linked_repair_cycle(automatic_cycle, monkeypatch):
    legacy_preparation(automatic_cycle, monkeypatch)
    return linked_repair_cycle.__wrapped__(automatic_cycle, monkeypatch)


def legacy_reference_failure(value, monkeypatch, contract):
    # Воспроизводим сохранённый результат прежней версии протокола.
    with monkeypatch.context() as previous:
        previous.setattr(protocol_module, "QUARANTINE_ERRORS",
            protocol_module.QUARANTINE_ERRORS - {"TRANSLATION_REFERENCE_CHANGED"})
        previous.setattr(protocol_module, "REPAIR_CONTRACT", contract)
        previous.setattr(value.cycle, "repair_factory", contract_factory(value, contract))
        with pytest.raises(AgentError, match="TRANSLATION_REFERENCE_CHANGED"):
            value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    assert saved["automatic_repair_attempts"][-1]["state"] == "failed"
    assert saved["error"] == "TRANSLATION_REFERENCE_CHANGED"
    ServerState(value.core).initialize()
    return value


@pytest.fixture
def failed_reference(legacy_linked_repair_cycle, monkeypatch):
    return legacy_reference_failure(legacy_linked_repair_cycle, monkeypatch, "article-repair-v4")


@pytest.fixture
def failed_v5_reference(legacy_linked_repair_cycle, monkeypatch):
    return legacy_reference_failure(legacy_linked_repair_cycle, monkeypatch, "article-repair-v5")


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
        path = value.core.settings.root / "prompts" / repair_prompt_name(claim["contract"])
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


NUMBER_REASON = "Адресный карантин полного ответа: редактор удалил исходный диапазон 50–65 миль."


@pytest.fixture
def numeric_repair_cycle(automatic_cycle, monkeypatch):
    (cycle, _, article, _, _, _, _), _ = automatic_cycle
    article["original"] += " This layer is 80–105 km (50–65 miles) above sea level."
    original = cycle._quality

    def numeric_quality(*args, **kwargs):
        quality = original(*args, **kwargs)
        quality.translator.value["body"] += " Этот слой находится на высоте 80–105 км (50–65 миль) над уровнем моря."
        return quality

    monkeypatch.setattr(cycle, "_quality", numeric_quality)
    value = repair_fixture.__wrapped__(automatic_cycle, monkeypatch)
    value.controls.output = {"title": "Космический ветер", "body": CORRECT_BODY
        + " Этот слой находится на высоте 80–105 км над уровнем моря."}
    return value


@pytest.fixture
def legacy_numeric_repair_cycle(automatic_cycle, monkeypatch):
    legacy_preparation(automatic_cycle, monkeypatch)
    return numeric_repair_cycle.__wrapped__(automatic_cycle, monkeypatch)


@pytest.fixture
def failed_number(legacy_numeric_repair_cycle, monkeypatch):
    value = legacy_numeric_repair_cycle

    # Старый failed создаётся под настоящим v3, а не переименованием истории.
    with monkeypatch.context() as previous:
        previous.setattr(protocol_module, "QUARANTINE_ERRORS",
            protocol_module.QUARANTINE_ERRORS - {"TRANSLATION_NUMBER_CHANGED"})
        previous.setattr(protocol_module, "REPAIR_CONTRACT", "article-repair-v3")
        previous.setattr(value.cycle, "repair_factory", contract_factory(value, "article-repair-v3"))
        with pytest.raises(AgentError, match="TRANSLATION_NUMBER_CHANGED"):
            value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    assert saved["automatic_repair_attempts"][-1]["contract"] == "article-repair-v3"
    assert saved["automatic_repair_attempts"][-1]["state"] == "failed"
    assert saved["error"] == "TRANSLATION_NUMBER_CHANGED"
    ServerState(value.core).initialize()
    return value


def number_quarantine(value):
    return AutomaticArticleRepair(value.cycle).quarantine_failed_number(value.initial["id"], NUMBER_REASON)


def test_future_complete_number_failure_is_quarantined_without_more_inference(numeric_repair_cycle):
    value = numeric_repair_cycle
    with pytest.raises(AgentError, match="TRANSLATION_NUMBER_CHANGED"):
        value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    claim = saved["automatic_repair_attempts"][-1]
    assert claim["state"] == "not_repairable" and claim["outcome_origin"] == "protocol_guard"
    assert claim["contract"] == protocol_module.REPAIR_CONTRACT
    assert claim["error"] == "TRANSLATION_NUMBER_CHANGED"
    assert saved["error"] == "ARTICLE_QUALITY_REJECTED"
    assert value.cycle.automatic_repair_status(saved)["reason"] == "NOT_REPAIRABLE"
    value.stamp[0] += 3600
    assert value.cycle.automatic_repair_status(current(value))["reason"] == "EXPIRED"
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    assert value.trace.read_bytes() == value.initial_trace
    no_delivery(value)


def test_old_v3_number_failure_is_quarantined_once_with_original_evidence(failed_number):
    value = failed_number
    before = current(value)
    previous = before["automatic_repair_attempts"][-1]
    raw_path = value.core.settings.root / previous["repair_trace"]
    raw = raw_path.read_bytes()
    value.stamp[0] += 5 * 3600  # Адресный карантин не восстанавливает просроченный слот.
    result = number_quarantine(value)
    saved = current(value)
    claim = saved["automatic_repair_attempts"][-1]
    assert result["state"] == claim["state"] == "not_repairable"
    assert claim["quarantine_previous_failure"] == previous
    assert claim["contract"] == "article-repair-v3"
    assert claim["error"] == previous["error"] and claim["finished_at"] == previous["finished_at"]
    assert saved["slot"] == before["slot"] and saved["url"] == before["url"]
    assert saved["preparation_attempts"] == before["preparation_attempts"]
    assert saved["error"] == "ARTICLE_QUALITY_REJECTED"
    assert value.cycle.automatic_repair_status(saved)["reason"] == "EXPIRED"
    assert raw_path.read_bytes() == raw and value.trace.read_bytes() == value.initial_trace
    with value.core.db.session() as session:
        audit = session.get(Audit, str(uuid5(NAMESPACE_URL, claim["id"] + ":quarantine")))
        assert audit.details == {"claim_id": claim["id"], "error": "TRANSLATION_NUMBER_CHANGED",
            "trace_hash": claim["repair_trace_hash"], "snapshot_hash": claim["snapshot_hash"]}
        receipt = session.get(Audit, str(uuid5(NAMESPACE_URL, claim["id"] + ":number-quarantine")))
        assert receipt.action == "automatic_article_repair_number_quarantine"
        assert receipt.details["previous_failure"] == previous and receipt.details["reason"] == NUMBER_REASON
    with pytest.raises(AgentError):
        number_quarantine(value)
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)


@pytest.mark.parametrize("damage", ["raw", "raw_code_mismatch", "missing_return", "audit", "binding",
    "snapshot", "input", "prompt", "policy", "source", "lease", "target", "external",
    "network", "unknown", "other_code", "contract"])
def test_number_quarantine_rejects_damage_other_codes_and_unknown_outcomes(failed_number, damage):
    value = failed_number
    ledger = value.cycle._load()
    record = ledger["records"][0]
    claim = record["automatic_repair_attempts"][-1]
    path = value.core.settings.root / claim["repair_trace"]
    if damage in {"raw", "raw_code_mismatch", "missing_return"}:
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        if damage == "raw":
            events[1]["raw"] += " tampered"
        elif damage == "raw_code_mismatch":
            events[1]["raw"] = json.dumps({"title": "Космический ветер", "body": CORRECT_BODY
                + " Этот слой находится на высоте 80–105 км (50–65 миль) над уровнем моря."})
        else:
            events.pop(1)
        path.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")
    elif damage == "audit":
        with value.core.db.session.begin() as session:
            session.delete(session.get(Audit, claim["id"]))
    elif damage == "binding":
        claim["input_hash"] = "0" * 64
    elif damage in {"snapshot", "input"}:
        path = value.core.settings.root / (claim["snapshot_path"] if damage == "snapshot" else record["input_file"])
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "prompt":
        path = value.core.settings.root / "prompts" / repair_prompt_name(claim["contract"])
        path.write_bytes(path.read_bytes() + b" ")
    elif damage == "policy":
        path = value.core.settings.root / "data/hourly-article-policy.local.json"
        policy = json.loads(path.read_text(encoding="utf-8"))
        policy["owner_instruction"] += " changed"
        path.write_text(json.dumps(policy), encoding="utf-8")
    elif damage == "source":
        value.core.settings.source(record["source_id"])["rights"]["allow_derivative"] = False
    elif damage == "lease":
        add_unrelated_lease(value)
    elif damage == "target":
        with value.core.db.session.begin() as session:
            session.get(Target, "max").paused = False
    elif damage == "external":
        record["postiz_id"] = "unconfirmed"
    elif damage in {"network", "unknown", "other_code"}:
        errors = {"network": "OPENCODE_GENERATION_FAILED", "unknown": "POSTIZ_UNKNOWN",
                  "other_code": "ARTICLE_POST_TOO_LONG"}
        record["error"] = claim["error"] = errors[damage]
    else:
        claim["contract"] = "unknown-contract"
    value.cycle._save(ledger)
    before = current(value)
    with pytest.raises((AgentError, OSError)):
        number_quarantine(value)
    assert current(value) == before
    with value.core.db.session() as session:
        assert not session.scalar(select(Audit).where(Audit.action == "automatic_article_repair_quarantined"))
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    assert not value.records and not value.writes


def test_number_quarantine_durable_claim_blocks_retry_after_lost_save(failed_number, monkeypatch):
    value = failed_number
    before = current(value)

    def lost(_ledger):
        raise OSError("simulated lost number quarantine ledger write")

    with monkeypatch.context() as failure:
        failure.setattr(value.cycle, "_save", lost)
        with pytest.raises(OSError, match="lost number quarantine"):
            number_quarantine(value)
    assert current(value) == before
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_QUARANTINE_ALREADY_ATTEMPTED"):
        number_quarantine(value)
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)


def test_number_method_cannot_reclassify_a_reference_failure(failed_reference):
    before = current(failed_reference)
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_NUMBER_QUARANTINE_NOT_SAFE"):
        number_quarantine(failed_reference)
    assert current(failed_reference) == before


@pytest.mark.parametrize("fixture_name,quarantiner,contract", [
    ("failed_reference", quarantine, "article-repair-v4"),
    ("failed_number", number_quarantine, "article-repair-v3"),
    ("failed_v5_reference", quarantine, "article-repair-v5"),
])
def test_legacy_failed_claim_uses_its_old_prompt_and_context_after_v5_upgrade(
        request, fixture_name, quarantiner, contract):
    value = request.getfixturevalue(fixture_name)
    before = current(value)
    claim = before["automatic_repair_attempts"][-1]
    assert claim["contract"] == contract
    prompts = {"ARTICLE_TRANSLATE.txt", "ARTICLE_REVIEW_MEANING_FIRST_V1.txt", repair_prompt_name(contract)}
    assert set(claim["prompts"]) == prompts
    assert "reference_contract" not in claim
    old_prompt_hashes = copy.deepcopy(claim["prompts"])
    trace = value.core.settings.root / claim["repair_trace"]
    original = trace.read_bytes()
    events = [json.loads(line) for line in original.decode("utf-8").splitlines()]
    context_keys = {"source_untrusted", "candidate_untrusted", "feedback_untrusted", "numeric_tokens_must_match_exactly"}
    if contract == "article-repair-v5":
        context_keys.add("reference_url_manifest")
        assert events[0]["context"]["reference_url_manifest"]["contract"] == LEGACY_REFERENCE_CONTRACT
    assert set(events[0]["context"]) == context_keys
    # Даже другой новый промпт не изменяет чистое воспроизведение старого ответа.
    future_prompt = value.core.settings.root / "prompts/ARTICLE_REPAIR_V6.txt"
    future_prompt.write_bytes(future_prompt.read_bytes() + b"\nFuture-only prompt revision.\n")
    assert AutomaticArticleRepair(value.cycle)._prompts(contract) == old_prompt_hashes
    quarantiner(value)
    saved = current(value)
    assert saved["automatic_repair_attempts"][-1]["prompts"] == old_prompt_hashes
    assert saved["automatic_repair_attempts"][-1]["quarantine_previous_failure"] == claim
    assert trace.read_bytes() == original
    assert value.cycle.automatic_repair_status(saved)["reason"] == "NOT_REPAIRABLE"
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)


def test_new_claim_binds_v6_prompt_and_exact_reference_manifest(linked_repair_cycle):
    value = linked_repair_cycle
    with pytest.raises(AgentError, match="TRANSLATION_REFERENCE_CHANGED"):
        value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    claim = saved["automatic_repair_attempts"][-1]
    assert claim["contract"] == "article-repair-v6" and claim["reference_contract"] == REFERENCE_CONTRACT
    assert set(claim["prompts"]) == {"ARTICLE_TRANSLATE.txt", "ARTICLE_REVIEW_MEANING_FIRST_V1.txt",
                                     "ARTICLE_REPAIR_V6.txt"}
    assert claim["prompts"] == AutomaticArticleRepair(value.cycle)._prompts()
    events = [json.loads(line) for line in
              (value.core.settings.root / claim["repair_trace"]).read_text(encoding="utf-8").splitlines()]
    assert events[0]["binding"]["prompt_hash"] == claim["prompts"]["ARTICLE_REPAIR_V6.txt"]
    assert events[0]["binding"]["reference_contract"] == REFERENCE_CONTRACT
    assert events[0]["context"]["reference_url_manifest"] == {"contract": REFERENCE_CONTRACT,
        "required_urls": [REFERENCE], "current_urls": [REFERENCE], "missing_urls": [], "unexpected_urls": []}
    assert value.cycle.automatic_repair_status(saved)["reason"] == "NOT_REPAIRABLE"
    old_prompt = value.core.settings.root / "prompts/ARTICLE_REPAIR.txt"
    old_prompt.write_bytes(old_prompt.read_bytes() + b"\nUnrelated legacy prompt revision.\n")
    assert value.cycle.automatic_repair_status(saved)["reason"] == "NOT_REPAIRABLE"
    prompt = value.core.settings.root / "prompts/ARTICLE_REPAIR_V6.txt"
    prompt.write_bytes(prompt.read_bytes() + b"\nChanged after saved v6 claim.\n")
    assert value.cycle.automatic_repair_status(saved)["reason"] == "TRACE_INVALID"
    assert current(value) == saved
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)


@pytest.mark.parametrize("contract", ["article-repair-v3", "article-repair-v4", "article-repair-v5"])
def test_old_no_change_is_not_resampled_with_v6_prompt(legacy_repair_cycle, monkeypatch, contract):
    value = legacy_repair_cycle
    value.controls.output = lambda context: context["candidate_untrusted"]
    with monkeypatch.context() as previous:
        previous.setattr(protocol_module, "REPAIR_CONTRACT", contract)
        previous.setattr(value.cycle, "repair_factory", contract_factory(value, contract))
        with pytest.raises(AgentError, match="ARTICLE_REPAIR_UNCHANGED"):
            value.cycle.repair_rejected(value.initial["id"])
    before = current(value)
    claim = before["automatic_repair_attempts"][-1]
    assert claim["contract"] == contract and claim["state"] == "no_change"
    assert set(claim["prompts"]) == {"ARTICLE_TRANSLATE.txt", "ARTICLE_REVIEW_MEANING_FIRST_V1.txt",
                                     repair_prompt_name(contract)}
    trace = value.core.settings.root / claim["repair_trace"]
    raw = trace.read_bytes()
    prompt = value.core.settings.root / "prompts/ARTICLE_REPAIR_V6.txt"
    prompt.write_bytes(prompt.read_bytes() + b"\nFuture-only prompt revision.\n")
    assert value.cycle.automatic_repair_status(before)["reason"] == "NO_CHANGE"
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_NO_CHANGE"):
        value.cycle.repair_rejected(before["id"])
    assert current(value) == before and trace.read_bytes() == raw
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)


@pytest.mark.parametrize("damage", ["source", "manifest"])
def test_v6_complete_proposal_with_changed_context_is_blocked_before_fresh_quality(repair_cycle, damage):
    value = repair_cycle

    def tamper_started_context():
        claim = current(value)["automatic_repair_attempts"][-1]
        path = value.core.settings.root / claim["repair_trace"]
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert len(events) == 1 and events[0]["phase"] == "repair_started"
        if damage == "source":
            events[0]["context"]["source_untrusted"]["title"] += " Changed source."
        else:
            events[0]["context"]["reference_url_manifest"]["missing_urls"] = [REFERENCE]
        path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")

    value.controls.editor_hook = tamper_started_context
    value.review.update(approved=True, issues=[])
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID"):
        value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    assert saved["automatic_repair_attempts"][-1]["state"] == "failed"
    assert value.cycle.automatic_repair_status(saved)["reason"] == "FAILED"
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    assert value.trace.read_bytes() == value.initial_trace
    no_delivery(value)


def test_v6_cached_quarantine_does_not_trust_a_changed_url_manifest(linked_repair_cycle):
    value = linked_repair_cycle

    def tamper_started_manifest():
        claim = current(value)["automatic_repair_attempts"][-1]
        path = value.core.settings.root / claim["repair_trace"]
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        events[0]["context"]["reference_url_manifest"]["required_urls"] = []
        path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")

    value.controls.editor_hook = tamper_started_manifest
    with pytest.raises(AgentError, match="TRANSLATION_REFERENCE_CHANGED"):
        value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    claim = saved["automatic_repair_attempts"][-1]
    assert claim["state"] == "failed" and claim.get("outcome_origin") is None
    assert value.cycle.automatic_repair_status(saved)["reason"] == "FAILED"
    with value.core.db.session() as session:
        assert not session.scalar(select(Audit).where(Audit.action == "automatic_article_repair_quarantined"))
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)


@pytest.mark.parametrize("damage", ["start", "attempt", "record"])
def test_v6_requires_consistent_frozen_quality_reference_markers_before_reserving_a_claim(repair_cycle, damage):
    value = repair_cycle
    ledger = value.cycle._load()
    record = ledger["records"][0]
    assert record["reference_contract"] == REFERENCE_CONTRACT
    if damage == "start":
        path = value.core.settings.root / record["quality_trace"]
        events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
        assert events[0]["context"]["reference_contract"] == REFERENCE_CONTRACT
        events[0]["context"]["reference_contract"] = LEGACY_REFERENCE_CONTRACT
        path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    elif damage == "attempt":
        record["preparation_attempts"][-1]["reference_contract"] = LEGACY_REFERENCE_CONTRACT
        value.cycle._save(ledger)
    else:
        record["reference_contract"] = LEGACY_REFERENCE_CONTRACT
        value.cycle._save(ledger)
    before = current(value)
    assert value.cycle.automatic_repair_status(before)["reason"] == "TRACE_INVALID"
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_TRACE_INVALID"):
        value.cycle.repair_rejected(before["id"])
    assert current(value) == before
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 0}
    no_delivery(value)


@pytest.mark.parametrize("damage", ["claim", "context", "snapshot"])
def test_v6_reference_contract_binding_cannot_be_downgraded_after_quarantine(linked_repair_cycle, damage):
    value = linked_repair_cycle
    with pytest.raises(AgentError, match="TRANSLATION_REFERENCE_CHANGED"):
        value.cycle.repair_rejected(value.initial["id"])
    ledger = value.cycle._load()
    record = ledger["records"][0]
    claim = record["automatic_repair_attempts"][-1]
    if damage == "claim":
        claim.pop("reference_contract")
        value.cycle._save(ledger)
    else:
        path = value.core.settings.root / (claim["repair_trace"] if damage == "context" else claim["snapshot_path"])
        if damage == "context":
            events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            events[0]["context"]["reference_url_manifest"]["contract"] = LEGACY_REFERENCE_CONTRACT
            path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
        else:
            snapshot = json.loads(path.read_text(encoding="utf-8"))
            snapshot.pop("reference_contract")
            path.write_text(json.dumps(snapshot, ensure_ascii=False), encoding="utf-8")
    before = current(value)
    assert value.cycle.automatic_repair_status(before)["reason"] in {"CLAIM_LOST", "TRACE_INVALID"}
    assert current(value) == before
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)
