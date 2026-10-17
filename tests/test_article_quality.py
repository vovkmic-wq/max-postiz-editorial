import json

import httpx
import pytest

from max_postiz_agent.article_quality import (
    MAGELLANIC_EVIDENCE,
    ArticleQuality,
    adjudicate_scientific_spelling,
    canonical_article_url,
)
from max_postiz_agent.config import AgentError

TITLE = "A small telescope studies the night sky"
ORIGINAL = (
    "A small telescope observed the night sky for 3 hours. Researchers studied the "
    "light from a distant star and checked their observations before sharing the results."
)
TRANSLATION = {"title": "Небольшой телескоп изучает ночное небо",
               "body": "Небольшой телескоп наблюдал ночное небо 3 часа. Исследователи изучили "
                       "свет далёкой звезды и проверили наблюдения, прежде чем сообщить результаты."}
APPROVAL = {"approved": True, "topic": "science", "issues": []}
URL = "https://science.nasa.gov/example/"


class Model:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.prompts = []

    def generate_text(self, prompt):
        self.prompts.append(json.loads(prompt))
        response = self.responses[min(len(self.prompts) - 1, len(self.responses) - 1)]
        return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False), {}


def quality(translator=None, reviewer=None, matches=None, status=200, trace=None):
    return ArticleQuality(translator or Model(TRANSLATION), reviewer or Model(APPROVAL),
        trace=trace,
        transport=httpx.MockTransport(lambda _: httpx.Response(status,
            json={"matches": [] if matches is None else matches})))


def test_translation_review_spelling_and_real_source_attribution():
    translator, reviewer = Model(TRANSLATION), Model(APPROVAL)
    check = quality(translator, reviewer)
    try:
        result = check.prepare(TITLE, ORIGINAL, URL + "#section")
    finally:
        check.close()
    assert result["url"] == URL
    assert result["translation"] == TRANSLATION["title"] + "\n\n" + TRANSLATION["body"]
    assert result["original"] == TITLE + "\n\n" + ORIGINAL
    assert result["body"].endswith(URL + "\nПеревод и редактура с помощью нейросетей.")
    assert len(result["body"]) <= 3500
    assert result["topic"] == "science"
    assert len(translator.prompts) == len(reviewer.prompts) == 1
    assert translator.prompts[0]["source_untrusted"]["body"] == ORIGINAL
    assert reviewer.prompts[0]["source_untrusted"] == result["original"]


@pytest.mark.parametrize("url", ["http://nasa.gov/article", "https://127.0.0.1/article",
    "https://10.20.30.45/article", "https://[::1]/article", "https://localhost/article",
    "https://host.internal/article", "https://user:password@nasa.gov/article",
    "https://nasa.gov:444/article", "https://nasa.gov/a%0db", "https://nasa.gov\\evil/a",
    "https://127.1/article", "https://0177.0.0.1/article", "https://0x7f000001/article",
    "https://nasa.gov.evil.local/a", "https://nasa.gov/a\nb"])
def test_invalid_urls_never_reach_models(url):
    translator = Model(TRANSLATION)
    check = quality(translator)
    try:
        with pytest.raises(AgentError, match="ARTICLE_URL_INVALID"):
            check.prepare(TITLE, ORIGINAL, url)
    finally:
        check.close()
    assert not translator.prompts


def test_canonical_url_preserves_article_query_and_removes_only_fragment():
    assert canonical_article_url("https://Science.NASA.gov:443/a?q=star#part") == (
        "https://science.nasa.gov/a?q=star")


@pytest.mark.parametrize(("title", "original", "code"), [
    ("A", ORIGINAL, "ARTICLE_INPUT_SIZE_INVALID"),
    (TITLE, "A short note", "ARTICLE_INPUT_SIZE_INVALID"),
    (TITLE, "The science " * 900, "ARTICLE_INPUT_SIZE_INVALID"),
    ("Небольшой телескоп", "Русский текст о телескопе и звёздах. " * 8,
     "ARTICLE_ENGLISH_SOURCE_REQUIRED"),
    (TITLE, None, "ARTICLE_INPUT_INVALID"),
])
def test_invalid_input_never_reaches_models(title, original, code):
    translator = Model(TRANSLATION)
    check = quality(translator)
    try:
        with pytest.raises(AgentError, match=code):
            check.prepare(title, original, URL)
    finally:
        check.close()
    assert not translator.prompts


