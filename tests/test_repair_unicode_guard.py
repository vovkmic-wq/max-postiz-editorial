"""V7 запрещает латинскую маску до новой QA; V3–V6 читаются прежним кодом."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from max_postiz_agent.article_links import REFERENCE_CONTRACT
from max_postiz_agent.article_repair import (
    ArticleRepair, REPAIR_CONTRACT, _latin_mask, repair_prompt_name,
)
from max_postiz_agent.article_repair_cycle import MAX_AUTOMATIC_REPAIRS
from max_postiz_agent.config import AgentError
from test_article_repair import ARTICLE, CANDIDATE, FEEDBACK, Editor
from test_automatic_article_repair import CORRECT_BODY, current, no_delivery, repair_cycle as repair_fixture

pytest_plugins = ["test_automatic_article_repair"]


def named_case(before="Хоралек", after="Хорáлек"):
    article = {**ARTICLE, "original": ARTICLE["original"] + " The picture was taken by Petr Horalek."}
    previous = {**CANDIDATE, "body": CANDIDATE["body"] + " Автор снимка — " + before + "."}
    candidate = {**previous, "body": previous["body"].replace(before, after)}
    return article, previous, candidate


@pytest.mark.parametrize("after", ["Хорáлек", "Хорaлек", "Horalek", "Horálek", "Hóralek"])
def test_v7_blocks_full_and_mixed_latin_in_actual_diff_before_fresh_quality(after):
    article, previous, candidate = named_case(after=after)
    editor, events = Editor(candidate), []
    with pytest.raises(AgentError, match="ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN"):
        ArticleRepair(editor, trace=events.append).propose(article, previous, FEEDBACK)
    assert REPAIR_CONTRACT == "article-repair-v7"
    assert len(editor.calls) == 1
    assert [event["phase"] for event in events] == ["repair_started", "repair_returned", "repair_failed"]
    assert events[1]["raw"] == json.dumps(candidate, ensure_ascii=False)
    assert events[-1]["error"] == "ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN"
    assert events[0]["binding"]["contract"] == "article-repair-v7"
    assert events[0]["binding"]["reference_contract"] == REFERENCE_CONTRACT


@pytest.mark.parametrize("contract", ["article-repair-v3", "article-repair-v4", "article-repair-v5", "article-repair-v6"])
def test_historical_contracts_replay_the_same_mixed_candidate_and_exact_old_evidence(contract):
    article, previous, candidate = named_case()
    editor, original_events = Editor(candidate), []
    original = ArticleRepair(editor, trace=original_events.append, contract=contract).propose(article, previous, FEEDBACK)
    raw = original_events[1]["raw"]
    replay_editor, replay_events = Editor(raw), []
    replay = ArticleRepair(replay_editor, trace=replay_events.append, contract=contract).propose(article, previous, FEEDBACK)
    assert replay == original and replay_events == original_events
    assert replay_editor.calls == editor.calls
    assert original["evidence"]["contract"] == contract
    assert original["candidate"] == candidate and original["raw"] == raw
    assert ("reference_contract" in original["evidence"]) == (contract == "article-repair-v6")
    assert _latin_mask("Хоралек", "Хорáлек") is False
    assert _latin_mask("Хоралек", "Хорáлек", guard_mixed=True) is True


def test_v7_rejects_mixed_latin_in_model_report_even_when_actual_candidate_is_cyrillic():
    article, previous, _ = named_case()
    candidate = {**previous, "body": previous["body"].replace("изучила", "изучили")}
    output = {"candidate": candidate, "changes": [{"field": "body", "before": "Хоралек", "after": "Хорáлек",
        "source_quote": "Petr Horalek", "reason": "Восстановлено написание имени по английскому оригиналу."}]}
    editor, events = Editor(output), []
    with pytest.raises(AgentError, match="ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN"):
        ArticleRepair(editor, trace=events.append).propose(article, previous, FEEDBACK)
    assert events[-1]["error"] == "ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN" and len(editor.calls) == 1


@pytest.mark.parametrize("before,after", [
    ("Хоралк", "Хоралек"), ("учёные изучила", "учёные изучили"),
    ("NASA Hy-MT2 Nino", "NASA Hy-MT2 Niño"), ("Horalek", "Horálek"),
])
def test_v7_allows_cyrillic_typos_and_existing_unicode_latin_names(before, after):
    assert _latin_mask(before, after, guard_mixed=True) is False


def test_v7_candidate_keeps_unchanged_latin_name_and_acronym_while_correcting_cyrillic_typo():
    article, previous, _ = named_case()
    previous["body"] += " NASA и Horálek сохранены."
    candidate = {**previous, "body": previous["body"].replace("изучила", "изучили")}
    editor = Editor(candidate)
    result = ArticleRepair(editor).propose(article, previous, FEEDBACK)
    assert result["candidate"] == candidate and len(editor.calls) == 1


def test_v7_prompt_is_new_and_old_v6_file_is_unchanged():
    prompts = Path(__file__).resolve().parents[1] / "prompts"
    old = (prompts / "ARTICLE_REPAIR_V6.txt").read_text(encoding="utf-8")
    new = (prompts / "ARTICLE_REPAIR_V7.txt").read_text(encoding="utf-8")
    assert repair_prompt_name("article-repair-v6") == "ARTICLE_REPAIR_V6.txt"
    assert repair_prompt_name("article-repair-v7") == "ARTICLE_REPAIR_V7.txt"
    assert hashlib.sha256(old.encode()).hexdigest() == "6aac286e48fe749391b49255373a1ff5067fd914086e70b48a935bebbe5b7209"
    assert new.startswith(old) and "«Хоралек» → «Хорáлек»" in new
    assert "article-repair-v7" in new and "латинские буквы с диакритикой" in new
    assert hashlib.sha256(old.encode()).hexdigest() != hashlib.sha256(new.encode()).hexdigest()


@pytest.fixture
def mixed_repair_cycle(automatic_cycle, monkeypatch):
    (cycle, _, article, _, _, _, _), _ = automatic_cycle
    article["original"] += " The picture was taken by Petr Horalek."
    original_quality = cycle._quality

    def quality(*args, **kwargs):
        result = original_quality(*args, **kwargs)
        result.translator.value["body"] += " Автор снимка — Хоралек."
        return result

    monkeypatch.setattr(cycle, "_quality", quality)
    value = repair_fixture.__wrapped__(automatic_cycle, monkeypatch)
    value.controls.output = {"title": "Космический ветер", "body": CORRECT_BODY + " Автор снимка — Хорáлек."}
    return value


def test_new_v7_complete_mixed_response_is_quarantined_without_min_lt_or_retry(mixed_repair_cycle):
    value = mixed_repair_cycle
    original_record = copy.deepcopy(current(value))
    with pytest.raises(AgentError, match="ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN"):
        value.cycle.repair_rejected(value.initial["id"])
    saved = current(value)
    claim = saved["automatic_repair_attempts"][-1]
    assert claim["contract"] == "article-repair-v7" and claim["reference_contract"] == REFERENCE_CONTRACT
    assert claim["state"] == "not_repairable" and claim["outcome_origin"] == "protocol_guard"
    assert claim["error"] == "ARTICLE_REPAIR_LATIN_MASK_FORBIDDEN"
    assert saved["error"] == "ARTICLE_QUALITY_REJECTED" and MAX_AUTOMATIC_REPAIRS == 2
    assert value.cycle.automatic_repair_status(saved)["reason"] == "NOT_REPAIRABLE"
    trace = value.core.settings.root / claim["repair_trace"]
    raw = trace.read_bytes()
    events = [json.loads(line) for line in raw.decode("utf-8").splitlines()]
    assert [event["phase"] for event in events] == ["repair_started", "repair_returned", "repair_failed"]
    assert json.loads(events[1]["raw"]) == value.controls.output
    assert "ARTICLE_REPAIR_V7.txt" in claim["prompts"] and "ARTICLE_REPAIR_V6.txt" not in claim["prompts"]
    assert value.trace.read_bytes() == value.initial_trace
    assert saved["preparation_attempts"] == original_record["preparation_attempts"]
    with pytest.raises(AgentError, match="ARTICLE_AUTOMATIC_REPAIR_NOT_REPAIRABLE"):
        value.cycle.repair_rejected(saved["id"])
    assert trace.read_bytes() == raw
    assert value.counts == {"hy": 1, "min": 1, "lt": 1, "qwen": 1}
    no_delivery(value)
