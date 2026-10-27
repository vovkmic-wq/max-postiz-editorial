"""Три точные лексические области; никакого semantic pass и сетевых запросов."""
import copy
import re

import pytest

from max_postiz_agent.scientific_terms import (
    GODDARD_EVIDENCE_URL, GODDARD_REFERENCE_SHA256,
    HUBBLE_EVIDENCE_URL, HUBBLE_FORMS, HUBBLE_REFERENCE_SHA256,
    SCIENTIFIC_SPELLING_CONTRACT, STAR_FORMING_EVIDENCE_URL,
    STAR_FORMING_FORMS, STAR_FORMING_REFERENCE_SHA256, scoped_spelling_evidence,
)


def finding(text, word):
    start = text.index(word)
    return {"offset": len(text[:start].encode("utf-16-le")) // 2,
        "length": len(word.encode("utf-16-le")) // 2, "message": "Unknown spelling",
        "replacements": [{"value": "неверная подсказка"}],
        "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling", "category": {"id": "TYPOS"}},
        "type": {"typeName": "UnknownWord"}}


@pytest.mark.parametrize("word", sorted(HUBBLE_FORMS))
def test_hubble_finite_forms_require_named_telescope_context(word):
    text = "Наблюдения космического телескопа «" + word + "» сохранены."
    match = finding(text, word)
    before = copy.deepcopy(match)
    evidence = scoped_spelling_evidence("The Hubble Space Telescope made observations.", text, match)
    assert evidence["word"] == word and evidence["source_term"] == "Hubble Space Telescope"
    assert evidence["evidence_url"] == HUBBLE_EVIDENCE_URL
    assert evidence["reference_sha256"] == HUBBLE_REFERENCE_SHA256
    assert evidence["contract"] == SCIENTIFIC_SPELLING_CONTRACT == "source-bound-terms-v1"
    assert evidence["reason"] == "SOURCE_BOUND_VERIFIED_SCIENTIFIC_TERM"
    assert evidence["match"] == match == before
    evidence["match"]["replacements"].clear()
    assert match == before
    assert not {"approved", "passed", "semantic_pass"} & set(evidence)


@pytest.mark.parametrize("text", ["телескоп Хаббл", "телескопа им. Хаббла", "телескопом имени Хаббла",
    "телескоп NASA/ESA «Хаббл»", "Хаббл — космический телескоп", "«Хаббл» (космический телескоп)"])
def test_hubble_context_is_a_direct_telescope_phrase(text):
    word = "Хаббла" if "Хаббла" in text else "Хаббл"
    assert scoped_spelling_evidence("Hubble Space Telescope", text, finding(text, word)) is not None


@pytest.mark.parametrize("text", ["Центр космических полетов им. Годдарда НАСА",
    "в центре космических полётов имени Годдарда", "Центра космических полетов Годдарда"])
def test_only_goddard_genitive_in_the_full_named_center_context(text):
    evidence = scoped_spelling_evidence("NASA's Goddard Space Flight Center (GSFC).", text, finding(text, "Годдарда"))
    assert evidence["word"] == "Годдарда" and evidence["source_term"] == "Goddard Space Flight Center"
    assert evidence["reference_sha256"] == GODDARD_REFERENCE_SHA256
    assert evidence["evidence_url"] == GODDARD_EVIDENCE_URL


@pytest.mark.parametrize("word", sorted(STAR_FORMING_FORMS))
def test_only_explicit_normative_star_forming_inflections(word):
    text = "Описание: " + word + " объекты и области."
    evidence = scoped_spelling_evidence("A star-forming region is shown.", text, finding(text, word))
    assert evidence["word"] == word and evidence["source_term"] == "star-forming"
    assert evidence["evidence_url"] == STAR_FORMING_EVIDENCE_URL
    assert evidence["reference_sha256"] == STAR_FORMING_REFERENCE_SHA256