def test_number_change_repaired_without_reviewing_bad_translation():
    translator = Model({**TRANSLATION, "body": TRANSLATION["body"].replace("3", "4")}, TRANSLATION)
    reviewer = Model(APPROVAL)
    check = quality(translator, reviewer)
    try:
        assert check.prepare(TITLE, ORIGINAL, URL)["topic"] == "science"
    finally:
        check.close()
    assert len(translator.prompts) == 2 and len(reviewer.prompts) == 1
    feedback = translator.prompts[1]["repair_feedback"]
    assert feedback["code"] == "TRANSLATION_NUMBER_CHANGED"
    assert feedback["original_numbers"] == ["3"] and feedback["translated_numbers"] == ["4"]


def test_json_repairs_share_one_bounded_attempt_budget():
    translator = Model("not JSON", TRANSLATION)
    reviewer = Model({"approved": "true", "topic": "science", "issues": []}, APPROVAL)
    check = quality(translator, reviewer)
    try:
        assert check.prepare(TITLE, ORIGINAL, URL)["topic"] == "science"
    finally:
        check.close()
    assert len(translator.prompts) == 3 and len(reviewer.prompts) == 2


@pytest.mark.parametrize("approval", [
    {"approved": False, "topic": "science", "issues": ["Invented claim"]},
    {"approved": True, "topic": "other", "issues": []},
    {"approved": True, "topic": "politics", "issues": []},
    {"approved": True, "topic": "science", "issues": ["Changed negation"]},
])
def test_independent_rejection_cannot_be_ignored(approval):
    translator, reviewer = Model(TRANSLATION), Model(approval)
    check = quality(translator, reviewer)
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert len(translator.prompts) == len(reviewer.prompts) == 3


def test_spelling_issues_block_even_unanimous_models():
    translator = Model(TRANSLATION)
    check = quality(translator, matches=[{"message": "Ошибка", "offset": 5, "length": 4}])
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert len(translator.prompts) == 3
    assert translator.prompts[1]["repair_feedback"]["spelling_matches"]


def test_spelling_unavailable_fails_closed_without_generation_retry():
    translator = Model(TRANSLATION)
    check = quality(translator, status=503)
    try:
        with pytest.raises(AgentError, match="ARTICLE_SPELLING_UNAVAILABLE"):
            check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert len(translator.prompts) == 1


def test_no_truncation_or_approval_for_overlong_translation():
    translator, reviewer = Model({**TRANSLATION, "body": TRANSLATION["body"] * 30}), Model(APPROVAL)
    check = quality(translator, reviewer)
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert len(translator.prompts) == 3 and not reviewer.prompts


def test_independent_models_and_loopback_spelling_are_required():
    model = Model(TRANSLATION)
    with pytest.raises(AgentError, match="ARTICLE_INDEPENDENT_REVIEW_REQUIRED"):
        ArticleQuality(model, model)
    translator, reviewer = Model(TRANSLATION), Model(APPROVAL)
    translator.model = reviewer.model = "same-model"
    with pytest.raises(AgentError, match="ARTICLE_INDEPENDENT_REVIEW_REQUIRED"):
        ArticleQuality(translator, reviewer)
    with pytest.raises(AgentError, match="LOCAL_SPELLING_REQUIRED"):
        ArticleQuality(Model(TRANSLATION), Model(APPROVAL), spelling_url="https://example.com/check")


def test_trace_records_successful_phase_outputs_without_stdout(capsys):
    events = []
    check = quality(trace=events.append)
    try:
        result = check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert [event["phase"] for event in events] == [
        "translation_started", "translation_returned", "translation_parsed", "gate_passed",
        "review_started", "review_returned", "review_parsed", "spelling_started",
        "spelling_returned", "spelling_adjudicated", "accepted"]
    assert all(event["attempt"] == 1 for event in events)
    assert events[0]["context"]["source_untrusted"]["body"] == ORIGINAL
    assert json.loads(events[1]["raw"]) == TRANSLATION
    assert events[6]["review"] == APPROVAL
    assert events[8]["matches"] == []
    assert events[-1]["result"] == result
    assert capsys.readouterr().out == ""


