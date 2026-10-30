"""Plain MAX links are fixed before their exact text is checked or staged."""

import copy
from urllib.parse import parse_qs

import httpx
import pytest

from max_postiz_agent.article_quality import (
    MEANING_FIRST_REVIEW_RUBRIC,
    ArticleQuality,
    article_reference_urls,
    normalize_article_links,
)
from max_postiz_agent.config import AgentError, fingerprint
from test_article_quality import ORIGINAL, TITLE, TRANSLATION, URL, Model

STUDY = "https://www.science.org/doi/10.1126/sciadv.aed8089"
FEATURE = "https://science.nasa.gov/earth/ocean-nutrient-stress/"
SOURCE = ORIGINAL + f" See [the study]({STUDY}), then [this NASA feature]({FEATURE})."
BODY = TRANSLATION["body"] + f" См. [исследование]({STUDY}), затем [статью NASA]({FEATURE})."
PLAIN_BODY = (TRANSLATION["body"] + " См. исследование, затем статью NASA.\n\n"
              + STUDY + "\n" + FEATURE)
PASS = {"approved": True, "topic": "science", "issues": [], "warnings": []}


@pytest.mark.parametrize(("text", "expected"), [
    (f"[Исследование]({STUDY}), результаты.", f"Исследование, результаты.\n\n{STUDY}"),
    (f"Подробнее — в [статье NASA]({FEATURE}).", f"Подробнее — в статье NASA.\n\n{FEATURE}"),
    (f"[А]({FEATURE}) и [Б]({STUDY}); [В]({FEATURE})!",
     f"А и Б; В!\n\n{FEATURE}\n{STUDY}\n{FEATURE}"),
    ("[Термин](https://example.org/a_(b)).", "Термин.\n\nhttps://example.org/a_(b)"),
    ("[Термин](https://example.org/a_(b_(c))?key=value#section).",
     "Термин.\n\nhttps://example.org/a_(b_(c))?key=value#section"),
    ("[Ссылка](http://example.org/path?q=3).", "Ссылка.\n\nhttp://example.org/path?q=3"),
])
def test_only_markdown_delimiters_move_and_urls_are_exact_isolated_lines(text, expected):
    assert normalize_article_links(text) == expected
    assert normalize_article_links(expected) == expected


@pytest.mark.parametrize("text", [
    "Ссылка (https://example.org/path), продолжение.",
    "Ссылка https://example.org/a_(b). Обычные (пояснения) сохранены.",
    "(Текст без ссылок.) [Пояснение без назначения].",
    "![Изображение](https://example.org/image.png)",
    r"\[Экранированный текст](https://example.org/path)",
    '[Ссылка](https://example.org/path "Заголовок")',
    "[Незакрытая ссылка](https://example.org/a_(b)",
    "[Не HTTP](mailto:example@example.org)",
    "  Пробелы без Markdown не переписываются.\n",
])
def test_unrelated_parentheses_plain_urls_and_unsupported_syntax_are_unchanged(text):
    assert normalize_article_links(text) == text


def checker(candidate):
    translator, reviewer, events, spelling_texts = Model(candidate), Model(PASS), [], []

    def spelling(request):
        spelling_texts.append(parse_qs(request.content.decode())["text"][0])
        return httpx.Response(200, json={"matches": []})

    check = ArticleQuality(translator, reviewer, review_rubric=MEANING_FIRST_REVIEW_RUBRIC,
                           trace=events.append, transport=httpx.MockTransport(spelling))
    return check, translator, reviewer, events, spelling_texts


