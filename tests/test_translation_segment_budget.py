"""Explicit smaller MADLAD segments retain full independent article QA."""
import hashlib
import json
from urllib.parse import parse_qs

import httpx
import pytest

from max_postiz_agent.article_quality import ArticleQuality
from max_postiz_agent.config import AgentError
from max_postiz_agent.translation import (DedicatedTranslator, MADLAD_ARTIFACT_SHA256,
    MADLAD_BACKEND_CONTRACT, MADLAD_MODEL, MADLAD_SEGMENTATION_CONTRACT)


def metrics():
    return {"backend_contract": MADLAD_BACKEND_CONTRACT, "artifact_sha256": MADLAD_ARTIFACT_SHA256,
            "seconds": 1}


@pytest.mark.parametrize("limit", [399, 1401, 600.0, "600", True, None])
def test_segment_budget_requires_bounded_exact_integer(limit):
    with pytest.raises(AgentError, match="TRANSLATION_CONFIG_INVALID"):
        DedicatedTranslator({"model": MADLAD_MODEL, "base_url": "http://127.0.0.1:8094", "segment_chars": limit})


@pytest.mark.parametrize("model", ["hy-mt2-7b-q8", "hy-mt2-1.8b-q8", "translategemma-12b-q4", "opus-mt-en-ru-int8"])
def test_nondefault_budget_does_not_change_other_model_routes(model):
    with pytest.raises(AgentError, match="TRANSLATION_CONFIG_INVALID"):
        DedicatedTranslator({"model": model, "base_url": "http://127.0.0.1:8094", "segment_chars": 600})


@pytest.mark.parametrize("explicit", [False, True])
def test_default_budget_keeps_legacy_metrics_exactly(explicit):
    config = {"model": MADLAD_MODEL, "base_url": "http://127.0.0.1:8094"}
    if explicit:
        config["segment_chars"] = 1400
    translator = DedicatedTranslator(config, transport=httpx.MockTransport(lambda _: httpx.Response(200,
        json={"model": MADLAD_MODEL, "complete": True, "text": "Точный перевод.", "metrics": metrics()})))
    try:
        _, info = translator.generate_text(json.dumps({"source_untrusted": {"title": "Title", "body": "Body."}}))
    finally:
        translator.client.close()
    assert info == {"model": MADLAD_MODEL, "segments": [{**metrics(), "numeric_spelling_restored": 0}] * 2,
                    "cache_reused": False}


def test_smaller_budget_hashes_all_requests_and_never_emits_plaintext_in_metrics():
    calls = []
    source = "A complete scientific sentence is preserved without truncating any final observation. " * 30

    def translate(request):
        value = json.loads(request.content)
        calls.append(value["text"])
        return httpx.Response(200, json={"model": MADLAD_MODEL, "complete": True,
            "text": "Перевод завершён.", "metrics": metrics()})

    translator = DedicatedTranslator({"model": MADLAD_MODEL, "base_url": "http://127.0.0.1:8094",
        "segment_chars": 600}, transport=httpx.MockTransport(translate))
    try:
        _, info = translator.generate_text(json.dumps({"source_untrusted": {"title": "A title", "body": source}}))
        _, cached = translator.generate_text(json.dumps({"source_untrusted": {"title": "A title", "body": source}}))
    finally:
        translator.client.close()
    assert info["segmentation_contract"] == MADLAD_SEGMENTATION_CONTRACT and info["segment_chars"] == 600
    assert len(calls) > 3 and all(len(part) <= 600 for part in calls)
    assert cached == {**info, "cache_reused": True}
    assert len(info["segments"]) == len(calls)
    for index, (part, saved) in enumerate(zip(calls, info["segments"], strict=True)):
        assert saved["segment_index"] == index
        assert saved["segment_input_sha256"] == hashlib.sha256(part.encode()).hexdigest()
        assert saved["segment_output_sha256"] == hashlib.sha256("Перевод завершён.".encode()).hexdigest()
        assert saved["segment_input_chars"] == len(part)
        assert saved["segment_field"] == ("title" if index == 0 else "body")
        assert part not in json.dumps(saved, ensure_ascii=False)
    assert " ".join(calls[1:]).strip() == source.strip()


