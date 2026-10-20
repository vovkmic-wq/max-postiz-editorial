"""Only the documented MUR term can resolve this scientific LT false positive."""
import copy
import json
from urllib.parse import parse_qs

import httpx
import pytest

from max_postiz_agent.article_quality import (ArticleQuality, MEANING_FIRST_REVIEW_RUBRIC,
    MULTISCALE_EVIDENCE, MULTISCALE_PHRASE, MULTISCALE_SOURCE_TERM,
    adjudicate_scientific_spelling, scientific_spelling_review)
from max_postiz_agent.config import AgentError


TITLE = "Sea Surface Temperature Analysis"
ORIGINAL = ("The " + MULTISCALE_SOURCE_TERM + " uses satellite observations to study the ocean and show "
            "how surface temperatures change around the world.")
RU_TITLE = "Температура поверхности моря"
BODY = ("Использованы данные многомасштабного анализа температуры поверхности моря сверхвысокого "
        "разрешения (Multi-scale Ultra-high Resolution, MUR), которые показывают изменения "
        "температуры океана по наблюдениям спутников.")
URL = "https://svs.gsfc.nasa.gov/5600/"
REVIEW = {"approved": True, "topic": "science", "issues": [], "warnings": ["Совет редактору.", "Замечание о стиле."]}


def match_for(text):
    start = text.index(MULTISCALE_PHRASE)
    return {"offset": len(text[:start].encode("utf-16-le")) // 2, "length": 16,
            "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling"},
            "type": {"typeName": "UnknownWord"}}


class NoModel:
    def __init__(self, model):
        self.model, self.calls = model, 0

    def generate_text(self, _):
        self.calls += 1
        pytest.fail("Saved positive recovery must never call a model")


def quality(matches="term", status=200, payload=None, rubric=MEANING_FIRST_REVIEW_RUBRIC):
    models, events, texts = [NoModel("translator"), NoModel("reviewer")], [], []

    def spelling(request):
        text = parse_qs(request.content.decode())["text"][0]
        texts.append(text)
        found = [match_for(text)] if matches == "term" else matches
        return httpx.Response(status, json={"matches": found} if payload is None else payload)

    check = ArticleQuality(*models, review_rubric=rubric, trace=events.append,
                           transport=httpx.MockTransport(spelling))
    return check, models, events, texts


def recover(check, **changes):
    return check.recover_scientific_spelling(**{"title": RU_TITLE, "body": BODY,
        "original_title": TITLE, "original": ORIGINAL, "url": URL,
        "raw_review": json.dumps(REVIEW, ensure_ascii=False),
        "expected_review_prompt_hash": check.review_prompt_hash, **changes})


def test_exact_term_uses_fresh_lt_and_preserves_saved_raw_without_inference():
    check, models, events, texts = quality()
    raw = "```json\n" + json.dumps(REVIEW, ensure_ascii=False, indent=2) + "\n```"
    try:
        result = recover(check, raw_review=raw)
    finally:
        check.close()
    assert texts == [RU_TITLE + "\n\n" + BODY]
    assert all(model.calls == 0 for model in models)
    evidence = result["machine_review_evidence"]
    assert evidence["review_raw"] == raw and evidence["review"] == REVIEW
    assert result["warnings"] == REVIEW["warnings"] and result["machine_review_status"] == "passed"
    assert evidence["spelling_unresolved"] == [] and evidence["hard_gates_passed"] is True
    assert evidence["spelling_matches"] == [match_for(texts[0])]
    resolved = evidence["spelling_adjudicated"][0]
    assert resolved["word"] == "многомасштабного" and resolved["phrase"] == MULTISCALE_PHRASE
    assert resolved["source_term"] == MULTISCALE_SOURCE_TERM and resolved["evidence_url"] == MULTISCALE_EVIDENCE
    phases = [event["phase"] for event in events]
    assert phases[:2] == ["saved_review_recovery_started", "saved_review_recovery_validated"]
    assert events[0]["raw"] == raw and events[-1]["mode"] == "scientific_spelling_recovery"
    assert not set(phases) & {"translation_started", "review_started", "review_returned"}
    # The ordinary automatic path must validate this same evidence unchanged.
    from max_postiz_agent.automatic_policy import _validate
    assert _validate({**evidence, "trace_hash": "0" * 64}, result["title"], result["body"],
                     {"title": TITLE, "body": ORIGINAL, "url": URL}) == REVIEW


def test_utf16_offset_is_exact_and_cannot_resolve_another_location():
    text = "🌊 " + BODY
    finding = match_for(text)
    original = copy.deepcopy(finding)
    yes, no = adjudicate_scientific_spelling(ORIGINAL, text, [finding])
    assert len(yes) == 1 and no == [] and finding == original
    finding["offset"] = text.index(MULTISCALE_PHRASE)  # Python index differs from Java UTF-16.
    assert adjudicate_scientific_spelling(ORIGINAL, text, [finding]) == ([], [finding])