def test_trace_exposes_numerical_repair_and_attempt_boundaries():
    events = []
    translator = Model({**TRANSLATION, "body": TRANSLATION["body"].replace("3", "4")}, TRANSLATION)
    check = quality(translator, trace=events.append)
    try:
        check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    failed_gate = next(event for event in events if event["phase"] == "gate_rejected")
    assert failed_gate["attempt"] == 1 and failed_gate["error"] == "TRANSLATION_NUMBER_CHANGED"
    second_call = next(event for event in events
                       if event["phase"] == "translation_started" and event["attempt"] == 2)
    feedback = second_call["context"]["repair_feedback"]
    assert feedback["code"] == "TRANSLATION_NUMBER_CHANGED"
    assert feedback["original_numbers"] == ["3"] and feedback["translated_numbers"] == ["4"]
    assert events[-1]["phase"] == "accepted" and events[-1]["attempt"] == 2


def test_trace_records_review_spelling_feedback_and_final_rejection():
    events = []
    review = {"approved": False, "topic": "science", "issues": ["Changed meaning"]}
    check = quality(reviewer=Model(review), matches=[{"message": "Ошибка"}], trace=events.append)
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert sum(event["phase"] == "retry" for event in events) == 3
    assert events[-1] == {"phase": "rejected", "attempt": 3, "error": "ARTICLE_QUALITY_REJECTED",
        "feedback": {"review_approved": False, "topic": "science",
                     "review_issues": ["Changed meaning"], "spelling_matches": [{"message": "Ошибка"}]}}
    assert not any(event["phase"] == "accepted" for event in events)


def test_trace_retains_bad_json_response_for_diagnosis():
    events = []
    check = quality(translator=Model("not JSON", TRANSLATION), trace=events.append)
    try:
        check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    invalid = next(event for event in events if event["phase"] == "translation_invalid")
    assert invalid == {"phase": "translation_invalid", "attempt": 1,
                       "error": "INVALID_EDITOR_RESULT", "raw": "not JSON"}


@pytest.mark.parametrize("failure_phase", ["translation_started", "accepted"])
def test_trace_write_failure_never_returns_approved_content(failure_phase):
    translator = Model(TRANSLATION)

    def broken_trace(event):
        if event["phase"] == failure_phase:
            raise OSError("private diagnostic disk failure")

    check = quality(translator, trace=broken_trace)
    try:
        with pytest.raises(AgentError, match="ARTICLE_TRACE_FAILED"):
            check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert len(translator.prompts) == (0 if failure_phase == "translation_started" else 1)


def test_trace_observer_cannot_modify_content_or_feedback():
    def mutating_trace(event):
        if event["phase"] == "translation_parsed":
            event["translation"]["body"] = "Подмена текста наблюдателем."
        elif event["phase"] == "accepted":
            event["result"]["body"] = "Подмена результата наблюдателем."

    check = quality(trace=mutating_trace)
    try:
        result = check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert TRANSLATION["body"] in result["body"]


def test_rejected_candidate_is_supplied_with_linguistic_evidence_for_exact_repair():
    rejected = {**TRANSLATION, "body": TRANSLATION["body"].replace(
        "изучили свет", "наблюдали и интересовались светом")}
    issues = ["Coordinated verbs have incompatible complements; restore idiomatic grammar and source meaning."]
    translator = Model(rejected, TRANSLATION)
    reviewer = Model({"approved": False, "topic": "science", "issues": issues}, APPROVAL)
    check = quality(translator, reviewer)
    try:
        result = check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert len(translator.prompts) == len(reviewer.prompts) == 2
    second_prompt = translator.prompts[1]
    assert second_prompt["previous_translation"] == rejected
    assert second_prompt["repair_feedback"]["review_issues"] == issues
    assert second_prompt["source_untrusted"]["body"] == ORIGINAL
    assert reviewer.prompts[1]["translation_untrusted"] == result["translation"]
    assert result["translation"] == TRANSLATION["title"] + "\n\n" + TRANSLATION["body"]
    assert "repair that exact prior" in second_prompt["instruction"]


def scientific_match(text):
    start = text.index("Магелланово")
    return {"offset": len(text[:start].encode("utf-16-le")) // 2, "length": len("Магелланово"),
        "type": {"typeName": "UnknownWord"},
        "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling"}}


