"""An unchanged dedicated translation cannot shop for a verifiable review."""

import json

import httpx
import pytest

from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.config import AgentError
from max_postiz_agent.translation import DedicatedTranslator

TITLE = "A classroom experiment records cooling"
ORIGINAL = (
    "During a classroom experiment, a glass jar stood on a wooden table. "
    "After the lamp was switched off, the jar slowly cooled while a sensor recorded its temperature."
)
TRANSLATION = {"title": "Учебный опыт с охлаждением",
               "body": "Во время учебного опыта стеклянная банка стояла на деревянном столе. "
                       "После выключения лампы банка медленно остывала, а датчик фиксировал её температуру."}
APPROVAL = {"approved": True, "topic": "science", "issues": []}
URL = "https://example.invalid/classroom-experiment/"


class Reviewer:
    model = "independent-reviewer"

    def __init__(self, *responses):
        self.responses = responses
        self.prompts = []

    def generate_text(self, prompt):
        self.prompts.append(json.loads(prompt))
        response = self.responses[min(len(self.prompts) - 1, len(self.responses) - 1)]
        if isinstance(response, Exception):
            raise response
        return response if isinstance(response, str) else json.dumps(response, ensure_ascii=False), {}


def dedicated_quality(reviewer, events):
    translations, spelling = [], []

    def translate(request):
        value = json.loads(request.content)
        translations.append(value)
        return httpx.Response(200, json={"model": value["model"], "complete": True,
            "text": TRANSLATION["title"] if value["text"] == TITLE else TRANSLATION["body"]})

    def spell(request):
        spelling.append(request)
        return httpx.Response(200, json={"matches": []})

    translator = DedicatedTranslator(
        {"model": "hy-mt2-7b-q8", "base_url": "http://127.0.0.1:8094"},
        transport=httpx.MockTransport(translate))
    check = ArticleQuality(translator, reviewer, trace=events.append,
                           transport=httpx.MockTransport(spell))
    return check, translations, spelling


@pytest.mark.parametrize("invalid", [
    "not JSON; the review has no verifiable verdict",
    {"approved": "true", "topic": "science", "issues": []},
    {"approved": True, "issues": []},
    {**APPROVAL, "unexpected": "an invalid schema cannot grant approval"},
])
def test_unverifiable_dedicated_review_cannot_be_replaced_by_later_approval(invalid, capsys):
    reviewer, events = Reviewer(invalid, APPROVAL), []
    check, translations, spelling = dedicated_quality(reviewer, events)
    try:
        assert check.translator.immutable_candidates is True
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
        check.translator.client.close()
    assert len(reviewer.prompts) == 1
    assert len(translations) == 2  # Exactly one title/body translation; subsequent calls reuse it.
    assert not spelling
    assert not any(event["phase"] == "accepted" for event in events)
    raw = invalid if isinstance(invalid, str) else json.dumps(invalid, ensure_ascii=False)
    assert next(event for event in events if event["phase"] == "review_returned")["raw"] == raw
    assert events[-1]["feedback"]["raw_review_untrusted"] == raw
    if isinstance(invalid, dict):
        assert events[-1]["feedback"]["review_untrusted"] == invalid
    assert sum(event["phase"] == "candidate_repeated" for event in events) == 2
    assert not capsys.readouterr().out


def test_negative_dedicated_review_keeps_findings_and_spelling_without_resampling():
    issue = "The candidate changes a fact in the source."
    rejection = {"approved": False, "topic": "science", "issues": [{"issue": issue}]}
    reviewer, events = Reviewer(rejection, APPROVAL), []
    check, translations, spelling = dedicated_quality(reviewer, events)
    try:
        with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
            check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
        check.translator.client.close()
    assert len(reviewer.prompts) == len(spelling) == 1
    assert len(translations) == 2
    assert events[-1]["feedback"]["review_approved"] is False
    assert events[-1]["feedback"]["review_issues"] == [issue]
    assert next(event for event in events if event["phase"] == "review_returned")["raw"] == (
        json.dumps(rejection, ensure_ascii=False))


def test_changed_candidate_after_unverifiable_review_requires_its_own_review():
    revised = {**TRANSLATION, "body": TRANSLATION["body"].replace("фиксировал", "регистрировал")}

    class ChangingTranslator(Reviewer):
        # The immutable marker must block the exact text, not every future candidate.
        immutable_candidates = True
        model = "dedicated-translation-contract"

    translator = ChangingTranslator(TRANSLATION, revised)
    reviewer, events = Reviewer("not JSON", APPROVAL), []
    check = ArticleQuality(translator, reviewer, trace=events.append,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": []})))
    try:
        result = check.prepare(TITLE, ORIGINAL, URL)
    finally:
        check.close()
    assert len(reviewer.prompts) == 2
    assert reviewer.prompts[0]["translation_untrusted"] != reviewer.prompts[1]["translation_untrusted"]
    assert reviewer.prompts[1]["translation_untrusted"] == result["translation"]
    assert translator.prompts[1]["repair_feedback"]["raw_review_untrusted"] == "not JSON"
    assert result["translation"] == revised["title"] + "\n\n" + revised["body"]


@pytest.mark.parametrize("code", ["OPENCODE_PROVIDER_UNAVAILABLE", "INVALID_EDITOR_RESULT"])
@pytest.mark.parametrize("phase", ["prepare", "candidate"])
def test_infrastructure_503_propagates_without_quality_rejection_or_retry(code, phase):
    unavailable = AgentError(code, 503)
    reviewer, events = Reviewer(unavailable, APPROVAL), []
    check, translations, spelling = dedicated_quality(reviewer, events)
    try:
        with pytest.raises(AgentError) as failure:
            if phase == "prepare":
                check.prepare(TITLE, ORIGINAL, URL)
            else:
                check.check_candidate(TRANSLATION["title"], TRANSLATION["body"], TITLE, ORIGINAL, URL)
    finally:
        check.close()
        check.translator.client.close()
    assert failure.value is unavailable and failure.value.status == 503
    assert len(reviewer.prompts) == 1 and len(translations) == (2 if phase == "prepare" else 0)
    assert not spelling
    assert events[-1]["phase"] == "review_failed"
    assert not any(event["phase"] in {"retry", "rejected", "accepted"} for event in events)
