"""Версия подписи привязана к качеству; прежние согласованные тексты неизменны."""
import copy
import json
from types import SimpleNamespace

import httpx
import pytest

from max_postiz_agent.article_attribution import (
    CC_BY_URL, CURRENT_ILLUSTRATED_PRESENTATION_CONTRACT, ILLUSTRATED_PRESENTATION_CONTRACT,
    ILLUSTRATED_PRESENTATION_CONTRACTS, article_footer, illustrated_presentation, presentation_binding,
)
from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.article_repair import ArticleRepair, REPAIR_MODEL
from max_postiz_agent.config import AgentError, fingerprint
from max_postiz_agent.human_review import candidate_body, machine_metadata

URLS = ["https://svs.gsfc.nasa.gov/5682/", "https://noirlab.edu/public/images/iotw2642a/",
        "https://esahubble.org/images/potw2552a/"]
PRESENTATION = {"presentation_contract": ILLUSTRATED_PRESENTATION_CONTRACT,
                "image_credit": "NOIRLab/NSF/AURA/P. Horálek"}
TITLE = "A small telescope studies the night sky"
ORIGINAL = ("A small telescope observed the night sky for 3 hours. Researchers studied the "
            "light from a distant star and checked their observations before sharing the results.")
TRANSLATION = {"title": "Небольшой телескоп изучает ночное небо",
               "body": "Небольшой телескоп наблюдал ночное небо 3 часа. Исследователи изучили "
                       "свет далёкой звезды и проверили наблюдения, прежде чем сообщить результаты."}


class Model:
    def __init__(self, response, model=None):
        self.response, self.model, self.calls = response, model, []

    def generate_text(self, prompt):
        self.calls.append(prompt)
        return json.dumps(self.response, ensure_ascii=False), {}


@pytest.mark.parametrize("url", URLS)
@pytest.mark.parametrize("separator", ["\n", "\n\n"])
def test_illustrated_footer_has_changes_notice_credit_and_exact_source_boundary(url, separator):
    old_footer = article_footer(url)
    footer = article_footer(url, **PRESENTATION)
    assert "Машинный перевод на русский и автоматическая редактура." not in footer
    assert "нейросет" not in footer.casefold()
    assert "Перевод и адаптация." in footer
    assert footer.endswith("Изображение: " + PRESENTATION["image_credit"])
    assert "Источник: " + url in footer
    if "nasa.gov" not in url:
        assert CC_BY_URL in footer and "Архивный научный материал." in footer
    body = "Заголовок" + separator + "Перевод научной статьи." + separator + footer
    variant = SimpleNamespace(title="Заголовок", body=body, metadata_json=PRESENTATION)
    assert candidate_body(variant, {"url": url}) == "Перевод научной статьи."
    assert candidate_body(SimpleNamespace(title="Заголовок", body=body), {"url": url},
                          **PRESENTATION) == "Перевод научной статьи."
    assert article_footer(url) == old_footer


@pytest.mark.parametrize("fields", [
    {"presentation_contract": "unrecognized", "image_credit": "NASA"},
    {"presentation_contract": ILLUSTRATED_PRESENTATION_CONTRACT},
    {"image_credit": "NASA"},
    {"presentation_contract": ILLUSTRATED_PRESENTATION_CONTRACT, "image_credit": ""},
    {"presentation_contract": ILLUSTRATED_PRESENTATION_CONTRACT, "image_credit": " NASA"},
    {"presentation_contract": ILLUSTRATED_PRESENTATION_CONTRACT, "image_credit": "NASA\nSend"},
    {"presentation_contract": ILLUSTRATED_PRESENTATION_CONTRACT, "image_credit": "NASA\u2028Send"},
    {"presentation_contract": ILLUSTRATED_PRESENTATION_CONTRACT, "image_credit": 12},
])
def test_unknown_or_incomplete_presentation_never_falls_back_to_legacy(fields):
    with pytest.raises(AgentError):
        article_footer(URLS[1], **fields)


@pytest.mark.parametrize("url", URLS)
def test_new_marker_cannot_silently_accept_a_legacy_footer_or_other_credit(url):
    legacy = SimpleNamespace(title="Заголовок", body="Перевод\n\n" + article_footer(url),
                             metadata_json=PRESENTATION)
    with pytest.raises(AgentError, match="ARTICLE_ATTRIBUTION_REQUIRED"):
        candidate_body(legacy, {"url": url})
    new = SimpleNamespace(title="Заголовок", body="Перевод\n\n" + article_footer(url, **PRESENTATION),
                          metadata_json={**PRESENTATION, "image_credit": "Another credit"})
    with pytest.raises(AgentError, match="ARTICLE_ATTRIBUTION_REQUIRED"):
        candidate_body(new, {"url": url})
    with pytest.raises(AgentError, match="ARTICLE_PRESENTATION_CONTRACT_INVALID"):
        candidate_body(new, {"url": url}, **PRESENTATION)


