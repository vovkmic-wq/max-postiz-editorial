"""Future practical rubric: bounded language advice, unchanged factual hard gates."""
import copy
import json
from pathlib import Path

import httpx
import pytest

from max_postiz_agent.article_quality import (
    ArticleQuality, HOURLY_PRACTICAL_REVIEW_RUBRIC, MEANING_FIRST_REVIEW_RUBRIC,
    normalize_rubric_review, practical_language_budget, review_prompt_name,
    rubric_spelling, rubric_translation_gate,
)
from max_postiz_agent.config import AgentError

RUBRIC = HOURLY_PRACTICAL_REVIEW_RUBRIC
TITLE = "A small telescope studies the night sky"
ORIGINAL = ("A small telescope observed the night sky for 3 hours. Researchers studied the "
    "light from a distant star and checked their observations before sharing the results.")
RU = {"title": "Небольшой телескоп изучает ночное небо", "body":
    "Небольшой телескоп наблюдал ночное небо 3 часа. Исследователи изучили "
    "свет далёкой звезды и проверили наблюдения, прежде чем сообщить результаты."}
APPROVED = {"approved": True, "topic": "science", "issues": [],
    "warnings": ["Буквальный оборот допустим для машинного перевода."]}
URL = "https://svs.gsfc.nasa.gov/1234/"


class Model:
    immutable_candidates = True

    def __init__(self, value):
        self.value, self.prompts = value, []

    def generate_text(self, prompt):
        self.prompts.append(json.loads(prompt))
        return json.dumps(self.value, ensure_ascii=False), {}


def finding(issue="misspelling", category="TYPOS", kind="UnknownWord"):
    return {"rule": {"id": "TEST_RULE", "issueType": issue, "category": {"id": category}},
        "type": {"typeName": kind}, "offset": 0, "length": 9, "message": "Замечание"}


@pytest.fixture
def prompts(tmp_path):
    root = Path(__file__).resolve().parents[1] / "prompts"
    (tmp_path / "ARTICLE_TRANSLATE.txt").write_text((root / "ARTICLE_TRANSLATE.txt").read_text(encoding="utf-8"), encoding="utf-8")
    (tmp_path / review_prompt_name(RUBRIC)).write_text("Practical independent review fixture", encoding="utf-8")
    return tmp_path


def check(prompts, matches=None, review=None, status=200):
    reviewer = Model(APPROVED if review is None else review)
    quality = ArticleQuality(Model(RU), reviewer, review_rubric=RUBRIC, prompt_dir=prompts,
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json={"matches": matches or []})))
    return quality, reviewer


@pytest.mark.parametrize("item", [finding(), finding("grammar", "GRAMMAR", "Other"),
    finding("typographical", "PUNCTUATION", "Other"), finding("style", "STYLE", "Other")])
def test_practical_preserves_ordinary_language_findings_as_advice(item):
    original = copy.deepcopy(item)
    advisory, blocking = rubric_spelling(ORIGINAL, RU["title"] + "\n" + RU["body"], [item], RUBRIC)
    assert blocking == [] and item == original
    assert advisory[0]["match"] == item
    assert advisory[0]["reason"] == "NON_BLOCKING_PRACTICAL_LANGUAGE_ADVICE"
    assert advisory[0]["review_rubric"] == RUBRIC
    assert advisory[0]["readability_budget"]["max_findings"] == 8


def test_same_spelling_and_grammar_still_block_historical_meaning_first():
    for item in (finding(), finding("grammar", "GRAMMAR", "Other")):
        assert rubric_spelling(ORIGINAL, RU["title"], [item], MEANING_FIRST_REVIEW_RUBRIC) == ([], [item])


def test_readability_budget_excludes_urls_and_grows_only_with_russian_words():
    assert practical_language_budget("слово " * 80) == {"russian_words": 80, "max_findings": 8}
    assert practical_language_budget("слово " * 81) == {"russian_words": 81, "max_findings": 9}
    assert practical_language_budget("слово " * 8 + "https://example.org/" + "/русское" * 100) == {
        "russian_words": 8, "max_findings": 8}


def test_excessive_findings_remain_blocking():
    items = [finding() for _ in range(9)]
    assert rubric_spelling(ORIGINAL, RU["title"], items, RUBRIC) == ([], items)


@pytest.mark.parametrize("change", [{"offset": -1}, {"offset": 99999}, {"length": 0},
    {"rule": {"id": "UNKNOWN", "issueType": "new_unsupported_type", "category": {"id": "GRAMMAR"}}},
    {"rule": {}}, {"type": {"typeName": "Unrecognized"}}])
def test_malformed_or_unsupported_findings_are_never_silenced(change):
    item = {**finding(), **change}
    assert rubric_spelling(ORIGINAL, RU["title"], [item], RUBRIC) == ([], [item])


def test_practical_machine_evidence_retains_raw_findings_and_exact_rubric(prompts):
    items = [finding(), finding("grammar", "GRAMMAR", "Other")]
    quality, reviewer = check(prompts, items)
    try:
        result = quality.check_candidate(**RU, original_title=TITLE, original=ORIGINAL, url=URL)
    finally:
        quality.close()
    evidence = result["machine_review_evidence"]
    assert result["machine_review_status"] == "passed"
    assert result["review_rubric"] == evidence["review_rubric"] == RUBRIC
    assert evidence["spelling_matches"] == items and evidence["spelling_unresolved"] == []
    assert len(evidence["spelling_adjudicated"]) == 2
    assert evidence["hard_gates_passed"] is True
    assert json.loads(evidence["review_raw"]) == APPROVED
    assert reviewer.prompts[0]["review_rubric"] == RUBRIC
    assert result["reference_contract"] == "article-links-v2"


