"""A versioned fidelity rubric tolerates advice while preserving substantive vetoes."""

import copy
import hashlib
import json

import httpx
import pytest

from max_postiz_agent.article_quality import (
    LEGACY_REVIEW_RUBRIC,
    MEANING_FIRST_REVIEW_RUBRIC,
    ArticleQuality,
    _normalize_review,
    normalize_rubric_review,
    rubric_spelling,
    rubric_translation_gate,
)
from max_postiz_agent.config import AgentError, fingerprint
from test_article_quality import ORIGINAL, TITLE, TRANSLATION, URL, Model

RUBRIC = MEANING_FIRST_REVIEW_RUBRIC
PASS = {"approved": True, "topic": "science", "issues": [], "warnings": []}
ADVICE = {**PASS, "warnings": ["Возможна более плавная формулировка заголовка."]}
REJECT = {"approved": False, "topic": "science", "issues": ["Изменено отрицание в исходнике."],
          "warnings": ["Стиль заголовка можно улучшить."]}
STYLE = {"message": "Optional less repetitive wording", "offset": 0, "length": 2,
         "rule": {"id": "OPTIONAL_STYLE", "issueType": "style", "category": {"id": "STYLE"}},
         "type": {"typeName": "Other"}}


def checker(review=None, matches=None, status=200, translator=None, **kwargs):
    translator = translator or Model(TRANSLATION)
    reviewer = Model(PASS if review is None else review)
    events = []
    check = ArticleQuality(translator, reviewer, trace=events.append, review_rubric=RUBRIC,
        transport=httpx.MockTransport(lambda _: httpx.Response(status,
            json={"matches": [] if matches is None else matches})), **kwargs)
    return check, translator, reviewer, events