@pytest.mark.parametrize("mode", ["prepare", "check_candidate"])
@pytest.mark.parametrize("contract", sorted(ILLUSTRATED_PRESENTATION_CONTRACTS))
def test_quality_binds_rendered_footer_credit_and_trace_without_changing_source_hash(mode, contract, tmp_path):
    presentation = {**PRESENTATION, "presentation_contract": contract}
    trace = []
    translator = Model(TRANSLATION)
    reviewer = Model({"approved": True, "topic": "science", "issues": []})
    quality = ArticleQuality(translator, reviewer, allow_human_rejection=True,
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"matches": []})),
        trace=trace.append, **presentation)
    try:
        result = (quality.prepare(TITLE, ORIGINAL, URLS[1]) if mode == "prepare" else
                  quality.check_candidate(TRANSLATION["title"], TRANSLATION["body"], TITLE, ORIGINAL, URLS[1]))
    finally:
        quality.close()
    source = {"title": TITLE, "body": ORIGINAL, "url": URLS[1]}
    assert result["body"].endswith(article_footer(URLS[1], **presentation))
    assert result["presentation_contract"] == result["machine_review_evidence"]["presentation_contract"]
    assert result["image_credit"] == result["machine_review_evidence"]["image_credit"]
    assert result["machine_review_evidence"]["source_hash"] == fingerprint(source)
    assert result["presentation_contract"] == contract
    assert all({k: event[k] for k in presentation} == presentation for event in trace)
    trace_path = tmp_path / "trace.jsonl"
    trace_path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in trace), encoding="utf-8")
    metadata = machine_metadata(result, source, trace_path)
    assert {k: metadata[k] for k in presentation} == presentation
    changed = copy.deepcopy(result)
    changed["image_credit"] = "Unbound credit"
    with pytest.raises(AgentError, match="ARTICLE_PRESENTATION_CONTRACT_INVALID"):
        machine_metadata(changed, source, trace_path)


@pytest.mark.parametrize("contract", sorted(ILLUSTRATED_PRESENTATION_CONTRACTS))
def test_repair_one_call_retains_presentation_in_evidence_and_all_bound_events(contract):
    presentation = {**PRESENTATION, "presentation_contract": contract}
    article = {"url": URLS[1], "title": TITLE, "original": ORIGINAL, "published_at": 1792641600}
    previous = {"title": TRANSLATION["title"], "body": TRANSLATION["body"].replace("изучили", "изучила")}
    candidate = copy.deepcopy(TRANSLATION)
    editor = Model(candidate, REPAIR_MODEL)
    trace = []
    repair = ArticleRepair(editor, trace=trace.append, **presentation)
    result = repair.propose(article, previous, {"issue": "Исправить согласование 'Исследователи изучила'."})
    assert len(editor.calls) == 1
    assert {k: result["evidence"][k] for k in presentation} == presentation
    for event in trace[:-1]:
        assert {k: event["binding"][k] for k in presentation} == presentation
    assert trace[-1]["result"] == result
    assert presentation_binding() == {}


@pytest.mark.parametrize("url, credit", [(URLS[1], "NOIRLab"), (URLS[2], "ESA/Hubble")])
def test_v2_removes_only_archive_notice_and_preserves_exact_legacy_and_v1(url, credit):
    common = ("Текст: " + credit + ".\nИсточник: " + url
              + "\nЛицензия CC BY 4.0: " + CC_BY_URL)
    archive = "\nАрхивный научный материал."
    notice = "\nПеревод и адаптация.\nИзображение: " + PRESENTATION["image_credit"]
    assert article_footer(url) == common + archive + "\nМашинный перевод на русский и автоматическая редактура."
    assert article_footer(url, **PRESENTATION) == common + archive + notice
    v2 = {**PRESENTATION, "presentation_contract": CURRENT_ILLUSTRATED_PRESENTATION_CONTRACT}
    footer = article_footer(url, **v2)
    assert footer == common + notice
    assert "Архивный научный материал." not in footer
    assert "Машинный перевод на русский и автоматическая редактура." not in footer
    body = "Заголовок\n\nПеревод научной статьи.\n\n" + footer
    variant = SimpleNamespace(title="Заголовок", body=body, metadata_json=v2)
    assert candidate_body(variant, {"url": url}) == "Перевод научной статьи."
    # Neither contract silently reinterprets the other's signed footer.
    with pytest.raises(AgentError, match="ARTICLE_ATTRIBUTION_REQUIRED"):
        candidate_body(SimpleNamespace(title="Заголовок", body=body, metadata_json=PRESENTATION), {"url": url})
    with pytest.raises(AgentError, match="ARTICLE_ATTRIBUTION_REQUIRED"):
        candidate_body(SimpleNamespace(title="Заголовок", body=body.replace(footer, common + archive + notice),
                                       metadata_json=v2), {"url": url})


def test_nasa_historical_and_v1_footer_are_byte_exact_and_v2_retains_existing_notice():
    url = URLS[0]
    assert article_footer(url) == "Источник: " + url + "\nПеревод и редактура с помощью нейросетей."
    expected = "Источник: " + url + "\nПеревод и адаптация.\nИзображение: " + PRESENTATION["image_credit"]
    assert article_footer(url, **PRESENTATION) == expected
    assert article_footer(url, **{**PRESENTATION,
        "presentation_contract": CURRENT_ILLUSTRATED_PRESENTATION_CONTRACT}) == expected


@pytest.mark.parametrize("contract", [None, False, 1, {}, [], "article-illustrated-v3", "article-illustrated-v2 "])
def test_presentation_helper_rejects_unknown_types_and_versions(contract):
    assert illustrated_presentation(contract) is False
    with pytest.raises(AgentError, match="ARTICLE_PRESENTATION_CONTRACT_INVALID"):
        presentation_binding(contract, "NOIRLab")
