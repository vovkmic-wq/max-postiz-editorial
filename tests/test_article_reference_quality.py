"""Новый контракт literal URL не переписывает исторические проверки качества."""

import copy
import json

import httpx
import pytest
from sqlalchemy import select

from max_postiz_agent.article_links import LEGACY_REFERENCE_CONTRACT, REFERENCE_CONTRACT
from max_postiz_agent.article_quality import (
    ArticleQuality,
    article_reference_urls,
    article_review_context,
    rubric_translation_gate,
)
from max_postiz_agent.config import AgentError
from max_postiz_agent.db import Audit

pytest_plugins = ["test_automatic_policy"]

TITLE = "A small telescope studies the night sky"
ORIGINAL = (
    "A small telescope observed the night sky for 3 hours. Researchers studied the "
    "light from a distant star and checked their observations before sharing the results."
)
URL = "https://science.nasa.gov/example/"
REFERENCE = "https://en.wikipedia.org/wiki/Asterism_(astronomy)"
CANDIDATE = {"title": "Небольшой телескоп изучает ночное небо",
             "body": "Небольшой телескоп наблюдал ночное небо 3 часа. Исследователи изучили "
                     "свет далёкой звезды и проверили наблюдения, прежде чем сообщить результаты."}
APPROVED = {"approved": True, "topic": "science", "issues": [], "warnings": []}


class Model:
    immutable_candidates = True

    def __init__(self, response):
        self.response, self.prompts = response, []

    def generate_text(self, prompt):
        self.prompts.append(json.loads(prompt))
        return json.dumps(self.response, ensure_ascii=False), {}


def checker(*, contract=None, rubric="meaning-first-v1", candidate=None):
    events, requests = [], []
    translator = Model(candidate or CANDIDATE)
    reviewer = Model(APPROVED if rubric == "meaning-first-v1" else
                     {key: APPROVED[key] for key in ("approved", "topic", "issues")})

    def spelling(request):
        requests.append(request)
        return httpx.Response(200, json={"matches": []})

    quality = ArticleQuality(translator, reviewer, trace=events.append, review_rubric=rubric,
        reference_contract=contract, transport=httpx.MockTransport(spelling))
    return quality, translator, reviewer, events, requests


@pytest.mark.parametrize(("rubric", "contract"), [
    ("legacy", LEGACY_REFERENCE_CONTRACT), ("meaning-first-v1", REFERENCE_CONTRACT),
])
def test_default_contract_is_explicit_for_new_meaning_first_only(rubric, contract):
    quality, *_ = checker(rubric=rubric)
    try:
        assert quality.reference_contract == contract
    finally:
        quality.close()


@pytest.mark.parametrize("contract", ["article-links-v3", "", True, [], {}])
def test_unknown_contract_never_reaches_models(contract):
    translator, reviewer = Model(CANDIDATE), Model(APPROVED)
    with pytest.raises(AgentError, match="ARTICLE_REFERENCE_CONTRACT_INVALID"):
        ArticleQuality(translator, reviewer, reference_contract=contract)
    assert not translator.prompts and not reviewer.prompts


@pytest.mark.parametrize("rubric", ["legacy", "meaning-first-v1"])
def test_legacy_review_context_keeps_exact_old_fields(rubric):
    expected = {"instruction": "instruction", "source_untrusted": "source",
                "translation_untrusted": "translation"}
    if rubric == "meaning-first-v1":
        expected.update(review_rubric=rubric, review_prompt_hash="prompt-hash")
    assert article_review_context("instruction", "source", "translation", rubric, "prompt-hash",
                                  reference_contract=LEGACY_REFERENCE_CONTRACT) == expected


def test_v2_review_context_contains_computed_literal_urls_only():
    context = article_review_context("instruction", "source\n" + REFERENCE,
        "перевод\n" + REFERENCE, "meaning-first-v1", "prompt-hash", reference_contract=REFERENCE_CONTRACT)
    assert context == {"instruction": "instruction", "source_untrusted": "source\n" + REFERENCE,
        "translation_untrusted": "перевод\n" + REFERENCE, "review_rubric": "meaning-first-v1",
        "review_prompt_hash": "prompt-hash", "reference_check": {"contract": REFERENCE_CONTRACT,
            "source_urls": [REFERENCE], "candidate_urls": [REFERENCE]}}


