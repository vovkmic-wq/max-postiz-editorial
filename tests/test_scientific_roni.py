"""Only the source-bound RONI proper name can resolve this UnknownWord."""
import copy
import json

import httpx
import pytest

from max_postiz_agent.article_quality import (
    ArticleQuality,
    RONI_EVIDENCE,
    RONI_SOURCE_TERM,
    multiscale_spelling_evidence,
    roni_spelling_evidence,
    rubric_spelling,
)
from max_postiz_agent.config import AgentError

SOURCE_TITLE = RONI_SOURCE_TERM
SOURCE = ("Researchers use the Relative Oceanic Niño Index (RONI) to study changes in ocean temperatures "
          "and compare climate observations across the tropical Pacific Ocean.")
TITLE = "Измерения относительного океанического индекса Ниньо (RONI)"
BODY = ("Относительный океанический индекс Ниньо (RONI) помогает учёным изучать изменения температуры океана "
        "и сравнивать климатические наблюдения в тропической части Тихого океана.")
URL = "https://svs.gsfc.nasa.gov/5685/"
POSITIVE = {"approved": True, "topic": "science", "issues": [], "warnings": ["Допустим другой стиль."]}


def finding(text, start=None):
    start = text.index("Ниньо") if start is None else start
    return {"offset": len(text[:start].encode("utf-16-le")) // 2, "length": len("Ниньо"),
            "message": "Unknown scientific proper name.", "type": {"typeName": "UnknownWord"},
            "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling"}}


def both_findings(text):
    return [finding(text, index) for index in range(len(text)) if text.startswith("Ниньо", index)]


@pytest.mark.parametrize("phrase", ["относительного океанического индекса Ниньо (RONI)",
    "Относительного океанического индекса Ниньо (RONI)", "относительный океанический индекс Ниньо (RONI)",
    "Относительный океанический индекс Ниньо (RONI)"])
def test_exact_roni_name_preserves_full_source_and_raw_finding(phrase):
    match = finding(phrase)
    before = copy.deepcopy(match)
    evidence = roni_spelling_evidence("We use " + RONI_SOURCE_TERM + ".", phrase, match)
    assert evidence == {"match": before, "word": "Ниньо", "phrase": phrase, "source_term": RONI_SOURCE_TERM,
                        "evidence_url": RONI_EVIDENCE, "reason": "SOURCE_BOUND_VERIFIED_SCIENTIFIC_TERM"}
    assert match == before
    evidence["match"]["rule"]["id"] = "changed-copy"
    assert match == before


def test_utf16_offsets_support_emoji_and_two_exact_occurrences_without_python_index_fallback():
    text = "🌊 " + TITLE + "\n\n" + BODY
    matches = both_findings(text)
    adjudicated, unresolved = rubric_spelling(SOURCE_TITLE + "\n\n" + SOURCE, text, matches, "meaning-first-v1")
    assert len(adjudicated) == 2 and unresolved == []
    assert roni_spelling_evidence(SOURCE, text, {**matches[0], "offset": text.index("Ниньо")}) is None


@pytest.mark.parametrize("original", ["Oceanic Niño Index (ONI)", "Relative Oceanic Nino Index (RONI)",
    "Relative Oceanic Niño Index", "relative Oceanic Niño Index (RONI)",
    "FakeRelative Oceanic Niño Index (RONI)", "Relative Oceanic Niño Index (RONI)Suffix",
    "Relative Oceanic Niño Index (RONI)-suffix", "предRelative Oceanic Niño Index (RONI)",
    "Relative Oceanic Niño Index (RONI)другой", "An unrelated scientific source."])
def test_wrong_or_embedded_source_term_never_exempts(original):
    assert roni_spelling_evidence(original, TITLE, finding(TITLE)) is None


@pytest.mark.parametrize("translated", ["Ниньо", "Океанический индекс Ниньо (ONI)",
    "относительный океанический индекс Эль-Ниньо (RONI)",
    "относительный океанический индекс Нинья (RONI)",
    "относительный океанический индекс Ниньо (OTHER)",
    "относительный океанический индекс Ниньо", "относительного океанического индексов Ниньо (RONI)",
    "неотносительный океанический индекс Ниньо (RONI)",
    "относительный океанический индекс Ниньо (RONI)другой",
    "ОТНОСИТЕЛЬНЫЙ океанический индекс Ниньо (RONI)"])