@pytest.mark.parametrize("original,text,word", [
    ("Hubble surveyed the field.", "телескоп Хаббл", "Хаббл"),
    ("A space telescope.", "телескоп Хаббла", "Хаббла"),
    ("Hubble Space Telescope", "Эдвин Хаббл рассказал о телескопе.", "Хаббл"),
    ("Hubble Space Telescope", "Хаббла упомянули в статье. Телескоп наблюдал позже.", "Хаббла"),
    ("Goddard launched a rocket.", "Центр космических полетов Годдарда", "Годдарда"),
    ("Goddard Space Flight Center", "сведения Годдарда о космическом центре", "Годдарда"),
    ("Goddard Space Flight Center", "Центр космических полетов Годдард", "Годдард"),
    ("Goddard Space Flight Center", "Центр космических полетов Годдарду", "Годдарду"),
    ("A starry region.", "звездообразующая область", "звездообразующая"),
    ("non-star-forming galaxy", "звездообразующая область", "звездообразующая"),
    ("non‑star-forming galaxy", "звездообразующая область", "звездообразующая"),
    ("A prestar-forming region.", "звездообразующая область", "звездообразующая"),
    ("A star-formings region.", "звездообразующая область", "звездообразующая"),
])
def test_wrong_source_or_missing_proper_noun_context_stays_unresolved(original, text, word):
    assert scoped_spelling_evidence(original, text, finding(text, word)) is None


@pytest.mark.parametrize("original", ["https://example.org/star-forming", "www.example.org/star-forming",
    "See [source](https://example.org/star-forming?q=star-forming#star-forming)", "ftp://example.org/star-forming"])
def test_url_only_english_term_is_not_narrative_evidence(original):
    text = "звездообразующие области"
    assert scoped_spelling_evidence(original, text, finding(text, "звездообразующие")) is None


def test_visible_link_caption_is_narrative_but_translated_url_token_is_not():
    text = "звездообразующие области"
    assert scoped_spelling_evidence("See [star-forming](https://example.org/photo).", text,
        finding(text, "звездообразующие")) is not None
    url_text = "Ссылка https://example.org/звездообразующие"
    assert scoped_spelling_evidence("A star-forming region.", url_text,
        finding(url_text, "звездообразующие")) is None


@pytest.mark.parametrize("word", ["Хабблааа", "Хабл", "Хабблла", "хаббла", "ХАББЛА", "Хaббла",
    "Годдардаа", "Годарда", "звездообразующ", "звездообразуюшая", "звездообразующимися",
    "звёздообразующая", "флоккулентом", "вихрящими", "вихряются", "Dove", "Голубь"])
def test_typos_homoglyphs_stems_and_other_scopes_are_never_resolved(word):
    source = "Hubble Space Telescope and Goddard Space Flight Center study a star-forming region."
    text = "Центр космических полетов " + word + "; телескоп " + word
    assert scoped_spelling_evidence(source, text, finding(text, word)) is None


@pytest.mark.parametrize("source", ["Hubble ſpace Telescope", "Hubble Spаce Telescope",
    "Hubble Space Telesсope", "xHubble Space Telescope", "Hubble Space Telescopes"])
def test_english_homoglyphs_and_joined_named_terms_are_not_canonical_source(source):
    text = "телескоп Хаббла"
    assert scoped_spelling_evidence(source, text, finding(text, "Хаббла")) is None


@pytest.mark.parametrize("prefix,suffix", [("сверх", ""), ("", "123"), ("не-", ""), ("не‑", ""),
    ("", "\u200d"), ("", "\u0301")])
def test_match_must_cover_a_whole_standalone_word(prefix, suffix):
    text = "телескоп " + prefix + "Хаббла" + suffix
    assert scoped_spelling_evidence("Hubble Space Telescope", text, finding(text, "Хаббла")) is None


def test_java_utf16_offsets_work_after_emoji_and_python_offsets_do_not():
    text = "😀 телескоп Хаббла"
    match = finding(text, "Хаббла")
    assert scoped_spelling_evidence("Hubble Space Telescope", text, match) is not None
    assert scoped_spelling_evidence("Hubble Space Telescope", text, {**match, "offset": text.index("Хаббла")}) is None
    assert scoped_spelling_evidence("Hubble Space Telescope", text, {**match, "offset": 1}) is None


@pytest.mark.parametrize("change", [{"offset": True}, {"offset": 0.0}, {"offset": -1}, {"offset": 9999},
    {"length": True}, {"length": 0}, {"length": 7}, {"length": 5}, {"length": "6"}, {"rule": None},
    {"rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling"}},
    {"rule": {"id": "OTHER", "issueType": "misspelling", "category": {"id": "TYPOS"}}},
    {"rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "grammar", "category": {"id": "TYPOS"}}},
    {"rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling", "category": {"id": "GRAMMAR"}}},
    {"type": None}, {"type": {"typeName": "Other"}}])
def test_malformed_or_wrong_lt_rule_and_exact_span_stay_unresolved(change):
    text = "телескоп Хаббла"
    assert scoped_spelling_evidence("Hubble Space Telescope", text, {**finding(text, "Хаббла"), **change}) is None