@pytest.mark.parametrize("change", [
    {"offset": 0}, {"offset": True}, {"length": 15}, {"length": "16"},
    {"rule": {"id": "OTHER_RULE", "issueType": "misspelling"}},
    {"rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "grammar"}},
    {"type": {"typeName": "Other"}},
])
def test_other_rules_types_lengths_or_offsets_remain_blocking(change):
    finding = {**match_for(BODY), **change}
    assert adjudicate_scientific_spelling(ORIGINAL, BODY, [finding]) == ([], [finding])


@pytest.mark.parametrize("source,text", [
    (ORIGINAL.replace("Multi-scale", "Multiscale"), BODY),
    (ORIGINAL.replace("(MUR)", "(OTHER)"), BODY),
    (ORIGINAL.replace("Sea Surface Temperature Analysis", "Ocean Analysis"), BODY),
    (ORIGINAL, BODY.replace("многомасштабного анализа", "многомасштабного метода")),
    (ORIGINAL, BODY.replace("многомасштабного", "сверхмногомасштабного")),
])
def test_not_a_general_multiscale_or_prefix_allowlist(source, text):
    finding = match_for(BODY)
    assert adjudicate_scientific_spelling(source, text, [finding]) == ([], [finding])


@pytest.mark.parametrize("changes,error", [
    ({"body": BODY + " В 2049 году."}, "TRANSLATION_NUMBER_CHANGED"),
    ({"original": ORIGINAL + " See https://example.org/source/"}, "TRANSLATION_REFERENCE_CHANGED"),
    ({"body": BODY * 40}, "ARTICLE_POST_TOO_LONG"),
    ({"expected_review_prompt_hash": "0" * 64}, "ARTICLE_SCIENTIFIC_SPELLING_NOT_RECOVERABLE"),
    ({"body": " " + BODY}, "ARTICLE_SCIENTIFIC_SPELLING_NOT_RECOVERABLE"),
    ({"original": " " + ORIGINAL}, "ARTICLE_SCIENTIFIC_SPELLING_NOT_RECOVERABLE"),
    ({"url": URL + "#fragment"}, "ARTICLE_SCIENTIFIC_SPELLING_NOT_RECOVERABLE"),
])
def test_saved_review_does_not_override_current_exact_input_or_hard_gates(changes, error):
    check, models, events, texts = quality()
    try:
        with pytest.raises(AgentError, match=error):
            recover(check, **changes)
    finally:
        check.close()
    assert not texts and all(model.calls == 0 for model in models)
    assert not any(event["phase"] == "accepted" for event in events)


@pytest.mark.parametrize("review", [
    {**REVIEW, "approved": False}, {**REVIEW, "approved": "true"}, {**REVIEW, "approved": 1},
    {**REVIEW, "issues": ["A real semantic error."]}, {**REVIEW, "topic": "other"},
    {**REVIEW, "extra": "unsupported"}, {**REVIEW, "warnings": ["x"] * 9},
])
def test_negative_malformed_or_substantive_saved_reviews_cannot_recover(review):
    check, models, events, texts = quality()
    try:
        with pytest.raises(AgentError, match="ARTICLE_SCIENTIFIC_SPELLING_NOT_RECOVERABLE"):
            recover(check, raw_review=json.dumps(review))
    finally:
        check.close()
    assert not texts and not events and all(model.calls == 0 for model in models)


@pytest.mark.parametrize("raw", [
    '{"approved":false,"approved":true,"topic":"science","issues":[],"warnings":[]}',
    "not JSON", "[]", " " * 65536 + json.dumps(REVIEW),
], ids=["duplicate-keys", "not-json", "array", "oversized"])
def test_ambiguous_or_oversized_raw_cannot_be_reused(raw):
    with pytest.raises(AgentError, match="ARTICLE_SCIENTIFIC_SPELLING_NOT_RECOVERABLE"):
        scientific_spelling_review(raw)


@pytest.mark.parametrize("matches,status,payload,error", [
    ([{"rule": {"id": "REAL_GRAMMAR", "issueType": "grammar"}}], 200, None, "ARTICLE_QUALITY_REJECTED"),
    ([{}], 200, None, "ARTICLE_QUALITY_REJECTED"),
    ([], 503, None, "ARTICLE_SPELLING_UNAVAILABLE"),
    ([], 200, {"matches": "invalid"}, "ARTICLE_SPELLING_UNAVAILABLE"),
])
def test_other_fresh_lt_or_infrastructure_errors_still_block(matches, status, payload, error):
    check, models, events, texts = quality(matches=matches, status=status, payload=payload)
    try:
        with pytest.raises(AgentError, match=error):
            recover(check)
    finally:
        check.close()
    assert len(texts) == 1 and all(model.calls == 0 for model in models)
    assert not any(event["phase"] == "accepted" for event in events)


def test_additional_unresolved_match_is_not_hidden_by_term_adjudication():
    matches = [match_for(RU_TITLE + "\n\n" + BODY), {"rule": {"id": "REAL_GRAMMAR", "issueType": "grammar"}}]
    check, _, _, _ = quality(matches=matches)
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            recover(check)
    finally:
        check.close()