@pytest.mark.parametrize("review", [
    {"approved": False, "topic": "science", "issues": ["Главный объект заменён созвездием."], "warnings": []},
    {"approved": True, "topic": "science", "issues": ["Отрицание потеряно."], "warnings": []},
    {"approved": True, "topic": "science", "issues": [], "warnings": "incorrect type"},
])
def test_substantive_refusal_or_protocol_problem_still_blocks(prompts, review):
    quality, reviewer = check(prompts, review=review)
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            quality.check_candidate(**RU, original_title=TITLE, original=ORIGINAL, url=URL)
    finally:
        quality.close()
    assert len(reviewer.prompts) == 1


@pytest.mark.parametrize(("source", "translation", "code"), [
    ("The telescope studied 3 stars.", "Телескоп изучил 4 звезды.", "TRANSLATION_NUMBER_CHANGED"),
    ("The telescope studied the night sky.", "The telescope studied the night sky.", "TRANSLATION_NOT_RUSSIAN"),
    ("The telescope studied https://example.org/exact the night sky.", "Телескоп изучил ночное небо.", "TRANSLATION_REFERENCE_CHANGED"),
])
def test_practical_keeps_numbers_language_and_literal_references(source, translation, code):
    with pytest.raises(AgentError) as error:
        rubric_translation_gate(source, translation, RUBRIC, reference_contract="article-links-v2")
    assert error.value.code == code


def test_language_service_failure_is_not_advice(prompts):
    quality, _ = check(prompts, status=503)
    try:
        with pytest.raises(AgentError, match="ARTICLE_SPELLING_UNAVAILABLE"):
            quality.check_candidate(**RU, original_title=TITLE, original=ORIGINAL, url=URL)
    finally:
        quality.close()


def test_contradictory_positive_verdict_is_invalid_only_for_new_rubric():
    raw = {**APPROVED, "issues": ["A substantive omission."]}
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_SCHEMA_INVALID"):
        normalize_rubric_review(raw, RUBRIC)
    assert normalize_rubric_review(raw, MEANING_FIRST_REVIEW_RUBRIC) == raw


def test_initial_immutable_translation_uses_practical_evidence(prompts):
    quality, reviewer = check(prompts, matches=[finding()])
    try:
        result = quality.prepare(TITLE, ORIGINAL, URL)
    finally:
        quality.close()
    assert result["machine_review_status"] == "passed"
    assert result["review_rubric"] == RUBRIC
    assert len(reviewer.prompts) == 1
    assert result["machine_review_evidence"]["spelling_matches"] == [finding()]


def test_approved_semantics_cannot_override_high_error_density(prompts):
    quality, reviewer = check(prompts, matches=[finding() for _ in range(9)])
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            quality.check_candidate(**RU, original_title=TITLE, original=ORIGINAL, url=URL)
    finally:
        quality.close()
    assert len(reviewer.prompts) == 1


def test_utf16_finding_cannot_split_a_surrogate_pair():
    item = {**finding(), "offset": 1, "length": 1}
    assert rubric_spelling(ORIGINAL, "😀" + RU["body"], [item], RUBRIC) == ([], [item])


def test_style_only_and_optional_typography_never_exhaust_readability_budget():
    items = [finding("style", "STYLE", "Other") for _ in range(20)]
    items += [finding("typographical", "PUNCTUATION", "Other") for _ in range(20)]
    advisory, blocking = rubric_spelling(ORIGINAL, RU["title"], items, RUBRIC)
    assert blocking == [] and len(advisory) == 40
    assert all(item["readability_budget"]["nonstyle_readability_findings"] == 0 for item in advisory)


def test_mixed_findings_budget_counts_language_errors_and_retains_every_advice():
    items = [finding("style", "STYLE", "Other") for _ in range(20)] + [finding() for _ in range(3)]
    advisory, blocking = rubric_spelling(ORIGINAL, RU["title"], items, RUBRIC)
    assert blocking == [] and len(advisory) == 23
    assert all(item["readability_budget"]["nonstyle_readability_findings"] == 3 for item in advisory)
    over = items + [finding("grammar", "GRAMMAR", "Other") for _ in range(6)]
    advisory, blocking = rubric_spelling(ORIGINAL, RU["title"], over, RUBRIC)
    assert len(advisory) == 20 and len(blocking) == 9


@pytest.mark.parametrize("initial", [True, False])
def test_practical_fresh_scientific_evidence_replays_identically_at_approval(prompts, initial):
    from test_article_quality import (
        SPACETIME_BODY, SPACETIME_ORIGINAL, SPACETIME_TITLE, spacetime_match,
    )
    from max_postiz_agent.automatic_policy import _validate

    english_title = "Extreme mass ratio inspirals"
    translated = {"title": SPACETIME_TITLE, "body": SPACETIME_BODY}
    match = spacetime_match(SPACETIME_TITLE + "\n\n" + SPACETIME_BODY)
    quality = ArticleQuality(Model(translated), Model(APPROVED), review_rubric=RUBRIC,
        prompt_dir=prompts, transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"matches": [match]})))
    try:
        result = (quality.prepare(english_title, SPACETIME_ORIGINAL, URL) if initial
                  else quality.check_candidate(**translated, original_title=english_title,
                      original=SPACETIME_ORIGINAL, url=URL))
    finally:
        quality.close()
    evidence = {**result["machine_review_evidence"], "trace_hash": "0" * 64}
    assert evidence["spelling_matches"] == [match]
    assert evidence["spelling_adjudicated"][0]["reason"] == "SOURCE_BOUND_VERIFIED_SCIENTIFIC_TERM"
    assert _validate(evidence, result["title"], result["body"],
        {"title": english_title, "body": SPACETIME_ORIGINAL, "url": URL})["approved"] is True