@pytest.mark.parametrize("defect", [None, "number", "review", "spelling"])
def test_actual_600_route_preserves_whole_article_review_spelling_and_hard_gates(defect):
    title = "A classroom sensor records cooling"
    english = [
        "During a classroom experiment a glass jar stood on a wooden table while a sensor recorded its temperature, "
        "and the students carefully checked that the lamp and every component of the instrument were working correctly.",
        "After the lamp was switched off the jar slowly cooled for 3 hours, while the students compared the sensor readings "
        "with their earlier observations and preserved the original records without changing the experimental conditions.",
        "The calibration on the wooden table confirmed that the observed cooling was consistent across the complete "
        "set of measurements, and the researchers reported the results together with the limitations of the experiment.",
        "The team explained that these classroom observations demonstrate a measured change in temperature rather than "
        "a change in the composition of the glass, and this distinction was preserved in the final scientific report.",
    ]
    russian = [
        "Во время учебного опыта стеклянная банка стояла на деревянном столе, а датчик записывал её температуру. "
        "Ученики тщательно проверили правильную работу лампы и всех компонентов прибора.",
        "После выключения лампы банка медленно остывала 3 часа. Ученики сравнивали показания датчика с прежними "
        "наблюдениями и сохраняли исходные записи, не меняя условий опыта.",
        "Калибровка на деревянном столе подтвердила согласованность наблюдаемого охлаждения во всём наборе измерений. "
        "Исследователи сообщили результаты вместе с ограничениями опыта.",
        "Команда объяснила, что учебные наблюдения показывают измеренное изменение температуры, а не изменение "
        "состава стекла. Это различие сохранилось в окончательном научном отчёте.",
    ]
    if defect == "number":
        russian[1] = russian[1].replace("3 часа", "4 часа")
    if defect == "spelling":
        russian[0] = russian[0].replace("деревянном", "деревяном")
    original, expected_body = " ".join(english), " ".join(russian)
    ru_title = "Учебный датчик записывает охлаждение"
    expected = ru_title + "\n\n" + expected_body
    calls, reviews, spells, events = [], [], [], []

    def translate(request):
        part = json.loads(request.content)["text"]
        calls.append(part)
        if part == title:
            translated = ru_title
        else:
            owned = [ru for en, ru in zip(english, russian, strict=True) if en in part]
            assert owned, "The fixture requires actual complete source sentences, not a translator stub."
            translated = " ".join(owned)
        return httpx.Response(200, json={"model": MADLAD_MODEL, "complete": True, "text": translated,
                                        "metrics": metrics()})

    class IndependentReviewer:
        model = "independent-ministral"

        def generate_text(self, prompt):
            value = json.loads(prompt)
            reviews.append(value)
            assert value["source_untrusted"] == title + "\n\n" + original
            assert value["translation_untrusted"] == expected
            return json.dumps({"approved": defect != "review", "topic": "science", "warnings": [],
                "issues": ["The candidate changes a source fact."] if defect == "review" else []}), {}

    def spelling(request):
        observed = parse_qs(request.content.decode())["text"][0]
        spells.append(observed)
        assert observed == expected
        matches = []
        if defect == "spelling":
            matches = [{"offset": expected.index("деревяном"), "length": len("деревяном"),
                "rule": {"id": "MORFOLOGIK_RULE_RU_RU", "issueType": "misspelling", "category": {"id": "TYPOS"}},
                "type": {"typeName": "UnknownWord"}}]
        return httpx.Response(200, json={"matches": matches})

    translator = DedicatedTranslator({"model": MADLAD_MODEL, "base_url": "http://127.0.0.1:8094",
        "segment_chars": 600}, transport=httpx.MockTransport(translate))
    quality = ArticleQuality(translator, IndependentReviewer(), review_rubric="meaning-first-v1",
        transport=httpx.MockTransport(spelling), trace=events.append)
    try:
        if defect is None:
            result = quality.prepare(title, original, "https://science.nasa.gov/observations/")
            assert result["translation"] == expected
        else:
            with pytest.raises(AgentError, match="ARTICLE_QUALITY_REJECTED"):
                quality.prepare(title, original, "https://science.nasa.gov/observations/")
    finally:
        quality.close()
        translator.client.close()
    assert len(calls) >= 3 and all(len(part) <= 600 for part in calls)
    assert all(sum(sentence in part for part in calls[1:]) == 1 for sentence in english)
    translated_event = next(event for event in events if event["phase"] == "translation_returned")
    assert translated_event["translation_metadata"]["segmentation_contract"] == MADLAD_SEGMENTATION_CONTRACT
    assert len(reviews) == len(spells) == (0 if defect == "number" else 1)
    assert not any(event["phase"] == "accepted" for event in events) if defect else events[-1]["phase"] == "accepted"