@pytest.mark.parametrize("method", ["prepare", "candidate"])
def test_style_advice_is_a_pass_with_raw_advice_bound_to_exact_evidence(method):
    check, translator, reviewer, events = checker(review=ADVICE, matches=[STYLE])
    try:
        if method == "prepare":
            result = check.prepare(TITLE, ORIGINAL, URL)
        else:
            result = check.check_candidate(TRANSLATION["title"], TRANSLATION["body"], TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert len(reviewer.prompts) == 1
    assert result["machine_review_status"] == "passed" and result["warnings"] == ADVICE["warnings"]
    assert result["review_rubric"] == RUBRIC
    assert result["review_prompt_hash"] == hashlib.sha256(check.review_prompt.encode()).hexdigest()
    evidence = result["machine_review_evidence"]
    assert evidence["review"] == ADVICE and json.loads(evidence["review_raw"]) == ADVICE
    assert evidence["review_rubric"] == RUBRIC
    assert evidence["review_prompt_hash"] == result["review_prompt_hash"]
    assert evidence["spelling_matches"] == [STYLE] and evidence["spelling_unresolved"] == []
    assert evidence["spelling_adjudicated"] == [
        {"match": STYLE, "reason": "NON_BLOCKING_STYLE_RULE", "review_rubric": RUBRIC}]
    assert evidence["text_hash"] == fingerprint({"title": result["title"], "body": result["body"]})
    assert evidence["source_hash"] == fingerprint({"title": TITLE, "body": ORIGINAL, "url": URL})
    assert reviewer.prompts[0]["review_rubric"] == RUBRIC
    assert reviewer.prompts[0]["review_prompt_hash"] == result["review_prompt_hash"]
    assert events[-1]["phase"] == "accepted"
    if method == "candidate":
        assert translator.prompts == []


def test_new_prompt_is_selected_explicitly_and_legacy_stays_unchanged():
    modern, *_ = checker()
    legacy = ArticleQuality(Model(TRANSLATION), Model({key: PASS[key] for key in ("approved", "topic", "issues")}),
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": []})))
    try:
        assert "Rubric: meaning-first-v1" in modern.review_prompt
        assert "warnings" in modern.review_prompt
        assert "temporal, spatial, directional or causal" in modern.review_prompt
        assert "Read the title as a complete Russian phrase and reject any unidiomatic wording." in legacy.review_prompt
        assert modern.review_prompt_hash != legacy.review_prompt_hash
        assert legacy.review_rubric == LEGACY_REVIEW_RUBRIC
        assert normalize_rubric_review(REJECT, RUBRIC)["approved"] is False
        with pytest.raises(AgentError, match="ARTICLE_REVIEW_SCHEMA_INVALID"):
            _normalize_review(PASS)
    finally:
        modern.close()
        legacy.close()


@pytest.mark.parametrize("bad", [
    {key: PASS[key] for key in ("approved", "topic", "issues")},
    {**PASS, "approved": "true"}, {**PASS, "warnings": "just style"},
    {**PASS, "warnings": [""]}, {**PASS, "warnings": ["a"] * 9},
    {**PASS, "warnings": [{"issue": "not a plain string"}]},
    {**PASS, "warnings": ["a" * 1025]}, {**PASS, "extra": "cannot weaken the schema"},
])
def test_new_schema_requires_explicit_advice_array_without_coercion(bad):
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_SCHEMA_INVALID"):
        normalize_rubric_review(bad, RUBRIC)


def test_full_negative_and_advisory_findings_are_preserved_without_verdict_conversion():
    review = {**REJECT, "issues": ["x" * 1024] * 3, "warnings": ["y" * 1024] * 3}
    before = copy.deepcopy(review)
    normalized = normalize_rubric_review(review, RUBRIC)
    assert normalized == review == before and normalized["approved"] is False
    normalized["warnings"].clear()
    assert review == before


def test_unchanged_dedicated_rejection_is_not_sampled_again_or_changed_to_pass():
    translator = Model(TRANSLATION)
    translator.immutable_candidates = True
    check, _, reviewer, events = checker(review=REJECT, translator=translator)
    reviewer.responses.append(PASS)
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert len(reviewer.prompts) == 1
    assert next(event["review"] for event in events if event["phase"] == "review_parsed") == REJECT
    assert not any(event["phase"] == "accepted" for event in events)


def test_historical_human_export_keeps_new_rubric_negative_status_negative():
    check, *_ = checker(review=REJECT, allow_human_rejection=True)
    try:
        result = check.check_candidate(TRANSLATION["title"], TRANSLATION["body"], TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert result["machine_review_status"] == "rejected"
    assert result["machine_review_evidence"]["review"] == REJECT


@pytest.mark.parametrize("change", [
    lambda x: x["rule"].update(issueType="grammar"),
    lambda x: x["rule"].update(issueType="misspelling"),
    lambda x: x["rule"].update(issueType="typographical"),
    lambda x: x["rule"].pop("issueType"),
    lambda x: x["type"].update(typeName="UnknownWord"),
    lambda x: x["rule"]["category"].update(id="TYPOS"),
    lambda x: x["rule"]["category"].update(id="GRAMMAR"),
    lambda x: x["rule"]["category"].pop("id"),
])
def test_real_or_unknown_lt_errors_cannot_be_disguised_as_advice(change):
    match = copy.deepcopy(STYLE)
    change(match)
    adjudicated, unresolved = rubric_spelling(TITLE + ORIGINAL, TRANSLATION["body"], [match], RUBRIC)
    assert not adjudicated and unresolved == [match]
    check, *_ = checker(matches=[match])
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            check.check_candidate(TRANSLATION["title"], TRANSLATION["body"], TITLE, ORIGINAL, URL)
    finally:
        check.close()


def test_style_matches_remain_blocking_under_the_legacy_rubric():
    assert rubric_spelling(ORIGINAL, TRANSLATION["body"], [STYLE], LEGACY_REVIEW_RUBRIC) == ([], [STYLE])


def test_lt_infrastructure_failure_is_never_an_advisory_or_pass():
    check, *_ = checker(status=503)
    try:
        with pytest.raises(AgentError, match="ARTICLE_SPELLING_UNAVAILABLE") as error:
            check.check_candidate(TRANSLATION["title"], TRANSLATION["body"], TITLE, ORIGINAL, URL)
        assert error.value.status == 503
    finally:
        check.close()


@pytest.mark.parametrize("url", ["", "https://example.org/replaced/", "https://example.org/source/extra"])
def test_literal_embedded_source_references_are_hard_gates_before_review(url):
    source = ORIGINAL + " Read [the source](https://example.org/source/)."
    translated = TRANSLATION["body"] + (" См. [источник](" + url + ")." if url else "")
    check, _, reviewer, events = checker()
    try:
        with pytest.raises(AgentError, match="TRANSLATION_REFERENCE_CHANGED"):
            check.check_candidate(TRANSLATION["title"], translated, TITLE, source, URL)
    finally:
        check.close()
    assert not reviewer.prompts and any(e["phase"] == "gate_rejected" for e in events)


def test_markdown_anchor_translation_preserves_doi_and_source_urls():
    reference = "https://www.science.org/doi/10.1126/sciadv.aed8089"
    source = ORIGINAL + " See [a new study](" + reference + ")."
    translated = TRANSLATION["body"] + " См. [новое исследование](" + reference + ")."
    rubric_translation_gate(TITLE + "\n\n" + source, TRANSLATION["title"] + "\n\n" + translated, RUBRIC)


@pytest.mark.parametrize("changed", [TRANSLATION["body"].replace("3", "4"), ORIGINAL])
def test_numbers_and_russian_language_remain_blocking(changed):
    check, _, reviewer, _ = checker()
    try:
        with pytest.raises(AgentError, match="TRANSLATION_NUMBER_CHANGED|TRANSLATION_NOT_RUSSIAN"):
            check.check_candidate(TRANSLATION["title"], changed, TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert not reviewer.prompts


@pytest.mark.parametrize("rubric", ["unknown", None, []])
def test_unrecognized_rubric_cannot_silently_fall_back_to_a_looser_check(rubric):
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_RUBRIC_INVALID"):
        rubric_spelling(ORIGINAL, TRANSLATION["body"], [], rubric)
    with pytest.raises(AgentError, match="ARTICLE_REVIEW_RUBRIC_INVALID"):
        rubric_translation_gate(ORIGINAL, TRANSLATION["body"], rubric)