@pytest.mark.parametrize("method", ["prepare", "candidate"])
def test_final_plain_text_is_reviewed_spelled_returned_and_hashed_without_changing_source(method):
    candidate = {**TRANSLATION, "body": BODY}
    before = copy.deepcopy(candidate)
    check, translator, reviewer, events, spelling_texts = checker(candidate)
    try:
        result = (check.prepare(TITLE, SOURCE, URL) if method == "prepare" else
                  check.check_candidate(candidate["title"], candidate["body"], TITLE, SOURCE, URL))
    finally:
        check.close()
    text = candidate["title"] + "\n\n" + PLAIN_BODY
    full = text + "\n\nИсточник: " + URL + "\nПеревод и редактура с помощью нейросетей."
    assert result["translation"] == text and result["body"] == full
    assert result["original"] == TITLE + "\n\n" + SOURCE
    assert candidate == before
    assert len(reviewer.prompts) == 1 and spelling_texts == [text]
    assert reviewer.prompts[0]["translation_untrusted"] == text
    assert reviewer.prompts[0]["source_untrusted"] == result["original"]
    evidence = result["machine_review_evidence"]
    assert evidence["text_hash"] == fingerprint({"title": candidate["title"], "body": full})
    assert evidence["source_hash"] == fingerprint({"title": TITLE, "body": SOURCE, "url": URL})
    assert article_reference_urls(text) == article_reference_urls(result["original"])
    assert STUDY in full.splitlines() and FEATURE in full.splitlines()
    if method == "prepare":
        assert len(translator.prompts) == 1
        assert translator.prompts[0]["source_untrusted"]["body"] == SOURCE
        assert next(event["translation"] for event in events
                    if event["phase"] == "translation_parsed") == {**candidate, "body": PLAIN_BODY}
    else:
        assert translator.prompts == []
        started = next(event for event in events if event["phase"] == "candidate_started")
        assert started["candidate"] == {**candidate, "body": PLAIN_BODY}
        assert started["candidate_raw"] == candidate


@pytest.mark.parametrize("method", ["prepare", "candidate"])
def test_title_links_also_are_canonical_before_checks(method):
    candidate = {"title": TRANSLATION["title"] + f" [NASA]({FEATURE})", "body": TRANSLATION["body"]}
    source_title = TITLE + f" [NASA]({FEATURE})"
    check, _, reviewer, _, spelling_texts = checker(candidate)
    try:
        result = (check.prepare(source_title, ORIGINAL, URL) if method == "prepare" else
                  check.check_candidate(candidate["title"], candidate["body"], source_title, ORIGINAL, URL))
    finally:
        check.close()
    assert result["title"] == TRANSLATION["title"] + " NASA\n\n" + FEATURE
    assert reviewer.prompts[0]["translation_untrusted"] == result["translation"] == spelling_texts[0]


@pytest.mark.parametrize(("changed", "code"), [
    (BODY.replace(FEATURE, "https://example.org/replaced/"), "TRANSLATION_REFERENCE_CHANGED"),
    (BODY.replace("aed8089", "aed8090"), "TRANSLATION_NUMBER_CHANGED"),
    (BODY.replace("3 часа", "4 часа"), "TRANSLATION_NUMBER_CHANGED"),
])
def test_plain_link_conversion_never_repairs_or_weakens_reference_or_number_gates(changed, code):
    check, translator, reviewer, events, spelling_texts = checker({**TRANSLATION, "body": changed})
    try:
        with pytest.raises(AgentError, match=code):
            check.check_candidate(TRANSLATION["title"], changed, TITLE, SOURCE, URL)
    finally:
        check.close()
    assert translator.prompts == reviewer.prompts == spelling_texts == []
    assert any(event["phase"] == "gate_rejected" and event["error"] == code for event in events)


def test_candidate_trace_retains_raw_whitespace_provenance_and_noop_trace_stays_legacy():
    check, _, _, events, _ = checker(TRANSLATION)
    try:
        check.check_candidate(" " + TRANSLATION["title"], TRANSLATION["body"] + "\n",
                              TITLE, ORIGINAL, URL)
        started = next(event for event in events if event["phase"] == "candidate_started")
        assert started["candidate"] == TRANSLATION
        assert started["candidate_raw"] == {"title": " " + TRANSLATION["title"],
                                            "body": TRANSLATION["body"] + "\n"}
        events.clear()
        check.check_candidate(TRANSLATION["title"], TRANSLATION["body"], TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert "candidate_raw" not in events[0]


def test_equivalent_markdown_and_plain_rejection_are_not_reviewed_again_in_translation_loop():
    candidate = {**TRANSLATION, "body": BODY}
    check, translator, reviewer, events, spelling_texts = checker(candidate)
    translator.responses.extend([{**candidate, "body": PLAIN_BODY}, candidate])
    reviewer.responses = [{**PASS, "approved": False, "issues": ["Изменено существенное утверждение."]}, PASS]
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            check.prepare(TITLE, SOURCE, URL)
    finally:
        check.close()
    assert len(translator.prompts) == 3 and len(reviewer.prompts) == len(spelling_texts) == 1
    assert sum(event["phase"] == "candidate_repeated" for event in events) == 2
    assert not any(event["phase"] == "accepted" for event in events)