def test_legacy_parser_and_gate_remain_exact_while_v2_blocks_lost_parenthesis():
    source = TITLE + "\n\n" + ORIGINAL + "\n" + REFERENCE
    translation = CANDIDATE["title"] + "\n\n" + CANDIDATE["body"] + "\n" + REFERENCE[:-1]
    assert article_reference_urls(REFERENCE) == {REFERENCE[:-1]}
    # Исторический недостаток воспроизводится для replay, но не для новых QA.
    rubric_translation_gate(source, translation, "meaning-first-v1")
    with pytest.raises(AgentError, match="TRANSLATION_REFERENCE_CHANGED"):
        rubric_translation_gate(source, translation, "meaning-first-v1",
                                reference_contract=REFERENCE_CONTRACT)


@pytest.mark.parametrize("route", ["prepare", "candidate"])
@pytest.mark.parametrize("contract", [LEGACY_REFERENCE_CONTRACT, REFERENCE_CONTRACT])
def test_exact_reference_contract_is_bound_to_trace_context_and_evidence(route, contract):
    candidate = {**CANDIDATE, "body": CANDIDATE["body"] + "\n" + REFERENCE}
    quality, translator, reviewer, events, requests = checker(contract=contract, candidate=candidate)
    try:
        result = (quality.prepare(TITLE, ORIGINAL + "\n" + REFERENCE, URL) if route == "prepare" else
                  quality.check_candidate(candidate["title"], candidate["body"], TITLE,
                                          ORIGINAL + "\n" + REFERENCE, URL))
    finally:
        quality.close()
    assert len(translator.prompts) == (1 if route == "prepare" else 0)
    assert len(reviewer.prompts) == len(requests) == 1
    first = events[0]["context"] if route == "prepare" else events[0]
    evidence = result["machine_review_evidence"]
    if contract == REFERENCE_CONTRACT:
        assert first["reference_contract"] == result["reference_contract"] == contract
        expected = {"contract": contract, "source_urls": [REFERENCE], "candidate_urls": [REFERENCE]}
        assert reviewer.prompts[0]["reference_check"] == evidence["reference_check"] == expected
    else:
        assert "reference_contract" not in first and "reference_contract" not in result
        assert "reference_check" not in reviewer.prompts[0] and "reference_check" not in evidence
    assert result["translation"] == candidate["title"] + "\n\n" + candidate["body"]
    assert evidence["review_raw"] == json.dumps(APPROVED, ensure_ascii=False)
    assert events[-1]["result"] == result


@pytest.mark.parametrize("suffix", ["", ")extra", "?new=value", "#different"])
def test_v2_reference_changes_stop_before_reviewer_or_spelling(suffix):
    body = CANDIDATE["body"] + "\n" + REFERENCE[:-1] + suffix
    quality, translator, reviewer, events, requests = checker()
    try:
        with pytest.raises(AgentError, match="TRANSLATION_REFERENCE_CHANGED"):
            quality.check_candidate(CANDIDATE["title"], body, TITLE, ORIGINAL + "\n" + REFERENCE, URL)
    finally:
        quality.close()
    assert not translator.prompts and not reviewer.prompts and not requests
    assert events[-1]["error"] == "TRANSLATION_REFERENCE_CHANGED"