@pytest.mark.parametrize(("russian", "english"), [
    ("Большое Магелланово облако", "Large Magellanic Cloud"),
    ("Малое Магелланово облако", "Small Magellanic Cloud"),
])
def test_only_corresponding_documented_full_scientific_phrase_is_adjudicated(russian, english):
    text = "Научные наблюдения: " + russian + "."
    match = scientific_match(text)
    accepted, unresolved = adjudicate_scientific_spelling(english, text, [match])
    assert unresolved == [] and len(accepted) == 1
    assert accepted[0]["match"] == match
    assert accepted[0]["evidence_url"] == MAGELLANIC_EVIDENCE
    assert accepted[0]["phrase"] == russian and accepted[0]["source_term"] == english


@pytest.mark.parametrize(("original", "text", "change"), [
    ("Small Magellanic Cloud", "Большое Магелланово облако", {}),
    ("Large Magellanic Cloud", "Малое Магелланово облако", {}),
    ("Another distant galaxy", "Большое Магелланово облако", {}),
    ("Large Magellanic Cloud", "Звезда Магелланово находится далеко", {}),
    ("Large Magellanic Cloud", "Большое Магелланово облаком", {}),
    ("Large Magellanic Cloud", "Большое Магелланово облако", {"offset": 0}),
    ("Large Magellanic Cloud", "Большое Магелланово облако", {"length": 10}),
    ("Large Magellanic Cloud", "Большое Магелланово облако", {"offset": True}),
    ("Large Magellanic Cloud", "Большое Магелланово облако", {"rule": {"id": "OTHER", "issueType": "misspelling"}}),
    ("Large Magellanic Cloud", "Большое Магелланово облако", {"rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "grammar"}}),
    ("Large Magellanic Cloud", "Большое Магелланово облако", {"type": {"typeName": "Other"}}),
])
def test_scientific_exception_rejects_mismatched_source_context_offsets_and_rule(original, text, change):
    match = {**scientific_match(text), **change}
    accepted, unresolved = adjudicate_scientific_spelling(original, text, [match])
    assert accepted == [] and unresolved == [match]


def test_scientific_exception_uses_languagetool_utf16_offsets():
    text = "🔭 Большое Магелланово облако"
    match = scientific_match(text)
    assert adjudicate_scientific_spelling("Large Magellanic Cloud", text, [match])[1] == []
    assert adjudicate_scientific_spelling("Large Magellanic Cloud", text,
        [{**match, "offset": text.index("Магелланово")}])[0] == []


def test_scientific_adjudication_preserves_raw_trace_and_other_findings_still_block():
    translated = {"title": "Наблюдения соседней галактики",
        "body": "Большое Магелланово облако — соседняя галактика. Учёные исследуют её звёзды и наблюдают за их светом."}
    text = translated["title"] + "\n\n" + translated["body"]
    original = "The Large Magellanic Cloud is a nearby galaxy. Scientists study its stars and observe their light."
    match = scientific_match(text)
    events = []
    check = quality(translator=Model(translated), matches=[match], trace=events.append)
    try:
        result = check.prepare("Observations of a nearby galaxy", original, URL)
    finally:
        check.close()
    assert translated["body"] in result["body"]
    assert next(event for event in events if event["phase"] == "spelling_returned")["matches"] == [match]
    decision = next(event for event in events if event["phase"] == "spelling_adjudicated")
    assert decision["unresolved"] == []
    assert decision["adjudicated"][0]["evidence_url"] == MAGELLANIC_EVIDENCE
    unrelated = {"message": "Другая опечатка", "offset": 0, "length": 3,
                 "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling"}}
    check = quality(translator=Model(translated), matches=[match, unrelated])
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            check.prepare("Observations of a nearby galaxy", original, URL)
    finally:
        check.close()


def test_exact_editorial_candidate_is_checked_without_translator_call():
    events = []
    translator, reviewer = Model("Must never be called"), Model(APPROVAL)
    check = quality(translator, reviewer, trace=events.append)
    try:
        result = check.check_candidate(TRANSLATION["title"], TRANSLATION["body"], TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert result["translation"] == TRANSLATION["title"] + "\n\n" + TRANSLATION["body"]
    assert result["original"] == TITLE + "\n\n" + ORIGINAL
    assert result["url"] == URL and result["topic"] == "science"
    assert result["body"].endswith(URL + "\nПеревод и редактура с помощью нейросетей.")
    assert not translator.prompts and len(reviewer.prompts) == 1
    assert reviewer.prompts[0]["translation_untrusted"] == result["translation"]
    assert events[0]["phase"] == "candidate_started" and events[-1]["phase"] == "accepted"
    assert not any(event["phase"] == "translation_started" for event in events)


@pytest.mark.parametrize(("changes", "expected"), [
    ({"body": TRANSLATION["body"].replace("3", "4")}, "TRANSLATION_NUMBER_CHANGED"),
    ({"body": TRANSLATION["body"] * 35}, "ARTICLE_POST_TOO_LONG"),
    ({"title": "English title", "body": ORIGINAL}, "TRANSLATION_NOT_RUSSIAN"),
    ({"title": ""}, "ARTICLE_TRANSLATION_INVALID"),
    ({"url": "https://127.0.0.1/article"}, "ARTICLE_URL_INVALID"),
    ({"original": "Source too short"}, "ARTICLE_INPUT_SIZE_INVALID"),
])
def test_invalid_exact_candidate_stops_before_any_model(changes, expected):
    translator, reviewer = Model(TRANSLATION), Model(APPROVAL)
    check = quality(translator, reviewer)
    candidate = {"title": TRANSLATION["title"], "body": TRANSLATION["body"],
                 "original_title": TITLE, "original": ORIGINAL, "url": URL, **changes}
    try:
        with pytest.raises(AgentError, match=expected):
            check.check_candidate(**candidate)
    finally:
        check.close()
    assert not translator.prompts and not reviewer.prompts


@pytest.mark.parametrize(("review", "matches", "status", "error"), [
    ({"approved": False, "topic": "science", "issues": ["Изменён временной смысл."]}, [], 200,
     "ARTICLE_QUALITY_REJECTED"),
    (APPROVAL, [{"message": "Опечатка"}], 200, "ARTICLE_QUALITY_REJECTED"),
    (APPROVAL, [], 503, "ARTICLE_SPELLING_UNAVAILABLE"),
    ({"approved": True, "topic": "other", "issues": []}, [], 200, "ARTICLE_QUALITY_REJECTED"),
    ({"approved": "true", "topic": "science", "issues": []}, [], 200, "ARTICLE_QUALITY_REJECTED"),
    ("```json {incomplete", [], 200, "ARTICLE_QUALITY_REJECTED"),
])
def test_exact_candidate_review_and_spelling_failures_never_trigger_translation(review, matches, status, error):
    translator, reviewer = Model(TRANSLATION), Model(review)
    check = quality(translator, reviewer, matches=matches, status=status)
    try:
        with pytest.raises(AgentError, match=error):
            check.check_candidate(TRANSLATION["title"], TRANSLATION["body"], TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert not translator.prompts and len(reviewer.prompts) == 1


def test_candidate_trace_failure_cannot_return_success():
    def unavailable_trace(event):
        if event["phase"] == "accepted":
            raise OSError("private trace storage unavailable")

    translator = Model(TRANSLATION)
    check = quality(translator, trace=unavailable_trace)
    try:
        with pytest.raises(AgentError, match="ARTICLE_TRACE_FAILED"):
            check.check_candidate(TRANSLATION["title"], TRANSLATION["body"], TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert not translator.prompts


def test_numeric_format_feedback_repairs_time_notation_without_relaxing_gate():
    original = "The observatory recorded a flash at 11:59 p.m. Researchers checked the image before sharing the observation."
    converted = {"title": "Наблюдение вспышки",
        "body": "Обсерватория зарегистрировала вспышку в 23:59. Исследователи проверили изображение, прежде чем сообщить о наблюдении."}
    preserved = {**converted, "body": converted["body"].replace("23:59", "11:59 вечера")}
    translator, reviewer = Model(converted, preserved), Model(APPROVAL)
    check = quality(translator, reviewer)
    try:
        result = check.prepare("An observatory records a flash", original, URL)
    finally:
        check.close()
    assert len(translator.prompts) == 2 and len(reviewer.prompts) == 1
    feedback = translator.prompts[1]["repair_feedback"]
    assert feedback["code"] == "TRANSLATION_NUMBER_CHANGED"
    assert feedback["original_numbers"] == ["11", "59"]
    assert feedback["translated_numbers"] == ["23", "59"]
    assert "Do not convert units, times or number formats" in feedback["advice"]
    assert translator.prompts[1]["previous_translation"] == converted
    assert "11:59 вечера" in result["translation"] and "23:59" not in result["translation"]