def test_wrong_word_context_inflection_or_boundary_never_exempts(translated):
    match = finding(translated) if "Ниньо" in translated else {**finding(TITLE), "offset": 0}
    assert roni_spelling_evidence(SOURCE, translated, match) is None


@pytest.mark.parametrize("change", [{"offset": 0}, {"offset": -1}, {"offset": True}, {"offset": 44.0},
    {"offset": "44"}, {"length": 4}, {"length": 6}, {"length": True}, {"length": 5.0}, {"length": "5"},
    {"type": {"typeName": "Other"}}, {"type": None},
    {"rule": {"id": "OTHER", "issueType": "misspelling"}},
    {"rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "grammar"}}, {"rule": None}])
def test_wrong_positions_types_and_grammar_rules_are_never_exempted(change):
    assert roni_spelling_evidence(SOURCE, TITLE, {**finding(TITLE), **change}) is None


@pytest.mark.parametrize("rubric", ["legacy", "meaning-first-v1"])
def test_unrelated_findings_remain_blocking_and_mur_behavior_is_unchanged(rubric):
    match = finding(TITLE)
    other = {"offset": 0, "length": 3, "message": "A real grammatical error.",
             "type": {"typeName": "Other"}, "rule": {"id": "REAL_GRAMMAR", "issueType": "grammar"}}
    accepted, unresolved = rubric_spelling(SOURCE, TITLE, [match, other], rubric)
    assert len(accepted) == 1 and accepted[0]["match"] == match and unresolved == [other]
    mur = "многомасштабного анализа"
    mur_match = {**finding(TITLE), "offset": 0, "length": len("многомасштабного")}
    assert multiscale_spelling_evidence(
        "Multi-scale Ultra-high Resolution (MUR) Sea Surface Temperature Analysis", mur, mur_match)
    assert multiscale_spelling_evidence(SOURCE, mur, mur_match) is None
    assert roni_spelling_evidence(SOURCE, mur, mur_match) is None


class Model:
    def __init__(self, response):
        self.response, self.calls = response, []

    def generate_text(self, prompt):
        self.calls.append(prompt)
        return self.response if isinstance(self.response, str) else json.dumps(self.response), {}


def checker(review=None, matches=None, status=200):
    translator, reviewer = Model({}), Model(POSITIVE if review is None else review)
    events, calls = [], []

    def spelling(request):
        calls.append(request)
        return httpx.Response(status, json={"matches": both_findings(TITLE + "\n\n" + BODY) if matches is None else matches})

    quality = ArticleQuality(translator, reviewer, review_rubric="meaning-first-v1", trace=events.append,
                             transport=httpx.MockTransport(spelling))
    return quality, translator, reviewer, events, calls


def test_candidate_pass_records_raw_matches_and_wmo_evidence_after_independent_review():
    quality, translator, reviewer, events, calls = checker()
    try:
        prepared = quality.check_candidate(TITLE, BODY, SOURCE_TITLE, SOURCE, URL)
    finally:
        quality.close()
    assert prepared["machine_review_status"] == "passed"
    proof = prepared["machine_review_evidence"]
    assert len(proof["spelling_matches"]) == len(proof["spelling_adjudicated"]) == 2
    assert proof["spelling_unresolved"] == [] and proof["review"] == POSITIVE
    assert all(value["evidence_url"] == RONI_EVIDENCE for value in proof["spelling_adjudicated"])
    assert not translator.calls and len(reviewer.calls) == len(calls) == 1 and events[-1]["phase"] == "accepted"


@pytest.mark.parametrize("review", [{"approved": False, "topic": "science", "issues": ["Changed fact."], "warnings": []},
    {"approved": True, "topic": "science", "issues": ["Changed fact."], "warnings": []},
    "not JSON", {"approved": "true", "topic": "science", "issues": [], "warnings": []}])
def test_negative_or_invalid_ministral_verdict_still_blocks(review):
    quality, translator, reviewer, _, _ = checker(review=review)
    try:
        with pytest.raises(AgentError):
            quality.check_candidate(TITLE, BODY, SOURCE_TITLE, SOURCE, URL)
    finally:
        quality.close()
    assert not translator.calls and len(reviewer.calls) == 1


def test_saved_positive_roni_recovery_never_samples_either_model():
    quality, translator, reviewer, events, calls = checker()
    raw = json.dumps(POSITIVE)
    try:
        prepared = quality.recover_roni_spelling(TITLE, BODY, SOURCE_TITLE, SOURCE, URL,
            raw_review=raw, expected_review_prompt_hash=quality.review_prompt_hash)
    finally:
        quality.close()
    assert prepared["machine_review_status"] == "passed" and prepared["machine_review_evidence"]["review_raw"] == raw
    assert len(calls) == 1 and not translator.calls and not reviewer.calls
    assert events[0]["phase"] == "saved_review_recovery_started" and events[0]["kind"] == "roni_spelling"
    assert events[-1]["phase"] == "accepted" and events[-1]["mode"] == "roni_spelling_recovery"


@pytest.mark.parametrize("change", ["url", "source", "candidate", "prompt", "negative", "invalid", "numbers", "reference", "lt", "lt_unavailable"])
def test_roni_recovery_keeps_url_source_review_and_hard_gates(change):
    match = {"offset": 0, "length": 3, "message": "A real error.",
             "rule": {"id": "REAL_GRAMMAR", "issueType": "grammar"}}
    quality, translator, reviewer, _, _ = checker(matches=[match] if change == "lt" else None,
                                                 status=503 if change == "lt_unavailable" else 200)
    title, body, source, url, raw, prompt = TITLE, BODY, SOURCE, URL, json.dumps(POSITIVE), quality.review_prompt_hash
    if change == "url":
        url = "https://svs.gsfc.nasa.gov/5600/"
    elif change == "source":
        source = source.replace("Niño", "Nino")
        title_source = SOURCE_TITLE.replace("Niño", "Nino")
    elif change == "candidate":
        title, body = title.replace("Ниньо", "Нинья"), body.replace("Ниньо", "Нинья")
    elif change == "prompt":
        prompt = "0" * 64
    elif change == "negative":
        raw = json.dumps({**POSITIVE, "approved": False, "issues": ["Changed fact."]})
    elif change == "invalid":
        raw = "not JSON"
    elif change == "numbers":
        source += " Researchers compared 3 observations."
    elif change == "reference":
        source += " Data: https://data.nasa.gov/observations/"
    try:
        with pytest.raises(AgentError):
            quality.recover_roni_spelling(title, body, title_source if change == "source" else SOURCE_TITLE,
                source, url, raw_review=raw, expected_review_prompt_hash=prompt)
    finally:
        quality.close()
    assert not translator.calls and not reviewer.calls


@pytest.mark.parametrize("count", [0, 1])
def test_roni_recovery_accepts_zero_or_one_fresh_roni_finding_without_model(count):
    matches = both_findings(TITLE + "\n\n" + BODY)[:count]
    quality, translator, reviewer, _, _ = checker(matches=matches)
    try:
        result = quality.recover_roni_spelling(TITLE, BODY, SOURCE_TITLE, SOURCE, URL,
            raw_review=json.dumps(POSITIVE), expected_review_prompt_hash=quality.review_prompt_hash)
    finally:
        quality.close()
    assert result["machine_review_status"] == "passed" and not translator.calls and not reviewer.calls


def test_roni_recovery_rejects_duplicate_findings_even_when_word_is_correct():
    match = finding(TITLE + "\n\n" + BODY)
    quality, translator, reviewer, _, _ = checker(matches=[match, copy.deepcopy(match)])
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            quality.recover_roni_spelling(TITLE, BODY, SOURCE_TITLE, SOURCE, URL,
                raw_review=json.dumps(POSITIVE), expected_review_prompt_hash=quality.review_prompt_hash)
    finally:
        quality.close()
    assert not translator.calls and not reviewer.calls


def test_roni_recovery_does_not_borrow_other_scientific_allowances():
    source = SOURCE + " Researchers also observed the Large Magellanic Cloud."
    body = BODY + " Учёные также наблюдали Большое Магелланово облако."
    text = TITLE + "\n\n" + body
    start = text.index("Магелланово")
    match = {**finding(TITLE), "offset": len(text[:start].encode("utf-16-le")) // 2, "length": len("Магелланово")}
    assert rubric_spelling(SOURCE_TITLE + "\n\n" + source, text, [match], "meaning-first-v1")[1] == []
    quality, translator, reviewer, _, _ = checker(matches=[match])
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            quality.recover_roni_spelling(TITLE, body, SOURCE_TITLE, source, URL,
                raw_review=json.dumps(POSITIVE), expected_review_prompt_hash=quality.review_prompt_hash)
    finally:
        quality.close()
    assert not translator.calls and not reviewer.calls