def test_new_cycle_contract_is_durable_before_hy_and_in_audit(automatic_cycle, monkeypatch):
    values, _ = automatic_cycle
    cycle, core, article, *_ = values
    original_quality = cycle._quality
    observed = []

    def quality(record_id=None, review_only=False):
        record = copy.deepcopy(cycle._load()["records"][0])
        with core.db.session() as session:
            audit = session.scalar(select(Audit).where(Audit.action == "hourly_article_prepare_attempt"))
            observed.append((record, copy.deepcopy(audit.details)))
        return original_quality(record_id, review_only)

    monkeypatch.setattr(cycle, "_quality", quality)
    result = cycle.prepare(article)
    record, audit = observed[0]
    assert record["state"] == "preparing"
    assert record["reference_contract"] == record["preparation_attempts"][0]["reference_contract"] == REFERENCE_CONTRACT
    assert audit["attempt"] == 1 and audit["reference_contract"] == REFERENCE_CONTRACT
    assert result["reference_contract"] == REFERENCE_CONTRACT


def test_real_cycle_factory_keeps_missing_record_contract_legacy(cycle):
    value, *_ = cycle
    value.quality_factory = None
    quality = value._quality()
    try:
        assert quality.reference_contract == LEGACY_REFERENCE_CONTRACT
    finally:
        quality.close()
        quality.translator.client.close()
        quality.reviewer.client.close()


@pytest.mark.parametrize("history", ["review_protocol_recoveries", "automatic_corrections",
    "automatic_spelling_recoveries", "automatic_roni_spelling_recoveries"])
def test_separate_quality_trace_inherits_only_durable_owner_contract(cycle, history):
    value, *_ = cycle
    trace_id = "11111111-2222-4333-8444-555555555555"
    value._save({"records": [{"id": "owner", "slot": 1, "state": "checking",
        "reference_contract": REFERENCE_CONTRACT,
        history: [{"quality_trace": f"data/hourly-articles/quality-{trace_id}.local.jsonl"}]}]})
    assert value._quality_reference_contract(trace_id) == REFERENCE_CONTRACT
    assert value._quality_reference_contract("unowned-trace") == LEGACY_REFERENCE_CONTRACT


def test_separate_quality_trace_with_two_owners_fails_closed(cycle):
    value, *_ = cycle
    trace_id = "11111111-2222-4333-8444-555555555555"
    value._save({"records": [{"id": owner, "slot": number, "state": "checking",
        "quality_trace": f"data/hourly-articles/quality-{trace_id}.local.jsonl"}
        for number, owner in enumerate(("first", "second"))]})
    with pytest.raises(AgentError, match="ARTICLE_REFERENCE_CONTRACT_INVALID"):
        value._quality_reference_contract(trace_id)


@pytest.mark.parametrize("tamper", ["record", "attempt", "start", "review-contract", "review-source",
                                  "review-candidate", "review-field"])
def test_rejected_v2_candidate_history_does_not_accept_downgraded_or_forged_manifest(
        automatic_cycle, tamper):
    values, review = automatic_cycle
    cycle, core, article, *_ = values
    review.update(approved=False, issues=["The result omits an important scientific fact."])
    with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
        cycle.prepare(article)
    record = cycle._load()["records"][0]
    attempt = record["preparation_attempts"][0]
    # Исходный отказ остаётся узнаваемым и запрещает новый опрос того же RU.
    assert cycle._load_canonical_initial_candidates(record, attempt, article)
    path = core.settings.root / record["quality_trace"]
    events = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    context = next(event["context"] for event in events if event["phase"] == "review_started")
    if tamper == "record":
        record.pop("reference_contract")
    elif tamper == "attempt":
        attempt.pop("reference_contract")
    elif tamper == "start":
        events[0]["context"].pop("reference_contract")
    elif tamper == "review-contract":
        context["reference_check"]["contract"] = LEGACY_REFERENCE_CONTRACT
    elif tamper == "review-source":
        context["reference_check"]["source_urls"] = ["https://untrusted.example/injected"]
    elif tamper == "review-candidate":
        context["reference_check"]["candidate_urls"] = ["https://untrusted.example/injected"]
    else:
        context["untrusted_extra"] = "do not validate references"
    path.write_text("".join(json.dumps(event, ensure_ascii=False) + "\n" for event in events), encoding="utf-8")
    with pytest.raises(AgentError, match="ARTICLE_REJECTED_CANDIDATE_TRACE_REQUIRED"):
        cycle._load_canonical_initial_candidates(record, attempt, article)