def test_spelling_term_does_not_clear_a_separate_grammar_finding_or_rewrite_raw():
    text = "телескоп Хаббла"
    spelling = finding(text, "Хаббла")
    grammar = {**copy.deepcopy(spelling), "rule": {"id": "RU_AGREEMENT", "issueType": "grammar", "category": {"id": "GRAMMAR"}},
        "type": {"typeName": "Other"}}
    matches = [spelling, grammar]
    before = copy.deepcopy(matches)
    adjudicated, unresolved = [], []
    for match in matches:
        evidence = scoped_spelling_evidence("Hubble Space Telescope", text, match)
        (adjudicated if evidence else unresolved).append(evidence or match)
    assert len(adjudicated) == 1 and unresolved == [grammar] and matches == before


@pytest.mark.parametrize("original,text,match", [(None, "word", {}), ("source", None, {}), ("source", "word", None),
    ("\ud800 Hubble Space Telescope", "телескоп Хаббла", {}),
    ("Hubble Space Telescope", "телескоп Хаббла\x00", {})])
def test_malformed_inputs_do_not_raise_or_resolve(original, text, match):
    assert scoped_spelling_evidence(original, text, match) is None


def test_reference_hashes_are_pinned_complete_sha256_without_runtime_fetching():
    assert all(re.fullmatch(r"[0-9a-f]{64}", value) for value in
        (HUBBLE_REFERENCE_SHA256, GODDARD_REFERENCE_SHA256, STAR_FORMING_REFERENCE_SHA256))


def test_second_hubble_mention_uses_an_exact_named_telescope_anchor_in_same_document():
    text = "😀 Снимок космического телескопа Хаббла сохранён. Затем Хаббл наблюдал другую область."
    match = finding(text, "Хаббл наблюдал")
    match["length"] = len("Хаббл")
    evidence = scoped_spelling_evidence("The Hubble Space Telescope observed the region.", text, match)
    assert evidence["word"] == "Хаббл" and evidence["context_scope"] == "document_telescope"
    assert evidence["anchor_phrase"] == "телескопа Хаббла"
    span = evidence["anchor_span"]
    assert span["offset_unit"] == "unicode_codepoint"
    assert text[span["start"]:span["end"]] == evidence["anchor_phrase"]
    assert evidence["match"] == match


def test_local_telescope_evidence_retains_its_original_shape_without_document_fields():
    text = "Снимок телескопа Хаббла. Хаббл наблюдал небо."
    evidence = scoped_spelling_evidence("Hubble Space Telescope", text, finding(text, "Хаббла"))
    assert evidence["phrase"] == "телескопа Хаббла"
    assert not {"context_scope", "anchor_phrase", "anchor_span"} & set(evidence)


@pytest.mark.parametrize("prefix", ["Космический телескоп наблюдал небо. ",
    "Эдвин Хаббл изучал небо. Космический телескоп наблюдал позже. ",
    "Снимок телескопа Хабл сохранён. ", "Снимок телескопа Хaббла сохранён. ",
    "Снимок телескопа Хабблааа сохранён. ", "Снимок телескопа Хаббла\u200d сохранён. ",
    "Указан https://example.org/телескопа/Хаббла. "])
def test_document_scope_requires_a_valid_named_russian_anchor_not_typos_or_unnamed_telescope(prefix):
    text = prefix + "Хаббл наблюдал область."
    match = finding(text, "Хаббл наблюдал")
    match["length"] = len("Хаббл")
    assert scoped_spelling_evidence("Hubble Space Telescope", text, match) is None


def test_document_anchor_does_not_replace_the_full_english_narrative_requirement():
    text = "Снимок телескопа Хаббла сохранён. Хаббл наблюдал область."
    match = finding(text, "Хаббл наблюдал")
    match["length"] = len("Хаббл")
    original = "See https://example.org/Hubble%20Space%20Telescope only."
    assert scoped_spelling_evidence(original, text, match) is None


def test_url_masking_cannot_manufacture_a_named_telescope_anchor():
    text = "телескоп https://example.org/mention Хаббла. Затем Хаббл наблюдал небо."
    match = finding(text, "Хаббл наблюдал")
    match["length"] = len("Хаббл")
    assert scoped_spelling_evidence("Hubble Space Telescope", text, match) is None


@pytest.mark.parametrize("agency", ["NASA", "НАСА"])
def test_goddard_center_allows_only_the_explicit_nasa_agency_between_center_and_name(agency):
    text = "в Центре космических полетов " + agency + " имени Годдарда"
    assert scoped_spelling_evidence("Goddard Space Flight Center", text, finding(text, "Годдарда")) is not None
