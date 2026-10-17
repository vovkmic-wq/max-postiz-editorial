import json

import httpx
import pytest

from max_postiz_agent.article_quality import ArticleQuality, canonical_article_url
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


def quality(translator=None, reviewer=None, matches=None, status=200):
    return ArticleQuality(translator or Model(TRANSLATION), reviewer or Model(APPROVAL),
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
    assert translator.prompts[1]["repair_feedback"] == "TRANSLATION_NUMBER_CHANGED"


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
